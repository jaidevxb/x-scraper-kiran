"""Logging setup plus a macOS desktop notification on failure.

A background job that fails silently is worse than no job at all, so every
failure writes logs/last_error.txt and pops a Notification Center alert.
"""

from __future__ import annotations

import logging
import logging.handlers
import subprocess
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def setup_logging(log_dir: Path, level: str = "INFO", quiet_console: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "scraper.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if not quiet_console:
        # A Windows console defaults to a legacy codepage and would raise on
        # emoji in post text; never let logging itself break the run.
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)

    # requests/urllib3 chatter is noise at DEBUG level.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def write_last_error(log_dir: Path, message: str) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "last_error.txt").write_text(
            f"{datetime.now().isoformat()}\n\n{message}\n", encoding="utf-8"
        )
    except OSError:
        log.debug("Could not write last_error.txt", exc_info=True)


def clear_last_error(log_dir: Path) -> None:
    try:
        (log_dir / "last_error.txt").unlink(missing_ok=True)
    except OSError:
        pass


def desktop_notify(title: str, message: str) -> None:
    """Notification Center on macOS; a no-op everywhere else."""
    if sys.platform != "darwin":
        log.debug("Skipping desktop notification (not macOS): %s — %s", title, message)
        return
    safe_title = title.replace('"', "'")[:80]
    safe_message = message.replace('"', "'").replace("\n", " ")[:220]
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("osascript notification failed", exc_info=True)
