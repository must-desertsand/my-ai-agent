"""R44: a PDF deck for each delivered insight, uploaded into the insight's DM thread after the text.

Decks never delay or block text delivery: they run only after `deliver_pending` (the tick: one deck per tick; the
`insights` command: all of them, after the insights lock is released). Each item's deck has a state in the cursor
`deck:{item_id}` (JSON): the hash of the insight text it was built from, the attempts so far, the PDF and the cover
method ("codex" or "template"). An attempt is counted before it starts, so a crash or the tick watchdog can't make it
run forever: the first attempt plus at most 2 retries on later ticks, then one "gave up" log line and nothing more.
A build that worked is kept, so a retry after a failed upload only uploads. A revised insight (edit, shorter) has a
new hash, so it gets a fresh deck on the next run, reusing the Codex cover.

Two Slack writes, both `upload_pdf`: the deck into Haseeb's self-DM (a failure may retry, R26: a duplicate in his own
DM is cosmetic), and, for an insight Haseeb has posted (status `posted`, `post_ts:{id}` set by
`InsightHandlers.post`), the deck for exactly the posted text into the channel post's thread, in the channel the post went to
(`channels.post_channel`: the one recorded before the post was sent, the leadership channel for a post made before the choice
existed). The attach runs here, after the post is recorded, never inside `post` (fix round 1): a crash during it cannot leave
the post looking interrupted. It is attempted once: `attach:{id}` is written before the call and never retried (R16); on failure
Haseeb gets "⚠️ Deck didn't attach — the text is posted" in the DM. A post made before its deck finished gets the
deck when it does.

The pause (the dashboard's) is not a failure. The gateway refuses a write with `SlackPaused` before it makes any call,
so nothing was uploaded: an attach puts `attach:{id}` back as it was, so the deck can still be attached after Resume,
and a deck's DM upload gives its try back instead of using one of the three.

Nor is any other failure that guarantees nothing was sent. An upload has three steps (get the upload URL, POST the
bytes, share the file), and only the last, `slack_complete_file_upload`, names a conversation: a failure of the first
two, of whatever kind (the gateway raises `SlackNotShared`), cannot have shared the file; and the sharing call's own
`SlackUnavailable` and `SlackNotCalled` mean no call was made. An attach that fails like that leaves `attach:{id}` at
`late` (not `failed`, and not unset, which would make the next tick try again by itself every minute): `attach`, in the DM
or on the dashboard, works again, and he is told so. A timeout, a tool-call mismatch or a tool error of the sharing call
may have shared the file, so those stay `failed` (R16).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path

from .. import channels, events
from ..config import Settings
from ..deck import codexcheck
from ..deck.codex import codex_cover, cover_topic, resolve_codex
from ..deck.designer import design_deck
from ..deck.render import build_deck
from ..models import Item
from ..commands import send_dm
from ..notify import notify
from ..render import MARKET_LABELS, Idea, Insight
from ..slack_gateway import SlackGateway, SlackNotCalled, SlackNotShared, SlackPaused, SlackUnavailable
from ..store import Store

log = logging.getLogger(__name__)

JOB = "decks"

DECK_COMMENT = "📎 Deck"
TEST_COMMENT = "📎 Deck (designer v2)"  # R49: `haseeb-agent deck-test <item-id>`
ATTACH_FAILED = "⚠️ Deck didn't attach — the text is posted"
ATTACH_NOT_SENT_TEXT = "Deck didn't attach (nothing was sent). Use Attach, or reply attach, to try again"
ATTACH_NOT_SENT = "⚠️ " + ATTACH_NOT_SENT_TEXT
# a failure that guarantees nothing reached the thread: the run made no Slack call, or the upload stopped before the step
# that shares the file (SlackPaused, a SlackUnavailable, is handled before this, as it always was)
NOT_SENT = (SlackUnavailable, SlackNotCalled, SlackNotShared)
LATE_NOTE = "📎 Deck finished after you posted — not attached; reply `attach` to add it"  # R50
_wall = time.time  # R50: when a DM upload finished, on the clock Slack's ts values use
MAX_TRIES = 3  # the first attempt plus 2 retries on later ticks
ATTACH_DAYS = 2  # a posted insight from today or yesterday can still get its deck attached
KEEP_DAYS = 30   # deck folders older than this are deleted
COVER = "codex-cover.png"


def decks_root(settings: Settings) -> Path:
    return Path(settings.db_path).parent / "decks"


def content_hash(insight: dict) -> str:
    return hashlib.sha256(json.dumps(insight, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _key(item_id: str) -> str:
    return f"deck:{item_id}"


def _state(store: Store, item_id: str) -> dict:
    try:
        st = json.loads(store.get_cursor(_key(item_id)) or "{}")
    except ValueError:
        return {}
    return st if isinstance(st, dict) else {}


def _insight_meta(item: Item) -> dict | None:
    from .insights import _meta  # late import: insights imports this module for the post attach
    try:
        return _meta(item)
    except ValueError:
        return None


def current_deck(store: Store, settings: Settings, item: Item, root: Path | None = None) -> Path | None:
    """The deck uploaded for exactly the item's current text, if its file still exists; else None."""
    meta = _insight_meta(item)
    st = _state(store, item.id)
    if meta is None or st.get("state") != "done" or st.get("sha") != content_hash(meta["insight"]):
        return None
    pdf = Path(str(st.get("pdf") or ""))
    base = Path(root or decks_root(settings)).resolve()
    if not pdf.is_file() or not pdf.resolve().is_relative_to(base):
        return None
    return pdf


