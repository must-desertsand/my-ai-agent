"""`haseeb-agent usage-backfill` (usage_backfill.py): the usage history from Claude's session transcripts and the company
proxy's spend logs. Transcripts here are INVENTED, written in the shape of the real ones; the proxy is a fake on
`httpx.MockTransport`. Nothing real is read or called (the offline guard is on), and no text of a transcript or a log is
ever stored: the tests hunt for the invented secrets in the database file."""
import hashlib
import json
import logging
import os
import shutil
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from haseeb_agent import usage_backfill as ub
from haseeb_agent.cli import main
from haseeb_agent.jobs import enrich, feedback, insights, todo
from haseeb_agent.litellm import LLM
from haseeb_agent import judge, message_prompt, slack_gateway
from haseeb_agent.models import UsageRow
from haseeb_agent.offline import OfflineError
from haseeb_agent.store import Store
from tests.test_litellm import KEY, make_settings

ROOT = Path(__file__).resolve().parent.parent
KW = timezone(timedelta(hours=3))
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
SECRET = "SECRET-CHANNEL-MESSAGE-TEXT"
SLACK_PROMPT = slack_gateway._PROMPT.format(tool="mcp__claude_ai_Slack__slack_read_channel",
                                            args=json.dumps({"channel_id": "C1", "limit": 20, "note": SECRET}))
SID = "5f0c1c4e-7a41-4e0a-9d7b-3b1c0a5e2a11"
MY_KEY_HASH = ub.key_filter(make_settings())  # how the proxy knows the agent's key


@pytest.fixture(autouse=True)
def _restore_package_log_level():
    """main() sets the haseeb_agent logger to INFO: it must not leak into the caplog tests of other modules (the same
    guard tests/test_cli.py has)."""
    logger = logging.getLogger("haseeb_agent")
    before = logger.level
    yield
    logger.setLevel(before)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "agent.sqlite")
    yield s
    s.close()


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    return d


def rows(store):
    return store.usage_since(EPOCH)


def dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))  # compact, as the real transcripts are


def write_session(folder: Path, name: str, *, prompt: str = SLACK_PROMPT, start: datetime,
                  models: dict | None = None, wall_ms: int | None = 9000, state: bool = True,
                  messages: list[dict] | None = None, junk: bool = True, fresh: bool = False) -> Path:
    """An invented transcript in the shape of the real ones: a queue line, the user prompt, request dumps, assistant lines
    and, at the end, the `cost-state` line with the session's `modelUsage`. It was last written an hour ago, unless `fresh`."""
    ts = start.astimezone(UTC).isoformat().replace("+00:00", "Z")
    lines = [dump({"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "sessionId": name,
                   "content": prompt}),
             dump({"parentUuid": None, "type": "user", "message": {"role": "user", "content": prompt},
                   "timestamp": ts, "sessionId": name})]
    if junk:
        lines += [dump({"type": "api-request", "sessionId": name, "params": {"blob": SECRET * 50}}),
                  "this line is not JSON " + SECRET, dump(["not", "an", "object"]), ""]
    for m in messages or []:
        lines.append(dump(m))
    if state and models is not None:
        lines.append(dump({"type": "cost-state", "sessionId": name, "totalCostUSD": 1.0,
                           "startTime": int(start.timestamp() * 1000), "totalDuration": wall_ms,
                           "modelUsage": models}))
    p = folder / f"{name}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not fresh:
        age(p)
    return p


def age(path: Path, seconds: float = 3600) -> None:
    """Make a transcript look as if it was last written `seconds` ago (a file written now looks like a run still going)."""
    then = time.time() - seconds
    os.utime(path, (then, then))


def usage_of(inp=0, out=0, read=0, write=0, cost=0.0, think=0) -> dict:
    return {"inputTokens": inp, "outputTokens": out, "cacheReadInputTokens": read, "cacheCreationInputTokens": write,
            "thinkingTokens": think, "webSearchRequests": 0, "costUSD": cost}


def assistant(msg_id: str, model: str, *, inp=0, out=0, read=0, write=0, cost=None) -> dict:
    ev = {"type": "assistant", "message": {"id": msg_id, "model": model, "role": "assistant",
                                           "content": [{"type": "text", "text": SECRET}],
                                           "usage": {"input_tokens": inp, "output_tokens": out,
                                                     "cache_read_input_tokens": read,
                                                     "cache_creation_input_tokens": write}}}
    if cost is not None:
        ev["costUSD"] = cost
    return ev


def all_text(tmp_path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in (tmp_path / "data").glob("agent.sqlite*"))


# -- which job a session was for -----------------------------------------------------------------------------------------

def test_the_slack_gateways_prompt_names_the_tool_but_not_the_job_that_asked():
    """The same read serves insights, the todo, feedback and the DM commands: the transcript cannot say which, so the job is
    `unknown` and the tab shows "Earlier runs (job unknown)", not Slack (review M3). The step, the tool, is confident."""
    assert ub.infer_job_step(SLACK_PROMPT) == ("unknown", "slack_read_channel")
    assert ub.infer_job_step("\n  " + SLACK_PROMPT) == ("unknown", "slack_read_channel")


def test_the_early_and_hand_run_slack_prompts_name_a_tool_too():
    assert ub.infer_job_step("Call mcp__claude_ai_Slack__slack_read_user_profile now") == (
        "unknown", "slack_read_user_profile")
    assert ub.infer_job_step("Call the tool mcp__claude_ai_Slack__slack_add_reaction") == ("unknown", "slack_add_reaction")


def test_the_research_prompt():
    prompt = insights._research_prompt(date(2026, 10, 20), "must context", "prefs", ["a topic"])
    assert ub.infer_job_step(prompt) == ("insights", "research")


@pytest.mark.parametrize("system,job,step", [
    (lambda: insights._system("must context"), "insights", "insight_writer"),
    (lambda: judge._SYSTEM.format(blurb=judge._KIND_BLURB["insight"], axes="- a: b", example='"a": 0'), "insights", "judge"),
    (lambda: judge._SYSTEM.format(blurb=judge._KIND_BLURB["feedback"], axes="- a: b", example='"a": 0'), "feedback", "judge"),
    (lambda: feedback._system("must context"), "feedback", "feedback_writer"),
    (lambda: feedback._SUMMARIZE_SYSTEM, "feedback", "summarize"),
    (lambda: todo._system("U1"), "todo", "todo_writer"),
    (lambda: message_prompt.SYSTEM, "compose", "feedback_writer"),
    (lambda: enrich._LABEL_SYSTEM, "history", "summarize"),
    (lambda: enrich._TOPIC_SYSTEM, "history", "summarize"),
])
def test_every_chat_step_is_recognised_from_the_prompt_the_code_really_sends(system, job, step):
    """The inference rules are anchored to the real prompts: the chain sends "SYSTEM:\\n<system prompt>\\n\\nUSER:..." to
    `claude`, captured here from a real `LLM._claude_chat`. If a prompt's opening is edited, this fails and says which rule
    to update (usage_backfill._SYSTEM_OPENINGS)."""
    seen = []

    def fake(prompt, *, model, allowed_tools, **kw):
        from haseeb_agent.claude_cli import ClaudeResult
        seen.append(prompt)
        return ClaudeResult("x", [], 0.0, True, None)

    LLM(make_settings(), claude=fake).complete("mixed", system(), "the user part")
    assert seen[0].startswith("SYSTEM:\n")
    assert ub.infer_job_step(seen[0]) == (job, step)


