import fcntl
import json
import logging
import os
import plistlib
import shutil
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import cli, controls
from haseeb_agent.cli import (
    JOBS,
    Runtime,
    TickState,
    due,
    main,
    run_tick,
    tick_state,
    try_lock,
)
from haseeb_agent.config import load_settings
from haseeb_agent.jobs.todo import item_id as todo_item_id
from haseeb_agent.models import Item
from haseeb_agent.slack_gateway import SlackPaused
from haseeb_agent.store import Store
from tests.fakes import FakeSlack, PausedSlack

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")
MONDAY = date(2026, 10, 19)
SUNDAY = date(2026, 10, 18)
OPEN = TickState(todo_final=False)
FINAL = TickState(todo_final=True)
OVERTIME = TickState(todo_final=False, huddle_end=cli.OVERTIME_END)  # the huddle runs past 16:05 (2026-10-24)


def at(h, m, s=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=KW)


def ago(now, seconds):
    return now - timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _restore_package_log_level():
    """main() sets the haseeb_agent logger to INFO; don't let that leak into other modules' caplog tests."""
    logger = logging.getLogger("haseeb_agent")
    before = logger.level
    yield
    logger.setLevel(before)


def test_main_logs_package_info_lines(repo):
    main(["weekly"], runtime_factory=fake_runtime())
    assert logging.getLogger("haseeb_agent").getEffectiveLevel() == logging.INFO


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)
    return replace(s, db_path=str(tmp_path / "data" / "agent.sqlite"))


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


# --------------------------------------------------------------------------
# R31: per-job cadence
# --------------------------------------------------------------------------

class TestDueBriefCasesUnderR31:
    """The brief's three should_poll cases, rewritten for R31 (process_dm is the poll they described)."""

    def test_huddle_window_last_poll_61s_ago(self, settings):
        now = at(14, 40)
        assert due("process_dm", now, ago(now, 61), settings, OPEN) is True

    def test_workday_daytime_last_poll_120s_ago(self, settings):
        now = at(11, 0)
        assert due("process_dm", now, ago(now, 120), settings, OPEN) is False

    def test_sunday_last_poll_600s_ago(self, settings):
        now = at(11, 0, day=SUNDAY)
        assert due("process_dm", now, ago(now, 600), settings, OPEN) is False


class TestProcessDm:
    @pytest.mark.parametrize("h,m,elapsed,expected", [
        (11, 0, 300, True),     # workday 08:00–20:00: every 300 s
        (11, 0, 200, False),
        (8, 0, 300, True),      # 08:00 opens the daytime cadence
        (7, 59, 300, False),    # before 08:00: 1800 s
        (7, 59, 1800, True),
        (19, 59, 300, True),
        (20, 0, 300, False),    # 20:00 closes it
        (20, 0, 1800, True),
        (14, 24, 61, False),    # the 60 s window starts at 14:25
        (14, 25, 61, True),
        (16, 4, 61, True),      # ...and runs to 16:05
        (16, 5, 61, False),
        (16, 5, 300, True),
    ])
    def test_workday(self, settings, h, m, elapsed, expected):
        now = at(h, m)
        assert due("process_dm", now, ago(now, elapsed), settings, OPEN) is expected

    @pytest.mark.parametrize("h,m", [(11, 0), (14, 40), (21, 0)])
    def test_weekend_is_1800s_all_day(self, settings, h, m):
        now = at(h, m, day=SUNDAY)
        assert due("process_dm", now, ago(now, 1700), settings, OPEN) is False
        assert due("process_dm", now, ago(now, 1800), settings, OPEN) is True

    def test_deliver_pending_follows_process_dm(self, settings):
        for now, elapsed in [(at(14, 40), 61), (at(11, 0), 120), (at(11, 0), 300), (at(11, 0, day=SUNDAY), 600)]:
            assert due("deliver_pending", now, ago(now, elapsed), settings, OPEN) == \
                due("process_dm", now, ago(now, elapsed), settings, OPEN)


class TestRunTodo:
    def test_huddle_window_every_60s_while_open(self, settings):
        now = at(15, 10)
        assert due("run_todo", now, ago(now, 61), settings, OPEN) is True

    def test_final_todo_falls_back_to_process_dm_daytime_cadence(self, settings):
        now = at(15, 10)
        assert due("run_todo", now, ago(now, 61), settings, FINAL) is False
        assert due("run_todo", now, ago(now, 300), settings, FINAL) is True

    def test_late_autopost_still_covered_until_1605(self, settings):
        # an auto-post due at 16:02 needs a run after it
        now = at(16, 3)
        assert due("run_todo", now, ago(now, 61), settings, OPEN) is True

    def test_overtime_keeps_the_60s_poll_after_1605(self, settings):
        now = at(16, 30)
        assert due("run_todo", now, ago(now, 61), settings, OVERTIME) is True
        assert due("process_dm", now, ago(now, 61), settings, OVERTIME) is True
        assert due("run_todo", now, ago(now, 61), settings, OPEN) is False  # no overtime: process_dm's 300 s
        assert due("run_todo", at(18, 0), ago(at(18, 0), 61), settings, OVERTIME) is False  # it ends at 18:00

    def test_outside_window_follows_process_dm(self, settings):
        now = at(11, 0)
        assert due("run_todo", now, ago(now, 120), settings, OPEN) is False
        assert due("run_todo", now, ago(now, 300), settings, OPEN) is True

    def test_weekend_1800s_even_in_huddle_hours(self, settings):
        now = at(15, 10, day=SUNDAY)
        assert due("run_todo", now, ago(now, 61), settings, OPEN) is False
        assert due("run_todo", now, ago(now, 1800), settings, OPEN) is True


class TestRunFeedback:
    @pytest.mark.parametrize("now,elapsed,expected", [
        (at(11, 0), 500, False),
        (at(11, 0), 600, True),     # workday 08:00–20:00: every 600 s
        (at(15, 0), 61, False),     # no 60 s window for feedback
        (at(21, 0), 600, False),    # workday evening: 1800 s
        (at(21, 0), 1800, True),
        (at(11, 0, day=SUNDAY), 600, False),
        (at(11, 0, day=SUNDAY), 1800, True),
    ])
    def test_cadence(self, settings, now, elapsed, expected):
        assert due("run_feedback", now, ago(now, elapsed), settings, OPEN) is expected


class TestR33FeedbackYieldsToTheHuddle:
    def test_skipped_in_huddle_window_while_todo_open(self, settings):
        now = at(15, 0)
        assert due("run_feedback", now, ago(now, 3600), settings, OPEN) is False
        assert due("run_feedback", now, None, settings, OPEN) is False

    def test_runs_once_the_todo_is_final(self, settings):
        now = at(15, 0)
        assert due("run_feedback", now, ago(now, 600), settings, FINAL) is True

    def test_still_skipped_in_the_overtime(self, settings):
        now = at(16, 30)
        assert due("run_feedback", now, ago(now, 3600), settings, OVERTIME) is False
        assert due("run_feedback", now, ago(now, 3600), settings, OPEN) is True

    @pytest.mark.parametrize("h,m", [(14, 24), (16, 5)])
    def test_runs_at_the_window_edges(self, settings, h, m):
        now = at(h, m)
        assert due("run_feedback", now, ago(now, 600), settings, OPEN) is True

    def test_weekend_unaffected(self, settings):
        now = at(15, 0, day=SUNDAY)
        assert due("run_feedback", now, ago(now, 1800), settings, OPEN) is True

    def test_tick_skips_feedback_and_keeps_its_cursor(self, settings, store):
        store.set_cursor("tick_last:run_feedback", at(13, 0).isoformat())
        calls = []
        clock = Clock(at(15, 0))
        assert run_tick(settings, store, spy_jobs(calls), clock=clock) == ["process_dm", "deliver_pending", "run_todo"]
        assert store.get_cursor("tick_last:run_feedback") == at(13, 0).isoformat()
        clock.now = at(16, 5)  # first eligible tick after the window
        assert "run_feedback" in run_tick(settings, store, spy_jobs(calls), clock=clock)


class TestDueEdges:
    def test_never_run_is_due(self, settings):
        for job in JOBS:
            assert due(job, at(11, 0, day=SUNDAY), None, settings, FINAL) is True

    def test_launchd_jitter_a_few_seconds_early_still_counts(self, settings):
        now = at(14, 40)
        assert due("process_dm", now, ago(now, 57), settings, OPEN) is True
        assert due("process_dm", now, ago(now, 30), settings, OPEN) is False

    def test_last_run_in_the_future_is_due(self, settings):
        # the clock went backwards: never stall until the clock catches up
        now = at(11, 0)
        assert due("run_feedback", now, now + timedelta(hours=2), settings, OPEN) is True

    def test_now_in_another_zone_is_read_in_kuwait_time(self, settings):
        now = at(14, 40).astimezone(UTC)  # 11:40 UTC
        assert due("process_dm", now, ago(now, 61), settings, OPEN) is True

    def test_naive_now_rejected(self, settings):
        with pytest.raises(ValueError):
            due("process_dm", datetime(2026, 10, 19, 14, 40), None, settings, OPEN)

    def test_unknown_job_rejected(self, settings):
        with pytest.raises(KeyError):
            due("weekly", at(11, 0), None, settings, OPEN)


# --------------------------------------------------------------------------
# Is today's todo final
# --------------------------------------------------------------------------

def _todo(status, day=MONDAY):
    return Item(id=todo_item_id(day), kind="todo", day=day, status=status, body="Next Todo:\n1. x", title="t",
                dm_ts=None, source_ts=None, score=None, models={}, attempts=0, edited=False,
                created_at=at(15, 0, day=day), acted_at=None)


