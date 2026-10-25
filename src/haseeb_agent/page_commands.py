"""Dashboard commands, run by the tick (the only Slack writer).

A click on the dashboard writes a `page_commands` row; the dashboard itself never calls Slack. Each tick runs the
queued rows here, oldest first, through the same `_Runner` code as Haseeb's DM replies (commands.py), so a page
`post` and a DM `post` settle on the item's status: whichever runs first posts, the other answers "Already posted".

Per row:
  - it goes `running` before any work, then `done` or `failed`, with the status text as its result. Only `queued`
    rows ever run, so a row is never run twice. A row left `running` by a dead tick is failed after 2 minutes; a
    `post` one with the MAY_HAVE_POSTED wording (R16, like `recover_interrupted` for a DM post);
  - the row's created time stands in for the reply ts: R50 (a deck attaches only if its DM upload came first) and
    N2 (post the version Haseeb saw);
  - N2 again: the page sends the sha256 of the draft it showed. If the item's body changed since, the row fails
    "draft changed, reload" with no Slack call. `cancel` and `attach` skip this (nothing to protect / posted item);
  - a `post` behaves exactly like a DM `post` (D7): the format check only. The R25/R27/R28 guard is for the
    unattended auto-post, and a click is attended;
  - a `post` may carry a channel id (`arg`: the channel Haseeb chose for an idea or a message; none is the leadership channel). The
    runner looks it up in his list again when the row runs (`commands.post_target`): an id that is not in it (a stale page, a channel
    he left, a DM or user id), a channel on a feedback draft or the Next Todo, or another channel than the one a possible post is
    pinned to is refused with a short reason, with no Slack call, and the draft is left exactly as it was;
  - the 👀 / ✅ / ⚠️ reactions go on the item's own DM message, and the status line plus DASHBOARD_NOTE goes into
    its DM thread through send_dm, so the own-ts is recorded (R23, R43);
  - `cancel` works on todo items only and stops a pending auto-post in this tick (jobs/todo.py also checks for
    cancel rows right before its send);
  - `compose` (the dashboard's composer) is the one verb with no item yet: its row carries the id the new item will
    get and Haseeb's rough text, and the tick polishes it into a `message` item and sends the draft to his DM
    (jobs/message.py). It ends `done` with a short status, or `failed` with the reason. While the huddle poll must not
    be blinded (R33's predicate) it is not claimed at all and waits, and its 15 minutes count from the end of that hold;
  - the pause (the dashboard's) expires the rows queued before it (`expire_for_pause`, run by the paused tick, no
    Slack call): a click made before the kill switch does not wake up and run on the first tick after Resume.

Logs and events carry ids, verbs and kinds only, never text (M6).
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from . import events
from .commands import EARLIER_POST as _EARLIER
from .commands import (DASHBOARD_NOTE, MAY_HAVE_POSTED, Command, Declined, Handlers, _maybe_landed, _Runner, created_ts,
                       held_at, mark_post_failed, name_the_channel, post_failed_key, post_target, recover_interrupted)
from .config import Settings
from .jobs.todo import body_shown_at, cancel_from_page
from .models import Item, PageCommand
from .slack_gateway import SlackError, SlackGateway
from .store import Store

log = logging.getLogger(__name__)

__all__ = ["DASHBOARD_NOTE", "body_hash", "expire_for_pause", "page_due", "process_page"]

JOB = "process_page"
BATCH = 10
STALE_RUNNING = timedelta(minutes=2)
EXPIRE_QUEUED = timedelta(minutes=15)  # M5: a click this old is never run (the agent was not running to take it)
EXPIRED = "Expired — this click waited too long (the agent wasn't running). Click again if you still want it."
EXPIRED_BY_PAUSE = "expired: queued before the pause"
DRAFT_CHANGED = "draft changed, reload"
NOT_FOUND = "item not found"
NO_INSTRUCTION = "⚠️ edit needs an instruction"
NOT_TODO = "⚠️ cancel works on Next Todo items only"
INTERRUPTED_PAGE = "⚠️ Interrupted — reload and try again"
COMPOSED = "✅ Polished · the draft is below and in your DM"
COMPOSE_OFF = "⚠️ The composer is not available"
COMPOSE_NOT_SENT = "⚠️ The draft could not be sent to your DM. Try again"
COMPOSE_MAYBE_SENT = "⚠️ The draft may have reached your DM. Check there before you try again"
EARLIER_POST = f"⚠️ {_EARLIER}"  # the page answers this one without a Slack call (the DM path: `_execute`)
POSTED_UNRECORDED = "Posted, but the result could not be recorded — check the channel"
HUDDLE_ROWS, HUDDLE_BUDGET_S = 3, 60  # M3: while the huddle is open, the todo job must get its tick
HASHED = ("post", "edit", "shorter", "skip")  # the verbs that act on the draft Haseeb saw


def _sha(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def body_hash(item: Item) -> str:
    """The sha256 hex of the item's body: the hash the page sends with a command (N2)."""
    return _sha(item.body)


