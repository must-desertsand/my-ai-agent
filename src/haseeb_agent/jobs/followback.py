"""Job 2b: the threads the feedback job watches (2026-10-21; fix round 1 after the review, 2026-10-22).

The feedback job reads top-level posts only. Two kinds of reply never reach it, and both are looked for here, inside the same
job (`run_feedback` calls `watch_threads` after the new posts), under the same schedule, huddle hold and pause:

  1. FOLLOW-BACKS. A teammate answers Haseeb's feedback (fixes made, a question, a new build). The thread of a post he gave
     posted feedback on is watched; a reply from the thread's author (or one that @mentions Haseeb) that is newer than his
     latest message in it, and is more than thanks, gets a short draft: what was verified on the links or the attachment of
     THAT reply (the site review, in claims mode), an answer to the question, at most one next step. Item
     `followback-<reply ts>`. At most 2 a thread, ever.
  2. REPLIES IN THE THREAD OF HIS INSIGHTS. The thread of an insight he posted is watched; a teammate's reply newer than his
     latest message there is sorted by a cheap model (`summarize` chain):
       own_post       a standalone update of their own, put in the wrong place: a NORMAL feedback draft (item
                      `threadfb-<reply ts>`; the same prompt, quality gate, site review and limits as top-level feedback,
                      and it counts toward their 2-a-day cap), opening with a note that it landed in his insight's thread;
       about_insight  a comment or question about the insight: a short reply (item `insreply-<reply ts>`) from the insight's
                      own text and evidence, which invents nothing and says "not verified";
       other          thanks, emoji, chatter: nothing.
     At most 3 drafts of any kind in one insight thread.

"His latest message" is any message of his in the thread except what the agent posted as part of the insight: the parts of the
post (`insight_part:<ts>`, recorded by `InsightHandlers.post`) and the deck it attaches later. A reply he answered by hand is
left alone.

Detection is cheap. A thread is watched for 7 days from his reply or post, and scanned while its post is at most 4 days old:
one read of the channel back to the oldest such post (at most every 30 minutes in working hours, every 2 hours otherwise)
shows, for each watched thread, its newest reply; a thread whose newest reply is newer than the one last looked at (cursor
`followback_seen:<thread ts>`) is read with `read_thread`, at most 5 reads a run for both kinds together. A full page is
logged and the next scan reads what is older (`followback_cut`); a thread that cannot be read is retried 3 times, a scan apart,
and then left alone until it has a newer reply. At most 2 drafts a run, and 6 a day across both kinds; a failed attempt uses
the allowance like a delivery, and the site review of a reply is made once.

Delivery is the feedback job's: a claim per reply (`feedback_claim:<reply ts>`, the C1 fix), the item created insert-only, a
card in the DM, `post` / `edit:` / `shorter` / `skip` as for any feedback item (FeedbackHandlers), never an auto-post. A 👀
goes on the teammate's REPLY when drafting starts and a ✅ on it when Haseeb's reply is posted.

Everything from Slack, the reviewed site or a file is data between tags (followback_prompt.py). A draft is checked in code:
"not checked" where nothing was checked ("first screen only" where one first screen was), no number, id or address that nobody
wrote. Nothing here is posted without Haseeb's `post`. `preview` (`haseeb-agent watch --dry-run`) shows what the next look would
find: it drafts nothing, the watch stores nothing and it prints no text, but its Slack reads are real (headless Haiku runs on
the shared quota; a read that fails for a passing reason is tried once more) and the gateway may note a new tool as on any read.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from .. import channels, events, history
from ..commands import dm_send_failed, send_dm
from ..config import Settings
from ..followback_prompt import (CLASSES, CLASSIFY_SYSTEM, CONTEXT_CHARS, EARLIER_CHARS, FOLLOWBACK_SYSTEM, INSREPLY_SYSTEM,
                                 ORIGINAL_CHARS, classify_user, followback_user, insreply_user, parse_class, valid_followback,
                                 valid_insreply)
from ..litellm import LLM, LLMError
from ..models import FileRef, Item, Msg
from ..notify import notify as show_banner
from ..offline import OfflineError
from ..render import model_line, render_followback_dm, render_insreply_dm
from ..site_review import service as site_service
from ..site_review.targets import file_label, find_links, is_html_file
from ..slack_gateway import FileContent, SlackError, SlackGateway, SlackPaused, forget_last_read_failure
from ..store import Store
from . import feedback as fb
from .insights import PART_PREFIX, SCHEMA_V2, draft_from
from .todo import permalink

log = logging.getLogger(__name__)

JOB = fb.JOB
WATCH_DAYS = 7
WATCH_WINDOW = timedelta(days=WATCH_DAYS)
SCAN_EVERY = timedelta(minutes=30)       # the channel is looked at for news at most this often in working hours...
SCAN_EVERY_OFF = timedelta(hours=2)      # ...and at most this often at night and on days off, when the team writes little
DAY_HOURS = (time(8, 0), time(20, 0))    # working hours, Kuwait time on a workday (the tick's own day window)
SCAN_DAYS = 4                            # the scan looks at threads whose post is this young: a small window, a small page
SCAN_AT = "followback_scan_at"
CUT_AT = "followback_cut"                # the ts a full channel page was cut at: the next scan reads what is older
MAX_READS_PER_RUN = 5                    # thread reads, both kinds together
MAX_REPLY_AGE = timedelta(days=2)        # an older reply is no longer answered (and a stale backlog is never drafted)
MAX_PER_DAY = 6                          # drafts for replies in threads (follow-backs and insight threads), a day
MAX_FOLLOWBACKS_PER_THREAD = 2           # ever
MAX_DRAFTS_PER_INSIGHT = 3               # drafts of every kind (replies, posts of their own) in the thread of one insight
CLASS_PREFIX = "threadwatch_class:"      # + reply ts: how a reply in an insight's thread was sorted
DONE_PREFIX = fb.THREAD_DONE_PREFIX      # + reply ts: nothing more to do for this reply (the reason)
FAIL_PREFIX = "threadwatch_fail:"        # + reply ts: failed attempts to draft for it
CTX_PREFIX = "followback_ctx:"           # + item id: what `revise` needs to redraft (JSON)
READ_FAIL_PREFIX = "threadwatch_readfail:"  # + thread ts: "<failed reads> <time of the last>" of a thread that could not be read
MAX_READ_FAILS = 3                       # tries at reading a thread before it is left alone until it has a newer reply
NOTICE_PREFIX = "threadwatch_notice:"    # + reply ts: "<who>|<link>", a reply given up on whose notice has not gone out yet
NOTICE_DAY_PREFIX = "threadwatch_notices:"  # + Kuwait date: how many give-up lines went out that day
MAX_NOTICES_PER_DAY = 3
FAILED_DAY_PREFIX = "threadwatch_failed:"   # + Kuwait date: attempts to draft that failed that day (they use the day's allowance)

_QUIET_STRIP = re.compile(r":[a-z0-9_+\-]+:|<@[UW][A-Z0-9]+(?:\|[^>]*)?>")


SPENDS = ("delivered", "failed", "gave_up")  # outcomes of an attempt that used the run's and the day's allowance
RETRIES = ("pending", "failed")              # outcomes after which the reply is tried again on a later run


class _Stop(Exception):
    """The scan ends here for this run: paused, Slack or the models are down. Nothing is marked."""


@dataclass
class Env:
    slack: SlackGateway
    llm: LLM
    store: Store
    settings: Settings
    now: datetime


@dataclass
class Watch:
    thread_ts: str
    kind: str                  # "feedback": a teammate's post Haseeb gave feedback on; "insight": a posted insight of his
    anchor: datetime           # when his latest posted reply in the thread went out
    item: Item | None = None   # the insight, for an insight's thread


@dataclass
class Budget:
    reads: int = MAX_READS_PER_RUN
    drafts: int = 0
    day: int = 0
    delivered: int = 0
    incomplete: bool = False   # something was left for the next run (budget spent, a try failed): do not rest for SCAN_EVERY


# ---------------------------------------------------------------------------------------------------------------- small facts

def _secs(value: str | None) -> int | None:
    """Whole epoch seconds of a ts or a latest_reply, the precision the channel page shows a reply time at."""
    try:
        return int(float(value)) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def mentions_haseeb(text: str, settings: Settings) -> bool:
    """Whether a message @mentions Haseeb: `<@U08CZCGJW3E>` or `<@U08CZCGJW3E|Haseeb Zafar>`."""
    return re.search(rf"<@{re.escape(settings.haseeb_id)}(?:\|[^>]*)?>", text or "") is not None


def _teammate(msg: Msg, settings: Settings) -> bool:
    """Not Haseeb's own message, not a bot's, not Slackbot's."""
    return not (msg.is_bot or msg.user_id in (settings.haseeb_id, "USLACKBOT"))


