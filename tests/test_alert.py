"""I7: an out-of-band macOS alert when Slack is unreachable or a failure notice can't reach the DM."""
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import alert, cli, controls
from haseeb_agent.alert import ALERT_TEXT, SlackHealth, notifier_for, request_alert
from haseeb_agent.cli import JOBS, Runtime
from haseeb_agent.config import load_settings
from haseeb_agent.slack_gateway import SlackError, SlackPaused
from haseeb_agent.store import Store
from tests.fakes import FakeSlack

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")


def at(h, m, day=date(2026, 10, 19)):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=KW)


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    shutil.copy(ROOT / "tests" / "fixtures" / "config.toml", tmp_path / "config.toml")
    (tmp_path / ".env").write_text("LITELLM_API_KEY=k\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class DownSlack(FakeSlack):
    def __init__(self, down=True):
        super().__init__()
        self.down = down

    def read_channel(self, channel_id, oldest, limit=50):
        if self.down:
            raise SlackError("claude exit 1")
        return super().read_channel(channel_id, oldest, limit)


def tick(slack, now, notes, calls_slack=True):
    def factory(settings):
        return Runtime(settings=settings, store=Store(settings.db_path), slack=SlackHealth(slack), llm=None, handlers={})

    def jobs(rt):
        def job(n):
            if calls_slack:
                try:
                    rt.slack.read_channel("C1", None)
                except SlackError:
                    pass
            return "ok"
        return {name: job for name in JOBS}

    return cli.cmd_tick(load_settings(), factory, jobs, lambda: now, notifier=notes)


def test_the_alert_goes_through_notify_with_kind_alert_and_a_sound(repo, banners):
    """One code path: the outage alert is a notify() banner, not a separate osascript call."""
    notifier_for(load_settings())(ALERT_TEXT)
    assert [(b.kind, b.message, b.sound) for b in banners] == [("alert", ALERT_TEXT, True)]


def test_the_default_notifier_of_a_tick_is_notify(repo, banners):
    slack = DownSlack()
    for minute in (0, 10, 20):
        tick(slack, at(10, minute), None)  # no injected notifier
    assert banners.messages == [ALERT_TEXT] and banners[0].kind == "alert"


def test_the_hourly_throttle_still_holds_with_the_default_notifier(repo, banners):
    slack = DownSlack()
    for minute in range(0, 60, 10):
        tick(slack, at(10, minute), None)
    assert len(banners) == 1
    tick(slack, at(11, 20), None)
    assert len(banners) == 2


def test_a_banner_that_could_not_be_shown_does_not_start_the_throttle(repo):
    calls = []

    def failing(text):
        calls.append(text)
        return False  # notify() returns False: no notifier worked

    slack = DownSlack()
    for minute in (0, 10, 20, 30):
        tick(slack, at(10, minute), failing)
    assert len(calls) == 2  # tried at 10:20 and again at 10:30, because nothing was shown


def test_alert_text_is_fixed():
    assert ALERT_TEXT == "Haseeb's Agent can't reach Slack — check tick.err.log"


def test_three_failing_ticks_raise_one_alert(repo):
    notes, slack = Notes(), DownSlack()  # ticks 10 min apart, so every job is due (R31 cadence)
    tick(slack, at(10, 0), notes)
    tick(slack, at(10, 10), notes)
    assert notes.calls == []
    tick(slack, at(10, 20), notes)
    assert notes.calls == [ALERT_TEXT]


def test_alert_at_most_once_an_hour(repo):
    notes, slack = Notes(), DownSlack()
    for minute in range(0, 60, 10):
        tick(slack, at(10, minute), notes)
    assert len(notes.calls) == 1  # raised at 10:20
    tick(slack, at(11, 10), notes)
    assert len(notes.calls) == 1
    tick(slack, at(11, 20), notes)
    assert len(notes.calls) == 2


def test_a_successful_call_resets_the_streak(repo):
    notes, slack = Notes(), DownSlack()
    tick(slack, at(10, 0), notes)
    tick(slack, at(10, 10), notes)
    slack.down = False
    tick(slack, at(10, 20), notes)
    slack.down = True
    tick(slack, at(10, 30), notes)
    tick(slack, at(10, 40), notes)
    assert notes.calls == []


def test_ticks_without_slack_calls_do_not_count(repo):
    notes, slack = Notes(), DownSlack()
    tick(slack, at(10, 0), notes)
    tick(slack, at(10, 10), notes)
    tick(slack, at(10, 20), notes, calls_slack=False)
    assert notes.calls == []
    tick(slack, at(10, 30), notes)
    assert len(notes.calls) == 1


def test_a_failure_notice_that_cannot_be_sent_raises_an_alert(repo):
    notes = Notes()
    store = Store(load_settings().db_path)
    request_alert(store, "todo notice not sent")
    tick(FakeSlack(), at(15, 30), notes, calls_slack=False)
    assert notes.calls == [ALERT_TEXT]
    tick(FakeSlack(), at(15, 31), notes, calls_slack=False)
    assert len(notes.calls) == 1  # the request is consumed


def test_notifier_failure_never_breaks_the_tick(repo):
    def boom(text):
        raise OSError("no osascript")
    slack = DownSlack()
    for minute in range(0, 30, 10):
        assert tick(slack, at(10, minute), boom) == 0


def test_slack_health_counts_calls_and_failures():
    h = SlackHealth(DownSlack())
    with pytest.raises(SlackError):
        h.read_channel("C1", None)
    h.send("U1", "x")
    assert (h.calls, h.failures) == (2, 1)


def test_a_paused_refusal_is_neither_a_call_nor_a_failure():
    """The dashboard's Pause refuses a write before any call: it must not look like three failing ticks (an outage)."""
    class Pausing(FakeSlack):
        def send(self, channel_id, text, thread_ts=None):
            raise SlackPaused("slack_send_message: the agent is paused, nothing was sent")

    h = SlackHealth(Pausing())
    with pytest.raises(SlackPaused):
        h.send("U1", "x")
    assert (h.calls, h.failures) == (0, 0)
    h.read_channel("C1", None)
    with pytest.raises(SlackPaused):
        h.send("U1", "x")
    assert (h.calls, h.failures) == (1, 0)  # the read succeeded: not "every call failed"


def test_no_alert_request_is_filed_while_paused(tmp_path):
    """A failure notice that could not go out because Haseeb paused the agent is no outage."""
    store = Store(tmp_path / "a.sqlite")
    controls.set_paused(store, True, at(10, 0))
    request_alert(store, "an insight could not be delivered")
    assert not store.get_cursor("alert_request")
    controls.set_paused(store, False, at(10, 5))
    request_alert(store, "an insight could not be delivered")
    assert store.get_cursor("alert_request") == "an insight could not be delivered"


# -- the jobs file the request when their failure notice can't go out -----------------------------------------

def test_todo_notice_failure_requests_an_alert(tmp_path):
    from haseeb_agent.jobs.todo import notify
    from haseeb_agent.models import Item
    settings = load_settings(ROOT / "tests" / "fixtures" / "config.toml", _env(tmp_path))
    store = Store(tmp_path / "a.sqlite")
    item = Item(id="todo-2026-10-19", kind="todo", day=date(2026, 10, 19), status="failed", body="", title="t",
                dm_ts="1.1", source_ts=None, score=None, models={}, attempts=0, edited=False,
                created_at=at(15, 0), acted_at=None)
    slack = FakeSlack()
    slack.fail_next("down", method="send")
    assert notify(slack, store, settings, item, "⚠️ x") is False
    assert store.get_cursor("alert_request")


def test_insights_status_line_failure_requests_an_alert(tmp_path):
    from haseeb_agent.jobs.insights import _say
    settings = load_settings(ROOT / "tests" / "fixtures" / "config.toml", _env(tmp_path))
    store = Store(tmp_path / "a.sqlite")
    slack = FakeSlack()
    slack.fail_next("down", method="send")
    _say(slack, store, settings, "⚠️ Research failed today — no insights")
    assert store.get_cursor("alert_request")


def test_command_failure_reply_that_cannot_be_sent_requests_an_alert(tmp_path):
    from haseeb_agent.commands import process_dm
    from haseeb_agent.models import Item, Msg
    settings = load_settings(ROOT / "tests" / "fixtures" / "config.toml", _env(tmp_path))
    store = Store(tmp_path / "a.sqlite")
    now = datetime.fromtimestamp(1_800_000_500, KW)
    store.upsert_item(Item(id="i1", kind="insight", day=now.date(), status="delivered", body="b", title="t",
                           dm_ts="1800000100.000001", source_ts=None, score=None, models={}, attempts=0,
                           edited=False, created_at=now, acted_at=None))

    class Slack(FakeSlack):
        def send(self, channel_id, text, thread_ts=None):
            if text.startswith("⚠️"):
                raise SlackError("down")
            return super().send(channel_id, text, thread_ts)

    slack = Slack()
    parent = slack.add_message(settings.self_dm_id, "1800000100.000001", "item", user_id=settings.haseeb_id,
                               reply_count=1, latest_reply="1800000200")
    slack.add_thread(settings.self_dm_id, parent, [Msg(ts="1800000200.000001", user_id=settings.haseeb_id,
                                                       user_name="H", text="post", thread_ts=parent.ts,
                                                       reply_count=0, latest_reply=None)])

    class Fails:
        def post(self, item, reply_ts=None):
            raise RuntimeError("nope")

        def revise(self, item, instruction):
            raise RuntimeError("nope")

    process_dm(slack, store, settings, {"insight": Fails()}, now)
    assert store.get_cursor("alert_request")


def _env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return env


def test_alert_module_has_no_job_imports():
    import ast
    tree = ast.parse(Path(alert.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m and "jobs" in m for m in mods)