def test_the_one_word_taken_from_a_prompt_must_be_a_tool_the_connector_has():
    """A probe run by hand ("Call mcp__claude_ai_Slack__<anything>") must not put anything of its own into a row (review M8)."""
    for tool in slack_gateway.SLACK_TOOLS:
        assert ub.infer_job_step(f"Call the tool mcp__claude_ai_Slack__{tool} once") == ("unknown", tool)
    assert ub.infer_job_step("Call mcp__claude_ai_Slack__Some_sentence_of_words_that_is_text_x" + "y" * 300) == ("unknown", "other")
    assert ub.infer_job_step("Call mcp__claude_ai_Slack__slack_read_channel_but_not_quite") == ("unknown", "other")


@pytest.mark.parametrize("prompt", [None, "", "Reply with the single word: ok", "Use WebSearch once for something",
                                    "SYSTEM:\nYou are a pirate", "Call the tool Bash once"])
def test_a_prompt_that_matches_no_rule_is_unknown(prompt):
    assert ub.infer_job_step(prompt) == ("unknown", "other")


def test_only_the_opening_of_a_prompt_is_looked_at():
    assert ub.infer_job_step("x" * 1000 + SLACK_PROMPT) == ("unknown", "other")


# -- Claude transcripts --------------------------------------------------------------------------------------------------

def test_a_session_becomes_a_row_per_model_with_its_counts_cost_and_inferred_job(tmp_path, store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)  # 09:00 in Kuwait
    write_session(folder, SID, start=start, wall_ms=8876, models={
        "claude-haiku-5-5": usage_of(inp=6, out=291, read=24003, write=1212, cost=0.00062853),
        "claude-sonnet-5-5": usage_of(inp=2, out=900, read=100, write=50, cost=0.4)})
    res = ub.backfill_claude(store, folder, KW)
    assert (res.sessions, res.added_rows, res.existing_rows, res.newer, res.empty) == (1, 2, 0, 0, 0)
    main_row, side = rows(store)
    assert (main_row.model, main_row.cost_usd, main_row.output_tokens, main_row.latency_ms) == (
        "claude-sonnet-5-5", 0.4, 900, 8876)
    assert (side.model, side.input_tokens, side.output_tokens, side.cache_read_tokens, side.cache_write_tokens,
            side.cost_usd, side.latency_ms) == ("claude-haiku-5-5", 6, 291, 24003, 1212, 0.00062853, None)
    for r in (main_row, side):
        assert (r.provider, r.source, r.ref, r.job, r.step, r.cost_basis, r.ok) == (
            "claude", "backfill", f"{SID}.jsonl", "unknown", "slack_read_channel", "list", True)
        assert r.at == datetime(2026, 10, 20, 9, 0, tzinfo=KW) and r.at.utcoffset() == timedelta(hours=3)
    assert res.first_day == date(2026, 10, 20)


def test_no_text_of_a_transcript_is_stored(tmp_path, store, folder):
    write_session(folder, "a", start=datetime(2026, 10, 20, 6, 0, tzinfo=UTC), models={"claude-haiku-5-5": usage_of(out=5)},
                  messages=[assistant("m1", "claude-haiku-5-5", out=5)])
    ub.backfill_claude(store, folder, KW)
    store.close()
    blob = all_text(tmp_path)
    assert SECRET.encode() not in blob and b"channel_id" not in blob