def default_cover_maker(settings: Settings) -> Callable[[str, Path], Path | None]:
    def make(topic: str, out: Path) -> Path | None:
        codex = resolve_codex(settings.codex_path)
        if codex is None:
            log.info("deck: codex not found, using the template cover")
            return None
        return codex_cover(topic, out, codex=codex, version=settings.codex_version)
    return make


def default_designer(settings: Settings):
    """(ins, day, out_pdf, *, deck_id) -> DesignResult when designer decks are on and Codex exists; else None."""
    if not settings.deck_designer:
        return None
    codex = resolve_codex(settings.codex_path)
    if codex is None:
        log.info("deck: codex not found, using the template deck")
        return None

    def design(ins: Insight | Idea, day, out_pdf: Path, *, deck_id: str, keep: bool = False):
        return design_deck(ins, day, out_pdf, deck_id=deck_id, codex=codex, models=settings.deck_models,
                           effort=settings.deck_effort, chrome=settings.chrome_path,
                           codex_version=settings.codex_version, keep=keep)
    return design


CODEX_VERSION_ALERT = "⚠️ Codex version changed — designer decks paused, run smoke"


def _codex_version_alert(store: Store, settings: Settings, today) -> None:
    """The review of 466e622: a Codex other than the pinned one runs no deck or cover (codexcheck refuses it); this
    tells Haseeb, at most once a day."""
    codex = resolve_codex(settings.codex_path)
    if codex is None or codexcheck.version_ok(codex, settings.codex_version):
        return
    key = f"codex_version_alert:{today.isoformat()}"
    if store.get_cursor(key):
        return
    if notify(settings, "alert", CODEX_VERSION_ALERT, sound=True):
        store.set_cursor(key, "1")


def _cover_label(draft: Insight | Idea) -> str:
    """What the cover's topic puts before the title: an idea's market label, an insight's category."""
    return MARKET_LABELS.get(draft.market, "") if isinstance(draft, Idea) else draft.category