def _quiet(reply: Msg) -> bool:
    """A reply that cannot need an answer: no file and (emoji codes and mentions aside) under 8 letters or digits, so a link
    still counts. It is `other` without asking a model."""
    if reply.files:
        return False
    return len(re.findall(r"\w", _QUIET_STRIP.sub(" ", reply.text or ""))) < 8


_ACK_WORDS = frozenset("""
    thanks thank you thx ty tysm so very much a lot lots many for the your this that it these those them feedback review
    reviewing comments comment notes help input time ok okay k sure yes yeah yep yup right noted got get understood agreed
    agree good great nice cool awesome perfect excellent lovely wonderful amazing appreciate appreciated will do ill i'll
    going to gonna fix fixing look looking check checking work working update updating on later today tomorrow tonight soon
    now shortly asap haseeb lol haha hehe wow cheers please welcome np no problem and also but be is are was were am i we me
    my our us let know one into
""".split())


def _acknowledgement(reply: Msg) -> bool:
    """A reply that only says thanks or that it will be done ("Thanks a lot Haseeb, I will fix these today."): no file, no
    question, no number, and nothing in it but words of thanks and of promise (emoji codes and mentions aside; a link or an
    address has words of its own). It needs no answer, and the 6 a day are not spent on politeness. One word of its own ("the
    badges") and it is a reply."""
    text = reply.text or ""
    if reply.files or "?" in text:
        return False
    words = re.findall(r"[a-z0-9']+", _QUIET_STRIP.sub(" ", text).lower())
    return bool(words) and all(w in _ACK_WORDS for w in words)


def _feedback_eligible(env: Env, w: Watch, parent: Msg, replies: list[Msg]) -> list[Msg]:
    """The replies in a teammate's thread that may be answered: newer than Haseeb's latest message, from a teammate, with
    something to answer (not a bare thanks, not a quiet one), and from the thread's author or @mentioning him. Oldest first."""
    se = env.settings
    floor = _floor(env, w, replies)
    return [r for r in replies if Decimal(r.ts) > floor and _teammate(r, se) and not _quiet(r) and not _acknowledgement(r)
            and (r.user_id == parent.user_id or mentions_haseeb(r.text or "", se))]


def _insight_eligible(env: Env, w: Watch, replies: list[Msg]) -> list[Msg]:
    """The replies in the thread of his insight that are sorted: newer than Haseeb's latest message, from any teammate."""
    floor = _floor(env, w, replies)
    return [r for r in replies if Decimal(r.ts) > floor and _teammate(r, env.settings)]


def _only_questions(text: str) -> bool:
    """Whether a reply is nothing but questions (emoji codes and mentions aside): it claims nothing, so there is nothing in it
    to call "not checked"."""
    plain = _QUIET_STRIP.sub(" ", text or "").strip()
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", plain) if x.strip()]
    return bool(sentences) and all(x.endswith("?") for x in sentences)


def _anchor(item: Item) -> datetime:
    return item.acted_at or item.created_at


def _is_deck_share(env: Env, w: Watch, r: Msg) -> bool:
    """The deck job's file share in an insight's thread: a PDF with no text, or with the deck's "📎" comment, or named like the
    deck job names its upload (`<insight id>.pdf`), once the agent has attached a deck there (`attach:<insight>` says it
    tried). Without that record, or for a PDF he sent with words of his own, it is his own message."""
    from .decks import DECK_COMMENT  # late: decks pulls in the whole deck builder
    if w.item is None or env.store.get_cursor(f"attach:{w.item.id}") not in ("started", "done", "failed"):
        return False
    text = (r.text or "").strip()
    plain = not text or text.startswith(DECK_COMMENT)
    named = f"{w.item.id}.pdf".lower()
    return any((f.mimetype.lower() == "application/pdf" or f.name.lower().endswith(".pdf"))
               and (plain or f.name.lower() == named) for f in r.files)


def _is_part(env: Env, w: Watch, r: Msg) -> bool:
    """A further part of the insight post itself (a long insight goes out as several messages). `InsightHandlers.post` records
    the ts of each part it sends (`insight_part:<ts>` = the insight's id), so this is an exact set: what the agent recorded as
    a part of THIS insight, and nothing it merely looks like."""
    return w.item is not None and env.store.get_cursor(f"{PART_PREFIX}{r.ts}") == w.item.id


def _counts_as_his(env: Env, w: Watch, r: Msg) -> bool:
    """Whether a message in the thread is a reply of Haseeb's that a teammate's reply must be newer than: any message of his
    (one he typed, or one he approved through `post`: a feedback, a follow-back, a reply on his insight), except what the agent
    itself posted as part of an insight: the recorded parts of the post, and the deck it attaches later, which must not hide a
    reply that came before it. In a teammate's thread nothing is left out: his posted feedback and follow-backs are his
    replies there."""
    if r.user_id != env.settings.haseeb_id:
        return False
    if w.kind != "insight":
        return True
    return not (_is_part(env, w, r) or _is_deck_share(env, w, r))


def _floor(env: Env, w: Watch, replies: list[Msg]) -> Decimal:
    """The time a reply must be newer than to be answered: Haseeb's latest message in the thread that counts (see
    `_counts_as_his`; else the time his reply or his post went out), and not older than MAX_REPLY_AGE."""
    mine = [Decimal(r.ts) for r in replies if _counts_as_his(env, w, r)]
    base = max(mine) if mine else Decimal(f"{w.anchor.timestamp():.6f}")
    return max(base, Decimal(f"{(env.now - MAX_REPLY_AGE).timestamp():.6f}"))


