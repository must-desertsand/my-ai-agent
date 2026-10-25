"""haseeb-agent: tick | insights [--force] | decks [--retry] | weekly | smoke | slack-check | feedback --ts TS | usage-backfill | ...

launchd runs `tick` every 60 s, `insights` at 08:15 Mon–Fri and `weekly` on Fri 17:00, with the repo root as the working directory (R5),
so config.toml, .env and data/ resolve relative to it. The dashboard's run controls start the other commands (spawn_job).

While the agent is paused (cursor `agent_paused`, see controls.py) the tick records its heartbeat and skips every job,
and every other job command refuses at start: one stderr line, exit 0. `slack-check` only reads, so it still runs. The
paused tick also does database-only housekeeping (`pause_housekeeping`): clicks queued before the pause expire and a
pending Next Todo auto-post is cancelled, with no Slack call.

The tick decides per job whether it is due (ruling R31) and runs the due ones in a fixed order. It also keeps Haseeb's list
of channels (channels.py): one Slack read a day, or when the dashboard asked for it, never while paused and never in the
huddle hold. Each job is isolated: a failure is recorded and printed to stderr, and the tick still exits 0 so launchd keeps its schedule.
Only a settings failure exits 1. Output lines name jobs and counts, never message text or keys.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time as _time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from . import alert, channels, controls, events, usage
from .alert import SlackHealth
from .commands import HELD_KEY, Handlers, process_dm, send_dm
from .config import Settings, load_settings
from .jobs.feedback import FeedbackHandlers, FeedbackRefused, draft_one, run_feedback
from .jobs.decks import deck_test, decks_pending, deliver_decks, retry_given_up
from .jobs.insights import (CATCH_UP_END, CATCH_UP_LAST, InsightHandlers, close_unreachable_day, deliver_pending,
                            run_insights)
from .jobs.message import MessageHandlers
from .jobs.todo import OVERTIME_END
from .jobs.todo import TERMINAL as TODO_TERMINAL
from .jobs.todo import TodoHandlers, cancel_autoposts_for_pause, run_todo
from .jobs.todo import item_id as todo_item_id
from .jobs.todo import overtime as todo_overtime
from .litellm import LLM
from .metrics import render_weekly, week_stats
from .notify import notify
from .offline import guard
from .page_commands import HUDDLE_BUDGET_S as PAGE_HUDDLE_BUDGET_S
from .page_commands import HUDDLE_ROWS as PAGE_HUDDLE_ROWS
from .page_commands import expire_for_pause, page_due, process_page
from .slack_gateway import (ClaudeConnectorSlack, SlackGateway, SlackPaused, call_timeout, profile_user_id,
                            read_failures)
from .store import Store

log = logging.getLogger(__name__)

SMOKE_TEXT = "🧪 Haseeb's Agent smoke test"

# Run order on a tick. deliver_pending runs on every tick on which process_dm runs (R26, R31).
JOBS: tuple[str, ...] = ("process_dm", "deliver_pending", "run_todo", "run_feedback")
# Dashboard commands run before all of them, on every tick with page work queued (and only then: an empty queue
# costs one DB query, no Slack call and no event). Kept out of JOBS, whose four jobs are due on their own cadence.
PAGE_JOB = "process_page"
# The channel list's refresh (channels.py) runs right after the page commands and before the four jobs. Like the page job it is
# kept out of JOBS: it has no cadence of its own, `channels.wanted` says when (a day old or missing, or a refresh was requested). It
# waits out the huddle hold (a read nobody is waiting on must not delay the Next Todo): the tick looks at the hold when it starts and
# the job looks again when it starts, because the jobs before it can run for minutes. And, like every job, it waits out the pause. A
# failed refresh is in the run log but not in the weekly "Failed job runs" (store.UNCOUNTED_JOBS names this job).
CHANNELS_JOB = "refresh_channels"
# The commands that refuse to start while the agent is paused (the tick pauses itself, in run_tick). The two diagnostics
# that send a message or build a deck are in it; `slack-check` only reads and is not.
PAUSE_GATED: frozenset[str] = frozenset({"insights", "decks", "weekly", "smoke", "deck-test", "feedback",
                                         "history-import", "history-enrich", "history-refresh"})

# R31 windows, on workdays only, Asia/Kuwait.
DAY = (time(8, 0), time(20, 0))
HUDDLE = (time(14, 25), time(16, 5))  # covers an auto-post due up to 16:04 (jobs.todo: WINDOW_END - POST_MARGIN)
# ...and on to OVERTIME_END (18:00) on a day whose Next Todo is still under way at 16:05 (TickState.huddle_end).
DM_HUDDLE_S, DM_DAY_S, DM_OFF_S = 60, 300, 1800
FEEDBACK_DAY_S, FEEDBACK_OFF_S = 600, 1800
# launchd's StartInterval drifts by a second or two; without this a 60 s job would run every other tick.
JITTER_S = 5

MAX_REASON = 200
# I9: when the Mac was off at 08:15, a tick starts the (not forced) insights run in this window, at most every 30 min.
# The same retry covers a day an outage left open (I8: the writers; the research too, within its cap of tries a day,
# and a research outage starts the 30 minutes itself, see jobs.insights.CATCH_UP_LAST).
CATCH_UP = (time(8, 15), CATCH_UP_END)
CATCH_UP_EVERY = timedelta(minutes=30)
# A tick that runs longer than this is stopped (a hung connector call must never leave the agent silently dead).
TICK_LIMIT_S = 15 * 60
# If the watchdog's exception is swallowed somewhere, the process hard-exits this much later.
WATCHDOG_GRACE_S = 30
# R44 (fix round 1): the tick never builds decks; it starts `haseeb-agent decks` detached, at most this often while
# that process has not yet taken its lock.
DECKS_SPAWN_EVERY = timedelta(minutes=2)


@dataclass(frozen=True)
class TickState:
    todo_final: bool
    huddle_end: time = HUDDLE[1]  # OVERTIME_END while a long huddle keeps today's Next Todo under way (jobs.todo.overtime)


@dataclass
class Runtime:
    settings: Settings
    store: Store
    slack: SlackGateway
    llm: LLM | None
    handlers: dict[str, Handlers]
    # R44: (now, limit) -> decks uploaded, and (now) -> is there deck work. None: no deck step (tests' fake runtimes).
    decks: Callable[[datetime, int | None], int] | None = None
    decks_pending: Callable[[datetime], bool] | None = None


# --------------------------------------------------------------------------
# R31: when each job is due
# --------------------------------------------------------------------------

def _in(t: time, window: tuple[time, time]) -> bool:
    return window[0] <= t < window[1]


def _local(now: datetime, settings: Settings) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("due() needs a timezone-aware `now`")
    return now.astimezone(settings.tz)


def _huddle(state: TickState | None) -> tuple[time, time]:
    """The day's huddle window: HUDDLE, ending at OVERTIME_END instead while today's Next Todo is in overtime."""
    return HUDDLE if state is None else (HUDDLE[0], state.huddle_end)


def interval_s(job: str, now: datetime, settings: Settings, state: TickState) -> int:
    local = _local(now, settings)
    workday = local.weekday() in settings.workdays
    t = local.time()
    day = workday and _in(t, DAY)
    huddle = workday and _in(t, _huddle(state))
    dm_base = DM_DAY_S if day else DM_OFF_S
    if job in ("process_dm", "deliver_pending"):
        return DM_HUDDLE_S if huddle else dm_base
    if job == "run_todo":
        # 60 s around the huddle until today's todo is final; then process_dm's daytime cadence. (Inside 14:25–16:05
        # process_dm itself is 60 s, so "the process_dm cadence" there would make the final flag a no-op.)
        return DM_HUDDLE_S if huddle and not state.todo_final else dm_base
    if job == "run_feedback":
        return FEEDBACK_DAY_S if day else FEEDBACK_OFF_S
    raise KeyError(job)


HUDDLE_SLACK_TIMEOUT_S = 75
HUDDLE_SLACK_JOBS = ("process_page", "process_dm", "run_todo")


def slack_timeout(job: str, now: datetime, settings: Settings, state: TickState | None = None) -> int | None:
    """Monday hardening: in the huddle window (workdays 14:25–16:05, and its overtime with `state`) a Slack call from
    process_page, process_dm or run_todo gets 75 s instead of 180, so one slow call (108–155 s on 2026-10-17) cannot eat
    the 5-minute Next Todo. None: the gateway's default."""
    local = _local(now, settings)
    if job in HUDDLE_SLACK_JOBS and local.weekday() in settings.workdays and _in(local.time(), _huddle(state)):
        return HUDDLE_SLACK_TIMEOUT_S
    return None


