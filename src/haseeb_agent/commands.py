"""Haseeb's DM thread commands: post, post #channel, edit: <instruction>, shorter, skip, and `say: <rough message>` at the top level.

Each command is a reply by Haseeb in an item's DM thread. A reply is recorded as handled before any slow work
starts, so a second tick (or a crash and restart) can never run it twice.

Concurrency across processes is handled by the Task 12 tick lock; nothing here is safe against two simultaneous
ticks. In Phase 1 the agent sends as Haseeb, so its own DM messages look like Haseeb's. Every ts it sends is
recorded as `own:{ts}` (see send_dm) and process_dm ignores those replies (R23).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

from . import channels, events
from .alert import request_alert
from .config import Settings
from .models import CHANNEL_NAME, Item, Msg
from .slack_gateway import SlackError, SlackGateway, SlackPaused, SlackTimeout, SlackUnavailable
from .store import Store

log = logging.getLogger(__name__)

SHORTER_INSTRUCTION = "Make it shorter and tighter, keep the facts."
MAX_ATTEMPTS = 3
WINDOW = timedelta(days=7)
DM_PAGE = 20  # I5 (R37): newest 20 top-level DM messages since the oldest open item
INTERRUPTED = "⚠️ Post was interrupted — check the channel before re-posting"
MAY_HAVE_POSTED = "⚠️ Post may have gone out — check the channel before re-posting ({reason})"
MAX_PREF_CHARS = 500
EARLIER_POST = "An earlier post may have gone out — check the channel before posting again"  # C1, R1
# Where a post goes (docs/superpowers/specs/2026-10-22-post-channel-design.md). An idea and a message of his own may go to any
# channel in his list; every other kind posts only to its thread in the leadership channel and refuses a channel.
CHANNEL_KINDS = ("insight", "message")
LEADERSHIP_ONLY = "this draft posts only to its thread in the leadership channel"
# The words of every "may have gone out" line that say where to look. For a draft with a recorded channel they name it:
# "check #survive-ai" (`name_the_channel`); for every other draft they stay as they are, and "the channel" is the leadership one.
CHECK_THE_CHANNEL = "check the channel"
DASHBOARD_NOTE = "_Done from the dashboard_"  # under the status line of a command run from the dashboard
PAUSED_NOTHING_SENT = "paused: nothing was sent"  # a command the pause refused before any write (the dashboard's row), any verb
# A DM command the pause refused mid-run leaves no reaction or reply (every DM write is refused). So one notice per item is
# queued (this cursor family) and sent in the item's DM thread by the first process_dm after Resume, retried until sent.
PAUSED_COMMAND_NOTICE = "A command you sent during the pause was not run — send it again"
PAUSED_NOTICE_PREFIX = "paused_command_notice:"

_EDIT = re.compile(r"edit\s*:\s*(\S.*)", re.IGNORECASE | re.DOTALL)
# `post <channel>`: "post" and ONE token that is written as a channel: `#survive-ai`, or Slack's own markup for a channel picked by
# autocomplete (`<#C093L3Y8UM6|survive-ai>`, `<#C093L3Y8UM6>`). A bare word after `post` (`post it`, `post now`, `post survive-ai`) is
# ordinary chat, exactly as before the choice existed. It must not be a command: a reply like "post it" would cancel the Next
# Todo's auto-post without telling him (`jobs.todo._cancel_for_reply` leaves the notice to a command's own reply), and a channel
# called #it or #now would be posted to with no confirm. What follows is only looked up in his list (`post_target`), never taken as is.
_POST_TO = re.compile(r"post\s+(#\S+|<#[A-Za-z0-9]+(?:\|[^<>|\s]*)?>)", re.IGNORECASE)
# `say: <rough message>`: a top-level DM message from Haseeb that starts with "say:", in any case, after any leading
# spaces. A thread reply, a message from anyone else or a bot, and a message that only has "say:" somewhere inside it
# are ordinary chat.
_SAY = re.compile(r"\s*say:(.*)", re.IGNORECASE | re.DOTALL)
SAY_FIRST_LOOKBACK_S = 600  # the first look for `say:` messages reaches this far back, never to older chat
SAY_IDLE_EVERY_S = 240      # with no open item the DM is read for `say:` at most this often (a read is a Claude run)
SAY_MAX_AGE_S = 3600        # a `say:` older than this (an hour, not counting a huddle hold) is not drafted: send it again
SAY_TOO_OLD = "⚠️ too old to polish; send it again"
# The last time a tick was inside the huddle hold (R33's predicate, `cli.huddle_open`): composer rows and `say:` drafts
# wait while it lasts, and the age limits below count from it, so a wait of up to 100 minutes does not make them stale.
HELD_KEY = "message_held_at"


class Unsettled(ValueError):
    """A refusal that leaves the item exactly as it is: an earlier channel write's outcome is not known yet (CB3).
    Marking the item failed here would end the todo job's run (failed is terminal) before it records that outcome."""


class Declined(Unsettled):
    """A refusal of our own, raised by `post_target` before anything is marked or sent: the channel asked for is not one this draft
    may go to (not in his list, a DM or user id, a feedback draft or the Next Todo with a channel at all), or a retry is pinned to
    another channel. It leaves the item exactly as it is, like `Unsettled`: no failure time, no attempt, no lock (R16 locks a draft
    only after a post that may have gone out). The message is one short line for Haseeb. Never raise it from a handler: that runs
    after `inflight:` is set, and an `Unsettled` leaves that marker behind (a handler's refusal is a plain ValueError)."""


@dataclass(frozen=True)
class Command:
    kind: Literal["post", "edit", "shorter", "skip", "attach"]
    arg: str | None = None


class Handlers(Protocol):
    def post(self, item: Item, reply_ts: str | None = None, channel_id: str | None = None) -> str:
        """Post the item and return the permalink, in the channel it went to. `reply_ts` is the ts of Haseeb's `post` reply,
        so a handler can post the version he was looking at (N2). `channel_id` is the channel he chose, which the runner has
        looked up in his list just before (`post_target`); None is the default, the leadership channel. Only an idea and a
        message take one: the handlers of the other kinds refuse it with a plain ValueError (a backstop: `post_target` refuses first)."""

    def revise(self, item: Item, instruction: str) -> Item:
        """Write a revised version into the DM (the handler sends it) and return it."""


