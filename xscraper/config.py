"""Configuration loading. Reads a .env file with no third-party dependency."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=value, # comments, optional surrounding quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key] = val
    return values


def _as_bool(val: str, default: bool) -> bool:
    if val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(val: str, default: int, name: str) -> int:
    if val == "":
        return default
    try:
        return int(val)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {val!r}") from exc


@dataclass(frozen=True)
class Config:
    apify_token: str
    actor_id: str
    handle: str
    hashtag: str
    base_dir: Path
    date_folder_format: str
    max_posts_user: int
    max_posts_hashtag: int
    lookback_days: int
    tz: tzinfo
    tz_name: str
    include_retweets: bool
    include_user_replies: bool
    include_hashtag_replies: bool
    download_media: bool
    download_video_files: bool
    media_timeout: int
    schedule_hour: int
    schedule_minute: int
    actor_timeout: int
    http_retries: int
    min_free_disk_mb: int
    notify_on_failure: bool
    log_level: str
    save_raw: bool

    # Derived paths -------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def state_dir(self) -> Path:
        return self.base_dir / "state"

    @property
    def log_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "run.lock"

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.state_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def now(self) -> datetime:
        return datetime.now(self.tz)


def _resolve_tz(name: str) -> tuple[tzinfo, str]:
    """Blank name -> the machine's local timezone (correct for the Mac)."""
    if not name:
        local = datetime.now().astimezone().tzinfo
        if local is None:  # pragma: no cover - astimezone always sets tzinfo
            raise ConfigError("Could not determine the local timezone.")
        return local, str(local)
    try:
        return ZoneInfo(name), name
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"Unknown TIMEZONE {name!r}. Use an IANA name such as Asia/Kolkata, "
            "or leave it blank to use the machine's local timezone."
        ) from exc


def load_config(env_file: Path | None = None) -> Config:
    """Real environment variables win over the .env file, which helps in tests."""
    env_path = env_file or PROJECT_ROOT / ".env"
    file_values = _read_env_file(env_path)

    def get(key: str, default: str = "") -> str:
        if os.environ.get(key):
            return os.environ[key]
        return file_values.get(key, default)

    token = get("APIFY_TOKEN").strip()
    if not token:
        raise ConfigError(
            f"APIFY_TOKEN is not set. Add it to {env_path} "
            "(copy .env.example to .env if you have not yet)."
        )

    date_format = get("DATE_FOLDER_FORMAT", "iso").strip().lower()
    if date_format not in {"iso", "long"}:
        raise ConfigError("DATE_FOLDER_FORMAT must be 'iso' or 'long'.")

    lookback = _as_int(get("LOOKBACK_DAYS"), 3, "LOOKBACK_DAYS")
    if lookback < 0:
        raise ConfigError("LOOKBACK_DAYS cannot be negative.")

    tz, tz_name = _resolve_tz(get("TIMEZONE").strip())

    hour = _as_int(get("SCHEDULE_HOUR"), 22, "SCHEDULE_HOUR")
    minute = _as_int(get("SCHEDULE_MINUTE"), 0, "SCHEDULE_MINUTE")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError("SCHEDULE_HOUR must be 0-23 and SCHEDULE_MINUTE 0-59.")

    handle = get("X_HANDLE", "emollick").strip().lstrip("@")
    hashtag = get("HASHTAG", "GeminiEnterprise").strip().lstrip("#")
    if not handle and not hashtag:
        raise ConfigError("Set at least one of X_HANDLE or HASHTAG.")

    return Config(
        apify_token=token,
        actor_id=get("APIFY_ACTOR_ID", "apidojo~tweet-scraper").strip(),
        handle=handle,
        hashtag=hashtag,
        base_dir=Path(get("BASE_DIR", "~/XScraper")).expanduser(),
        date_folder_format=date_format,
        max_posts_user=_as_int(get("MAX_POSTS_USER"), 100, "MAX_POSTS_USER"),
        max_posts_hashtag=_as_int(get("MAX_POSTS_HASHTAG"), 300, "MAX_POSTS_HASHTAG"),
        lookback_days=lookback,
        tz=tz,
        tz_name=tz_name,
        include_retweets=_as_bool(get("INCLUDE_RETWEETS"), False),
        include_user_replies=_as_bool(get("INCLUDE_USER_REPLIES"), True),
        include_hashtag_replies=_as_bool(get("INCLUDE_HASHTAG_REPLIES"), False),
        download_media=_as_bool(get("DOWNLOAD_MEDIA"), True),
        download_video_files=_as_bool(get("DOWNLOAD_VIDEO_FILES"), False),
        media_timeout=_as_int(get("MEDIA_TIMEOUT_SECONDS"), 60, "MEDIA_TIMEOUT_SECONDS"),
        schedule_hour=hour,
        schedule_minute=minute,
        actor_timeout=_as_int(get("ACTOR_TIMEOUT_SECONDS"), 1800, "ACTOR_TIMEOUT_SECONDS"),
        http_retries=_as_int(get("HTTP_RETRIES"), 4, "HTTP_RETRIES"),
        min_free_disk_mb=_as_int(get("MIN_FREE_DISK_MB"), 500, "MIN_FREE_DISK_MB"),
        notify_on_failure=_as_bool(get("NOTIFY_ON_FAILURE"), True),
        log_level=get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        save_raw=_as_bool(get("SAVE_RAW"), False),
    )