def test_running_it_again_adds_nothing(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    write_session(folder, "a", start=start, models={"claude-haiku-5-5": usage_of(out=5, cost=0.1)})
    write_session(folder, "b", start=start + timedelta(hours=1), models={"claude-haiku-5-5": usage_of(out=7, cost=0.2)})
    first = ub.backfill_claude(store, folder, KW)
    second = ub.backfill_claude(store, folder, KW)
    assert (first.added_rows, first.existing_rows) == (2, 0)
    assert (second.added_rows, second.existing_rows, second.sessions) == (0, 2, 2)
    assert store.usage_count() == 2
    write_session(folder, "c", start=start + timedelta(hours=2), models={"claude-haiku-5-5": usage_of(out=9)})
    third = ub.backfill_claude(store, folder, KW)  # a new session is added, the old ones are not
    assert (third.added_rows, third.existing_rows) == (1, 2)


def test_sessions_after_the_first_live_record_are_left_to_the_live_record(store, folder):
    live_at = datetime(2026, 10, 21, 10, 0, tzinfo=KW)
    store.add_usage(UsageRow(at=live_at, provider="claude", model="claude-haiku-5-5", source="live"))
    write_session(folder, "before", start=live_at - timedelta(hours=1), models={"claude-haiku-5-5": usage_of(out=1)})
    write_session(folder, "after", start=live_at + timedelta(minutes=5), models={"claude-haiku-5-5": usage_of(out=2)})
    write_session(folder, "same", start=live_at, models={"claude-haiku-5-5": usage_of(out=3)})
    res = ub.backfill_claude(store, folder, KW)
    assert (res.sessions, res.added_rows, res.newer) == (3, 1, 2)
    assert [r.ref for r in rows(store) if r.source == "backfill"] == ["before.jsonl"]


def test_a_run_the_live_record_has_is_never_added_again_even_when_it_looks_old(store, folder):
    """The live row of a Claude run carries the session's file name as its ref, so the backfill finds it by name."""
    live_at = datetime(2026, 10, 21, 10, 0, tzinfo=KW)
    store.add_usage(UsageRow(at=live_at, provider="claude", model="claude-haiku-5-5", source="live", ref=f"{SID}.jsonl"))
    write_session(folder, SID, start=live_at - timedelta(minutes=1), models={"claude-haiku-5-5": usage_of(out=1)})
    res = ub.backfill_claude(store, folder, KW)
    assert (res.added_rows, res.existing_rows) == (0, 1)


def test_without_a_cost_state_the_assistant_messages_are_summed_once_per_message(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    write_session(folder, "old-format", start=start, state=False, messages=[
        assistant("m1", "claude-haiku-5-5", inp=3, out=10, read=100, write=7, cost=0.01),
        assistant("m1", "claude-haiku-5-5", inp=3, out=25, read=100, write=7, cost=0.02),  # m1 again, further on
        assistant("m2", "claude-haiku-5-5", inp=1, out=5, read=50, write=0, cost=0.03),
        assistant("m3", "<synthetic>", inp=99, out=99),                                    # the CLI's placeholder
        assistant("m4", "claude-sonnet-5-5", out=40)])
    res = ub.backfill_claude(store, folder, KW)
    assert (res.added_rows, res.approx) == (0, 1)  # approximate rows are counted apart: they are recomputed on every run
    assert {r.ref for r in rows(store)} == {"approx:old-format.jsonl"}
    by_model = {r.model: r for r in rows(store)}
    h = by_model["claude-haiku-5-5"]
    assert (h.input_tokens, h.output_tokens, h.cache_read_tokens, h.cache_write_tokens) == (4, 30, 150, 7)
    assert (h.cost_usd, h.cost_basis) == (pytest.approx(0.05), "list")  # m1's larger figure + m2
    s = by_model["claude-sonnet-5-5"]
    assert (s.cost_usd, s.cost_basis, s.output_tokens) == (None, "unknown", 40)  # no cost anywhere: none is made up
    assert "<synthetic>" not in by_model


def test_a_session_with_no_usage_adds_nothing(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    write_session(folder, "limit", start=start, models={})                                       # the weekly limit
    write_session(folder, "zeros", start=start, models={"claude-haiku-5-5": usage_of()})
    write_session(folder, "no-state", start=start, state=False)
    write_session(folder, "synthetic-only", start=start, state=False, messages=[assistant("m", "<synthetic>", out=3)])
    (folder / "garbage.jsonl").write_text("not json\n{\n", encoding="utf-8")
    (folder / "empty.jsonl").write_text("", encoding="utf-8")
    (folder / "binary.jsonl").write_bytes(b"\xff\xfe\x00\x01 \xc3\x28\n")
    (folder / "notes.txt").write_text("not a transcript", encoding="utf-8")
    res = ub.backfill_claude(store, folder, KW)
    assert (res.sessions, res.added_rows, res.empty) == (7, 0, 7) and store.usage_count() == 0
    assert res.first_day is None


def test_one_unreadable_transcript_does_not_stop_the_others(store, folder, monkeypatch):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    write_session(folder, "good", start=start, models={"claude-haiku-5-5": usage_of(out=5, cost=0.1)})
    write_session(folder, "odd", start=start, models={"claude-haiku-5-5": usage_of(out=6, cost=0.2)})
    real = ub.read_session

    def read(path, tz):
        if path.stem == "odd":
            raise ValueError("an odd value: SECRET-CHANNEL-MESSAGE-TEXT")
        return real(path, tz)

    monkeypatch.setattr(ub, "read_session", read)
    res = ub.backfill_claude(store, folder, KW)
    assert (res.sessions, res.added_rows, res.unreadable) == (2, 1, 1)
    assert [r.ref for r in rows(store)] == ["good.jsonl"]


def test_odd_values_in_a_transcript_are_cleaned_and_never_abort_the_run(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    weird = {"inputTokens": "²", "outputTokens": 2**80, "cacheReadInputTokens": "١٢٣", "cacheCreationInputTokens": -5,
             "costUSD": float("inf")}
    write_session(folder, "weird", start=start, models={"x" * 200: weird, "claude-haiku-5-5": usage_of(out=5, cost=0.1)})
    res = ub.backfill_claude(store, folder, KW)
    assert (res.added_rows, res.rejected, res.unreadable) == (2, 0, 0)
    by_model = {r.model: r for r in rows(store)}
    r = by_model["unknown"]  # a model name that is not an identifier
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, r.cost_usd) == (
        None, 2**62, None, None, None)


def test_a_row_the_store_will_not_take_is_counted_and_the_run_goes_on(store, folder, monkeypatch):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    write_session(folder, "a", start=start, models={"claude-haiku-5-5": usage_of(out=5)})
    write_session(folder, "b", start=start, models={"claude-haiku-5-5": usage_of(out=6)})
    real = ub.session_rows

    def rows_of(s):
        made = real(s)
        if s.name == "b.jsonl":
            made[0].ref = "a sentence, not an identifier"
        return made

    monkeypatch.setattr(ub, "session_rows", rows_of)
    res = ub.backfill_claude(store, folder, KW)
    assert (res.added_rows, res.rejected) == (1, 1) and [r.ref for r in rows(store)] == ["a.jsonl"]
    code, out = run(store, folder, skip_proxy=True)
    assert code == 0 and out[0].endswith("0 without usage, 1 row(s) rejected")


def test_one_odd_proxy_log_does_not_stop_the_others(store, monkeypatch):
    t = datetime(2026, 10, 19, 9, 30, tzinfo=KW)
    proxy = FakeProxy(logs=[spend_log("ok-1", "glm-5.3", t), spend_log("odd", "glm-5.3", t), spend_log("ok-2", "glm-5.3", t)])
    real = ub.litellm_row

    def row(entry, tz):
        if entry.get("request_id") == "odd":
            raise TypeError("an odd value")
        return real(entry, tz)

    monkeypatch.setattr(ub, "litellm_row", row)
    res = litellm(store, proxy)
    assert (res.added_rows, res.rejected) == (2, 1)


def test_a_transcript_cut_off_mid_line_is_read_as_far_as_it_goes(store, folder):
    p = write_session(folder, "cut", start=datetime(2026, 10, 20, 6, 0, tzinfo=UTC), state=False,
                      messages=[assistant("m1", "claude-haiku-5-5", out=8)])
    p.write_text(p.read_text() + '{"type":"assistant","message":{"id":"m2","model":"claude-haiku', encoding="utf-8")
    age(p)
    assert ub.backfill_claude(store, folder, KW).approx == 1
    assert rows(store)[0].output_tokens == 8


def test_a_transcript_still_being_written_is_left_for_the_live_record_or_the_next_run(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    write_session(folder, "running", start=start, state=False, fresh=True,
                  messages=[assistant("m1", "claude-haiku-5-5", out=8)])
    write_session(folder, "done", start=start, state=False, messages=[assistant("m1", "claude-haiku-5-5", out=9)])
    res = ub.backfill_claude(store, folder, KW)
    assert (res.sessions, res.approx, res.busy) == (2, 1, 1)
    assert [r.ref for r in rows(store)] == ["approx:done.jsonl"]
    later = ub.backfill_claude(store, folder, KW, now=time.time() + 3600)  # an hour on, nothing has been written to it
    assert (later.approx, later.busy) == (2, 0)
    assert sorted(r.ref for r in rows(store)) == ["approx:done.jsonl", "approx:running.jsonl"]


# -- a transcript with no final figures is approximate (review M1) ---------------------------------------------------------

def test_an_idle_transcript_with_no_cost_state_is_approximate_and_recomputed_on_every_run(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    p = write_session(folder, "killed", start=start, state=False, messages=[assistant("m1", "claude-haiku-5-5", inp=2, out=10)])
    first = ub.backfill_claude(store, folder, KW)
    assert (first.approx, first.added_rows) == (1, 0)
    [r] = rows(store)
    assert (r.ref, r.output_tokens, r.cost_usd, r.cost_basis, r.latency_ms, r.source) == (
        "approx:killed.jsonl", 10, None, "unknown", None, "backfill")
    p.write_text(p.read_text() + dump(assistant("m2", "claude-haiku-5-5", inp=1, out=25)) + "\n", encoding="utf-8")
    age(p)
    second = ub.backfill_claude(store, folder, KW)  # it grew: what an earlier run made of it is replaced, not added to
    [r] = rows(store)
    assert second.approx == 1 and (r.input_tokens, r.output_tokens) == (3, 35) and store.usage_count() == 1
    third = ub.backfill_claude(store, folder, KW)
    assert third.approx == 1 and store.usage_count() == 1 and [x.output_tokens for x in rows(store)] == [35]


def test_the_approximate_rows_of_a_run_are_replaced_when_its_cost_state_line_appears(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    p = write_session(folder, "late", start=start, state=False,
                      messages=[assistant("m1", "claude-haiku-5-5", out=10), assistant("m2", "claude-sonnet-5-5", out=3)])
    ub.backfill_claude(store, folder, KW)
    assert sorted(r.ref for r in rows(store)) == ["approx:late.jsonl", "approx:late.jsonl"]  # a row per model
    write_session(folder, "late", start=start, models={"claude-haiku-5-5": usage_of(inp=5, out=99, cost=0.2)})  # the final figures
    res = ub.backfill_claude(store, folder, KW)
    assert (res.added_rows, res.approx) == (1, 0)
    [r] = rows(store)
    assert (r.ref, r.model, r.output_tokens, r.cost_usd) == ("late.jsonl", "claude-haiku-5-5", 99, 0.2)  # no approximate left
    again = ub.backfill_claude(store, folder, KW)
    assert (again.added_rows, again.existing_rows) == (0, 1) and store.usage_count() == 1


def test_a_run_that_was_never_approximate_loses_nothing_to_the_replacement(store, folder):
    start = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    write_session(folder, "final", start=start, models={"claude-haiku-5-5": usage_of(out=5, cost=0.1)})
    write_session(folder, "other", start=start, state=False, messages=[assistant("m1", "claude-haiku-5-5", out=4)])
    ub.backfill_claude(store, folder, KW)
    ub.backfill_claude(store, folder, KW)
    assert sorted(r.ref for r in rows(store)) == ["approx:other.jsonl", "final.jsonl"]


def test_run_says_how_many_approximate_runs_it_summed(store, folder):
    write_session(folder, "killed", start=datetime(2026, 10, 20, 6, 0, tzinfo=UTC), state=False,
                  messages=[assistant("m1", "claude-haiku-5-5", out=8)])
    code, out = run(store, folder, skip_proxy=True)
    assert code == 0 and out[0].endswith("0 without usage, 1 approximate")
    code, again = run(store, folder, skip_proxy=True)
    assert again[0].endswith("0 without usage, 1 approximate")  # the same line on a rerun: nothing is "added" twice


def test_a_transcript_with_its_cost_state_is_final_however_new(store, folder):
    write_session(folder, "just-ended", start=datetime(2026, 10, 20, 6, 0, tzinfo=UTC), fresh=True,
                  models={"claude-haiku-5-5": usage_of(out=5, cost=0.1)})
    res = ub.backfill_claude(store, folder, KW)
    assert (res.added_rows, res.busy) == (1, 0)


def test_a_missing_folder_is_reported_not_an_error(tmp_path, store):
    res = ub.backfill_claude(store, tmp_path / "nope", KW)
    assert res.missing and res.sessions == 0


def test_the_default_folder_is_the_agents_claude_home():
    assert ub.transcripts_dir(Path("/Users/cb/.haseeb-agent/claude-home"), Path("/h/.claude/projects")) == Path(
        "/h/.claude/projects/-Users-cb--haseeb-agent-claude-home")


def test_a_naive_start_time_is_utc(store, folder):
    p = write_session(folder, "naive", start=datetime(2026, 10, 20, 6, 0, tzinfo=UTC), state=False,
                      messages=[assistant("m1", "claude-haiku-5-5", out=8)])
    p.write_text(p.read_text().replace("2026-10-20T06:00:00Z", "2026-10-20T06:00:00"), encoding="utf-8")
    age(p)
    ub.backfill_claude(store, folder, KW)
    assert rows(store)[0].at == datetime(2026, 10, 20, 9, 0, tzinfo=KW)


# -- the company proxy ---------------------------------------------------------------------------------------------------

def spend_log(req_id, model, start: datetime, *, prompt=100, completion=20, spend=0.002, took=2.5,
              call_type="acompletion", **kw) -> dict:
    return {"request_id": req_id, "model": model + "-0613", "model_group": model, "call_type": call_type,
            "startTime": start.astimezone(UTC).isoformat(), "endTime": (start + timedelta(seconds=took)).astimezone(UTC).isoformat(),
            "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion, "spend": spend,
            "api_key": MY_KEY_HASH, "messages": SECRET, "response": {"choices": [SECRET]}, "metadata": {"q": SECRET}, **kw}


KEY_INFO = {"info": {"spend": 1000.0, "model_spend": {"glm-5.3": 1000.0}}}  # what /key/info says about the agent's key


def as_json(status: int, data) -> httpx.Response:
    """A JSON answer, `null` included (httpx's `json=None` means no body at all)."""
    return httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json"})


class FakeProxy:
    """Answers GET /spend/logs and GET /key/info as scripted; remembers what it was asked."""

    def __init__(self, logs=None, spend_status=200, key_info=KEY_INFO, key_status=200, exc=None, by_summarize=None):
        self.logs, self.spend_status, self.key_info, self.key_status, self.exc = logs, spend_status, key_info, key_status, exc
        self.by_summarize = by_summarize  # {"false": answer, "true": answer}: what /spend/logs says to each way of asking
        self.requests = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.exc:
            raise self.exc
        if req.url.path == "/spend/logs":
            answer = self.by_summarize[req.url.params.get("summarize")] if self.by_summarize else self.logs
            if isinstance(answer, Exception):
                raise answer
            return as_json(self.spend_status, answer if self.spend_status == 200 else {"error": SECRET})
        if req.url.path == "/key/info":
            return as_json(self.key_status, self.key_info if self.key_status == 200 else {"error": SECRET})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=KW)
D0 = date(2026, 10, 17)


def litellm(store, proxy, **kw):
    return ub.backfill_litellm(store, make_settings(), D0, NOW, proxy.client(), **kw)


def test_per_request_logs_become_rows_with_the_proxys_own_cost(tmp_path, store):
    t = datetime(2026, 10, 19, 9, 30, tzinfo=KW)
    proxy = FakeProxy(logs=[
        spend_log("req-1", "deepseek-v4-pro", t, prompt=120, completion=30, spend=0.0021),
        spend_log("req-2", "qwen3-embedding", t + timedelta(minutes=1), prompt=40, completion=0, spend=0.00001,
                  call_type="aembedding"),
        spend_log("req-3", "glm-5.3", t + timedelta(minutes=2), prompt=10, completion=0, spend=0.0, status="failure"),
    ])
    res = litellm(store, proxy)
    assert (res.state, res.added_rows, res.existing_rows) == ("imported", 3, 0)
    a, b, c = rows(store)
    assert (a.provider, a.model, a.ref, a.source, a.job, a.step) == ("litellm", "deepseek-v4-pro", "req-1", "backfill",
                                                                     "other", "other")
    assert (a.input_tokens, a.output_tokens, a.cost_usd, a.cost_basis, a.latency_ms, a.ok) == (
        120, 30, 0.0021, "proxy", 2500, True)
    assert a.at == t and a.at.utcoffset() == timedelta(hours=3)
    assert (b.step, b.input_tokens, b.output_tokens) == ("embed", 40, None)
    assert (c.ok, c.cost_usd, c.cost_basis) == (False, 0.0, "proxy")
    store.close()
    assert SECRET.encode() not in all_text(tmp_path)


def test_the_proxy_is_asked_with_the_agents_key_for_per_request_logs_over_the_window(store):
    proxy = FakeProxy(logs=[])
    litellm(store, proxy)
    [req] = proxy.requests
    assert req.url.path == "/spend/logs" and req.method == "GET"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert dict(req.url.params) == {"start_date": "2026-10-17", "end_date": "2026-10-22", "summarize": "false",
                                    "api_key": MY_KEY_HASH}  # only this key's spend, asked for by the hash the proxy keeps


# -- only the agent's own key (review M2) --------------------------------------------------------------------------------

def test_both_ways_of_asking_name_this_keys_hash(store):
    proxy = FakeProxy(by_summarize={"false": httpx.ReadTimeout("slow"), "true": [summary("2026-10-18", {"glm-5.3": 0.5})]})
    litellm(store, proxy)
    asked = [r.url.params for r in proxy.requests if r.url.path == "/spend/logs"]
    assert [p["summarize"] for p in asked] == ["false", "true"] and all(p["api_key"] == MY_KEY_HASH for p in asked)
    assert len(MY_KEY_HASH) == 64 and KEY not in MY_KEY_HASH and MY_KEY_HASH == hashlib.sha256(KEY.encode()).hexdigest()


def test_logs_that_name_another_key_mean_the_proxy_did_not_filter_and_nothing_is_imported(store):
    t = datetime(2026, 10, 19, 9, 30, tzinfo=KW)
    logs = [spend_log("mine", "glm-5.3", t), spend_log("theirs", "glm-5.3", t, api_key="b" * 64)]
    res = litellm(store, FakeProxy(logs=logs))
    assert (res.state, res.added_rows) == ("unavailable", 0) and store.usage_count() == 0
    assert res.reason == "/spend/logs returned another key's logs (the proxy did not filter by key)"
    assert KEY not in res.reason and MY_KEY_HASH not in res.reason and "b" * 64 not in res.reason


def test_logs_of_this_key_are_taken_whether_it_is_named_hashed_plain_or_not_at_all(store):
    t = datetime(2026, 10, 19, 9, 30, tzinfo=KW)
    plain = spend_log("plain", "glm-5.3", t, api_key=KEY)
    unnamed = spend_log("unnamed", "glm-5.3", t)
    del unnamed["api_key"]
    empty = spend_log("empty", "glm-5.3", t, api_key="")
    res = litellm(store, FakeProxy(logs=[spend_log("hashed", "glm-5.3", t), plain, unnamed, empty]))
    assert (res.state, res.added_rows) == ("imported", 4)


def test_a_summary_is_taken_when_the_keys_own_spend_covers_it(store):
    days = [summary("2026-10-18", {"glm-5.3": 3.0}), summary("2026-10-19", {"glm-5.3": 2.0})]
    res = litellm(store, FakeProxy(logs=days, key_info={"info": {"spend": 5.0}}))
    assert (res.state, res.added_rows) == ("imported", 2)
    again = litellm(store, FakeProxy(logs=days, key_info={"spend": 5.04}))  # (a shape without "info", and a hair of slack)
    assert again.state == "imported"


@pytest.mark.parametrize("key_info,key_status,reason", [
    ({"info": {"spend": 4.0}}, 200, "/spend/logs: the daily totals are more than this key's own spend (the proxy did not filter by key)"),
    ({"info": {}}, 200, "/spend/logs: the daily totals cannot be tied to this key (/key/info has no spend)"),
    ({"info": {"spend": "lots"}}, 200, "/spend/logs: the daily totals cannot be tied to this key (/key/info has no spend)"),
    (None, 404, "/spend/logs: the daily totals cannot be tied to this key (/key/info 404)"),
    (None, 200, "/spend/logs: the daily totals cannot be tied to this key (/key/info empty)"),
])
def test_a_summary_that_cannot_be_tied_to_the_key_is_not_taken(store, key_info, key_status, reason):
    """A summary cannot be filtered afterwards: counted for the wrong key it is someone else's spend counted as Haseeb's."""
    days = [summary("2026-10-18", {"glm-5.3": 3.0}), summary("2026-10-19", {"glm-5.3": 2.0})]
    res = litellm(store, FakeProxy(logs=days, key_info=key_info, key_status=key_status))
    assert (res.state, res.reason, res.added_rows) == ("unavailable", reason, 0) and store.usage_count() == 0


def test_a_summary_with_another_keys_breakdown_is_refused_without_asking_key_info(store):
    day = {**summary("2026-10-18", {"glm-5.3": 3.0}), "api_keys": {MY_KEY_HASH: 1.0, "c" * 64: 2.0}}
    proxy = FakeProxy(logs=[day])
    res = litellm(store, proxy)
    assert res.reason == "/spend/logs: the daily totals name other keys (the proxy did not filter by key)"
    assert [r.url.path for r in proxy.requests] == ["/spend/logs"] and store.usage_count() == 0
    mine = {**summary("2026-10-18", {"glm-5.3": 3.0}), "api_keys": {MY_KEY_HASH: 3.0}}
    assert litellm(store, FakeProxy(logs=[mine])).state == "imported"


# -- the size cap, enforced while the answer streams in (review M2) -------------------------------------------------------

class Chunks(httpx.SyncByteStream):
    """A body that would be `n` chunks of `size` bytes, and counts how many were taken."""

    def __init__(self, n: int, size: int = 100):
        self.n, self.size, self.taken = n, size, 0

    def __iter__(self):
        for _ in range(self.n):
            self.taken += 1
            yield b" " * self.size


def streamed(body: Chunks, **headers):
    return lambda req: httpx.Response(200, stream=body, headers={"content-type": "application/json", **headers})


def test_a_huge_answer_is_refused_as_it_streams_in_not_after_all_of_it_is_in_memory(store, monkeypatch):
    monkeypatch.setattr(ub, "MAX_PROXY_BYTES", 1000)
    body = Chunks(1_000_000)  # a hundred megabytes, if anyone read it all
    client = httpx.Client(transport=httpx.MockTransport(streamed(body)))
    payload, why = ub._ask(client, make_settings(), "/spend/logs")
    assert (payload, why) == (None, "too large") and body.taken == 11  # 11 x 100 bytes passed 1000, and nothing more was read


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read(monkeypatch):
    monkeypatch.setattr(ub, "MAX_PROXY_BYTES", 1000)
    body = Chunks(10)
    client = httpx.Client(transport=httpx.MockTransport(streamed(body, **{"content-length": "5000"})))
    assert ub._ask(client, make_settings(), "/spend/logs") == (None, "too large") and body.taken == 0


def test_an_answer_just_under_the_cap_is_read(monkeypatch):
    monkeypatch.setattr(ub, "MAX_PROXY_BYTES", 1000)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'[' + b' ' * 990 + b']')))
    assert ub._ask(client, make_settings(), "/spend/logs") == ([], "")


def test_an_answer_that_trickles_in_past_the_deadline_is_a_timeout(monkeypatch):
    monkeypatch.setattr(ub, "PROXY_DEADLINE_S", -1)
    client = httpx.Client(transport=httpx.MockTransport(streamed(Chunks(5))))
    assert ub._ask(client, make_settings(), "/spend/logs") == (None, "timeout")


def test_an_answer_that_is_not_json_or_not_utf8_is_named_as_such(monkeypatch):
    for body in (b"<html>nope</html>", b"\xff\xfe\xfa"):
        client = httpx.Client(transport=httpx.MockTransport(lambda req, b=body: httpx.Response(200, content=b)))
        assert ub._ask(client, make_settings(), "/spend/logs") == (None, "not JSON")


def test_an_error_status_is_named_without_reading_its_body():
    body = Chunks(1000)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500, stream=body)))
    assert ub._ask(client, make_settings(), "/spend/logs") == (None, "500") and body.taken == 0