def huddle_open(now: datetime, settings: Settings, state: TickState) -> bool:
    """A workday 14:25–16:05 (or its overtime) with today's todo not final yet: the huddle poll must not be blinded."""
    local = _local(now, settings)
    return local.weekday() in settings.workdays and _in(local.time(), _huddle(state)) and not state.todo_final


def due(job: str, now: datetime, last_run: datetime | None, settings: Settings, state: TickState) -> bool:
    every = interval_s(job, now, settings, state)
    if job == "run_feedback" and huddle_open(now, settings, state):
        # R33: a feedback run takes minutes and would hold the tick lock while the todo needs a 60 s poll.
        # Skipped, not recorded, so it runs on the first eligible tick after the window or once the todo is final.
        return False
    if last_run is None:
        return True
    elapsed = (now - last_run).total_seconds()
    return elapsed < 0 or elapsed >= every - JITTER_S  # a clock that went backwards must not stall a job


def message_hold(store: Store, settings: Settings) -> Callable[[datetime], bool]:
    """R33's predicate for the composer: in the huddle window AND while today's todo is unsettled. Composer rows and
    `say:` drafts wait while it holds (jobs/message.py `holding`)."""
    return lambda now: huddle_open(now, settings, tick_state(store, settings, now))


def tick_state(store: Store, settings: Settings, now: datetime) -> TickState:
    """Today's todo is final when its item is terminal or the "no huddle" line went out (see jobs/todo.py). Past 16:05
    the huddle window runs on to OVERTIME_END while the todo is still under way (jobs.todo.overtime)."""
    tid = todo_item_id(_local(now, settings).date())
    item = store.get_item(tid)
    final = (item is not None and item.status in TODO_TERMINAL) or store.get_cursor(f"nohuddle:{tid}") is not None
    longer = not final and todo_overtime(store, settings, now)
    return TickState(todo_final=final, huddle_end=OVERTIME_END if longer else HUDDLE[1])


# --------------------------------------------------------------------------
# Locks
# --------------------------------------------------------------------------

@contextmanager
def try_lock(path: Path, *, blocking: bool = False) -> Iterator[bool]:
    """An exclusive flock on `path`. Yields False at once when another process holds it (non-blocking)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _data_dir(settings: Settings) -> Path:
    return Path(settings.db_path).parent


def tick_lock_path(settings: Settings) -> Path:
    return _data_dir(settings) / "tick.lock"


def insights_lock_path(settings: Settings) -> Path:
    """Held for a whole `insights` run, so a tick's deliver_pending never races the run's own delivery."""
    return _data_dir(settings) / "insights.lock"


def enrich_lock_path(settings: Settings) -> Path:
    """Held for a whole `history-enrich` run, so the nightly spawn and a manual run never overlap."""
    return _data_dir(settings) / "enrich.lock"


def decks_lock_path(settings: Settings) -> Path:
    """Held while decks are built and uploaded, so the tick and an `insights` run never upload the same deck."""
    return _data_dir(settings) / "decks.lock"


def _job_lock(settings: Settings, name: str) -> Path:
    """The lock file of a dashboard job (controls.JOBS): the dashboard's "is it running" probe looks at the same file."""
    job = controls.BY_NAME[name]
    assert job.lock is not None
    return _data_dir(settings) / job.lock


def weekly_lock_path(settings: Settings) -> Path:
    return _job_lock(settings, "weekly")


def history_import_lock_path(settings: Settings) -> Path:
    """Held for a whole `history-import` run, apart from the tick lock it also takes: the tick lock is shared with the
    tick, so it cannot tell the dashboard whether it is the import that is running."""
    return _job_lock(settings, "history_import")


def slack_check_lock_path(settings: Settings) -> Path:
    return _job_lock(settings, "slack_check")


def feedback_lock_path(settings: Settings) -> Path:
    return _job_lock(settings, "feedback")


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def build_runtime(settings: Settings) -> Runtime:
    store = Store(settings.db_path)
    # The dashboard reads while the tick writes, so the DB is switched to WAL once (after a backup copy). A busy
    # or unwritable DB must never fail the tick: log one text-free line, and the next tick tries again.
    try:
        store.enable_wal(Path(settings.db_path).parent, datetime.now().astimezone())
    except (sqlite3.Error, OSError, RuntimeError) as e:
        log.warning("WAL switch skipped, will retry on the next tick (%s)", type(e).__name__)
    # R13; wrapped so the tick can tell an outage from a quiet tick (I7)
    slack = SlackHealth(ClaudeConnectorSlack(settings, dm_channels={settings.self_dm_id: settings.self_dm_channel},
                                             store=store))
    llm = LLM(settings)
    handlers: dict[str, Handlers] = {
        "insight": InsightHandlers(slack, llm, store, settings),
        "feedback": FeedbackHandlers(slack, llm, store, settings),
        "todo": TodoHandlers(slack, llm, store, settings),
        # the composer and `say:` (jobs/message.py); they wait while the huddle poll must not be blinded (R33)
        "message": MessageHandlers(slack, llm, store, settings, hold=message_hold(store, settings)),
    }
    return Runtime(settings=settings, store=store, slack=slack, llm=llm, handlers=handlers,
                   decks=lambda now, limit: deliver_decks(slack, store, settings, now, limit=limit),
                   decks_pending=lambda now: decks_pending(store, settings, now))


def _deck_step(rt: Runtime, now: datetime, limit: int | None) -> str:
    """R44: build and upload decks after the text. Never raises; returns a summary suffix for the job line."""
    if rt.decks is None:
        return ""
    try:
        with try_lock(decks_lock_path(rt.settings)) as got:
            if not got:
                return "; decks busy"
            n = rt.decks(now, limit)
    except Exception as e:  # noqa: BLE001 - a deck never fails or delays the text (R44)
        print(f"{_stamp(now)} deck step failed: {_reason(e)}", file=sys.stderr, flush=True)
        return "; deck step failed"
    return f"; {n} deck(s)"


# N4: the intermediate of the double fork. It starts the real command in a new session and exits at once, so the
# command is reparented to launchd (pid 1) and is no child of the tick: the watchdog's `pkill -P <tick pid>`
# cannot reach it. It inherits the tick's stdout/stderr (the launchd log files).
_DETACH = ("import subprocess, sys; "
           "subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)")
SPAWN_TIMEOUT_S = 30


def _spawn_detached(cmd: list[str]) -> None:
    """N4 double fork: the command runs in a new session, reparented to launchd, so it is no child of the tick and
    neither the tick lock nor the watchdog covers it."""
    subprocess.run([sys.executable, "-I", "-c", _DETACH, *cmd], cwd=os.getcwd(), stdin=subprocess.DEVNULL,
                   close_fds=True, timeout=SPAWN_TIMEOUT_S, check=True)


def spawn_decks(argv: list[str] | None = None) -> None:
    """Start `haseeb-agent decks` as its own detached process (R44, fix round 1). It takes data/decks.lock itself
    and exits at once if another deck process holds it."""
    if argv is None:
        guard("spawn decks")
    _spawn_detached(argv or [sys.executable, "-m", "haseeb_agent.cli", "decks"])