class TestTickState:
    def test_no_item_is_open(self, store, settings):
        assert tick_state(store, settings, at(15, 0)).todo_final is False

    @pytest.mark.parametrize("status,final", [
        ("drafted", False), ("delivered", False), ("posted", True), ("skipped", True), ("failed", True),
        ("expired", True),
    ])
    def test_status(self, store, settings, status, final):
        store.upsert_item(_todo(status))
        assert tick_state(store, settings, at(15, 0)).todo_final is final

    def test_the_window_runs_to_1800_while_a_long_huddle_is_still_running(self, store, settings):
        store.upsert_item(replace(_todo("delivered"), source_ts="1792495719.263309"))  # the pre-draft, no notes yet
        assert tick_state(store, settings, at(16, 30)).huddle_end == cli.OVERTIME_END
        assert tick_state(store, settings, at(15, 30)).huddle_end == cli.HUDDLE[1]  # 16:05 until then
        store.set_cursor(f"notes:{todo_item_id(MONDAY)}", "1792500000.000001")  # the notes are drafted, nothing pending
        assert tick_state(store, settings, at(16, 30)).huddle_end == cli.HUDDLE[1]
        store.set_cursor(f"autopost:{todo_item_id(MONDAY)}", at(16, 31).isoformat())  # an auto-post still to fire
        assert tick_state(store, settings, at(16, 30)).huddle_end == cli.OVERTIME_END
        store.upsert_item(replace(_todo("posted"), source_ts="1792495719.263309"))
        assert tick_state(store, settings, at(16, 30)) == TickState(todo_final=True)  # final: back to 16:05

    def test_no_huddle_sent_is_final(self, store, settings):
        store.set_cursor(f"nohuddle:{todo_item_id(MONDAY)}", at(15, 25).isoformat())
        assert tick_state(store, settings, at(15, 30)).todo_final is True

    def test_yesterdays_final_todo_does_not_count(self, store, settings):
        store.upsert_item(_todo("posted", day=MONDAY - timedelta(days=1)))
        assert tick_state(store, settings, at(15, 0)).todo_final is False

    def test_day_is_kuwait_day(self, store, settings):
        store.upsert_item(_todo("posted"))
        # 22:30 UTC Sunday is 01:30 Monday in Kuwait
        now = datetime(2026, 10, 18, 22, 30, tzinfo=UTC)
        assert tick_state(store, settings, now).todo_final is True


# --------------------------------------------------------------------------
# The tick: order, isolation, cursors, logging
# --------------------------------------------------------------------------

class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


def spy_jobs(calls, fail=()):
    def make(name):
        def run(now):
            calls.append((name, now))
            if name in fail:
                raise RuntimeError(f"{name} exploded")
            return f"{name} done"
        return run
    return {name: make(name) for name in JOBS}


class TestRunTick:
    def test_order_when_all_due(self, settings, store, capsys):
        calls = []
        ran = run_tick(settings, store, spy_jobs(calls), clock=Clock(at(11, 0)))
        assert ran == ["process_dm", "deliver_pending", "run_todo", "run_feedback"]
        assert [c[0] for c in calls] == ran
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 4 and all(" ok " in line for line in out)

    def test_records_tick_last_and_respects_it(self, settings, store):
        calls = []
        clock = Clock(at(11, 0))
        run_tick(settings, store, spy_jobs(calls), clock=clock)
        assert store.get_cursor("tick_last:process_dm") == at(11, 0).isoformat()
        calls.clear()
        clock.now = at(11, 5)  # 300 s later: DM and todo are due again, feedback (600 s) is not
        assert run_tick(settings, store, spy_jobs(calls), clock=clock) == ["process_dm", "deliver_pending", "run_todo"]
        calls.clear()
        clock.now = at(11, 5, 20)
        assert run_tick(settings, store, spy_jobs(calls), clock=clock) == []

    def test_one_job_failure_does_not_stop_others(self, settings, store, capsys):
        calls = []
        ran = run_tick(settings, store, spy_jobs(calls, fail={"process_dm", "run_todo"}), clock=Clock(at(11, 0)))
        assert [c[0] for c in calls] == ["process_dm", "deliver_pending", "run_todo", "run_feedback"]
        assert ran == ["process_dm", "deliver_pending", "run_todo", "run_feedback"]
        runs = {(job, ok): detail for job, ok, detail, _ in store.runs()}
        assert "process_dm exploded" in runs[("process_dm", False)]
        assert "run_todo exploded" in runs[("run_todo", False)]
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        # failures: exactly one stderr line each, and nothing on stdout; successes: one stdout line each
        err = captured.err.strip().splitlines()
        assert len(err) == 2 and "process_dm failed" in err[0] and "run_todo failed" in err[1]
        out = captured.out.strip().splitlines()
        assert [line.split()[1] for line in out] == ["deliver_pending", "run_feedback"]
        # a failed job still counts as run, so a persistent failure does not hammer every tick
        assert store.get_cursor("tick_last:process_dm") == at(11, 0).isoformat()

    def test_failure_reason_is_short(self, settings, store):
        def boom(now):
            raise RuntimeError("x" * 5000)
        jobs = {**spy_jobs([]), "run_feedback": boom}
        run_tick(settings, store, jobs, clock=Clock(at(11, 0)))
        detail = next(d for job, ok, d, _ in store.runs() if job == "run_feedback")
        assert len(detail) <= 240

    def test_corrupt_or_naive_cursor_counts_as_never_run(self, settings, store):
        store.set_cursor("tick_last:process_dm", "2026-10-19T14:39:00")  # naive
        store.set_cursor("tick_last:run_feedback", "garbage")
        calls = []
        ran = run_tick(settings, store, spy_jobs(calls), clock=Clock(at(11, 0)))
        assert "process_dm" in ran and "run_feedback" in ran

    def test_deliver_pending_only_with_process_dm(self, settings, store):
        calls = []
        clock = Clock(at(11, 0))
        store.set_cursor("tick_last:process_dm", at(10, 58).isoformat())        # 120 s ago: not due
        store.set_cursor("tick_last:deliver_pending", at(10, 0).isoformat())    # long ago, but tied to process_dm
        store.set_cursor("tick_last:run_todo", at(10, 58).isoformat())
        store.set_cursor("tick_last:run_feedback", at(10, 0).isoformat())
        assert run_tick(settings, store, spy_jobs(calls), clock=clock) == ["run_feedback"]

    def test_todo_final_slows_todo_in_huddle_window(self, settings, store):
        store.upsert_item(_todo("posted"))
        for job in JOBS:
            store.set_cursor(f"tick_last:{job}", at(15, 9).isoformat())
        calls = []
        assert run_tick(settings, store, spy_jobs(calls), clock=Clock(at(15, 10))) == ["process_dm", "deliver_pending"]

    def test_each_job_gets_a_fresh_now(self, settings, store):
        clock = Clock(at(11, 0))
        seen = []

        def slow(name):
            def run(now):
                seen.append((name, now))
                clock.now = clock.now + timedelta(seconds=30)
                return ""
            return run
        run_tick(settings, store, {n: slow(n) for n in JOBS}, clock=clock)
        assert [s[1] for s in seen] == [at(11, 0), at(11, 0, 30), at(11, 1), at(11, 1, 30)]
        assert all(s[1].tzinfo is not None for s in seen)


# --------------------------------------------------------------------------
# Locks
# --------------------------------------------------------------------------

def hold(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")  # noqa: SIM115 - held open: the caller closes it to release the lock
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


class TestTickLockHeldDuringJobs:
    def test_a_job_sees_the_tick_lock_held(self, repo):
        seen = []

        def probe(now):
            with try_lock(cli.tick_lock_path(load_settings())) as got:
                seen.append(got)
            return ""
        main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: {n: probe for n in JOBS},
             clock=Clock(at(11, 0)))
        assert seen == [False] * 4
        with try_lock(cli.tick_lock_path(load_settings())) as got:
            assert got is True  # released after the tick


class TestWatchdog:
    def _tick(self, jobs, limit_s=0.5, grace_s=5):
        settings = load_settings()
        rc = cli.cmd_tick(settings, fake_runtime(), lambda rt: jobs, Clock(at(11, 0)), limit_s=limit_s, grace_s=grace_s)
        return settings, rc

    def test_hung_job_is_stopped_recorded_and_exits_0(self, repo, capsys):
        import time as t
        began = t.monotonic()
        settings, rc = self._tick({n: (lambda now: t.sleep(30)) for n in JOBS})
        assert rc == 0
        assert t.monotonic() - began < 5
        assert ("tick", False, "watchdog") in [(j, ok, d) for j, ok, d, _ in Store(settings.db_path).runs()]
        assert "watchdog" in capsys.readouterr().err
        with try_lock(cli.tick_lock_path(settings)) as got:
            assert got is True
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)  # disarmed

    def test_hung_subprocess_is_killed(self, repo):
        import time as t
        began = t.monotonic()

        def hang(now):
            subprocess.run(["sleep", "30"], check=False)
        _, rc = self._tick({**spy_jobs([]), "process_dm": hang})
        assert rc == 0
        assert t.monotonic() - began < 5
        left = subprocess.run(["pgrep", "-P", str(os.getpid()), "sleep"], capture_output=True, text=True, check=False)
        assert left.stdout.strip() == ""

    def test_no_watchdog_record_on_a_normal_tick(self, repo):
        settings, rc = self._tick(spy_jobs([]), limit_s=10)
        assert rc == 0
        assert not any(j == "tick" for j, *_ in Store(settings.db_path).runs())
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

    def test_swallowed_timeout_still_hard_exits_0(self, repo):
        script = (
            "import time\n"
            "from datetime import datetime\n"
            "from haseeb_agent import cli\n"
            "from haseeb_agent.config import load_settings\n"
            "from haseeb_agent.store import Store\n"
            "s = load_settings()\n"
            "def stubborn(now):\n"
            "    while True:\n"
            "        try:\n"
            "            time.sleep(10)\n"
            "        except BaseException:\n"
            "            pass\n"
            "rt = lambda st: cli.Runtime(settings=st, store=Store(st.db_path), slack=None, llm=None, handlers={})\n"
            "cli.cmd_tick(s, rt, lambda r: {n: stubborn for n in cli.JOBS}, lambda: datetime.now(s.tz),\n"
            "             limit_s=0.5, grace_s=0.5)\n"
            "print('not reached')\n"
        )
        import sys as _sys
        import time as t
        began = t.monotonic()
        proc = subprocess.run([_sys.executable, "-c", script], cwd=repo, capture_output=True, text=True, timeout=20, check=False)
        assert proc.returncode == 0
        assert "not reached" not in proc.stdout
        assert "hard exit" in proc.stderr
        assert t.monotonic() - began < 10
        with try_lock(repo / "data" / "tick.lock") as got:
            assert got is True  # the exit released the flock


class TestLock:
    def test_try_lock_reports_busy(self, tmp_path):
        held = hold(tmp_path / "x.lock")
        try:
            with try_lock(tmp_path / "x.lock") as got:
                assert got is False
        finally:
            held.close()
        with try_lock(tmp_path / "x.lock") as got:
            assert got is True

    def test_lock_released_after_use(self, tmp_path):
        with try_lock(tmp_path / "x.lock") as got:
            assert got
        with try_lock(tmp_path / "x.lock") as again:
            assert again