def test_proxy_history_is_added_once_and_only_before_the_first_live_record(store):
    t = datetime(2026, 10, 20, 9, 0, tzinfo=KW)
    store.add_usage(UsageRow(at=t + timedelta(hours=5), provider="litellm", model="glm-5.3", source="live"))
    proxy = FakeProxy(logs=[spend_log("old", "glm-5.3", t), spend_log("new", "glm-5.3", t + timedelta(hours=6))])
    first = litellm(store, proxy)
    second = litellm(store, proxy)
    assert (first.added_rows, first.newer) == (1, 1) and (second.added_rows, second.existing_rows) == (0, 1)
    assert sorted(r.ref or "-" for r in rows(store)) == ["-", "old"]


def test_a_live_row_with_the_proxys_call_id_blocks_the_backfilled_copy(store):
    t = datetime(2026, 10, 20, 9, 0, tzinfo=KW)
    store.add_usage(UsageRow(at=t + timedelta(hours=9), provider="litellm", model="glm-5.3", source="live", ref="req-9"))
    proxy = FakeProxy(logs=[spend_log("req-9", "glm-5.3", t)])  # looks older, but it is the same call
    res = litellm(store, proxy)
    assert (res.added_rows, res.existing_rows) == (0, 1)


def test_a_log_without_a_request_id_gets_a_stable_ref(store):
    t = datetime(2026, 10, 19, 9, 30, tzinfo=KW)
    log = spend_log("x", "glm-5.3", t)
    del log["request_id"]
    proxy = FakeProxy(logs={"data": [log]})
    assert litellm(store, proxy).added_rows == 1
    assert litellm(store, proxy).added_rows == 0  # the same log again is the same row
    assert rows(store)[0].ref.startswith("h-")