def _build(ins: Insight | Idea, item: Item, out_dir: Path, builder, cover_maker, designer=None,
           store: Store | None = None) -> tuple[Path, str]:
    """(the PDF, its model label). With a designer: its deck ("codex <model>", "codex <model> (repaired)" after the
    R49 repair turn), else the template deck without a
    second Codex run ("template (fact-check)" when the fact check refused the designer's deck, else "template").
    With a `store`, the design and render steps are recorded as events."""
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{item.id}.pdf"  # the upload's file name
    cover = out_dir / COVER
    if designer is not None:
        with events.step(store, JOB, "design") as ev:
            res = designer(ins, item.day, out_dir / "designer.pdf", deck_id=item.id)
            ev.model = getattr(res, "model", None)
        if res.pdf is not None:
            Path(res.pdf).replace(final)
            log.info("deck: %s designed by codex %s in %.0f s: %d pages, %d images", item.id, res.model,
                     res.seconds, res.pages, res.images)
            return final.resolve(), f"codex {res.model}" + (" (repaired)" if res.repaired else "")
        label = "template (fact-check)" if res.reason == "fact-check" else "template"
        log.info("deck: %s uses the template deck (designer: %s)", item.id, res.reason)
        cover = cover if cover.is_file() else None
    else:
        if not cover.is_file():
            with events.step(store, JOB, "design"):
                made = cover_maker(cover_topic(ins.title, _cover_label(ins)), cover)
            cover = Path(made) if made is not None else None
        label = "codex" if cover is not None else "template"
    with events.step(store, JOB, "render"):
        pdf = builder(ins, item.day, out_dir / "build", cover=cover)
    Path(pdf).replace(final)
    shutil.rmtree(out_dir / "build", ignore_errors=True)  # the HTML and the cover's copy; codex-cover.png stays for a revision
    return final.resolve(), label


def _prune(root: Path, now: datetime) -> None:
    """Delete deck folders not touched in KEEP_DAYS days."""
    cutoff = now.timestamp() - KEEP_DAYS * 86400
    try:
        old = [d for d in root.iterdir() if d.is_dir() and d.stat().st_mtime < cutoff]
    except OSError:
        return
    for d in old:
        shutil.rmtree(d, ignore_errors=True)
    if old:
        log.info("deck: pruned %d deck folder(s) older than %d days", len(old), KEEP_DAYS)


def _previous(st: dict) -> dict | None:
    """The deck made for an older text of the insight, as far as it can still be shown while the deck for the newer
    text is made: a finished one (its PDF, model and text hash), or the one an earlier revision already carried. A build
    for the newer text writes to the same file name, so once one has been built (`built`) the old file is gone."""
    if st.get("state") == "done" and isinstance(st.get("pdf"), str) and st["pdf"]:
        return {k: st[k] for k in ("sha", "pdf", "model") if k in st}
    prev = st.get("prev")
    return prev if isinstance(prev, dict) and not st.get("built") else None


def _dm_work(store: Store, item: Item) -> tuple[dict, str, dict] | None:
    """(meta, text hash, state) when this item's DM deck still needs an attempt; None otherwise. The state of a
    revised text starts fresh, and carries the deck of the older text as `prev` (the dashboard shows it, as the one
    for the previous text, until the new one is in the DM)."""
    if item.status not in ("delivered", "posted") or not item.dm_ts:
        return None
    meta = _insight_meta(item)
    if meta is None:
        return None
    sha = content_hash(meta["insight"])
    st = _state(store, item.id)
    if st.get("sha") != sha:
        prev = _previous(st)
        st = {"sha": sha, "tries": 0, "state": "pending"}  # new or revised text: a fresh deck
        if prev:
            st["prev"] = prev
    if st.get("state") in ("done", "gave_up"):
        return None
    return meta, sha, st


def _attach_due(store: Store, settings: Settings, item: Item, root: Path) -> bool:
    return (item.status == "posted" and not store.get_cursor(f"attach:{item.id}")
            and bool(store.get_cursor(f"post_ts:{item.id}")) and current_deck(store, settings, item, root) is not None)


def decks_pending(store: Store, settings: Settings, now: datetime, root: Path | None = None) -> bool:
    """Cheap check for the tick: is there a DM deck to build or upload, or a deck to attach to a post?"""
    if not settings.decks_enabled:
        return False
    root = Path(root or decks_root(settings))
    today = now.astimezone(settings.tz).date()
    if any(_dm_work(store, it) is not None for it in store.items_for_day("insight", today)):
        return True
    return any(_attach_due(store, settings, it, root) for it in store.recent_items("insight", ATTACH_DAYS, today=today))


