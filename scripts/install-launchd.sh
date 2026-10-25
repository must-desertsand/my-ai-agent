#!/usr/bin/env bash
# Install Haseeb's Agent as four launchd user agents: com.haseeb.agent.tick (every 60 s),
# com.haseeb.agent.insights (08:15 Mon-Fri), com.haseeb.agent.weekly (Fri 17:00), Mac local time, and
# com.haseeb.agent.dashboard (the local dashboard on 127.0.0.1, kept alive).
#
#   scripts/install-launchd.sh                    fill in paths, copy to ~/Library/LaunchAgents, bootstrap
#   scripts/install-launchd.sh --render-only DIR  fill in paths into DIR and lint them; no launchctl, no copy
#
# Installing makes the agent live in Lion's channel (todo auto-post). Ruling R32: install only after the final
# review, and tell Haseeb first.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV="${UV:-$HOME/.local/bin/uv}"
LABELS=(com.haseeb.agent.tick com.haseeb.agent.insights com.haseeb.agent.weekly com.haseeb.agent.dashboard)
LOGS="$HOME/Library/Logs/haseeb-agent"

render_only=""
if [[ "${1:-}" == "--render-only" ]]; then
  render_only="${2:?usage: $0 --render-only DIR}"
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--render-only DIR]" >&2
  exit 2
fi

# The paths go into XML and a sed replacement: refuse characters that would break either.
for p in "$REPO" "$UV" "$HOME"; do
  if [[ "$p" == *[\|\&\<\>\\]* ]]; then
    echo "error: path contains a character this script cannot embed: $p" >&2
    exit 1
  fi
done

if [[ ! -x "$UV" ]]; then
  echo "error: uv not found at $UV (set UV=/path/to/uv)" >&2
  exit 1
fi

# launchd's StartCalendarInterval uses the Mac's local time; 08:15 is meant as Asia/Kuwait (UTC+3).
if [[ "$(date +%z)" != "+0300" ]]; then
  echo "WARNING: the Mac's time zone is $(date +%Z) ($(date +%z)), not Kuwait (+0300)." >&2
  echo "WARNING: the 08:15 insights run will fire at 08:15 local time, not 08:15 Kuwait time." >&2
fi

render() {  # render <label> <dest-file>
  sed -e "s|__UV__|$UV|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/deploy/launchd/$1.plist" > "$2"
  plutil -lint "$2" >/dev/null
}

if [[ -n "$render_only" ]]; then
  mkdir -p "$render_only"
  for label in "${LABELS[@]}"; do
    render "$label" "$render_only/$label.plist"
    echo "rendered $render_only/$label.plist"
  done
  exit 0
fi

DEST="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
mkdir -p "$DEST" "$LOGS" "$REPO/data"
for label in "${LABELS[@]}"; do
  dst="$DEST/$label.plist"
  render "$label" "$dst"
  # Reinstall: drop the old definition and wait (up to ~5 s) until launchd has really let go of it.
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  for _ in $(seq 1 50); do
    launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
    sleep 0.1
  done
  # A label left disabled by an earlier `launchctl disable` cannot be bootstrapped, so enable it first.
  launchctl enable "$DOMAIN/$label"
  if ! launchctl bootstrap "$DOMAIN" "$dst"; then
    echo "bootstrap of $label failed, retrying once" >&2
    sleep 1
    launchctl bootstrap "$DOMAIN" "$dst"
  fi
  launchctl enable "$DOMAIN/$label"
  echo "installed $label"
done
launchctl list | grep com.haseeb.agent || true
echo "logs: $LOGS"
