"""Download post images (and optionally videos) into the post's media/ folder."""

from __future__ import annotations

import logging
import mimetypes
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .normalize import MediaItem

log = logging.getLogger(__name__)

# pbs.twimg.com serves a resized image by default; name=orig gives the original.
_ORIG_HOSTS = {"pbs.twimg.com"}

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}


def _full_size(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc not in _ORIG_HOSTS or "name=" in (parsed.query or ""):
        return url
    joiner = "&" if parsed.query else "?"
    return f"{url}{joiner}name=orig"


def _extension(url: str, content_type: str | None) -> str:
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _EXT_BY_TYPE:
            return _EXT_BY_TYPE[base]
        guessed = mimetypes.guess_extension(base)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


class MediaDownloader:
    def __init__(self, timeout: int = 60, retries: int = 3, download_videos: bool = False) -> None:
        self.timeout = timeout
        self.retries = max(1, retries)
        self.download_videos = download_videos
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; xscraper/1.0)"})

    def _fetch(self, url: str, dest_dir: Path, stem: str) -> str:
        """Download to a temp name, then rename — a partial file never lands."""
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with self.session.get(url, timeout=self.timeout, stream=True) as resp:
                    if resp.status_code == 404:
                        # Deleted or expired media: no amount of retrying fixes it.
                        raise FileNotFoundError(f"404 for {url}")
                    resp.raise_for_status()
                    ext = _extension(url, resp.headers.get("Content-Type"))
                    final = dest_dir / f"{stem}{ext}"
                    tmp = dest_dir / f".{stem}{ext}.part"
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    with open(tmp, "wb") as handle:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                handle.write(chunk)
                    if tmp.stat().st_size == 0:
                        tmp.unlink(missing_ok=True)
                        raise OSError("downloaded 0 bytes")
                    tmp.replace(final)
                    return final.name
            except FileNotFoundError:
                raise
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                log.warning("Media download failed (%d/%d) %s: %s",
                            attempt, self.retries, url, exc)
                if attempt < self.retries:
                    time.sleep(min(20.0, 2.0 ** attempt) + random.uniform(0, 1))
        raise OSError(f"could not download {url}: {last_error}")

    def download_all(self, media: list[MediaItem], post_dir: Path) -> None:
        """Fill in local_path / error on each item. Never raises.

        A missing image must not cost us the post's text, so failures are
        recorded in the front matter and the remote URL is kept.
        """
        if not media:
            return
        media_dir = post_dir / "media"
        for index, item in enumerate(media, start=1):
            stem = f"{index}"
            try:
                name = self._fetch(_full_size(item.source_url), media_dir, stem)
                item.local_path = f"media/{name}"
            except FileNotFoundError:
                item.error = "not found (404) — media deleted or expired"
                log.warning("Media gone for %s", item.source_url)
            except OSError as exc:
                item.error = str(exc)

            if item.video_url and self.download_videos:
                try:
                    name = self._fetch(item.video_url, media_dir, f"{index}-video")
                    item.local_path = item.local_path or f"media/{name}"
                    item.error = item.error or ""
                except (FileNotFoundError, OSError) as exc:
                    log.warning("Video download failed for post dir %s: %s", post_dir, exc)