def spawn_insights(argv: list[str] | None = None) -> None:
    """Start `haseeb-agent insights` (not forced) as its own process, as launchd does at 08:15. It takes the
    insights lock itself and can run for up to 45 min, so it must not run inside the tick (15 min watchdog, tick
    lock). A double fork (N4) detaches it from the tick: the watchdog kills only the tick's own children (a hung
    `claude`), never this run. A new session, plus AbandonProcessGroup in the tick plist, keeps it alive after the
    tick exits. `argv` replaces the command (tests). Raises if the intermediate fails."""
    if argv is None:
        guard("spawn insights")
    _spawn_detached(argv or [sys.executable, "-m", "haseeb_agent.cli", "insights"])


def spawn_enrich(argv: list[str] | None = None) -> None:
    """Start `haseeb-agent history-enrich` as its own detached process (N4 double fork), like `spawn_insights`: it
    can run for many minutes, so it must not run inside the tick. It takes data/enrich.lock itself."""
    if argv is None:
        guard("spawn enrich")
    _spawn_detached(argv or [sys.executable, "-m", "haseeb_agent.cli", "history-enrich"])


def spawn_job(args: list[str], argv: list[str] | None = None) -> None:
    """Start `haseeb-agent <args>` as its own detached process (N4 double fork) for the dashboard's run controls.
    `args` comes from controls.spawn_args: a fixed allowlisted command, plus the one ts a feedback run takes, which is
    checked there. `argv` replaces the command (tests). Raises if the intermediate fails."""
    if argv is None:
        guard(f"spawn {args[0] if args else 'job'}")
    _spawn_detached(argv or [sys.executable, "-m", "haseeb_agent.cli", *args])


ENRICH_FROM = time(20, 0)  # D2: the nightly enrichment starts at the first tick at or after 20:00 local


def enrich_due(store: Store, settings: Settings, now: datetime) -> bool:
    """D2: once per local day, from 20:00 on (any later tick that day until it has run), never in the huddle window."""
    local = _local(now, settings)
    if local.time() < ENRICH_FROM or (local.weekday() in settings.workdays and _in(local.time(), HUDDLE)):
        return False
    return store.get_cursor("enrich_last_day") != local.date().isoformat()


def catch_up_due(store: Store, settings: Settings, now: datetime) -> bool:
    """I9: a workday between 08:15 and 13:00, today's insights have not run, and no catch-up in the last 30 min.
    A day an outage left open counts as not run: its `insights_ran` cursor is emptied (I8, and a research outage)."""
    local = _local(now, settings)
    if local.weekday() not in settings.workdays or not _in(local.time(), CATCH_UP):
        return False
    if store.get_cursor(f"insights_ran:{local.date().isoformat()}"):
        return False
    last = store.get_cursor(CATCH_UP_LAST)
    try:
        if last and timedelta(0) <= local - datetime.fromisoformat(last) < CATCH_UP_EVERY:
            return False
    except (ValueError, TypeError):
        pass
    return True


def default_jobs(rt: Runtime) -> dict[str, Callable[[datetime], str]]:
    """Each job takes a timezone-aware `now` and returns a one-line summary with no message text."""
    s, st, sl = rt.settings, rt.store, rt.slack

    def page(now: datetime) -> str:
        # M3: inside the open huddle window a click must not delay the 60 s todo poll
        budget = ({"limit": PAGE_HUDDLE_ROWS, "budget_s": PAGE_HUDDLE_BUDGET_S}
                  if huddle_open(now, s, tick_state(st, s, now)) else {})
        return f"{process_page(sl, st, s, rt.handlers, now, **budget)} page command(s)"

    def refresh_channels(now: datetime) -> str:
        if controls.paused_since(st) is not None:  # a pause that landed during this tick: no call, and a request waits
            return "paused; nothing read"
        if huddle_open(now, s, tick_state(st, s, now)):  # the jobs before this one ran into the hold: no call, and a request waits
            return "huddle hold; nothing read"
        forced = channels.take_request(st)  # taken before the read: a request makes one try, whatever comes of it
        if channels.refresh(sl, st, now, force=forced):
            return f"{len(channels.cached(st)[1])} channel(s)"
        return "list not refreshed (the old one is kept)"

    def dm(now: datetime) -> str:
        return f"{process_dm(sl, st, s, rt.handlers, now)} command(s)"

    def deliver(now: datetime) -> str:
        with try_lock(insights_lock_path(s)) as got:
            if not got:
                return "skipped: an insights run is in progress"
            out = f"{len(deliver_pending(sl, st, s, now))} insight(s) delivered"
            if catch_up_due(st, s, now):  # checked while holding the lock, so it is free right now (I9)
                st.set_cursor(CATCH_UP_LAST, _local(now, s).isoformat())
                try:
                    spawn_insights()
                except Exception as e:  # noqa: BLE001 - never fail the delivery over the catch-up
                    print(f"{_stamp(now)} insights catch-up could not start: {_reason(e)}", file=sys.stderr,
                          flush=True)
                else:
                    out += "; insights catch-up started"
            if close_unreachable_day(sl, st, s, now):  # past the window with the models still down: say so, once
                out += "; day closed (models unreachable)"
        if enrich_due(st, s, now):
            st.set_cursor("enrich_last_day", _local(now, s).date().isoformat())  # first: once per day, even if it fails
            try:
                spawn_enrich()
            except Exception as e:  # noqa: BLE001 - never fail the delivery over the enrichment
                print(f"{_stamp(now)} enrichment could not start: {_reason(e)}", file=sys.stderr, flush=True)
            else:
                out += "; enrichment started"
        return out + _start_decks(rt, now)  # after the text, outside the insights lock, detached (R44)

    def todo(now: datetime) -> str:
        # I2: the job re-reads the clock after drafting, so the 120 s veto counts from the DM's delivery.
        run_todo(sl, rt.llm, st, s, now, clock=lambda: datetime.now(s.tz))
        item = st.get_item(todo_item_id(now.astimezone(s.tz).date()))
        return f"today's todo: {item.status if item else 'none'}"

    def feedback(now: datetime) -> str:
        return f"{run_feedback(sl, rt.llm, st, s, now)} draft(s) delivered"

    return {PAGE_JOB: page, CHANNELS_JOB: refresh_channels, "process_dm": dm, "deliver_pending": deliver, "run_todo": todo,
            "run_feedback": feedback}


