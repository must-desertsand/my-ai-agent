"""Monday hardening (from 2026-10-17's logs): a shorter Slack timeout in the huddle window, MCP_TOOL_TIMEOUT, one
retry for reads on a transient failure, an honest tick.log line and failed-run metrics, and the 5-minute Next Todo
under one failed read."""
import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import cli
from haseeb_agent import slack_gateway as sg
from haseeb_agent.claude_cli import ClaudeResult, _parse_stream, run_claude
from haseeb_agent.config import load_settings
from haseeb_agent.metrics import render_weekly, week_stats
from haseeb_agent.slack_gateway import ClaudeConnectorSlack, SlackError, SlackTimeout
from haseeb_agent.store import Store
from tests.test_slack_gateway import EMPTY_PAGE, FakeRun, tool

KW = ZoneInfo("Asia/Kuwait")
MONDAY = date(2026, 10, 19)
SAT = date(2026, 10, 17)


def at(h, m, s=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=KW)


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text("LITELLM_API_KEY=k\n")
    return load_settings(Path(__file__).parent / "fixtures" / "config.toml", tmp_path / ".env")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agent.sqlite")


class Clock:
    def __init__(self, t=0.0):
        self.t, self.sleeps = t, []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def server_error():
    """A run that failed before any Slack call with an Anthropic API server_error (claude exit 1)."""
    return ClaudeResult("API Error: 500 Internal server error", [], 0.0, False, "claude exit 1: ", api_error="server_error")


def gateway(settings, run, clock=None):
    clock = clock or Clock()
    return ClaudeConnectorSlack(settings, run=run, sleep=clock.sleep, monotonic=clock.monotonic), clock


# -- 1. the huddle-window timeout and MCP_TOOL_TIMEOUT -----------------------------------------------------------

@pytest.mark.parametrize("job,now,want", [
    ("process_dm", at(14, 25), 75), ("run_todo", at(15, 30), 75), ("process_dm", at(16, 4, 59), 75),
    ("process_dm", at(14, 24, 59), None), ("run_todo", at(16, 5), None), ("process_dm", at(15, 0, day=SAT), None),
    ("run_feedback", at(15, 0), None), ("deliver_pending", at(15, 0), None),
])
def test_slack_calls_from_dm_and_todo_get_75_s_in_the_huddle_window(settings, job, now, want):
    assert cli.slack_timeout(job, now, settings) == want


def test_a_tick_that_began_before_1605_gives_an_overtime_job_the_huddle_budget(settings, store):
    """Review of 3cfd9a8: the tick starts at 16:04:50, run_todo starts at 16:05:05, in the day's overtime."""
    from haseeb_agent.jobs.todo import item_id
    from haseeb_agent.models import Item
    store.upsert_item(Item(id=item_id(MONDAY), kind="todo", day=MONDAY, status="delivered", body="Next Todo:\n1. x",
                           title="t", dm_ts="1.1", source_ts="1792495719.263309", score=None, models={}, attempts=0,
                           edited=False, created_at=at(15, 0), acted_at=None))  # the pre-draft, no notes yet
    times, seen = [at(16, 4, 50), at(16, 5, 5)], {}  # the first call is the tick's start, later ones the jobs' time
    clock = lambda: times[0] if len(times) == 1 else times.pop(0)  # noqa: E731

    def job(name):
        def run(now):
            seen[name] = (now, sg.current_timeout())
            return "ok"
        return run
    jobs = {name: job(name) for name in cli.JOBS}
    cli.run_tick(settings, store, jobs, clock=clock)
    assert seen.get("run_todo") == (at(16, 5, 5), 75)


def test_the_75_s_budget_holds_in_the_overtime(settings):
    """2026-10-24: while a long huddle keeps the todo window open past 16:05, its calls keep the huddle budget."""
    overtime = cli.TickState(todo_final=False, huddle_end=cli.OVERTIME_END)
    assert cli.slack_timeout("run_todo", at(16, 30), settings, overtime) == 75
    assert cli.slack_timeout("run_todo", at(16, 30), settings, cli.TickState(todo_final=False)) is None


