#!/bin/bash
# Removes the schedule. Never touches collected data, logs or state.
set -euo pipefail

LABEL="com.kiran.xscraper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

echo "Removing the LaunchAgent…"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST" 2>/dev/null || true

if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  echo "  ✓ deleted $PLIST"
else
  echo "  ! no plist found at $PLIST (already removed?)"
fi

echo
echo "The scheduled job is gone. Your collected posts are untouched."
echo "To remove the data too, delete the BASE_DIR from .env (default ~/XScraper) yourself."