def test_logs_without_a_time_or_a_model_are_left_out(store):
    t = datetime(2026, 10, 19, 9, 30, tzinfo=KW)
    no_model = {**spend_log("b", "glm-5.3", t), "model": None, "model_group": None}
    no_time = {**spend_log("d", "glm-5.3", t), "startTime": "yesterday-ish"}
    proxy = FakeProxy(logs=[{"request_id": "a", "model": "m"}, no_model, no_time, spend_log("c", "glm-5.3", t), "junk", 7])
    assert litellm(store, proxy).added_rows == 1


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_a_proxy_that_refuses_is_skipped_with_the_status_and_nothing_else(store, status):
    proxy = FakeProxy(spend_status=status, key_status=status)
    res = litellm(store, proxy)
    assert res.state == "unavailable" and res.reason == f"/spend/logs {status}; /key/info {status}"
    assert SECRET not in res.reason and KEY not in res.reason
    assert store.usage_count() == 0


def test_a_proxy_that_times_out_is_skipped(store):
    proxy = FakeProxy(exc=httpx.ReadTimeout("slow"))
    res = litellm(store, proxy)
    assert (res.state, res.reason) == ("unavailable", "/spend/logs timeout; /key/info timeout")


def test_a_proxy_that_cannot_be_reached_is_skipped_by_error_type(store):
    res = litellm(store, FakeProxy(exc=httpx.ConnectError("no route")))
    assert res.reason == "/spend/logs ConnectError; /key/info ConnectError"


