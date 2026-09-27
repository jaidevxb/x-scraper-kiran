# Setting this up on the MacBook

Follow these in order. Total time: about 5 minutes.

Anything in a grey box is a command — copy it, paste it into **Terminal**, press Enter.

> **Opening Terminal:** press `Cmd + Space`, type `terminal`, press Enter.

---

## Step 1 — Check Python is there

```bash
python3 --version
```

You want **3.9 or higher**. If you get `command not found`, run this and click
through the installer that pops up, then try again:

```bash
xcode-select --install
```

---

## Step 2 — Copy the project onto the Mac

Put the project folder somewhere permanent in the home directory. **Not** on the
Desktop or in Documents — macOS asks background jobs for permission to touch
those folders, and a job that runs at 10pm has nobody around to click "Allow".

```bash
mkdir -p ~/Projects
```

Then copy the project folder into `~/Projects/` (AirDrop, USB, `git clone`,
whatever is easiest). You should end up with `~/Projects/kiran-apify`.

Check it landed:

```bash
ls ~/Projects/kiran-apify
```

You should see `xscraper`, `scripts`, `requirements.txt`, `.env.example`.

---

## Step 3 — (Optional) bring over the posts already collected

60 posts from 24–27 September were collected while the project was being built.
They are **not in the git repo** — the repo is public, and 6 MB of someone else's
posts and images does not belong in it. So you have two choices:

**Option A — copy the folder over** (keeps the exact files and their history).
If you have the `output/` folder from the development machine (AirDrop, USB, zip):

```bash
mv ~/Downloads/output ~/XScraper
ls ~/XScraper/data          # should list four date folders
```

This brings `state/state.db` too, so the scraper already knows those 60 post ids
and will not re-download them.

**Option B — just re-collect them** (simpler, costs about 3 cents).
Skip this step entirely and run the backfill after Step 4:

```bash
cd ~/Projects/kiran-apify
.venv/bin/python3 -m xscraper run --from 2026-09-24 --to 2026-09-27
```

Either way you end up with the same four day folders. If you do nothing at all,
the scraper simply starts from today and those four days stay missing.

---

## Step 4 — Run the installer

```bash
cd ~/Projects/kiran-apify
bash scripts/install.sh
```

It will:

1. check Python
2. build a virtualenv and install the two dependencies
3. ask for the Apify API token — **paste it when prompted** (it will not be
   shown as you type on some terminals; that is normal, just paste and Enter)
4. lock the `.env` file to owner-only
5. create `~/XScraper/{data,state,logs}`
6. verify the token actually works against Apify
7. install and start the scheduled job

If any step fails it stops and tells you why. Nothing is half-installed.

---

## Step 5 — Do a collection by hand

The scheduler is now live but will not act until 10pm. Run it once so you see it
working:

```bash
cd ~/Projects/kiran-apify
.venv/bin/python3 -m xscraper run --force
```

Takes 1–3 minutes. You will see a line per post as it writes.

If you took **Option A** in Step 3, expect a summary like `new=4 duplicates=56` —
that is the deduplication doing its job, not a failure. `--force` is needed in
that case because a successful run is already on record in the database you
copied over. Otherwise it is just a convenience so you do not have to wait
until 10pm.

From tomorrow the schedule handles it with no flags.

---

## Step 6 — Check the output

```bash
open ~/XScraper/data
```

Finder opens. You should see one folder per day, and inside each day one folder
per post containing `post.md` and a `media/` folder for images.

---

## That's it

It now runs every day at **22:00** local time, by itself, forever.

---
---

# Things you'll want to know

## Making it an Obsidian vault

Open Obsidian → **Open folder as vault** → pick `~/XScraper/data`. The images
show up inline because the markdown links to them with relative paths.

You can also point Obsidian at `~/XScraper` itself if you want the logs visible.

## Checking on it

```bash
cd ~/Projects/kiran-apify
.venv/bin/python3 -m xscraper status
```

Shows the last successful run, whether a run is currently due, which days are
still pending, and the last 10 runs with any errors.

Watch the log live:

```bash
tail -f ~/XScraper/logs/scraper.log
```

## Forcing a run right now

```bash
cd ~/Projects/kiran-apify
.venv/bin/python3 -m xscraper run --force
```

Or make launchd fire it exactly as it would at 10pm (this is the better test,
because it exercises the real scheduled path):

```bash
launchctl kickstart -k gui/$(id -u)/com.kiran.xscraper
```

## Backfilling old days

```bash
cd ~/Projects/kiran-apify
.venv/bin/python3 -m xscraper run --from 2026-09-01 --to 2026-09-14
```

Safe to run repeatedly — anything already collected is skipped.

## Changing settings

```bash
open -e ~/Projects/kiran-apify/.env
```

Everything is in there with a comment explaining it: the handle, the hashtag,
the daily caps, the run time, the folder location.

**If you change `SCHEDULE_HOUR` or `SCHEDULE_MINUTE`, re-run the installer** so
the LaunchAgent picks up the new time:

```bash
cd ~/Projects/kiran-apify && bash scripts/install.sh
```

Other settings take effect on the next run with no reinstall.

## Turning it off

```bash
cd ~/Projects/kiran-apify
bash scripts/uninstall.sh
```

Stops the schedule. Your collected posts stay exactly where they are.

---

# If the Mac is asleep or shut down at 10pm

This is handled, three different ways at once:

| Situation | What happens |
|---|---|
| Mac **asleep** at 22:00 | launchd runs the job the moment it wakes |
| Mac **off** at 22:00 | the job runs at the next login |
| Job **crashed** or had no internet | the next run retries that day automatically |
| Off for a **week** | the next run backfills every missed day |

You do not need to do anything. The scraper checks on startup whether the run is
actually due, so the login trigger never causes duplicate work or duplicate cost.

### Optional: wake the Mac automatically at 9:55pm

If you want the collection to happen at 10pm sharp rather than at the next wake,
you can tell macOS to wake (or power on) the machine five minutes before:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 21:55:00
```

To undo it later:

```bash
sudo pmset repeat cancel
```

This only works if the Mac is plugged in or the lid is open. It is genuinely
optional — everything works without it, just with a delay until the next wake.

---

# Troubleshooting

### "Nothing happened at 10pm"

Check the job is registered:

```bash
launchctl list | grep xscraper
```

Three columns: PID, last exit code, label. A `0` in the middle means the last
run succeeded. `-` in the first column just means it is not running right now,
which is correct between runs.

Then check the log:

```bash
tail -50 ~/XScraper/logs/scraper.log
cat ~/XScraper/logs/last_error.txt      # only exists if something failed
```

### "Apify credits or plan limit reached"

The Apify account is out of credit. Top it up at
<https://console.apify.com/billing>. The scraper stops cleanly and resumes on
the next run once there is credit — no days are lost, because the lookback
window backfills them.

### "I get a macOS permission popup"

You put the data folder somewhere protected (Desktop, Documents, iCloud Drive).
Move `BASE_DIR` in `.env` back to `~/XScraper` and re-run the installer.

### Starting completely over

```bash
cd ~/Projects/kiran-apify
bash scripts/uninstall.sh
rm -rf ~/XScraper          # deletes ALL collected posts — be sure
bash scripts/install.sh
```

Deleting only `~/XScraper/state/state.db` is the softer option: the markdown
stays, and the next run re-checks the lookback window without re-downloading
anything it can still see on disk.
