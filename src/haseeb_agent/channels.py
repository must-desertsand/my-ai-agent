"""The channels Haseeb is a member of, and which of them a post may go to (docs/superpowers/specs/2026-10-22-post-channel-design.md).

An idea or a message of his own can be posted to any channel he is in. The leadership channel (`settings.channel_id`) stays the
default and is always a valid target. This module owns everything about the others:

The list. The gateway's read-only `list_channels()` returns the connector's page as `Channel`s, and `slack_format.parse_channels`
refuses a cut, forged or doubtful page, so a read either gives the whole list or fails. `refresh` keeps the last good read in the
cursor `slack_channels`, JSON `{"fetched_at": <iso>, "channels": [{"id", "name", "private"}]}`. Only a good read replaces it: a failed
or refused one keeps the list that was there, and the list's age is only shown (no age makes a channel invalid). The cache is read back
through the same shape rules as the page (models.CHANNEL_ID and CHANNEL_NAME) and a channel that fails them is dropped, so a damaged or
hand-edited cache cannot put markup, a very long name or a DM id in front of the dashboard or a post.

When. The tick (cli.CHANNELS_JOB) refreshes a list that is missing or a day old, and makes one try when a refresh was requested (the
cursor `slack_channels_refresh`, which the dashboard sets because it never calls Slack itself, and which the tick takes; the dashboard
may set it once in 10 minutes, `ask_refresh`). After a try that
failed, the next automatic one waits at least 6 hours; a requested one does not wait. `slack_channels_tried` is the time of the last
try, marked before the call, so an unexpected error, or a tick stopped by its watchdog, waits too and no run is paid for every minute.
Never while the agent is paused: a paused tick makes no Slack call (ruling 1), and the list is read again after Resume. The tick also
leaves it to after the huddle window while the Next Todo is open (cli.run_tick).

Where a post goes. `targets` are the leadership channel first, then the others A-Z by name, no duplicates. `resolve` turns what Haseeb
typed (an id, `#name`, `name`, Slack's `<#ID|name>` or `<#ID>`, in any case) into one of the targets, and into nothing else: an id or a
name that is not in the list, a DM, a user, a text that fits two channels, or any look-alike with a non-ASCII letter gets None.
`record_post_channel` writes the channel a draft was posted to (the cursor `post_channel:<item id>`, a channel id and nothing else), and
`post_channel` reads it back for every reader of a post (the permalink, the deck attach, the thread watch, the dashboard's link); a post
with no record, which includes every post made before this existed, is in the leadership channel.

Channel names come from Slack and are untrusted: they are checked, shown as text only, and never put in a model prompt.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from . import controls
from .config import Settings
from .models import CHANNEL_ID, CHANNEL_NAME, Channel  # Channel is defined with the other shared values, and used as channels.Channel
from .slack_gateway import SlackError, SlackGateway
from .store import Store

log = logging.getLogger(__name__)

__all__ = ["ASKED_KEY", "ASK_EVERY", "CACHE_KEY", "Channel", "MAX_AGE", "POST_CHANNEL_PREFIX", "REFRESH_KEY", "RETRY_AFTER",
           "TRIED_KEY", "ask_refresh", "cached", "due", "post_channel", "record_post_channel", "recorded_post_channel", "refresh",
           "request_refresh", "requested", "resolve", "take_request", "targets", "wanted"]

CACHE_KEY = "slack_channels"                # {"fetched_at": <iso>, "channels": [{"id", "name", "private"}]}
TRIED_KEY = "slack_channels_tried"          # the time of the last try, marked before the read
REFRESH_KEY = "slack_channels_refresh"      # a refresh was asked for (the dashboard sets it, the tick takes it)
ASKED_KEY = "slack_channels_asked"          # the time of the last request the dashboard made (its throttle, `ask_refresh`)
POST_CHANNEL_PREFIX = "post_channel:"       # + the item id: the channel id the draft was posted to
MAX_AGE = timedelta(days=1)                 # the list is read again once it is this old
RETRY_AFTER = timedelta(hours=6)            # the next automatic try waits this long after a try
ASK_EVERY = timedelta(minutes=10)           # the dashboard may ask for a refresh this often

# What Slack makes of a channel Haseeb picks by autocomplete: <#C093L3Y8UM6|survive-ai> or <#C093L3Y8UM6>.
_MARKUP = re.compile(r"<#([A-Za-z0-9]+)(?:\|[^<>|]*)?>")


def _need_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("a timezone-aware time is needed")


def _aware(value: object) -> datetime | None:
    """A stored time: an ISO text with an offset. Anything else (not a text, not a time, no offset) is None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.utcoffset() is not None else None