def summary(day: str, models: dict) -> dict:
    """One day of the proxy's summary: the spend of each model (the proxy's days are UTC days)."""
    return {"startTime": f"{day}T00:00:00", "spend": sum(v for v in models.values() if isinstance(v, float)),
            "users": {"u": 1.0}, "models": models}


def test_a_daily_summary_becomes_a_cost_only_row_per_model_and_day(store):
    # NOW is 12:00 on the 21st in Kuwait, 09:00 UTC: the UTC days up to the 20th are over, the 21st is not
    proxy = FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 0.5, "deepseek-v4-pro": 0.25}),
                            summary("2026-10-20", {"glm-5.3": 1.5}), summary("2026-10-21", {"glm-5.3": 9.0})])
    res = litellm(store, proxy)
    assert (res.state, res.daily, res.added_rows, res.existing_rows, res.newer) == ("imported", True, 3, 0, 1)
    a, b, c = rows(store)
    for r in (a, b, c):
        assert (r.provider, r.job, r.step, r.cost_basis, r.source, r.ok) == ("litellm", "other", "daily_total", "proxy",
                                                                           "backfill", True)
        assert (r.input_tokens, r.output_tokens, r.latency_ms) == (None, None, None)  # a day total says no more than its cost
    assert [(r.model, r.cost_usd, r.ref) for r in (a, b, c)] == [
        ("glm-5.3", 0.5, "day:2026-10-18:glm-5.3"), ("deepseek-v4-pro", 0.25, "day:2026-10-18:deepseek-v4-pro"),
        ("glm-5.3", 1.5, "day:2026-10-20:glm-5.3")]
    assert a.at == datetime(2026, 10, 18, 3, 0, tzinfo=KW) and c.at == datetime(2026, 10, 20, 3, 0, tzinfo=KW)
    assert [(r.url.path, r.url.params.get("summarize")) for r in proxy.requests] == [
        ("/spend/logs", "false"), ("/key/info", None)]  # (and /key/info once, to tie the summary to the key)


def test_daily_totals_stop_before_the_day_the_live_record_starts_in(store):
    live_at = datetime(2026, 10, 19, 10, 0, tzinfo=KW)  # 07:00 UTC on the 19th
    store.add_usage(UsageRow(at=live_at, provider="litellm", model="glm-5.3", source="live"))
    proxy = FakeProxy(logs=[summary("2026-10-17", {"glm-5.3": 1.0}), summary("2026-10-18", {"glm-5.3": 2.0}),
                            summary("2026-10-19", {"glm-5.3": 3.0})])
    res = litellm(store, proxy)  # the 18th is over before the live record began; the 19th holds it, and a day cannot be cut
    assert (res.added_rows, res.newer) == (2, 1)
    assert sorted(r.ref for r in rows(store) if r.source == "backfill") == ["day:2026-10-17:glm-5.3", "day:2026-10-18:glm-5.3"]


def test_daily_totals_again_add_nothing(store):
    proxy = FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 0.5})])
    assert litellm(store, proxy).added_rows == 1
    again = litellm(store, proxy)
    assert (again.added_rows, again.existing_rows) == (0, 1)


