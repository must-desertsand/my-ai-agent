"""Keeps a copy of the program channel's messages for the dashboard.

`capture` is called right after a job has read the channel (never the self-DM); it stores what the job already got
and makes no Slack call of its own. `classify` gives each message a kind by rules only.

D15: a channel page also shows how many replies a post has and when the newest came. When a huddle or insight post
shows a newer reply than the last one noted, `capture` puts its thread on a queue (a cursor, so database only). The
nightly refresh (jobs.history_import.run_thread_refresh) reads those threads. Only the pages the jobs read are seen.

Slack text is untrusted and private: nothing here logs it.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime

from .config import Settings
from .models import Msg, StoredMessage
from .store import Store

log = logging.getLogger(__name__)

NOTES_READY = "AI huddle notes are ready"
HUDDLE_STARTED = "A huddle started"
REFRESH_QUEUE = "history_refresh:threads"  # JSON list of parent ts whose new replies are not read yet, oldest first
REFRESH_SEEN = "history_refresh:seen:"  # + parent ts: the newest latest_reply (epoch seconds) queued or already read
REFRESH_CAP = 200  # the queue keeps the newest 200; a flood drops the oldest
# Leading noise before the words that matter: whitespace, emoji, Slack :shortcodes: and markdown marks.
_LEAD = r"(?:\s|:[a-z0-9_+\-]+:|[^\w\s])*+"  # possessive: no backtracking on hostile text
_TODO = re.compile(rf"^{_LEAD}next\s+todo\b", re.IGNORECASE)
_INSIGHT = re.compile(rf"^{_LEAD}(?:daily\s+insight|insight)\b", re.IGNORECASE)


def _top_level(m: Msg) -> bool:
    return m.thread_ts in (None, "", m.ts)


def _is_pdf(m: Msg) -> bool:
    return any((f.mimetype or "").lower() == "application/pdf" or (f.name or "").lower().endswith(".pdf")
               for f in m.files)


def _first_line(text: str) -> str:
    return next((ln for ln in (text or "").splitlines() if ln.strip()), "")


def classify(msg: Msg, parent: Msg | None, settings: Settings) -> str:
    """The message kind, first rule that matches wins:
    1. A Slackbot reply in a Slackbot thread that announces the AI huddle notes: huddle_notes.
    2. Starts with "Next todo" (after whitespace and emoji): todo.
    3. A top-level post with a PDF, or whose first line starts "Daily insight" or "Insight": insight.
    4. A reply to a post that rule 3 calls an insight: feedback.
    5. Anything else: other."""
    text = msg.text or ""
    if (parent is not None and parent.is_bot and msg.is_bot
            and (NOTES_READY in text or NOTES_READY in (parent.text or ""))):
        return "huddle_notes"
    if _TODO.match(text):
        return "todo"
    reply = parent is not None and msg.ts != parent.ts
    if _top_level(msg) and not reply and (_is_pdf(msg) or _INSIGHT.match(_first_line(text))):
        return "insight"
    if reply and classify(parent, None, settings) == "insight":
        return "feedback"
    return "other"


def wants_thread(m: Msg, settings: Settings) -> bool:
    """A huddle-bot post or an insight that has replies: the two kinds of thread whose replies the History keeps."""
    if m.reply_count <= 0:
        return False
    if m.is_bot and (HUDDLE_STARTED in (m.text or "") or NOTES_READY in (m.text or "")):
        return True
    return classify(m, None, settings) == "insight"


def capture(store: Store, settings: Settings, msgs: Iterable[Msg], *, source_channel: str, parent: Msg | None = None,
            queue_refresh: bool = True) -> int:
    """Store messages read from the program channel (`settings.channel_id`) and return how many are new or edited.

    M6: `source_channel` is the channel the caller read, and it has no default. A read of any other channel (the
    self-DM above all) is never stored: 0, and nothing is written.

    For a thread read pass the thread's parent: the replies are classified against it, and the parent is stored
    too. Idempotent: a message that has not changed is not written again, and a topic or a kind that a later
    pass set (anything but "other") is kept. A database error is logged (type only) and never reaches the job.

    D15: a huddle or insight post with a newer reply than the last one noted goes on the refresh queue, and a
    thread read in full marks its newest reply as noted. `queue_refresh=False` is for a caller that reads those
    threads itself (the import), so its pages queue nothing. Database only: no Slack call is ever made here."""
    if source_channel != settings.channel_id:
        log.warning("history: a read of another channel was not captured")
        return 0
    batch = list(msgs)
    if parent is not None:
        batch.insert(0, parent)
    new, failed = 0, False
    for m in batch:
        try:
            if _store(store, settings, m, None if parent is None or m is parent else parent):
                new += 1
        except Exception as e:  # noqa: BLE001 - capturing history must never fail or change a job
            failed = True
            log.warning("history: could not store message %s (%s)", getattr(m, "ts", "?"), type(e).__name__)
            continue
        if parent is None and queue_refresh:
            _noting(_note_replies, store, settings, m)
    if parent is not None and not failed:  # a reply that was not stored must stay news
        _noting(_note_read, store, settings, parent)
    return new


def _noting(note, store: Store, settings: Settings, m: Msg) -> None:
    try:
        note(store, settings, m)
    except Exception as e:  # noqa: BLE001 - the refresh queue must never fail or change a job
        log.warning("history: could not note the replies of %s (%s)", getattr(m, "ts", "?"), type(e).__name__)


def _secs(value: str | None) -> int | None:
    """Whole epoch seconds of a ts or latest_reply, the precision Slack shows a reply time at; None if unusable."""
    try:
        return int(float(value)) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _note_replies(store: Store, settings: Settings, m: Msg) -> None:
    latest = _secs(m.latest_reply)
    if latest is None or not _top_level(m) or not wants_thread(m, settings):
        return
    key = f"{REFRESH_SEEN}{m.ts}"
    seen = _secs(store.get_cursor(key))
    if seen is not None and latest <= seen:
        return
    enqueue_refresh(store, m.ts)
    store.set_cursor(key, str(latest))  # after the queue: a failure in between only queues the thread again


def _note_read(store: Store, settings: Settings, parent: Msg) -> None:
    latest = _secs(parent.latest_reply)
    if latest is None or not wants_thread(parent, settings):
        return
    key = f"{REFRESH_SEEN}{parent.ts}"
    seen = _secs(store.get_cursor(key))
    if seen is None or latest > seen:
        store.set_cursor(key, str(latest))


def _parse_queue(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [t for t in data if isinstance(t, str)] if isinstance(data, list) else []


def load_queue(store: Store, key: str) -> list[str]:
    """A queue of thread ts kept in a cursor as a JSON list. A missing or unreadable cursor is an empty queue."""
    return _parse_queue(store.get_cursor(key))


def enqueue_refresh(store: Store, ts: str) -> None:
    """Put a thread at the end of the refresh queue unless it is there; keep only the newest REFRESH_CAP."""
    def add(raw: str | None) -> str:
        queue = _parse_queue(raw)
        if ts not in queue:
            queue.append(ts)
        return json.dumps(queue[-REFRESH_CAP:])
    store.update_cursor(REFRESH_QUEUE, add)


def dequeue_refresh(store: Store, ts: str) -> None:
    """Take a thread off the refresh queue. It edits the queue as it is now, not as a caller read it earlier: the
    tick may have queued another thread while a Slack call was running."""
    store.update_cursor(REFRESH_QUEUE, lambda raw: json.dumps([t for t in _parse_queue(raw) if t != ts]))


def _store(store: Store, settings: Settings, m: Msg, parent: Msg | None) -> bool:
    channel = settings.channel_id
    kind = classify(m, parent, settings)
    old = store.get_message(channel, m.ts)
    if old is not None and kind == "other":
        kind = old.kind  # a later pass (or a person) relabelled it; the rules alone do not know better
    name = m.user_name or m.user_id
    day = datetime.fromtimestamp(float(m.ts), settings.tz).date()
    stored = StoredMessage(
        channel=channel, ts=m.ts, thread_ts=m.thread_ts or (parent.ts if parent else None),
        user_id=m.user_id, user_name=name, text=m.text or "", kind=kind, day=day, topic=old.topic if old else None,
    )
    if old is None or old.user_name != name:
        store.upsert_person(m.user_id, name)
    if old is not None and (old.text, old.user_name, old.thread_ts, old.kind, old.day) == (
            stored.text, stored.user_name, stored.thread_ts, stored.kind, stored.day):
        return False
    return store.upsert_message(stored)