# --------------------------------------------------------------------------
# main(): the CLI
# --------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working directory shaped like the repo root, as launchd's WorkingDirectory gives it (R5)."""
    shutil.copy(ROOT / "tests" / "fixtures" / "config.toml", tmp_path / "config.toml")
    (tmp_path / ".env").write_text("LITELLM_API_KEY=k\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def fake_runtime(slack=None):
    def factory(settings):
        return Runtime(settings=settings, store=Store(settings.db_path), slack=slack or FakeSlack(), llm=None,
                       handlers={})
    return factory


class TestMainTick:
    def test_tick_runs_jobs_and_exits_0(self, repo):
        calls = []
        rc = main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls),
                  clock=Clock(at(11, 0)))
        assert rc == 0
        assert [c[0] for c in calls] == list(JOBS)

    def test_tick_lock_second_instance_exits(self, repo):
        held = hold(repo / "data" / "tick.lock")
        built = []
        try:
            rc = main(["tick"], runtime_factory=lambda s: built.append(s), jobs_factory=lambda rt: spy_jobs(built),
                      clock=Clock(at(14, 40)))
        finally:
            held.close()
        assert rc == 0
        assert built == []  # nothing built, nothing run

    def test_tick_exits_0_even_when_every_job_fails(self, repo):
        calls = []
        rc = main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls, fail=set(JOBS)),
                  clock=Clock(at(11, 0)))
        assert rc == 0
        assert len(calls) == 4

    def test_tick_exits_1_when_settings_cannot_load(self, repo, capsys):
        (repo / ".env").write_text("")
        rc = main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs([]),
                  clock=Clock(at(14, 40)))
        assert rc == 1
        assert "LITELLM_API_KEY" in capsys.readouterr().err

    def test_tick_survives_runtime_build_failure(self, repo, capsys):
        def broken(settings):
            raise RuntimeError("cannot build")
        rc = main(["tick"], runtime_factory=broken, jobs_factory=lambda rt: spy_jobs([]), clock=Clock(at(14, 40)))
        assert rc == 0
        assert "cannot build" in capsys.readouterr().err

    def test_weekly_sends_the_report_to_the_self_dm(self, repo, capsys):
        slack = FakeSlack()
        rc = main(["weekly"], runtime_factory=fake_runtime(slack), clock=Clock(at(17, 0, day=date(2026, 10, 23))))
        assert rc == 0
        assert len(slack.sent) == 1
        sent = slack.sent[0]
        assert (sent.channel_id, sent.thread_ts) == ("U08CZCGJW3E", None)
        assert sent.text.startswith("**Haseeb's Agent: weekly report, Mon 19 Oct**")
        assert "Missed days: 5" in sent.text  # empty database: five workdays, nothing posted
        assert Store(repo / "data" / "agent.sqlite").get_cursor(f"own:{sent.ts}") == "1"  # R23
        assert "weekly ok" in capsys.readouterr().out

    def test_weekly_counts_the_week_that_contains_today(self, repo):
        st = Store(repo / "data" / "agent.sqlite")
        st.upsert_item(Item(
            id="insight-2026-10-20-1", kind="insight", day=date(2026, 10, 20), status="posted", body="{}", title="t",
            dm_ts="1.1", source_ts=None, score=90.0, models={}, attempts=0, edited=True,
            created_at=at(8, 30, day=date(2026, 10, 20)), acted_at=None))
        st.close()
        slack = FakeSlack()
        main(["weekly"], runtime_factory=fake_runtime(slack), clock=Clock(at(17, 0, day=date(2026, 10, 23))))
        text = slack.sent[0].text
        assert "Posted: 1 (1 edited first, 0 auto-posted)" in text
        assert "Human intervention: 100%" in text
        assert "Missed days: 4" in text

    def test_weekly_week_boundary_is_the_kuwait_date(self, repo):
        # Sunday 22:00 UTC is already Monday 01:00 in Kuwait, so the report is for the week starting Mon 26 Oct.
        slack = FakeSlack()
        main(["weekly"], runtime_factory=fake_runtime(slack), clock=Clock(datetime(2026, 10, 25, 22, 0, tzinfo=UTC)))
        assert slack.sent[0].text.startswith("**Haseeb's Agent: weekly report, Mon 26 Oct**")
        assert "Missed days: 1" in slack.sent[0].text  # only Monday has started in Kuwait

    def test_weekly_failure_is_recorded_and_exits_1(self, repo, capsys):
        slack = FakeSlack()
        slack.fail_next("boom", method="send")
        rc = main(["weekly"], runtime_factory=fake_runtime(slack), clock=Clock(at(17, 0, day=date(2026, 10, 23))))
        assert rc == 1
        assert "weekly failed" in capsys.readouterr().err
        assert Store(repo / "data" / "agent.sqlite").runs()[0][:2] == ("weekly", False)

    def test_unknown_command_exits_2(self, repo):
        with pytest.raises(SystemExit) as e:
            main(["frobnicate"])
        assert e.value.code == 2


class TestDefaultJobs:
    """The real job wiring, against FakeSlack."""

    def test_tick_with_real_jobs_on_an_empty_workspace(self, repo, capsys):
        slack = FakeSlack()
        rc = main(["tick"], runtime_factory=fake_runtime(slack), clock=Clock(at(11, 0, day=SUNDAY)))
        assert rc == 0
        out = capsys.readouterr().out
        for job in JOBS:
            assert f" {job} ok" in out
        assert slack.sent == []  # nothing to say on an empty Sunday

    def test_deliver_pending_skipped_while_insights_run_holds_its_lock(self, repo):
        settings = load_settings()
        rt = fake_runtime()(settings)
        held = hold(Path(settings.db_path).parent / "insights.lock")
        try:
            assert "skipped" in cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        finally:
            held.close()
        assert "skipped" not in cli.default_jobs(rt)["deliver_pending"](at(11, 0))

    def _decks_rt(self, monkeypatch, pending=True, spawned=None, order=None):
        settings = load_settings()
        rt = fake_runtime()(settings)
        order = order if order is not None else []
        monkeypatch.setattr(cli, "deliver_pending", lambda *a, **k: order.append("text") or [])
        monkeypatch.setattr(cli, "spawn_decks", lambda: (spawned if spawned is not None else order).append("spawn"))
        rt.decks = lambda now, limit: pytest.fail("the tick never builds decks itself")
        rt.decks_pending = lambda now: pending
        return settings, rt

    def test_deliver_job_spawns_the_deck_process_after_the_text(self, repo, monkeypatch):
        """R44 + fix round 1: the tick never builds decks under its lock; it starts `haseeb-agent decks` detached."""
        order = []
        _, rt = self._decks_rt(monkeypatch, order=order)
        out = cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        assert order == ["text", "spawn"] and "decks started" in out

    def test_no_spawn_without_pending_decks(self, repo, monkeypatch):
        spawned = []
        _, rt = self._decks_rt(monkeypatch, pending=False, spawned=spawned)
        cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        assert spawned == []

    def test_never_spawned_twice(self, repo, monkeypatch):
        spawned = []
        settings, rt = self._decks_rt(monkeypatch, spawned=spawned)
        held = hold(cli.decks_lock_path(settings))  # a deck process is running
        try:
            assert "decks running" in cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        finally:
            held.close()
        assert spawned == []
        cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        cli.default_jobs(rt)["deliver_pending"](at(11, 1))  # the child may not hold its lock yet
        assert spawned == ["spawn"]
        cli.default_jobs(rt)["deliver_pending"](at(11, 3))
        assert spawned == ["spawn", "spawn"]

    def test_no_spawn_when_decks_are_disabled(self, repo, monkeypatch):
        from dataclasses import replace
        spawned = []
        settings, rt = self._decks_rt(monkeypatch, spawned=spawned)
        rt.settings = replace(settings, decks_enabled=False)
        cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        assert spawned == []

    def test_a_spawn_failure_never_fails_the_deliver_job(self, repo, monkeypatch, capsys):
        _, rt = self._decks_rt(monkeypatch)
        monkeypatch.setattr(cli, "spawn_decks", lambda: (_ for _ in ()).throw(OSError("no python")))
        out = cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        assert "decks could not start" in out and "no python" in capsys.readouterr().err

    def test_no_deck_step_while_an_insights_run_holds_its_lock(self, repo, monkeypatch):
        spawned = []
        settings, rt = self._decks_rt(monkeypatch, spawned=spawned)
        held = hold(Path(settings.db_path).parent / "insights.lock")
        try:
            assert "skipped" in cli.default_jobs(rt)["deliver_pending"](at(11, 0))
        finally:
            held.close()
        assert spawned == []

    def test_spawn_decks_runs_the_decks_command_detached(self, monkeypatch):
        monkeypatch.delenv("HASEEB_AGENT_OFFLINE")  # subprocess.run is faked below, so nothing real runs
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0)
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        cli.spawn_decks()
        cmd = seen["cmd"]
        assert cmd[1:3] == ["-I", "-c"] and "start_new_session=True" in cmd[3]
        assert cmd[5:] == ["-m", "haseeb_agent.cli", "decks"]
        assert seen["kw"]["stdin"] == subprocess.DEVNULL and seen["kw"]["check"] is True

    def test_decks_command_builds_everything_under_its_lock(self, repo, capsys):
        calls = []

        def factory(settings):
            rt = fake_runtime()(settings)
            rt.decks = lambda now, limit: calls.append(limit) or 2
            return rt
        assert main(["decks"], runtime_factory=factory, clock=Clock(at(11, 0))) == 0
        assert calls == [None] and "2 deck(s)" in capsys.readouterr().out
        held = hold(cli.decks_lock_path(load_settings()))
        try:
            assert main(["decks"], runtime_factory=factory, clock=Clock(at(11, 0))) == 0
        finally:
            held.close()
        assert calls == [None]  # a second process exits at once

    def test_todo_job_gets_a_live_kuwait_clock(self, repo, monkeypatch):
        seen = {}

        def spy(slack, llm, store, settings, now, *, clock=None):
            seen["clock"] = clock
        monkeypatch.setattr(cli, "run_todo", spy)
        rt = fake_runtime()(load_settings())
        cli.default_jobs(rt)["run_todo"](at(15, 18))
        got = seen["clock"]()
        assert got.utcoffset() == timedelta(hours=3) and abs((got - datetime.now(UTC)).total_seconds()) < 5

    def test_runtime_wires_the_self_dm_channel(self, repo, monkeypatch):
        seen = {}

        class Spy:
            def __init__(self, settings, dm_channels=None, **kw):
                seen["dm"] = dm_channels
        monkeypatch.setattr(cli, "ClaudeConnectorSlack", Spy)
        rt = cli.build_runtime(load_settings())
        assert seen["dm"] == {"U08CZCGJW3E": "D08D9HFB8BB"}
        assert set(rt.handlers) == {"insight", "feedback", "todo", "message"}  # message: the composer and `say:`
        assert callable(getattr(rt.handlers["message"], "compose", None))  # the page command `compose`
        assert callable(getattr(rt.handlers["message"], "say", None))  # and `say:` in the DM