def parse_command(text: str) -> Command | None:
    t = (text or "").strip()
    low = t.lower()
    if low in ("post", "shorter", "skip", "attach"):
        return Command(low)  # type: ignore[arg-type]
    if m := _EDIT.fullmatch(t):
        return Command("edit", m.group(1).strip())
    if m := _POST_TO.fullmatch(t):  # `post #survive-ai`, or what Slack makes of a channel picked by autocomplete
        return Command("post", m.group(1))
    return None


def say_text(msg: Msg, settings: Settings) -> str | None:
    """The rough message of a `say:` DM message, "" when nothing follows the colon, or None when `msg` is no `say:`
    command at all."""
    if msg.user_id != settings.haseeb_id or msg.is_bot:
        return None
    if msg.thread_ts and msg.thread_ts != msg.ts:  # a reply, not a message of its own
        return None
    m = _SAY.fullmatch(msg.text or "")
    return m.group(1).strip() if m else None


_MD_LINK_TEXT = re.compile(r"\[([^\[\]]*)\]\(([^()\s]*)\)")
_SLACK_LINK = re.compile(r"<([^<>|\s]+)(?:\|([^<>]*))?>")
_SENT_USING = re.compile(r"sentusing[a-z0-9]{0,30}")  # the connector's "Sent using @Claude" line, as a digest


def _digest(text: str) -> str:
    """The WHOLE text reduced to its letters and digits, so `**1. Gold**` and the `*1. Gold*` the connector may store
    for it compare equal, and a link compares the same whether it is `[title](url)` or `<url|title>`. Empty when
    nothing is left to compare."""
    text = _MD_LINK_TEXT.sub(r"\1 \2", text or "")
    text = _SLACK_LINK.sub(lambda m: f"{m.group(2) or ''} {m.group(1)}", text)
    return re.sub(r"[\W_]+", "", text).casefold()


def _same_text(want: str, found: str) -> bool:
    """`want` is the digest of the text that was sent. A message found in the DM is that text when its digest is the
    same, or the same followed only by the connector's "Sent using" line. Never a prefix or a first line: a card that
    shares a header (every message draft starts with the same line) is not the same message."""
    got = _digest(found)
    return got == want or (got.startswith(want) and _SENT_USING.fullmatch(got[len(want):]) is not None)


def _timeout_key(text: str, thread_ts: str | None) -> str | None:
    digest = _digest(text)
    if not digest:
        return None
    return "dm_timeout:" + hashlib.sha1(f"{thread_ts or ''}|{digest}".encode()).hexdigest()[:20]


MARKER_TTL_S = 3600     # R43: a timeout marker older than this is dropped (a day-1 line must not swallow day 2's)
ADOPT_SLACK_S = 30      # R43: a message may be adopted if its ts is at most this much before the attempt began


def _marker_time(store: Store, marker: str) -> float | None:
    """The epoch time of the attempt that timed out, or None (no marker, or a value that is not a time)."""
    try:
        return float(store.get_cursor(marker) or "")
    except ValueError:
        return None


def _adopt_landed(slack: SlackGateway, store: Store, settings: Settings, text: str, thread_ts: str | None,
                  since: float) -> str | None:
    """R43: after a timeout the send may have reached the DM. Look for one of Haseeb's own messages, not yet recorded
    as the agent's, with the same WHOLE text (letters and digits only, and the connector's "Sent using" line allowed)
    and a ts from the attempt on (`since` minus 30 s): a top-level message on the newest 20 of the DM page, a thread
    reply in the thread (replies are not on the channel page). The newest match wins. Returns its ts, or None. A
    missed adoption costs a duplicate card; a wrong one would put a different text under Haseeb's `post`, so only the
    same text is adopted."""
    want = _digest(text)
    try:
        if thread_ts is None:
            found = slack.read_channel(settings.self_dm_id, None, DM_PAGE)
        else:
            _, found = slack.read_thread(settings.self_dm_id, thread_ts)
    except SlackError as e:
        log.warning("could not look for a timed-out DM message, sending again: %s", e)
        return None
    floor = Decimal(str(since - ADOPT_SLACK_S))
    mine = [m for m in found if m.user_id == settings.haseeb_id and not m.is_bot
            and _same_text(want, m.text) and _ts(m.ts) >= floor and store.get_cursor(f"own:{m.ts}") is None]
    return max(mine, key=lambda m: _ts(m.ts)).ts if mine else None


def send_dm(slack: SlackGateway, store: Store, settings: Settings, text: str, thread_ts: str | None = None, *,
            clock: Callable[[], float] = time.time) -> str:
    """Send to Haseeb's self-DM and record the ts as the agent's own message (R23).

    Every job and handler must send DM messages through this helper. (A crash between the send and the record
    would leave one unrecorded message; it can only be misread if its whole text is a command.)

    R43: a send that timed out may have reached the DM, so the helper remembers it (by thread and the whole text) with
    the time the attempt began. The next send of the same message, within an hour, first reads the DM for a message
    of Haseeb's with that same text from that time on; the newest one is adopted (its ts is returned and recorded
    as own) instead of sending a duplicate. A different text, even under the same first line, is a different message
    and is sent. An older marker is dropped. A normal send reads nothing. `clock` is the epoch clock (tests).
    """
    marker = _timeout_key(text, thread_ts)
    now = clock()
    since = _marker_time(store, marker) if marker else None
    if marker and store.get_cursor(marker):
        if since is None or now - since > MARKER_TTL_S:
            store.set_cursor(marker, "")  # expired (or unreadable): this is a new message
            since = None
        else:
            ts = _adopt_landed(slack, store, settings, text, thread_ts, since)
            if ts is not None:
                store.set_cursor(f"own:{ts}", "1")
                store.set_cursor(marker, "")
                return ts
    try:
        ts = slack.send(settings.self_dm_id, text, thread_ts=thread_ts)
    except SlackTimeout:
        if marker and since is None:  # a repeat keeps the first attempt's time
            store.set_cursor(marker, repr(now))
        raise
    if marker and store.get_cursor(marker):
        store.set_cursor(marker, "")
    store.set_cursor(f"own:{ts}", "1")
    return ts


