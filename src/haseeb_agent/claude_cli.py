"""Headless `claude -p` runner. Every Slack read/write goes through here, because the
Slack connector authorised on Haseeb's Claude account is only reachable via the CLI.

Do not add --strict-mcp-config or --tools "" to a Slack run: both remove the connector.

Every run leaves one row per model it used in `model_usage` (usage.py): tokens and the CLI's list-price cost, never text.

Every run is locked down (final review C1): `--permission-mode dontAsk` (a tool that is not in --allowedTools is
denied, never auto-approved; Haseeb's own ~/.claude/settings.json says `defaultMode: "auto"`) and
`--setting-sources project,local`, so user-scope settings (that default mode, his allow lists) never load.
Slack runs also pass `--disallowedTools` (see slack_gateway).
"""
from __future__ import annotations

from haseeb_agent.offline import guard
from haseeb_agent.usage import record_claude

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# The real runner, captured at import (final review I1): the offline guard compares the runner with this, not with
# `subprocess.run` at call time, which a test may have faked while the default argument still holds the real one.
_REAL_RUN = subprocess.run

# Dedicated empty working directory, so no project CLAUDE.md is loaded.
AGENT_HOME = Path.home() / ".haseeb-agent" / "claude-home"
USER_SETTINGS = Path.home() / ".claude" / "settings.json"

# Ruling R6: no hooks (they cost ~14 s per call and pollute claude-mem).
_CLI_SETTINGS = '{"disableAllHooks":true}'
# C1: never auto-approve; never load user-scope settings.
PERMISSION_MODE = "dontAsk"
SETTING_SOURCES = "project,local"

# Tools the CLI calls for its own plumbing (deferred-tool loading; from 2.1.308, waiting for the MCP servers to
# connect when tools are loaded up front). Their tool_result blocks carry no data, so they are not reported as results.
_PLUMBING_TOOLS = {"ToolSearch", "WaitForMcpServers"}


@dataclass(frozen=True)
class ToolResult:
    tool: str
    text: str
    is_error: bool
    input: dict = field(default_factory=dict)  # the arguments the model passed (tool_use input), for verification


@dataclass(frozen=True)
class ClaudeResult:
    final_text: str
    tool_results: list[ToolResult]
    cost_usd: float
    ok: bool
    error: str | None
    usage: dict = field(default_factory=dict)  # the result event's usage block, for cost measurement
    # Every tool_use block in stream order, plumbing (ToolSearch) included: (tool name, input). The gateway checks
    # these, not just the results, so a call that produced no result is still seen (C1).
    tool_uses: list[tuple[str, dict]] = field(default_factory=list)
    permission_mode: str | None = None    # from the system/init event; None when the stream had none
    init_tools: list[str] = field(default_factory=list)  # the tool list the init event announced
    # An Anthropic API error the run reported ("server_error", or the "API Error: ..." line, at most 120 chars), or
    # "". The Slack gateway retries a read once on a transient one (see slack_gateway._transient).
    api_error: str = ""
    # The subtype of the final result event ("success", "error_max_turns", ...), or None when the run had none. A run
    # that stops at --max-turns may come back as an error or as ok with no answer: this tells it from an outage.
    subtype: str | None = None


MCP_TOOL_TIMEOUT_MS = 60_000
# How long the CLI may hold the first turn for the connectors to connect (CLAUDE_CODE_MCP_STARTUP_WAIT_MS). Without it
# Claude Code 2.1.308 holds a -p run for 2 s only: on 2026-10-23 the Slack connector was not connected by then in about
# 45% of the runs, which then had to wait for it themselves, and many wrote the call out as text instead.
MCP_STARTUP_WAIT_MS = 20_000
MCP_READ_TOOLS = frozenset({"slack_read_channel", "slack_read_thread", "slack_read_file", "slack_read_user_profile",
                            "slack_list_user_channels"})


def _claude_bin() -> str:
    # launchd's PATH usually lacks ~/.local/bin.
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.exists() else "claude"


