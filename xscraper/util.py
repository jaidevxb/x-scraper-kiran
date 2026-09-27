"""Date parsing, day-folder naming, atomic writes, locking, disk checks."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

log = logging.getLogger(__name__)

# X returns e.g. "Sat Sep 26 04:54:05 +0000 2026". We parse it by hand rather
# than with strptime("%a %b %d ...") because %a/%b are locale-dependent and this
# runs unattended on a machine whose locale we do not control.
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


class ParseError(ValueError):
    pass


def parse_twitter_datetime(value: str) -> datetime:
    """Parse X's created_at into a timezone-aware UTC datetime.

    Falls back to ISO-8601 so the code keeps working if the actor ever switches
    format on us.
    """
    if not value:
        raise ParseError("empty created_at")
    text = value.strip()
    parts = text.split()
    if len(parts) == 6 and parts[1] in _MONTHS:
        _, mon, day, clock, offset, year = parts
        hh, mm, ss = clock.split(":")
        sign = 1 if offset[0] != "-" else -1
        delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        tz = timezone(sign * delta)
        dt = datetime(
            int(year), _MONTHS[mon], int(day),
            int(hh), int(mm), int(ss), tzinfo=tz,
        )
        return dt.astimezone(timezone.utc)

    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ParseError(f"unrecognised created_at: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_folder_name(day: date, fmt: str) -> str:
    """'iso' -> 2026-09-15 ; 'long' -> 15 september."""
    if fmt == "long":
        return f"{day.day} {_MONTH_NAMES[day.month - 1]}"
    return day.isoformat()


def day_start(day: date, tz: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def to_utc_query_stamp(dt: datetime) -> str:
    """Format for X's since:/until: operators, e.g. 2026-09-15_00:00:00_UTC."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S_UTC")


def last_scheduled_trigger(now: datetime, hour: int, minute: int) -> datetime:
    """The most recent time the scheduled job should have fired, at or before now.

    This is what makes a run-at-login trigger safe: we only skip when a run
    already succeeded *after* the last scheduled moment.
    """
    today_trigger = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if today_trigger <= now:
        return today_trigger
    return today_trigger - timedelta(days=1)


def free_disk_mb(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free // (1024 * 1024)


def atomic_write_text(target: Path, content: str) -> None:
    """Write via a temp file in the same directory, then rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def replace_dir(staged: Path, target: Path) -> None:
    """Move a fully-built staging directory into place.

    os.replace() cannot replace a non-empty directory, so an existing target is
    moved aside first and only deleted once the new one has landed.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        os.replace(staged, target)
        return
    backup = target.with_name(target.name + ".old-" + os.urandom(4).hex())
    os.replace(target, backup)
    try:
        os.replace(staged, target)
    except BaseException:
        os.replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def sanitize_component(text: str, limit: int = 40) -> str:
    """Make a string safe for a file/folder name on macOS and Windows alike."""
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in text)
    safe = "-".join(part for part in safe.split("-") if part)
    return safe[:limit] or "unknown"


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    """Cross-platform advisory lock so two runs never overlap.

    A scheduled run and a manual run can collide; without this they would race
    on the same folders and the same SQLite file.
    """

    # Windows locks a byte *range*, not the whole file, so we lock one byte far
    # past the end of the data. Locking byte 0 instead would (a) depend on where
    # the file pointer happened to be and (b) make writing the pid record fail.
    _WINDOWS_LOCK_OFFSET = 1 << 30

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # r+ keeps the existing file; w+ creates it. Neither truncates behind a lock.
        mode = "r+" if self.path.exists() else "w+"
        self._handle = open(self.path, mode, encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(self._WINDOWS_LOCK_OFFSET)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise AlreadyRunning(
                f"Another run already holds {self.path}. Exiting without doing anything."
            ) from exc

        try:
            self._handle.seek(0)
            self._handle.write(
                f"pid={os.getpid()} started={datetime.now().isoformat()}".ljust(120) + "\n"
            )
            self._handle.flush()
        except OSError:
            # The stamp is only a debugging aid; holding the lock is what matters.
            log.debug("Could not stamp the lock file", exc_info=True)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(self._WINDOWS_LOCK_OFFSET)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best effort on release
            log.debug("Could not release lock cleanly", exc_info=True)
        finally:
            self._handle.close()
            self._handle = None