class TestSmokeAndInsights:
    def test_smoke_sends_reacts_reads_back(self, repo, capsys):
        slack = FakeSlack()
        assert main(["smoke"], runtime_factory=fake_runtime(slack)) == 0
        assert [s.text for s in slack.sent] == ["🧪 Haseeb's Agent smoke test"]
        ts = slack.sent[0].ts
        assert slack.sent[0].channel_id == "U08CZCGJW3E"
        assert [(r.channel_id, r.ts, r.emoji) for r in slack.reactions] == [("D08D9HFB8BB", ts, "eyes")]
        assert ts in capsys.readouterr().out
        store = Store(load_settings().db_path)
        assert store.get_cursor(f"own:{ts}") == "1"  # R23: never read back as a command

    def test_smoke_fails_when_message_not_read_back(self, repo):
        class Blind(FakeSlack):
            def read_channel(self, channel_id, oldest, limit=50):
                return []
        assert main(["smoke"], runtime_factory=fake_runtime(Blind())) == 1

    def test_insights_force_passes_force_keyword_then_delivers(self, repo, monkeypatch, capsys):
        calls = []

        def fake_run(slack, llm, store, settings, now, research=None, *, force=False, **kw):
            calls.append(("run", force, now.tzinfo is not None))
            return []

        def fake_deliver(slack, store, settings, now):
            calls.append(("deliver",))
            return []
        monkeypatch.setattr(cli, "run_insights", fake_run)
        monkeypatch.setattr(cli, "deliver_pending", fake_deliver)
        assert main(["insights", "--force"], runtime_factory=fake_runtime(), clock=Clock(at(11, 0, day=SUNDAY))) == 0
        assert calls == [("run", True, True), ("deliver",)]
        calls.clear()
        assert main(["insights"], runtime_factory=fake_runtime(), clock=Clock(at(8, 15))) == 0
        assert calls == [("run", False, True), ("deliver",)]

    def test_insights_builds_every_deck_after_delivery(self, repo, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(cli, "run_insights", lambda *a, **k: calls.append("run") or [])
        monkeypatch.setattr(cli, "deliver_pending", lambda *a, **k: calls.append("deliver") or [])

        def factory(settings):
            rt = fake_runtime()(settings)

            def decks(now, limit):
                with cli.try_lock(cli.insights_lock_path(settings)) as free:
                    calls.append(("decks", limit, free))
                return 2
            rt.decks = decks
            return rt
        assert main(["insights"], runtime_factory=factory, clock=Clock(at(8, 15))) == 0
        assert calls == ["run", "deliver", ("decks", None, True)]  # all of them, after the lock is released
        assert "2 deck(s)" in capsys.readouterr().out

    def test_insights_deck_failure_still_exits_0(self, repo, monkeypatch, capsys):
        monkeypatch.setattr(cli, "run_insights", lambda *a, **k: [])
        monkeypatch.setattr(cli, "deliver_pending", lambda *a, **k: [])

        def factory(settings):
            rt = fake_runtime()(settings)
            rt.decks = lambda now, limit: (_ for _ in ()).throw(RuntimeError("codex exploded"))
            return rt
        assert main(["insights"], runtime_factory=factory, clock=Clock(at(8, 15))) == 0
        assert "deck step failed" in capsys.readouterr().err

    def test_insights_runtime_failure_is_recorded(self, repo, capsys):
        def broken(settings):
            raise RuntimeError("cannot build")
        assert main(["insights"], runtime_factory=broken, clock=Clock(at(8, 15))) == 1
        store = Store(load_settings().db_path)
        assert ("insights", False) in [(j, ok) for j, ok, d, _ in store.runs() if "cannot build" in d]

    def test_insights_raising_record_run_does_not_crash(self, repo, monkeypatch, capsys):
        def boom(*a, **kw):
            raise RuntimeError("research exploded")

        def bad_record(self, *a, **kw):
            raise RuntimeError("disk full")
        monkeypatch.setattr(cli, "run_insights", boom)
        monkeypatch.setattr(Store, "record_run", bad_record)
        assert main(["insights"], runtime_factory=fake_runtime(), clock=Clock(at(8, 15))) == 1
        err = capsys.readouterr().err
        assert "research exploded" in err and "could not record" in err

    def test_insights_failure_is_recorded_and_exits_1(self, repo, monkeypatch, capsys):
        def boom(*a, **kw):
            raise RuntimeError("research exploded")
        monkeypatch.setattr(cli, "run_insights", boom)
        assert main(["insights"], runtime_factory=fake_runtime(), clock=Clock(at(8, 15))) == 1
        assert "research exploded" in capsys.readouterr().err
        store = Store(load_settings().db_path)
        assert any(job == "insights" and not ok and "research exploded" in d for job, ok, d, _ in store.runs())


# --------------------------------------------------------------------------
# launchd files (R32: rendered and linted here, never installed)
# --------------------------------------------------------------------------

LAUNCHD = ROOT / "deploy" / "launchd"


def render(name, tmp_path):
    out = tmp_path / "rendered"
    subprocess.run(["bash", str(ROOT / "scripts" / "install-launchd.sh"), "--render-only", str(out)],
                   check=True, capture_output=True, text=True)
    with open(out / name, "rb") as f:
        return plistlib.load(f)


class TestLaunchd:
    def test_scripts_parse(self):
        for script in ("install-launchd.sh", "uninstall-launchd.sh"):
            subprocess.run(["bash", "-n", str(ROOT / "scripts" / script)], check=True)

    def test_tick_plist(self, tmp_path):
        p = render("com.haseeb.agent.tick.plist", tmp_path)
        home = str(Path.home())
        assert p["Label"] == "com.haseeb.agent.tick"
        assert p["StartInterval"] == 60
        assert p["ProgramArguments"] == [f"{home}/.local/bin/uv", "run", "--project", str(ROOT), "haseeb-agent", "tick"]
        assert p["WorkingDirectory"] == str(ROOT)  # R5
        assert p["EnvironmentVariables"]["PATH"].split(":")[:2] == [f"{home}/.local/bin", "/opt/homebrew/bin"]
        assert "/usr/bin" in p["EnvironmentVariables"]["PATH"].split(":")
        assert p["StandardOutPath"].startswith(f"{home}/Library/Logs/haseeb-agent/")
        assert p["StandardErrorPath"].startswith(f"{home}/Library/Logs/haseeb-agent/")

    def test_insights_plist(self, tmp_path):
        p = render("com.haseeb.agent.insights.plist", tmp_path)
        assert p["Label"] == "com.haseeb.agent.insights"
        assert p["ProgramArguments"][-1] == "insights"
        assert sorted((c["Weekday"], c["Hour"], c["Minute"]) for c in p["StartCalendarInterval"]) == \
            [(d, 8, 15) for d in range(1, 6)]
        assert p["WorkingDirectory"] == str(ROOT)
        assert not p.get("RunAtLoad", False)  # loading must never trigger a research run

    def test_weekly_plist(self, tmp_path):
        p = render("com.haseeb.agent.weekly.plist", tmp_path)
        assert p["Label"] == "com.haseeb.agent.weekly"
        assert p["ProgramArguments"][-1] == "weekly"
        assert p["StartCalendarInterval"] == {"Weekday": 5, "Hour": 17, "Minute": 0}  # Fri 17:00
        assert p["WorkingDirectory"] == str(ROOT)  # R5
        assert not p.get("RunAtLoad", False)  # R32: loading must never send a report

    def test_templates_have_no_leftover_placeholders_once_rendered(self, tmp_path):
        render("com.haseeb.agent.tick.plist", tmp_path)
        for f in (tmp_path / "rendered").iterdir():
            assert "__" not in f.read_text()


class TestOutageAlertWiring:
    def test_build_runtime_wraps_the_gateway_for_outage_counting(self, repo):
        from haseeb_agent.alert import SlackHealth
        rt = cli.build_runtime(load_settings())
        assert isinstance(rt.slack, SlackHealth)
        assert all(h.slack is rt.slack for h in rt.handlers.values())

    def test_insights_checks_for_an_alert_request(self, repo, monkeypatch):
        from haseeb_agent.alert import request_alert
        seen = []
        monkeypatch.setattr(cli, "run_insights", lambda *a, **k: [])
        monkeypatch.setattr(cli, "deliver_pending", lambda *a, **k: [])
        factory = fake_runtime()
        rt_store = {}

        def wrapped(settings):
            rt = factory(settings)
            request_alert(rt.store, "x")
            rt_store["s"] = rt.store
            return rt
        assert main(["insights"], runtime_factory=wrapped, clock=Clock(at(8, 20)),
                    notifier=lambda t: seen.append(t)) == 0
        assert len(seen) == 1


class TestInsightsCatchUp:
    """I9: when the Mac was off at 08:15, a tick starts the (not forced) insights run between 08:15 and 13:00."""

    def _deliver(self, repo, now, monkeypatch, spawned):
        monkeypatch.setattr(cli, "spawn_insights", lambda: spawned.append(now))
        rt = fake_runtime()(load_settings())
        return rt, cli.default_jobs(rt)["deliver_pending"](now)

    @pytest.mark.parametrize("h, m, expect", [
        (8, 14, False), (8, 15, True), (10, 0, True), (12, 59, True), (13, 0, False), (19, 0, False),
    ])
    def test_window(self, repo, monkeypatch, h, m, expect):
        spawned = []
        _, out = self._deliver(repo, at(h, m), monkeypatch, spawned)
        assert bool(spawned) is expect
        assert ("catch-up" in out) is expect

    def test_not_on_weekends(self, repo, monkeypatch):
        spawned = []
        self._deliver(repo, at(10, 0, day=SUNDAY), monkeypatch, spawned)
        assert spawned == []

    def test_not_once_insights_ran_today(self, repo, monkeypatch):
        spawned = []
        store = Store(load_settings().db_path)
        store.set_cursor(f"insights_ran:{MONDAY.isoformat()}", at(8, 15).isoformat())
        self._deliver(repo, at(10, 0), monkeypatch, spawned)
        assert spawned == []

    def test_an_emptied_ran_cursor_counts_as_not_ran(self, repo, monkeypatch):
        spawned = []
        Store(load_settings().db_path).set_cursor(f"insights_ran:{MONDAY.isoformat()}", "")  # I8: models were down
        self._deliver(repo, at(10, 0), monkeypatch, spawned)
        assert len(spawned) == 1

    def test_not_while_the_insights_lock_is_held(self, repo, monkeypatch):
        spawned = []
        held = hold(Path(load_settings().db_path).parent / "insights.lock")
        try:
            _, out = self._deliver(repo, at(10, 0), monkeypatch, spawned)
        finally:
            held.close()
        assert spawned == [] and "skipped" in out

    def test_at_most_once_every_30_minutes(self, repo, monkeypatch):
        spawned = []
        for minute in (0, 5, 10, 29):
            self._deliver(repo, at(10, minute), monkeypatch, spawned)
        assert len(spawned) == 1
        self._deliver(repo, at(10, 30), monkeypatch, spawned)
        assert len(spawned) == 2

    def test_a_research_outage_puts_the_next_catch_up_30_minutes_out(self, repo, monkeypatch):
        """launchd's 08:15 run is no catch-up, so the throttle knew no previous try and the tick at 08:20 retried: the
        research tries were used up by 08:50. A research outage now starts the 30 minutes itself, through the cursor
        the tick's own stamp uses, so the real deliver job first spawns the catch-up at 08:45."""
        from haseeb_agent.claude_cli import ClaudeResult
        from haseeb_agent.jobs.insights import run_insights
        spawned = []
        monkeypatch.setattr(cli, "spawn_insights", lambda: spawned.append(1))
        settings = load_settings()
        rt = fake_runtime()(settings)

        def down(prompt, **kw):
            return ClaudeResult("", [], 0.0, False, "claude exit 1: Claude is down")
        assert run_insights(rt.slack, None, rt.store, settings, at(8, 15), research=down) == []  # launchd's run
        job = cli.default_jobs(rt)["deliver_pending"]
        for minute in (16, 20, 25, 44):
            job(at(8, minute))
        assert spawned == []
        job(at(8, 45))
        assert len(spawned) == 1

    def test_spawn_failure_does_not_fail_delivery(self, repo, monkeypatch):
        def boom():
            raise OSError("no python")
        monkeypatch.setattr(cli, "spawn_insights", boom)
        rt = fake_runtime()(load_settings())
        out = cli.default_jobs(rt)["deliver_pending"](at(10, 0))
        assert "delivered" in out

    def test_spawn_runs_the_unforced_insights_command_detached(self, monkeypatch):
        monkeypatch.delenv("HASEEB_AGENT_OFFLINE")  # subprocess.run is faked below, so nothing real runs
        # N4: through a short-lived intermediate (`python -I -c <detach> <argv>`) that starts the run in a new
        # session and exits, so the run is reparented to launchd and is no longer the tick's child.
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0)
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        cli.spawn_insights()
        cmd = seen["cmd"]
        assert cmd[1:3] == ["-I", "-c"] and "start_new_session=True" in cmd[3]
        assert cmd[-1] == "insights" and "--force" not in cmd
        assert cmd[5:7] == ["-m", "haseeb_agent.cli"]
        assert seen["kw"]["stdin"] == subprocess.DEVNULL and seen["kw"]["check"] is True

    def test_n4_watchdog_kill_leaves_the_catch_up_run_alive(self, repo, tmp_path):
        """A real subprocess: the tick spawns the catch-up, then hangs; the watchdog fires and kills the tick's
        children. The catch-up is not one of them (it was reparented), so it keeps running."""
        import time as t
        pidfile = tmp_path / "catchup.pid"
        child = [sys.executable, "-c",
                 f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(30)"]
        direct = []

        def job(now):
            cli.spawn_insights(child)
            direct.append(subprocess.Popen(["sleep", "30"]))  # a plain child, which the watchdog must kill
            t.sleep(30)
        settings = load_settings()
        rc = cli.cmd_tick(settings, fake_runtime(), lambda rt: {**spy_jobs([]), "deliver_pending": job},
                          Clock(at(11, 0)), limit_s=1.5, grace_s=5)
        assert rc == 0
        deadline = t.monotonic() + 5
        while not (pidfile.exists() and pidfile.read_text()) and t.monotonic() < deadline:
            t.sleep(0.05)
        pid = int(pidfile.read_text())
        try:
            assert direct[0].wait(timeout=5) is not None  # the tick's own child was killed
            t.sleep(0.3)
            os.kill(pid, 0)  # still alive: raises ProcessLookupError otherwise
            ppid = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
            assert int(ppid) != os.getpid()
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def test_tick_plist_keeps_the_spawned_run_alive(self, tmp_path):
        p = render("com.haseeb.agent.tick.plist", tmp_path)
        assert p["AbandonProcessGroup"] is True


class TestWeeklyBanner:
    def test_banner_after_the_weekly_dm(self, repo, banners):
        rc = main(["weekly"], runtime_factory=fake_runtime(FakeSlack()), clock=Clock(at(17, 0, day=date(2026, 10, 23))))
        assert rc == 0
        assert [(b.kind, b.message, b.sound) for b in banners] == [("weekly", "📊 Weekly report ready", False)]

    def test_no_banner_when_the_weekly_dm_fails(self, repo, banners):
        slack = FakeSlack()
        slack.fail_next("down", method="send")
        rc = main(["weekly"], runtime_factory=fake_runtime(slack), clock=Clock(at(17, 0, day=date(2026, 10, 23))))
        assert rc == 1 and list(banners) == []

    def test_smoke_raises_no_banner(self, repo, banners):
        main(["smoke"], runtime_factory=fake_runtime(FakeSlack()))
        assert list(banners) == []


def test_build_runtime_gives_the_gateway_the_store_for_the_once_a_day_alert(settings):
    """R42: the new-tools alert is limited to once a day through the store, across the short-lived processes."""
    from haseeb_agent.cli import build_runtime
    rt = build_runtime(settings)
    assert rt.slack.inner._store is rt.store


class TestUnreachableDayClose:
    """The tick's deliver job closes a day on which the writing models stayed down past the catch-up window."""

    def test_the_catch_up_window_ends_where_the_day_closes(self):
        from haseeb_agent.jobs.insights import CATCH_UP_END
        assert cli.CATCH_UP[1] == CATCH_UP_END

    def test_deliver_job_sends_the_final_line_once(self, repo, banners):
        settings = load_settings()
        rt = fake_runtime()(settings)
        day = at(10, 0).date().isoformat()
        rt.store.set_cursor(f"insights_down:{day}", at(8, 20).isoformat())  # the models were down this morning
        job = cli.default_jobs(rt)["deliver_pending"]
        job(at(12, 59))
        assert rt.slack.sent == []
        out = job(at(13, 1))
        assert [m.text for m in rt.slack.sent] == ["⚠️ No ideas today: models stayed unreachable"]
        assert "day closed" in out
        job(at(13, 31))
        assert len(rt.slack.sent) == 1
        assert banners.messages == ["⚠️ No ideas today — see DM"]


def test_http_client_loggers_are_pinned_to_warning(repo):
    """httpx logs every request URL at INFO, and a Slack upload URL carries a signed token (Task 14 fix round 1)."""
    import logging
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    main(["decks"], runtime_factory=fake_runtime(), clock=Clock(at(11, 0)))
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


class TestDeckTest:
    def test_prints_the_outcome_and_exits_0_after_an_upload(self, repo, monkeypatch, capsys):
        from haseeb_agent.deck.designer import DesignResult
        seen = {}

        def fake(slack, store, settings, item_id):
            seen["id"] = item_id
            return DesignResult(Path("x.pdf"), "gpt-6.1-sol", "ok", pages=10, images=3, seconds=151.0,
                                repaired=True, first_problems={"html:name": 1}), True
        monkeypatch.setattr(cli, "deck_test", fake)
        assert main(["deck-test", "insight-2026-10-17-1"], runtime_factory=fake_runtime()) == 0
        out = capsys.readouterr().out
        assert seen["id"] == "insight-2026-10-17-1"
        for part in ("model: gpt-6.1-sol", "time: 151 s", "pages: 10", "images: 3", "fact check: passed",
                     "html:name=1", "repaired: yes", "uploaded"):
            assert part in out, part

    def test_a_failed_design_exits_1_and_says_why(self, repo, monkeypatch, capsys):
        from haseeb_agent.deck.designer import DesignResult
        monkeypatch.setattr(cli, "deck_test", lambda *a: (DesignResult(None, None, "fact-check", seconds=200.0,
                                                                       problems={"name": 2}), False))
        assert main(["deck-test", "insight-2026-10-17-1"], runtime_factory=fake_runtime()) == 1
        out = capsys.readouterr().out
        assert "fact check: refused (name=2)" in out and "repaired: no" in out and "not uploaded" in out

    def test_refused_tokens_go_to_stderr_only_capped_and_truncated(self, repo, monkeypatch, capsys, tmp_path):
        from haseeb_agent.deck.designer import DesignResult
        refused = (("first html", "name", "Xyz"),) + tuple(("html", "number", f"{i}" * 50) for i in range(12))
        seen = {}

        def fake(*a, **k):
            seen.update(k)
            return DesignResult(None, None, "fact-check", seconds=234.0, problems={"name": 1}, generated=3,
                                refused=refused, work=tmp_path / "w"), False
        monkeypatch.setattr(cli, "deck_test", fake)
        assert main(["deck-test", "insight-2026-10-17-1", "--keep"], runtime_factory=fake_runtime()) == 1
        cap = capsys.readouterr()
        assert seen == {"keep": True}
        assert "generated images: 3" in cap.out and f"kept: {tmp_path / 'w'}" in cap.out
        assert 'refused (first html) name: "Xyz"' in cap.err and "Xyz" not in cap.out
        lines = [x for x in cap.err.splitlines() if x.startswith("  refused")]
        assert len(lines) == 10 and all(len(x.split(": ", 1)[1]) <= 42 for x in lines)  # 40 chars and quotes

    def test_without_keep_nothing_is_passed(self, repo, monkeypatch, capsys):
        from haseeb_agent.deck.designer import DesignResult
        seen = []
        monkeypatch.setattr(cli, "deck_test", lambda *a, **k: seen.append(k) or (DesignResult(None, None, "exit"), False))
        main(["deck-test", "insight-2026-10-17-1"], runtime_factory=fake_runtime())
        assert seen == [{}] and "kept:" not in capsys.readouterr().out

    def test_an_error_exits_1(self, repo, monkeypatch, capsys):
        monkeypatch.setattr(cli, "deck_test", lambda *a: (_ for _ in ()).throw(ValueError("no item nope")))
        assert main(["deck-test", "nope"], runtime_factory=fake_runtime()) == 1
        assert "no item nope" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Run controls (2026-10-20): pause, the record a job leaves, the new commands
# --------------------------------------------------------------------------

PAUSED_AT = "2026-10-20T10:00:00+03:00"
TS1 = "1760000000.000100"


def repo_store(repo):
    return Store(repo / "data" / "agent.sqlite")


def pause(repo):
    st = repo_store(repo)
    controls.set_paused(st, True, datetime.fromisoformat(PAUSED_AT))
    st.close()


def last_of(repo, job):
    st = repo_store(repo)
    try:
        return controls.read_last(st, job)
    finally:
        st.close()


def no_runtime(settings):
    raise AssertionError("a paused command must not even build its runtime")


class NoSlack:
    """A gateway that fails the test on any use: what a paused tick does is the database only."""

    def __getattr__(self, name):
        raise AssertionError(f"a paused tick called Slack ({name})")


class TestPause:
    def test_a_paused_tick_records_its_heartbeat_and_runs_no_job(self, repo, capsys):
        pause(repo)
        calls = []
        rc = main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls),
                  clock=Clock(at(11, 0)))
        assert rc == 0 and calls == []
        st = repo_store(repo)
        assert st.get_cursor("tick_seen") == at(11, 0).isoformat()  # the dashboard still sees the Mac awake
        assert st.cursors("tick_last:") == {}  # nothing ran, so nothing is "last run"
        assert f"tick paused since {PAUSED_AT}: no job ran" in capsys.readouterr().out
        assert last_of(repo, "tick") == {"at": at(11, 0).isoformat(), "ok": True, "summary": "paused: no job ran"}

    def test_a_paused_tick_runs_no_page_job(self, repo):
        """No dashboard click runs while paused; the page refuses new ones. (What was queued before is expired: below.)"""
        pause(repo)
        st = repo_store(repo)
        st.enqueue_page_command("i1", "post", None, "0" * 64, datetime.fromisoformat("2026-10-20T10:00:05+03:00"))
        calls = []
        jobs = {**spy_jobs(calls), cli.PAGE_JOB: lambda now: calls.append(("page", now)) or ""}
        assert run_tick(load_settings(), st, jobs, clock=Clock(at(11, 0, day=date(2026, 10, 20)))) == []
        assert calls == [] and [c.state for c in st.recent_page_commands(5)] == ["queued"]  # no sooner than Resume

    def test_a_paused_tick_expires_the_clicks_queued_before_the_pause(self, repo, capsys):
        """M2a: the kill switch also kills an armed action. A click queued before the pause would otherwise run as the
        first thing of the first tick after Resume, with no new confirmation."""
        pause(repo)  # 2026-10-20 10:00 +03:00
        st = repo_store(repo)
        before = st.enqueue_page_command("i1", "post", None, "0" * 64, datetime.fromisoformat("2026-10-20T09:55:00+03:00"))
        long_before = st.enqueue_page_command("i2", "skip", None, "0" * 64, at(11, 0))  # a naive-looking old one
        after = st.enqueue_page_command("i3", "post", None, "0" * 64, datetime.fromisoformat("2026-10-20T10:00:05+03:00"))
        calls = []
        rc = main(["tick"], runtime_factory=fake_runtime(NoSlack()), jobs_factory=lambda rt: spy_jobs(calls),
                  clock=Clock(at(10, 5, day=date(2026, 10, 20))))
        assert rc == 0 and calls == []
        for cid in (before, long_before):
            c = repo_store(repo).page_command(cid)
            assert (c.state, c.result) == ("failed", "expired: queued before the pause")
        assert repo_store(repo).page_command(after).state == "queued"  # made after the pause time (a click in flight)
        assert f"tick paused since {PAUSED_AT}: no job ran (2 queued click(s) expired)" in capsys.readouterr().out
        assert last_of(repo, "tick")["summary"] == "paused: no job ran (2 queued click(s) expired)"
        main(["tick"], runtime_factory=fake_runtime(NoSlack()), jobs_factory=lambda rt: spy_jobs(calls),
             clock=Clock(at(10, 6, day=date(2026, 10, 20))))  # a second paused tick finds nothing more to do
        assert last_of(repo, "tick")["summary"] == "paused: no job ran"

    def test_an_expired_click_does_not_run_after_resume(self, repo):
        pause(repo)
        st = repo_store(repo)
        cid = st.enqueue_page_command("i1", "post", None, "0" * 64, datetime.fromisoformat("2026-10-20T09:55:00+03:00"))
        main(["tick"], runtime_factory=fake_runtime(NoSlack()), jobs_factory=lambda rt: spy_jobs([]),
             clock=Clock(at(10, 5, day=date(2026, 10, 20))))
        controls.set_paused(st, False, at(10, 6, day=date(2026, 10, 20)))
        from haseeb_agent.page_commands import page_due
        assert page_due(st) is False and st.page_command(cid).state == "failed"  # nothing is left for the first tick

    def test_resume_brings_the_jobs_back(self, repo):
        pause(repo)
        calls = []
        main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls), clock=Clock(at(11, 0)))
        assert calls == []
        st = repo_store(repo)
        controls.set_paused(st, False, at(11, 1))
        st.close()
        main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls), clock=Clock(at(11, 1)))
        assert [c[0] for c in calls] == list(JOBS)

    @pytest.mark.parametrize("argv,job", [
        (["insights"], "insights"), (["insights", "--force"], "insights"), (["decks"], "decks"),
        (["decks", "--retry"], "decks"), (["weekly"], "weekly"), (["history-import"], "history_import"),
        (["history-enrich"], "enrich"), (["feedback", "--ts", TS1], "feedback"), (["history-refresh"], None),
        (["smoke"], None), (["deck-test", "insight-2026-10-17-1"], None),
    ])
    def test_every_job_command_refuses_at_start_while_paused(self, repo, capsys, argv, job):
        pause(repo)
        rc = main(argv, runtime_factory=no_runtime, clock=Clock(at(11, 0)))
        cap = capsys.readouterr()
        assert rc == 0 and cap.out == ""  # exit 0: skipped, not failed
        lines = cap.err.strip().splitlines()
        assert len(lines) == 1 and f"{argv[0]}: the agent is paused since {PAUSED_AT}" in lines[0]
        assert "nothing was run" in lines[0]
        if job:  # the control shows the skip as the last run
            assert last_of(repo, job) == {"at": at(11, 0).isoformat(), "ok": True, "summary": "skipped: paused"}
        else:
            assert repo_store(repo).cursors("job_last:") == {}

    def test_an_unreadable_pause_state_fails_closed(self, repo, monkeypatch, capsys):
        monkeypatch.setattr(controls, "paused_since_db", lambda s: (_ for _ in ()).throw(OSError("disk")))
        assert main(["weekly"], runtime_factory=no_runtime, clock=Clock(at(11, 0))) == 1
        assert "cannot read the pause state (OSError); not running" in capsys.readouterr().err

    def test_slack_check_reads_only_so_it_runs_while_paused(self, repo, capsys):
        pause(repo)
        slack = FakeSlack()
        assert main(["slack-check"], runtime_factory=fake_runtime(slack), clock=Clock(at(11, 0))) == 0
        assert slack.sent == [] and "slack-check ok" in capsys.readouterr().out

    def test_not_paused_runs_as_before(self, repo):
        st = repo_store(repo)
        controls.set_paused(st, True, at(10, 0))
        controls.set_paused(st, False, at(10, 5))  # paused once, resumed
        st.close()
        slack = FakeSlack()
        assert main(["weekly"], runtime_factory=fake_runtime(slack), clock=Clock(at(17, 0, day=date(2026, 10, 23)))) == 0
        assert len(slack.sent) == 1