def test_the_window_budget_caps_reads_only(settings):
    """Review of 6e6b2cd: a send, an upload or a reaction keeps the default 180 s even in the huddle window, so a
    slow auto-post (108-155 s on 2026-10-17) is not cut off into a false "may have failed"."""
    run = FakeRun(tool("slack_read_channel", EMPTY_PAGE), tool("slack_send_message", '{"ts": "1792150147.376979"}'),
                  tool("slack_add_reaction", "ok"), tool("slack_read_channel", EMPTY_PAGE))
    gw, _ = gateway(settings, run)
    with sg.call_timeout(75):
        gw.read_channel("C1", None)
        gw.send("C1", "x")
        gw.react("C1", "1.2", "eyes")
    gw.read_channel("C1", None)
    assert [kw["timeout_s"] for _, kw in run.calls] == [75, 180, 180, 180]


def test_mcp_runs_get_a_60_s_mcp_tool_timeout(tmp_path):
    seen = []

    def runner(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"ok"}\n', "")

    run_claude("p", model="m", allowed_tools=["mcp__claude_ai_Slack__slack_read_channel"], runner=runner,
               cwd=tmp_path, user_settings=tmp_path / "none.json")
    run_claude("p", model="m", allowed_tools=["WebSearch"], runner=runner, cwd=tmp_path,
               user_settings=tmp_path / "none.json")
    assert seen[0]["MCP_TOOL_TIMEOUT"] == "60000" and "PATH" in seen[0]
    assert seen[1] is None or "MCP_TOOL_TIMEOUT" not in seen[1]


@pytest.mark.parametrize("name", ["slack_send_message", "slack_add_reaction", "slack_get_file_upload_url",
                                  "slack_complete_file_upload"])
def test_write_runs_get_no_mcp_tool_timeout(tmp_path, name):
    """Review of 6e6b2cd: an MCP abort on a write may land after it; writes keep the connector's own timeout."""
    seen = []

    def runner(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"ok"}\n', "")

    run_claude("p", model="m", allowed_tools=["mcp__claude_ai_Slack__" + name], runner=runner, cwd=tmp_path,
               user_settings=tmp_path / "none.json")
    run_claude("p", model="m", allowed_tools=["mcp__claude_ai_Slack__slack_read_file"], runner=runner, cwd=tmp_path,
               user_settings=tmp_path / "none.json")
    assert seen[0] is None or "MCP_TOOL_TIMEOUT" not in seen[0]
    assert seen[1]["MCP_TOOL_TIMEOUT"] == "60000"


@pytest.mark.parametrize("error", ["MCP error -32001: Request timed out", "Tool call aborted: timeout",
                                   "The operation was aborted due to timeout"])
def test_a_write_tool_timeout_is_a_may_have_landed_timeout(settings, error):
    run = FakeRun(tool("slack_send_message", error, is_error=True))
    gw, _ = gateway(settings, run)
    with pytest.raises(SlackTimeout):
        gw.send("C1", "x")


def test_any_write_tool_error_after_55_s_is_a_may_have_landed_timeout(settings):
    class Slow(FakeRun):
        def __call__(self, prompt, **kw):
            clock.t += self.took
            return super().__call__(prompt, **kw)

    clock = Clock()
    run = Slow(tool("slack_send_message", "internal_error", is_error=True),
               tool("slack_send_message", "channel_not_found", is_error=True))
    gw, _ = gateway(settings, run, clock)
    run.took = 56
    with pytest.raises(SlackTimeout):
        gw.send("C1", "x")
    run.took = 3
    with pytest.raises(SlackError) as e:
        gw.send("C1", "x")
    assert not isinstance(e.value, SlackTimeout)


def test_a_read_tool_timeout_stays_a_plain_read_failure(settings):
    run = FakeRun(tool("slack_read_channel", "MCP error -32001: Request timed out", is_error=True))
    gw, _ = gateway(settings, run)
    with pytest.raises(SlackError) as e:
        gw.read_channel("C1", None)
    assert not isinstance(e.value, SlackTimeout)


