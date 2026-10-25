"""Run controls, shared by the CLI and the dashboard (2026-10-20): the pause switch, the allowlist of jobs the
dashboard may start, the "is it running" probe, and the record a finished job leaves for its control.

Pause. The cursor `agent_paused` holds the ISO time the agent was paused, or "" once resumed (cleared, not deleted).
Anything non-empty counts as paused, so an unreadable value fails closed. Three things enforce it: the tick skips every job,
every job command refuses at start (cli.main), and the Slack gateway refuses every write (slack_gateway). Reads stay
allowed. A paused tick also expires the dashboard clicks queued before the pause and cancels a pending Next Todo
auto-post, database only (cli.pause_housekeeping).

One run per job. A job counts as running while its lock is held (a non-blocking flock probe), or for 2 minutes after
the dashboard spawned it, unless the job has ended since (`job_last:<name>` is newer than the spawn): the same idea as
the tick's `decks_spawned_at`, which covers the moments before the new process has taken its lock.

Nothing here stores message text. A job's summary is a count or a fixed phrase, a failure is an exception class name.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from .config import Settings
from .store import Store

PAUSE_KEY = "agent_paused"
IDENTITY_KEY = "slack_identity"
LAST_PREFIX = "job_last:"
SPAWNED_PREFIX = "job_spawned:"
SPAWN_WINDOW = timedelta(minutes=2)  # the same window as cli.DECKS_SPAWN_EVERY
SUMMARY_MAX = 160
TS_RE = re.compile(r"[0-9]{10}\.[0-9]{6}")  # a Slack message ts: ASCII digits only (\d takes any Unicode digit); fullmatch


@dataclass(frozen=True)
class Job:
    name: str                      # the API's name for it, also the suffix of its cursors
    lock: str | None               # the lock file in the data folder that the job's process holds while it runs
    argv: tuple[str, ...] | None   # after `python -m haseeb_agent.cli`: fixed, never client-supplied. None: the tick
    takes_ts: bool = False         # the one client-supplied value: a post's ts (feedback)


# The allowlist. The order is the order of the dashboard's controls.
JOBS: tuple[Job, ...] = (
    Job("tick", "tick.lock", None),
    Job("insights", "insights.lock", ("insights", "--force")),
    Job("decks", "decks.lock", ("decks", "--retry")),
    Job("weekly", "weekly.lock", ("weekly",)),
    Job("history_import", "history_import.lock", ("history-import",)),
    Job("enrich", "enrich.lock", ("history-enrich",)),
    Job("slack_check", "slack_check.lock", ("slack-check",)),
    Job("feedback", "feedback.lock", ("feedback",), takes_ts=True),
)
BY_NAME: dict[str, Job] = {j.name: j for j in JOBS}

# The CLI command that runs each job (the key of cli.main's dispatch) -> its job: the first word after `haseeb-agent` in
# the job's fixed argv, and the tick, which is kicked and not spawned, runs as `tick`.
COMMAND_JOB: dict[str, str] = {(j.argv[0] if j.argv else j.name): j.name for j in JOBS}


def valid_ts(ts: object) -> bool:
    return isinstance(ts, str) and TS_RE.fullmatch(ts) is not None


def spawn_args(job: Job, ts: str | None = None) -> list[str]:
    """What the dashboard runs for `job`, after `python -m haseeb_agent.cli`. The ts is the only client value, and it
    must be a ts."""
    if job.argv is None:
        raise ValueError(f"{job.name} is kicked, not spawned")
    if job.takes_ts != (ts is not None):
        raise ValueError(f"{job.name} {'needs' if job.takes_ts else 'takes no'} ts")
    if ts is not None and not valid_ts(ts):
        raise ValueError("not a ts")
    return [*job.argv, *(["--ts", ts] if ts is not None else [])]


def lock_path(settings: Settings, job: Job) -> Path | None:
    return None if job.lock is None else Path(settings.db_path).parent / job.lock


def lock_held(path: Path | None) -> bool:
    """True while another process holds the flock on `path`. A non-blocking probe: it takes the lock for an instant
    when it is free, and never creates the file (a lock that was never taken is not held)."""
    if path is None:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# The pause switch
# --------------------------------------------------------------------------

def paused_since(store: Store) -> str | None:
    """When the agent was paused (the cursor's text), or None. Raises what the store raises: callers that enforce
    the pause decide what an unreadable store means."""
    value = store.get_cursor(PAUSE_KEY)
    return value if value else None


def set_paused(store: Store, paused: bool, now: datetime) -> str | None:
    """Pause or resume; returns the pause time afterwards. Pausing twice keeps the first time."""
    current = paused_since(store)
    if paused:
        if current is not None:
            return current
        store.set_cursor(PAUSE_KEY, now.isoformat())
        return now.isoformat()
    if current is not None:
        store.set_cursor(PAUSE_KEY, "")
    return None


def paused_since_db(settings: Settings) -> str | None:
    """`paused_since` on a short-lived connection of its own (a job command before it builds its runtime)."""
    store = Store(settings.db_path)
    try:
        return paused_since(store)
    finally:
        store.close()


# --------------------------------------------------------------------------
# Records: what a finished job leaves, when it was spawned, whose Slack it posts as
# --------------------------------------------------------------------------

def _dt(value: object, tz: tzinfo | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz) if tz is not None else None
    return dt


def _json(raw: str | None) -> dict | None:
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def clean_summary(text: str) -> str:
    """One short line: whitespace collapsed, at most SUMMARY_MAX characters."""
    text = " ".join(str(text).split())
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"


def record_last(store: Store, name: str, at: datetime, ok: bool, summary: str) -> None:
    store.set_cursor(f"{LAST_PREFIX}{name}", json.dumps({"at": at.isoformat(), "ok": bool(ok),
                                                         "summary": clean_summary(summary)}))


def read_last(store: Store, name: str) -> dict | None:
    """`{at, ok, summary}` of the job's last finished run, or None."""
    data = _json(store.get_cursor(f"{LAST_PREFIX}{name}"))
    if data is None or not isinstance(data.get("at"), str) or not isinstance(data.get("ok"), bool):
        return None
    summary = data.get("summary")
    return {"at": data["at"], "ok": data["ok"], "summary": summary if isinstance(summary, str) else ""}


def mark_spawned(store: Store, name: str, at: datetime, arg: str | None = None) -> None:
    store.set_cursor(f"{SPAWNED_PREFIX}{name}", json.dumps({"at": at.isoformat(), "arg": arg}))


def clear_spawned(store: Store, name: str) -> None:
    store.set_cursor(f"{SPAWNED_PREFIX}{name}", "")


def read_spawned(store: Store, name: str, tz: tzinfo | None = None) -> tuple[datetime, str | None] | None:
    data = _json(store.get_cursor(f"{SPAWNED_PREFIX}{name}"))
    at = _dt(data.get("at"), tz) if data else None
    if at is None:
        return None
    arg = data.get("arg")
    return at, arg if isinstance(arg, str) else None


def record_identity(store: Store, user_id: str | None, ok: bool, at: datetime, error: str | None) -> None:
    """The result of `slack-check`: never the profile itself (it holds an email)."""
    store.set_cursor(IDENTITY_KEY, json.dumps({"user_id": user_id, "ok": bool(ok), "at": at.isoformat(),
                                               "error": clean_summary(error) if error else None}))


def read_identity(store: Store) -> dict | None:
    data = _json(store.get_cursor(IDENTITY_KEY))
    if data is None:
        return None
    uid, at, error = data.get("user_id"), data.get("at"), data.get("error")
    return {"user_id": uid if isinstance(uid, str) else None, "ok": data.get("ok") is True,
            "at": at if isinstance(at, str) else None, "error": error if isinstance(error, str) else None}


# --------------------------------------------------------------------------
# Is it running?
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class JobState:
    running: bool
    started_at: str | None   # when the dashboard spawned the run that is going on, if it did
    last: dict | None        # read_last
    arg: str | None = None   # the ts a running feedback job drafts for


def job_state(store: Store, settings: Settings, job: Job, now: datetime) -> JobState:
    last = read_last(store, job.name)
    spawned = read_spawned(store, job.name, settings.tz)
    ended = _dt(last["at"], settings.tz) if last else None
    # A spawn the job has not ended since, and that is still recent: the process may not hold its lock yet.
    pending = (spawned is not None and timedelta(0) <= now - spawned[0] < SPAWN_WINDOW
               and not (ended is not None and ended >= spawned[0]))
    held = lock_held(lock_path(settings, job))
    running = held or pending
    from_spawn = running and spawned is not None and not (ended is not None and ended >= spawned[0])
    return JobState(running, spawned[0].isoformat() if from_spawn else None, last,
                    spawned[1] if from_spawn else None)
