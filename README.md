# X daily archiver

Collects every post from one X account and one hashtag, once a day, and stores
each post as a self-contained markdown file with its images downloaded next to
it. No server, no hosting — a background job on a MacBook.

**Currently configured for:** `@emollick` and `#GeminiEnterprise`.

**To install on the Mac, follow [SETUP_MAC.md](SETUP_MAC.md).** This file is the
technical reference.

---

## Output layout

```
~/XScraper/
├── data/
│   ├── 2026-09-25/
│   │   ├── 2103485857147617393_emollick/
│   │   │   ├── post.md
│   │   │   └── media/
│   │   │       └── 1.jpg
│   │   └── 2103398544388567197_wasifahmed91/
│   │       └── post.md
│   └── 2026-09-26/
│       └── …
├── state/
│   └── state.db          SQLite: seen post ids, day coverage, run history
└── logs/
    ├── scraper.log       rotating, 5 × 5 MB
    └── last_error.txt    only present when the last run failed
```

Post folders are named `<post-id>_<handle>`. The post id is globally unique and
contains only digits, so collisions and unsafe-filename bugs are impossible.

A post is filed under **its own creation date in local time**, never under the
date it was fetched. This is the single decision that makes late runs, backfills
and midnight boundaries all behave correctly with no special cases.

### A post.md

```markdown
---
id: "2103709671865602100"
url: "https://x.com/emollick/status/2103709671865602100"
author: "emollick"
author_name: "Ethan Mollick"
created_at: "2026-09-26T04:54:05Z"
created_at_local: "2026-09-26T10:24:05+05:30"
day: "2026-09-26"
matched_source: ["user"]
type: "quote"
lang: "en"
hashtags: []
mentions: []
links: []
conversation_id: "2103709671865602100"
quoted_post_id: "2103665811051397256"
metrics:
  likes: 243
  retweets: 20
  replies: 44
  quotes: 10
  bookmarks: 35
  views: 29518
media:
  - type: "photo"
    path: "media/1.jpg"
    remote_url: "https://pbs.twimg.com/media/HTHgSXCXIAACrjj.jpg"
scraped_at: "2026-09-27T10:49:54+05:30"
---

# @emollick — 26 Sep 2026, 10:24

And the incidents apparently continue. …

![image](media/1.jpg)

---

> **Quoting @MicahCarroll · 26 Sep 2026, 01:59 UTC**
>
> Some new misalignment disclosures from OpenAI: …

---

[View on X](https://x.com/emollick/status/2103709671865602100)
```

Image links are **relative**, so a post folder is portable and renders in
Obsidian, VS Code and GitHub alike. `t.co` shorteners are expanded to real URLs
in both the body and the quoted text.

---

## How a run works

1. Take an exclusive lock so a manual and a scheduled run can never overlap.
2. Check whether the run is actually due (see *Scheduling* below).
3. Clean up any staging folders left by a previous crash.
4. Compute the window: `[today − LOOKBACK_DAYS, now]`, extended backwards to
   cover any day still marked incomplete.
5. Fire **two** Apify queries, one per source, each spanning the whole window.
6. Normalize, filter, and drop anything outside the window.
7. Merge the two result sets by post id, so a post matching both the handle and
   the hashtag is written once with `matched_source: ["hashtag", "user"]`.
8. Skip ids already in SQLite; write the rest.
9. Mark each covered day `complete` — except today, which stays `partial` so
   tomorrow's run always sweeps up this evening's posts.

### Why one query per source, not one per day

The actor bills a **50-tweet minimum per query**. Splitting a 3-day window into
three queries per source would triple the floor cost for no benefit, because
posts get routed to the correct day folder from their own timestamps anyway.

### Why a lookback window at all

The job runs at 22:00, so posts made between 22:00 and midnight would otherwise
never be collected. The lookback means tomorrow's run picks them up and files
them under *today*. It also self-heals any day that failed or was missed.

---

## Scheduling

A launchd `LaunchAgent` at `~/Library/LaunchAgents/com.kiran.xscraper.plist`,
with three overlapping triggers:

- `StartCalendarInterval` at 22:00 — launchd fires this on wake if the Mac was
  asleep at the time.
- `RunAtLoad` — fires at every login and boot, covering a Mac that was fully
  powered off at 22:00.
- The lookback window — backfills whole days that were missed regardless.

`RunAtLoad` is safe because of the guard in `Runner.should_run`:

> Skip only if a run already succeeded **after** the most recent scheduled
> moment.

Walk through it:

| Event | Last scheduled moment | Last success | Decision |
|---|---|---|---|
| 22:00, Mac awake | today 22:00 | yesterday 22:00 | **run** |
| 23:00 login, same evening | today 22:00 | today 22:00 | skip |
| Mac was off at 22:00; login 09:00 next day | yesterday 22:00 | 2 days ago | **run** |
| 10:00 login, an hour after that catch-up | yesterday 22:00 | today 09:00 | skip |
| 22:00 that same evening | today 22:00 | today 09:00 | **run** |

`SCHEDULE_HOUR` / `SCHEDULE_MINUTE` in `.env` feed both the plist and the guard,
so they cannot drift apart — but changing them means re-running `install.sh`.

---

## Volume and cost

