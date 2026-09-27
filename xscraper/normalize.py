"""Turn a raw Apify tweet object into a clean Post.

Field names here were verified against a live response from
apidojo~tweet-scraper, but every access is defensive: the actor can change and
a single odd item must never kill the whole run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from .util import ParseError, parse_twitter_datetime

log = logging.getLogger(__name__)

HASHTAG_RE = re.compile(r"(?<!\w)#(\w{1,139})")
MENTION_RE = re.compile(r"(?<!\w)@(\w{1,15})")


@dataclass
class MediaItem:
    kind: str           # photo | video | animated_gif
    source_url: str     # the image (or video poster) to download
    video_url: str = "" # best MP4 variant, for video/gif
    duration_ms: int = 0
    local_path: str = ""
    error: str = ""


@dataclass
class QuotedPost:
    author: str = ""
    author_name: str = ""
    text: str = ""
    url: str = ""
    created_at: datetime | None = None


@dataclass
class Post:
    id: str
    url: str
    author: str
    author_name: str
    text: str
    created_at: datetime              # always UTC
    created_at_local: datetime
    kind: str                         # tweet | reply | quote | retweet
    lang: str = ""
    conversation_id: str = ""
    quote_id: str = ""
    possibly_sensitive: bool = False
    is_reply: bool = False
    is_quote: bool = False
    is_retweet: bool = False
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)
    quoted: QuotedPost | None = None
    sources: set[str] = field(default_factory=set)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def local_day(self):
        return self.created_at_local.date()

    @property
    def folder_name(self) -> str:
        from .util import sanitize_component

        return f"{self.id}_{sanitize_component(self.author, 20)}"


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pick_best_video(video_info: dict[str, Any]) -> tuple[str, int]:
    """Highest-bitrate MP4 variant. Skips the HLS .m3u8 playlist."""
    best_url, best_rate = "", -1
    for variant in video_info.get("variants") or []:
        if variant.get("content_type") != "video/mp4":
            continue
        rate = _int(variant.get("bitrate"))
        if rate > best_rate:
            best_url, best_rate = variant.get("url") or "", rate
    return best_url, _int(video_info.get("duration_millis"))


def _extract_media(raw: dict[str, Any]) -> list[MediaItem]:
    """Media lives in extendedEntities.media; entities.media only has the first."""
    entries = (raw.get("extendedEntities") or {}).get("media")
    if not entries:
        entries = (raw.get("entities") or {}).get("media") or []

    items: list[MediaItem] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get("media_url_https") or entry.get("media_url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        kind = entry.get("type") or "photo"
        item = MediaItem(kind=kind, source_url=url)
        if kind in {"video", "animated_gif"}:
            item.video_url, item.duration_ms = _pick_best_video(entry.get("video_info") or {})
        items.append(item)

    # Fallback: a bare list of URLs on the top-level `media` key.
    if not items:
        for url in raw.get("media") or []:
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                items.append(MediaItem(kind="photo", source_url=url))
    return items


def _expand_links(text: str, raw: dict[str, Any]) -> tuple[str, list[str]]:
    """Swap t.co shorteners for real URLs and drop the trailing media link.

    A t.co link is meaningless in an archived markdown file; the expanded URL is
    the thing a human reading this in six months actually needs.
    """
    body = text
    links: list[str] = []

    for entry in (raw.get("entities") or {}).get("urls") or []:
        if not isinstance(entry, dict):
            continue
        short = entry.get("url") or ""
        expanded = entry.get("expanded_url") or ""
        if short and expanded:
            body = body.replace(short, expanded)
            if expanded not in links:
                links.append(expanded)

    # The t.co link X appends for its own photo/video is pure noise.
    media_entries = (raw.get("extendedEntities") or {}).get("media") or []
    media_entries = media_entries or ((raw.get("entities") or {}).get("media") or [])
    for entry in media_entries:
        if isinstance(entry, dict) and entry.get("url"):
            body = body.replace(entry["url"], "")

    return body.strip(), links


def _extract_tags(text: str, raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Prefer the entities block; fall back to scanning the text.

    The live payload often omits entities.hashtags entirely, so the regex path
    is the normal case rather than an exotic fallback.
    """
    entities = raw.get("entities") or {}

    hashtags = [
        h["text"] for h in entities.get("hashtags") or []
        if isinstance(h, dict) and h.get("text")
    ]
    if not hashtags:
        hashtags = HASHTAG_RE.findall(text)

    mentions = [
        m["screen_name"] for m in entities.get("user_mentions") or []
        if isinstance(m, dict) and m.get("screen_name")
    ]
    if not mentions:
        mentions = MENTION_RE.findall(text)

    def dedupe(values: list[str]) -> list[str]:
        out, seen = [], set()
        for value in values:
            key = value.lower()
            if key not in seen:
                seen.add(key)
                out.append(value)
        return out

    return dedupe(hashtags), dedupe(mentions)


