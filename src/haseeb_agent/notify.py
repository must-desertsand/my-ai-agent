"""One Mac banner per delivery to Haseeb's DM (Phase 1: the agent writes as Haseeb, so Slack never notifies him).

`notify(settings, kind, message)` shows a macOS notification that opens his self-DM when clicked. It prefers
terminal-notifier (the click action), resolved from PATH with the Homebrew path as a fallback because launchd's
PATH may not have it, and falls back to osascript, which has no click action. Under launchd it needs no extra
setup: a user LaunchAgent runs in the GUI session.

Rules:
- The banner carries a short label only: counts, times, a sanitized author name. Callers never pass draft text,
  titles from the web or teammate text.
- The message reaches osascript as an argv item, never inside the AppleScript source.
- It never raises. On failure it returns False and logs one line naming the kind and the reason, never the message.
- `kind` names the banner group (`haseeb-agent-<kind>`), so a newer banner of a kind replaces the older one.
- `runner` (and `which`, `is_file`) are injectable; a test that gives no runner gets the module hook `_run`.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable

from .config import Settings

log = logging.getLogger(__name__)

TITLE = "Haseeb's Agent"
FALLBACK_BIN = "/opt/homebrew/bin/terminal-notifier"
TIMEOUT_S = 5
MAX_CHARS = 200

# Hooks: tests replace these so no test can raise a real banner (tests/conftest.py).
_run = subprocess.run
_which = shutil.which
_is_file = os.path.isfile


def _group(kind: str) -> str:
    return "haseeb-agent-" + (re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-") or "agent")


def _terminal_notifier(which, is_file) -> str | None:
    found = which("terminal-notifier")
    if found:
        return found
    return FALLBACK_BIN if is_file(FALLBACK_BIN) else None


def _osascript(message: str, sound: bool) -> list[str]:
    show = "display notification (item 1 of argv) with title (item 2 of argv)"
    if sound:
        show += ' sound name "default"'  # a literal; the message is never part of the script
    return ["osascript", "-e", "on run argv", "-e", show, "-e", "end run", message, TITLE]


def notify(settings: Settings, kind: str, message: str, *, sound: bool = False,
           runner: Callable | None = None, which: Callable | None = None,
           is_file: Callable | None = None) -> bool:
    """Show the banner. True when a notifier exited 0. Never raises."""
    try:
        if not settings.notify_enabled:
            return False
        text = " ".join((message or "").split())[:MAX_CHARS]
        if not text:  # terminal-notifier reads stdin when -message is empty
            log.warning("notify %s: empty message, not shown", kind)
            return False
        run = runner or _run
        tn = _terminal_notifier(which or _which, is_file or _is_file)
        attempts: list[tuple[str, list[str]]] = []
        if tn:
            cmd = [tn, "-title", TITLE, "-message", text, "-group", _group(kind),
                   "-open", f"slack://channel?team={settings.slack_team_id}&id={settings.self_dm_channel}"]
            if sound:
                cmd += ["-sound", "default"]
            attempts.append(("terminal-notifier", cmd))
        attempts.append(("osascript", _osascript(text, sound)))
        reason = "no notifier"
        for name, cmd in attempts:
            try:
                done = run(cmd, capture_output=True, timeout=TIMEOUT_S, check=False)
            except subprocess.TimeoutExpired:
                # A banner may already be showing; a second one from the fallback would double it.
                reason = f"{name} timed out"
                break
            except Exception as e:  # noqa: BLE001 - a banner must never break a job
                reason = f"{name} {type(e).__name__}"
                continue
            if getattr(done, "returncode", 1) == 0:
                return True
            reason = f"{name} exit {getattr(done, 'returncode', '?')}"
        log.warning("notify %s failed: %s", kind, reason)
        return False
    except Exception as e:  # noqa: BLE001 - including a bad Settings
        log.warning("notify %s failed: %s", kind, type(e).__name__)
        return False