MAX_DM_SENDS = 3
TIMEOUT_COUNT_EVERY = timedelta(minutes=10)  # R43: a timeout counts toward the cap at most this often


def _maybe_landed(exc: Exception) -> bool:
    """A send that failed this way may still have reached Slack: the model's call differed from the request, a
    tool outside the allowlist was offered or called, or the run timed out (its stream is lost). An outage
    (SlackUnavailable: the run failed before any Slack call) never has (N3)."""
    if isinstance(exc, SlackUnavailable):
        return False
    text = str(exc)
    return "tool-call mismatch" in text or "unexpected tool" in text


def dm_send_failed(store: Store, key: str, exc: Exception, now: datetime | None = None) -> bool:
    """I4 (caps R26): record one failed DM send of one part (`key`). True when that part must not be sent again:
    after MAX_DM_SENDS counted failures, or once a resend after a may-have-landed failure has failed too (such a
    send gets at most one resend).

    N3: an outage (SlackUnavailable) does not count at all, so a 10–20 min outage (or `insights`' immediate second
    delivery attempt during one) never uses up the cap and loses the day's insights. It is only tallied as
    `outages`. Retries stay bounded: each job retries only its own open work on its own cadence (insights only
    today's), and the I7 alert fires on an outage streak.

    R43: a timeout (SlackTimeout) counts at most once per 10 minutes (measured from the last counted one), because
    a timeout says nothing about whether the message landed and the resend looks for it first (send_dm). `now` is
    the clock (tests)."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:  # a naive `now` is taken as UTC; it must never raise below
        now = now.replace(tzinfo=UTC)
    try:
        state = json.loads(store.get_cursor(f"dm_fail:{key}") or "{}")
        n, after = int(state.get("n", 0)), int(state.get("after_mismatch", 0))
        outages = int(state.get("outages", 0))
        counted = state.get("t_timeout")
    except (ValueError, TypeError, AttributeError):
        n, after, outages, counted = 0, 0, 0, None
    try:
        counted_at = datetime.fromisoformat(counted) if counted else None
        if counted_at is not None and counted_at.tzinfo is None:
            counted_at = counted_at.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        counted, counted_at = None, None  # an unreadable stamp loses only the 10-minute window, not the counts
    timeout = isinstance(exc, SlackTimeout)
    if isinstance(exc, SlackUnavailable):
        outages += 1
    elif timeout and counted_at is not None and timedelta(0) <= now - counted_at < TIMEOUT_COUNT_EVERY:
        pass  # R43: the same stretch of timeouts; already counted
    else:
        n += 1
        if after or _maybe_landed(exc):
            after += 1
        if timeout:
            counted = now.isoformat()
    store.set_cursor(f"dm_fail:{key}", json.dumps({"n": n, "after_mismatch": after, "outages": outages,
                                                   **({"t_timeout": counted} if counted else {})}))
    return n >= MAX_DM_SENDS or after >= 2


def post_failed_key(item_id: str) -> str:
    return f"post_failed_at:{item_id}"


def _wall() -> float:
    """The wall clock (epoch seconds). Tests that run on fixed clocks pin it."""
    return time.time()


def send_key(item_id: str) -> str:
    return f"send_started:{item_id}"


def mark_send_started(store: Store, item_id: str, at: float | None = None) -> None:
    """Set right before an item's channel send and cleared with its outcome. While set, a failure of the post is
    recorded as may-have-posted whatever the exception (`_Runner._fail`). `at`: epoch seconds (default: now)."""
    store.set_cursor(send_key(item_id), f"{_wall() if at is None else at:.6f}")


def clear_send_started(store: Store, item_id: str) -> None:
    store.set_cursor(send_key(item_id), "")


def mark_post_failed(store: Store, item_id: str, now: datetime | None = None, *, wall: bool = True) -> None:
    """Review C1: record when a `post` of this item may have gone out (epoch seconds, the later of `now` and, with
    `wall`, the wall clock, and never moved back). A dashboard `post` clicked at or before it is refused, so a
    double click never becomes a second channel write; only a click made after the failure (an informed override)
    posts. The todo job passes its own clock (`wall=False`), which is the wall clock in production."""
    at = max(now.timestamp() if now else 0.0, _wall() if wall else 0.0)
    try:
        old = Decimal(store.get_cursor(post_failed_key(item_id)) or "0")
    except ArithmeticError:
        old = Decimal(0)
    store.set_cursor(post_failed_key(item_id), f"{max(old, Decimal(f'{at:.6f}'))}")


# --------------------------------------------------------------------------
# Where a post goes: the channel he named, checked against his list just before the send (post-channel design)
# --------------------------------------------------------------------------

_PICKED = re.compile(r"<#([A-Za-z0-9]+)(?:\|([^<>|]*))?>")  # Slack's markup for a channel picked by autocomplete
_ID_LIKE = re.compile(r"[A-Za-z][A-Z0-9]{8,12}")             # the shape of any Slack id, a C... channel's or a D... DM's


def channel_label(store: Store, settings: Settings, channel_id: str) -> str:
    """How a channel is said to Haseeb: `#name` from the list the agent holds, "the leadership channel" for the leadership channel
    when the list does not know its name, else `channel <id>` (a channel he has left since). Names are validated when they are
    cached (models.CHANNEL_NAME), so they hold no markup and no space."""
    for known in channels.cached(store)[1]:
        if known.id == channel_id:
            return f"#{known.name}"
    return "the leadership channel" if channel_id == settings.channel_id else f"channel {channel_id}"


def where_posted(store: Store, settings: Settings, item_id: str) -> str | None:
    """The channel a draft's post was recorded to (before its send), said to Haseeb; None when nothing is recorded (a feedback draft,
    the Next Todo, a post made before the choice existed: all of them in the leadership channel's thread, as ever)."""
    recorded = channels.recorded_post_channel(store, item_id)
    return None if recorded is None else channel_label(store, settings, recorded)


