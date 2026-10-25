import json
import subprocess
from pathlib import Path

import pytest

from haseeb_agent.claude_cli import ClaudeResult, ToolResult, ensure_agent_home, extract_json, run_claude

FIXTURE = Path(__file__).parent / "fixtures" / "stream_read_profile.jsonl"
PROFILE_TOOL = "mcp__claude_ai_Slack__slack_read_user_profile"


class FakeRunner:
    """Stands in for subprocess.run; records the call."""

    def __init__(self, stdout="", returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.cmd = None
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test may touch the real home: default cwd and user settings point into tmp_path."""
    from haseeb_agent import claude_cli
    monkeypatch.setattr(claude_cli, "AGENT_HOME", tmp_path / "agent-home")
    monkeypatch.setattr(claude_cli.run_claude, "__kwdefaults__",
                        {**claude_cli.run_claude.__kwdefaults__,
                         "cwd": tmp_path / "agent-home", "user_settings": tmp_path / "user-settings.json"})
    monkeypatch.setattr(claude_cli.ensure_agent_home, "__defaults__",
                        (tmp_path / "agent-home", tmp_path / "user-settings.json"))
    return tmp_path


def run_fixture(**kw):
    runner = FakeRunner(stdout=FIXTURE.read_text())
    result = run_claude("p", model="haiku", allowed_tools=[PROFILE_TOOL], runner=runner, **kw)
    return result, runner


def test_parses_tool_result_from_stream():
    result, _ = run_fixture()
    assert result.ok and result.error is None
    assert len(result.tool_results) == 1  # ToolSearch plumbing is not a data tool result
    tr = result.tool_results[0]
    assert tr == ToolResult(tool=PROFILE_TOOL, text=tr.text, is_error=False, input=tr.input)
    assert tr.input and all(isinstance(k, str) for k in tr.input)  # the tool_use input is recorded, joined by id
    assert "Haseeb Zafar" in tr.text
    assert "Haseeb Zafar" in result.final_text
    assert result.cost_usd == pytest.approx(0.00802561)
    assert result.usage["cache_creation_input_tokens"] == 34280


def test_command_line_and_process_options(tmp_path):
    _, runner = run_fixture(timeout_s=42, cwd=tmp_path)
    assert runner.cmd[0].endswith("claude")
    assert runner.cmd[1:3] == ["-p", "p"]
    flags = runner.cmd[3:]
    assert flags[flags.index("--model") + 1] == "haiku"
    assert flags[flags.index("--allowedTools") + 1] == PROFILE_TOOL
    assert flags[flags.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in flags and "--disable-slash-commands" in flags
    assert "--strict-mcp-config" not in flags and "--tools" not in flags  # both drop the Slack connector
    assert runner.kwargs["timeout"] == 42
    assert runner.kwargs["cwd"] == tmp_path
    assert runner.kwargs["stdin"] == subprocess.DEVNULL  # else the CLI waits 3s for stdin
    assert runner.kwargs["capture_output"]
    assert runner.kwargs["encoding"] == "utf-8" and runner.kwargs["errors"] == "replace"
    assert "text" not in runner.kwargs  # locale decoding could raise on non-ASCII stream text
    assert flags[flags.index("--settings") + 1] == '{"disableAllHooks":true}'


def test_max_turns_flag_only_when_given():
    _, runner = run_fixture()
    assert "--max-turns" not in runner.cmd
    _, runner = run_fixture(max_turns=4)
    assert runner.cmd[runner.cmd.index("--max-turns") + 1] == "4"


def test_tool_input_joined_by_tool_use_id():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "mcp__x__a", "input": {"k": 1}},
            {"type": "tool_use", "id": "b", "name": "mcp__x__b", "input": {"k": 2}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b", "content": "B"},
            {"type": "tool_result", "tool_use_id": "a", "content": "A"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0},
    ]
    runner = FakeRunner(stdout="\n".join(json.dumps(e) for e in events))
    result = run_claude("p", model="haiku", allowed_tools=[], runner=runner)
    assert [(r.tool, r.input) for r in result.tool_results] == [("mcp__x__b", {"k": 2}), ("mcp__x__a", {"k": 1})]


def test_allowed_tools_joined_with_commas(isolated_home):
    runner = FakeRunner(stdout=FIXTURE.read_text())
    run_claude("p", model="haiku", allowed_tools=["a", "b"], runner=runner)
    assert runner.cmd[runner.cmd.index("--allowedTools") + 1] == "a,b"
    assert runner.kwargs["cwd"] == isolated_home / "agent-home"


def test_timeout_returns_not_ok():
    runner = FakeRunner(exc=subprocess.TimeoutExpired(cmd="claude", timeout=5))
    result = run_claude("p", model="haiku", allowed_tools=[], timeout_s=5, runner=runner)
    assert result.ok is False
    assert "timeout" in result.error
    assert result.tool_results == []


def test_nonzero_exit_returns_not_ok():
    runner = FakeRunner(stdout="", returncode=1, stderr="boom: not logged in")
    result = run_claude("p", model="haiku", allowed_tools=[], runner=runner)
    assert result.ok is False
    assert "exit 1" in result.error and "not logged in" in result.error


def test_missing_binary_returns_not_ok():
    runner = FakeRunner(exc=FileNotFoundError("claude"))
    result = run_claude("p", model="haiku", allowed_tools=[], runner=runner)
    assert result.ok is False and "claude" in result.error


def test_no_result_event_is_not_ok():
    runner = FakeRunner(stdout='{"type":"system","subtype":"init"}\nnot json\n')
    result = run_claude("p", model="haiku", allowed_tools=[], runner=runner)
    assert result.ok is False and "result" in result.error


def test_error_result_event_is_not_ok():
    ev = {"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": "kaput", "total_cost_usd": 0.5}
    runner = FakeRunner(stdout=json.dumps(ev) + "\n")
    result = run_claude("p", model="haiku", allowed_tools=[], runner=runner)
    # M6: the error names the subtype and the text's length, never the model's text (it can quote Slack messages)
    assert result.ok is False and "kaput" not in result.error
    assert "error_during_execution" in result.error and "5 chars" in result.error
    assert result.final_text == "kaput"  # still available to the caller, just not in the error string
    assert result.cost_usd == 0.5


def result_event(**over):
    ev = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0}
    ev.update(over)
    return json.dumps(ev) + "\n"


def run_stream(stdout="", **kw):
    return run_claude("p", model="haiku", allowed_tools=[], runner=FakeRunner(stdout=stdout, **kw))


def test_the_final_events_subtype_is_exposed():
    """A run that stops at --max-turns is told from an outage by the subtype, however the CLI reports the stop."""
    assert run_stream(result_event()).subtype == "success"
    as_error = run_stream(result_event(subtype="error_max_turns", is_error=True, result=""))
    assert as_error.ok is False and as_error.subtype == "error_max_turns" and "error_max_turns" in as_error.error
    as_exit = run_stream(result_event(subtype="error_max_turns", is_error=True, result=""), returncode=1, stderr="x")
    assert as_exit.ok is False and as_exit.subtype == "error_max_turns" and "exit 1" in as_exit.error
    as_ok = run_stream(result_event(subtype="error_max_turns", is_error=False, result=""))
    assert as_ok.ok is True and as_ok.final_text == "" and as_ok.subtype == "error_max_turns"
    other = run_stream(result_event(subtype="error_during_execution", is_error=True, result="kaput"))
    assert other.ok is False and other.subtype == "error_during_execution"


def test_no_subtype_without_a_result_event_or_a_run():
    for result in (run_stream('{"type":"system","subtype":"init"}\nnot json\n'),  # the init event's subtype is not it
                   run_claude("p", model="haiku", allowed_tools=[],
                              runner=FakeRunner(exc=subprocess.TimeoutExpired(cmd="claude", timeout=5))),
                   run_claude("p", model="haiku", allowed_tools=[], runner=FakeRunner(exc=FileNotFoundError("claude"))),
                   run_stream(result_event(subtype=7)),  # not a string: ignored
                   run_stream(result_event(subtype=None))):
        assert result.subtype is None
    assert ClaudeResult("", [], 0.0, True, None).subtype is None  # every other constructor is untouched


def test_tool_result_error_flag_and_string_content():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__x__a", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "denied"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.1},
    ]
    runner = FakeRunner(stdout="\n".join(json.dumps(e) for e in events))
    result = run_claude("p", model="haiku", allowed_tools=[], runner=runner)
    assert result.ok
    assert result.tool_results == [ToolResult(tool="mcp__x__a", text="denied", is_error=True)]
    assert result.tool_results[0].input == {}
    assert isinstance(result, ClaudeResult)


def test_extract_json_strips_fences():
    # The real fenced string seen on 2026-10-17
    text = '```json\n{"ok":true,"user_id":null,"name":"Haseeb Zafar (하시브 자파르)"}\n```'
    assert extract_json(text) == {"ok": True, "user_id": None, "name": "Haseeb Zafar (하시브 자파르)"}
    assert extract_json('```json\n{"ok":true}\n```') == {"ok": True}


def test_extract_json_strips_surrounding_prose():
    assert extract_json('Here you go: {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    assert extract_json('Result:\n```json\n{"a": 1}\n```\nDone [x]') == {"a": 1}
    assert extract_json('{"a": "}"}') == {"a": "}"}


def test_extract_json_raises_on_truncated_output():
    with pytest.raises(ValueError):
        extract_json('{"a": {"b":1}')
    with pytest.raises(ValueError):
        extract_json('```json\n{"a": [1, {"b": 2}\n```')


def test_settings_disable_exactly_the_enabled_plugins(isolated_home):
    user = isolated_home / "user-settings.json"
    user.write_text(json.dumps({"enabledPlugins": {"a@m": True, "b@m": False, "c@m": True}, "other": 1}))
    home = isolated_home / "home"
    ensure_agent_home(home, user)
    written = json.loads((home / ".claude" / "settings.json").read_text())
    assert written == {"enabledPlugins": {"a@m": False, "c@m": False}}


def test_settings_regenerated_each_run_and_tolerates_missing_user_file(isolated_home):
    home = isolated_home / "home"
    ensure_agent_home(home, isolated_home / "nope.json")
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {"enabledPlugins": {}}
    bad = isolated_home / "bad.json"
    bad.write_text("{not json")
    ensure_agent_home(home, bad)
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {"enabledPlugins": {}}
    good = isolated_home / "good.json"
    good.write_text(json.dumps({"enabledPlugins": {"x@m": True}}))
    ensure_agent_home(home, good)
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {"enabledPlugins": {"x@m": False}}


def test_run_claude_writes_settings_and_runs_in_home(isolated_home):
    user = isolated_home / "u.json"
    user.write_text(json.dumps({"enabledPlugins": {"p@m": True, "q@m": False}}))
    home = isolated_home / "h"
    runner = FakeRunner(stdout=FIXTURE.read_text())
    run_claude("p", model="haiku", allowed_tools=[], cwd=home, user_settings=user, runner=runner)
    assert runner.kwargs["cwd"] == home
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {"enabledPlugins": {"p@m": False}}
    assert runner.cmd[runner.cmd.index("--settings") + 1] == '{"disableAllHooks":true}'


def test_extract_json_raises_without_json():
    with pytest.raises(ValueError):
        extract_json("sorry, I could not do that")
    with pytest.raises(ValueError):
        extract_json("")


def test_completion_gets_no_tools_and_strict_mcp():
    runner = FakeRunner(stdout=FIXTURE.read_text())
    run_claude("p", model="haiku", allowed_tools=[], runner=runner)
    assert runner.cmd[runner.cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in runner.cmd


def test_research_gets_only_its_builtins():
    runner = FakeRunner(stdout=FIXTURE.read_text())
    run_claude("p", model="sonnet", allowed_tools=["WebSearch", "WebFetch"], runner=runner)
    assert runner.cmd[runner.cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    assert "--strict-mcp-config" in runner.cmd


def test_mcp_call_has_neither_flag():
    runner = FakeRunner(stdout=FIXTURE.read_text())
    run_claude("p", model="haiku", allowed_tools=[PROFILE_TOOL], runner=runner)
    assert "--tools" not in runner.cmd and "--strict-mcp-config" not in runner.cmd


# --- C1: headless runs never inherit Haseeb's permission mode or user settings ---------------------------------

def test_every_run_pins_dont_ask_and_project_local_settings():
    for tools in ([PROFILE_TOOL], [], ["WebSearch", "WebFetch"]):
        runner = FakeRunner(stdout=FIXTURE.read_text())
        run_claude("p", model="haiku", allowed_tools=tools, runner=runner)
        cmd = runner.cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"  # never "auto" from ~/.claude/settings.json
        assert cmd[cmd.index("--setting-sources") + 1] == "project,local"  # user-scope settings don't load
        assert "--dangerously-skip-permissions" not in cmd and "bypassPermissions" not in cmd


def test_disallowed_tools_flag_only_when_given():
    _, runner = run_fixture()
    assert "--disallowedTools" not in runner.cmd
    _, runner = run_fixture(disallowed_tools=["Bash", "mcp__claude_ai_Notion"])
    assert runner.cmd[runner.cmd.index("--disallowedTools") + 1] == "Bash,mcp__claude_ai_Notion"


def test_slack_run_argv_is_pinned(tmp_path):
    runner = FakeRunner(stdout=FIXTURE.read_text())
    run_claude("p", model="haiku", allowed_tools=[PROFILE_TOOL], disallowed_tools=["Bash", "Read"], max_turns=4,
               cwd=tmp_path, runner=runner)
    assert runner.cmd[1:] == [
        "-p", "p", "--model", "haiku", "--allowedTools", PROFILE_TOOL, "--output-format", "stream-json", "--verbose",
        "--disable-slash-commands", "--settings", '{"disableAllHooks":true}', "--permission-mode", "dontAsk",
        "--setting-sources", "project,local", "--disallowedTools", "Bash,Read", "--max-turns", "4",
    ]


def test_tool_uses_lists_every_call_in_order_including_plumbing():
    result, _ = run_fixture()
    names = [n for n, _ in result.tool_uses]
    assert names == ["ToolSearch", PROFILE_TOOL]
    assert all(isinstance(i, dict) for _, i in result.tool_uses)


def test_tool_use_without_result_is_still_listed():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "id"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0},
    ]
    result = run_claude("p", model="haiku", allowed_tools=[], runner=FakeRunner(stdout="\n".join(map(json.dumps, events))))
    assert result.tool_uses == [("Bash", {"command": "id"})]
    assert result.tool_results == []


def test_init_event_permission_mode_and_tools_are_recorded():
    init = {"type": "system", "subtype": "init", "permissionMode": "dontAsk", "tools": ["ToolSearch", PROFILE_TOOL]}
    stdout = json.dumps(init) + "\n" + FIXTURE.read_text()
    result = run_claude("p", model="haiku", allowed_tools=[PROFILE_TOOL], runner=FakeRunner(stdout=stdout))
    assert result.permission_mode == "dontAsk"
    assert result.init_tools == ["ToolSearch", PROFILE_TOOL]


def test_no_init_event_leaves_permission_mode_unknown():
    result, _ = run_fixture()
    assert result.permission_mode is None and result.init_tools == []