def ensure_agent_home(cwd: Path = AGENT_HOME, user_settings: Path = USER_SETTINGS) -> None:
    """Create cwd and write cwd/.claude/settings.json disabling every plugin the user has enabled.

    Regenerated on every call so newly installed plugins are covered. A missing or unreadable
    user settings file yields an empty map. The Slack connector is not a plugin and stays available.
    """
    try:
        enabled = json.loads(Path(user_settings).read_text(encoding="utf-8")).get("enabledPlugins") or {}
        keys = sorted(k for k, v in enabled.items() if v is True)
    except (OSError, ValueError, AttributeError):
        keys = []
    target = Path(cwd) / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"enabledPlugins": {k: False for k in keys}}, indent=2) + "\n"
    try:
        if target.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    target.write_text(content, encoding="utf-8")


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and "text" in b
        )
    return ""


@dataclass
class _Stream:
    results: list[ToolResult] = field(default_factory=list)
    uses: list[tuple[str, dict]] = field(default_factory=list)
    final: dict | None = None
    init: dict | None = None
    api_error: str = ""


def _parse_stream(stdout: str) -> _Stream:
    names: dict[str, tuple[str, dict]] = {}  # tool_use id -> (tool name, input)
    out = _Stream()
    results = out.results
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("error"), str) and event["error"] and not out.api_error:
            out.api_error = event["error"][:120]  # e.g. "server_error" on the assistant event (session log)
        if event.get("type") == "result":
            out.final = event
            text = event.get("result")
            if event.get("is_error") and isinstance(text, str) and text.startswith("API Error") and not out.api_error:
                out.api_error = text[:120]
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            out.init = event
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if (event.get("type") == "assistant" and block.get("type") == "text" and not out.api_error
                    and str(block.get("text", "")).startswith("API Error")):
                out.api_error = str(block["text"])[:120]
            if block.get("type") == "tool_use":
                given = block.get("input")
                entry = (str(block.get("name", "")), given if isinstance(given, dict) else {})
                names[block.get("id", "")] = entry
                out.uses.append(entry)
            elif block.get("type") == "tool_result":
                tool, given = names.get(block.get("tool_use_id", ""), ("unknown", {}))
                if tool in _PLUMBING_TOOLS:
                    continue
                results.append(ToolResult(
                    tool=tool,
                    text=_block_text(block.get("content")),
                    is_error=bool(block.get("is_error", False)),
                    input=given,
                ))
    return out