def requests_of_a_day(day="2026-10-18", model="glm-5.3", cost=1.0, n=3) -> list[dict]:
    """`n` requests on one UTC day that add up to n x cost."""
    t = datetime.fromisoformat(f"{day}T09:00:00+00:00")
    return [spend_log(f"{day}-r{i}", model, t + timedelta(hours=i), spend=cost) for i in range(n)]


def stored_cost(store) -> float:
    return sum(r.cost_usd or 0.0 for r in rows(store) if r.provider == "litellm")


def test_a_day_whose_requests_are_here_is_not_counted_again_from_the_summary(store):
    """$3.00 was spent in three requests: it stays $3.00 stored however the proxy answers on a later run."""
    per_request = FakeProxy(logs=requests_of_a_day())
    as_summary = FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 3.0})])
    assert litellm(store, per_request).added_rows == 3
    res = litellm(store, as_summary)
    assert (res.daily, res.added_rows, res.covered) == (True, 0, 1)
    assert stored_cost(store) == pytest.approx(3.0) and all(r.step != "daily_total" for r in rows(store))
    # the realistic way it happens: the per-request ask is too slow on a later run and the summary is taken instead
    slow = FakeProxy(by_summarize={"false": httpx.ReadTimeout("slow"), "true": [summary("2026-10-18", {"glm-5.3": 3.0})]})
    assert (litellm(store, slow).added_rows, litellm(store, slow).covered) == (0, 1)
    # and a rerun of each way of asking changes nothing
    assert litellm(store, per_request).added_rows == 0 and litellm(store, as_summary).added_rows == 0
    assert stored_cost(store) == pytest.approx(3.0) and store.usage_count() == 3


def test_the_requests_of_a_day_replace_the_summary_an_earlier_run_took(store):
    per_request = FakeProxy(logs=requests_of_a_day())
    as_summary = FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 3.0})])
    assert litellm(store, as_summary).added_rows == 1 and stored_cost(store) == pytest.approx(3.0)
    res = litellm(store, per_request)
    assert (res.added_rows, res.replaced) == (3, 1)
    assert stored_cost(store) == pytest.approx(3.0)  # $3.00 spent, $3.00 stored, not $6.00
    assert [r.step for r in rows(store)] == ["other"] * 3
    again = litellm(store, per_request)  # a rerun: nothing to add and nothing left to replace
    assert (again.added_rows, again.replaced, again.existing_rows) == (0, 0, 3)
    assert litellm(store, as_summary).covered == 1 and stored_cost(store) == pytest.approx(3.0)  # the summary stays out


def test_only_the_days_the_requests_cover_lose_their_summary(store):
    days = [summary(d, {"glm-5.3": 3.0}) for d in ("2026-10-17", "2026-10-18", "2026-10-19")]
    assert litellm(store, FakeProxy(logs=days)).added_rows == 3
    res = litellm(store, FakeProxy(logs=requests_of_a_day("2026-10-18")))
    assert (res.added_rows, res.replaced) == (3, 1)
    assert sorted(r.ref for r in rows(store) if r.step == "daily_total") == ["day:2026-10-17:glm-5.3", "day:2026-10-19:glm-5.3"]
    assert stored_cost(store) == pytest.approx(9.0)


def test_the_summary_of_one_model_does_not_touch_the_requests_of_another_day(store):
    assert litellm(store, FakeProxy(logs=requests_of_a_day("2026-10-18"))).added_rows == 3
    res = litellm(store, FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 3.0}), summary("2026-10-19", {"glm-5.3": 2.0})]))
    assert (res.added_rows, res.covered) == (1, 1)  # the 19th has no requests here: its summary is taken
    assert stored_cost(store) == pytest.approx(5.0)


def test_run_says_how_many_summaries_the_request_logs_replaced(store, folder):
    litellm(store, FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 3.0})]))
    code, out = run(store, folder, FakeProxy(logs=requests_of_a_day()))
    assert code == 0 and out[-1] == ("usage-backfill: litellm: 3 row(s) added, 0 already there, 0 newer than the live "
                                     "record, 1 daily total(s) replaced by the request logs")


def test_a_model_with_no_name_no_cost_or_nothing_spent_is_no_row(store):
    proxy = FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 0.0, "": 1.0, "a model": 2.0, "kimi-k3": {"spend": 0.75},
                                                   "bad": "x", "neg": -1.0}),
                            {"startTime": "garbage", "models": {"glm-5.3": 1.0}}])
    assert litellm(store, proxy).added_rows == 1
    assert [(r.model, r.cost_usd) for r in rows(store)] == [("kimi-k3", 0.75)]


def test_a_per_request_answer_too_big_to_read_is_asked_for_again_as_a_summary(store, monkeypatch):
    monkeypatch.setattr(ub, "MAX_PROXY_BYTES", 900)
    t = datetime(2026, 10, 19, 9, 30, tzinfo=KW)
    proxy = FakeProxy(by_summarize={"false": [spend_log(f"r{i}", "glm-5.3", t) for i in range(6)],
                                    "true": [summary("2026-10-18", {"glm-5.3": 0.5})]})
    res = litellm(store, proxy)
    assert (res.state, res.daily, res.added_rows) == ("imported", True, 1)
    assert [r.url.params["summarize"] for r in proxy.requests if r.url.path == "/spend/logs"] == ["false", "true"]


def test_a_per_request_answer_that_times_out_is_asked_for_again_as_a_summary(store):
    proxy = FakeProxy(by_summarize={"false": httpx.ReadTimeout("slow"), "true": [summary("2026-10-18", {"glm-5.3": 0.5})]})
    res = litellm(store, proxy)
    assert (res.state, res.daily, res.added_rows) == ("imported", True, 1)
    assert [r.url.params["summarize"] for r in proxy.requests if r.url.path == "/spend/logs"] == ["false", "true"]


def test_when_both_ways_of_asking_time_out_the_import_is_skipped(store):
    proxy = FakeProxy(by_summarize={"false": httpx.ReadTimeout("slow"), "true": httpx.ReadTimeout("slow")}, key_status=404)
    res = litellm(store, proxy)
    assert (res.state, res.reason) == ("unavailable", "/spend/logs timeout; /key/info 404")


@pytest.mark.parametrize("answer", [{"detail": "nothing here"}, "text", 7, [], [{"spend": 1.0}], [{"models": "not a map"}]])
def test_an_answer_that_is_neither_logs_nor_a_summary_is_skipped(store, answer):
    proxy = FakeProxy(logs=answer, key_status=404)
    res = litellm(store, proxy)
    if answer == []:  # an empty list is an answer with nothing in it: imported, zero rows
        assert (res.state, res.added_rows) == ("imported", 0)
    else:
        assert (res.state, res.reason) == ("unavailable", "/spend/logs no usable logs; /key/info 404")
    assert store.usage_count() == 0


def test_a_refusal_is_not_asked_again_but_key_info_is_looked_at(store):
    proxy = FakeProxy(spend_status=403, key_info={"info": {"spend": 3.2, "model_spend": {"glm-5.3": 3.2}}})
    res = litellm(store, proxy)
    assert [r.url.path for r in proxy.requests] == ["/spend/logs", "/key/info"]
    assert (res.state, res.reason) == ("unavailable", "/spend/logs 403; /key/info lifetime totals only")
    assert store.usage_count() == 0