def test_the_stream_reports_an_api_server_error():
    out = "\n".join(json.dumps(e) for e in [
        {"type": "assistant", "error": "server_error",
         "message": {"content": [{"type": "text", "text": "API Error: Can't reach the API server"}]}},
        {"type": "result", "is_error": True, "result": "API Error: Can't reach the API server"}])
    assert _parse_stream(out).api_error == "server_error"
    out = json.dumps({"type": "result", "is_error": True,
                      "result": 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'})
    assert _parse_stream(out).api_error.startswith("API Error: 529")
    assert _parse_stream(json.dumps({"type": "result", "result": "fine"})).api_error == ""


# -- 2. one retry for reads only -----------------------------------------------------------------------------------

def test_a_read_with_a_server_error_is_retried_once_after_5_s(settings):
    run = FakeRun(server_error(), tool("slack_read_channel", EMPTY_PAGE))
    gw, clock = gateway(settings, run)
    with sg.call_timeout(75):
        assert gw.read_channel("C1", None) == []
    assert clock.sleeps == [5] and len(run.calls) == 2
    assert run.calls[1][1]["timeout_s"] == 70  # 75 - 0 elapsed - 5 back-off


def test_a_read_the_model_did_not_call_is_retried(settings):
    run = FakeRun(ClaudeResult("I won't", [], 0.0, True, None), tool("slack_read_thread", _thread()))
    gw, clock = gateway(settings, run)
    gw.read_thread("C1", "1.0")
    assert clock.sleeps == [5] and len(run.calls) == 2


def _thread():
    return (Path(__file__).parent / "fixtures" / "thread.txt").read_text()


@pytest.mark.parametrize("first", [
    ClaudeResult("", [], 0.0, False, "timeout after 75s"),  # may have landed / budget gone: never retried
    ClaudeResult("", [], 0.0, False, "claude exit 1: boom"),  # not a transient API error
])
def test_other_read_failures_are_not_retried(settings, first):
    run = FakeRun(first, tool("slack_read_channel", EMPTY_PAGE))
    gw, clock = gateway(settings, run)
    with pytest.raises(SlackError):
        gw.read_channel("C1", None)
    assert len(run.calls) == 1 and clock.sleeps == []


def test_no_retry_without_budget_left(settings):
    class Slow(FakeRun):
        def __call__(self, prompt, **kw):
            clock.t += 60  # the failed attempt took 60 s of the 75
            return super().__call__(prompt, **kw)

    clock = Clock()
    run = Slow(server_error(), tool("slack_read_channel", EMPTY_PAGE))
    gw, _ = gateway(settings, run, clock)
    with sg.call_timeout(75), pytest.raises(SlackError):
        gw.read_channel("C1", None)
    assert len(run.calls) == 1


@pytest.mark.parametrize("call", [lambda gw: gw.send("C1", "x"), lambda gw: gw.react("C1", "1.2", "eyes")])
def test_writes_are_never_retried(settings, call):
    run = FakeRun(server_error(), server_error())
    gw, clock = gateway(settings, run)
    with pytest.raises(SlackError):
        call(gw)
    assert len(run.calls) == 1 and clock.sleeps == []


# -- 3. an honest tick.log and the weekly count --------------------------------------------------------------------

def test_a_failed_read_makes_the_jobs_line_fail_and_counts_in_the_week(settings, store, capsys):
    run = FakeRun(server_error(), server_error())
    gw, _ = gateway(settings, run)

    def dm(now):
        try:
            gw.read_channel("D1", None)
        except SlackError:
            pass  # process_dm keeps going without the page
        return "0 command(s)"

    noop = lambda now: "x"  # noqa: E731
    jobs = {"process_dm": dm, "deliver_pending": noop, "run_todo": noop, "run_feedback": noop}
    cli.run_tick(settings, store, jobs, clock=lambda: at(10, 0))
    out = capsys.readouterr().out
    assert "process_dm fail slack_read_channel server_error" in out and "process_dm ok" not in out
    assert "deliver_pending ok" in out
    (job, ok, detail, _), *_ = store.runs()
    assert (job, ok) == ("process_dm", False) and "slack_read_channel server_error" in detail
    today = date.today()  # record_run stamps the real time
    stats = week_stats(store, today - timedelta(days=today.weekday()), today=today)  # as cmd_weekly does
    assert stats.failed_runs == 1 and "Failed job runs: 1" in render_weekly(stats)


def test_a_read_that_succeeds_on_the_retry_is_ok(settings, store, capsys):
    run = FakeRun(server_error(), tool("slack_read_channel", EMPTY_PAGE))
    gw, _ = gateway(settings, run)
    noop = lambda now: "x"  # noqa: E731
    jobs = {"process_dm": lambda now: (gw.read_channel("D1", None), "0 command(s)")[1], "deliver_pending": noop,
            "run_todo": noop, "run_feedback": noop}
    cli.run_tick(settings, store, jobs, clock=lambda: at(10, 0))
    assert "process_dm ok 0 command(s)" in capsys.readouterr().out and store.runs() == []


# -- 4. the 14:30 huddle and the Next Todo inside 5 minutes, with one 75 s failed read ------------------------------

def test_huddle_detection_and_next_todo_inside_5_minutes_with_one_failed_read(settings, store, capsys):
    from haseeb_agent.jobs.todo import run_todo
    from tests.test_todo import HASEEB, CH, FILE, NOTES_MD, FakeLLM, TodoSlack, msg, notes_file, notes_reply, \
        seed_huddle, ts_at
    from haseeb_agent.slack_gateway import FileContent

    now = [at(14, 30, 0)]
    sent_at = []

    class Slow(TodoSlack):
        failed = False

        def read_channel(self, channel_id, oldest, limit=50):
            if channel_id == CH and not self.failed:
                self.failed = True
                assert sg.current_timeout() == 75  # the huddle-window budget
                now[0] += timedelta(seconds=75)  # the whole budget, then the run is killed
                raise SlackTimeout("slack_read_channel: tool-call mismatch: no call seen (timeout after 75s)")
            return super().read_channel(channel_id, oldest, limit)

        def send(self, channel_id, text, thread_ts=None):
            sent_at.append(now[0])
            return super().send(channel_id, text, thread_ts)

    slack = Slow()
    slack.files[FILE] = FileContent(text=NOTES_MD, local_path=None, mimetype="application/vnd.slack-docs")
    huddle_ts = ts_at(14, 29, 7)
    seed_huddle(slack, [notes_reply(ts=ts_at(14, 29, 50), fid=FILE)], huddle_ts=huddle_ts)
    noop = lambda n: "x"  # noqa: E731
    jobs = {"process_dm": noop, "deliver_pending": noop, "run_feedback": noop,
            "run_todo": lambda n: run_todo(slack, FakeLLM(), store, settings, n, clock=lambda: now[0]) or "done"}
    start = now[0]
    while not slack.dm_sends() and now[0] < start + timedelta(minutes=5):
        tick_at = now[0]
        cli.run_tick(settings, store, jobs, clock=lambda: now[0])
        # launchd: the next StartInterval fire at least 60 s after this one started, never while a tick runs
        nxt = tick_at + timedelta(seconds=60)
        while nxt < now[0]:
            nxt += timedelta(seconds=60)
        now[0] = nxt
    (dm,) = slack.dm_sends()
    assert dm.text.startswith("Next Todo:") and sent_at[0] - start <= timedelta(minutes=5)
    assert sent_at[0] - start <= timedelta(minutes=3)  # 14:30 fail (75 s), 14:32 success


@pytest.mark.parametrize("tools", [["mcp__claude_ai_Slack__slack_read_channel"], ["mcp__claude_ai_Slack__slack_send_message"],
                                   ["WebSearch", "WebFetch"]])
def test_every_run_gets_its_tools_up_front(tmp_path, tools):
    """2026-10-23: Claude Code 2.1.308 deferred MCP tools behind ToolSearch and Haiku wrote the Slack call as text
    instead of making it. ENABLE_TOOL_SEARCH=false loads the tools up front for reads, writes and research alike."""
    seen = []

    def runner(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"ok"}\n', "")

    run_claude("p", model="m", allowed_tools=tools, runner=runner, cwd=tmp_path, user_settings=tmp_path / "none.json")
    assert seen[0]["ENABLE_TOOL_SEARCH"] == "false" and "PATH" in seen[0]


@pytest.mark.parametrize("tools,wait", [(["mcp__claude_ai_Slack__slack_read_channel"], "20000"),
                                        (["mcp__claude_ai_Slack__slack_send_message"], "20000"),
                                        (["WebSearch", "WebFetch"], None)])
def test_a_slack_run_waits_for_its_connector_before_the_first_turn(tmp_path, tools, wait, monkeypatch):
    """2026-10-23: Claude Code 2.1.308 starts the first turn after 2 s even while a connector is still connecting. The
    Slack connector was not ready by then in about 45% of runs, and Haiku then often wrote the call out as text. A run
    with a connector tool lets the CLI wait up to 20 s; a run without one has no connector to wait for."""
    monkeypatch.delenv("CLAUDE_CODE_MCP_STARTUP_WAIT_MS", raising=False)
    seen = []

    def runner(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"ok"}\n', "")

    run_claude("p", model="m", allowed_tools=tools, runner=runner, cwd=tmp_path, user_settings=tmp_path / "none.json")
    assert seen[0].get("CLAUDE_CODE_MCP_STARTUP_WAIT_MS") == wait
