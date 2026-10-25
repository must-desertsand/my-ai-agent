"""One-time, resumable, paced import of the program channel's past messages for the dashboard.

Pages the channel backwards (newest to oldest) with `read_channel(..., latest=cursor)` and keeps a queue of threads
to read (huddle-bot parents and insight posts that have replies). Both live in cursors, so a stop (the call cap, the
huddle window, a Slack failure, a crash) resumes where it left off. Messages are stored by `history.capture`, which is
idempotent, so a rerun never duplicates. Slack text is never logged or returned: only counts and error kinds.

`run_thread_refresh` (D15) is the same job for what comes after the import: the threads that `history.capture` queued
because a huddle or insight post showed a newer reply. It runs nightly inside `history-enrich`, a few threads a night,
right after `sweep_recent` (D15a), one read of the last 3 days of the channel, so a reply that no job's page showed is
noticed too.

M4: a call can take up to 180 s, so none starts while it could still be running at 14:25 on a workday, and the clock
is read again after each pacing pause.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import history
from ..cli import HUDDLE, _in, _local
from ..config import Settings
from ..slack_gateway import DEFAULT_TIMEOUT_S, SlackError, SlackGateway, _kind
from ..store import Store

log = logging.getLogger(__name__)

PAGE_LIMIT = 50
PACE_S = 2.0
CURSOR_OLDEST = "history_import:oldest"   # ts of the oldest message read so far; the next page is older than it
CURSOR_THREADS = "history_import:threads"  # JSON list of parent ts still to read
CURSOR_DONE = "history_import:done"
MAX_THREAD_FAILS = 3  # D6: a thread that fails this many runs is dropped, so one poison thread cannot block the import
IMPORT_FAILS = "history_import:thread_fail:"  # + ts: failed reads of a thread of the import
REFRESH_FAILS = "history_refresh:thread_fail:"  # + ts: failed reads of a thread of the refresh
REFRESH_MAX_CALLS = 10  # threads read per night
SWEEP_DAYS = 3  # D15a: the nightly sweep reads the channel from this far back
SWEEP_LIMIT = 50
CALL_REACH = timedelta(seconds=DEFAULT_TIMEOUT_S)  # M4: how long a Slack call may still be running after it starts


@dataclass(frozen=True)
class ImportResult:
    calls: int
    messages_new: int
    threads_done: int
    finished: bool
    stopped_reason: str | None


def _key(ts: str) -> tuple[int, int]:
    sec, _, frac = ts.partition(".")
    return int(sec), int(frac or 0)


def _load_queue(store: Store) -> list[str]:
    return history.load_queue(store, CURSOR_THREADS)


def _in_huddle(now: datetime, settings: Settings) -> bool:
    local = _local(now, settings)
    return local.weekday() in settings.workdays and _in(local.time(), HUDDLE)


def _near_huddle(now: datetime, settings: Settings) -> bool:
    """M4: True in the huddle window, and also when a call started now could still be running at its start (14:25)
    on a workday. The tick's own jobs keep the window; these paced reads keep out of it."""
    local = _local(now, settings)
    if local.weekday() not in settings.workdays:
        return False
    start = datetime.combine(local.date(), HUDDLE[0], local.tzinfo)
    end = datetime.combine(local.date(), HUDDLE[1], local.tzinfo)
    return local < end and local + CALL_REACH >= start


def _paced(now_fn: Callable[[], datetime], sleep_fn: Callable[[float], None], settings: Settings, calls: int) -> bool:
    """Before a call: False when it must not start (M4). After the first call, pause PACE_S and look again."""
    if _near_huddle(now_fn(), settings):
        return False
    if calls:
        sleep_fn(PACE_S)
        if _near_huddle(now_fn(), settings):  # the pause may have brought the window within reach
            return False
    return True


def _count_failure(store: Store, prefix: str, ts: str) -> bool:
    """D6: count one more failed read of thread `ts`. True once it has failed MAX_THREAD_FAILS times: drop it."""
    key = f"{prefix}{ts}"
    fails = int(store.get_cursor(key) or 0) + 1
    store.set_cursor(key, str(fails))
    return fails >= MAX_THREAD_FAILS


def _clear_failures(store: Store, prefix: str, ts: str) -> None:
    key = f"{prefix}{ts}"
    if store.get_cursor(key) not in (None, "0"):
        store.set_cursor(key, "0")


