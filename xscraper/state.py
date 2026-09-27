"""SQLite state: which posts we have, which days are done, run history.

This is the file that makes the whole thing idempotent. Deleting it does not
lose the markdown — it only makes the next run re-check the lookback window.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,
    day         TEXT NOT NULL,
    author      TEXT,
    folder      TEXT,
    sources     TEXT,
    created_at  TEXT,
    scraped_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_day ON posts(day);

CREATE TABLE IF NOT EXISTS days (
    day          TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    post_count   INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT,
    last_success TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    trigger      TEXT,
    fetched      INTEGER DEFAULT 0,
    new_posts    INTEGER DEFAULT 0,
    window_start TEXT,
    window_end   TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class State:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        # WAL survives an abrupt shutdown (lid closed, battery dead) far better.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- posts -----------------------------------------------------------
    def known_post_ids(self, ids: list[str]) -> dict[str, sqlite3.Row]:
        """Look up a batch of ids at once rather than one query per post."""
        found: dict[str, sqlite3.Row] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT * FROM posts WHERE id IN ({placeholders})", chunk
            ).fetchall()
            for row in rows:
                found[row["id"]] = row
        return found

    def record_post(
        self,
        post_id: str,
        day: date,
        author: str,
        folder: str,
        sources: list[str],
        created_at: datetime,
        scraped_at: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO posts"
            " (id, day, author, folder, sources, created_at, scraped_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                post_id,
                day.isoformat(),
                author,
                folder,
                ",".join(sorted(sources)),
                created_at.isoformat(),
                scraped_at.isoformat(),
            ),
        )
        self.conn.commit()

    def update_post_sources(self, post_id: str, sources: list[str]) -> None:
        self.conn.execute(
            "UPDATE posts SET sources = ? WHERE id = ?",
            (",".join(sorted(sources)), post_id),
        )
        self.conn.commit()

    def post_count_for_day(self, day: date) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE day = ?", (day.isoformat(),)
        ).fetchone()
        return int(row["n"])

    # --- days ------------------------------------------------------------
    def mark_day(
        self,
        day: date,
        status: str,
        when: datetime,
        note: str | None = None,
    ) -> None:
        """status is one of: complete, partial, failed."""
        count = self.post_count_for_day(day)
        success = when.isoformat() if status == "complete" else None
        self.conn.execute(
            """
            INSERT INTO days (day, status, post_count, last_attempt, last_success, note)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET
                status       = excluded.status,
                post_count   = excluded.post_count,
                last_attempt = excluded.last_attempt,
                last_success = COALESCE(excluded.last_success, days.last_success),
                note         = excluded.note
            """,
            (day.isoformat(), status, count, when.isoformat(), success, note),
        )
        self.conn.commit()

    def incomplete_days(self, since: date) -> list[date]:
        rows = self.conn.execute(
            "SELECT day FROM days WHERE status != 'complete' AND day >= ? ORDER BY day",
            (since.isoformat(),),
        ).fetchall()
        return [date.fromisoformat(row["day"]) for row in rows]

    def day_status(self, day: date) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM days WHERE day = ?", (day.isoformat(),)
        ).fetchone()
        return row["status"] if row else None

    # --- runs ------------------------------------------------------------
    def start_run(
        self, started_at: datetime, trigger: str, window_start: datetime, window_end: datetime
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, status, trigger, window_start, window_end)"
            " VALUES (?, 'running', ?, ?, ?)",
            (started_at.isoformat(), trigger, window_start.isoformat(), window_end.isoformat()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        finished_at: datetime,
        status: str,
        fetched: int = 0,
        new_posts: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, fetched=?, new_posts=?, error=?"
            " WHERE id=?",
            (finished_at.isoformat(), status, fetched, new_posts, error, run_id),
        )
        self.conn.commit()

    def last_successful_run_finish(self) -> datetime | None:
        row = self.conn.execute(
            "SELECT finished_at FROM runs WHERE status = 'success' AND finished_at IS NOT NULL"
            " ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        if not row or not row["finished_at"]:
            return None
        return datetime.fromisoformat(row["finished_at"])

    def reap_stale_runs(self, now: datetime, max_age_hours: int = 6) -> int:
        """A run killed mid-flight (shutdown, SIGKILL) leaves a 'running' row.

        Sweep those so they do not confuse the status report forever.
        """
        rows = self.conn.execute(
            "SELECT id, started_at FROM runs WHERE status = 'running'"
        ).fetchall()
        stale = 0
        for row in rows:
            started = datetime.fromisoformat(row["started_at"])
            if (now - started).total_seconds() > max_age_hours * 3600:
                self.conn.execute(
                    "UPDATE runs SET status='interrupted', finished_at=?,"
                    " error='process disappeared (shutdown or kill)' WHERE id=?",
                    (now.isoformat(), row["id"]),
                )
                stale += 1
        if stale:
            self.conn.commit()
        return stale

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- meta ------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()