def name_the_channel(text: str, store: Store, settings: Settings, item_id: str) -> str:
    """`text`, a "may have gone out" line, with "check the channel" naming the channel the draft's post was recorded to ("check
    #survive-ai"). Without a record the line is as it was. Never raises: the line must reach Haseeb whatever the store says."""
    try:
        label = where_posted(store, settings, item_id)
    except Exception:  # noqa: BLE001
        return text
    return text if label is None else text.replace(CHECK_THE_CHANNEL, f"check {label}", 1)


def _asked_for(arg: str) -> str:
    """What he typed, as it may be said back to him: `#name` for a name, the id for an id, and "that" for anything else (a link, a
    sentence, markup that fits neither). Only a token of the shapes Slack's own names and ids have is repeated."""
    text = arg.strip()
    if m := _PICKED.fullmatch(text):  # its label when that is a name, else its id
        text = m.group(2) if m.group(2) and m.group(2).isascii() and CHANNEL_NAME.fullmatch(m.group(2).casefold()) else m.group(1)
    text = text.removeprefix("#")
    if _ID_LIKE.fullmatch(text):
        return text
    return f"#{text}" if text.isascii() and CHANNEL_NAME.fullmatch(text.casefold()) else "that"


def not_in_list(store: Store, settings: Settings, arg: str) -> str:
    """The one reply for a channel that is not one of his: what he asked for, the date of the list it was looked up in, and that
    nothing was posted."""
    fetched, found = channels.cached(store)
    age = (f"list from {fetched.astimezone(settings.tz).date().isoformat()}" if fetched is not None
           else "list from an unknown date" if found else "no list yet")
    return f"{_asked_for(arg)} is not one of your channels ({age}) · nothing posted"


def post_target(store: Store, settings: Settings, item: Item, arg: str | None) -> str | None:
    """Where Haseeb's `post` of `item` goes: the id of the channel he named (`arg`: a DM word, or the dashboard's channel id), or None
    when he named none (the default: the leadership channel). This is the check that runs again just before the send, whatever the
    page showed when he clicked. Raises `Declined`, with a line for him, and writes nothing, when:
      - the draft is not an idea or a message and a channel was named at all (a feedback draft and the Next Todo post only to their
        thread in the leadership channel);
      - what he named is not one of his channels: not in the list, a DM or a user id, a name two channels share, a channel he left
        since the page was drawn (`channels.resolve` takes only the leadership channel and the cached list);
      - a post of this draft may already have gone out (`post_failed_at:`) and this is another channel than the one it was recorded
        to: a retry stays where the first went, so that one draft is never in two channels. With no record that channel is the
        leadership channel, where every post made before the choice existed went."""
    named = arg.strip() if isinstance(arg, str) else ""
    if item.kind not in CHANNEL_KINDS:
        if named:
            raise Declined(LEADERSHIP_ONLY)
        return None
    target = None
    if named:
        channel = channels.resolve(store, settings, named)
        if channel is None:
            raise Declined(not_in_list(store, settings, named))
        target = channel.id
    if store.get_cursor(post_failed_key(item.id)):
        there = channels.post_channel(store, settings, item.id)
        if (target or settings.channel_id) != there:
            raise Declined(f"this draft may already be in {channel_label(store, settings, there)}; if it isn't there, post it by hand")
    return target


def _clean_instruction(text: str) -> str:
    return " ".join(text.split())[:MAX_PREF_CHARS]


def _ts(value: str) -> Decimal:
    return Decimal(value)  # exact; "sec.micro" compares numerically (R1)


