"""Out-of-band alert (final review I7): a macOS notification when the agent cannot reach Haseeb through Slack.

Two triggers, both checked by the tick (and by `insights`) after its jobs ran:
- three consecutive ticks in which Slack calls were attempted and every one of them failed (ticks with no Slack
  call neither extend nor break the streak);
- a job could not send a DM failure notice (it files a request with `request_alert`).

At most one notification an hour. The banner is shown by notify.py (one code path for every banner): the text is
fixed and reaches osascript as an argument, never as AppleScript source. The notifier is injectable for tests.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import controls
from .config import Settings
from .models import Msg
from .notify import notify
from .store import Store

log = logging.getLogger(__name__)

ALERT_TEXT = "Haseeb's Agent can't reach Slack — check tick.err.log"
ALERT_EVERY = timedelta(hours=1)
OUTAGE_TICKS = 3

_STREAK = "alert_slack_streak"
_LAST = "alert_last"
_REQUEST = "alert_request"


def notifier_for(settings: Settings) -> Callable[[str], bool]:
    """The default notifier: a notify() banner of kind `alert`, with a sound. The title is notify's own."""
    def show(text: str) -> bool:
        return notify(settings, "alert", text, sound=True)
    return show


class SlackHealth:
    """A SlackGateway wrapper that counts calls and SlackError failures (reset per tick)."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.failures = 0

    def reset(self) -> None:
        self.calls = self.failures = 0

    def _do(self, name: str, *args, **kwargs):
        from .slack_gateway import SlackError, SlackPaused
        self.calls += 1
        try:
            return getattr(self.inner, name)(*args, **kwargs)
        except SlackPaused:
            self.calls -= 1  # refused before any call (the dashboard's Pause): no call and no failure, so no outage
            raise
        except SlackError:
            self.failures += 1
            raise

    def read_channel(self, channel_id: str, oldest: str | None, limit: int = 50, *, latest: str | None = None):
        if latest is None:
            return self._do("read_channel", channel_id, oldest, limit)
        return self._do("read_channel", channel_id, oldest, limit, latest=latest)

    def read_thread(self, channel_id: str, ts: str):
        return self._do("read_thread", channel_id, ts)

    def send(self, channel_id: str, text: str, thread_ts: str | None = None) -> str:
        return self._do("send", channel_id, text, thread_ts=thread_ts)

    def react(self, channel_id: str, ts: str, emoji: str) -> None:
        return self._do("react", channel_id, ts, emoji)

    def read_file(self, file_id: str):
        return self._do("read_file", file_id)

    def upload_pdf(self, channel_id: str, path: Path, title: str, thread_ts: str | None, comment: str | None) -> None:
        return self._do("upload_pdf", channel_id, path, title, thread_ts, comment)

    def read_profile(self) -> str:
        return self._do("read_profile")

    def list_channels(self):
        return self._do("list_channels")


def request_alert(store: Store, reason: str) -> None:
    """A job's DM failure notice could not be sent: ask the next check to alert. Never raises. Not while the agent
    is paused: a notice that could not go out because Haseeb paused the agent is no outage."""
    try:
        if controls.paused_since(store):
            return
        store.set_cursor(_REQUEST, " ".join(reason.split())[:100] or "notice not sent")
    except Exception:  # noqa: BLE001 - the alert path must never take a job down
        log.exception("could not file an alert request")


def _fire(store: Store, now: datetime, notifier, why: str) -> bool:
    last = store.get_cursor(_LAST)
    try:
        if last and now - datetime.fromisoformat(last) < ALERT_EVERY:
            return False
    except (ValueError, TypeError):
        pass
    try:
        shown = notifier(ALERT_TEXT)
    except Exception as e:  # noqa: BLE001 - nothing the notifier does may break the tick
        log.warning("alert notification failed: %s", type(e).__name__)
        return False
    if shown is False:  # notify() found no working notifier: not shown, so the throttle does not start
        return False
    store.set_cursor(_LAST, now.isoformat())
    log.warning("alert raised: %s", why)
    return True


def check(store: Store, now: datetime, notifier, health: SlackHealth | None = None) -> bool:
    """Update the outage streak from `health` (this tick's Slack calls) and alert if a trigger holds."""
    try:
        streak = int(store.get_cursor(_STREAK) or 0)
    except ValueError:
        streak = 0
    if health is not None and health.calls:
        streak = streak + 1 if health.failures == health.calls else 0
        store.set_cursor(_STREAK, str(streak))
    requested = store.get_cursor(_REQUEST)
    if requested:
        store.set_cursor(_REQUEST, "")
        return _fire(store, now, notifier, f"request: {requested}")
    if streak >= OUTAGE_TICKS:
        return _fire(store, now, notifier, f"{streak} ticks with every Slack call failing")
    return False
