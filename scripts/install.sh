#!/bin/bash
# ---------------------------------------------------------------------------
# One-shot installer for macOS. Safe to re-run: it upgrades an existing
# install rather than duplicating anything.
# ---------------------------------------------------------------------------
set -euo pipefail

LABEL="com.kiran.xscraper"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV="$PROJECT_DIR/.venv"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

bold "X scraper installer"
echo

# --- 1. sanity checks ------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. On Linux use systemd; on Windows use Task Scheduler."

if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found. Install the Xcode command line tools first:
      xcode-select --install"
fi

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
[ "$PY_OK" = "1" ] || die "Python 3.9 or newer is required (found $(python3 -V 2>&1)).
      Install a newer one:  brew install python@3.12"
ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)"

# --- 2. virtualenv ---------------------------------------------------------
if [ ! -x "$VENV/bin/python3" ]; then
  python3 -m venv "$VENV" || die "Could not create a virtualenv at $VENV"
  ok "created virtualenv"
else
  ok "virtualenv already present"
fi

"$VENV/bin/python3" -m pip install --quiet --upgrade pip
"$VENV/bin/python3" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"
ok "dependencies installed"

# --- 3. configuration -----------------------------------------------------
ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
  ok "created .env from .env.example"
fi

# A .env handed over from a Windows machine arrives with CRLF endings. Normalise
# it once, here, rather than defending against \r at every read site.
if LC_ALL=C grep -q $'\r' "$ENV_FILE" 2>/dev/null; then
  /usr/bin/sed -i '' $'s/\r$//' "$ENV_FILE"
  ok "converted .env from Windows (CRLF) to Unix (LF) line endings"
fi

if ! grep -qE '^APIFY_TOKEN=.+' "$ENV_FILE"; then
  echo
  bold "Apify API token needed"
  echo "  Find it at https://console.apify.com/settings/integrations"
  printf '  Paste it here: '
  read -r TOKEN
  [ -n "$TOKEN" ] || die "No token entered. Edit $ENV_FILE by hand and re-run."
  # Portable in-place edit (BSD sed on macOS needs the empty -i argument).
  /usr/bin/sed -i '' "s|^APIFY_TOKEN=.*|APIFY_TOKEN=$TOKEN|" "$ENV_FILE"
  ok "token saved"
else
  ok "APIFY_TOKEN already set"
fi

chmod 600 "$ENV_FILE"
ok ".env locked down to owner-only (chmod 600)"

# --- 4. read settings back so the plist matches the config ---------------
read_setting() {
  local key="$1" default="$2" value
  # tr -d '\r' matters: a .env edited or transferred from Windows has CRLF
  # endings, and a stray \r would end up inside the generated plist or create a
  # directory with a carriage return in its name.
  value=$(grep -E "^$key=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- \
          | tr -d '\r' | tr -d '"' | tr -d "'" | xargs || true)
  echo "${value:-$default}"
}
HOUR=$(read_setting SCHEDULE_HOUR 22)
MINUTE=$(read_setting SCHEDULE_MINUTE 0)
BASE_DIR=$(read_setting BASE_DIR "$HOME/XScraper")
BASE_DIR="${BASE_DIR/#\~/$HOME}"

mkdir -p "$BASE_DIR/data" "$BASE_DIR/state" "$BASE_DIR/logs"
ok "data directory ready: $BASE_DIR"

chmod +x "$SCRIPT_DIR/run.sh"

# --- 5. verify config + token before scheduling anything -----------------
echo
bold "Checking configuration"
if ! (cd "$PROJECT_DIR" && "$VENV/bin/python3" -m xscraper check); then
  die "Configuration check failed — fix the above, then re-run this installer."
fi

# --- 6. LaunchAgent ------------------------------------------------------
echo
bold "Installing the LaunchAgent"
mkdir -p "$HOME/Library/LaunchAgents"

/usr/bin/sed \
  -e "s|__LABEL__|$LABEL|g" \
  -e "s|__RUN_SH__|$SCRIPT_DIR/run.sh|g" \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  -e "s|__LOG_DIR__|$BASE_DIR/logs|g" \
  -e "s|__HOUR__|$HOUR|g" \
  -e "s|__MINUTE__|$MINUTE|g" \
  "$SCRIPT_DIR/$LABEL.plist.template" > "$PLIST_DEST"

plutil -lint "$PLIST_DEST" >/dev/null || die "Generated plist is malformed: $PLIST_DEST"
ok "wrote $PLIST_DEST"

UID_NUM="$(id -u)"
# Unload any previous version first, otherwise bootstrap reports EALREADY.
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST_DEST" 2>/dev/null || true

if launchctl bootstrap "gui/$UID_NUM" "$PLIST_DEST" 2>/dev/null; then
  ok "loaded via launchctl bootstrap"
elif launchctl load -w "$PLIST_DEST" 2>/dev/null; then
  ok "loaded via launchctl load (older macOS)"
else
  die "Could not load the LaunchAgent. Try manually:
      launchctl bootstrap gui/$UID_NUM $PLIST_DEST"
fi

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  ok "launchd confirms the job is registered"
else
  warn "launchd did not confirm registration — check: launchctl list | grep $LABEL"
fi

# --- 7. done -------------------------------------------------------------
echo
bold "Installed."
cat <<EOF

  Runs daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE") local time.
  If the Mac is asleep or switched off then, it collects on the next wake or login.

  Output   : $BASE_DIR/data/<YYYY-MM-DD>/<post-id>_<handle>/post.md
  Logs     : $BASE_DIR/logs/scraper.log

  Useful commands (from $PROJECT_DIR):
    .venv/bin/python3 -m xscraper status              show coverage and recent runs
    .venv/bin/python3 -m xscraper run --force         collect right now
    .venv/bin/python3 -m xscraper run --dry-run       show queries, call nothing
    launchctl kickstart -k gui/$UID_NUM/$LABEL        make launchd fire it now
    bash scripts/uninstall.sh                         remove the schedule

  Run the first collection now with:
    .venv/bin/python3 -m xscraper run --force

EOF