def _quoted(raw: dict[str, Any]) -> QuotedPost | None:
    quote = raw.get("quote")
    if not isinstance(quote, dict) or not quote:
        return None
    author = (quote.get("author") or {}) if isinstance(quote.get("author"), dict) else {}
    created = None
    if quote.get("createdAt"):
        try:
            created = parse_twitter_datetime(quote["createdAt"])
        except ParseError:
            created = None
    quote_text = (quote.get("fullText") or quote.get("text") or "").strip()
    quote_body, _ = _expand_links(quote_text, quote)
    return QuotedPost(
        author=author.get("userName") or "",
        author_name=author.get("name") or "",
        text=quote_body,
        url=quote.get("url") or quote.get("twitterUrl") or "",
        created_at=created,
    )


def normalize(raw: dict[str, Any], tz: tzinfo, source: str) -> Post | None:
    """Return a Post, or None if the item is not a usable tweet."""
    if not isinstance(raw, dict):
        return None

    # The actor occasionally emits non-tweet rows (errors, rate-limit notices).
    if raw.get("type") not in (None, "tweet"):
        log.debug("Skipping non-tweet item of type %r", raw.get("type"))
        return None

    post_id = str(raw.get("id") or raw.get("id_str") or "").strip()
    if not post_id:
        log.warning("Skipping item with no id: %s", str(raw)[:160])
        return None

    try:
        created_utc = parse_twitter_datetime(raw.get("createdAt") or raw.get("created_at") or "")
    except ParseError as exc:
        log.warning("Skipping post %s: %s", post_id, exc)
        return None

    author_block = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author = (author_block.get("userName") or "").strip()
    author_name = (author_block.get("name") or "").strip()

    text = (raw.get("fullText") or raw.get("text") or "").strip()
    body, links = _expand_links(text, raw)
    hashtags, mentions = _extract_tags(text, raw)

    is_reply = bool(raw.get("isReply"))
    is_quote = bool(raw.get("isQuote"))
    is_retweet = bool(raw.get("isRetweet"))
    if is_retweet:
        kind = "retweet"
    elif is_reply:
        kind = "reply"
    elif is_quote:
        kind = "quote"
    else:
        kind = "tweet"

    url = raw.get("url") or raw.get("twitterUrl") or ""
    if not url and author:
        url = f"https://x.com/{author}/status/{post_id}"

    return Post(
        id=post_id,
        url=url,
        author=author,
        author_name=author_name,
        text=body,
        created_at=created_utc,
        created_at_local=created_utc.astimezone(tz),
        kind=kind,
        lang=raw.get("lang") or "",
        conversation_id=str(raw.get("conversationId") or ""),
        quote_id=str(raw.get("quoteId") or ""),
        possibly_sensitive=bool(raw.get("possiblySensitive")),
        is_reply=is_reply,
        is_quote=is_quote,
        is_retweet=is_retweet,
        hashtags=hashtags,
        mentions=mentions,
        links=links,
        media=_extract_media(raw),
        metrics={
            "likes": _int(raw.get("likeCount")),
            "retweets": _int(raw.get("retweetCount")),
            "replies": _int(raw.get("replyCount")),
            "quotes": _int(raw.get("quoteCount")),
            "bookmarks": _int(raw.get("bookmarkCount")),
            "views": _int(raw.get("viewCount")),
        },
        quoted=_quoted(raw),
        sources={source},
        raw=raw,
    )