def retry_given_up(store: Store, settings: Settings, now: datetime) -> int:
    """The dashboard's "Build decks": give each of today's decks that gave up (the attempts used, or the state
    `gave_up`) exactly one more try: its tries go back to the last one, so the run that follows makes one attempt, and
    if that fails the deck gives up again at once instead of the tick spending two more tries on its own minutes later
    (each click is one attempt, which is what "another try" says). A deck that was built keeps its PDF, so the retry may
    only upload it. Returns how many were reset. `deliver_decks` then does the work, for these and for the decks that are
    simply missing."""
    today = now.astimezone(settings.tz).date()
    reset = 0
    for item in store.items_for_day("insight", today):
        if item.status not in ("delivered", "posted") or not item.dm_ts:
            continue
        st = _state(store, item.id)
        if st.get("state") == "done" or not (st.get("state") == "gave_up" or int(st.get("tries", 0) or 0) >= MAX_TRIES):
            continue
        st.update(tries=MAX_TRIES - 1, state="pending")  # one attempt left
        store.set_cursor(_key(item.id), json.dumps(st))
        reset += 1
    return reset


_DEFAULT = object()


def deliver_decks(slack: SlackGateway, store: Store, settings: Settings, now: datetime, *,
                  builder=None, cover_maker=None, designer=_DEFAULT, out_root: Path | None = None,
                  limit: int | None = None) -> int:
    """Build and upload the decks of today's delivered insights. Returns how many were uploaded. Never raises an
    Exception for one item (a BaseException, such as the tick watchdog's, still propagates)."""
    if not settings.decks_enabled:
        return 0
    from .insights import draft_from  # late import, see _insight_meta
    builder = builder or partial(build_deck, chrome=settings.chrome_path)
    uses_codex = cover_maker is None or designer is _DEFAULT  # production defaults (tests inject their own)
    cover_maker = cover_maker or default_cover_maker(settings)
    designer = default_designer(settings) if designer is _DEFAULT else designer
    root = Path(out_root or decks_root(settings))
    _prune(root, now)
    today = now.astimezone(settings.tz).date()  # R4
    done = attempted = 0
    paused = False
    for item in store.items_for_day("insight", today):
        work = _dm_work(store, item)
        if work is None:
            continue
        meta, sha, st = work
        if int(st.get("tries", 0)) >= MAX_TRIES:  # the last attempts died without a result (a crash, the watchdog)
            st["state"] = "gave_up"
            store.set_cursor(_key(item.id), json.dumps(st))
            log.warning("deck: %s gave up after %d tries (interrupted)", item.id, st["tries"])
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1
        st["tries"] = int(st.get("tries", 0)) + 1
        store.set_cursor(_key(item.id), json.dumps(st))  # counted first: a crash can't make it retry forever
        try:
            pdf = Path(str(st.get("pdf") or ""))
            if st.get("built") != sha or not pdf.is_file():
                if uses_codex:
                    uses_codex = False
                    _codex_version_alert(store, settings, today)
                pdf, model = _build(draft_from(meta), item, root / item.id, builder, cover_maker, designer, store)
                st.update(built=sha, pdf=str(pdf), model=model)
                store.set_cursor(_key(item.id), json.dumps(st))
            with events.step(store, JOB, "upload"):
                slack.upload_pdf(settings.self_dm_channel, pdf, item.title, item.dm_ts, DECK_COMMENT)
            st["dm_at"] = f"{_wall():.6f}"  # R50: Haseeb could see this deck from now on
        except SlackPaused:
            # The upload was refused before any call (Pause): not an attempt, so the try is given back. The PDF built
            # so far is kept; every further write is refused too, so the run ends here.
            st["tries"] = max(0, int(st["tries"]) - 1)
            store.set_cursor(_key(item.id), json.dumps(st))
            log.info("deck: %s waits for Resume (the agent is paused)", item.id)
            paused = True
            break
        except Exception as e:  # noqa: BLE001 - a deck never fails the item (R44)
            reason = f"{type(e).__name__}: {' '.join(str(e).split())[:160]}"
            if st["tries"] >= MAX_TRIES:
                st["state"] = "gave_up"
                log.warning("deck: %s gave up after %d tries: %s", item.id, st["tries"], reason)
            else:
                log.info("deck: %s attempt %d/%d failed, retrying on the next tick: %s", item.id, st["tries"],
                         MAX_TRIES, reason)
            store.set_cursor(_key(item.id), json.dumps(st))
            continue
        st["state"] = "done"
        st.pop("prev", None)  # the deck of the older text is no longer the one to show
        store.set_cursor(_key(item.id), json.dumps(st))
        store.set_model(item.id, "deck", st["model"])  # never touches status: a post made meanwhile stays posted
        done += 1
    if not paused and (attached := _attach_posted(slack, store, settings, today, root)):
        log.info("deck: attached %d deck(s) to channel posts", attached)
    return done