class TestPausedMidRun:
    """M1: the pause landed while a run was going, and the gateway refused a write before any call (SlackPaused). Nothing
    was sent, so the run did not fail: no `runs` row (the weekly "Failed job runs" count it), and its control reads ok
    with "stopped: paused", like a run the pause refused at the start."""

    FRIDAY = Clock(at(17, 0, day=date(2026, 10, 23)))

    def _stopped(self, repo, job, capsys, rc):
        assert rc == 0
        assert last_of(repo, job) == {"at": self.FRIDAY().isoformat() if job == "weekly" else at(11, 0).isoformat(),
                                      "ok": True, "summary": "stopped: paused"}
        assert repo_store(repo).runs() == []  # not a failed run
        assert f"{job}: the agent was paused; stopped, nothing more was sent" in capsys.readouterr().err

    def test_weekly(self, repo, capsys):
        slack = PausedSlack()
        rc = main(["weekly"], runtime_factory=fake_runtime(slack), clock=self.FRIDAY)
        self._stopped(repo, "weekly", capsys, rc)
        assert slack.sent == [] and slack.refused == ["send"]

    def test_feedback(self, repo, capsys):
        from tests.test_feedback import FakeLLM
        slack = PausedSlack()
        slack.paused = False  # the post is read while running; the pause lands before the draft's DM
        TestFeedbackCommand()._seed(repo, slack=slack)
        slack.paused = True
        rc = main(["feedback", "--ts", TS1], runtime_factory=TestFeedbackCommand()._rt(slack, FakeLLM()),
                  clock=Clock(at(11, 0)))
        self._stopped(repo, "feedback", capsys, rc)
        st = repo_store(repo)
        assert st.get_item(f"feedback-{TS1}") is None and st.get_cursor(f"feedback_claim:{TS1}") is None  # free to redo

    def test_decks(self, repo, capsys):
        def factory(settings):
            rt = fake_runtime()(settings)
            rt.decks = lambda now, limit: (_ for _ in ()).throw(SlackPaused("slack_get_file_upload_url: paused"))
            return rt
        rc = main(["decks"], runtime_factory=factory, clock=Clock(at(11, 0)))
        self._stopped(repo, "decks", capsys, rc)

    def test_insights(self, repo, capsys, monkeypatch):
        monkeypatch.setattr(cli, "run_insights", lambda *a, **k: (_ for _ in ()).throw(SlackPaused("send: paused")))
        rc = main(["insights", "--force"], runtime_factory=fake_runtime(), clock=Clock(at(11, 0)))
        self._stopped(repo, "insights", capsys, rc)

    def test_a_tick_job(self, repo, capsys):
        """The job is stopped, not failed; the rest of the tick ends there (every further write is refused too)."""
        calls = []

        def stopped(now):
            calls.append("process_dm")
            raise SlackPaused("send: paused")
        jobs = {**spy_jobs(calls), "process_dm": stopped}
        rc = main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: jobs, clock=Clock(at(11, 0)))
        assert rc == 0 and calls == ["process_dm"]
        assert "process_dm stopped: the agent was paused" in capsys.readouterr().out
        assert repo_store(repo).runs() == []
        assert last_of(repo, "tick") == {"at": at(11, 0).isoformat(), "ok": True,
                                         "summary": "1 job(s) ran, stopped by the pause"}