def _reason(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return text if len(text) <= MAX_REASON else text[: MAX_REASON - 1] + "…"


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def _start_decks(rt: Runtime, now: datetime) -> str:
    """Spawn the deck process when there is deck work and none is running. Never raises; a summary suffix."""
    if rt.decks_pending is None or not rt.settings.decks_enabled:
        return ""
    try:
        with try_lock(decks_lock_path(rt.settings)) as free:
            if not free:
                return "; decks running"
        last = rt.store.get_cursor("decks_spawned_at")
        try:
            recent = bool(last) and timedelta(0) <= now - datetime.fromisoformat(last) < DECKS_SPAWN_EVERY
        except (ValueError, TypeError):
            recent = False
        if recent or not rt.decks_pending(now):  # recent: the child may not hold its lock yet
            return ""
        rt.store.set_cursor("decks_spawned_at", now.isoformat())
        spawn_decks()
    except Exception as e:  # noqa: BLE001 - a deck never fails or delays the text (R44)
        print(f"{_stamp(now)} decks could not start: {_reason(e)}", file=sys.stderr, flush=True)
        return "; decks could not start"
    return "; decks started"


# --------------------------------------------------------------------------
# What a finished job command leaves for its dashboard control (controls.record_last)
# --------------------------------------------------------------------------

_NOTE: ContextVar[dict | None] = ContextVar("job_note", default=None)


def note(summary: str, *, ok: bool | None = None) -> None:
    """What the running job command tells its control: a short summary, and whether the run counts as ok when its exit
    code alone would not say (the tick exits 0 whatever its jobs did). Counts and fixed phrases only, never message
    text. Nothing happens outside `run_recorded`."""
    box = _NOTE.get()
    if box is not None:
        box["summary"] = summary
        if ok is not None:
            box["ok"] = ok


def _record_last(settings: Settings, job: str, at: datetime, ok: bool, summary: str) -> None:
    """controls.record_last on a connection of its own; never raises (a record must not fail the job)."""
    try:
        store = Store(settings.db_path)
        try:
            controls.record_last(store, job, at, ok, summary)
        finally:
            store.close()
    except Exception as e:  # noqa: BLE001
        print(f"{job}: could not record the last run: {type(e).__name__}", file=sys.stderr, flush=True)


def run_recorded(settings: Settings, job: str, clock: Callable[[], datetime], fn: Callable[[], int]) -> int:
    """Run a job command and, when it ends, write `job_last:<job>` = {at, ok, summary} (try/finally, so a failure and
    even an exception leave one). ok is the exit code being 0 unless the command said otherwise (`note`); an exception
    is a failure whose summary is its class name."""
    box: dict = {}
    token = _NOTE.set(box)
    rc, failure = 1, None
    try:
        with usage.session(settings.db_path, job):  # the model calls of this run are recorded for this job (usage.py)
            rc = fn()
        return rc
    except BaseException as e:
        failure = type(e).__name__
        raise
    finally:
        _NOTE.reset(token)
        ok = failure is None and box.get("ok", rc == 0)
        _record_last(settings, job, clock(), ok, failure or box.get("summary") or "")


def _refuse_when_paused(settings: Settings, command: str, clock: Callable[[], datetime]) -> int | None:
    """None when `command` may run. While the agent is paused: one stderr line and exit 0 (the run is skipped, not
    failed, so nothing retries it). A pause state that cannot be read fails closed: exit 1."""
    try:
        since = controls.paused_since_db(settings)
    except Exception as e:  # noqa: BLE001
        print(f"{_stamp(clock())} {command}: cannot read the pause state ({type(e).__name__}); not running",
              file=sys.stderr, flush=True)
        return 1
    if since is None:
        return None
    print(f"{_stamp(clock())} {command}: the agent is paused since {since}; nothing was run (Resume on the dashboard)",
          file=sys.stderr, flush=True)
    if job := controls.COMMAND_JOB.get(command):
        _record_last(settings, job, clock(), True, "skipped: paused")
    return 0


def pause_housekeeping(settings: Settings, store: Store, since: str, now: datetime) -> str:
    """What a paused tick does besides its heartbeat: the database only, no Slack call. (a) Dashboard clicks queued
    before the pause expire, so the kill switch also kills an armed action; (b) a pending Next Todo auto-post is
    cancelled, with its "reply post to post it" notice queued for after Resume. Each part is isolated and never raises.
    Returns what it did, for the tick's line ("" when it found nothing to do)."""
    done: list[str] = []
    try:
        if n := expire_for_pause(store, since, now):
            done.append(f"{n} queued click(s) expired")
    except Exception as e:  # noqa: BLE001
        print(f"{_stamp(now)} pause housekeeping (clicks) failed: {_reason(e)}", file=sys.stderr, flush=True)
    try:
        if n := cancel_autoposts_for_pause(store, settings, now):
            done.append(f"{n} auto-post(s) cancelled")
    except Exception as e:  # noqa: BLE001
        print(f"{_stamp(now)} pause housekeeping (auto-post) failed: {_reason(e)}", file=sys.stderr, flush=True)
    return ", ".join(done)


def _stopped_by_pause(clock: Callable[[], datetime], command: str) -> int:
    """A run the pause stopped in the middle: the gateway refused a write with SlackPaused before any call, so nothing was
    sent and the run did not fail. It is not recorded as a failed run (no `runs` row, so the weekly "Failed job runs"
    stay honest) and its control reads "stopped: paused", ok, like a run the pause refused at the start. Exit 0."""
    print(f"{_stamp(clock())} {command}: the agent was paused; stopped, nothing more was sent (Resume on the dashboard)",
          file=sys.stderr, flush=True)
    note("stopped: paused")
    return 0


# --------------------------------------------------------------------------
# The tick
# --------------------------------------------------------------------------

def run_tick(settings: Settings, store: Store, jobs: Mapping[str, Callable[[datetime], str]], *,
             clock: Callable[[], datetime]) -> list[str]:
    """Run the jobs due now, in JOBS order, each isolated. Returns the names of the jobs that ran."""
    start = clock()
    try:  # the dashboard's "Mac awake": written even by a quiet tick, and never allowed to fail the tick
        store.set_cursor("tick_seen", start.isoformat())
    except Exception as e:  # noqa: BLE001
        log.warning("tick_seen not recorded (%s)", type(e).__name__)
    if (since := controls.paused_since(store)) is not None:  # the dashboard's Pause: the heartbeat above, no job
        did = pause_housekeeping(settings, store, since, start)  # DB only: clicks queued before it, the auto-post
        print(f"{_stamp(start)} tick paused since {since}: no job ran" + (f" ({did})" if did else ""), flush=True)
        note("paused: no job ran" + (f" ({did})" if did else ""))
        return []
    state = tick_state(store, settings, start)
    hold = huddle_open(start, settings, state)
    if hold:  # composer rows and `say:` drafts wait (jobs/message.py), and what is queued does not age meanwhile
        try:
            store.set_cursor(HELD_KEY, start.isoformat())
        except Exception as e:  # noqa: BLE001 - a busy DB must not stop the tick
            log.warning("%s not recorded (%s)", HELD_KEY, type(e).__name__)

    def last(job: str) -> datetime | None:
        value = store.get_cursor(f"tick_last:{job}")
        try:
            dt = datetime.fromisoformat(value) if value else None
        except ValueError:
            return None
        return dt if dt is not None and dt.utcoffset() is not None else None  # unreadable: treat as never run

    dm_due = due("process_dm", start, last("process_dm"), settings, state)
    todo_due = due("run_todo", start, last("run_todo"), settings, state)
    feedback_due = due("run_feedback", start, last("run_feedback"), settings, state)
    wanted = {"process_dm": dm_due, "deliver_pending": dm_due, "run_todo": todo_due, "run_feedback": feedback_due}
    try:
        wanted[PAGE_JOB] = PAGE_JOB in jobs and page_due(store, hold_compose=hold)
    except Exception as e:  # noqa: BLE001 - a busy DB must not stop the other jobs
        print(f"{_stamp(start)} {PAGE_JOB} skipped: {_reason(e)}", file=sys.stderr, flush=True)
        wanted[PAGE_JOB] = False
    try:
        wanted[CHANNELS_JOB] = CHANNELS_JOB in jobs and not hold and channels.wanted(store, start)
    except Exception as e:  # noqa: BLE001 - a busy DB must not stop the other jobs
        print(f"{_stamp(start)} {CHANNELS_JOB} skipped: {_reason(e)}", file=sys.stderr, flush=True)
        wanted[CHANNELS_JOB] = False

    ran: list[str] = []
    bad: list[str] = []
    stopped = False
    for job in (PAGE_JOB, CHANNELS_JOB, *JOBS):  # page commands first: a click is answered before the slower jobs run
        if not wanted[job]:
            continue
        now = clock()  # a fresh `now` per job: an earlier job may have taken a while
        began = _time.monotonic()
        try:
            store.set_cursor(f"tick_last:{job}", start.isoformat())  # first, so a crash can't make it run every tick
            # per job: a tick that began just before 16:05 may run this job in the day's overtime
            budget = slack_timeout(job, now, settings, tick_state(store, settings, now))
            with read_failures() as failed, call_timeout(budget), usage.scoped(job):
                summary = jobs[job](now)
            if failed:
                # Honest tick.log: a job that carried on without a Slack page did not run "ok" (counted as a
                # failed run in the weekly metrics).
                bad.append(job)
                print(f"{_stamp(now)} {job} fail {failed[-1]} ({_time.monotonic() - began:.1f}s)", flush=True)
                _record(store, settings, job, f"slack read failed: {', '.join(dict.fromkeys(failed))}"[:MAX_REASON])
            else:
                print(f"{_stamp(now)} {job} ok {summary} ({_time.monotonic() - began:.1f}s)", flush=True)
        except SlackPaused:
            # The pause landed while this job ran and the gateway refused a write before any call: nothing was sent and
            # the job did not fail. Every write after it would be refused too, so the tick ends here.
            print(f"{_stamp(now)} {job} stopped: the agent was paused ({_time.monotonic() - began:.1f}s)", flush=True)
            ran.append(job)
            stopped = True
            break
        except Exception as e:  # noqa: BLE001 - one job must never stop the others
            reason = _reason(e)
            bad.append(job)
            print(f"{_stamp(now)} {job} failed: {reason} ({_time.monotonic() - began:.1f}s)", file=sys.stderr,
                  flush=True)
            _record(store, settings, job, reason)
        ran.append(job)
    note(f"{len(ran)} job(s) ran" + (f", {len(bad)} failed" if bad else "") + (", stopped by the pause" if stopped else ""),
         ok=not bad)
    return ran


class TickTimeout(BaseException):
    """Raised by the watchdog. A BaseException, so the jobs' `except Exception` guards don't swallow it, and
    subprocess.run kills its child (a hung `claude`) on the way out."""


def _kill_children() -> None:
    """Best effort: stop any child processes left behind (claude and what it started)."""
    try:
        subprocess.run(["pkill", "-TERM", "-P", str(os.getpid())], capture_output=True, timeout=5, check=False)
    except Exception:  # noqa: BLE001, S110 - best effort while exiting
        pass


@contextmanager
def watchdog(limit_s: float, grace_s: float = WATCHDOG_GRACE_S) -> Iterator[None]:
    """SIGALRM after `limit_s`: raise TickTimeout. If that is swallowed, hard-exit 0 after `grace_s` more
    (the exit releases the flock)."""
    fired = False

    def on_alarm(signum, frame):
        nonlocal fired
        if not fired:
            fired = True
            signal.setitimer(signal.ITIMER_REAL, grace_s)
            raise TickTimeout()
        print(f"{_stamp(datetime.now().astimezone())} tick watchdog: hard exit", file=sys.stderr, flush=True)
        _kill_children()
        os._exit(0)

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, limit_s)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _record(store: Store | None, settings: Settings, job: str, reason: str) -> None:
    """record_run that never raises; opens the store itself when none was built."""
    try:
        (store or Store(settings.db_path)).record_run(job, False, reason)
    except Exception as e:  # noqa: BLE001
        print(f"{job}: could not record the failure: {_reason(e)}", file=sys.stderr, flush=True)