def page_due(store: Store, *, hold_compose: bool = False) -> bool:
    """True when there is page work: a queued row, or a running one to recover. A DB query only, no Slack. With
    `hold_compose` (the huddle hold) a queued composer row is not work: it waits, and the tick does not wake for it."""
    rows = store.page_commands_in(("queued", "running"), limit=200)
    return any(not (hold_compose and c.verb == "compose" and c.state == "queued") for c in rows)


def _aware(dt: datetime, now: datetime) -> datetime:
    return dt if dt.tzinfo is not None and dt.utcoffset() is not None else dt.astimezone(now.tzinfo)


def expire_for_pause(store: Store, since: str, now: datetime) -> int:
    """The agent is paused (`since`: the ISO time of the pause, the cursor's text): end every queued row that was
    created before it as failed, with EXPIRED_BY_PAUSE, and return how many. The database only, no Slack call and no
    reaction or DM line. The kill switch must also kill a click that was armed before it, which would otherwise run as
    the first thing of the first tick after Resume with no new confirmation. A row created after the pause time (a click
    that was in flight when it landed) is left queued; a `since` that cannot be read as a time cannot tell which came
    first, so then every queued row expires."""
    try:
        paused_at = datetime.fromisoformat(since)
        if paused_at.tzinfo is None or paused_at.utcoffset() is None:
            paused_at = paused_at.replace(tzinfo=now.tzinfo)
    except (TypeError, ValueError):
        paused_at = None
    due = [c.id for c in store.page_commands_in(("queued",), limit=1000)
           if paused_at is None or _aware(c.created_at, now) < paused_at]  # the pause time, as a time, not as text
    if not due:
        return 0
    # One UPDATE, `WHERE state='queued'`: a row that a tick claimed meanwhile is left to it, and none is left half-way.
    expired = store.fail_queued_page_commands(due, EXPIRED_BY_PAUSE)
    if expired:
        log.info("%d page command(s) expired: queued before the pause", expired)
    return expired


def _recover_running(store: Store, settings: Settings, now: datetime) -> None:
    """A row still `running` here was left by a tick that died (the tick lock allows one at a time) or whose
    outcome could not be recorded. It is never run again: after 2 minutes it is failed. A `post` gets the
    may-have-posted wording (R16) and records the failure time (C1), unless the post's permalink was recorded, which
    means it went out (M4). The item itself is handled by `recover_interrupted` through the `inflight:` marker."""
    for c in store.page_commands_in(("running",)):
        if now - _aware(c.created_at, now) < STALE_RUNNING:
            continue
        if c.verb != "post":
            text = INTERRUPTED_PAGE
        elif store.get_cursor(f"permalink:{c.item_id}"):
            text = name_the_channel(POSTED_UNRECORDED, store, settings, c.item_id)
        else:
            text = name_the_channel(MAY_HAVE_POSTED, store, settings, c.item_id).format(reason="the run was interrupted")
            mark_post_failed(store, c.item_id, now)
        store.set_page_command(c.id, "failed", text)
        log.warning("page command %s (%s on %s) was interrupted; marked failed", c.id, c.verb, c.item_id)


def _shown_changed(store: Store, item: Item, c: PageCommand) -> bool:
    """N2: the page's hash must match the item's body and, for a todo or a message, the version its `post` would send
    for this click (`body_shown_at`; none that is older than the click counts as changed too)."""
    if body_hash(item) != c.shown_hash:
        return True
    if item.kind not in ("todo", "message"):  # the kinds whose drafts are versioned (N2)
        return False
    try:
        shown = body_shown_at(store, item.id, created_ts(c.created_at))
    except LookupError:
        return True
    return shown is not None and _sha(shown) != c.shown_hash


def _refusal(store: Store, settings: Settings, item: Item | None, c: PageCommand) -> str | None:
    """Why this row must not run at all (answered with no Slack call), or None."""
    if item is None:
        return NOT_FOUND
    if c.verb in HASHED and item.status != "posted" and _shown_changed(store, item, c):
        return DRAFT_CHANGED  # N2: never act on a draft Haseeb did not see. (On a posted item `_execute` answers.)
    if c.verb == "post" and item.status != "posted":
        earlier = name_the_channel(EARLIER_POST, store, settings, item.id)  # "check #survive-ai" when a channel is recorded
        inflight = store.get_cursor(f"inflight:{item.id}")
        if inflight and inflight != "notify":
            return earlier  # CB2: a failed post whose failure time could not be recorded
        failed_at = store.get_cursor(post_failed_key(item.id))
        try:
            if failed_at and Decimal(created_ts(c.created_at)) <= Decimal(failed_at):
                return earlier  # C1: a click from before a may-have-posted failure never posts again
        except ArithmeticError:
            return earlier
        try:
            post_target(store, settings, item, c.arg)  # the channel looked up again, as the DM path does in `_execute`
        except Declined as e:
            return f"⚠️ {e}"
    if c.verb == "cancel" and item.kind != "todo":
        return NOT_TODO
    if c.verb == "edit" and not (c.arg or "").strip():
        return NO_INSTRUCTION
    return None