class TestJobLast:
    """Each spawned job command records job_last:<name> = {at, ok, summary} when it ends, a failure included."""

    FRIDAY = Clock(at(17, 0, day=date(2026, 10, 23)))

    def test_a_good_run(self, repo):
        assert main(["weekly"], runtime_factory=fake_runtime(), clock=self.FRIDAY) == 0
        assert last_of(repo, "weekly") == {"at": self.FRIDAY().isoformat(), "ok": True,
                                           "summary": "week of 2026-10-19, 0 posted"}

    def test_a_failed_run(self, repo):
        slack = FakeSlack()
        slack.fail_next("boom", method="send")
        assert main(["weekly"], runtime_factory=fake_runtime(slack), clock=self.FRIDAY) == 1
        assert last_of(repo, "weekly") == {"at": self.FRIDAY().isoformat(), "ok": False, "summary": "SlackError"}

    def test_a_run_that_dies_still_leaves_its_record(self, repo):
        def killed(settings):
            raise KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            main(["weekly"], runtime_factory=killed, clock=self.FRIDAY)
        assert last_of(repo, "weekly")["ok"] is False and last_of(repo, "weekly")["summary"] == "KeyboardInterrupt"
        assert cli._NOTE.get() is None  # the recorder does not leak into the next command

    def test_the_summary_is_a_count_never_text(self, repo):
        st = repo_store(repo)
        st.upsert_item(Item(id="insight-2026-10-20-1", kind="insight", day=date(2026, 10, 20), status="posted",
                            body="{}", title="SECRET TITLE", dm_ts="1.1", source_ts=None, score=90.0, models={},
                            attempts=0, edited=True, created_at=at(8, 30, day=date(2026, 10, 20)), acted_at=None))
        st.close()
        main(["weekly"], runtime_factory=fake_runtime(), clock=self.FRIDAY)
        assert "SECRET" not in repo_store(repo).get_cursor("job_last:weekly")

    def test_the_tick_records_itself_only_when_it_ran(self, repo):
        calls = []
        assert main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls),
                    clock=Clock(at(11, 0))) == 0
        assert last_of(repo, "tick") == {"at": at(11, 0).isoformat(), "ok": True, "summary": "4 job(s) ran"}
        held = hold(repo / "data" / "tick.lock")  # a tick that finds the lock held did nothing: no new record
        try:
            main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls),
                 clock=Clock(at(11, 30)))
        finally:
            held.close()
        assert last_of(repo, "tick")["at"] == at(11, 0).isoformat()

    def test_a_tick_whose_jobs_fail_is_not_ok_though_it_exits_0(self, repo):
        calls = []
        main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: spy_jobs(calls, fail={"run_todo"}),
             clock=Clock(at(11, 0)))
        assert last_of(repo, "tick") == {"at": at(11, 0).isoformat(), "ok": False, "summary": "4 job(s) ran, 1 failed"}

    def test_a_watchdog_stop_is_a_failed_tick(self, repo):
        def hang(now):
            import time as t
            t.sleep(30)
        settings = load_settings()
        cli.cmd_tick(settings, fake_runtime(), lambda rt: {**spy_jobs([]), "process_dm": hang}, Clock(at(11, 0)),
                     limit_s=0.5, grace_s=5)
        last = last_of(repo, "tick")
        assert last["ok"] is False and last["summary"] == "stopped by the watchdog after 0s"

    def test_weekly_leaves_no_sent_marker_so_it_can_never_stop_fridays_run(self, repo):
        """The dashboard's "Weekly report" and launchd's Friday 17:00 run the same command. Nothing it writes says
        "this week was sent", so neither run can make the other skip."""
        slack = FakeSlack()
        main(["weekly"], runtime_factory=fake_runtime(slack), clock=self.FRIDAY)
        main(["weekly"], runtime_factory=fake_runtime(slack), clock=self.FRIDAY)
        assert len(slack.sent) == 2  # a second run sends again
        keys = set(repo_store(repo).cursors(""))
        assert keys == {f"own:{x.ts}" for x in slack.sent} | {"job_last:weekly"}

    def test_a_weekly_run_that_meets_another_waits_for_it_and_then_sends(self, repo):
        """Friday's scheduled run must never be skipped: with a dashboard run in progress it waits for the lock."""
        import threading
        import time as t
        held = hold(repo / "data" / "weekly.lock")  # the dashboard's run, still going
        slack, done = FakeSlack(), []
        worker = threading.Thread(target=lambda: done.append(
            main(["weekly"], runtime_factory=fake_runtime(slack), clock=self.FRIDAY)))
        worker.start()
        try:
            t.sleep(0.4)
            assert done == [] and slack.sent == []  # waiting, not skipping and not sending
        finally:
            held.close()
        worker.join(10)
        assert done == [0] and len(slack.sent) == 1

    def test_weekly_holds_its_own_lock_while_it_runs(self, repo):
        seen = []

        def factory(settings):
            seen.append(controls.lock_held(cli.weekly_lock_path(settings)))
            return fake_runtime()(settings)
        main(["weekly"], runtime_factory=factory, clock=self.FRIDAY)
        assert seen == [True] and controls.lock_held(cli.weekly_lock_path(load_settings())) is False

    def test_weekly_is_a_job_step_on_the_live_feed(self, repo):
        main(["weekly"], runtime_factory=fake_runtime(), clock=self.FRIDAY)
        assert [(e.job, e.step, e.ok) for e in repo_store(repo).events_since(0)] == [("weekly", "send", True)]


