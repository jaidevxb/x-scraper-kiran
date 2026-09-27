#!/bin/bash
# Wrapper that launchd calls. Resolves its own location so the LaunchAgent does
# not care where the project lives.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python3"

cd "$PROJECT_DIR" || exit 1

if [ ! -x "$VENV_PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') FATAL: no virtualenv at $VENV_PY. Run scripts/install.sh." >&2
  exit 2
fi

# launchd hands us a bare PATH; some tooling expects the usual directories.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1
export PYTHONUTF8=1

exec "$VENV_PY" -m xscraper run --trigger "${1:-scheduled}"