def _run_one(runner: _Runner, store: Store, c: PageCommand) -> None:
    if not store.claim_page_command(c.id):  # first, compare-and-set (M2): whatever happens next, it never reruns
        log.info("page command %s is no longer queued; skipped", c.id)
        return
    waiting_since = _aware(c.created_at, runner.now)
    if c.verb == "compose" and (held := held_at(store)) is not None:
        waiting_since = max(waiting_since, held)  # a hold of up to 100 minutes is not "the agent wasn't running"
    if runner.now - waiting_since > EXPIRE_QUEUED:  # M5: answered with no Slack call
        store.set_page_command(c.id, "failed", EXPIRED)
        log.info("page command %s (%s on %s) expired unrun", c.id, c.verb, c.item_id)
        return
    if c.verb == "compose":  # no item yet: the handler makes it
        _run_compose(runner, store, c)
        return
    item = store.get_item(c.item_id)  # fresh, right before acting
    why = _refusal(store, runner.settings, item, c)
    if why is not None:
        store.set_page_command(c.id, "failed", why)
        log.info("page command %s (%s on %s) refused: %s", c.id, c.verb, c.item_id, why)
        return
    if c.verb == "cancel":
        runner._react_item(item, "eyes")
        ok, status = cancel_from_page(store, item, created_ts(c.created_at), c.id)
        runner.finish_page(item, c.id, ok, status)
        return
    runner.handle_page(item, Command(c.verb, c.arg), created_ts(c.created_at), c.id)  # type: ignore[arg-type]


def _run_compose(runner: _Runner, store: Store, c: PageCommand) -> None:
    """The composer's row: polish the rough text (`c.arg`) into the new message item `c.item_id` and send its draft to
    the DM. The row ends `done` or `failed`; a refusal or a failure leaves no item, and Haseeb tries again."""
    handler = runner.handlers.get("message")
    compose = getattr(handler, "compose", None)
    if compose is None:
        store.set_page_command(c.id, "failed", COMPOSE_OFF)
        return
    try:
        compose(c.arg or "", c.item_id, runner.now)
    except ValueError as e:  # a fixed phrase: nothing to polish, too long, already drafted
        store.set_page_command(c.id, "failed", f"⚠️ {e}")
        log.info("page command %s (compose) refused", c.id)
        return
    except SlackError as e:  # a timeout or a mismatch: the card may be there (and the same words will adopt it)
        store.set_page_command(c.id, "failed", COMPOSE_MAYBE_SENT if _maybe_landed(e) else COMPOSE_NOT_SENT)
        log.warning("page command %s (compose): the draft was not sent: %s", c.id, events.error_kind(e))
        return
    store.set_page_command(c.id, "done", COMPOSED)


def process_page(slack: SlackGateway, store: Store, settings: Settings, handlers: dict[str, Handlers],
                 now: datetime, *, limit: int = BATCH, budget_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> int:
    """Run the queued dashboard commands, oldest first, each exactly once. Returns how many were started (refused
    and failed ones included). With nothing queued or running it reads only the DB: no Slack call, no event.

    M3: at most `limit` rows, and no new row once `budget_s` seconds have passed since the start (the tick passes
    3 rows and 60 s while the huddle is open). The rest wait for the next tick."""
    began = clock()
    # R33's predicate (the message handler's `holding`): while the huddle poll must not be blinded a composer row stays
    # queued, not claimed, and the rows behind it still run. A polish is minutes of model calls under the tick lock.
    held = bool(getattr(handlers.get("message"), "holding", lambda _now: False)(now))
    if not page_due(store, hold_compose=held):
        return 0
    _recover_running(store, settings, now)
    recover_interrupted(slack, store, settings, now)  # as process_dm does: a post that died mid-send is told first
    queued = store.next_page_commands(limit, skip_verbs=("compose",)) if held else store.next_page_commands(limit)
    runner = _Runner(slack, store, settings, handlers, now)
    n = 0
    for c in queued:
        if budget_s is not None and n and clock() - began >= budget_s:
            log.info("page commands: the huddle budget is used; %d row(s) wait for the next tick", len(queued) - n)
            break
        try:
            with events.step(store, JOB, "command"):
                _run_one(runner, store, c)
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the others
            log.warning("page command %s (%s) failed: %s", c.id, c.verb, events.error_kind(e))
            try:
                current = store.page_command(c.id)
                if current is not None and current.state in ("queued", "running"):
                    text = (name_the_channel(MAY_HAVE_POSTED, store, settings, c.item_id).format(reason=events.error_kind(e))
                            if c.verb == "post" else f"⚠️ {events.error_kind(e)}")
                    if c.verb == "post":
                        mark_post_failed(store, c.item_id, now)  # C1
                    store.set_page_command(c.id, "failed", text)
            except Exception:  # noqa: BLE001
                log.exception("could not record the failure of page command %s", c.id)
        n += 1
    return n