def _seen_before_post(store: Store, item: Item) -> bool:
    """R50: did the deck's DM upload finish before Haseeb's `post` command (or, for a post made without one, before
    the post itself)? Only then could he have seen the deck that would go to the channel."""
    cutoff = store.get_cursor(f"post_cmd_ts:{item.id}") or store.get_cursor(f"post_ts:{item.id}")
    dm_at = _state(store, item.id).get("dm_at")
    try:
        return bool(cutoff and dm_at) and Decimal(str(dm_at)) < Decimal(cutoff)
    except InvalidOperation:
        return False


def _attach_posted(slack: SlackGateway, store: Store, settings: Settings, today, root: Path) -> int:
    """Attach each finished deck to its channel post's thread, once (R16), when Haseeb could have seen it before he
    posted (R50); otherwise tell him in the DM thread once, and `attach` adds it on request. Returns how many were
    attached."""
    n = 0
    for item in store.recent_items("insight", ATTACH_DAYS, today=today):
        key = f"attach:{item.id}"
        if not _attach_due(store, settings, item, root):
            continue  # not posted, already tried, or no deck yet for the posted text (maybe on a later run)
        if not _seen_before_post(store, item):
            store.set_cursor(key, "late")  # never attached on its own; the note goes once
            try:
                send_dm(slack, store, settings, LATE_NOTE, thread_ts=item.dm_ts)
            except Exception as e:  # noqa: BLE001 - a note must never fail the run
                log.warning("deck: the late-deck note for %s was not sent: %s", item.id, type(e).__name__)
            log.info("deck: %s finished after the post; not attached (R50)", item.id)
            continue
        try:
            if _attach(slack, store, settings, item, root, notify=True) == "done":
                n += 1
        except SlackPaused:
            log.info("deck: the agent is paused; attaching waits for Resume")
            break  # the marker was put back, and every further upload would be refused too
    return n


def _attach(slack: SlackGateway, store: Store, settings: Settings, item: Item, root: Path, *, notify: bool) -> str:
    """One channel upload of the current deck into the post's thread. `attach:{id}` is written before the call and
    an upload that may have shared the file is never retried (R16). Returns the marker it leaves: `done` (it went out),
    `late` (nothing was sent, see NOT_SENT: he can ask for it again) or `failed`. Raises SlackPaused, with the marker
    put back, when the pause refused the upload before it began."""
    key = f"attach:{item.id}"
    top = store.get_cursor(f"post_ts:{item.id}")
    channel = channels.post_channel(store, settings, item.id)  # where the post is: the thread to attach to is in that channel
    deck = current_deck(store, settings, item, root)
    before = store.get_cursor(key)  # None, or "late" when the attach was held back
    store.set_cursor(key, "started")  # before the call: a crash or the watchdog can't make it run twice
    try:
        slack.upload_pdf(channel, deck, item.title, top, None)
    except SlackPaused:
        # Refused before the file was shared (Pause): nothing is in the thread, so this is no failed attempt. The marker
        # goes back to what it was and the deck can be attached later; the pause is the caller's to handle.
        if before is None:
            store.delete_cursor(key)
        else:
            store.set_cursor(key, before)
        raise
    except NOT_SENT as e:
        # Nothing is in the thread (no call was made, or the upload stopped before the step that shares the file), so
        # this is no attempt R16 has to count. The marker is `late`: `attach` works again, and the next tick does not
        # try by itself (that would be every minute for as long as Slack hiccups).
        store.set_cursor(key, "late")
        log.warning("deck: attach to the post of %s sent nothing (%s); `attach` tries again", item.id, type(e).__name__)
        if notify:
            try:
                send_dm(slack, store, settings, ATTACH_NOT_SENT, thread_ts=item.dm_ts)
            except Exception as e2:  # noqa: BLE001 - a note (or a store error) must never fail the run
                log.warning("deck: the attach-not-sent note for %s was not sent: %s", item.id, type(e2).__name__)
        return "late"
    except Exception as e:  # noqa: BLE001 - never retried; the text is posted
        store.set_cursor(key, "failed")
        log.warning("deck: attach to the post of %s failed (not retried): %s", item.id, type(e).__name__)
        if notify:
            try:
                send_dm(slack, store, settings, ATTACH_FAILED, thread_ts=item.dm_ts)
            except Exception as e2:  # noqa: BLE001 - a note (or a store error) must never fail the run
                log.warning("deck: the attach-failure note for %s was not sent: %s", item.id, type(e2).__name__)
        return "failed"
    store.set_cursor(key, "done")
    return "done"