def _check_alert(rt: Runtime | None, clock, notifier) -> None:
    """I7: after the jobs, alert on a Slack outage streak or a failure notice that could not be sent."""
    if rt is None:
        return
    try:
        health = rt.slack if isinstance(rt.slack, SlackHealth) else None
        alert.check(rt.store, clock(), notifier or alert.notifier_for(rt.settings), health)
    except Exception as e:  # noqa: BLE001 - the alert must never break the tick
        print(f"alert check failed: {_reason(e)}", file=sys.stderr, flush=True)


def cmd_tick(settings: Settings, runtime_factory, jobs_factory, clock, limit_s: float = TICK_LIMIT_S,
             grace_s: float = WATCHDOG_GRACE_S, notifier=None) -> int:
    with try_lock(tick_lock_path(settings)) as got:
        if not got:
            return 0  # the previous tick is still running
        # recorded as job_last:tick for the dashboard's "Check round" (only a tick that held the lock and ran)
        return run_recorded(settings, "tick", clock,
                            lambda: _locked_tick(settings, runtime_factory, jobs_factory, clock, limit_s, grace_s,
                                                 notifier))


def _locked_tick(settings: Settings, runtime_factory, jobs_factory, clock, limit_s: float, grace_s: float,
                 notifier) -> int:
    rt = None
    try:
        with watchdog(limit_s, grace_s):
            rt = runtime_factory(settings)
            if isinstance(rt.slack, SlackHealth):
                rt.slack.reset()
            jobs = jobs_factory(rt)
            run_tick(settings, rt.store, jobs, clock=clock)
    except TickTimeout:
        print(f"{_stamp(clock())} tick watchdog: stopped after {limit_s:.0f}s", file=sys.stderr, flush=True)
        _kill_children()
        _record(rt.store if rt else None, settings, "tick", "watchdog")
        note(f"stopped by the watchdog after {limit_s:.0f}s", ok=False)
    except Exception as e:  # noqa: BLE001 - exit 0 so launchd keeps the 60 s schedule
        print(f"{_stamp(clock())} tick failed: {_reason(e)}", file=sys.stderr, flush=True)
        note(type(e).__name__, ok=False)
    _check_alert(rt, clock, notifier)
    return 0


# --------------------------------------------------------------------------
# Other commands
# --------------------------------------------------------------------------

def cmd_insights(settings: Settings, runtime_factory, clock, force: bool, notifier=None) -> int:
    began = _time.monotonic()
    with try_lock(insights_lock_path(settings), blocking=True):  # waits for a tick's short deliver_pending
        rt = None
        try:
            rt = runtime_factory(settings)
            items = run_insights(rt.slack, rt.llm, rt.store, settings, clock(), force=force)
            more = deliver_pending(rt.slack, rt.store, settings, clock())  # retries a failed DM send once more
        except SlackPaused:
            return _stopped_by_pause(clock, "insights")
        except Exception as e:  # noqa: BLE001
            reason = _reason(e)
            print(f"{_stamp(clock())} insights failed: {reason}", file=sys.stderr, flush=True)
            _record(rt.store if rt is not None else None, settings, "insights", reason)
            _check_alert(rt, clock, notifier)
            note(type(e).__name__, ok=False)
            return 1
        _check_alert(rt, clock, notifier)
    decks = _deck_step(rt, clock(), None)  # R44: every deck, once the text is out and the insights lock is free
    took = _time.monotonic() - began
    delivered = list({it.id: it for it in [*items, *more]}.values())
    print(f"{_stamp(clock())} insights ok: {len(delivered)} delivered{' (forced)' if force else ''}{decks} "
          f"({took:.0f}s)", flush=True)
    note(f"{len(delivered)} delivered{' (forced)' if force else ''}{decks}")
    for it in delivered:
        score = f"{it.score:.1f}" if it.score is not None else "—"
        print(f"  {it.id} {it.status} score={score} wrote={it.models.get('wrote') or '—'} "
              f"judged={it.models.get('judged') or '—'}", flush=True)
    return 0


def cmd_decks(settings: Settings, runtime_factory, clock, retry: bool = False) -> int:
    """The detached deck process the tick spawns: every pending deck and attach, under data/decks.lock. With `retry`
    (the dashboard's "Build decks"), today's decks that gave up get one more try first."""
    with try_lock(decks_lock_path(settings)) as got:
        if not got:
            print(f"{_stamp(clock())} decks: another deck process is running", flush=True)
            note("another deck process is running")
            return 0
        rt = None
        reset = 0
        try:
            rt = runtime_factory(settings)
            if retry:
                reset = retry_given_up(rt.store, settings, clock())
            n = rt.decks(clock(), None) if rt.decks is not None else 0
        except SlackPaused:
            return _stopped_by_pause(clock, "decks")
        except Exception as e:  # noqa: BLE001
            reason = _reason(e)
            print(f"{_stamp(clock())} decks failed: {reason}", file=sys.stderr, flush=True)
            _record(rt.store if rt is not None else None, settings, "decks", reason)
            note(type(e).__name__, ok=False)
            return 1
    again = f" ({reset} given-up deck(s) retried)" if retry else ""
    print(f"{_stamp(clock())} decks ok: {n} deck(s){again}", flush=True)
    note(f"{n} deck(s) uploaded{again}")
    return 0


