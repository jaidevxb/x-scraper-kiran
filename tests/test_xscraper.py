"""Tests for the logic that is easy to get subtly wrong.

Run with:  python -m pytest tests -q      (or: python tests/test_xscraper.py)
No network access needed — the Apify payload is a saved fixture.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xscraper.normalize import normalize  # noqa: E402
from xscraper.util import (  # noqa: E402
    RunLock,
    AlreadyRunning,
    atomic_write_text,
    day_folder_name,
    last_scheduled_trigger,
    parse_twitter_datetime,
    replace_dir,
    sanitize_component,
)
from xscraper.writer import render_markdown  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample_tweets.json"
IST = ZoneInfo("Asia/Kolkata")


def load_fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- date parsing ---------------------------------------------------------
def test_parses_x_date_format():
    dt = parse_twitter_datetime("Sat Sep 26 04:54:05 +0000 2026")
    assert dt == datetime(2026, 9, 26, 4, 54, 5, tzinfo=timezone.utc)


def test_parses_non_utc_offset():
    dt = parse_twitter_datetime("Sat Sep 26 10:54:05 +0600 2026")
    assert dt == datetime(2026, 9, 26, 4, 54, 5, tzinfo=timezone.utc)


def test_parses_iso_fallback():
    assert parse_twitter_datetime("2026-09-26T04:54:05Z") == datetime(
        2026, 9, 26, 4, 54, 5, tzinfo=timezone.utc
    )


def test_late_utc_post_lands_on_the_next_local_day():
    """The whole point of routing by the post's own timestamp.

    23:00 UTC is already 04:30 the next morning in IST, so the post belongs in
    tomorrow's folder — not in the folder for the day we happened to fetch it.
    """
    dt = parse_twitter_datetime("Sat Sep 26 23:00:00 +0000 2026")
    assert dt.astimezone(IST).date() == date(2026, 9, 27)


# --- day folder naming ---------------------------------------------------
def test_day_folder_formats():
    day = date(2026, 9, 15)
    assert day_folder_name(day, "iso") == "2026-09-15"
    assert day_folder_name(day, "long") == "15 september"


# --- the scheduling guard ------------------------------------------------
def test_guard_skips_a_login_later_the_same_evening():
    now = datetime(2026, 9, 26, 23, 30, tzinfo=IST)
    trigger = last_scheduled_trigger(now, 22, 0)
    assert trigger == datetime(2026, 9, 26, 22, 0, tzinfo=IST)
    success_at_2200 = datetime(2026, 9, 26, 22, 0, 30, tzinfo=IST)
    assert success_at_2200 > trigger          # already collected -> skip


def test_guard_runs_next_morning_if_the_mac_was_off_at_2200():
    now = datetime(2026, 9, 27, 9, 0, tzinfo=IST)
    trigger = last_scheduled_trigger(now, 22, 0)
    assert trigger == datetime(2026, 9, 26, 22, 0, tzinfo=IST)
    last_success = datetime(2026, 9, 25, 22, 0, tzinfo=IST)
    assert last_success < trigger             # missed yesterday -> run now


def test_guard_runs_again_at_the_next_scheduled_time():
    """Having caught up at 09:00 must not suppress the same evening's run."""
    caught_up_at = datetime(2026, 9, 27, 9, 0, tzinfo=IST)
    now = datetime(2026, 9, 27, 22, 0, tzinfo=IST)
    assert caught_up_at < last_scheduled_trigger(now, 22, 0)


def test_guard_before_first_trigger_of_the_day_looks_back_a_day():
    now = datetime(2026, 9, 27, 8, 0, tzinfo=IST)
    assert last_scheduled_trigger(now, 22, 0).date() == date(2026, 9, 26)


# --- normalizing the real payload ---------------------------------------
def test_normalizes_every_fixture_item():
    posts = [normalize(raw, IST, "user") for raw in load_fixture()]
    assert all(p is not None for p in posts)
    assert len({p.id for p in posts}) == len(posts)


def test_extracts_author_metrics_and_kind():
    raw = next(r for r in load_fixture() if r["id"] == "2103709671865602100")
    post = normalize(raw, IST, "user")
    assert post.author == "emollick"
    assert post.author_name == "Ethan Mollick"
    assert post.kind == "quote"
    assert post.metrics["likes"] > 0
    assert post.sources == {"user"}


def test_extracts_photo_media():
    raw = next(r for r in load_fixture() if r["id"] == "2103709671865602100")
    post = normalize(raw, IST, "user")
    assert len(post.media) == 1
    assert post.media[0].kind == "photo"
    assert post.media[0].source_url.startswith("https://pbs.twimg.com/")