def run_claude(
    prompt: str,
    *,
    model: str,
    allowed_tools: list[str],
    timeout_s: int = 180,
    max_turns: int | None = None,
    disallowed_tools: list[str] | None = None,
    cwd: Path = AGENT_HOME,
    runner=subprocess.run,
    user_settings: Path = USER_SETTINGS,
) -> ClaudeResult:
    if runner is _REAL_RUN:
        guard("claude")
    cmd = [
        _claude_bin(), "-p", prompt,
        "--model", model,
        "--allowedTools", ",".join(allowed_tools),
        "--output-format", "stream-json",
        "--verbose",
        "--disable-slash-commands",
        "--settings", _CLI_SETTINGS,
        "--permission-mode", PERMISSION_MODE,
        "--setting-sources", SETTING_SOURCES,
    ]
    if not any(t.startswith("mcp__") for t in allowed_tools):
        # Ruling R17: a run with no MCP tool must not get the built-in read tools (Read, Grep, Glob...)
        # nor the connector, so injected prompt text cannot read local files. MCP (Slack) runs keep
        # today's argv, since both flags would drop the connector.
        cmd += ["--tools", ",".join(allowed_tools), "--strict-mcp-config"]
    if disallowed_tools:
        cmd += ["--disallowedTools", ",".join(disallowed_tools)]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]

    stream = _Stream()
    began = time.monotonic()

    def result(text: str, cost: float, ok: bool, error: str | None, usage=None, *, record: bool = True) -> ClaudeResult:
        init = stream.init or {}
        mode = init.get("permissionMode")
        tools = init.get("tools")
        subtype = (stream.final or {}).get("subtype")
        res = ClaudeResult(
            text, stream.results, cost, ok, error, usage or {}, tool_uses=stream.uses,
            permission_mode=mode if isinstance(mode, str) else None,
            init_tools=[t for t in tools if isinstance(t, str)] if isinstance(tools, list) else [],
            api_error=stream.api_error, subtype=subtype if isinstance(subtype, str) else None,
        )
        if record:  # one row per model the run used; best effort, never raises (usage.record)
            record_claude(stream.final, model, init_model=init.get("model"), ok=ok, allowed_tools=allowed_tools,
                          latency_ms=int((time.monotonic() - began) * 1000))
        return res

    def fail(error: str, cost: float = 0.0, text: str = "", usage=None, *, record: bool = True) -> ClaudeResult:
        return result(text, cost, False, error, usage, record=record)

    # Claude Code 2.1.308 (auto-updated on 2026-10-23) loads MCP tools on demand behind ToolSearch; Haiku then often
    # wrote the Slack call out as text instead of making it ("the model did not call it"), so sends and reads failed.
    # Every run gets its tools up front, as before that update.
    env = {**os.environ, "ENABLE_TOOL_SEARCH": "false"}
    mcp = [t for t in allowed_tools if t.startswith("mcp__")]
    if mcp:
        env["CLAUDE_CODE_MCP_STARTUP_WAIT_MS"] = str(MCP_STARTUP_WAIT_MS)
    if mcp and all(t.rsplit("__", 1)[-1] in MCP_READ_TOOLS for t in mcp):
        # A hung connector read (18:27 on 2026-10-17: no response for 361 s) aborts in 60 s, not 6 minutes. Claude
        # Code 2.1.302 reads MCP_TOOL_TIMEOUT (ms) as the hard wall-clock limit per MCP call. Reads only: an aborted
        # write may still land after the abort, so writes keep the connector's own limit.
        env["MCP_TOOL_TIMEOUT"] = str(MCP_TOOL_TIMEOUT_MS)
    extra = {"env": env}
    try:
        ensure_agent_home(cwd, user_settings)
        proc = runner(
            cmd, cwd=cwd, timeout=timeout_s, capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,  # otherwise the CLI waits ~3s for piped stdin
            **extra,
        )
    except subprocess.TimeoutExpired:
        return fail(f"timeout after {timeout_s}s")
    except OSError as e:
        return fail(f"could not run claude: {e}", record=False)  # the process never started: no model was called

    stream = _parse_stream(proc.stdout or "")
    final = stream.final
    cost = float((final or {}).get("total_cost_usd") or 0.0)
    text = (final or {}).get("result") or ""
    usage = (final or {}).get("usage") or {}

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:]
        return fail(f"claude exit {proc.returncode}: {tail}", cost, text, usage)
    if final is None:
        return fail("no result event in claude output")
    if final.get("is_error"):
        # M6: the subtype and a length only; the model's text can quote Slack messages
        return fail(f"claude reported an error: {final.get('subtype') or 'error'} ({len(text)} chars of text)",
                    cost, text, usage)
    return result(text, cost, True, None, usage)


_FENCE = re.compile(r"```[A-Za-z0-9_-]*\s*\n?(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict | list:
    """Pull the JSON object/array out of model text.

    A ```json fenced block is preferred. Within a candidate the parse must start at its first
    `{` or `[` (trailing prose is fine), so truncated output raises instead of returning an inner object.
    """
    decoder = json.JSONDecoder()
    for candidate in [*_FENCE.findall(text), text]:
        starts = [i for i in (candidate.find("{"), candidate.find("[")) if i >= 0]
        if not starts:
            continue
        try:
            value, _ = decoder.raw_decode(candidate, min(starts))
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("no JSON found in text")