def cmd_history_import(settings: Settings, runtime_factory, clock, max_calls: int, sleep_fn=_time.sleep) -> int:
    """One-time, resumable import of the channel's history. Takes the tick lock (never overlaps a tick) and prints
    counts only, never message text."""
    from .jobs.history_import import run_import  # lazy: the job imports this module's window helpers
    with try_lock(history_import_lock_path(settings)) as mine:  # its own lock, for the dashboard's "is it running"
        if not mine:
            print("history-import: another import is running", flush=True)
            note("another import is running")
            return 0
        with try_lock(tick_lock_path(settings)) as got:
            if not got:
                print("a tick is running — try again in a minute", flush=True)
                note("a tick is running — try again in a minute", ok=False)
                return 1
            rt = None
            try:
                rt = runtime_factory(settings)
                with events.step(rt.store, "history_import", "import"):
                    res = run_import(rt.slack, rt.store, settings, clock, sleep_fn, max_calls=max_calls)
            except Exception as e:  # noqa: BLE001
                print(f"history-import failed: {type(e).__name__}", file=sys.stderr, flush=True)
                note(type(e).__name__, ok=False)
                return 1
            finally:
                if rt is not None:
                    rt.store.close()
    print(f"history-import: calls={res.calls} new messages={res.messages_new} threads read={res.threads_done} "
          f"finished={'yes' if res.finished else 'no'} stopped={res.stopped_reason or 'n/a'}", flush=True)
    note(f"{res.messages_new} new message(s)" + (f", stopped: {res.stopped_reason}" if res.stopped_reason else "")
         + (", finished" if res.finished else ""))
    return 1 if res.stopped_reason not in (None, "huddle window", "call cap") else 0  # a Slack failure shows


def _sweep_line(res) -> str:
    return (f"history-sweep: calls={res.calls} new messages={res.messages_new} queued={res.queued} "
            f"stopped={res.stopped_reason or 'n/a'}")


def _refresh_line(res) -> str:
    return (f"history-refresh: calls={res.calls} threads read={res.threads_done} new messages={res.messages_new} "
            f"dropped={res.dropped} pending={res.pending} stopped={res.stopped_reason or 'n/a'}")


def cmd_history_refresh(settings: Settings, runtime_factory, clock, max_calls: int, sleep_fn=None) -> int:
    """D15: read the threads that got new replies since the last look (the nightly run does this too). Takes the
    tick lock (never overlaps a tick), like history-import, and prints counts only, never message text."""
    from .jobs.history_import import run_thread_refresh  # lazy: the job imports this module's window helpers
    with try_lock(tick_lock_path(settings)) as got:
        if not got:
            print("a tick is running — try again in a minute", flush=True)
            return 1
        rt = None
        try:
            rt = runtime_factory(settings)
            res = run_thread_refresh(rt.slack, rt.store, settings, clock, sleep_fn or _time.sleep, max_calls=max_calls)
        except Exception as e:  # noqa: BLE001
            print(f"history-refresh failed: {type(e).__name__}", file=sys.stderr, flush=True)
            return 1
        finally:
            if rt is not None:
                rt.store.close()
    print(_refresh_line(res), flush=True)
    return 1 if res.stopped_reason not in (None, "huddle window", "call cap") else 0  # a Slack failure shows


def cmd_history_enrich(settings: Settings, runtime_factory, clock, max_items: int, sleep_fn=None) -> int:
    """First one read of the last 3 days of the channel (D15a), then the threads that got new replies (D15: at most 10
    a night, paced), then label the leftover "other" messages and recompute topics, so the new replies are labelled
    the same night. Takes its own lock (the nightly spawn is detached from the tick) and prints counts only, never
    message text."""
    with try_lock(enrich_lock_path(settings)) as got:
        if not got:
            print("history-enrich: another run is in progress", flush=True)
            note("another run is in progress")
            return 0
        rt = None
        try:
            rt = runtime_factory(settings)
            with events.step(rt.store, "enrich", "enrich"):
                res = _enrich_steps(rt, settings, clock, max_items, sleep_fn)
        except Exception as e:  # noqa: BLE001
            print(f"history-enrich failed: {type(e).__name__}", file=sys.stderr, flush=True)
            note(type(e).__name__, ok=False)
            return 1
        finally:
            if rt is not None:
                rt.store.close()
    print(f"history-enrich: labelled={res.labelled} topics={res.topics} stopped={res.stopped_reason or 'n/a'}",
          flush=True)
    note(f"labelled {res.labelled}, {res.topics} topic(s)"
         + (f", stopped: {res.stopped_reason}" if res.stopped_reason else ""))
    return 0


def _enrich_steps(rt: Runtime, settings: Settings, clock, max_items: int, sleep_fn):
    """The three steps of `history-enrich`, in order: the 3-day sweep, the thread refresh, then the labels and topics.
    The first two never stop the third (the night's labels do not wait on Slack)."""
    from .jobs import history_import  # lazy: the job imports this module's window helpers
    from .jobs.enrich import run_enrich
    from .jobs.history_import import run_thread_refresh
    try:
        swept = history_import.sweep_recent(rt.slack, rt.store, settings, clock, sleep_fn or _time.sleep)
        print(_sweep_line(swept), flush=True)
    except Exception as e:  # noqa: BLE001 - the night's refresh and labels do not wait on Slack
        print(f"history-sweep failed: {type(e).__name__}", file=sys.stderr, flush=True)
    try:
        refreshed = run_thread_refresh(rt.slack, rt.store, settings, clock, sleep_fn or _time.sleep)
        print(_refresh_line(refreshed), flush=True)
    except Exception as e:  # noqa: BLE001 - the night's labels do not wait on Slack
        print(f"history-refresh failed: {type(e).__name__}", file=sys.stderr, flush=True)
    return run_enrich(rt.store, settings, rt.llm, clock, max_items=max_items)


def cmd_weekly(settings: Settings, runtime_factory, clock) -> int:
    """Send the weekly metrics to Haseeb's self-DM. Reads the store only; nothing goes to the channel.

    It leaves no "this week was sent" marker, so a run from the dashboard can never stop Friday's scheduled one. Its
    own lock (waited for, never skipped) lets the dashboard tell that it is running; a scheduled run that meets a
    dashboard run waits for it and then sends too (a second copy of a numbers-only DM is harmless)."""
    with try_lock(weekly_lock_path(settings), blocking=True):
        rt = None
        try:
            rt = runtime_factory(settings)
            today = clock().astimezone(settings.tz).date()  # R4
            week_start = today - timedelta(days=today.weekday())
            stats = week_stats(rt.store, week_start, workdays=settings.workdays, today=today)
            with events.step(rt.store, "weekly", "send"):
                send_dm(rt.slack, rt.store, settings, render_weekly(stats))  # recorded as own:{ts} (R23)
            notify(settings, "weekly", "📊 Weekly report ready")
        except SlackPaused:
            return _stopped_by_pause(clock, "weekly")
        except Exception as e:  # noqa: BLE001
            reason = _reason(e)
            print(f"{_stamp(clock())} weekly failed: {reason}", file=sys.stderr, flush=True)
            _record(rt.store if rt is not None else None, settings, "weekly", reason)
            note(type(e).__name__, ok=False)
            return 1
    print(f"{_stamp(clock())} weekly ok: week of {week_start.isoformat()}, {stats.posted} posted", flush=True)
    note(f"week of {week_start.isoformat()}, {stats.posted} posted")
    return 0