def attach_now(slack: SlackGateway, store: Store, settings: Settings, item: Item, root: Path | None = None) -> str:
    """R50: Haseeb's `attach` reply. Uploads the deck he got in the DM for exactly the posted text into the channel
    post's thread, once. Returns the status reply; raises ValueError when there is nothing to attach (or when nothing
    was sent and he can ask again), and SlackPaused (nothing was uploaded, the deck can still be attached) when the
    pause refused it."""
    root = Path(root or decks_root(settings))
    if item.kind != "insight" or item.status != "posted" or not store.get_cursor(f"post_ts:{item.id}"):
        raise ValueError("attach works on a posted insight or idea")
    state = store.get_cursor(f"attach:{item.id}")
    if state == "done":
        return "📎 Deck already attached"
    if state in ("started", "failed"):
        raise ValueError("the deck attach was already tried; it is not retried")
    if current_deck(store, settings, item, root) is None:
        raise ValueError("no deck for the posted text has reached the DM yet")
    outcome = _attach(slack, store, settings, item, root, notify=False)
    if outcome == "late":
        raise ValueError(ATTACH_NOT_SENT_TEXT)  # nothing was sent: the marker is `late`, so `attach` can be tried again
    if outcome != "done":
        raise ValueError("the deck didn't attach — the text is posted")
    return "📎 Deck attached to the post"


def deck_test(slack: SlackGateway, store: Store, settings: Settings, item_id: str, *, designer=None,
              out_root: Path | None = None, keep: bool = False):
    """R49: `haseeb-agent deck-test <item-id>`. A designer deck for that insight whatever `deck_designer` says,
    uploaded into the item's self-DM thread with the same `upload_pdf` call production uses. (DesignResult,
    uploaded). Never writes to the channel and never changes the item: no status, model or deck-state write.
    `keep` (--keep): the designer's work dir with its copied images, and the PDF, stay for a look."""
    if settings.self_dm_channel == settings.channel_id:
        raise ValueError("the self-DM channel is the leadership channel; refusing")
    item = store.get_item(item_id)
    if item is None:
        raise ValueError(f"no item {item_id}")
    meta = _insight_meta(item)
    if meta is None or not item.dm_ts:
        raise ValueError(f"{item_id} is not a delivered insight")
    designer = designer or default_designer(replace(settings, deck_designer=True))
    if designer is None:
        raise ValueError("codex not found")
    from .insights import draft_from  # late import, see _insight_meta
    folder = Path(out_root or decks_root(settings)).resolve() / "deck-test" / item.id
    folder.mkdir(parents=True, exist_ok=True)
    res = designer(draft_from(meta), item.day, folder / f"{item.id}.pdf", deck_id=f"deck-test-{item.id}",
                   **({"keep": True} if keep else {}))
    if res.pdf is None:
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
        return res, False
    try:
        slack.upload_pdf(settings.self_dm_channel, Path(res.pdf), item.title, item.dm_ts, TEST_COMMENT)
    finally:
        if not keep:
            Path(res.pdf).unlink(missing_ok=True)  # M8: a test deck leaves nothing behind
            shutil.rmtree(folder, ignore_errors=True)
    return res, True
