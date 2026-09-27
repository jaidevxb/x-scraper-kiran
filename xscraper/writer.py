"""Render a Post to markdown and land it atomically in its day folder."""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .config import Config
from .mediadl import MediaDownloader
from .normalize import Post
from .util import atomic_write_text, day_folder_name, replace_dir

log = logging.getLogger(__name__)

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _yaml_scalar(value: object) -> str:
    """Emit a YAML-safe scalar. Strings are always double-quoted and escaped."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "")
    return f'"{escaped}"'


def _yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_scalar(v) for v in values) + "]"


def _pretty_stamp(dt: datetime) -> str:
    return f"{dt.day} {_MONTH_ABBR[dt.month - 1]} {dt.year}, {dt:%H:%M}"


def render_markdown(post: Post) -> str:
    local = post.created_at_local
    lines: list[str] = ["---"]
    lines.append(f"id: {_yaml_scalar(post.id)}")
    lines.append(f"url: {_yaml_scalar(post.url)}")
    lines.append(f"author: {_yaml_scalar(post.author)}")
    lines.append(f"author_name: {_yaml_scalar(post.author_name)}")
    lines.append(f"created_at: {_yaml_scalar(post.created_at.strftime('%Y-%m-%dT%H:%M:%SZ'))}")
    lines.append(f"created_at_local: {_yaml_scalar(local.isoformat())}")
    lines.append(f"day: {_yaml_scalar(local.date().isoformat())}")
    lines.append(f"matched_source: {_yaml_list(sorted(post.sources))}")
    lines.append(f"type: {_yaml_scalar(post.kind)}")
    lines.append(f"lang: {_yaml_scalar(post.lang)}")
    lines.append(f"hashtags: {_yaml_list(post.hashtags)}")
    lines.append(f"mentions: {_yaml_list(post.mentions)}")
    lines.append(f"links: {_yaml_list(post.links)}")
    lines.append(f"conversation_id: {_yaml_scalar(post.conversation_id)}")
    if post.quote_id:
        lines.append(f"quoted_post_id: {_yaml_scalar(post.quote_id)}")
    if post.possibly_sensitive:
        lines.append("possibly_sensitive: true")

    lines.append("metrics:")
    for key in ("likes", "retweets", "replies", "quotes", "bookmarks", "views"):
        lines.append(f"  {key}: {post.metrics.get(key, 0)}")

    if post.media:
        lines.append("media:")
        for item in post.media:
            lines.append(f"  - type: {_yaml_scalar(item.kind)}")
            if item.local_path:
                lines.append(f"    path: {_yaml_scalar(item.local_path)}")
            lines.append(f"    remote_url: {_yaml_scalar(item.source_url)}")
            if item.video_url:
                lines.append(f"    video_url: {_yaml_scalar(item.video_url)}")
            if item.duration_ms:
                lines.append(f"    duration_ms: {item.duration_ms}")
            if item.error:
                lines.append(f"    error: {_yaml_scalar(item.error)}")
        if any(item.error for item in post.media):
            lines.append("media_error: true")
    else:
        lines.append("media: []")

    lines.append(f"scraped_at: {_yaml_scalar(datetime.now(post.created_at_local.tzinfo).isoformat())}")
    lines.append("---")
    lines.append("")

    handle = f"@{post.author}" if post.author else "unknown author"
    lines.append(f"# {handle} — {_pretty_stamp(local)}")
    lines.append("")

    if post.kind == "reply":
        lines.append("*(reply)*")
        lines.append("")

    lines.append(post.text if post.text else "*(no text — media or poll only)*")
    lines.append("")

    for item in post.media:
        if item.local_path:
            label = "video thumbnail" if item.kind in {"video", "animated_gif"} else "image"
            lines.append(f"![{label}]({item.local_path})")
            if item.video_url and not item.local_path.endswith(".mp4"):
                lines.append(f"[▶ watch video]({item.video_url})")
        else:
            lines.append(f"<!-- media not downloaded: {item.source_url} ({item.error}) -->")
            lines.append(f"[media link]({item.source_url})")
        lines.append("")

    if post.quoted:
        quoted_handle = f"@{post.quoted.author}" if post.quoted.author else "a deleted account"
        header = f"**Quoting {quoted_handle}"
        if post.quoted.created_at:
            header += f" · {_pretty_stamp(post.quoted.created_at)} UTC"
        header += "**"
        lines.append("---")
        lines.append("")
        lines.append(f"> {header}")
        lines.append(">")
        body = post.quoted.text or "*(no text)*"
        for para in body.splitlines() or [""]:
            lines.append(f"> {para}".rstrip())
        if post.quoted.url:
            lines.append(">")
            lines.append(f"> [original]({post.quoted.url})")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"[View on X]({post.url})")
    lines.append("")
    return "\n".join(lines)


class PostWriter:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.downloader = MediaDownloader(
            timeout=config.media_timeout,
            download_videos=config.download_video_files,
        )

    def day_dir(self, post: Post) -> Path:
        return self.config.data_dir / day_folder_name(
            post.local_day, self.config.date_folder_format
        )

    def post_dir(self, post: Post) -> Path:
        return self.day_dir(post) / post.folder_name

    def write(self, post: Post) -> Path:
        """Build the post folder in a staging dir, then move it into place.

        A crash (or a shutdown) partway through leaves the staging dir behind and
        the real folder untouched, so we never publish a half-written post.
        """
        target = self.post_dir(post)
        target.parent.mkdir(parents=True, exist_ok=True)

        staging_root = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(target.parent)))
        staged = staging_root / post.folder_name
        staged.mkdir()
        try:
            if self.config.download_media and post.media:
                self.downloader.download_all(post.media, staged)

            atomic_write_text(staged / "post.md", render_markdown(post))

            if self.config.save_raw:
                atomic_write_text(
                    staged / "raw.json",
                    json.dumps(post.raw, ensure_ascii=False, indent=2),
                )

            replace_dir(staged, target)
            return target
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    def patch_sources(self, post_dir: Path, sources: list[str]) -> bool:
        """Update matched_source in an already-written post.md.

        Happens when a post first showed up under one source and a later run
        finds it under the other.
        """
        md = post_dir / "post.md"
        if not md.exists():
            return False
        text = md.read_text(encoding="utf-8")
        replacement = f"matched_source: {_yaml_list(sorted(sources))}"
        new_text, count = re.subn(
            r"^matched_source: .*$", replacement, text, count=1, flags=re.MULTILINE
        )
        if count == 0 or new_text == text:
            return False
        atomic_write_text(md, new_text)
        return True

    def cleanup_staging(self) -> int:
        """Remove staging/backup dirs left by an earlier crash."""
        removed = 0
        if not self.config.data_dir.exists():
            return 0
        for path in self.config.data_dir.glob("*/.staging-*"):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        for path in self.config.data_dir.glob("*/*.old-*"):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        if removed:
            log.info("Cleaned up %d leftover staging folder(s) from a previous crash", removed)
        return removed