def _done(store: Store, ts: str) -> bool:
    """Nothing more to do for this reply: it has a draft (of any kind: as a post of the channel too, when it was also sent
    there), or was sorted away, capped or given up (by this watch or by the feedback scan of the channel)."""
    return (fb.drafted_for(store, ts) or store.get_cursor(f"{DONE_PREFIX}{ts}") is not None
            or any(store.get_cursor(f"{prefix}{ts}") is not None
                   for prefix in ("feedback_gaveup:", "feedback_empty:", "feedback_capped:")))


def _finish(store: Store, ts: str, why: str) -> None:
    store.set_cursor(f"{DONE_PREFIX}{ts}", why)


def scan_every(settings: Settings, now: datetime) -> timedelta:
    """How long a complete scan rests: SCAN_EVERY in working hours, SCAN_EVERY_OFF at night and on a day off."""
    local = now.astimezone(settings.tz)
    working = local.weekday() in settings.workdays and DAY_HOURS[0] <= local.time() < DAY_HOURS[1]
    return SCAN_EVERY if working else SCAN_EVERY_OFF


def insight_drafts(store: Store, thread_ts: str) -> int:
    """The drafts made for replies in the thread of an insight, of both kinds, whatever became of them."""
    return store.count_thread_items(fb.INSREPLY, thread_ts) + store.count_thread_items(fb.THREADFB, thread_ts)


def made_today(store: Store, settings: Settings, now: datetime) -> int:
    """Drafts for replies in threads (follow-backs and insight-thread drafts) made today, in Kuwait time."""
    day = now.astimezone(settings.tz).date()
    return sum(1 for it in store.items_for_day("feedback", day) if fb.variant_of(it.id))


def failed_today(store: Store, settings: Settings, now: datetime) -> int:
    """Attempts to draft for a reply in a thread that failed today. They use the day's allowance like a draft does."""
    try:
        return int(store.get_cursor(f"{FAILED_DAY_PREFIX}{now.astimezone(settings.tz).date().isoformat()}") or 0)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------------------------------------------- what to watch

def watched_threads(store: Store, settings: Settings, now: datetime) -> list[Watch]:
    """The threads to watch: where Haseeb has a POSTED feedback reply (in a teammate's thread) and the threads of his posted
    insights, with his reply in the last 7 days. A follow-back thread that is full (2 follow-backs) is no longer watched. Only
    the leadership channel is watched: an insight he posted to another channel is not (the docs say so)."""
    today = now.astimezone(settings.tz).date()
    since = now - WATCH_WINDOW
    found: dict[str, Watch] = {}
    for it in store.recent_items("feedback", WATCH_DAYS * 2, today):
        if it.status != "posted" or not it.source_ts or fb.variant_of(it.id) in ("own_post", "insight_reply"):
            continue  # (the replies he posted in an insight's thread belong to that insight's watch)
        try:
            anchor = _anchor(it)
            fresh = anchor >= since
        except TypeError:  # a clock without a zone: not guessed at
            continue
        if fresh and (it.source_ts not in found or anchor > found[it.source_ts].anchor):
            found[it.source_ts] = Watch(it.source_ts, "feedback", anchor)
    for it in store.recent_items("insight", WATCH_DAYS * 2, today):
        ts = store.get_cursor(f"post_ts:{it.id}") if it.status == "posted" else None
        try:
            fresh = _anchor(it) >= since
        except TypeError:
            continue
        # The watch reads the leadership channel and nothing else: an idea he posted to another channel is not watched (a post
        # with no record of its channel, which includes every one made before the choice existed, is in the leadership channel).
        if ts and fresh and channels.post_channel(store, settings, it.id) == settings.channel_id:
            found[ts] = Watch(ts, "insight", _anchor(it), it)
    return [w for w in found.values() if not _full(store, w)]


def _full(store: Store, w: Watch) -> bool:
    """A thread that can take no more drafts (2 follow-backs, or 3 drafts on an insight) cannot change anything: not watched."""
    if w.kind == "feedback":
        return store.count_thread_items(fb.FOLLOWBACK, w.thread_ts) >= MAX_FOLLOWBACKS_PER_THREAD
    return insight_drafts(store, w.thread_ts) >= MAX_DRAFTS_PER_INSIGHT


def insight_text(item: Item | None) -> str:
    """An insight's own text and evidence, as it was posted, for the writer and the sorter. Since 2026-10-22 the daily
    post is an idea (a v2 body, jobs/insights.py): its sections stand in for the insight's."""
    if item is None:
        return ""
    try:
        meta = json.loads(item.body)
        d = meta["insight"]
        if meta.get("v") == SCHEMA_V2:
            idea = draft_from(meta)
            lines = [f"Title: {idea.title} · {idea.price}", f"The idea: {idea.idea}", f"Why now: {idea.why_now}",
                     f"Who pays, and how much: {idea.who_pays}", f"How we build it with AI: {idea.build_with_ai}",
                     f"First step this week: {idea.first_step}"]
            if idea.risks:
                lines.append("Risks: " + "; ".join(idea.risks))
        else:
            lines = [f"Title: {d['title']}", f"What happened: {d['what_happened']}", f"Why it matters: {d['why_it_matters']}",
                     f"If we were in Silicon Valley: {d['silicon_valley']}"]
        evidence = [f"{e['title']} ({e['url']})" for e in d.get("evidence") or [] if isinstance(e, dict)]
        if evidence:
            lines.append("Evidence: " + "; ".join(evidence))
        if d.get("not_verified"):
            lines.append("Not verified at the time: " + "; ".join(str(x) for x in d["not_verified"]))
        return "\n".join(lines)
    except (ValueError, KeyError, TypeError):
        return item.title or ""


def insight_gist(item: Item | None) -> str:
    """What the post is about in a few sentences, for the sorter: an insight's "what happened", an idea's "the idea"."""
    try:
        d = json.loads(item.body)["insight"] if item is not None else {}
        gist = d["what_happened"] if "what_happened" in d else d.get("idea", "")
        return gist if isinstance(gist, str) else ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return ""


def _haseeb_words(replies: list[Msg], settings: Settings) -> str:
    """What Haseeb last said in the thread (his latest two messages), as it stands in the thread."""
    mine = [(r.text or "").strip() for r in replies if r.user_id == settings.haseeb_id and (r.text or "").strip()]
    return fb._defuse("\n\n".join(mine[-2:]))[-EARLIER_CHARS:]


# ---------------------------------------------------------------------------------------------------------------- the watch

def watch_threads(slack: SlackGateway, llm: LLM, store: Store, settings: Settings, now: datetime, *,
                  drafts_allowed: int = fb.MAX_THREAD_DRAFTS) -> int:
    """Look at the watched threads and draft for the replies that need an answer. Returns the drafts delivered to the DM.
    Never raises (the offline guard apart): a draft that cannot be made waits for the next run."""
    try:
        return _watch(Env(slack, llm, store, settings, now), drafts_allowed)
    except OfflineError:
        raise
    except _Stop:
        return 0
    except Exception as e:  # noqa: BLE001 - the watch is an addition: it must never fail the feedback run
        log.warning("followback: the thread watch failed (%s)", type(e).__name__)
        return 0