def test_video_post_gets_a_poster_and_the_best_mp4():
    raw = next(r for r in load_fixture() if r["id"] == "2103272686570918334")
    post = normalize(raw, IST, "user")
    video = next(m for m in post.media if m.kind == "video")
    assert video.source_url.endswith(".jpg")        # poster image
    # URLs carry a query string (…/voEQY2.mp4?tag=29), so check the path, and
    # make sure we did not pick the HLS playlist variant.
    assert ".mp4" in video.video_url
    assert ".m3u8" not in video.video_url
    assert "1920x1080" in video.video_url           # highest-bitrate variant
    assert video.duration_ms > 0


def test_multi_image_post_keeps_all_images():
    raw = next(r for r in load_fixture() if r["id"] == "2103325773323018520")
    post = normalize(raw, IST, "user")
    assert len(post.media) == 4


def test_tco_links_are_expanded():
    raw = next(r for r in load_fixture() if r["id"] == "2103325773323018520")
    post = normalize(raw, IST, "user")
    assert post.links, "expected at least one expanded link"
    assert not any(link.startswith("https://t.co/") for link in post.links)
    assert "t.co" not in post.text


def test_quoted_post_is_captured():
    raw = next(r for r in load_fixture() if r["id"] == "2103709671865602100")
    post = normalize(raw, IST, "user")
    assert post.quoted is not None
    assert post.quoted.author == "MicahCarroll"
    assert post.quoted.text


def test_non_tweet_rows_are_skipped():
    assert normalize({"type": "error", "error": "rate limited"}, IST, "user") is None
    assert normalize({"id": "1"}, IST, "user") is None          # no createdAt
    assert normalize({"createdAt": "Sat Sep 26 04:54:05 +0000 2026"}, IST, "user") is None
    assert normalize("not a dict", IST, "user") is None


def test_folder_name_is_id_plus_handle():
    post = normalize(load_fixture()[0], IST, "user")
    assert post.folder_name == f"{post.id}_emollick"


# --- markdown ------------------------------------------------------------
def test_markdown_has_frontmatter_and_body():
    post = normalize(load_fixture()[0], IST, "user")
    post.media[0].local_path = "media/1.jpg"
    md = render_markdown(post)
    assert md.startswith("---\n")
    assert md.count("---\n") >= 2
    assert f'id: "{post.id}"' in md
    assert "![image](media/1.jpg)" in md
    assert post.url in md


def test_markdown_frontmatter_is_valid_yaml_when_pyyaml_is_available():
    try:
        import yaml
    except ImportError:
        return
    for raw in load_fixture():
        post = normalize(raw, IST, "user")
        front = render_markdown(post).split("---", 2)[1]
        data = yaml.safe_load(front)
        assert data["id"] == post.id
        assert data["day"] == post.local_day.isoformat()


def test_markdown_escapes_quotes_and_newlines_in_frontmatter():
    post = normalize(load_fixture()[0], IST, "user")
    post.author_name = 'Weird "Name"\nwith newline'
    front = render_markdown(post).split("---", 2)[1]
    assert '\\"Name\\"' in front
    assert "\\n" in front
    # the literal newline must not have leaked into the front matter block
    assert "with newline" in front.replace("\\n", "")


def test_markdown_notes_failed_media_instead_of_dropping_the_post():
    post = normalize(load_fixture()[0], IST, "user")
    post.media[0].local_path = ""
    post.media[0].error = "not found (404)"
    md = render_markdown(post)
    assert "media_error: true" in md
    assert post.text[:30] in md


# --- filesystem safety --------------------------------------------------
def test_sanitize_component_strips_path_characters():
    assert "/" not in sanitize_component("bad/name")
    assert ".." not in sanitize_component("..")
    assert sanitize_component("") == "unknown"
    assert sanitize_component("Ethan Mollick") == "Ethan-Mollick"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "sub" / "post.md"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert not list(tmp_path.glob("**/.tmp-*"))


def test_replace_dir_overwrites_a_non_empty_folder(tmp_path):
    target = tmp_path / "post"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "post.md").write_text("new", encoding="utf-8")

    replace_dir(staged, target)
    assert (target / "post.md").read_text(encoding="utf-8") == "new"
    assert not (target / "old.txt").exists()
    assert not list(tmp_path.glob("*.old-*"))


def test_lock_blocks_a_second_run(tmp_path):
    lock_path = tmp_path / "run.lock"
    with RunLock(lock_path):
        try:
            with RunLock(lock_path):
                raise AssertionError("a second run acquired the lock")
        except AlreadyRunning:
            pass
    # released, so it can be taken again
    with RunLock(lock_path):
        pass


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    tmp_root = Path(__file__).parent / ".tmp"
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                case_dir = tmp_root / name
                case_dir.mkdir(parents=True, exist_ok=True)
                fn(case_dir)
            else:
                fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"ok   {name}")
    import shutil

    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
