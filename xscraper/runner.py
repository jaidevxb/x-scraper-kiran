"""The run itself: build queries, fetch, filter, dedupe, write, record state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .apify import ApifyClient, ApifyError, ApifyFatalError
from .config import Config
from .normalize import Post, normalize
from .notify import clear_last_error, desktop_notify, write_last_error
from .state import State
from .util import (
    day_folder_name,
    day_start,
    free_disk_mb,
    last_scheduled_trigger,
    to_utc_query_stamp,
)
from .writer import PostWriter

log = logging.getLogger(__name__)

SOURCE_USER = "user"
SOURCE_HASHTAG = "hashtag"


class DiskSpaceError(RuntimeError):
    pass


@dataclass
class RunSummary:
    skipped: bool = False
    skip_reason: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    fetched: int = 0
    written: int = 0
    duplicates: int = 0
    filtered: int = 0
    out_of_window: int = 0
    sources_patched: int = 0
    days_touched: dict[str, int] = field(default_factory=dict)
    query_errors: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        if self.skipped:
            return f"skipped: {self.skip_reason}"
        days = ", ".join(f"{day} (+{count})" for day, count in sorted(self.days_touched.items()))
        return (
            f"fetched={self.fetched} new={self.written} duplicates={self.duplicates} "
            f"filtered={self.filtered} outside_window={self.out_of_window} "
            f"days=[{days or 'none'}]"
            + (f" errors={len(self.query_errors)}" if self.query_errors else "")
        )


@dataclass
class Query:
    source: str
    search_term: str
    max_items: int


class Runner:
    def __init__(self, config: Config, state: State, client: ApifyClient) -> None:
        self.config = config
        self.state = state
        self.client = client
        self.writer = PostWriter(config)

    # --- window ----------------------------------------------------------
    def compute_window(
        self,
        now: datetime,
        start_day: date | None = None,
        end_day: date | None = None,
    ) -> tuple[datetime, datetime, date, date]:
        """Return (start_dt, end_dt, first_day, last_day) in the configured tz."""
        if start_day is None:
            start_day = now.date() - timedelta(days=self.config.lookback_days)
        if end_day is None:
            end_day = now.date()

        # Any day we previously failed on gets pulled back in, even if it has
        # fallen outside the normal lookback window.
        oldest_broken = self.state.incomplete_days(
            since=start_day - timedelta(days=60)
        )
        if oldest_broken and oldest_broken[0] < start_day:
            log.info("Extending window back to %s to retry incomplete day(s)", oldest_broken[0])
            start_day = oldest_broken[0]

        start_dt = day_start(start_day, self.config.tz)
        end_dt = min(now, day_start(end_day, self.config.tz) + timedelta(days=1))
        return start_dt, end_dt, start_day, end_day

    # --- queries ---------------------------------------------------------
    def build_queries(self, start_dt: datetime, end_dt: datetime) -> list[Query]:
        """One search term per source, spanning the whole window.

        One query per source (rather than one per source per day) matters: the
        actor bills a 50-tweet minimum *per query*, so splitting by day would
        multiply the floor cost for no benefit. Posts are routed to the right day
        folder afterwards from their own timestamp.

        The window is padded by a day on each side because since:/until: are
        evaluated in UTC while our day boundaries are local; exact filtering
        happens in Python.
        """
        since = to_utc_query_stamp(start_dt - timedelta(days=1))
        until = to_utc_query_stamp(end_dt + timedelta(days=1))
        window = f"since:{since} until:{until}"

        queries: list[Query] = []
        if self.config.handle:
            term = f"from:{self.config.handle} {window}"
            if not self.config.include_retweets:
                term += " -filter:nativeretweets"
            queries.append(Query(SOURCE_USER, term, self.config.max_posts_user))

        if self.config.hashtag:
            term = f"#{self.config.hashtag} {window}"
            if not self.config.include_retweets:
                term += " -filter:nativeretweets"
            if not self.config.include_hashtag_replies:
                term += " -filter:replies"
            queries.append(Query(SOURCE_HASHTAG, term, self.config.max_posts_hashtag))

        return queries

    def actor_input(self, query: Query) -> dict[str, Any]:
        return {
            "searchTerms": [query.search_term],
            "maxItems": query.max_items,
            "sort": "Latest",
            "includeSearchTerms": False,
        }

    # --- filtering -------------------------------------------------------
    def keep(self, post: Post, source: str) -> tuple[bool, str]:
        if post.is_retweet and not self.config.include_retweets:
            return False, "retweet"
        if post.is_reply:
            if source == SOURCE_USER and not self.config.include_user_replies:
                return False, "user reply"
            if source == SOURCE_HASHTAG and not self.config.include_hashtag_replies:
                return False, "hashtag reply"
        if source == SOURCE_USER and self.config.handle:
            # A `from:` search should only ever return the target account, but
            # guard against the actor bleeding in unrelated results.
            if post.author.lower() != self.config.handle.lower():
                return False, f"wrong author ({post.author})"
        return True, ""

    # --- the run ---------------------------------------------------------
    def should_run(self, now: datetime, force: bool, trigger: str) -> tuple[bool, str]:
        """Guard that makes the run-at-login trigger safe.

        We skip only when a run already succeeded *after* the most recent
        scheduled moment. So: fires at 22:00 as normal; a login at 23:00 the same
        evening does nothing; but a login the next morning after the Mac was off
        at 22:00 does run, because the last success predates that 22:00 trigger.
        """
        if force:
            return True, ""
        last_success = self.state.last_successful_run_finish()
        if last_success is None:
            return True, ""
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=self.config.tz)
        trigger_time = last_scheduled_trigger(
            now, self.config.schedule_hour, self.config.schedule_minute
        )
        if last_success > trigger_time:
            return False, (
                f"a run already succeeded at {last_success:%Y-%m-%d %H:%M} which is after the "
                f"last scheduled time ({trigger_time:%Y-%m-%d %H:%M}); use --force to override"
            )
        return True, ""

    def run(
        self,
        trigger: str = "manual",
        force: bool = False,
        start_day: date | None = None,
        end_day: date | None = None,
        dry_run: bool = False,
    ) -> RunSummary:
        config = self.config
        now = config.now()
        summary = RunSummary()

        self.state.reap_stale_runs(now)

        allowed, reason = self.should_run(now, force or start_day is not None, trigger)
        if not allowed:
            log.info("Nothing to do: %s", reason)
            summary.skipped = True
            summary.skip_reason = reason
            return summary

        free = free_disk_mb(config.base_dir)
        if free < config.min_free_disk_mb:
            raise DiskSpaceError(
                f"Only {free} MB free under {config.base_dir}; "
                f"MIN_FREE_DISK_MB is {config.min_free_disk_mb}. Refusing to run."
            )

        self.writer.cleanup_staging()

        start_dt, end_dt, first_day, last_day = self.compute_window(now, start_day, end_day)
        summary.window_start, summary.window_end = start_dt, end_dt
        log.info(
            "Window %s -> %s (%s), days %s..%s",
            start_dt.isoformat(), end_dt.isoformat(), config.tz_name, first_day, last_day,
        )

        run_id = self.state.start_run(now, trigger, start_dt, end_dt)
        queries = self.build_queries(start_dt, end_dt)

        # id -> Post, merging sources so a post matching both handle and hashtag
        # is written once with both recorded.
        collected: dict[str, Post] = {}

        for query in queries:
            log.info("[%s] query: %s (cap %d)", query.source, query.search_term, query.max_items)
            if dry_run:
                continue
            try:
                items = self.client.run_and_collect(
                    self.actor_input(query), hard_cap=query.max_items
                )
            except ApifyFatalError:
                # Bad token or no credit: every other query will fail the same
                # way, so stop instead of marking the day merely "partial".
                self.state.finish_run(
                    run_id, config.now(), "failed", error="fatal Apify error", new_posts=0
                )
                raise
            except ApifyError as exc:
                # One source failing must not lose the other source's posts.
                msg = f"[{query.source}] {exc}"
                log.error("%s", msg)
                summary.query_errors.append(msg)
                continue

            summary.fetched += len(items)
            if len(items) >= query.max_items:
                log.warning(
                    "[%s] hit the cap of %d items — there may be more posts. "
                    "Consider raising MAX_POSTS_%s.",
                    query.source, query.max_items, query.source.upper(),
                )

            for raw in items:
                post = normalize(raw, config.tz, query.source)
                if post is None:
                    continue

                keep, why = self.keep(post, query.source)
                if not keep:
                    log.debug("Filtered %s: %s", post.id, why)
                    summary.filtered += 1
                    continue

                if not (first_day <= post.local_day <= last_day):
                    summary.out_of_window += 1
                    continue

                existing = collected.get(post.id)
                if existing is None:
                    collected[post.id] = post
                else:
                    existing.sources |= post.sources

        if dry_run:
            log.info("Dry run — no requests made, nothing written.")
            self.state.finish_run(run_id, config.now(), "dry-run")
            return summary

        # --- persist -----------------------------------------------------
        known = self.state.known_post_ids(list(collected.keys()))
        for post_id, post in sorted(collected.items(), key=lambda kv: kv[1].created_at):
            row = known.get(post_id)
            if row is not None:
                summary.duplicates += 1
                old_sources = {s for s in (row["sources"] or "").split(",") if s}
                merged = old_sources | post.sources
                if merged != old_sources:
                    folder = config.data_dir / day_folder_name(
                        post.local_day, config.date_folder_format
                    ) / post.folder_name
                    if self.writer.patch_sources(folder, sorted(merged)):
                        summary.sources_patched += 1
                    self.state.update_post_sources(post_id, sorted(merged))
                continue

            try:
                folder = self.writer.write(post)
            except OSError as exc:
                # Disk problem on one post: log it and keep going, then mark the
                # day partial so the next run retries it.
                log.error("Could not write post %s: %s", post_id, exc)
                summary.query_errors.append(f"write failed for {post_id}: {exc}")
                continue

            self.state.record_post(
                post_id=post.id,
                day=post.local_day,
                author=post.author,
                folder=str(folder),
                sources=sorted(post.sources),
                created_at=post.created_at,
                scraped_at=config.now(),
            )
            summary.written += 1
            key = day_folder_name(post.local_day, config.date_folder_format)
            summary.days_touched[key] = summary.days_touched.get(key, 0) + 1
            log.info("Wrote %s (%s) -> %s", post.id, post.kind, folder.name)

        # --- mark days ---------------------------------------------------
        finished_at = config.now()
        had_errors = bool(summary.query_errors)
        day_cursor = first_day
        while day_cursor <= last_day:
            # Today is never "complete" until the day is actually over, so
            # tomorrow's run always sweeps up this evening's remaining posts.
            is_today = day_cursor == finished_at.date()
            if had_errors:
                status, note = "partial", "; ".join(summary.query_errors)[:500]
            elif is_today:
                status, note = "partial", "day still in progress"
            else:
                status, note = "complete", None
            self.state.mark_day(day_cursor, status, finished_at, note)
            day_cursor += timedelta(days=1)

        status = "partial" if had_errors else "success"
        self.state.finish_run(
            run_id,
            finished_at,
            status,
            fetched=summary.fetched,
            new_posts=summary.written,
            error="; ".join(summary.query_errors)[:1000] or None,
        )

        if had_errors:
            write_last_error(config.log_dir, "\n".join(summary.query_errors))
            if config.notify_on_failure:
                desktop_notify(
                    "X scraper: partial run",
                    f"{summary.written} new posts, but {len(summary.query_errors)} query error(s).",
                )
        else:
            clear_last_error(config.log_dir)

        log.info("Run finished — %s", summary.as_text())
        return summary
