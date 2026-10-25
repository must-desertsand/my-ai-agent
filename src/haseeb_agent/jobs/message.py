"""Job 4 (2026-10-20): a message Haseeb writes himself, polished by the agent, posted by him.

Two doors lead here. The dashboard's composer queues a page command (verb `compose`, the rough text as its argument)
and `say: <rough message>` as a top-level message in his DM is found by `process_dm`. Both end the same way: the
tick polishes the text, sends the draft to his DM as an item of kind `message` (a new top-level DM message, like a
feedback draft; `post`, `edit: ...`, `shorter` and `skip` work in its thread and on the page), and waits.

`post` is the one channel write: a NEW top-level message in the channel he chose (the program channel unless he named another:
`post #name` in the DM, or the dashboard's choice), never a reply in a thread, with no footer and without the note. It runs
through the same `_Runner` code as every other kind, so the double-post rules (D8, R16: never retried, `inflight:`,
`send_started:`, `post_failed_at`) apply unchanged, and nothing auto-posts.
N2 applies too: every card that shows a draft is recorded as a version with the ts of its DM message
(`jobs.todo.add_version`), and `post` sends the version that was showing when he replied, never one that landed after
his reply (an `edit:` and a `post` in one run send the draft he saw, not the revision).

The model is the feedback writer's chain (config `models.feedback_writer`, through the LLM door) with the prompt in
`message_prompt.py`. When the whole chain fails, the draft is Haseeb's own text (cleaned when it is rendered) with the
note "not polished (model unavailable)". A reply whose links are not exactly his (a link or web address added, changed
or dropped to a look-alike: `message_prompt.links_in`) is not repaired and no other model is asked: the draft is his
own text with the note "not polished (a link changed)". An `edit` or `shorter` has no such fallback: it fails and
leaves the draft as it was.

An item keeps what it needs in cursors, like a feedback item: `message_raw:<id>` (his words, which also decide which
@channel / @here may be real) and `message_note:<id>` (what changed), both written once its card is in his DM. Edit
instructions are saved as preferences for this kind only (`store.add_pref(..., kind="message")`), and only this kind's
preferences reach its prompt.

A card that may be on his screen is never polished again (review C1). The first polish is kept before the first send
(`message_draft:<digest of his text>`, an hour), so a retry (a `say:` tried on a later run, a click on Polish again
after "may have reached your DM") sends exactly that text and R43 adopts the landed card only if it is that same text.
A revision is kept the same way, per item and command (`message_revision:<id>:<command ts>`).

Slack text is untrusted and private: nothing here logs it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .. import channels, events
from ..commands import MARKER_TTL_S, _maybe_landed, clear_send_started, mark_send_started, send_dm
from ..config import Settings
from ..litellm import LLM, LLMError
from ..message_prompt import (MAX_PREFS, MAX_RAW_CHARS, NOT_POLISHED, NOT_POLISHED_BAD, NOT_POLISHED_LINK, SYSTEM,
                              LinkChanged, normalize_raw, parse_reply, slack_to_text, user_prompt)
from ..models import Item
from ..notify import notify as show_banner
from ..render import mass_mentions, message_pings, render_message_dm, render_message_post, typed_people
from ..slack_gateway import SlackError, SlackGateway
from ..store import Store
from .todo import add_version, body_shown_at, permalink

log = logging.getLogger(__name__)

JOB = "compose"
WRITER_STEP = "feedback_writer"   # the feedback writer's chain: DeepSeek v4 Pro, then GLM 5.3 (config.toml [models])
MAX_TOKENS = 4000                 # a reasoning model spends tokens before it answers
TITLE = "Message to the channel"
KIND = "message"
KEPT_TTL_S = MARKER_TTL_S  # a kept polish is reused for as long as R43 remembers a timed-out send (an hour)
REVISION_UNSURE = ("The revised draft may have reached your DM, but I could not confirm it. `post` still sends the "
                   "draft above it; reply edit: again for a fresh one.")


class RevisionUnsure(ValueError):
    """The revised card's send may have landed (a timeout, a tool-call mismatch). The revision is not saved: the draft
    stays as it was, and Haseeb is told exactly that. A fixed phrase, never his text."""


@dataclass(frozen=True)
class Polished:
    text: str
    note: str
    model: str | None   # None: not polished, the text is his own


def new_item_id(now: datetime) -> str:
    """The id of a message item made on the page: the time of the click, like a Slack ts."""
    return f"message-{now.timestamp():.6f}"


def say_item_id(source_ts: str) -> str:
    """The id of a message item made from a `say:` DM: the ts of that DM message, so it is made once."""
    return f"message-{source_ts}"


def raw_key(item_id: str) -> str:
    return f"message_raw:{item_id}"


def note_key(item_id: str) -> str:
    return f"message_note:{item_id}"


def people_key(item_id: str) -> str:
    return f"message_people:{item_id}"


def draft_key(raw: str, people: dict[str, str] | None = None) -> str:
    """The kept first polish of one text of his (and the people it mentions): the same words always find the same
    record."""
    basis = raw if not people else raw + "\n" + json.dumps(sorted(people.items()), ensure_ascii=False)
    return "message_draft:" + hashlib.sha1(basis.encode()).hexdigest()[:20]


def revision_key(item_id: str, command_ts: str | None) -> str:
    return f"message_revision:{item_id}:{command_ts or ''}"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:20]


def post_link(settings: Settings, ts: str, channel_id: str | None = None) -> str:
    """Permalink of the new top-level post, in the channel it went to (the program channel by default), without the thread query."""
    return permalink(settings.workspace_url, settings.channel_id if channel_id is None else channel_id, ts, ts).split("?", 1)[0]


class MessageHandlers:
    """`Handlers` for kind "message" (see commands.process_dm and page_commands), plus the two ways in."""

    def __init__(self, slack: SlackGateway, llm: LLM, store: Store, settings: Settings, *,
                 clock: Callable[[], float] = time.time, hold: Callable[[datetime], bool] | None = None):
        self.slack, self.llm, self.store, self.settings = slack, llm, store, settings
        self.clock = clock  # the epoch clock: the age of a kept polish (tests)
        self.hold = hold  # R33's predicate (`cli.message_hold`): True while the huddle poll must not be blinded

    def holding(self, now: datetime) -> bool:
        """Composer rows and `say:` drafts wait while this is true: in the huddle window, until today's Next Todo is
        final. A polish is minutes of model calls under the tick lock, and the Next Todo has its 5-minute rule (R33
        skips the feedback job for the same reason)."""
        return bool(self.hold and self.hold(now))

    # -- what is kept for a retry --------------------------------------------------------------

    def _kept(self, key: str, **must_match) -> Polished | None:
        """The polish kept under `key`, or None: none, an unreadable one, one older than KEPT_TTL_S (cleared), or one
        whose `must_match` fields (the instruction and the text it was made from) are not these."""
        value = self.store.get_cursor(key)
        if not value:
            return None
        try:
            rec = json.loads(value)
            fresh = self.clock() - float(rec["at"]) <= KEPT_TTL_S
            polished = Polished(rec["text"], rec["note"], rec.get("model"))
            same = all(rec.get(k) == v for k, v in must_match.items())
        except (ValueError, KeyError, TypeError, AttributeError):
            fresh, polished, same = False, None, False
        if not fresh:
            self.store.set_cursor(key, "")  # old, or unreadable: gone
            return None
        return polished if same else None

    def _keep(self, key: str, polished: Polished, **fields) -> None:
        rec = {"text": polished.text, "note": polished.note, "model": polished.model, "at": self.clock(), **fields}
        self.store.set_cursor(key, json.dumps(rec, ensure_ascii=False))

    def _people(self, item_id: str) -> dict[str, str]:
        """The people he mentioned in a `say:` text, {Name: user id}; none for a message typed on the page."""
        try:
            value = json.loads(self.store.get_cursor(people_key(item_id)) or "{}")
        except ValueError:
            return {}
        return {str(n): str(u) for n, u in value.items()} if isinstance(value, dict) else {}

    def _purge(self, prefix: str) -> None:
        """Clear the kept polishes of this kind that nobody came back for (their age is checked by `_kept`)."""
        for key, value in self.store.cursors(prefix).items():
            if value:
                self._kept(key)

    # -- polishing --------------------------------------------------------------------------

    def polish(self, raw: str, *, current: str | None = None, instruction: str | None = None) -> Polished:
        """Ask the chain for a polished version, trying the next model when an answer cannot be used. LLMError when no
        model gave one (infra=True only if every failure was the models being unreachable). LinkChanged at once, with
        no other model asked, when an answer has a link that is not exactly his."""
        system = SYSTEM
        user = user_prompt(raw, self.store.prefs(MAX_PREFS, kind=KIND), current=current, instruction=instruction)
        after = None
        for _ in range(max(1, len(self.settings.models.get(WRITER_STEP, [])))):
            try:
                c = self.llm.complete(WRITER_STEP, system, user, max_tokens=MAX_TOKENS, json_mode=True, after=after)
            except LLMError as e:
                if after is None:
                    raise
                raise LLMError(str(e), infra=False) from None  # an earlier model answered, but badly
            try:
                text, note = parse_reply(c.text, raw, current or "", instruction or "")
            except LinkChanged:
                log.warning("compose: %s changed a link; the reply is not used", c.model)
                raise
            except ValueError as e:
                log.warning("compose: %s gave an unusable reply (%s); trying the next model", c.model, e)
                after = c.model
                continue
            return Polished(text, note, c.model)
        raise LLMError("no model produced a usable message")

    def _polish_or_fall_back(self, raw: str) -> Polished:
        """The chain's polish, or his own words with the note that says why: a changed link (not repaired, no other
        model asked), the models unreachable, or answers that could not be used (a model that did answer is not
        "unavailable")."""
        try:
            return self.polish(raw)
        except LinkChanged:
            return Polished(raw, NOT_POLISHED_LINK, None)
        except LLMError as e:
            log.warning("compose: no polished draft, using his own words (%s)", "unreachable" if e.infra else "bad")
            return Polished(raw, NOT_POLISHED if e.infra else NOT_POLISHED_BAD, None)

    # -- the two ways in --------------------------------------------------------------------

    def compose(self, raw: str, item_id: str, now: datetime, *, slack_text: bool = False) -> Item:
        """Polish `raw` and send the draft to the DM as the new item `item_id`. ValueError (a fixed phrase) when the
        text is empty or too long or the item exists; a Slack failure propagates, leaving no item. `slack_text`: `raw` is
        the text of a DM message (`say:`), with Slack's markup: mentions, links and entities are made plain text first
        (`message_prompt.slack_to_text`), and a person he mentioned stays a real mention in the post.

        The polish is kept before the send (`draft_key`): whatever happens to the send, a retry of these words sends
        exactly this text, never a second polish that R43 could confuse with a card that already landed."""
        raw, people = slack_to_text(raw) if slack_text else (normalize_raw(raw), {})
        if not raw:
            raise ValueError("there is nothing to polish")
        if len(raw) > MAX_RAW_CHARS:
            raise ValueError(f"that is {len(raw)} characters; the limit is {MAX_RAW_CHARS}")
        if self.store.get_item(item_id) is not None:
            raise ValueError("that message was already drafted")
        self._purge("message_draft:")
        key = draft_key(raw, people)
        polished = self._kept(key)
        if polished is None:
            with events.step(self.store, JOB, "polish") as ev:
                polished = self._polish_or_fall_back(raw)
                ev.model = polished.model
            self._keep(key, polished)
        text = render_message_dm(polished.text, polished.note, polished.model, pings=message_pings(polished.text, raw))
        with events.step(self.store, JOB, "deliver"):
            dm_ts = send_dm(self.slack, self.store, self.settings, text)
        # The card is in his DM: from here nothing may stop the item from existing, so what is only bookkeeping is
        # best effort (a made draft must never show as a failed Polish, review M11).
        self._best_effort("the draft version", lambda: add_version(self.store, item_id, dm_ts, polished.text))  # N2
        self._best_effort("the raw text", lambda: self.store.set_cursor(raw_key(item_id), raw))
        self._best_effort("the note", lambda: self.store.set_cursor(note_key(item_id), polished.note))
        if people:
            self._best_effort("the people", lambda: self.store.set_cursor(
                people_key(item_id), json.dumps(people, ensure_ascii=False)))
        item = Item(
            id=item_id, kind=KIND, day=now.astimezone(self.settings.tz).date(), status="delivered",
            body=polished.text, title=TITLE, dm_ts=dm_ts, source_ts=None, score=None,
            models={"wrote": polished.model} if polished.model else {}, attempts=0, edited=False, created_at=now,
            acted_at=None,
        )
        self.store.upsert_item(item)
        self._best_effort("the kept draft", lambda: self.store.set_cursor(key, ""))
        self._best_effort("the run record", lambda: self.store.record_run(
            JOB, True, f"sent {item_id} ({polished.model or 'not polished'})"))
        self._best_effort("the banner", lambda: show_banner(self.settings, "message",
                                                            "✍️ Your message is polished: check your DM"))
        return item

    @staticmethod
    def _best_effort(what: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - the draft exists; this is only bookkeeping
            log.warning("compose: %s was not recorded (%s)", what, type(e).__name__)

    def say(self, raw: str, source_ts: str, now: datetime) -> Item:
        """`say: <raw>` from the DM message with ts `source_ts`: the same item, made once for that message. A message
        whose item exists (an earlier run died before it noted the message as done) is not drafted again."""
        item_id = say_item_id(source_ts)
        if (existing := self.store.get_item(item_id)) is not None:
            return existing
        return self.compose(raw, item_id, now, slack_text=True)

    # -- Handlers (post, edit, shorter, skip run through commands._Runner) --------------------

    def post(self, item: Item, reply_ts: str | None = None, channel_id: str | None = None) -> str:
        """Post the draft to the channel as a new top-level message; returns its permalink, in the channel it went to. The only
        channel write in this module, and never retried (R16).

        `channel_id` is the channel he chose (the runner has looked it up in his list, `commands.post_target`); None is the
        program channel. It is recorded as `post_channel:{id}` before the send, with the send-started mark, so a post that fails
        keeps its target (the "may have gone out" lines name it and a retry is pinned to it). Anything that is not a channel id
        raises ValueError here, before any mark or send. The text is the same in every channel: the link guard and the rule that
        @channel and @here are real only when he typed them are applied to the draft, never to the channel.

        N2: with the command's `reply_ts` (the reply's, or the dashboard click's), the text posted is the draft version
        that was showing when he replied: the latest whose DM ts is earlier. A `post` written before a revision landed
        never sends it, and one older than every version is refused (LookupError), as the Next Todo does."""
        shown = body_shown_at(self.store, item.id, reply_ts) if reply_ts else None
        raw = self.store.get_cursor(raw_key(item.id)) or ""
        text = render_message_post(item.body if shown is None else shown, mass_mentions(raw),
                                   typed_people(raw, self._people(item.id)))
        if not text.strip():
            raise ValueError("the draft is empty, so there is nothing to post")
        channel = self.settings.channel_id if channel_id is None else channel_id
        channels.record_post_channel(self.store, item.id, channel)
        mark_send_started(self.store, item.id)  # a failure from here on may have posted (C1)
        ts = self.slack.send(channel, text)  # no thread_ts: a message of its own, not a reply
        clear_send_started(self.store, item.id)
        self.store.set_cursor(f"own:{ts}", "1")
        return post_link(self.settings, ts, channel)

    def revise(self, item: Item, instruction: str) -> Item:
        return self.revise_for(item, instruction, None)

    def revise_for(self, item: Item, instruction: str, command_ts: str | None) -> Item:
        """Re-polish the current draft with Haseeb's instruction and send the new version in the item's DM thread.
        `command_ts` is the ts of the command (the reply's, or the dashboard click's): the revision is kept per item
        and command before it is sent (`revision_key`), so the same command sent again sends exactly that text.
        A send that may have landed raises RevisionUnsure and leaves the draft as it was."""
        raw = self.store.get_cursor(raw_key(item.id)) or item.body
        key = revision_key(item.id, command_ts)
        self._purge("message_revision:")
        base = _sha(item.body)
        polished = self._kept(key, instruction=instruction, base=base)
        if polished is None:
            with events.step(self.store, JOB, "polish") as ev:
                polished = self.polish(raw, current=item.body, instruction=instruction)  # LLMError, LinkChanged: fails
                ev.model = polished.model
            self._keep(key, polished, instruction=instruction, base=base)
        text = render_message_dm(polished.text, polished.note, polished.model, revised=True,
                                 pings=message_pings(polished.text, raw))
        try:
            ts = send_dm(self.slack, self.store, self.settings, text, thread_ts=item.dm_ts)
        except SlackError as e:
            if _maybe_landed(e):  # its card may be in the thread: the record stays, and he is told which draft stands
                raise RevisionUnsure(REVISION_UNSURE) from e
            self.store.set_cursor(key, "")
            raise
        add_version(self.store, item.id, ts, polished.text)  # N2: before the body changes, so `post` never misses it
        self._best_effort("the note", lambda: self.store.set_cursor(note_key(item.id), polished.note))
        fresh = self.store.get_item(item.id) or item
        self.store.mark(item.id, fresh.status, body=polished.text, models={**fresh.models, "wrote": polished.model},
                        edited=True)
        self._best_effort("the kept revision", lambda: self.store.set_cursor(key, ""))
        return self.store.get_item(item.id) or item