def _watch(env: Env, drafts_allowed: int) -> int:
    st, se = env.store, env.settings
    if drafts_allowed <= 0:
        return 0
    watched = _in_window(env, watched_threads(st, se, env.now))
    if not watched:  # nothing the scan looks at: no read at all
        return 0
    cut = st.get_cursor(CUT_AT) or None  # walking back through a page that was full: no rest in between
    if cut is None:
        try:
            last = datetime.fromisoformat(st.get_cursor(SCAN_AT) or "")
            if env.now - last < scan_every(se, env.now):
                return 0
        except (ValueError, TypeError):
            pass
    day_left = MAX_PER_DAY - made_today(st, se, env.now) - failed_today(st, se, env.now)
    if day_left <= 0:
        return 0
    page = _read_page(env, watched, latest=cut)
    if page is None:
        return 0
    b = Budget(drafts=min(drafts_allowed, day_left), day=day_left)
    if len(page) >= fb.PAGE_LIMIT:  # the oldest watched threads fell off the page: the next scan reads what is older than it
        log.warning("followback: the channel page is full (%d messages); the next scan reads the older part", len(page))
        st.set_cursor(CUT_AT, min(page, key=lambda m: Decimal(m.ts)).ts)
        b.incomplete = True
    elif cut is not None:
        st.delete_cursor(CUT_AT)
    try:
        for latest, w, _stub in _news(env, page, watched):
            if b.reads <= 0 or b.drafts <= 0 or b.day <= 0:
                b.incomplete = True
                break
            if _backing_off(env, w):  # it could not be read a moment ago: not again before the next scan
                continue
            b.reads -= 1
            thread = _read_thread(env, w)
            if thread is None:
                _read_failed(env, w, latest)
                continue
            st.delete_cursor(f"{READ_FAIL_PREFIX}{w.thread_ts}")
            parent, replies = thread
            history.capture(st, se, replies, source_channel=se.channel_id, parent=parent)  # never raises
            if w.kind == "feedback":
                _feedback_thread(env, b, w, latest, parent, replies)
            else:
                _insight_thread(env, b, w, latest, parent, replies)
    except _Stop:
        b.incomplete = True
    else:
        try:
            _tell_gave_up(env)  # one line for the run, whoever it was given up on
        except _Stop:  # the pause landed: the line waits for Resume, and what was delivered stands
            b.incomplete = True
    if not b.incomplete:
        st.set_cursor(SCAN_AT, env.now.isoformat())
    return b.delivered


def _step(env: Env, name: str, quiet: bool):
    """The job step a read is recorded under (the Live page shows it); none for a preview, which writes nothing."""
    return contextlib.nullcontext() if quiet else events.step(env.store, JOB, name)


def _read_page(env: Env, watched: list[Watch], *, quiet: bool = False, latest: str | None = None) -> list[Msg] | None:
    """One read of the channel, back to the oldest watched thread: its parents show each thread's newest reply. `latest`: only
    what is older than that ts (the part of the channel that a full page left out)."""
    oldest = fb._neighbour(min(watched, key=lambda w: Decimal(w.thread_ts)).thread_ts, -1)
    try:
        with _step(env, "read_channel", quiet):
            return env.slack.read_channel(env.settings.channel_id, oldest, fb.PAGE_LIMIT, latest=latest)
    except SlackPaused:
        raise _Stop from None
    except SlackError as e:
        log.warning("followback: cannot read the channel: %s", e)
        return None


def _news(env: Env, page: list[Msg], watched: list[Watch], *, ignore_seen: bool = False) -> list[tuple[int, Watch, Msg]]:
    """The watched threads whose parent, on the channel page, shows a newer reply than the one last looked at (and not older
    than MAX_REPLY_AGE), the oldest news first. `ignore_seen`: every thread with a reply that young (the preview)."""
    by_ts = {m.ts: m for m in page}
    out: list[tuple[int, Watch, Msg]] = []
    oldest = int((env.now - MAX_REPLY_AGE).timestamp())
    for w in watched:
        parent = by_ts.get(w.thread_ts)
        latest = _secs(parent.latest_reply) if parent is not None else None
        if latest is None or latest < oldest:  # no reply to answer: nothing to read
            continue
        seen = None if ignore_seen else _secs(env.store.get_cursor(f"{fb.SEEN_PREFIX}{w.thread_ts}"))
        if seen is None or latest > seen:
            out.append((latest, w, parent))
    out.sort(key=lambda t: t[0])
    return out


def _read_thread(env: Env, w: Watch, *, quiet: bool = False) -> tuple[Msg, list[Msg]] | None:
    try:
        with _step(env, "read_thread", quiet):
            return env.slack.read_thread(env.settings.channel_id, w.thread_ts)
    except SlackPaused:
        raise _Stop from None
    except SlackError as e:
        log.warning("followback: cannot read thread %s: %s", w.thread_ts, e)
        return None


@dataclass(frozen=True)
class Candidate:
    """A reply the watch would draft for (a follow-back) or sort (a reply in the thread of an insight) right now."""
    thread_ts: str
    reply_ts: str
    kind: str        # "followback" (a teammate's thread), or "insight" (his insight's thread: the sorter decides what it gets)
    author_id: str   # the Slack user id, never a name or a word of what was said


@dataclass
class Preview:
    watched: int = 0                    # threads watched
    scanned: int = 0                    # ... of which the scan looks at
    with_news: int = 0                  # ... of which the channel page shows a reply of the last two days
    read: list[str] = field(default_factory=list)       # the threads that were read (their ts)
    not_read: int = 0                   # ... and those over the limit of reads
    candidates: list[Candidate] = field(default_factory=list)
    page_failed: bool = False           # the channel could not be read
    page_full: bool = False             # the page was full: threads older than its oldest message are not shown


PREVIEW_MAX_READS = 15


def preview(slack: SlackGateway, store: Store, settings: Settings, now: datetime) -> Preview:
    """What the watch would look at right now and for which replies. Nothing is drafted, sent or reacted to, no writing or sorting
    model is asked, and the watch records no step and writes nothing to the store (no seen mark, no cursor, no history). The Slack
    reads are real: the connector's headless Haiku runs on the shared quota, one for the channel page and up to PREVIEW_MAX_READS
    for threads, and, as on any read, the gateway may write its `new_tools_alert:` cursor when Claude Code offers new tools. The
    rests, the caps and the seen marks are left out, so it shows what is eligible at this moment, not what the next run would do.
    It is `haseeb-agent watch --dry-run`."""
    env = Env(slack, None, store, settings, now)  # type: ignore[arg-type]  # no writing or sorting model is ever asked
    out = Preview()
    watched = watched_threads(store, settings, now)
    out.watched = len(watched)
    scan = _in_window(env, watched)
    out.scanned = len(scan)
    if not scan:
        return out
    page = _read_page(env, scan, quiet=True)
    if page is None:
        out.page_failed = True
        return out
    out.page_full = len(page) >= fb.PAGE_LIMIT
    news = _news(env, page, scan, ignore_seen=True)
    out.with_news = len(news)
    for _latest, w, _stub in news:
        if len(out.read) >= PREVIEW_MAX_READS:
            out.not_read += 1
            continue
        thread = _read_thread(env, w, quiet=True)
        if thread is None:
            out.not_read += 1
            continue
        out.read.append(w.thread_ts)
        parent, replies = thread
        if w.kind == "feedback":
            eligible = _feedback_eligible(env, w, parent, replies)
            chosen = eligible[-1:] if eligible and not _done(store, eligible[-1].ts) and not _full(store, w) else []
            kind = "followback"
        else:
            chosen = [r for r in _insight_eligible(env, w, replies) if not _done(store, r.ts)] if not _full(store, w) else []
            kind = "insight"
        out.candidates += [Candidate(w.thread_ts, r.ts, kind, r.user_id) for r in chosen]
    return out