def _within(now: datetime, then: datetime | None, span: timedelta) -> bool:
    """`then` is no later than `now` and less than `span` before it. A time in the future (a clock that went back, a damaged
    cursor) is within nothing, so it can never hold a refresh off."""
    return then is not None and timedelta(0) <= now - then < span


def _valid(c: Channel) -> bool:
    """The shape rules, for a channel from anywhere: the page (already checked there), a gateway, or the cache."""
    return (isinstance(c.id, str) and isinstance(c.name, str) and isinstance(c.private, bool)
            and CHANNEL_ID.fullmatch(c.id) is not None and CHANNEL_NAME.fullmatch(c.name) is not None)


def _channel(row: object) -> Channel | None:
    """A cached row as a Channel, or None when it is not one."""
    if not isinstance(row, dict):
        return None
    channel = Channel(row.get("id"), row.get("name"), row.get("private"))  # type: ignore[arg-type]
    return channel if _valid(channel) else None


# --------------------------------------------------------------------------------------------------------------------
# The list and its cache
# --------------------------------------------------------------------------------------------------------------------

def cached(store: Store) -> tuple[datetime | None, list[Channel]]:
    """When the list was read (None when that is unknown) and the channels, in the order they were stored. Nothing cached, or a
    cache that is not the cache, is (None, []). A channel that fails the shape rules is dropped, a repeated id is kept once."""
    try:
        data = json.loads(store.get_cursor(CACHE_KEY) or "")
    except (ValueError, RecursionError):
        return None, []
    if not isinstance(data, dict):
        return None, []
    rows = data.get("channels")
    found: list[Channel] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        channel = _channel(row)
        if channel is not None and channel.id not in seen:
            seen.add(channel.id)
            found.append(channel)
    return _aware(data.get("fetched_at")), found


def due(store: Store, now: datetime) -> bool:
    """An automatic refresh is due: there is no list, or it is a day old, and no try was made in the last 6 hours."""
    _need_aware(now)
    fetched, _ = cached(store)
    if _within(now, fetched, MAX_AGE):
        return False
    return not _within(now, _aware(store.get_cursor(TRIED_KEY)), RETRY_AFTER)


def requested(store: Store) -> bool:
    return bool(store.get_cursor(REFRESH_KEY))


def request_refresh(store: Store, now: datetime) -> None:
    """Ask for a refresh (the dashboard's "Refresh list"). The tick makes one try at its next unpaused tick."""
    _need_aware(now)
    store.set_cursor(REFRESH_KEY, now.isoformat())


def ask_refresh(store: Store, now: datetime) -> datetime | None:
    """The dashboard's "Refresh list": `request_refresh`, at most once in ASK_EVERY (10 minutes). None when the request was made.
    Otherwise the time of the request made less than ASK_EVERY ago, and nothing is changed: a request still waiting for its tick (a
    pause, the huddle hold) keeps its own time, and one the tick has taken is not asked for again at once. The time of the last
    request is its own cursor (`slack_channels_asked`) because the tick clears the request when it takes it. A time in the future
    (a clock that went back) or one that cannot be read never holds a request off."""
    _need_aware(now)
    last = _aware(store.get_cursor(ASKED_KEY))
    if _within(now, last, ASK_EVERY):
        return last
    request_refresh(store, now)
    store.set_cursor(ASKED_KEY, now.isoformat())  # after the request: a failure in between leaves a request the page can repeat
    return None


def take_request(store: Store) -> bool:
    """True once for each request: the request is cleared, so it makes one try, whatever comes of it."""
    if not requested(store):
        return False
    store.set_cursor(REFRESH_KEY, "")
    return True


def wanted(store: Store, now: datetime) -> bool:
    """What the tick asks: a refresh was requested, or one is due."""
    return requested(store) or due(store, now)


def _paused(store: Store) -> bool:
    try:
        return controls.paused_since(store) is not None
    except Exception:  # noqa: BLE001 - a pause state that cannot be read fails closed, as the gateway's does for a write
        return True