Actor: [`apidojo/tweet-scraper`](https://apify.com/apidojo/tweet-scraper) at
**$0.40 per 1,000 tweets**, minimum 50 per query.

| Scenario | Tweets/day | Per day | Per month |
|---|---|---|---|
| Quiet day (2 queries × 50 floor) | 100 | $0.04 | **~$1.20** |
| Typical | ~150 | $0.06 | ~$1.80 |
| Both caps maxed out | 400 | $0.16 | ~$4.80 |

Caps are `MAX_POSTS_USER` (default 100) and `MAX_POSTS_HASHTAG` (default 300).
Hitting a cap logs a warning naming the variable to raise. Results are sorted
`Latest`, so a cap drops the oldest posts in the window, never the newest.

---

## Filtering

Applied in the query where possible (cheaper) and again in Python (reliable):

| Rule | Setting | Default |
|---|---|---|
| Pure retweets | `INCLUDE_RETWEETS` | excluded |
| The account's own replies | `INCLUDE_USER_REPLIES` | **included** |
| Replies from others in the hashtag feed | `INCLUDE_HASHTAG_REPLIES` | excluded |
| Wrong author in a `from:` search | always | excluded |

Quote tweets are always kept, with the quoted post rendered as a blockquote.

---

## Edge cases and how each is handled

| Case | Handling |
|---|---|
| Mac asleep / off at 22:00 | launchd wake catch-up + `RunAtLoad` + due-check guard |
| Offline for days | Lookback window; incomplete days pulled back in (up to 60 days) |
| Duplicate posts | SQLite primary key on post id, checked in batches |
| Post matches handle **and** hashtag | Merged in-run; cross-run merges patch `matched_source` in place |
| Manual + scheduled run collide | `RunLock` (`flock` on macOS, byte-range lock on Windows) |
| Apify 5xx / 429 / network down | Exponential backoff with jitter, then the day stays pending |
| Apify run hangs | Polled to `ACTOR_TIMEOUT_SECONDS`, then aborted; retried next run |
| Apify run fails partway | Partial dataset is still read rather than discarded |
| One source fails, other succeeds | Per-query error isolation; the good source still gets written |
| Out of Apify credit | Detected on 402/403, distinct exit code, desktop notification |
| Crash mid-post | Built in a staging dir, moved in with one atomic rename |
| Staging junk from an earlier crash | Swept at the start of every run |
| Interrupted run left `running` in the log | Reaped after 6 hours as `interrupted` |
| Image 404 / expired | Retried, then `media_error: true` plus the remote URL is kept |
| Video posts | Poster image downloaded, best MP4 variant recorded (`DOWNLOAD_VIDEO_FILES` to fetch it) |
| Disk nearly full | Pre-flight check against `MIN_FREE_DISK_MB`; refuses to start |
| Abrupt power loss | SQLite in WAL mode with `synchronous=FULL` |
| Timezone / DST | One configured tz throughout; day boundaries derived from it |
| Non-English macOS locale | `createdAt` parsed with an explicit month map, not locale-dependent `strptime` |
| Emoji / quotes / newlines in text | Escaped front matter, UTF-8 everywhere, UTF-8-safe console logging |
| Posts with no text | Rendered as `*(no text — media or poll only)*` |
| Days with zero posts | No empty folder created; recorded as complete with 0 posts |
| Unsafe characters in names | Folder is `<digits>_<sanitized handle>` |
| macOS protected folders | Data lives under `~/XScraper`, outside Desktop/Documents/iCloud |
| Secret leakage | Token in `.env` at `chmod 600`, never in the world-readable plist |
| Logs growing forever | Rotating handler, 5 × 5 MB |
| Silent failure | `last_error.txt` + Notification Center alert + non-zero exit code |

---

## Commands

```bash
python -m xscraper check                        # validate config and token
python -m xscraper status                       # coverage, due-state, run history
python -m xscraper run                          # what launchd calls
python -m xscraper run --force                  # ignore the due-check
python -m xscraper run --dry-run                # print queries, call nothing
python -m xscraper run --date 2026-09-10        # one specific day
python -m xscraper run --from 2026-09-01 --to 2026-09-07
```

Exit codes: `0` ok · `2` config error · `3` another run holds the lock ·
`4` recoverable (will retry) · `5` fatal (needs a human).

---

## Layout

```
xscraper/
  config.py      .env loading, validation, derived paths
  util.py        date parsing, day naming, atomic writes, the run lock
  state.py       SQLite: posts, days, runs, meta
  apify.py       REST client: start run, poll, page the dataset, retries
  normalize.py   raw Apify JSON -> Post
  mediadl.py     image/video download with retries
  writer.py      markdown rendering + atomic folder placement
  notify.py      logging, last_error.txt, macOS notifications
  runner.py      the orchestration and all the policy decisions
  cli.py         argparse entry point
scripts/
  install.sh     one-shot macOS installer (idempotent)
  uninstall.sh   removes the schedule, keeps the data
  run.sh         wrapper launchd invokes
  com.kiran.xscraper.plist.template
tests/
  test_xscraper.py           26 tests, no network needed
  fixtures/sample_tweets.json  real captured Apify response
```

## Tests

```bash
python tests/test_xscraper.py        # no pytest needed
python -m pytest tests -q            # if you have pytest
```

## Development on Windows

The scraper is cross-platform; only `scripts/` is macOS-specific. To test here,
set `BASE_DIR=./.testrun` and `TIMEZONE=Asia/Kolkata` in `.env`, then run the
CLI directly. The run lock, atomic renames and media downloads all work.