def _in_window(env: Env, watched: list[Watch]) -> list[Watch]:
    """The watched threads the scan looks at: those whose post is at most SCAN_DAYS old. The channel is read back to the oldest of
    them, so this is what keeps the page small; a thread older than that is not looked at (a reply to a post of last week is
    rare, and one that does come is not news to a scan that rests)."""
    oldest = Decimal(f"{(env.now - timedelta(days=SCAN_DAYS)).timestamp():.6f}")
    return [w for w in watched if Decimal(w.thread_ts) >= oldest]


def _read_tries(env: Env, w: Watch) -> tuple[int, datetime | None]:
    """How many times in a row the thread could not be read, and when the last try was."""
    try:
        n, _, at = (env.store.get_cursor(f"{READ_FAIL_PREFIX}{w.thread_ts}") or "").partition(" ")
        return int(n), datetime.fromisoformat(at)
    except ValueError:
        return 0, None


def _backing_off(env: Env, w: Watch) -> bool:
    """A thread that could not be read is not tried again for SCAN_EVERY, whatever else keeps the runs coming."""
    n, at = _read_tries(env, w)
    try:
        return n > 0 and at is not None and env.now - at < SCAN_EVERY
    except TypeError:  # a time without a zone: not guessed at
        return False


def _read_failed(env: Env, w: Watch, latest: int) -> None:
    """A thread that cannot be read (Slack fails, or the text is not something the parser trusts): it is tried again at the next
    scan, MAX_READ_FAILS times in all, and then left alone until it has a newer reply. The tick counts the first failed read as
    a failed run of the job; a retry that fails again is not counted a second time."""
    n, _ = _read_tries(env, w)
    n += 1
    if n > 1:
        forget_last_read_failure()
    if n >= MAX_READ_FAILS:
        log.warning("followback: thread %s could not be read %d times; left alone until it has a newer reply", w.thread_ts, n)
        env.store.delete_cursor(f"{READ_FAIL_PREFIX}{w.thread_ts}")
        _mark_seen(env, w, latest, [])
    else:
        env.store.set_cursor(f"{READ_FAIL_PREFIX}{w.thread_ts}", f"{n} {env.now.isoformat()}")


def _mark_seen(env: Env, w: Watch, page_latest: int, replies: list[Msg]) -> None:
    """The thread was looked at up to its newest reply: it is read again only when a newer one shows."""
    newest = max([page_latest] + [s for s in (_secs(r.ts) for r in replies) if s is not None])
    env.store.update_cursor(f"{fb.SEEN_PREFIX}{w.thread_ts}",
                            lambda raw: str(max(newest, int(raw))) if (raw or "").isdigit() else str(newest))


# ---------------------------------------------------------------------------------------------------------------- follow-backs

def _feedback_thread(env: Env, b: Budget, w: Watch, latest: int, parent: Msg, replies: list[Msg]) -> None:
    """A thread of a post Haseeb gave feedback on: answer the newest reply from its author (or that @mentions him) that is
    newer than his latest message there; the others of the same stretch are context."""
    se, st = env.settings, env.store
    eligible = _feedback_eligible(env, w, parent, replies)
    if not eligible or _done(st, eligible[-1].ts):
        _mark_seen(env, w, latest, replies)
        return
    target = eligible[-1]
    if st.count_thread_items(fb.FOLLOWBACK, w.thread_ts) >= MAX_FOLLOWBACKS_PER_THREAD:
        _mark_seen(env, w, latest, replies)
        return
    if b.drafts <= 0 or b.day <= 0:
        b.incomplete = True
        return
    iid = fb.followback_id(target.ts)
    thread_author = fb.safe_author(parent.user_name or parent.user_id)
    link = permalink(se.workspace_url, se.channel_id, target.ts, w.thread_ts)
    outcome = _attempt(env, target, iid, w.thread_ts, thread_author, link,
                       lambda mine: _draft_followback(env, w, parent, replies, eligible, target, mine))
    _settle(env, b, w, latest, replies, outcome)


def _draft_followback(env: Env, w: Watch, parent: Msg, replies: list[Msg], eligible: list[Msg], target: Msg,
                      mine: str) -> None:
    st, se = env.store, env.settings
    iid = fb.followback_id(target.ts)
    if st.get_item(iid) is not None:  # another run finished it after the caller looked
        raise fb._AlreadyDrafted(target.ts)
    _eyes(env, target)  # 👀 on the reply, before anything slow
    contents: dict[str, FileContent] = {}
    material = _material(env, [target], contents)  # the reply being answered: what it says and what its files say
    older = _context([r for r in eligible if r is not target])  # the rest of the stretch: context only, never reviewed
    # the review looks at the links and the attachment of the reply being answered, and at nothing else: "oops, here is the
    # right build" must not be checked against the build it replaces
    review = fb.looked_at(st, iid, lambda: site_service.review_post(
        target.text or "", target.files, read_html=fb._html_reader(env.slack, contents), llm=env.llm, store=st, settings=se,
        now=env.now, item_id=iid, renderers=fb.site_renderers(), claims=True))  # (made once: a retry reuses it)
    verified = review.statuses.count("verified")
    partly = "partly" in review.statuses  # holds on the first screen, which is all that was looked at
    # nothing was reviewed (no site, a failed review, no claim read): nothing in the reply was checked, and the draft says so,
    # unless the reply only asks a question
    need = partly or "not_checked" in review.statuses or (not review.statuses and not _only_questions(target.text or ""))
    thread_author = fb.safe_author(parent.user_name or parent.user_id)
    original = fb._defuse(parent.text or "")[:ORIGINAL_CHARS]
    earlier = _haseeb_words(replies, se)
    user = followback_user(thread_author, original, earlier, material, review.notes, st.prefs(fb.MAX_PREFS), context=older)
    sources = (material, older, review.notes or "", original, earlier)  # where a number may come from
    people = (material, older, original, earlier)  # where an address may come from: never the notes, which a page's text can reach
    with events.step(st, JOB, "write_draft") as ev:
        draft, wrote = fb._write(env.llm, se, FOLLOWBACK_SYSTEM, user, valid=lambda d: valid_followback(
            d, need_not_checked=need, verified=verified, partly=partly, sources=sources, links_from=people))
        ev.model = wrote
    _check_still_ours(env, iid, target, mine)
    st.set_cursor(f"feedback_src:{iid}", material)  # for `revise`
    st.set_cursor(f"feedback_author:{iid}", thread_author)
    replier = fb.safe_author(target.user_name or target.user_id)
    st.set_cursor(f"{CTX_PREFIX}{iid}", json.dumps({"orig": original, "mine": earlier, "older": older, "need": need,
                                                    "verified": verified, "partly": partly, "replier": replier,
                                                    "reply": (target.text or "")[:REPLY_KEPT]}))
    if review.line:
        st.set_cursor(f"feedback_review:{iid}", review.line)
    if review.notes:
        st.set_cursor(f"feedback_notes:{iid}", review.notes)
    link = permalink(se.workspace_url, se.channel_id, target.ts, w.thread_ts)
    card = render_followback_dm(thread_author, replier, link, target.text or "", draft, model_line(wrote, None, None, None),
                                review_line=review.line)
    _deliver_card(env, iid, w.thread_ts, f"Follow-back · {thread_author}'s thread", draft, wrote, card, target,
                  f"\U0001f4ac Follow-back draft: {thread_author}'s thread")


