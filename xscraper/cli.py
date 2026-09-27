"""Command line interface.

    python -m xscraper run                  # normal (what launchd calls)
    python -m xscraper run --force          # ignore the already-ran guard
    python -m xscraper run --date 2026-09-10
    python -m xscraper run --from 2026-09-01 --to 2026-09-07
    python -m xscraper run --dry-run        # print the queries, call nothing
    python -m xscraper status
    python -m xscraper check                # validate config + Apify token
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from .apify import ApifyClient, ApifyError, ApifyFatalError
from .config import Config, ConfigError, load_config
from .notify import desktop_notify, setup_logging, write_last_error
from .runner import DiskSpaceError, Runner
from .state import State
from .util import AlreadyRunning, RunLock, last_scheduled_trigger

log = logging.getLogger("xscraper")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_LOCKED = 3
EXIT_RECOVERABLE = 4
EXIT_FATAL = 5


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date in YYYY-MM-DD form")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xscraper", description="Archive an X account and a hashtag as daily markdown."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="collect posts")
    run.add_argument("--force", action="store_true",
                     help="run even if today's scheduled run already succeeded")
    run.add_argument("--date", type=_parse_day, help="collect one specific day")
    run.add_argument("--from", dest="from_day", type=_parse_day, help="backfill range start")
    run.add_argument("--to", dest="to_day", type=_parse_day, help="backfill range end")
    run.add_argument("--dry-run", action="store_true",
                     help="show the queries without calling Apify")
    run.add_argument("--trigger", default="manual",
                     help="label recorded in the run log (launchd uses 'scheduled')")
    run.add_argument("--quiet", action="store_true", help="log to file only")

    status = sub.add_parser("status", help="show recent runs and day coverage")
    status.add_argument("--limit", type=int, default=10)

    sub.add_parser("check", help="validate configuration and the Apify token")
    return parser


def cmd_status(config: Config, limit: int) -> int:
    with State(config.db_path) as state:
        now = config.now()
        print(f"Base dir       : {config.base_dir}")
        print(f"Handle         : @{config.handle or '-'}")
        print(f"Hashtag        : #{config.hashtag or '-'}")
        print(f"Timezone       : {config.tz_name}")
        print(f"Schedule       : {config.schedule_hour:02d}:{config.schedule_minute:02d} daily")
        print(f"Lookback       : {config.lookback_days} day(s)")
        last = state.last_successful_run_finish()
        trigger = last_scheduled_trigger(now, config.schedule_hour, config.schedule_minute)
        print(f"Last success   : {last.isoformat() if last else 'never'}")
        print(f"Last scheduled : {trigger.isoformat()}")
        if last is None or last <= trigger:
            print("Due now        : yes")
        else:
            print("Due now        : no (already collected for this cycle)")

        broken = state.incomplete_days(since=now.date().replace(day=1))
        print(f"Days pending   : {', '.join(d.isoformat() for d in broken) or 'none'}")

        print("\nRecent runs")
        rows = state.recent_runs(limit)
        if not rows:
            print("  (none yet)")
        for row in rows:
            started = row["started_at"][:19].replace("T", " ")
            print(
                f"  #{row['id']:<4} {started}  {row['status']:<11} "
                f"fetched={row['fetched'] or 0:<5} new={row['new_posts'] or 0:<5} "
                f"{row['trigger'] or ''}"
                + (f"  ! {row['error'][:70]}" if row["error"] else "")
            )

        err = config.log_dir / "last_error.txt"
        if err.exists():
            print(f"\nLast error ({err}):")
            print("  " + err.read_text(encoding="utf-8").strip().replace("\n", "\n  ")[:600])
    return EXIT_OK


def cmd_check(config: Config) -> int:
    print(f"Config loaded from  : {config.base_dir}")
    print(f"Timezone            : {config.tz_name} (now {config.now():%Y-%m-%d %H:%M:%S %z})")
    config.ensure_dirs()
    print(f"Directories ready   : {config.data_dir}")
    client = ApifyClient(config.apify_token, config.actor_id,
                         retries=2, timeout=config.actor_timeout)
    try:
        resp = client._request("GET", "users/me")
        data = resp.json().get("data") or {}
        print(f"Apify token         : OK (user {data.get('username', '?')})")
        plan = (data.get("plan") or {}) if isinstance(data.get("plan"), dict) else {}
        if plan:
            print(f"Apify plan          : {plan.get('id', 'unknown')}")
    except ApifyError as exc:
        print(f"Apify token         : FAILED — {exc}")
        return EXIT_CONFIG

    with State(config.db_path) as state:
        state.set_meta("last_check", datetime.now().isoformat())
    print("SQLite state        : OK")
    print("\nEverything looks fine. Try: python -m xscraper run --dry-run")
    return EXIT_OK


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    start_day = args.from_day
    end_day = args.to_day
    if args.date:
        start_day = end_day = args.date
    if (start_day is None) != (end_day is None):
        # One half of a range given: treat it as a single open-ended bound.
        start_day = start_day or end_day
        end_day = end_day or config.now().date()
    if start_day and end_day and start_day > end_day:
        print("--from must not be after --to", file=sys.stderr)
        return EXIT_CONFIG

    try:
        with RunLock(config.lock_path):
            with State(config.db_path) as state:
                client = ApifyClient(
                    config.apify_token, config.actor_id,
                    retries=config.http_retries, timeout=config.actor_timeout,
                )
                runner = Runner(config, state, client)
                summary = runner.run(
                    trigger=args.trigger,
                    force=args.force,
                    start_day=start_day,
                    end_day=end_day,
                    dry_run=args.dry_run,
                )
                print(summary.as_text())
                return EXIT_OK if not summary.query_errors else EXIT_RECOVERABLE
    except AlreadyRunning as exc:
        log.info("%s", exc)
        return EXIT_LOCKED
    except ApifyFatalError as exc:
        # Wrong token or no credit — retrying on a schedule would just fail
        # every night in silence, so this exits fatal and alerts.
        log.error("%s", exc)
        write_last_error(config.log_dir, str(exc))
        if config.notify_on_failure:
            desktop_notify("X scraper stopped — needs attention", str(exc))
        return EXIT_FATAL
    except DiskSpaceError as exc:
        log.error("%s", exc)
        write_last_error(config.log_dir, str(exc))
        if config.notify_on_failure:
            desktop_notify("X scraper stopped", str(exc))
        return EXIT_FATAL
    except ApifyError as exc:
        log.error("Run failed: %s", exc)
        write_last_error(config.log_dir, str(exc))
        if config.notify_on_failure:
            desktop_notify("X scraper failed", f"{exc}")
        return EXIT_RECOVERABLE
    except Exception as exc:  # noqa: BLE001 - last resort so launchd sees a clean exit code
        log.exception("Unexpected failure")
        write_last_error(config.log_dir, f"{type(exc).__name__}: {exc}")
        if config.notify_on_failure:
            desktop_notify("X scraper crashed", f"{type(exc).__name__}: {exc}")
        return EXIT_FATAL


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    config.ensure_dirs()
    setup_logging(
        config.log_dir,
        config.log_level,
        quiet_console=getattr(args, "quiet", False),
    )

    if args.command == "run":
        return cmd_run(config, args)
    if args.command == "status":
        return cmd_status(config, args.limit)
    if args.command == "check":
        return cmd_check(config)
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