def refresh(slack: SlackGateway, store: Store, now: datetime, *, force: bool = False) -> bool:
    """Read the list and keep it. True when this replaced the cache; False when it did not: the agent is paused (never, `force`
    or not: no Slack call is made), nothing was due (unless `force`: a requested refresh), or the read failed. A failed read
    (SlackError, a ValueError from a gateway that does not wrap its parser's, or an empty answer) keeps the list that was there,
    with its own age, and starts the 6 hours. Any other error is the caller's, after the try is marked."""
    _need_aware(now)
    if _paused(store):
        return False
    if not force and not due(store, now):
        return False
    store.set_cursor(TRIED_KEY, now.isoformat())  # before the read: whatever comes of it, the next automatic try waits
    try:
        found = slack.list_channels()
    except (SlackError, ValueError) as e:
        kept, _ = cached(store)
        log.warning("channel list: the read failed (%s); keeping the list from %s", type(e).__name__,
                    kept.date().isoformat() if kept else "never")
        return False
    good = [c for c in found if _valid(c)]  # the page parser already checked them; the cache only ever holds well-formed ones
    if not good:  # the gateway raises for a page with no channel; an empty answer must still never wipe a good list
        log.warning("channel list: the read gave no channel; keeping the list")
        return False
    if len(good) != len(found):
        log.info("channel list: %d of %d channel(s) not kept (not well-formed)", len(found) - len(good), len(found))
    store.set_cursor(CACHE_KEY, json.dumps({"fetched_at": now.isoformat(),
                                            "channels": [{"id": c.id, "name": c.name, "private": c.private} for c in good]}))
    return True


# --------------------------------------------------------------------------------------------------------------------
# Where a post may go
# --------------------------------------------------------------------------------------------------------------------

def targets(store: Store, settings: Settings) -> list[Channel]:
    """The channels a post may go to: the leadership channel first, even when the list lacks it or there is no list (it is then
    named by its id, the only name there is), then the others A-Z by name. No channel twice."""
    _, found = cached(store)
    lead = next((c for c in found if c.id == settings.channel_id), None) or Channel(settings.channel_id, settings.channel_id, False)
    rest = sorted((c for c in found if c.id != settings.channel_id), key=lambda c: (c.name, c.id))
    return [lead, *rest]


def resolve(store: Store, settings: Settings, text: str) -> Channel | None:
    """The target Haseeb named, or None. `text` is an id, `#name`, `name`, `<#ID|name>` or `<#ID>`, in any case, with any space
    around it. Slack's markup is an id and nothing else (its label is not read). Only a target is ever returned. Nothing is
    guessed: a text that fits two different channels (a name that is also another's id, a name two cached channels share), one
    with a non-ASCII letter (a look-alike), or an id or name that is not in the list gets None."""
    if not isinstance(text, str):
        return None
    typed = text.strip()
    if not typed.isascii():  # ids and names are ASCII; U+212A (the Kelvin sign) would case-fold to a k
        return None
    markup = _MARKUP.fullmatch(typed)
    key = (markup.group(1) if markup else typed.removeprefix("#")).casefold()
    if not key:
        return None
    hits = {c.id: c for c in targets(store, settings)
            if c.id.casefold() == key or (markup is None and c.name.casefold() == key)}
    return next(iter(hits.values())) if len(hits) == 1 else None


# --------------------------------------------------------------------------------------------------------------------
# The channel a draft was posted to
# --------------------------------------------------------------------------------------------------------------------

def recorded_post_channel(store: Store, item_id: str) -> str | None:
    """The channel id recorded for the draft, or None when there is none (or what is there is not a channel id)."""
    value = store.get_cursor(POST_CHANNEL_PREFIX + item_id)
    return value if value and CHANNEL_ID.fullmatch(value) else None


def post_channel(store: Store, settings: Settings, item_id: str) -> str:
    """The channel the draft was posted to: the recorded one, else the leadership channel, which is where every post made before
    the choice existed went."""
    return recorded_post_channel(store, item_id) or settings.channel_id


def record_post_channel(store: Store, item_id: str, channel_id: str) -> None:
    """Record the channel a draft is being posted to (before the send: an attempt that fails keeps its target). Only a channel
    id is recorded: a DM, a user or any text raises ValueError and writes nothing."""
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("not a channel id")
    store.set_cursor(POST_CHANNEL_PREFIX + item_id, channel_id)