def _one_line(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= 200 else text[:199] + "…"


class _Runner:
    def __init__(self, slack: SlackGateway, store: Store, settings: Settings, handlers: dict[str, Handlers], now: datetime):
        self.slack, self.store, self.settings, self.handlers, self.now = slack, store, settings, handlers, now

    # Reactions and status replies are best effort: a failed one must not undo or hide the command's outcome.
    def _react(self, ts: str, emoji: str) -> None:
        try:
            self.slack.react(self.settings.self_dm_channel, ts, emoji)
        except SlackError as e:
            log.warning("reaction %s failed: %s", emoji, e)

    def _say(self, item: Item, text: str, *, failure: bool = False) -> None:
        try:
            send_dm(self.slack, self.store, self.settings, text, thread_ts=item.dm_ts)
        except Exception as e:  # noqa: BLE001 - best effort, including the store write inside send_dm
            log.warning("status reply failed: %s", e)
            if failure:
                request_alert(self.store, "a command's failure reply was not sent")  # I7

    def _permalink(self, item: Item) -> str:
        return self.store.get_cursor(f"permalink:{item.id}") or "(link unknown)"

    # -- one command ------------------------------------------------------------------------

    def _execute(self, item: Item, cmd: Command, reply_ts: str | None = None) -> str:
        """Run the command and return the status reply. Raises on failure."""
        guard = getattr(self.handlers.get(item.kind), "guard", None)
        if guard is not None:  # D8: a handler may refuse every command first (`Unsettled` leaves the item alone)
            guard(item, cmd.kind)
        if cmd.kind == "post" and item.status != "posted":
            self._refuse_earlier_post(item, reply_ts)
        if cmd.kind == "attach":  # R50: the one command for a posted item
            attach = getattr(self.handlers.get(item.kind), "attach", None)
            if item.kind != "insight" or attach is None:
                raise ValueError("attach works on insights only")
            return attach(item)
        # Status guards. `post` stays allowed on a skipped or failed item as an explicit override.
        if item.status == "posted":
            return f"Already posted · {self._permalink(item)}"
        if item.status == "skipped" and cmd.kind != "post":
            return "⏭️ Already skipped"
        handler = self.handlers.get(item.kind)
        if handler is None and cmd.kind != "skip":
            raise LookupError(f"no handler for {item.kind} items")
        if cmd.kind == "post":
            # Where it goes, looked up in his list again NOW, whatever the page showed or he typed earlier. A `Declined` is raised
            # before anything is marked or sent, so a refusal of ours leaves the item as it was (`_fail`).
            channel_id = post_target(self.store, self.settings, item, cmd.arg)
            # Said in the reply when the post goes anywhere but the leadership channel: "✅ Posted to #survive-ai · <link>". The
            # leadership channel's line is as it always was. Worked out here, before the send: nothing after it may fail.
            to = ""
            if channel_id is not None and channel_id != self.settings.channel_id:
                to = f" to {channel_label(self.store, self.settings, channel_id)}"
            # M2: set before the channel write and cleared once the outcome is recorded, so a process that dies
            # mid-post leaves it behind for the next run to find.
            self.store.set_cursor(f"inflight:{item.id}", self.now.isoformat())
            if reply_ts:  # R50: a deck is attached only if its DM upload came before this command
                self.store.set_cursor(f"post_cmd_ts:{item.id}", reply_ts)
            # A SlackError here is final: the write may already have happened (R16). N2: the handler posts the
            # version that was showing when Haseeb replied. The channel is named only when he named one: the handler records it
            # (with its send-started mark, before the send) and posts to the leadership channel without it.
            link = handler.post(item, reply_ts=reply_ts, **({} if channel_id is None else {"channel_id": channel_id}))
            self.store.set_cursor(f"permalink:{item.id}", link)
            self.store.mark(item.id, "posted", acted_at=self.now, attempts=0)
            return f"✅ Posted{to} · {link}"
        if cmd.kind == "skip":
            if item.kind == "insight":  # R38: only an insight skip says something about topics Haseeb wants
                self.store.add_pref(f"skipped: {item.title}")
            self.store.mark(item.id, "skipped", acted_at=self.now, attempts=0)
            return "⏭️ Skipped"
        instruction = _clean_instruction(cmd.arg) if cmd.kind == "edit" else SHORTER_INSTRUCTION
        revise_for = getattr(handler, "revise_for", None)
        if revise_for is not None:  # a handler that keeps its revision per command (a message): see MessageHandlers
            revise_for(item, instruction, reply_ts)
        else:
            handler.revise(item, instruction)
        if item.kind == "message":  # what he asks of his own messages steers messages only, never the other kinds
            if cmd.kind == "edit":
                self.store.add_pref(instruction, kind="message")
        else:
            self.store.add_pref(instruction if cmd.kind == "edit" else "shorter")  # only after a successful revise
        # revise was slow: re-read, never write back the status we started with
        fresh = self.store.get_item(item.id) or item
        self.store.mark(item.id, fresh.status, edited=True, attempts=0)
        return "✏️ Revised below"

    def _refuse_earlier_post(self, item: Item, reply_ts: str | None) -> None:
        """C1, R1 (D8), one rule for the DM and the page: a `post` given at or before a recorded may-have-posted
        failure of this item (`post_failed_at`) never runs: it was sent before Haseeb could know the outcome (a second
        reply during the first one's hang, a double click). Nor does any post while `inflight:` holds a time: a
        failed post whose failure time could not be recorded (CB2). Only a command given after the failure is an
        informed override."""
        inflight = self.store.get_cursor(f"inflight:{item.id}")
        if inflight and inflight != "notify":
            raise Unsettled(name_the_channel(EARLIER_POST, self.store, self.settings, item.id))
        failed_at = self.store.get_cursor(post_failed_key(item.id))
        if not failed_at:
            return
        try:
            informed = reply_ts is not None and Decimal(reply_ts) > Decimal(failed_at)
        except ArithmeticError:
            informed = False
        if not informed:
            raise Unsettled(name_the_channel(EARLIER_POST, self.store, self.settings, item.id))

    def _fail(self, item: Item, cmd: Command, exc: Exception) -> bool:
        """Record a failed command. For a `post` that may have gone out, its failure time is recorded FIRST (C1, CB2):
        whenever the handler's send had started (`send_started:`), whatever the exception, and for any failure but a
        refusal (LookupError, ValueError) from a handler that sets no marker. Returns whether the caller may clear
        `inflight:`: False when that time could not be recorded (the marker then stays, so `recover_interrupted`
        records it on the next run), and False for an `Unsettled` refusal (M1): that command started nothing, so
        any `inflight:` belongs to an earlier post whose outcome is still unknown."""
        log.warning("command %s on %s failed: %s", cmd.kind, item.id, events.error_kind(exc) if isinstance(exc, Declined) else exc)
        if isinstance(exc, Unsettled):
            return False  # refused before any send; the item, its status and an earlier inflight: stay as they are
        if isinstance(exc, SlackPaused):
            return self._not_sent(item, cmd)
        recorded = True
        if cmd.kind == "post":
            try:
                started = self.store.get_cursor(send_key(item.id))
                if started or not isinstance(exc, (LookupError, ValueError)):
                    mark_post_failed(self.store, item.id, self.now)
                if started:
                    clear_send_started(self.store, item.id)
            except Exception:  # noqa: BLE001 - bookkeeping must not abort the tick
                log.exception("could not record the post failure time of %s", item.id)
                recorded = False
        try:
            fresh = self.store.get_item(item.id) or item
            attempts = fresh.attempts + 1
            # post is never retried automatically (R16): its first failure is final. Others fail after 3 attempts.
            final = cmd.kind == "post" or attempts >= MAX_ATTEMPTS
            status = "failed" if final and fresh.status != "posted" else fresh.status
            self.store.mark(item.id, status, attempts=attempts)
        except Exception:  # noqa: BLE001 - bookkeeping must not abort the tick
            log.exception("could not record the failure of %s on %s", cmd.kind, item.id)
        return recorded

    def _not_sent(self, item: Item, cmd: Command) -> bool:
        """The Slack gateway refused the write before making any call (the dashboard's Pause, `SlackPaused`), so
        nothing was sent, not even a part of it. The command left no trace: what the handler had marked as "send
        started" is taken back, no failure time is recorded (a `post` after Resume is a plain first post, not an
        override), no attempt is counted and the status stays. Returns whether the caller may clear `inflight:`
        (False when the marker could not be taken back: `recover_interrupted` then treats the post as unknown)."""
        if cmd.kind == "post":
            try:
                clear_send_started(self.store, item.id)
                if not self.store.get_cursor(post_failed_key(item.id)):
                    # the target the handler recorded went with the attempt. (After an earlier attempt that may have posted, the
                    # record is that attempt's, which the retry is pinned to and so did not change: it stays.)
                    self.store.delete_cursor(channels.POST_CHANNEL_PREFIX + item.id)
            except Exception:  # noqa: BLE001 - bookkeeping must not abort the tick
                log.exception("could not take back the send marker of %s", item.id)
                return False
        return True

    def _queue_paused_notice(self, item: Item) -> None:
        try:
            self.store.set_cursor(f"{PAUSED_NOTICE_PREFIX}{item.id}", self.now.isoformat())
        except Exception:  # noqa: BLE001 - bookkeeping must not abort the tick
            log.exception("could not queue the paused-command notice of %s", item.id)

    def handle(self, item: Item, reply: Msg, cmd: Command) -> None:
        self._react(reply.ts, "eyes")
        try:
            status = self._execute(item, cmd, reply.ts)
        except Exception as e:  # noqa: BLE001 - a failed command must never take the tick down
            if self._fail(item, cmd, e):  # CB2, M1: else `inflight:` stays for recover_interrupted
                self._clear_inflight(item, cmd)
            if isinstance(e, SlackPaused):
                # No reaction and no reply now (every DM write is refused), and `handled:` has moved, so the command
                # will not run again: one notice for after the Resume tells him to send it again.
                self._queue_paused_notice(item)
                return
            self._react(reply.ts, "warning")
            if cmd.kind == "post" and not isinstance(e, (LookupError, ValueError)):
                # M3: anything but a refusal before the send (unknown thread, invalid draft) may have posted
                text = name_the_channel(MAY_HAVE_POSTED, self.store, self.settings, item.id).format(reason=_one_line(e))
            else:
                text = f"⚠️ {_one_line(e)}"
            self._say(item, text, failure=True)
            return
        self._clear_inflight(item, cmd)
        self._react(reply.ts, "white_check_mark")
        self._say(item, status)

    # -- one dashboard command ----------------------------------------------------------------

    def handle_page(self, item: Item, cmd: Command, created_ts: str, cmd_id: int) -> None:
        """`handle` for a dashboard command (a `page_commands` row): the same `_execute`, `_fail` and
        `_clear_inflight`, with the row's created time as the reply ts (R50's deck cutoff and N2's shown version).
        The reactions go on the item's own DM message, and the status line, marked as done from the dashboard, goes
        into its DM thread through send_dm (R23, R43). The row ends `done` or `failed` with the status text."""
        self._react_item(item, "eyes")
        try:
            status = self._execute(item, cmd, created_ts)
        except Exception as e:  # noqa: BLE001 - a failed command must never take the tick down
            if self._fail(item, cmd, e):  # CB2, M1: else `inflight:` stays for recover_interrupted
                self._clear_inflight(item, cmd)
            if isinstance(e, SlackPaused):  # nothing was sent; no reaction and no DM line while every DM write is refused
                self.finish_page(item, cmd_id, False, PAUSED_NOTHING_SENT, tell=False)
                return
            if cmd.kind == "post" and not isinstance(e, (LookupError, ValueError)):
                text = name_the_channel(MAY_HAVE_POSTED, self.store, self.settings, item.id).format(reason=_one_line(e))  # M3, R16
            else:
                text = f"⚠️ {_one_line(e)}"
            self.finish_page(item, cmd_id, False, text)
            return
        self._clear_inflight(item, cmd)
        self.finish_page(item, cmd_id, True, status)

    def _react_item(self, item: Item, emoji: str) -> None:
        if item.dm_ts:
            self._react(item.dm_ts, emoji)

    def finish_page(self, item: Item, cmd_id: int, ok: bool, status: str, *, tell: bool = True) -> None:
        """Record a dashboard command's outcome first, then react on the item's DM message and say it in its thread
        (`tell` False: record only). A row that cannot be recorded stays `running`; it is never run again (only
        `queued` rows run)."""
        for attempt in (1, 2):  # M4: one retry; a row left `running` is failed by the stale-row recovery
            try:
                self.store.set_page_command(cmd_id, "done" if ok else "failed", status)
                break
            except Exception:  # noqa: BLE001 - bookkeeping must not abort the tick
                log.exception("could not record the outcome of page command %s (attempt %d)", cmd_id, attempt)
        if not tell:
            return
        self._react_item(item, "white_check_mark" if ok else "warning")
        if item.dm_ts:
            self._say(item, f"{status}\n{DASHBOARD_NOTE}", failure=not ok)

    def _clear_inflight(self, item: Item, cmd: Command) -> None:
        if cmd.kind == "post":
            try:
                self.store.set_cursor(f"inflight:{item.id}", "")
            except Exception:  # noqa: BLE001
                log.exception("could not clear the in-flight marker of %s", item.id)

    # -- one thread -------------------------------------------------------------------------

    def thread(self, item: Item, parent_ts: str) -> int | None:
        """Commands handled in the thread, or None when it could not be read (so `seen` must not advance)."""
        try:
            _, replies = self.slack.read_thread(self.settings.self_dm_id, parent_ts)
        except SlackError as e:  # R12: skip this thread for this tick
            log.warning("thread %s unreadable, skipped: %s", parent_ts, e)
            return None
        key = f"handled:{parent_ts}"
        handled_n = 0
        for reply in sorted(replies, key=lambda m: _ts(m.ts)):
            done = self.store.get_cursor(key)
            if done is not None and _ts(reply.ts) <= _ts(done):
                continue
            self.store.set_cursor(key, reply.ts)  # before any slow work: the idempotency rule
            if reply.user_id != self.settings.haseeb_id or reply.is_bot:
                continue
            if self.store.get_cursor(f"own:{reply.ts}") is not None:
                continue  # the agent's own message, sent as Haseeb (R23)
            cmd = parse_command(reply.text)
            if cmd is None:
                continue
            fresh = self.store.get_item(item.id)
            if fresh is None:
                continue
            self.handle(fresh, reply, cmd)
            handled_n += 1
        return handled_n


def recover_interrupted(slack: SlackGateway, store: Store, settings: Settings, now: datetime | None = None) -> None:
    """M2: an `inflight:{id}` marker still set means a `post` died between the channel write and its record. The
    item is marked failed (the post may or may not have landed, R16) and Haseeb is told; a notice that cannot be
    sent is retried on the next run (the marker then holds "notify")."""
    for key, value in store.cursors("inflight:").items():
        if not value:
            continue
        iid = key.split(":", 1)[1]
        item = store.get_item(iid)
        if item is None or item.status == "posted":
            store.set_cursor(key, "")
            continue
        if value != "notify":
            store.mark(iid, "failed", attempts=item.attempts + 1)
            mark_post_failed(store, iid, now)  # C1
            store.record_run("process_dm", False, f"{iid}: a post was interrupted; marked failed")
        try:
            send_dm(slack, store, settings, name_the_channel(INTERRUPTED, store, settings, iid), thread_ts=item.dm_ts)
        except SlackError as e:
            log.warning("could not send the interrupted-post notice for %s: %s", iid, e)
            request_alert(store, "an interrupted-post notice was not sent")
            store.set_cursor(key, "" if dm_send_failed(store, f"interrupted:{iid}", e) else "notify")
            continue
        store.set_cursor(key, "")


def _say_floor(store: Store, now: datetime) -> Decimal:
    """The ts after which a DM message may be a `say:` command: the cursor `say_oldest`, or on the very first look a few
    minutes back (never into old chat)."""
    raw = store.get_cursor("say_oldest")
    try:
        if raw:
            return Decimal(raw)
    except ArithmeticError:
        pass
    floor = Decimal(f"{now.timestamp() - SAY_FIRST_LOOKBACK_S:.6f}")
    store.set_cursor("say_oldest", f"{floor}")
    return floor


def _say_due(store: Store, now: datetime, have_open_items: bool) -> bool:
    """With an open item the DM is read anyway, so `say:` is looked for in the same page. With none, a read just for
    it is a Claude run, so at most every SAY_IDLE_EVERY_S."""
    if have_open_items:
        return True
    try:
        last = float(store.get_cursor("say_scanned_at") or "")
    except ValueError:
        return True
    elapsed = now.timestamp() - last
    return elapsed < 0 or elapsed >= SAY_IDLE_EVERY_S


SAY_PER_RUN = 2  # drafting is slow (a model, then a DM): the rest waits for the next run


def held_at(store: Store) -> datetime | None:
    """When a tick last saw the huddle hold, or None (never, or a value that is not a time)."""
    try:
        at = datetime.fromisoformat(store.get_cursor(HELD_KEY) or "")
    except ValueError:
        return None
    return at if at.tzinfo is not None and at.utcoffset() is not None else None


def _tell(runner: "_Runner", text: str) -> None:
    try:
        send_dm(runner.slack, runner.store, runner.settings, text)
    except Exception as e:  # noqa: BLE001 - a note must never fail the run
        log.warning("could not tell Haseeb about a say: message: %s", type(e).__name__)


SayDraft = Callable[[str, str, datetime], object]  # (his text, the DM message's ts, now): MessageHandlers.say


def _draft_say(runner: "_Runner", say: SayDraft, ts: str, raw: str) -> bool:
    """Draft one `say:` message. True when it is settled: drafted, or refused with a word to Haseeb, or given up on.
    False when it must be tried again: its draft could not be sent. Nothing here ever raises."""
    store, key = runner.store, f"say:{ts}"
    try:
        say(raw, ts, runner.now)
    except ValueError as e:  # a refusal (nothing after the colon, too long): said once, then settled
        _tell(runner, f"⚠️ {e}")
        store.set_cursor(key, "1")
        return True
    except SlackError as e:  # the draft's DM did not go out and no item was made: tried again, up to the usual cap
        log.warning("say: draft for %s could not be sent: %s", ts, e)
        if dm_send_failed(store, key, e, runner.now):
            store.set_cursor(key, "1")
            store.record_run("compose", False, f"say:{ts}: the draft could not be sent, gave up")
            request_alert(store, "a say: draft could not be sent")  # I7
            return True
        return False
    except Exception as e:  # noqa: BLE001 - a bug in one message must not wedge the DM
        fails = int(store.get_cursor(f"say_fail:{ts}") or 0) + 1
        store.set_cursor(f"say_fail:{ts}", str(fails))
        store.record_run("compose", False, f"say:{ts}: {events.error_kind(e)}")
        log.warning("say: draft for %s failed (%d/%d): %s", ts, fails, MAX_ATTEMPTS, events.error_kind(e))
        if fails < MAX_ATTEMPTS:
            return False
        _tell(runner, "⚠️ I couldn't polish that message. Post it yourself, or send it again with say:")
        store.set_cursor(key, "1")
        return True
    store.set_cursor(key, "1")
    return True


def _too_old(runner: "_Runner", ts: str) -> bool:
    """A `say:` that has waited more than SAY_MAX_AGE_S: the Mac was off or paused over a weekend, say. Counted from the
    later of the message and the last huddle hold, since a hold can keep it waiting for 100 minutes."""
    try:
        since = float(ts)
    except ValueError:
        return False
    if (held := held_at(runner.store)) is not None:
        since = max(since, held.timestamp())
    return runner.now.timestamp() - since > SAY_MAX_AGE_S


def _scan_say(runner: "_Runner", say: SayDraft, page: list[Msg], floor: Decimal) -> int:
    """Draft the `say:` messages of the DM page that are new (above the floor, not the agent's own, not done before),
    oldest first, once each. The cursor `say_oldest` moves past everything settled, plain chat included, and stops at a
    draft that could not be sent, so that one is found again. A `say:` that is too old is answered with one line and
    settled, without a draft (it takes no part in the SAY_PER_RUN budget). Returns how many were settled."""
    handled, drafted, advance, blocked = 0, 0, None, False
    for m in sorted(page, key=lambda m: _ts(m.ts)):
        try:
            if _ts(m.ts) <= floor:
                continue
        except ArithmeticError:
            continue
        if drafted >= SAY_PER_RUN:
            break  # the rest, and the cursor, wait for the next run
        key = f"say:{m.ts}"
        if runner.store.get_cursor(key) is None and runner.store.get_cursor(f"own:{m.ts}") is None:
            raw = say_text(m, runner.settings)
            if raw is not None:
                if _too_old(runner, m.ts):
                    _tell(runner, SAY_TOO_OLD)
                    runner.store.set_cursor(key, "1")
                    handled += 1
                elif _draft_say(runner, say, m.ts, raw):
                    drafted += 1
                    handled += 1
                else:
                    blocked = True
        if not blocked:
            advance = m.ts
    if advance is not None:
        runner.store.set_cursor("say_oldest", advance)
    return handled


def send_paused_notices(slack: SlackGateway, store: Store, settings: Settings) -> None:
    """A DM command that a pause landing mid-run refused is not run again (its reply is already marked handled), and the
    pause left no reaction or reply. For each item with a queued notice, say so in the item's DM thread, once however many
    commands were refused; a send that fails is retried on the next run, capped like every DM send (the same shape as
    `recover_interrupted`'s notice). Runs at the start of process_dm, so the first one after Resume sends it."""
    for key, value in store.cursors(PAUSED_NOTICE_PREFIX).items():
        if not value:
            continue
        iid = key[len(PAUSED_NOTICE_PREFIX):]
        item = store.get_item(iid)
        if item is not None and item.dm_ts:
            try:
                send_dm(slack, store, settings, PAUSED_COMMAND_NOTICE, thread_ts=item.dm_ts)
            except SlackError as e:
                log.warning("could not send the paused-command notice for %s: %s", iid, e)
                request_alert(store, "a paused-command notice was not sent")
                if not dm_send_failed(store, f"pausednote:{iid}", e):
                    continue  # kept: the next run tries again
        store.delete_cursor(key)


def process_dm(
    slack: SlackGateway, store: Store, settings: Settings, handlers: dict[str, Handlers], now: datetime
) -> int:
    """Find new command replies in the item threads of Haseeb's self-DM and run each exactly once. Also, when a
    `message` handler is wired, find new `say: <rough message>` top-level messages and draft each into a message item.

    Open items are the ones with a DM message that are delivered, drafted or failed (a failed item still takes
    `post` as an override, which AUTOPOST_FAILED and the failed-post warning invite) from the last 7 days. With none
    (and no `say:` look due), the DM is not read at all. The read starts 1 s before the oldest open item's message, or
    at the `say_oldest` cursor if that is older, and takes at most 20 messages, newest first, so on a busy DM the
    oldest open items can fall off the page (logged).

    Returns the number of commands handled (failed ones and `say:` drafts included).
    """
    recover_interrupted(slack, store, settings, now)
    send_paused_notices(slack, store, settings)  # a command the pause refused mid-run: told once, after the Resume
    # I5 (R37): read the DM only when some item can still take a command, and only from the oldest one's message.
    today = now.astimezone(settings.tz).date()  # R4
    open_items = store.open_dm_items(today - WINDOW)
    # R50: a posted insight whose deck finished after the post still takes `attach`
    open_items += [it for it in store.open_dm_items(today - WINDOW, statuses=("posted",))
                   if it.kind == "insight" and store.get_cursor(f"attach:{it.id}") == "late"]
    open_items.sort(key=lambda it: Decimal(it.dm_ts))
    composer = handlers.get("message")
    say = getattr(composer, "say", None)  # `say:` is on only when the composer is wired
    # R33's predicate: while the huddle poll must not be blinded, no `say:` is drafted (a polish is minutes of model
    # calls under the tick lock) and `say_oldest` stays where it is, so the first one waits its turn; commands run as ever
    held = say is not None and bool(getattr(composer, "holding", lambda _now: False)(now))
    look = say is not None and not held and _say_due(store, now, bool(open_items))
    if not open_items and not look:
        return 0
    floor = _say_floor(store, now) if look else None
    starts = ([Decimal(open_items[0].dm_ts) - 1] if open_items else []) + ([floor] if floor is not None else [])
    oldest = f"{min(starts)}"
    store.set_cursor("dm_oldest", oldest)
    try:
        with events.step(store, "process_dm", "read_dm"):
            page = slack.read_channel(settings.self_dm_id, oldest, DM_PAGE)
    except SlackError as e:
        log.warning("cannot read the DM: %s", e)
        return 0
    if look:
        store.set_cursor("say_scanned_at", f"{now.timestamp():.3f}")
        if len(page) >= DM_PAGE and _ts(min(m.ts for m in page)) > floor:  # newer than the floor: older ones are unread
            log.warning("the DM page is full; a say: message older than it may not have been seen")
    if open_items and len(page) >= DM_PAGE and Decimal(min(m.ts for m in page)) > Decimal(open_items[0].dm_ts):
        log.warning("the DM page is full; %d open item(s) older than it are not checked for commands",
                    sum(1 for it in open_items if Decimal(it.dm_ts) < Decimal(min(m.ts for m in page))))
    runner = _Runner(slack, store, settings, handlers, now)
    total = 0
    for parent in page:
        if not parent.latest_reply:
            continue
        item = store.item_by_dm_ts(parent.ts)
        if item is None:
            continue
        seen_key = f"seen:{parent.ts}"
        seen = store.get_cursor(seen_key)
        # R22: re-read only when latest_reply moved forward (whole-second precision); the per-reply handled cursor
        # keeps each reply exactly-once.
        if seen is not None and int(float(parent.latest_reply)) <= int(float(seen)):
            continue
        with events.step(store, "process_dm", "command"):
            n = runner.thread(item, parent.ts)
        if n is None:
            continue
        total += n
        store.set_cursor(seen_key, str(int(float(parent.latest_reply))))
    # `say:` drafts come last: a polish is a slow model call, and a `post` in a thread (the Next Todo's, at the end of
    # the huddle) must never wait behind one in the same run
    if look:
        total += _scan_say(runner, say, page, floor)
    return total


# --------------------------------------------------------------------------
# Dashboard commands: the shared bits (page_commands.py runs them; jobs/todo.py honours a `cancel`)
# --------------------------------------------------------------------------

def created_ts(created_at: datetime) -> str:
    """A dashboard command's created time as a Slack-style ts, used where a DM reply's ts would be (R50, N2). Both
    are on the Mac clock. A naive time is taken as local time, like the dashboard writes it."""
    return f"{created_at.timestamp():.6f}"


def finish_page_command(slack: SlackGateway, store: Store, settings: Settings, item: Item, cmd_id: int, ok: bool,
                        status: str, now: datetime) -> None:
    """Record a dashboard command's outcome and tell Haseeb on the item's DM message (see _Runner.finish_page)."""
    _Runner(slack, store, settings, {}, now).finish_page(item, cmd_id, ok, status)
