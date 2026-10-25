"""jobs.history_import: resumable, paced, off-hours import of the channel history."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import cli, history
from haseeb_agent.cli import main
from haseeb_agent.config import load_settings
from haseeb_agent.jobs.history_import import CURSOR_OLDEST, CURSOR_THREADS, run_import, run_thread_refresh
from haseeb_agent.models import Msg
from haseeb_agent.slack_gateway import SlackUnavailable
from haseeb_agent.store import Store
from tests.fakes import FakeSlack
from tests.test_cli import KW, _restore_package_log_level, at, fake_runtime, repo  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parent.parent
NIGHT = at(20, 0)


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agent.sqlite")
    yield s
    s.close()


def seed(slack, settings, n, base=1_790_000_000):
    for i in range(n):
        slack.add_message(settings.channel_id, f"{base + i}.000001", f"m{i}")


def stored(store, settings):
    return [m.ts for m in store.messages(limit=10_000)]


def test_import_pages_backwards_and_resumes(settings, store):
    slack = FakeSlack()
    seed(slack, settings, 120)  # pages of 50, 50, 20, then an empty one
    sleeps = []
    first = run_import(slack, store, settings, lambda: NIGHT, sleeps.append, max_calls=2)
    assert (first.calls, first.finished, first.stopped_reason) == (2, False, "call cap")
    assert len(stored(store, settings)) == 100
    mid = store.get_cursor(CURSOR_OLDEST)
    assert mid == "1790000020.000001"
    second = run_import(slack, store, settings, lambda: NIGHT, sleeps.append)
    assert second.finished and second.stopped_reason is None
    assert second.calls == 2 and second.messages_new == 20
    got = stored(store, settings)
    assert len(got) == len(set(got)) == 120
    assert store.get_cursor(CURSOR_OLDEST) == "1790000000.000001"
    assert run_import(slack, store, settings, lambda: NIGHT, sleeps.append).calls == 0  # done stays done


def test_import_reads_threads_of_huddle_and_insight_posts_only(settings, store):
    slack = FakeSlack()
    ch = settings.channel_id
    huddle = slack.add_message(ch, "1790000001.000001", "A huddle started", is_bot=True, user_id="USLACKBOT",
                               reply_count=2)
    insight = slack.add_message(ch, "1790000002.000001", "Daily insight: x", reply_count=1)
    chat = slack.add_message(ch, "1790000003.000001", "just chatting", reply_count=3)
    plain_insight = slack.add_message(ch, "1790000004.000001", "Insight: y")  # no replies
    reply = Msg(ts="1790000002.000002", user_id="U2", user_name="Bo", text="great", thread_ts=insight.ts,
                reply_count=0, latest_reply=None)
    notes = Msg(ts="1790000001.000002", user_id="USLACKBOT", user_name="Slackbot",
                text="AI huddle notes are ready", thread_ts=huddle.ts, reply_count=0, latest_reply=None, is_bot=True)
    slack.add_thread(ch, insight, [reply])
    slack.add_thread(ch, huddle, [notes])
    read = []
    orig = slack.read_thread
    slack.read_thread = lambda c, t: (read.append(t), orig(c, t))[1]
    res = run_import(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert res.finished and res.threads_done == 2
    assert sorted(read) == [huddle.ts, insight.ts]
    assert chat.ts not in read and plain_insight.ts not in read
    kinds = {m.ts: m.kind for m in store.messages(limit=100)}
    assert kinds[reply.ts] == "feedback" and kinds[notes.ts] == "huddle_notes"
    assert json.loads(store.get_cursor(CURSOR_THREADS)) == []


def test_import_refuses_in_huddle_window(settings, store):
    slack = FakeSlack()
    seed(slack, settings, 3)
    res = run_import(slack, store, settings, lambda: at(14, 40), lambda s: None)
    assert (res.calls, res.finished, res.stopped_reason) == (0, False, "huddle window")
    assert stored(store, settings) == []


def test_import_allows_the_huddle_hour_on_a_weekend(settings, store):
    slack = FakeSlack()
    seed(slack, settings, 3)
    saturday = datetime(2026, 10, 17, 14, 40, tzinfo=KW)
    res = run_import(slack, store, settings, lambda: saturday, lambda s: None)
    assert res.finished


def test_import_stops_entering_huddle_window(settings, store):
    """M4: a call may take up to 180 s, so none starts once it could still be running at 14:25 on a workday. The
    clock is read before the pause and again after it."""
    slack = FakeSlack()
    seed(slack, settings, 200)
    times = iter([at(14, 18),                      # call 1
                  at(14, 20), at(14, 21, 59),      # call 2: before and after its pause (14:24:59 + nothing)
                  at(14, 21, 59), at(14, 22)])     # call 3: 14:22 + 180 s reaches 14:25, so it never starts
    res = run_import(slack, store, settings, lambda: next(times), lambda s: None)
    assert (res.calls, res.stopped_reason, res.finished) == (2, "huddle window", False)


def test_import_paces_calls(settings, store):
    slack = FakeSlack()
    seed(slack, settings, 120)
    log = []
    orig = slack.read_channel
    slack.read_channel = lambda *a, **k: (log.append("call"), orig(*a, **k))[1]
    res = run_import(slack, store, settings, lambda: NIGHT, lambda s: log.append(s))
    assert res.calls == 4
    assert log == ["call", 2.0, "call", 2.0, "call", 2.0, "call"]  # never before the first call


def test_import_cap(settings, store):
    slack = FakeSlack()
    seed(slack, settings, 1000)
    res = run_import(slack, store, settings, lambda: NIGHT, lambda s: None, max_calls=5)
    assert (res.calls, res.finished) == (5, False)


def test_import_slack_failure_is_resumable(settings, store):
    slack = FakeSlack()
    seed(slack, settings, 160)
    n = {"c": 0}
    orig = slack.read_channel

    def flaky(*a, **k):
        n["c"] += 1
        if n["c"] == 3:
            raise SlackUnavailable("claude exit 1: secret message text")
        return orig(*a, **k)
    slack.read_channel = flaky
    res = run_import(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert res.finished is False and res.calls == 3
    assert res.stopped_reason == "claude exit 1" and "secret" not in res.stopped_reason
    assert len(stored(store, settings)) == 100
    slack.read_channel = orig
    again = run_import(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert again.finished
    assert len(stored(store, settings)) == 160


def test_a_poison_thread_is_dropped_on_the_third_failure(settings, store, caplog):
    slack = FakeSlack()
    ch = settings.channel_id
    slack.add_message(ch, "1790000002.000001", "Daily insight: x", reply_count=1)  # no thread seeded: always fails
    runs = [run_import(slack, store, settings, lambda: NIGHT, lambda s: None) for _ in range(2)]
    assert all(not r.finished and r.stopped_reason for r in runs)
    third = run_import(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert third.finished and third.threads_done == 0
    assert json.loads(store.get_cursor(CURSOR_THREADS)) == []
    assert "dropping unreadable thread 1790000002.000001" in caplog.text
    assert "Daily insight" not in caplog.text


def test_an_inclusive_latest_finishes_instead_of_looping(settings, store):
    slack = FakeSlack()
    seed(slack, settings, 120)
    orig = slack.read_channel

    def inclusive(channel_id, oldest, limit=50, *, latest=None):
        if latest is not None:  # includes the message at `latest` itself, and no older one fits the page
            page = orig(channel_id, oldest, 1, latest=f"{int(latest.split('.')[0])}.000002")
            return page
        return orig(channel_id, oldest, limit)
    slack.read_channel = inclusive
    res = run_import(slack, store, settings, lambda: NIGHT, lambda s: None, max_calls=50)
    assert res.finished and res.calls < 5


class TestCli:
    def test_prints_counts_only(self, repo, capsys, monkeypatch):  # noqa: F811
        slack = FakeSlack()
        s = load_settings()
        slack.add_message(s.channel_id, "1790000001.000001", "TOP SECRET TEXT")
        monkeypatch.setattr(cli._time, "sleep", lambda s_: None)
        rc = main(["history-import", "--max-calls", "10"], runtime_factory=fake_runtime(slack),
                  clock=lambda: NIGHT)
        out = capsys.readouterr().out
        assert rc == 0 and "new messages=1" in out and "finished=yes" in out and "SECRET" not in out

    def test_a_slack_failure_exits_1(self, repo, capsys, monkeypatch):  # noqa: F811
        slack = FakeSlack()
        slack.fail_next("slack_read_channel: boom")
        monkeypatch.setattr(cli._time, "sleep", lambda s_: None)
        rc = main(["history-import"], runtime_factory=fake_runtime(slack), clock=lambda: NIGHT)
        assert rc == 1 and "stopped=slack_read_channel" in capsys.readouterr().out

    def test_refused_while_a_tick_runs(self, repo, capsys):  # noqa: F811
        from haseeb_agent.cli import try_lock
        with try_lock(cli.tick_lock_path(load_settings())):
            rc = main(["history-import"], runtime_factory=fake_runtime(), clock=lambda: NIGHT)
        assert rc == 1
        assert "a tick is running — try again in a minute" in capsys.readouterr().out


# -- D15: the thread refresh ----------------------------------------------------------------------------------------

BASE = 1_790_000_000


def insight_thread(slack, settings, i, replies=2, text="Daily insight: topic", reply_text="feedback"):
    """An insight on the channel page with `replies` replies, readable as a thread. Returns (parent, replies)."""
    ts = f"{BASE + i * 1000}.000001"
    parent = slack.add_message(settings.channel_id, ts, f"{text} {i}", reply_count=replies,
                               latest_reply=str(BASE + i * 1000 + 10 * replies))
    kids = [Msg(ts=f"{BASE + i * 1000 + 10 * (k + 1)}.000001", user_id=f"U{k + 2}", user_name=f"Pat{k}",
                text=f"{reply_text} {i}.{k}", thread_ts=ts, reply_count=0, latest_reply=None)
            for k in range(replies)]
    slack.add_thread(settings.channel_id, parent, kids)
    return parent, kids


def queue(store):
    return json.loads(store.get_cursor(history.REFRESH_QUEUE) or "[]")


def fails_key(parent):
    return f"history_refresh:thread_fail:{parent.ts}"


def test_refresh_reads_the_queued_threads_paced_and_stores_the_replies_as_feedback(settings, store):
    slack = FakeSlack()
    made = [insight_thread(slack, settings, i) for i in range(3)]
    history.capture(store, settings, [parent for parent, _ in made], source_channel=settings.channel_id)  # a live job saw the posts, with replies
    assert queue(store) == [parent.ts for parent, _ in made]
    log = []
    real = slack.read_thread
    slack.read_thread = lambda c, t: (log.append(("read", t)), real(c, t))[1]
    res = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: log.append(s))
    assert (res.calls, res.threads_done, res.messages_new, res.dropped, res.pending, res.stopped_reason) == (
        3, 3, 6, 0, 0, None)
    assert [e[0] if isinstance(e, tuple) else e for e in log] == ["read", 2.0, "read", 2.0, "read"]  # not before 1st
    assert [e[1] for e in log if isinstance(e, tuple)] == [parent.ts for parent, _ in made]  # the oldest first
    kinds = {m.ts: m.kind for m in store.messages(limit=100)}
    assert all(kinds[kid.ts] == "feedback" for _, kids in made for kid in kids)
    assert queue(store) == []


def test_refresh_reads_at_most_max_calls_then_carries_on(settings, store):
    slack = FakeSlack()
    made = [insight_thread(slack, settings, i, replies=1) for i in range(12)]
    history.capture(store, settings, [parent for parent, _ in made], source_channel=settings.channel_id)
    first = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)  # the default is 10
    assert (first.calls, first.threads_done, first.pending, first.stopped_reason) == (10, 10, 2, "call cap")
    assert queue(store) == [parent.ts for parent, _ in made[10:]]
    second = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None, max_calls=1)
    assert (second.calls, second.pending, second.stopped_reason) == (1, 1, "call cap")
    third = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert (third.calls, third.pending, third.stopped_reason) == (1, 0, None)
    assert len([m for m in store.messages(limit=100) if m.kind == "feedback"]) == 12


def test_refresh_with_an_empty_queue_makes_no_call(settings, store):
    slack = FakeSlack()
    slack.read_thread = lambda *a: pytest.fail("no thread was queued")
    slack.read_channel = lambda *a, **k: pytest.fail("the refresh never reads the channel")
    res = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: pytest.fail("no call, no pause"))
    assert (res.calls, res.threads_done, res.pending, res.stopped_reason) == (0, 0, 0, None)
    store.set_cursor(history.REFRESH_QUEUE, "not json")  # an unreadable queue is an empty one
    assert run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None).calls == 0


def test_refresh_refuses_in_the_huddle_window(settings, store):
    slack = FakeSlack()
    parent, kids = insight_thread(slack, settings, 0)
    history.capture(store, settings, [parent], source_channel=settings.channel_id)
    res = run_thread_refresh(slack, store, settings, lambda: at(14, 40), lambda s: None)
    assert (res.calls, res.threads_done, res.pending, res.stopped_reason) == (0, 0, 1, "huddle window")
    assert queue(store) == [parent.ts] and kids[0].ts not in [m.ts for m in store.messages(limit=100)]
    saturday = datetime(2026, 10, 17, 14, 40, tzinfo=KW)  # the window is a workday thing
    assert run_thread_refresh(slack, store, settings, lambda: saturday, lambda s: None).threads_done == 1


def test_refresh_stops_before_a_call_that_would_start_in_the_huddle_window(settings, store):
    slack = FakeSlack()
    made = [insight_thread(slack, settings, i, replies=1) for i in range(4)]
    history.capture(store, settings, [parent for parent, _ in made], source_channel=settings.channel_id)
    times = iter([at(14, 18), at(14, 20), at(14, 21, 59), at(14, 21, 59), at(14, 22)])  # M4, as for the import
    res = run_thread_refresh(slack, store, settings, lambda: next(times), lambda s: None)
    assert (res.calls, res.threads_done, res.pending, res.stopped_reason) == (2, 2, 2, "huddle window")
    assert queue(store) == [parent.ts for parent, _ in made[2:]]


def test_a_failed_read_keeps_the_thread_queued_and_counts_the_failure(settings, store):
    slack = FakeSlack()
    parent, kids = insight_thread(slack, settings, 0)
    history.capture(store, settings, [parent], source_channel=settings.channel_id)
    slack.fail_next("slack_read_thread: boom with secret text", method="read_thread")
    res = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert (res.calls, res.threads_done, res.pending) == (1, 0, 1)
    assert res.stopped_reason == "slack_read_thread" and "secret" not in res.stopped_reason
    assert queue(store) == [parent.ts] and store.get_cursor(fails_key(parent)) == "1"
    again = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)  # Slack is back
    assert (again.threads_done, again.pending, again.stopped_reason) == (1, 0, None)
    assert queue(store) == [] and store.get_cursor(fails_key(parent)) == "0"  # a success clears the count


def test_a_failure_ends_the_run_so_an_outage_is_not_paid_for_ten_times(settings, store):
    slack = FakeSlack()
    made = [insight_thread(slack, settings, i, replies=1) for i in range(3)]
    history.capture(store, settings, [parent for parent, _ in made], source_channel=settings.channel_id)
    slack.fail_next("slack_read_thread: down", method="read_thread")
    res = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert (res.calls, res.pending) == (1, 3)


def test_a_poison_thread_in_the_refresh_is_dropped_on_the_third_failure(settings, store, caplog):
    slack = FakeSlack()
    ch = settings.channel_id
    poison = slack.add_message(ch, "1790000002.000001", "Daily insight: x", reply_count=1,
                               latest_reply="1790000100")  # no thread seeded: its read always fails
    other, kids = insight_thread(slack, settings, 5, replies=1)
    history.capture(store, settings, [poison, other], source_channel=settings.channel_id)
    runs = [run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None) for _ in range(2)]
    assert all(r.threads_done == 0 and r.pending == 2 and r.stopped_reason for r in runs)
    assert store.get_cursor(fails_key(poison)) == "2"
    third = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert (third.dropped, third.threads_done, third.pending, third.stopped_reason) == (1, 1, 0, None)
    assert third.calls == 2 and queue(store) == []  # the poison one is gone and the next thread was read
    assert "history refresh: dropping unreadable thread 1790000002.000001" in caplog.text
    assert "Daily insight" not in caplog.text
    # a later reply gives the thread a fresh set of tries
    history.capture(store, settings, [slack.add_message(ch, poison.ts, "Daily insight: x", reply_count=2,
                                                        latest_reply="1790000200")], source_channel=settings.channel_id)
    assert queue(store) == [poison.ts]
    again = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert again.dropped == 0 and again.pending == 1 and store.get_cursor(fails_key(poison)) == "1"


def test_a_thread_queued_while_a_read_is_running_is_kept(settings, store):
    """The nightly refresh is its own process: what a tick queues during the Slack call must survive."""
    slack = FakeSlack()
    a, _ = insight_thread(slack, settings, 0)
    b, _ = insight_thread(slack, settings, 1)
    history.capture(store, settings, [a], source_channel=settings.channel_id)
    real = slack.read_thread

    def read(channel, ts):
        history.capture(store, settings, [b], source_channel=settings.channel_id)  # a tick's capture, while this run waits for Slack
        return real(channel, ts)
    slack.read_thread = read
    res = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None, max_calls=1)
    assert (res.threads_done, res.pending, res.stopped_reason) == (1, 1, "call cap")
    assert queue(store) == [b.ts]


def test_a_refresh_read_marks_the_newest_reply_so_a_page_does_not_queue_it_again(settings, store):
    slack = FakeSlack()
    ch = settings.channel_id
    page_view = slack.add_message(ch, "1790000001.000001", "Daily insight: x", reply_count=1,
                                  latest_reply="1790000100")
    history.capture(store, settings, [page_view], source_channel=settings.channel_id)
    assert queue(store) == [page_view.ts]
    # two more replies arrive before the night: the thread read shows them
    newer = Msg(ts=page_view.ts, user_id="U1", user_name="User", text="Daily insight: x", thread_ts=page_view.ts,
                reply_count=3, latest_reply="1790000300")
    kids = [Msg(ts=f"{1790000100 + 100 * k}.000001", user_id="U2", user_name="Bo", text=f"r{k}",
                thread_ts=page_view.ts, reply_count=0, latest_reply=None) for k in range(3)]
    slack.add_thread(ch, newer, kids)
    res = run_thread_refresh(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert res.threads_done == 1 and res.messages_new == 3
    history.capture(store, settings, [newer], source_channel=settings.channel_id)  # the page now shows the newest reply the refresh already read
    assert queue(store) == []


def test_the_import_leaves_the_refresh_queue_alone(settings, store):
    slack = FakeSlack()
    made = [insight_thread(slack, settings, i) for i in range(3)]
    res = run_import(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert res.finished and res.threads_done == 3
    assert queue(store) == []  # it reads those threads itself
    history.capture(store, settings, [parent for parent, _ in made], source_channel=settings.channel_id)  # a live page shows the same newest replies
    assert queue(store) == []
    history.capture(store, settings, [slack.add_message(settings.channel_id, made[0][0].ts, "Daily insight: topic 0",
                                                        reply_count=3, latest_reply=str(BASE + 99))], source_channel=settings.channel_id)
    assert queue(store) == [made[0][0].ts]  # ... and a reply after the import is news


class TestRefreshCli:
    def seed(self, n=1, **kw):
        slack = FakeSlack()
        s = load_settings()
        made = [insight_thread(slack, s, i, **kw) for i in range(n)]
        st = Store(s.db_path)
        history.capture(st, s, [parent for parent, _ in made], source_channel=s.channel_id)  # what a live job does with the page
        st.close()
        return slack, s, made

    def test_prints_counts_only(self, repo, capsys, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cli._time, "sleep", lambda s_: None)
        slack, s, _ = self.seed(1, replies=2, reply_text="TOP SECRET TEXT")
        rc = main(["history-refresh", "--max-calls", "5"], runtime_factory=fake_runtime(slack), clock=lambda: NIGHT)
        captured = capsys.readouterr()
        assert rc == 0 and captured.out.splitlines() == [
            "history-refresh: calls=1 threads read=1 new messages=2 dropped=0 pending=0 stopped=n/a"]
        assert "SECRET" not in captured.out + captured.err and "Daily insight" not in captured.out
        st = Store(s.db_path)
        assert sum(m.kind == "feedback" for m in st.messages(limit=100)) == 2
        st.close()

    def test_the_default_is_ten_threads_a_run(self, repo, capsys, monkeypatch):  # noqa: F811
        sleeps = []
        monkeypatch.setattr(cli._time, "sleep", sleeps.append)
        slack, s, _ = self.seed(12, replies=1)
        rc = main(["history-refresh"], runtime_factory=fake_runtime(slack), clock=lambda: NIGHT)
        out = capsys.readouterr().out
        assert rc == 0 and "threads read=10" in out and "pending=2" in out and "stopped=call cap" in out
        assert sleeps == [2.0] * 9  # paced, and never before the first call

    def test_a_slack_failure_exits_1(self, repo, capsys, monkeypatch):  # noqa: F811
        monkeypatch.setattr(cli._time, "sleep", lambda s_: None)
        slack, s, _ = self.seed(1)
        slack.fail_next("slack_read_thread: boom", method="read_thread")
        rc = main(["history-refresh"], runtime_factory=fake_runtime(slack), clock=lambda: NIGHT)
        assert rc == 1 and "stopped=slack_read_thread" in capsys.readouterr().out

    def test_refused_in_the_huddle_window(self, repo, capsys):  # noqa: F811
        slack, s, _ = self.seed(1)
        slack.read_thread = lambda *a: pytest.fail("no Slack call in the huddle window")
        rc = main(["history-refresh"], runtime_factory=fake_runtime(slack), clock=lambda: at(15, 0))
        out = capsys.readouterr().out
        assert rc == 0 and "calls=0" in out and "pending=1" in out and "stopped=huddle window" in out

    def test_refused_while_a_tick_runs(self, repo, capsys):  # noqa: F811
        from haseeb_agent.cli import try_lock
        slack, s, _ = self.seed(1)
        slack.read_thread = lambda *a: pytest.fail("a tick is running")
        with try_lock(cli.tick_lock_path(load_settings())):
            rc = main(["history-refresh"], runtime_factory=fake_runtime(slack), clock=lambda: NIGHT)
        assert rc == 1
        assert "a tick is running — try again in a minute" in capsys.readouterr().out

    def test_a_crash_exits_1_without_text(self, repo, capsys):  # noqa: F811
        def boom(settings):
            raise RuntimeError("SECRET")
        rc = main(["history-refresh"], runtime_factory=boom, clock=lambda: NIGHT)
        captured = capsys.readouterr()
        assert rc == 1 and "history-refresh failed: RuntimeError" in captured.err
        assert "SECRET" not in captured.out + captured.err



# -- the final review (M4): no call that could still run at 14:25; the window is checked again after each pause --

def _prepare(kind, slack, settings, store):
    if kind == "import":
        seed(slack, settings, 400)
        return lambda now_fn, sleep_fn: run_import(slack, store, settings, now_fn, sleep_fn)
    made = [insight_thread(slack, settings, i, replies=1) for i in range(8)]
    history.capture(store, settings, [parent for parent, _ in made], source_channel=settings.channel_id)
    return lambda now_fn, sleep_fn: run_thread_refresh(slack, store, settings, now_fn, sleep_fn)


@pytest.mark.parametrize("kind", ["import", "refresh"])
def test_the_window_is_checked_again_after_the_pacing_pause(settings, store, kind):
    clock = [at(14, 20)]

    def pause(seconds):
        clock[0] += timedelta(seconds=90)  # a long pause (a slow machine, or a sleep that overslept)

    run = _prepare(kind, FakeSlack(), settings, store)
    res = run(lambda: clock[0], pause)
    # call 1 at 14:20:00, call 2 at 14:21:30 (+180 s = 14:24:30), then 14:23:00 after the pause: stop
    assert (res.calls, res.stopped_reason) == (2, "huddle window")


@pytest.mark.parametrize("kind", ["import", "refresh"])
@pytest.mark.parametrize("now,calls", [
    (at(14, 21, 59), True),             # 14:24:59 at the latest: fine
    (at(14, 22, 0), False),             # a 180 s call could reach 14:25
    (at(16, 4, 59), False),             # still in the window
    (at(16, 5), True),                  # the window is over
    (datetime(2026, 10, 17, 14, 23, tzinfo=KW), True),  # a Saturday
])
def test_no_call_starts_that_could_run_into_the_huddle_window(settings, store, kind, now, calls):
    run = _prepare(kind, FakeSlack(), settings, store)
    res = run(lambda: now, lambda s: None)
    assert (res.calls > 0) is calls
    if not calls:
        assert res.stopped_reason == "huddle window"


# -- D15a: the nightly sweep reads the last 3 days of the channel once, so new replies are noticed ----------------

from haseeb_agent.jobs.history_import import SWEEP_DAYS, sweep_recent  # noqa: E402

RECENT = NIGHT - timedelta(days=2)


def recent_insight(slack, settings, ts_at, replies=2):
    ts = f"{int(ts_at.timestamp())}.000001"
    parent = slack.add_message(settings.channel_id, ts, "Daily insight: fresh", reply_count=replies,
                               latest_reply=str(int(ts_at.timestamp()) + 60))
    kids = [Msg(ts=f"{int(ts_at.timestamp()) + 30 * (k + 1)}.000001", user_id="U7", user_name="Kim",
                text=f"reply {k}", thread_ts=ts, reply_count=0, latest_reply=None) for k in range(replies)]
    slack.add_thread(settings.channel_id, parent, kids)
    return parent, kids


def test_sweep_reads_the_last_three_days_once_and_queues_new_replies(settings, store):
    slack = FakeSlack()
    fresh, _ = recent_insight(slack, settings, RECENT)
    old, _ = recent_insight(slack, settings, NIGHT - timedelta(days=5))  # older than 3 days: not on the page
    reads = []
    real = slack.read_channel
    slack.read_channel = lambda c, o, limit=50, **k: (reads.append((c, o, limit)), real(c, o, limit, **k))[1]
    sleeps = []
    res = sweep_recent(slack, store, settings, lambda: NIGHT, sleeps.append)
    assert SWEEP_DAYS == 3 and reads == [(settings.channel_id, f"{(NIGHT - timedelta(days=3)).timestamp():.6f}", 50)]
    assert (res.calls, res.messages_new, res.queued, res.stopped_reason) == (1, 1, 1, None)
    assert queue(store) == [fresh.ts] and old.ts not in queue(store)
    assert sleeps == [2.0]  # paced: the refresh's first thread read comes 2 s later
    again = sweep_recent(slack, store, settings, lambda: NIGHT, sleeps.append)
    assert again.queued == 1 and queue(store) == [fresh.ts]  # no duplicate


def test_sweep_is_refused_near_and_in_the_huddle_window(settings, store):
    slack = FakeSlack()
    slack.read_channel = lambda *a, **k: pytest.fail("no Slack call near the huddle window")
    for now in (at(14, 22), at(15, 0)):
        res = sweep_recent(slack, store, settings, lambda: now, lambda s: pytest.fail("no pause"))
        assert (res.calls, res.stopped_reason) == (0, "huddle window")


def test_sweep_with_nothing_new_makes_one_call_and_no_pause(settings, store):
    slack = FakeSlack()
    res = sweep_recent(slack, store, settings, lambda: NIGHT, lambda s: pytest.fail("nothing queued, no pause"))
    assert (res.calls, res.messages_new, res.queued, res.stopped_reason) == (1, 0, 0, None)


def test_sweep_failure_is_reported_by_kind_only(settings, store):
    slack = FakeSlack()
    slack.fail_next("slack_read_channel: boom with secret text", method="read_channel")
    res = sweep_recent(slack, store, settings, lambda: NIGHT, lambda s: None)
    assert (res.calls, res.stopped_reason) == (1, "slack_read_channel") and "secret" not in res.stopped_reason


def test_sweep_through_the_real_gateway_is_offline_guarded(settings, store, monkeypatch):
    from haseeb_agent import claude_cli
    from haseeb_agent.offline import OfflineError
    from haseeb_agent.slack_gateway import ClaudeConnectorSlack
    monkeypatch.setattr(claude_cli, "_claude_bin", lambda: "/usr/bin/false")  # harmless, should a guard ever slip
    with pytest.raises(OfflineError):
        sweep_recent(ClaudeConnectorSlack(settings), store, settings, lambda: NIGHT, lambda s: None)