def cmd_slack_check(settings: Settings, runtime_factory, clock) -> int:
    """Read-only: which Slack account will the agent post as? Asks the connector for the connected account's own
    profile (no message is sent, unlike `smoke`) and records cursor `slack_identity` = {user_id, ok, at, error} for
    the dashboard: ok when the user id is Haseeb's. The profile itself is never printed, logged or stored (it holds an
    email); a profile with no readable id is recorded as a failure, never a pass. Exits 1 unless it is Haseeb's."""
    with try_lock(slack_check_lock_path(settings)) as got:
        if not got:
            print(f"{_stamp(clock())} slack-check: another check is running", flush=True)
            note("another check is running")
            return 0
        rt = None
        at = clock()
        user_id: str | None = None
        problem: str | None = None
        try:
            rt = runtime_factory(settings)
            with events.step(rt.store, "slack_check", "check"):
                user_id, problem = profile_user_id(rt.slack.read_profile())
        except Exception as e:  # noqa: BLE001
            problem = f"could not read the profile ({type(e).__name__})"
        ok = problem is None and user_id == settings.haseeb_id
        try:
            if rt is not None:
                controls.record_identity(rt.store, user_id, ok, at, problem)
            else:
                store = Store(settings.db_path)
                try:
                    controls.record_identity(store, user_id, ok, at, problem)
                finally:
                    store.close()
        except Exception as e:  # noqa: BLE001
            print(f"slack-check: could not record the result: {type(e).__name__}", file=sys.stderr, flush=True)
        finally:
            if rt is not None:
                rt.store.close()
    if ok:
        print(f"{_stamp(clock())} slack-check ok: the agent posts as you ({user_id})", flush=True)
        note("posting as you")
        return 0
    if problem is not None:
        print(f"{_stamp(clock())} slack-check failed: {problem}", file=sys.stderr, flush=True)
        note(problem, ok=False)
    else:
        print(f"{_stamp(clock())} slack-check: WARNING, the agent would post as {user_id}, not as you "
              f"({settings.haseeb_id})", file=sys.stderr, flush=True)
        note(f"posting as another account ({user_id})", ok=False)
    return 1


def cmd_feedback(settings: Settings, runtime_factory, clock, ts: str) -> int:
    """Draft feedback for one chosen channel post (the dashboard's "Draft feedback") and send it to the DM as an
    approvable item, like the tick does for a post it finds. Refuses Haseeb's own posts and a post that already has a
    feedback item (exit 1, the reason on stderr). One draft at a time, under its own lock."""
    with try_lock(feedback_lock_path(settings)) as got:
        if not got:
            print(f"{_stamp(clock())} feedback: another draft is running", flush=True)
            note("another draft is running")
            return 0
        rt = None
        try:
            rt = runtime_factory(settings)
            iid = draft_one(rt.slack, rt.llm, rt.store, settings, ts, clock())
        except FeedbackRefused as e:
            print(f"{_stamp(clock())} feedback refused: {e}", file=sys.stderr, flush=True)
            note(f"refused: {e}", ok=False)
            return 1
        except SlackPaused:
            return _stopped_by_pause(clock, "feedback")  # the draft is not kept: the click is made again after Resume
        except Exception as e:  # noqa: BLE001
            reason = _reason(e)
            print(f"{_stamp(clock())} feedback failed: {reason}", file=sys.stderr, flush=True)
            _record(rt.store if rt is not None else None, settings, "feedback", reason)
            note(type(e).__name__, ok=False)
            return 1
        finally:
            if rt is not None:
                rt.store.close()
    print(f"{_stamp(clock())} feedback ok: {iid} is in your DM", flush=True)
    note("draft sent to your DM")
    return 0


def cmd_usage_backfill(settings: Settings, clock, transcripts: Path | None = None, skip_proxy: bool = False,
                       http=None) -> int:
    """One-time, safe to repeat: the usage history from before the Usage tab (usage_backfill.py). Reads Claude's session
    transcripts and asks the company proxy for its spend logs, and prints counts only. It needs no runtime: no Slack, no
    model. The proxy is the one real outside call, behind the offline guard."""
    from . import usage_backfill  # lazy: nothing else needs it
    store = Store(settings.db_path)
    try:
        return usage_backfill.run(store, settings, clock(), folder=transcripts, skip_proxy=skip_proxy, http=http)
    except Exception as e:  # noqa: BLE001
        print(f"usage-backfill failed: {type(e).__name__}", file=sys.stderr, flush=True)
        return 1
    finally:
        store.close()


def cmd_watch_dry_run(settings: Settings, runtime_factory, clock) -> int:
    """Read-only preview of the thread watch (`haseeb-agent watch --dry-run`): the real channel and thread reads, then one line
    for each reply the watch would draft for or sort (thread ts, reply ts, kind, the author's Slack id), and a line of counts.
    No draft, DM or reaction, no writing or sorting model, no store write by the watch, and no text of any message is printed. Its
    Slack reads are real: the connector's headless Haiku runs, one for the channel and up to 15 for threads (16 in all, on the
    shared Claude quota), and, as on any read, the gateway may write its `new_tools_alert:` cursor. It runs while paused too."""
    from .jobs import followback
    rt = None
    try:
        rt = runtime_factory(settings)
        shown = followback.preview(rt.slack, rt.store, settings, clock())
    except Exception as e:  # noqa: BLE001
        print(f"{_stamp(clock())} watch dry-run failed: {_reason(e)}", file=sys.stderr, flush=True)
        return 1
    finally:
        if rt is not None:
            rt.store.close()
    if shown.page_failed:
        print(f"{_stamp(clock())} watch dry-run failed: the channel could not be read", file=sys.stderr, flush=True)
        return 1
    print(f"{_stamp(clock())} watch dry-run: nothing is drafted, sent or reacted to, and no text is printed; it reads Slack for real "
          f"(up to {followback.PREVIEW_MAX_READS + 1} headless Haiku runs, on the shared quota)", flush=True)
    for c in shown.candidates:
        print(f"eligible thread={c.thread_ts} reply={c.reply_ts} kind={c.kind} author={c.author_id}", flush=True)
    read = f" ({', '.join(shown.read)})" if shown.read else ""
    print(f"{_stamp(clock())} watch dry-run: {shown.watched} thread(s) watched, {shown.scanned} in the scan, {shown.with_news} "
          f"with a reply of the last 2 days, {len(shown.read)} read{read}"
          + (f", {shown.not_read} not read" if shown.not_read else "") + f", {len(shown.candidates)} eligible"
          + (" (the channel page was full: threads older than its oldest message are not shown)" if shown.page_full else ""),
          flush=True)
    return 0


def cmd_smoke(settings: Settings, runtime_factory) -> int:
    try:
        rt = runtime_factory(settings)
        ts = send_dm(rt.slack, rt.store, settings, SMOKE_TEXT)  # recorded as own:{ts} (R23)
        rt.slack.react(settings.self_dm_channel, ts, "eyes")
        page = rt.slack.read_channel(settings.self_dm_id, str(Decimal(ts) - 1))
    except Exception as e:  # noqa: BLE001
        print(f"smoke failed: {_reason(e)}", file=sys.stderr, flush=True)
        return 1
    if not any(m.ts == ts for m in page):
        print(f"smoke: sent {ts} but could not read it back", file=sys.stderr, flush=True)
        return 1
    print(f"smoke ok ts={ts}", flush=True)
    return 0


def _counts(problems: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(problems.items()))


MAX_SHOWN_TOKENS = 10
MAX_TOKEN_CHARS = 40