# ---------------------------------------------------------------------------------------------------------------- insight threads

def _insight_thread(env: Env, b: Budget, w: Watch, latest: int, parent: Msg, replies: list[Msg]) -> None:
    """The thread of an insight Haseeb posted: each teammate's reply newer than his latest message there is sorted, and
    drafted for when it is a post of their own (a normal feedback draft) or a comment on the insight (a short reply)."""
    se, st = env.settings, env.store
    waiting = False
    for reply in _insight_eligible(env, w, replies):
        if _done(st, reply.ts):
            continue
        if _full(st, w):  # no draft of any kind is possible: the reply is not even sorted
            _finish(st, reply.ts, "capped")
            continue
        kind = st.get_cursor(f"{CLASS_PREFIX}{reply.ts}")
        if kind not in CLASSES:
            if _quiet(reply):
                kind = "other"
            else:
                try:
                    kind = _classify(env, w, reply)
                except LLMError as e:
                    if e.infra:
                        raise _Stop from None
                    fails = _count_failure(st, reply.ts)
                    log.warning("followback: reply %s could not be sorted (attempt %d/%d)", reply.ts, fails, fb.MAX_FAILS)
                    if fails >= fb.MAX_FAILS:
                        _finish(st, reply.ts, "unsorted")  # quietly: sorting is not a draft, nothing was promised
                    else:
                        waiting = True
                    continue
            st.set_cursor(f"{CLASS_PREFIX}{reply.ts}", kind)
        if kind == "other":
            _finish(st, reply.ts, "other")
            continue
        if b.drafts <= 0 or b.day <= 0:
            b.incomplete = waiting = True
            continue
        outcome = _own_post(env, w, reply) if kind == "own_post" else _about_insight(env, w, parent, reply)
        if outcome in SPENDS:
            b.delivered += outcome == "delivered"
            b.drafts -= 1
            b.day -= 1
        if outcome in RETRIES:
            b.incomplete = waiting = True
    if not waiting:
        _mark_seen(env, w, latest, replies)


def _count_failure(store: Store, ts: str) -> int:
    fails = int(store.get_cursor(f"{FAIL_PREFIX}{ts}") or 0) + 1
    store.set_cursor(f"{FAIL_PREFIX}{ts}", str(fails))
    return fails


def _classify(env: Env, w: Watch, reply: Msg) -> str:
    """own_post, about_insight or other: the `summarize` chain's first usable answer. LLMError when there is none."""
    head = insight_gist(w.item)
    user = classify_user(w.item.title if w.item is not None else "", head, reply.text or "", files=len(reply.files),
                         links=len(find_links(reply.text or "")))
    after: str | None = None
    for _ in range(max(1, len(env.settings.models.get("summarize", [])))):
        try:
            c = env.llm.complete("summarize", CLASSIFY_SYSTEM, user, max_tokens=300, json_mode=True, after=after)
        except LLMError as e:
            if after is None:
                raise
            raise LLMError(str(e), infra=False) from None  # an earlier model answered, but badly
        try:
            return parse_class(c.text)
        except ValueError:
            log.warning("followback: %s gave an unusable sorting; trying the next model", c.model)
            after = c.model
    raise LLMError("no model sorted the reply")


def _own_post(env: Env, w: Watch, reply: Msg) -> str:
    """A post of their own in the thread of his insight: the NORMAL feedback pipeline, with its limits (not Lion's unless
    included, long enough or with a file, 2 drafts a teammate a day), plus the repost note at the top."""
    se, st = env.settings, env.store
    text = (reply.text or "").strip()
    if (reply.user_id == se.lion_id and not se.feedback_include_lion) or not (len(text) >= fb.MIN_POST_CHARS or reply.files):
        _finish(st, reply.ts, "not a post")
        return "done"
    per_author = int(st.get_cursor(fb._author_key(se, reply)) or 0)
    if per_author >= fb.MAX_PER_AUTHOR_DAY:
        _finish(st, reply.ts, "capped")
        log.info("followback: %s is past the %d-per-author-per-day cap, no draft", reply.ts, fb.MAX_PER_AUTHOR_DAY)
        return "done"
    iid = fb.threadfb_id(reply.ts)
    link = permalink(se.workspace_url, se.channel_id, reply.ts, w.thread_ts)
    where = fb.Where(iid, w.thread_ts, link, lead=fb.REPOST_LEAD, mention=reply.user_id,
                     banner="\U0001f4ac Feedback draft: an update that landed in your insight's thread")
    outcome = _attempt(env, reply, iid, w.thread_ts, fb.safe_author(reply.user_name or reply.user_id), link,
                       lambda mine: fb._deliver(env.slack, env.llm, st, se, reply, env.now, where), claimed=True)
    if outcome == "delivered":
        st.set_cursor(fb._author_key(se, reply), str(per_author + 1))
    return outcome


def _about_insight(env: Env, w: Watch, parent: Msg, reply: Msg) -> str:
    """A comment or a question on his insight: a short reply from the insight's own text, at most 3 a thread."""
    se, st = env.settings, env.store
    iid = fb.insreply_id(reply.ts)
    name = fb.safe_author(reply.user_name or reply.user_id)
    link = permalink(se.workspace_url, se.channel_id, reply.ts, w.thread_ts)
    return _attempt(env, reply, iid, w.thread_ts, name, link, lambda mine: _draft_insreply(env, w, reply, name, link, mine))


def _draft_insreply(env: Env, w: Watch, reply: Msg, name: str, link: str, mine: str) -> None:
    st, se = env.store, env.settings
    iid = fb.insreply_id(reply.ts)
    if st.get_item(iid) is not None:
        raise fb._AlreadyDrafted(reply.ts)
    _eyes(env, reply)
    insight = fb._defuse(insight_text(w.item))
    said = fb._defuse((reply.text or "").strip())
    if reply.files:
        said += f"\n[{len(reply.files)} file(s) attached]"
    if not said.strip():
        raise fb._NothingToReview()
    user = insreply_user(name, insight, f"{name}: {said}", st.prefs(fb.MAX_PREFS))
    with events.step(st, JOB, "write_draft") as ev:
        draft, wrote = fb._write(env.llm, se, INSREPLY_SYSTEM, user, valid=lambda d: valid_insreply(d, sources=(insight, said)))
        ev.model = wrote
    _check_still_ours(env, iid, reply, mine)
    st.set_cursor(f"feedback_src:{iid}", said)
    st.set_cursor(f"feedback_author:{iid}", name)
    st.set_cursor(f"{CTX_PREFIX}{iid}", json.dumps({"insight": insight}))
    st.set_cursor(f"thread_mention:{iid}", reply.user_id)  # `post` mentions them: it is Haseeb's thread, not theirs
    card = render_insreply_dm(name, link, reply.text or "", draft, model_line(wrote, None, None, None))
    _deliver_card(env, iid, w.thread_ts, f"Reply on your insight · {name}", draft, wrote, card, reply,
                  f"\U0001f4ac Reply draft: {name} commented on your insight")


