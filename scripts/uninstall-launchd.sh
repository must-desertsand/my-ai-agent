#!/usr/bin/env bash
# Stop and remove Haseeb's Agent launchd jobs. Logs and data/ are kept.
set -euo pipefail

DEST="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
for label in com.haseeb.agent.tick com.haseeb.agent.insights com.haseeb.agent.weekly com.haseeb.agent.dashboard; do
  if launchctl bootout "$DOMAIN/$label" 2>/dev/null; then
    echo "stopped $label"
  else
    echo "$label was not loaded"
  fi
  rm -f "$DEST/$label.plist"
done