class TestHistoryImportControls:
    def test_it_holds_its_own_lock_and_the_tick_lock(self, repo):
        seen = []

        class Spy(FakeSlack):
            def read_channel(self, channel_id, oldest, limit=50, *, latest=None):
                s = load_settings()
                seen.append((controls.lock_held(cli.history_import_lock_path(s)), controls.lock_held(cli.tick_lock_path(s))))
                return super().read_channel(channel_id, oldest, limit)
        assert main(["history-import"], runtime_factory=fake_runtime(Spy()), clock=Clock(at(11, 0))) == 0
        assert seen and set(seen) == {(True, True)}
        assert controls.lock_held(cli.history_import_lock_path(load_settings())) is False

    def test_a_second_import_is_refused_by_its_lock(self, repo, capsys):
        held = hold(repo / "data" / "history_import.lock")
        try:
            assert main(["history-import"], runtime_factory=no_runtime, clock=Clock(at(11, 0))) == 0
        finally:
            held.close()
        assert "another import is running" in capsys.readouterr().out
        assert last_of(repo, "history_import")["summary"] == "another import is running"

    def test_a_running_tick_is_the_refusal_reason_the_control_shows(self, repo, capsys):
        held = hold(repo / "data" / "tick.lock")
        try:
            assert main(["history-import"], runtime_factory=no_runtime, clock=Clock(at(11, 0))) == 1
        finally:
            held.close()
        assert "a tick is running — try again in a minute" in capsys.readouterr().out
        assert last_of(repo, "history_import") == {"at": at(11, 0).isoformat(), "ok": False,
                                                   "summary": "a tick is running — try again in a minute"}

    def test_inside_the_huddle_window_it_stops_and_says_so(self, repo, capsys):
        assert main(["history-import"], runtime_factory=fake_runtime(), clock=Clock(at(14, 30))) == 0
        last = last_of(repo, "history_import")
        assert last["ok"] is True and last["summary"] == "0 new message(s), stopped: huddle window"
        assert "stopped=huddle window" in capsys.readouterr().out

    def test_it_is_a_job_step_on_the_live_feed(self, repo):
        main(["history-import"], runtime_factory=fake_runtime(), clock=Clock(at(11, 0)))
        assert [(e.job, e.step) for e in repo_store(repo).events_since(0)] == [("history_import", "import")]


class TestEnrichControls:
    def test_it_leaves_its_record_and_a_step(self, repo, monkeypatch, capsys):
        from haseeb_agent.jobs import enrich, history_import
        monkeypatch.setattr(history_import, "sweep_recent", lambda *a, **k: history_import.SweepResult(1, 0, 0, None))
        monkeypatch.setattr(history_import, "run_thread_refresh",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("slack down")))  # never stops the labels
        monkeypatch.setattr(enrich, "run_enrich", lambda *a, **k: enrich.EnrichResult(labelled=4, topics=2))
        assert main(["history-enrich"], runtime_factory=fake_runtime(), clock=Clock(at(21, 0))) == 0
        cap = capsys.readouterr()
        assert "history-enrich: labelled=4 topics=2" in cap.out and "history-refresh failed: RuntimeError" in cap.err
        assert last_of(repo, "enrich") == {"at": at(21, 0).isoformat(), "ok": True, "summary": "labelled 4, 2 topic(s)"}
        assert [(e.job, e.step, e.ok) for e in repo_store(repo).events_since(0)] == [("enrich", "enrich", True)]

    def test_a_second_run_is_refused_by_its_lock(self, repo, capsys):
        held = hold(repo / "data" / "enrich.lock")
        try:
            assert main(["history-enrich"], runtime_factory=no_runtime, clock=Clock(at(21, 0))) == 0
        finally:
            held.close()
        assert "another run is in progress" in capsys.readouterr().out


class TestDecksRetry:
    def _rt(self, built):
        def factory(settings):
            rt = fake_runtime()(settings)
            rt.decks = lambda now, limit: built
            return rt
        return factory

    def _gave_up(self, repo, state="gave_up", tries=3):
        st = repo_store(repo)
        st.upsert_item(Item(id="insight-2026-10-19-1", kind="insight", day=MONDAY, status="delivered", body="{}",
                            title="t", dm_ts="1.1", source_ts=None, score=90.0, models={}, attempts=0, edited=False,
                            created_at=at(8, 30), acted_at=None))
        st.set_cursor("deck:insight-2026-10-19-1", json.dumps({"sha": "x", "tries": tries, "state": state}))
        st.close()

    def test_retry_resets_todays_given_up_decks_then_builds(self, repo, capsys):
        self._gave_up(repo)
        assert main(["decks", "--retry"], runtime_factory=self._rt(2), clock=Clock(at(11, 0))) == 0
        assert "decks ok: 2 deck(s) (1 given-up deck(s) retried)" in capsys.readouterr().out
        assert json.loads(repo_store(repo).get_cursor("deck:insight-2026-10-19-1")) == {"sha": "x", "tries": 2,
                                                                                         "state": "pending"}  # one left
        assert last_of(repo, "decks")["summary"] == "2 deck(s) uploaded (1 given-up deck(s) retried)"

    def test_plain_decks_leave_a_given_up_deck_given_up(self, repo, capsys):
        self._gave_up(repo)
        assert main(["decks"], runtime_factory=self._rt(0), clock=Clock(at(11, 0))) == 0
        assert capsys.readouterr().out.strip().endswith("decks ok: 0 deck(s)")  # the tick's spawn, unchanged
        assert json.loads(repo_store(repo).get_cursor("deck:insight-2026-10-19-1"))["state"] == "gave_up"

    def test_a_second_deck_process_is_refused_by_the_lock(self, repo, capsys):
        held = hold(repo / "data" / "decks.lock")
        try:
            assert main(["decks", "--retry"], runtime_factory=no_runtime, clock=Clock(at(11, 0))) == 0
        finally:
            held.close()
        assert "another deck process is running" in capsys.readouterr().out