def run_import(slack: SlackGateway, store: Store, settings: Settings, now_fn: Callable[[], datetime],
               sleep_fn: Callable[[float], None], max_calls: int = 150) -> ImportResult:
    channel = settings.channel_id
    calls = new = threads_done = 0

    def result(finished: bool, reason: str | None) -> ImportResult:
        return ImportResult(calls, new, threads_done, finished, reason)

    if store.get_cursor(CURSOR_DONE) == "1" and not _load_queue(store):
        return result(True, None)
    while True:
        queue = _load_queue(store)
        if calls >= max_calls:
            return result(False, "call cap")
        if not _paced(now_fn, sleep_fn, settings, calls):
            return result(False, "huddle window")
        calls += 1
        reading = queue[0] if queue else None
        try:
            if queue:
                ts = queue[0]
                parent, replies = slack.read_thread(channel, ts)
                new += history.capture(store, settings, replies, parent=parent, source_channel=channel)
                store.set_cursor(CURSOR_THREADS, json.dumps(queue[1:]))
                threads_done += 1
                continue
            before = store.get_cursor(CURSOR_OLDEST)
            page = slack.read_channel(channel, None, PAGE_LIMIT, latest=before)
            if not page:
                store.set_cursor(CURSOR_DONE, "1")
                return result(True, None)
            if before is not None and not any(_key(m.ts) < _key(before) for m in page):
                store.set_cursor(CURSOR_DONE, "1")  # `latest` was ignored or inclusive: nothing older to find
                return result(True, None)
            new += history.capture(store, settings, page, queue_refresh=False, source_channel=channel)  # the import reads these threads itself
            queue.extend(m.ts for m in page if history.wants_thread(m, settings) and m.ts not in queue)
            store.set_cursor(CURSOR_THREADS, json.dumps(queue))
            # Set last: a crash before this line rereads the same page, which capture stores idempotently.
            store.set_cursor(CURSOR_OLDEST, min((m.ts for m in page), key=_key))
        except SlackError as e:
            if reading is not None and _count_failure(store, IMPORT_FAILS, reading):
                log.warning("history import: dropping unreadable thread %s", reading)
                store.set_cursor(CURSOR_THREADS, json.dumps(queue[1:]))
                continue
            return result(False, _kind(str(e)) or type(e).__name__)


@dataclass(frozen=True)
class RefreshResult:
    calls: int
    threads_done: int
    messages_new: int
    dropped: int
    pending: int  # threads still queued when the run ended
    stopped_reason: str | None


def run_thread_refresh(slack: SlackGateway, store: Store, settings: Settings, now_fn: Callable[[], datetime],
                       sleep_fn: Callable[[float], None], max_calls: int = REFRESH_MAX_CALLS) -> RefreshResult:
    """D15: read the threads that `history.capture` queued because they got a new reply, oldest first, and store
    their replies. At most `max_calls` threads, 2 s apart, never in the huddle window.

    A thread leaves the queue once its replies are stored. A failed read keeps it at the head, counts the failure and
    ends the run (an outage is not paid for once per queued thread); D6: after MAX_THREAD_FAILS failed runs the
    thread is dropped and the run goes on, so one poison thread cannot hold the others back for ever. Only counts
    and error kinds are returned or logged, never Slack text."""
    channel = settings.channel_id
    calls = new = done = dropped = 0

    def result(reason: str | None) -> RefreshResult:
        return RefreshResult(calls, done, new, dropped, len(history.load_queue(store, history.REFRESH_QUEUE)), reason)

    while True:
        queue = history.load_queue(store, history.REFRESH_QUEUE)
        if not queue:
            return result(None)
        if calls >= max_calls:
            return result("call cap")
        if not _paced(now_fn, sleep_fn, settings, calls):
            return result("huddle window")
        calls += 1
        ts = queue[0]
        try:
            parent, replies = slack.read_thread(channel, ts)
        except SlackError as e:
            if _count_failure(store, REFRESH_FAILS, ts):
                log.warning("history refresh: dropping unreadable thread %s", ts)
                history.dequeue_refresh(store, ts)
                _clear_failures(store, REFRESH_FAILS, ts)  # a later reply gets a fresh set of tries
                dropped += 1
                continue
            return result(_kind(str(e)) or type(e).__name__)
        new += history.capture(store, settings, replies, parent=parent, source_channel=channel)
        # Off the queue only after the capture: a crash in between rereads the thread, and capture is idempotent.
        history.dequeue_refresh(store, ts)
        _clear_failures(store, REFRESH_FAILS, ts)
        done += 1


@dataclass(frozen=True)
class SweepResult:
    calls: int
    messages_new: int
    queued: int  # threads on the refresh queue after the sweep
    stopped_reason: str | None


def sweep_recent(slack: SlackGateway, store: Store, settings: Settings, now_fn: Callable[[], datetime],
                 sleep_fn: Callable[[float], None]) -> SweepResult:
    """D15a: one `read_channel` of the program channel from 3 days back (at most 50 posts), captured with the
    refresh queue on. An insight or huddle post whose newest reply is newer than the last one noted goes on the
    queue, so the refresh that follows reads replies that no job's page has shown. Refused near and in the huddle
    window (M4); after a read that queued anything it pauses PACE_S, so the refresh's first read is paced too. A
    Slack failure is returned by its kind only."""
    channel = settings.channel_id

    def queued() -> int:
        return len(history.load_queue(store, history.REFRESH_QUEUE))

    now = now_fn()
    if _near_huddle(now, settings):
        return SweepResult(0, 0, queued(), "huddle window")
    oldest = f"{(now - timedelta(days=SWEEP_DAYS)).timestamp():.6f}"
    try:
        page = slack.read_channel(channel, oldest, SWEEP_LIMIT)
    except SlackError as e:
        return SweepResult(1, 0, queued(), _kind(str(e)) or type(e).__name__)
    new = history.capture(store, settings, page, source_channel=channel)  # the refresh queue on (the default)
    waiting = queued()
    if waiting:
        sleep_fn(PACE_S)
    return SweepResult(1, new, waiting, None)