# ---------------------------------------------------------------------------------------------------------------- shared delivery

def _eyes(env: Env, reply: Msg) -> None:
    """👀 on the teammate's reply when drafting for it starts (never on Haseeb's, Lion's or a bot's)."""
    if fb._reacts_to(env.settings, reply):
        fb._react(env.slack, env.store, env.settings, "eyes", reply.ts)
    else:
        fb._no_reactions(env.store, reply.ts)


def _material(env: Env, replies: list[Msg], contents: dict[str, FileContent]) -> str:
    """What the writer is given of the reply (or replies): who said what, and the text of the files that are not HTML (an
    attached page is the site review's business). fb._NothingToReview when there is nothing."""
    parts: list[str] = []
    refs: list[FileRef] = []
    said = False
    for r in replies:
        who = fb.safe_author(r.user_name or r.user_id)
        text = (r.text or "").strip()
        if text:
            parts.append(f"{who}: {text}")
            said = True
        for f in r.files:
            if is_html_file(f):
                parts.append(f"[attached HTML file: {file_label(f.name)}; the reviewer looks at it separately]")
                said = True
            else:
                refs.append(f)
    files = fb.read_files(env.slack, refs, contents=contents)
    parts += [f"[file: {name}]\n{t}" for name, t in files]
    if not said and not any(not fb.is_placeholder(t) for _, t in files):
        if any(t == fb.UNREADABLE for _, t in files):
            raise fb._FilesUnavailable(fb.UNREADABLE)  # may work later: a failed try, not a verdict
        raise fb._NothingToReview()
    material = fb._defuse("\n\n".join(p for p in parts if p))
    return fb._summarize(env.llm, material) if len(material) > fb.SUMMARIZE_OVER else material


CONTEXT_EACH = 500   # one older reply, in the context given to the writer
REPLY_KEPT = 600     # the reply that was answered, kept for the card of a redraft (the card quotes the first 160 of it)


def _context(replies: list[Msg]) -> str:
    """The older replies of the stretch, newest first, one line each: who said what, and a file by its name only (its text is
    not read for the writer and its page is not reviewed). The writer's input cuts this at CONTEXT_CHARS from the end, so the
    oldest go first."""
    lines: list[str] = []
    for r in reversed(replies):
        said = " ".join((r.text or "").split())[:CONTEXT_EACH]
        names = ", ".join(file_label(f.name) for f in r.files[:3])
        bits = [b for b in (said, f"[attached: {names}]" if names else "") if b]
        if bits:
            lines.append(f"{fb.safe_author(r.user_name or r.user_id)}: {' '.join(bits)}")
    return fb._defuse("\n".join(lines))[:CONTEXT_CHARS]


def _check_still_ours(env: Env, iid: str, reply: Msg, mine: str) -> None:
    """Minutes of work later, once more right before anything is sent: still no item, and the claim still this run's."""
    if env.store.get_item(iid) is not None:
        raise fb._AlreadyDrafted(reply.ts)
    if env.store.get_cursor(fb.claim_key(reply.ts)) != mine:
        raise fb._Claimed(reply.ts)


def _deliver_card(env: Env, iid: str, thread_ts: str, title: str, draft: str, wrote: str, card: str, reply: Msg,
                  banner: str) -> None:
    """The DM, then the item (insert-only), as `_draft_and_send` does for a post."""
    st, se = env.store, env.settings
    with events.step(st, JOB, "deliver"):
        dm_ts = send_dm(env.slack, st, se, card)
    created = st.insert_item(Item(
        id=iid, kind="feedback", day=env.now.astimezone(se.tz).date(), status="delivered", body=draft, title=title,
        dm_ts=dm_ts, source_ts=thread_ts, score=None, models={"wrote": wrote}, attempts=0, edited=False,
        created_at=env.now, acted_at=None))
    if not created:
        log.warning("followback: %s was drafted twice; the second DM has no item and the first item is untouched", iid)
        st.record_run(JOB, False, f"{iid}: drafted twice, the existing item was kept")
        raise fb._AlreadyDrafted(reply.ts)
    st.record_run(JOB, True, f"sent {iid} ({wrote})")
    show_banner(se, "feedback", banner)  # the sanitized names only


def _attempt(env: Env, reply: Msg, iid: str, thread_ts: str, who: str, link: str, run: Callable[[str], None], *,
             claimed: bool = False) -> str:
    """One delivery for `reply`, under its claim (`claimed`: `run` takes the claim itself), and how it ended:
    "delivered"; "done" (nothing more to do for it); "pending" (another run holds it: tried again later); "failed" (the
    attempt failed and is counted, in the run's and the day's allowance: tried again on a later run); "gave_up" (the third
    failure: finished, and its notice waits for the end of the run). _Stop when nothing else will work this run (paused, Slack
    or the models down)."""
    st, se = env.store, env.settings
    try:
        if claimed:
            run("")
        else:
            claim, mine = fb.claim_key(reply.ts), env.now.isoformat()
            if not st.claim_cursor(claim, mine, env.now - fb.CLAIM_TTL):
                raise fb._Claimed(reply.ts)
            try:
                run(mine)
            finally:
                try:
                    st.release_cursor(claim, mine)
                except Exception as e:  # noqa: BLE001 - the claim goes stale by itself
                    log.warning("followback: could not release the claim on %s (%s)", reply.ts, type(e).__name__)
        return "delivered"
    except OfflineError:
        raise
    except fb._Claimed:
        log.info("followback: %s is being drafted by another run, left pending", reply.ts)
        return "pending"
    except fb._AlreadyDrafted:
        _finish(st, reply.ts, "drafted")
        return "done"
    except fb._NothingToReview:
        _finish(st, reply.ts, "empty")
        log.info("followback: %s has no readable text or files, skipped", reply.ts)
        return "done"
    except SlackPaused:
        raise _Stop from None
    except SlackError as e:
        log.warning("followback: DM for %s failed, retrying next run: %s", reply.ts, e)
        if dm_send_failed(st, f"threadwatch:{reply.ts}", e):  # I4: the send hit its cap
            _abandon(env, iid, thread_ts, who)
            _finish(st, reply.ts, "dm failed")
        raise _Stop from None
    except Exception as e:  # noqa: BLE001 - a model failure or a bad reply must not wedge the scan
        if isinstance(e, LLMError) and e.infra:  # I8: unreachable, not wrong: not counted
            log.warning("followback: models unreachable for %s, retried next run: %s", reply.ts, e)
            st.record_run(JOB, False, f"{iid}: models unreachable, not counted")
            raise _Stop from None
        fails = _count_failure(st, reply.ts)
        _count_day_failure(env)
        log.warning("followback: attempt %d/%d on %s failed: %s", fails, fb.MAX_FAILS, reply.ts, fb._err(e))
        st.record_run(JOB, False, f"{iid}: {fb._err(e)}")
        if fails < fb.MAX_FAILS:
            return "failed"
        _finish(st, reply.ts, "gave up")
        st.set_cursor(f"{NOTICE_PREFIX}{reply.ts}", f"{who}|{link}")  # told once, at the end of the run, with the others
        return "gave_up"