def cmd_deck_test(settings: Settings, runtime_factory, item_id: str, keep: bool = False) -> int:
    """R49: build a designer deck for one insight and upload it to its self-DM thread, whatever deck_designer says.
    The refused tokens go to Haseeb's terminal (stderr) only, at most 10 of 40 chars: never a log, Slack or the DB."""
    try:
        rt = runtime_factory(settings)
        res, uploaded = deck_test(rt.slack, rt.store, settings, item_id, **({"keep": True} if keep else {}))
    except Exception as e:  # noqa: BLE001
        print(f"deck-test failed: {_reason(e)}", file=sys.stderr, flush=True)
        return 1
    if res.reason == "ok":
        check = "passed" + (f" after the repair turn (first refused: {_counts(res.first_problems)})"
                            if res.repaired else "")
    elif res.reason == "fact-check":
        check = f"refused ({_counts(res.problems)})" + (
            f", first refused: {_counts(res.first_problems)}" if res.first_problems else "")
    else:
        check = f"not reached ({res.reason})"
    print(f"deck-test {item_id}: {res.reason}\n"
          f"  model: {res.model or '—'}\n"
          f"  time: {res.seconds:.0f} s\n"
          f"  pages: {res.pages}\n"
          f"  images: {res.images}\n"
          f"  generated images: {res.generated}\n"
          f"  fact check: {check}\n"
          f"  repaired: {'yes' if res.repaired else 'no'}\n"
          f"  {'uploaded to the self-DM thread' if uploaded else 'not uploaded'}"
          + (f"\n  kept: {res.work}" if res.work else ""), flush=True)
    shown = res.refused[:MAX_SHOWN_TOKENS]
    if shown:
        print("deck-test refused tokens (terminal only):", file=sys.stderr)
        for check, kind, token in shown:
            print(f'  refused ({check}) {kind}: "{token[:MAX_TOKEN_CHARS]}"', file=sys.stderr)
        if len(res.refused) > len(shown):
            print(f"  ... and {len(res.refused) - len(shown)} more", file=sys.stderr)
        sys.stderr.flush()
    return 0 if uploaded else 1


def _ts_arg(value: str) -> str:
    if not controls.valid_ts(value):
        raise argparse.ArgumentTypeError("a Slack message ts like 1760000000.000100")
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="haseeb-agent", description="Haseeb's Agent")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("tick", help="run the jobs that are due (launchd, every 60 s)")
    ins = sub.add_parser("insights", help="research, write and deliver today's insights")
    ins.add_argument("--force", action="store_true", help="ignore the workday and already-ran guards")
    dk = sub.add_parser("decks", help="build and upload pending insight decks (spawned by the tick)")
    dk.add_argument("--retry", action="store_true", help="give today's decks that gave up one more try first")
    sub.add_parser("weekly", help="send the weekly metrics to the self-DM (launchd, Fri 17:00)")
    sub.add_parser("smoke", help="send a test message to the self-DM and read it back")
    sub.add_parser("slack-check", help="read-only: which Slack account would the agent post as? (records the answer "
                                       "for the dashboard; sends nothing)")
    wa = sub.add_parser("watch", help="read-only preview of the thread watch: which replies in watched threads would get a draft")
    wa.add_argument("--dry-run", action="store_true", required=True,
                    help="the only mode: drafts, sends and reacts to nothing and prints no text, but reads Slack for real "
                         "(up to 16 headless Haiku runs, on the shared quota)")
    fb = sub.add_parser("feedback", help="draft feedback for one channel post and send it to your DM")
    fb.add_argument("--ts", required=True, type=_ts_arg, help="the post's ts, e.g. 1760000000.000100")
    hi = sub.add_parser("history-import", help="import the channel's past messages for the dashboard (resumable)")
    hi.add_argument("--max-calls", type=int, default=150, help="claude calls this run may make (default 150)")
    he = sub.add_parser("history-enrich", help="read threads with new replies, label leftover messages and group them "
                                               "into topics (nightly)")
    he.add_argument("--max-items", type=int, default=300, help="messages that may get a model label (default 300)")
    hr = sub.add_parser("history-refresh", help="read the threads that got new replies since the last look (paced)")
    hr.add_argument("--max-calls", type=int, default=10, help="threads this run may read (default 10)")
    dt = sub.add_parser("deck-test", help="build a designer deck for one insight and upload it to its DM thread")
    dt.add_argument("item_id", help="the insight's id, e.g. insight-2026-10-17-1")
    dt.add_argument("--keep", action="store_true", help="keep the work dir, its copied images and the PDF")
    ub = sub.add_parser("usage-backfill", help="one-time, safe to repeat: add the usage history from before the Usage tab "
                                               "(Claude's session transcripts, and the company proxy's spend logs if it has them)")
    ub.add_argument("--transcripts", type=Path, help="the folder of Claude session transcripts "
                                                     "(default: ~/.claude/projects/-Users-<you>--haseeb-agent-claude-home)")
    ub.add_argument("--skip-proxy", action="store_true", help="Claude's history only: make no call to the company proxy")
    dash = sub.add_parser("dashboard", help="the local dashboard (127.0.0.1 only)")
    dsub = dash.add_subparsers(dest="dashboard_command", required=True)
    dsub.add_parser("serve", help="run the dashboard server (launchd keeps it alive)")
    dsub.add_parser("open", help="print a single-use login URL (valid 60 s) and open it in the browser")
    dsub.add_parser("rotate-key", help="replace the dashboard key, restart the server and log every session out")
    return p


def main(argv: list[str] | None = None, *, runtime_factory=build_runtime, jobs_factory=default_jobs,
         clock: Callable[[], datetime] | None = None, notifier=None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Our INFO lines are counts and research-derived titles (never Slack text or keys); libraries stay at WARNING.
    logging.getLogger("haseeb_agent").setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore"):  # httpx logs request URLs at INFO; a Slack upload URL holds a signed token
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except Exception as e:  # noqa: BLE001
        print(f"cannot load settings: {_reason(e)}", file=sys.stderr, flush=True)
        return 1
    clock = clock or (lambda: datetime.now(settings.tz))  # R4: timezone-aware
    if args.command == "dashboard":
        from .dashboard import app as dashboard_app
        if args.dashboard_command == "serve":
            return dashboard_app.serve(settings)
        if args.dashboard_command == "rotate-key":
            return dashboard_app.rotate_dashboard_key()
        return dashboard_app.open_dashboard(settings)
    if args.command == "tick":  # pauses itself (run_tick) and records itself (cmd_tick)
        return cmd_tick(settings, runtime_factory, jobs_factory, clock, notifier=notifier)
    if args.command == "watch":  # read-only, so it runs while paused too
        return cmd_watch_dry_run(settings, runtime_factory, clock)
    if args.command == "slack-check":  # read-only, so it runs while paused too
        return run_recorded(settings, "slack_check", clock, lambda: cmd_slack_check(settings, runtime_factory, clock))
    if args.command in PAUSE_GATED and (refused := _refuse_when_paused(settings, args.command, clock)) is not None:
        return refused

    def recorded(fn: Callable[[], int]) -> int:
        return run_recorded(settings, controls.COMMAND_JOB[args.command], clock, fn)
    if args.command == "insights":
        return recorded(lambda: cmd_insights(settings, runtime_factory, clock, args.force, notifier=notifier))
    if args.command == "usage-backfill":  # reads files and the proxy's logs: no job, no Slack, so it is not paused either
        return cmd_usage_backfill(settings, clock, args.transcripts, args.skip_proxy)
    if args.command == "smoke":
        with usage.session(settings.db_path, "slack"):
            return cmd_smoke(settings, runtime_factory)
    if args.command == "decks":
        return recorded(lambda: cmd_decks(settings, runtime_factory, clock, args.retry))
    if args.command == "feedback":
        return recorded(lambda: cmd_feedback(settings, runtime_factory, clock, args.ts))
    if args.command == "history-import":
        return recorded(lambda: cmd_history_import(settings, runtime_factory, clock, args.max_calls))
    if args.command == "history-enrich":
        return recorded(lambda: cmd_history_enrich(settings, runtime_factory, clock, args.max_items))
    if args.command == "history-refresh":
        with usage.session(settings.db_path, "history"):
            return cmd_history_refresh(settings, runtime_factory, clock, args.max_calls)
    if args.command == "deck-test":
        with usage.session(settings.db_path, "deck_test"):
            return cmd_deck_test(settings, runtime_factory, args.item_id, args.keep)
    return recorded(lambda: cmd_weekly(settings, runtime_factory, clock))


if __name__ == "__main__":
    sys.exit(main())
