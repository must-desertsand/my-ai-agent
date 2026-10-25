"""Which job a model call is for, at the job entry points (cli.py): every job command runs in a usage session for its job
(`run_recorded` and the three commands outside it), and each job of the tick's round narrows it to its own. A call made
there, with no `events.step` around it, is still recorded, under the right job. No model is called: the "runtime" records
a row itself, the way the first thing a job does with a model would."""
import logging
from datetime import UTC, datetime

import pytest

from haseeb_agent import usage
from haseeb_agent.cli import JOBS, PAGE_JOB, main
from haseeb_agent.store import Store
from tests.test_cli import Clock, at, fake_runtime, repo  # noqa: F401 - `repo` is a fixture

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_package_log_level():
    """main() sets the haseeb_agent logger to INFO: it must not leak into the caplog tests of other modules (the same
    guard tests/test_cli.py has)."""
    logger = logging.getLogger("haseeb_agent")
    before = logger.level
    yield
    logger.setLevel(before)


def rows(repo_dir):
    s = Store(repo_dir / "data" / "agent.sqlite")
    try:
        return s.usage_since(EPOCH)
    finally:
        s.close()


def recording_runtime(then_raise: bool):
    """A runtime factory that records one row as soon as it is asked for, which is inside the command's session."""
    build = fake_runtime()

    def factory(settings):
        usage.record("claude", "claude-haiku-5-5", input_tokens=1)
        if then_raise:
            raise RuntimeError("stop here")  # the command catches it and ends; the row is already there
        return build(settings)
    return factory


@pytest.mark.parametrize("argv,job", [
    (["insights"], "insights"),
    (["insights", "--force"], "insights"),
    (["decks"], "decks"),
    (["weekly"], "weekly"),
    (["feedback", "--ts", "1760000000.000100"], "feedback"),
    (["history-import"], "history"),
    (["history-enrich"], "history"),
    (["history-refresh"], "history"),
    (["slack-check"], "slack"),
    (["smoke"], "slack"),
    (["deck-test", "insight-2026-10-20-1"], "deck_test"),
])
def test_a_job_command_records_the_calls_it_makes_for_its_job(repo, argv, job):
    main(argv, runtime_factory=recording_runtime(then_raise=True), clock=Clock(at(11, 0)))
    [r] = rows(repo)
    assert (r.job, r.step, r.source) == (job, "other", "live")


def test_the_session_ends_with_the_command(repo):
    main(["weekly"], runtime_factory=recording_runtime(then_raise=True), clock=Clock(at(11, 0)))
    usage.record("claude", "m")  # after the command: no session, so nowhere to go
    assert len(rows(repo)) == 1


@pytest.mark.parametrize("job,expect", [
    ("process_dm", "slack"), ("deliver_pending", "insights"), ("run_todo", "todo"), ("run_feedback", "feedback"),
    (PAGE_JOB, "slack"),
])
def test_each_job_of_the_ticks_round_is_recorded_under_its_own_job(repo, job, expect):
    def make(name):
        def run(now):
            if name == job:
                usage.record("litellm", "deepseek-v4-pro", input_tokens=2)
            return "ok"
        return run

    names = (PAGE_JOB, *JOBS)
    s = Store(repo / "data" / "agent.sqlite")
    s.enqueue_page_command("i1", "skip", None, "0" * 64, at(10, 59))  # so the page job is due
    s.close()
    main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: {n: make(n) for n in names},
         clock=Clock(at(11, 0)))
    [r] = rows(repo)
    assert (r.job, r.provider) == (expect, "litellm")


def test_a_tick_job_that_fails_still_leaves_the_rows_it_made(repo):
    def run(now):
        usage.record("claude", "m")
        raise RuntimeError("boom")

    main(["tick"], runtime_factory=fake_runtime(), jobs_factory=lambda rt: {"run_todo": run},
         clock=Clock(at(11, 0)))
    assert [r.job for r in rows(repo)] == ["todo"]


def test_a_run_outside_any_command_records_nothing(repo):
    usage.record("claude", "m")
    assert not (repo / "data" / "agent.sqlite").exists()