def test_a_json_null_is_an_empty_answer_not_a_blank_reason(store):
    res = litellm(store, FakeProxy(logs=None, key_status=404))
    assert res.reason == "/spend/logs empty; /key/info 404"


def test_the_real_client_is_never_built_while_the_offline_guard_is_on(store, monkeypatch):
    real_looking = httpx.Client(transport=httpx.HTTPTransport())  # a client on a real transport; never used below

    def boom(*a, **k):
        raise AssertionError("a real httpx client was built")

    monkeypatch.setattr(ub.httpx, "Client", boom)
    monkeypatch.setattr(real_looking, "stream", boom)
    with pytest.raises(OfflineError):  # guard() runs before anything real
        ub.backfill_litellm(store, make_settings(), D0, NOW, None)
    with pytest.raises(OfflineError):  # ... and a client that is not on a mock transport is a real one
        ub.backfill_litellm(store, make_settings(), D0, NOW, real_looking)


# -- the whole command ---------------------------------------------------------------------------------------------------

def run(store, folder, proxy=None, **kw):
    out = []
    code = ub.run(store, make_settings(), NOW, folder=folder, http=proxy.client() if proxy else None, emit=out.append, **kw)
    return code, out


def test_run_prints_counts_only_and_leaves_the_notes_the_tab_reads(store, folder):
    start = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    write_session(folder, "a", start=start, models={"claude-haiku-5-5": usage_of(out=5, cost=0.1)})
    proxy = FakeProxy(logs=[spend_log("r1", "glm-5.3", datetime(2026, 10, 19, 12, 0, tzinfo=KW))])
    code, out = run(store, folder, proxy)
    assert code == 0 and out == [
        "usage-backfill: claude: 1 session(s) read, 1 row(s) added, 0 already there, 0 newer than the live record, "
        "0 without usage",
        "usage-backfill: litellm: 1 row(s) added, 0 already there, 0 newer than the live record"]
    assert json.loads(store.get_cursor("usage_backfill:litellm"))["state"] == "imported"
    assert json.loads(store.get_cursor("usage_backfill:claude"))["from"] == "2026-10-19"
    code, out = run(store, folder, proxy)  # safe to run again
    assert code == 0 and "0 row(s) added, 1 already there" in out[0] and "0 row(s) added, 1 already there" in out[1]


def test_run_says_how_many_sessions_are_still_being_written(store, folder):
    write_session(folder, "running", start=datetime(2026, 10, 20, 6, 0, tzinfo=UTC), state=False, fresh=True,
                  messages=[assistant("m1", "claude-haiku-5-5", out=8)])
    code, out = run(store, folder, skip_proxy=True)
    assert code == 0 and out[0].endswith("0 without usage, 1 still being written")


def test_run_says_when_the_proxy_gave_daily_totals(store, folder):
    proxy = FakeProxy(logs=[summary("2026-10-18", {"glm-5.3": 0.5}), summary("2026-10-21", {"glm-5.3": 1.0})])
    code, out = run(store, folder, proxy)
    assert code == 0 and out[-1] == ("usage-backfill: litellm: 1 daily total(s) added, 0 already there, "
                                     "1 not counted (a day not over, or one the live record or the request logs already cover)")
    assert json.loads(store.get_cursor("usage_backfill:litellm"))["state"] == "imported"


def test_run_asks_the_proxy_from_the_oldest_session_on(store, folder):
    write_session(folder, "a", start=datetime(2026, 10, 18, 22, 0, tzinfo=UTC), models={"m-x": usage_of(out=5)})
    proxy = FakeProxy(logs=[])
    run(store, folder, proxy)
    assert proxy.requests[0].url.params["start_date"] == "2026-10-19"  # 22:00Z is already the 19th in Kuwait


def test_run_with_no_transcripts_asks_for_the_last_30_days(store, folder):
    proxy = FakeProxy(logs=[])
    code, out = run(store, folder / "missing", proxy)
    assert code == 0 and out[0] == "usage-backfill: claude: no transcripts folder; nothing to import"
    assert proxy.requests[0].url.params["start_date"] == "2026-09-21"


def test_run_says_when_the_proxy_has_no_history_and_the_tab_is_told(store, folder):
    code, out = run(store, folder, FakeProxy(spend_status=404, key_status=404))
    assert code == 0
    assert out[-1] == "usage-backfill: litellm: not available (/spend/logs 404; /key/info 404); company-proxy history is not counted"
    cur = json.loads(store.get_cursor("usage_backfill:litellm"))
    assert (cur["state"], cur["reason"]) == ("unavailable", "/spend/logs 404; /key/info 404")


def test_skip_proxy_makes_no_proxy_call_and_leaves_the_proxy_note_alone(store, folder):
    proxy = FakeProxy(logs=[])
    code, out = run(store, folder, proxy, skip_proxy=True)
    assert code == 0 and out[-1] == "usage-backfill: litellm: skipped (--skip-proxy)" and proxy.requests == []
    assert store.get_cursor("usage_backfill:litellm") is None


# -- the command line ----------------------------------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working directory shaped like the repo root: config.toml, .env, data/."""
    wd = tmp_path / "wd"
    wd.mkdir()
    shutil.copy(ROOT / "tests" / "fixtures" / "config.toml", wd / "config.toml")
    (wd / ".env").write_text("LITELLM_API_KEY=k\n")
    monkeypatch.chdir(wd)
    return wd


def test_the_command_imports_and_prints_counts_only(repo, folder, capsys):
    write_session(folder, "a", start=datetime(2026, 10, 20, 6, 0, tzinfo=UTC),
                  models={"claude-haiku-5-5": usage_of(out=5, cost=0.1)})
    rc = main(["usage-backfill", "--transcripts", str(folder), "--skip-proxy"])
    out = capsys.readouterr()
    assert rc == 0 and out.err == ""
    assert out.out.splitlines() == [
        "usage-backfill: claude: 1 session(s) read, 1 row(s) added, 0 already there, 0 newer than the live record, "
        "0 without usage",
        "usage-backfill: litellm: skipped (--skip-proxy)"]
    assert SECRET not in out.out
    s = Store(repo / "data" / "agent.sqlite")
    try:
        assert s.usage_count("backfill") == 1
    finally:
        s.close()


def test_the_command_cannot_reach_the_real_proxy_while_the_offline_guard_is_on(repo, folder, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real httpx client was built")

    monkeypatch.setattr(ub.httpx, "Client", boom)
    rc = main(["usage-backfill", "--transcripts", str(folder)])
    assert rc == 1 and "usage-backfill failed: OfflineError" in capsys.readouterr().err


def test_the_command_runs_while_the_agent_is_paused(repo, folder, capsys):
    """It reads files and asks for logs: no job, no Slack write, nothing for the pause to stop."""
    from haseeb_agent import controls
    s = Store(repo / "data" / "agent.sqlite")
    controls.set_paused(s, True, datetime.fromisoformat("2026-10-20T10:00:00+03:00"))
    s.close()
    assert main(["usage-backfill", "--transcripts", str(folder), "--skip-proxy"]) == 0
    assert "usage-backfill: claude:" in capsys.readouterr().out