class TestSlackCheck:
    HASEEB = "U08CZCGJW3E"

    def _run(self, repo, slack, capsys):
        rc = main(["slack-check"], runtime_factory=fake_runtime(slack), clock=Clock(at(11, 5)))
        cap = capsys.readouterr()
        return rc, cap.out + cap.err

    def _identity(self, repo):
        st = repo_store(repo)
        try:
            return controls.read_identity(st)
        finally:
            st.close()

    def test_the_account_is_haseebs(self, repo, capsys):
        slack = FakeSlack()
        rc, out = self._run(repo, slack, capsys)
        assert rc == 0 and f"slack-check ok: the agent posts as you ({self.HASEEB})" in out
        assert self._identity(repo) == {"user_id": self.HASEEB, "ok": True, "at": at(11, 5).isoformat(), "error": None}
        assert slack.sent == [] and slack.reactions == [] and slack.uploads == []  # read-only, unlike smoke
        assert last_of(repo, "slack_check") == {"at": at(11, 5).isoformat(), "ok": True, "summary": "posting as you"}

    def test_the_profile_itself_is_never_printed_or_stored(self, repo, capsys):
        slack = FakeSlack()
        rc, out = self._run(repo, slack, capsys)
        everything = out + json.dumps(repo_store(repo).cursors(""))
        assert "example.com" not in everything and "Display name" not in everything

    def test_another_account_is_a_red_warning_that_names_it(self, repo, capsys):
        slack = FakeSlack()
        slack.profile_text = json.dumps({"result": "Display name: yameen\nUser ID: U0OTHER12345\nyameen@example.com\n"})
        rc, out = self._run(repo, slack, capsys)
        assert rc == 1 and "WARNING, the agent would post as U0OTHER12345, not as you" in out
        assert self._identity(repo) == {"user_id": "U0OTHER12345", "ok": False, "at": at(11, 5).isoformat(),
                                        "error": None}
        assert last_of(repo, "slack_check")["ok"] is False
        assert "yameen" not in repo_store(repo).get_cursor("slack_identity")

    def test_the_live_profile_shape_is_a_pass(self, repo, capsys):
        """The tool result exactly as the connector returns it (tests/fixtures/user_profile_result.txt)."""
        slack = FakeSlack()
        slack.profile_text = (ROOT / "tests" / "fixtures" / "user_profile_result.txt").read_text()
        rc, out = self._run(repo, slack, capsys)
        assert rc == 0 and self._identity(repo)["ok"] is True and self._identity(repo)["user_id"] == self.HASEEB
        assert "sample" not in out.lower() and "sample" not in repo_store(repo).get_cursor("slack_identity").lower()

    @pytest.mark.parametrize("profile,why", [
        ("Display name: x\nNo identifier here.", "the profile has no user id"),
        ("User ID: U0OTHER12345\nUser ID: U0ABCDEF123", "the profile names several user ids"),
        # M4: without the `User ID:` label an id-shaped token proves nothing, even Haseeb's own: amber, never green
        ('{"result": "Username: someone\nReports to <@U08CZCGJW3E>"}', "the profile has no user id"),
        ("U08CZCGJW3E", "the profile has no user id"),
    ])
    def test_a_profile_without_one_readable_id_is_never_a_pass(self, repo, capsys, profile, why):
        slack = FakeSlack()
        slack.profile_text = profile
        rc, out = self._run(repo, slack, capsys)
        assert rc == 1 and why in out
        assert self._identity(repo) == {"user_id": None, "ok": False, "at": at(11, 5).isoformat(), "error": why}

    def test_a_failed_read_is_recorded_as_a_failure(self, repo, capsys):
        slack = FakeSlack()
        slack.fail_next("claude exit 1: SECRET", method="read_profile")
        rc, out = self._run(repo, slack, capsys)
        assert rc == 1 and "could not read the profile (SlackError)" in out and "SECRET" not in out
        assert self._identity(repo)["error"] == "could not read the profile (SlackError)"

    def test_a_runtime_that_cannot_be_built_is_recorded_too(self, repo, capsys):
        def broken(settings):
            raise RuntimeError("no claude")
        rc = main(["slack-check"], runtime_factory=broken, clock=Clock(at(11, 5)))
        assert rc == 1 and self._identity(repo)["error"] == "could not read the profile (RuntimeError)"

    def test_it_is_a_job_step_on_the_live_feed(self, repo, capsys):
        self._run(repo, FakeSlack(), capsys)
        assert [(e.job, e.step, e.ok) for e in repo_store(repo).events_since(0)] == [("slack_check", "check", True)]

    def test_a_second_check_is_refused_by_its_lock(self, repo, capsys):
        held = hold(repo / "data" / "slack_check.lock")
        try:
            rc, out = self._run(repo, FakeSlack(), capsys)
        finally:
            held.close()
        assert rc == 0 and "another check is running" in out and self._identity(repo) is None


class TestFeedbackCommand:
    CH = "C0C62C128D8"

    def _seed(self, repo, ts=TS1, user_id="U0A3S7LUZ41", text="Learned a lot this week. " * 8, slack=None):
        from haseeb_agent import history
        from haseeb_agent.models import Msg
        slack = slack or FakeSlack()
        msg = slack.add_message(self.CH, ts, text, user_id=user_id, user_name="Iqbal Qazi")
        st = repo_store(repo)
        history.capture(st, load_settings(), [msg], source_channel=self.CH)
        st.close()
        return slack

    def _rt(self, slack, llm):
        def factory(settings):
            rt = fake_runtime(slack)(settings)
            rt.llm = llm
            return rt
        return factory

    def test_it_drafts_for_the_chosen_post_and_sends_the_draft_to_the_dm(self, repo, capsys):
        from tests.test_feedback import DRAFT, FakeLLM
        slack = self._seed(repo)
        rc = main(["feedback", "--ts", TS1], runtime_factory=self._rt(slack, FakeLLM()), clock=Clock(at(11, 0)))
        assert rc == 0
        (dm,) = [x for x in slack.sent]
        assert dm.channel_id == "U08CZCGJW3E" and DRAFT.splitlines()[0] in dm.text
        item = repo_store(repo).get_item(f"feedback-{TS1}")
        assert item.status == "delivered" and item.body == DRAFT and item.dm_ts == dm.ts
        assert f"feedback ok: feedback-{TS1} is in your DM" in capsys.readouterr().out
        assert last_of(repo, "feedback") == {"at": at(11, 0).isoformat(), "ok": True, "summary": "draft sent to your DM"}

    def test_it_refuses_haseebs_own_post(self, repo, capsys):
        from tests.test_feedback import FakeLLM
        llm = FakeLLM()
        slack = self._seed(repo, user_id="U08CZCGJW3E")
        rc = main(["feedback", "--ts", TS1], runtime_factory=self._rt(slack, llm), clock=Clock(at(11, 0)))
        assert rc == 1 and "feedback refused: your own post" in capsys.readouterr().err
        assert slack.sent == [] and llm.calls == []
        assert last_of(repo, "feedback") == {"at": at(11, 0).isoformat(), "ok": False,
                                             "summary": "refused: your own post"}

    def test_it_refuses_a_post_that_already_has_a_draft(self, repo, capsys):
        from tests.test_feedback import FakeLLM
        slack = self._seed(repo)
        main(["feedback", "--ts", TS1], runtime_factory=self._rt(slack, FakeLLM()), clock=Clock(at(11, 0)))
        capsys.readouterr()
        llm = FakeLLM()
        rc = main(["feedback", "--ts", TS1], runtime_factory=self._rt(slack, llm), clock=Clock(at(11, 1)))
        assert rc == 1 and "already has a feedback draft" in capsys.readouterr().err
        assert len(slack.sent) == 1 and llm.calls == []

    def test_a_failure_exits_1_with_the_class_in_the_record(self, repo, capsys):
        from haseeb_agent.litellm import LLMError
        from tests.test_feedback import FakeLLM
        slack = self._seed(repo)
        rc = main(["feedback", "--ts", TS1], runtime_factory=self._rt(slack, FakeLLM(writer=[LLMError("down")])),
                  clock=Clock(at(11, 0)))
        assert rc == 1 and "feedback failed" in capsys.readouterr().err
        assert last_of(repo, "feedback")["summary"] == "LLMError" and last_of(repo, "feedback")["ok"] is False
        assert repo_store(repo).runs()[0][:2] == ("feedback", False)

    def test_one_draft_at_a_time(self, repo, capsys):
        held = hold(repo / "data" / "feedback.lock")
        try:
            assert main(["feedback", "--ts", TS1], runtime_factory=no_runtime, clock=Clock(at(11, 0))) == 0
        finally:
            held.close()
        assert "another draft is running" in capsys.readouterr().out

    @pytest.mark.parametrize("bad", ["", "abc", "1760000000", "1760000000.0001", "1760000000.000100 ", "-1760000000.000100",
                                     "1760000000.000100\n"])
    def test_the_ts_must_be_a_ts(self, repo, bad):
        with pytest.raises(SystemExit) as e:
            main(["feedback", "--ts", bad], runtime_factory=no_runtime)
        assert e.value.code == 2
        with pytest.raises(SystemExit) as e:
            main(["feedback"], runtime_factory=no_runtime)
        assert e.value.code == 2


class TestSpawnJob:
    def test_the_default_spawn_is_blocked_by_the_offline_guard(self, monkeypatch):
        from haseeb_agent.offline import OfflineError
        ran = []
        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
        with pytest.raises(OfflineError, match="spawn weekly"):
            cli.spawn_job(["weekly"])
        assert ran == []

    def test_it_starts_the_command_detached(self, monkeypatch):
        monkeypatch.delenv("HASEEB_AGENT_OFFLINE")  # subprocess.run is faked below, so nothing real runs
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0)
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        cli.spawn_job(["feedback", "--ts", TS1])
        cmd = seen["cmd"]
        assert cmd[1:3] == ["-I", "-c"] and "start_new_session=True" in cmd[3]  # the double fork (N4)
        assert cmd[5:] == ["-m", "haseeb_agent.cli", "feedback", "--ts", TS1]
        assert seen["kw"]["stdin"] == subprocess.DEVNULL and seen["kw"]["check"] is True