def _count_day_failure(env: Env) -> None:
    key = f"{FAILED_DAY_PREFIX}{env.now.astimezone(env.settings.tz).date().isoformat()}"
    try:
        env.store.set_cursor(key, str(failed_today(env.store, env.settings, env.now) + 1))
    except Exception as e:  # noqa: BLE001 - a count must not fail the run
        log.warning("followback: could not count a failed attempt (%s)", type(e).__name__)


def _tell_gave_up(env: Env) -> None:
    """One line in the DM for every reply the run gave up on (a reply the model could not draft three times): at most
    MAX_NOTICES_PER_DAY lines a day, then the replies stay given up without one. A line that cannot be sent waits for the next
    run (until its send cap is hit)."""
    st, se = env.store, env.settings
    waiting = sorted(st.cursors(NOTICE_PREFIX).items())
    if not waiting:
        return
    day_key = f"{NOTICE_DAY_PREFIX}{env.now.astimezone(se.tz).date().isoformat()}"
    told = int(st.get_cursor(day_key) or 0)
    if told >= MAX_NOTICES_PER_DAY:
        log.warning("followback: %d reply(ies) given up on, past the %d lines a day: no line", len(waiting), MAX_NOTICES_PER_DAY)
        for key, _ in waiting:
            st.delete_cursor(key)
        return
    parts = [value.partition("|") for _, value in waiting]
    if len(parts) == 1:
        who, _, link = parts[0]
        text = f"⚠️ Couldn't draft a reply for {who}'s message in a thread · {link}"
    else:
        text = (f"⚠️ Couldn't draft replies for {len(parts)} messages in threads · "
                + " · ".join(link for _, _, link in parts[:3]) + (f" · and {len(parts) - 3} more" if len(parts) > 3 else ""))
    try:
        send_dm(env.slack, st, se, text)
    except SlackPaused:
        raise _Stop from None
    except SlackError as e:
        log.warning("followback: the give-up line was not sent: %s", e)
        if dm_send_failed(st, "threadwatch:notice", e):  # the send hit its cap: they stay given up, unannounced
            for key, _ in waiting:
                st.delete_cursor(key)
        return
    st.set_cursor(day_key, str(told + 1))
    for key, _ in waiting:
        st.delete_cursor(key)


def _abandon(env: Env, iid: str, thread_ts: str, who: str) -> None:
    """I4: the DM send hit its cap. A failed item makes the reply handled, so it is never drafted again."""
    env.store.insert_item(Item(
        id=iid, kind="feedback", day=env.now.astimezone(env.settings.tz).date(), status="failed", body="",
        title=f"Feedback: {who}", dm_ts=None, source_ts=thread_ts, score=None, models={}, attempts=0, edited=False,
        created_at=env.now, acted_at=None))
    env.store.record_run(JOB, False, f"{iid}: DM send failed, gave up")


def _settle(env: Env, b: Budget, w: Watch, latest: int, replies: list[Msg], outcome: str) -> None:
    """After the attempt on a follow-back: a delivery and a failed attempt both use the budget; only one that is to be tried
    again (or that another run holds) keeps the thread unseen."""
    if outcome in SPENDS:
        b.delivered += outcome == "delivered"
        b.drafts -= 1
        b.day -= 1
    if outcome in RETRIES:
        b.incomplete = True
    else:
        _mark_seen(env, w, latest, replies)


# ---------------------------------------------------------------------------------------------------------------- edit / shorter

def revise_reply(handlers, item: Item, instruction: str) -> Item:
    """Redraft a follow-back or a reply on his insight with Haseeb's instruction and the saved preferences, and send it in the
    item's DM thread. No model judges it. `handlers` is the FeedbackHandlers asking."""
    st, se = handlers.store, handlers.settings
    material = st.get_cursor(f"feedback_src:{item.id}")
    try:
        ctx = json.loads(st.get_cursor(f"{CTX_PREFIX}{item.id}") or "")
    except ValueError:
        ctx = None
    if not material or not isinstance(ctx, dict):
        raise ValueError("the original reply is no longer saved, so I can't redraft it; post or skip this one")
    author = fb.safe_author(st.get_cursor(f"feedback_author:{item.id}"))
    prefs = st.prefs(fb.MAX_PREFS)
    reply_ts = fb.reply_ts_of(item.id) or ""
    link = fb.card_link(se, item)
    if fb.variant_of(item.id) == "followback":
        notes = st.get_cursor(f"feedback_notes:{item.id}") or None
        need, verified, partly = bool(ctx.get("need")), int(ctx.get("verified") or 0), bool(ctx.get("partly"))
        older = str(ctx.get("older") or "")
        user = followback_user(author, str(ctx.get("orig") or ""), str(ctx.get("mine") or ""), material, notes, prefs,
                               context=older, current=item.body, instruction=instruction)
        orig, mine = str(ctx.get("orig") or ""), str(ctx.get("mine") or "")
        # the current draft is a source too: it passed these checks, or he approved it as it stood, so keeping what it says
        # (a number or an address that came from an earlier instruction of his) adds no fact
        sources = (material, older, notes or "", orig, mine, instruction, item.body)
        people = (material, older, orig, mine, instruction, item.body)
        draft, wrote = fb._write(handlers.llm, se, FOLLOWBACK_SYSTEM, user, valid=lambda d: valid_followback(
            d, need_not_checked=need, verified=verified, partly=partly, sources=sources, links_from=people))
        # the card quotes the reply that was answered and names who wrote it (not the thread's author, not the first reply)
        card = render_followback_dm(author, str(ctx.get("replier") or author), link,
                                    str(ctx["reply"]) if "reply" in ctx else _first_reply_text(material), draft,
                                    model_line(wrote, None, None, None),
                                    review_line=st.get_cursor(f"feedback_review:{item.id}") or None)
    else:
        user = insreply_user(author, str(ctx.get("insight") or ""), f"{author}: {material}", prefs, current=item.body,
                             instruction=instruction)
        sources = (str(ctx.get("insight") or ""), material, instruction, item.body)  # (the current draft: see above)
        draft, wrote = fb._write(handlers.llm, se, INSREPLY_SYSTEM, user, valid=lambda d: valid_insreply(d, sources=sources))
        card = render_insreply_dm(author, link, material, draft, model_line(wrote, None, None, None))
    send_dm(handlers.slack, st, se, card, thread_ts=item.dm_ts)
    fresh = st.get_item(item.id) or item
    st.mark(item.id, fresh.status, body=draft, models={**fresh.models, "wrote": wrote}, edited=True)
    log.info("followback: %s redrafted", reply_ts)
    return st.get_item(item.id) or item


def _first_reply_text(material: str) -> str:
    """The reply text again for the card of a redraft: the saved material without its "name: " lead."""
    return material.split("\n\n", 1)[0].split(": ", 1)[-1]
