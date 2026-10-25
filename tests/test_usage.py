"""Model usage recording (usage.py): one text-free row per model call, written best effort at the lowest shared call paths
(`run_claude`, `LLM._post` and the embeddings call, the two Codex runners), attributed by a context variable.

No real call is made: fake runners, `httpx.MockTransport`, fixture streams, tmp databases. Offline guard on."""
import json
import logging
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import pytest

from haseeb_agent import events, usage
from haseeb_agent.claude_cli import run_claude
from haseeb_agent.deck import designer
from haseeb_agent.deck.codex import codex_cover
from haseeb_agent.litellm import LLM, LLMError
from haseeb_agent.offline import OfflineError
from haseeb_agent.store import Store
from tests.test_litellm import make_settings

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_STREAM = (FIXTURES / "stream_read_profile.jsonl").read_text()
CODEX_STREAM = (FIXTURES / "codex_image_stream.jsonl").read_text()
PROFILE_TOOL = "mcp__claude_ai_Slack__slack_read_user_profile"
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_lock_state():
    """The pause after a locked database is module state: no test may start in one, or leave one behind."""
    usage._lock.update(until=0.0, dropped=0)
    yield
    usage._lock.update(until=0.0, dropped=0)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "agent.sqlite"


@pytest.fixture
def store(db):
    s = Store(db)
    yield s
    s.close()


def rows(store):
    return store.usage_since(EPOCH)


# -- who a call is for ---------------------------------------------------------------------------------------------------

def test_outside_any_job_nothing_is_recorded_and_nothing_fails():
    usage.record("claude", "claude-haiku-5-5", input_tokens=5)  # no session, no store: a no-op, not an error


def test_a_session_records_into_its_database_for_its_job(db, store):
    with usage.session(db, "history_import"):
        usage.record("claude", "claude-haiku-5-5", input_tokens=5, output_tokens=2, cache_read_tokens=3,
                     cache_write_tokens=4, cost_usd=0.01, cost_basis="list", latency_ms=1500)
    [r] = rows(store)
    assert (r.provider, r.model, r.job, r.step, r.source, r.ref, r.ok) == (
        "claude", "claude-haiku-5-5", "history", "other", "live", None, True)
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (5, 2, 3, 4)
    assert (r.cost_usd, r.cost_basis, r.latency_ms) == (0.01, "list", 1500)
    # `at` is when the call began: now less its wall time, with the zone's offset
    assert timedelta(seconds=1.5) <= datetime.now().astimezone() - r.at < timedelta(seconds=30)
    usage.record("claude", "m")  # the session is over: this one has nowhere to go
    assert len(rows(store)) == 1


@pytest.mark.parametrize("name,job", [
    ("insights", "insights"), ("feedback", "feedback"), ("todo", "todo"), ("decks", "decks"), ("weekly", "weekly"),
    ("compose", "compose"), ("site_review", "site_review"), ("deck_test", "deck_test"), ("slack", "slack"),
    ("history_import", "history"), ("history_refresh", "history"), ("enrich", "history"), ("history", "history"),
    ("process_dm", "slack"), ("process_page", "slack"), ("slack_check", "slack"), ("smoke", "slack"),
    ("deliver_pending", "insights"), ("run_todo", "todo"), ("run_feedback", "feedback"),
    ("Site-Review", "site_review"),                 # case and dashes do not matter
    ("followback", "followback"),                   # a job this module never heard of keeps its own name
    ("tick", None), ("", None), ("  ", None), ("a b", None), ("x" * 121, None), (None, None), (7, None),
])
def test_job_names(name, job):
    assert usage.canonical_job(name) == job


def test_the_job_vocabulary_holds_the_designed_names():
    assert set(usage.JOBS) == {"insights", "feedback", "todo", "decks", "slack", "site_review", "compose", "history",
                               "weekly", "deck_test", "other", "unknown"}  # (`unknown` is the backfill's, review M3)


def test_a_row_with_no_job_anywhere_is_other(db, store):
    with usage.session(db, "tick"):  # the tick is a round of other jobs: it names none itself
        usage.record("claude", "m")
        usage.record("claude", "m", default_job="slack")
    assert [r.job for r in rows(store)] == ["other", "slack"]


def test_events_step_says_who_the_calls_inside_it_are_for(store):
    with events.step(store, "todo", "write_todo"):
        usage.record("litellm", "deepseek-v4.1-flash")
        with events.step(store, "feedback", "deliver"):
            usage.record("litellm", "m2")
        usage.record("litellm", "m3")  # back in the outer step
    usage.record("litellm", "m4")      # outside every step: nowhere to go
    assert [(r.model, r.job, r.step) for r in rows(store)] == [
        ("deepseek-v4.1-flash", "todo", "write_todo"), ("m2", "feedback", "deliver"), ("m3", "todo", "write_todo")]


def test_events_step_names_map_to_the_designed_jobs(store):
    for job, step in (("process_dm", "command"), ("history_import", "import"), ("enrich", "enrich"),
                      ("slack_check", "check")):
        with events.step(store, job, step):
            usage.record("claude", "m")
    assert [(r.job, r.step) for r in rows(store)] == [
        ("slack", "command"), ("history", "import"), ("history", "enrich"), ("slack", "check")]


def test_a_step_ends_its_scope_when_the_job_raises(store):
    with pytest.raises(ValueError):
        with events.step(store, "todo", "write_todo"):
            raise ValueError("boom")
    usage.record("claude", "m")
    assert rows(store) == []


def test_a_step_inside_a_session_uses_the_steps_job_and_the_session_afterwards(db, store):
    with usage.session(db, "insights"):
        usage.record("claude", "a")
        with events.step(store, "feedback", "deliver"):
            usage.record("claude", "b")
        usage.record("claude", "c")
    assert [(r.model, r.job, r.step) for r in rows(store)] == [
        ("a", "insights", "other"), ("b", "feedback", "deliver"), ("c", "insights", "other")]


def test_scoped_keeps_the_job_for_a_step_and_starts_a_new_step_for_a_job(db, store):
    with usage.session(db, "insights"):
        with usage.scoped(step="judge"):
            usage.record("litellm", "a")
            with usage.scoped("feedback"):  # a new job: the outer step does not follow it
                usage.record("litellm", "b")
            usage.record("litellm", "c")
    assert [(r.model, r.job, r.step) for r in rows(store)] == [
        ("a", "insights", "judge"), ("b", "feedback", "other"), ("c", "insights", "judge")]


def test_the_site_review_step_is_the_site_review_job_wherever_it_runs(db, store):
    """The site review runs inside the feedback job's step (`events.step(store, "feedback", "site_review")`), and its model
    step is `site_review` too: its calls are the spec's `site_review` job, not the feedback's."""
    ok = lambda req: reply("x", {"prompt_tokens": 3, "completion_tokens": 1})  # noqa: E731
    with events.step(store, "feedback", "site_review"):
        llm(ok, site_review=["litellm:gemini/gemini-3.1-pro-preview"]).complete("site_review", "s", "u")
        llm(ok).complete("feedback_writer", "s", "u")  # another step of the same job stays the feedback's
    assert [(r.job, r.step) for r in rows(store)] == [("site_review", "site_review"), ("feedback", "feedback_writer")]


def test_a_step_and_a_job_given_to_record_win_over_the_context(db, store):
    with usage.session(db, "insights"), usage.scoped(step="write"):
        usage.record("claude", "a", step="slack_read_channel", job="todo")
    assert [(r.job, r.step) for r in rows(store)] == [("todo", "slack_read_channel")]


# -- best effort ---------------------------------------------------------------------------------------------------------

def test_a_database_error_is_logged_by_type_only_and_swallowed(caplog):
    class Failing:
        def add_usage(self, row, busy_ms=None):
            raise sqlite3.OperationalError("disk I/O error: Haseeb's secret draft text")

    caplog.set_level(logging.WARNING)
    with usage.scoped("todo", "write", store=Failing()):
        usage.record("claude", "m")  # does not raise
    assert "OperationalError" in caplog.text and "secret" not in caplog.text


# -- a locked database (review M7) ---------------------------------------------------------------------------------------

def hold_the_write_lock(db):
    """Another connection that has the database to itself, as a stuck writer (an open sqlite3 shell) would."""
    other = sqlite3.connect(db, isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    return other


def test_a_locked_database_costs_a_call_a_fraction_of_a_second_once_and_the_row_is_dropped(db, store, caplog):
    other = hold_the_write_lock(db)
    caplog.set_level(logging.WARNING)
    try:
        began = time.monotonic()
        with usage.session(db, "todo"):          # a connection of its own, as a job with no step has
            usage.record("claude", "m", step="a_secret_step")
        assert time.monotonic() - began < 1.5   # the connection's own wait is 5 s: this one asks for 200 ms
        assert usage._lock["dropped"] == 1 and usage._lock["until"] > time.monotonic()
        # while the pause lasts a row is dropped at once, not after another wait
        began = time.monotonic()
        with usage.session(db, "todo"):
            for _ in range(5):
                usage.record("claude", "m")
        assert time.monotonic() - began < 0.5 and usage._lock["dropped"] == 6
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert rows(store) == []
    assert caplog.text.count("the database is locked, so a row was dropped") == 1  # said once, not once per row
    assert "a_secret_step" not in caplog.text


def test_a_locked_database_does_not_stall_a_step_that_holds_its_own_connection_either(db, store):
    """Under `events.step` the row goes in on the step's connection, whose own wait is 5 s: the insert asks for 200 ms
    of it, and the connection's wait is back afterwards."""
    other = hold_the_write_lock(db)
    try:
        with usage.scoped("todo", "write_todo", store=store):
            began = time.monotonic()
            usage.record("claude", "m")
        assert time.monotonic() - began < 1.5
        assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert rows(store) == []


def test_recording_resumes_after_the_pause_and_says_how_many_rows_the_lock_cost(db, store, caplog):
    caplog.set_level(logging.WARNING)
    other = hold_the_write_lock(db)
    try:
        with usage.session(db, "todo"):
            usage.record("claude", "first")
            usage.record("claude", "second")
    finally:
        other.execute("ROLLBACK")
        other.close()
    usage._lock["until"] = 0.0  # the pause is over
    with usage.session(db, "todo"):
        usage.record("claude", "third")
    assert [r.model for r in rows(store)] == ["third"]
    assert usage._lock["dropped"] == 0 and "2 row(s) were dropped while the database was locked" in caplog.text


def test_a_lock_that_is_not_there_changes_nothing(db, store):
    with usage.session(db, "todo"):
        usage.record("claude", "a")
        usage.record("claude", "b")
    assert [r.model for r in rows(store)] == ["a", "b"] and usage._lock == {"until": 0.0, "dropped": 0}


def test_a_database_that_cannot_be_opened_is_swallowed_too(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    with usage.session(tmp_path, "todo"):  # a directory is no database
        usage.record("claude", "m")
    assert "cannot record" in caplog.text


def test_a_step_whose_store_fails_still_runs_its_job(caplog):
    class Broken:
        def add_event(self, *a, **k):
            raise sqlite3.OperationalError("locked")

        def add_usage(self, row, *, busy_ms=None):
            raise sqlite3.OperationalError("locked")

    ran = []
    with events.step(Broken(), "j", "s"):
        usage.record("claude", "m")
        ran.append(1)
    assert ran == [1]


def test_what_goes_into_a_row_is_cleaned(db, store):
    with usage.session(db, "todo"):
        usage.record("litellm", "a model with spaces\nand a newline", step="bad step\n", input_tokens=-5,
                     output_tokens=float("nan"), cache_read_tokens=True, cache_write_tokens="12",
                     cost_usd=float("inf"), cost_basis="proxy", latency_ms=-1, ref="x y")
        usage.record("litellm", None, cost_usd=-0.5, cost_basis="list")
        usage.record("litellm", "gemini/gemini-3.1-pro-preview", cost_usd=0.0, cost_basis="proxy")
        usage.record("claude", "opus[1m]")
    a, b, c, d = rows(store)
    assert (a.model, a.step, a.input_tokens, a.output_tokens, a.cache_read_tokens, a.cache_write_tokens) == (
        "unknown", "other", None, None, None, 12)
    assert (a.cost_usd, a.cost_basis, a.latency_ms, a.ref) == (None, "unknown", None, None)  # a basis needs a cost
    assert a.at <= datetime.now().astimezone()  # a negative wall time is no wall time: it cannot move `at` ahead
    assert (b.model, b.cost_usd, b.cost_basis) == ("unknown", None, "unknown")
    assert (c.model, c.cost_usd, c.cost_basis) == ("gemini/gemini-3.1-pro-preview", 0.0, "proxy")  # free is not unknown
    assert d.model == "opus-1m"  # the identifier rule has no brackets: Claude's "[1m]" context-size suffix becomes a hyphen


def test_whatever_the_parsing_does_a_job_is_never_told(db, caplog, monkeypatch):
    """The reading of a result, a response or a stream happens before the INSERT: it is part of "best effort" too."""
    def boom(*a, **k):
        raise ValueError("unexpected shape: Haseeb's secret draft text")

    caplog.set_level(logging.WARNING)
    for name in ("claude_figures", "litellm_figures", "codex_figures"):
        monkeypatch.setattr(usage, name, boom)
    with usage.session(db, "todo"):
        usage.record_claude({"usage": {}}, "haiku", init_model=None, ok=True, latency_ms=1)
        usage.record_litellm("m", httpx.Response(200, json={}), latency_ms=1)
        usage.record_codex(["codex", "exec"], CODEX_STREAM, returncode=0, latency_ms=1, step="cover")
    assert caplog.text.count("cannot record a call (ValueError in record_") == 3 and "secret" not in caplog.text
    # and a real run, response and cover are untouched by it
    assert LLM(make_settings(), http=httpx.Client(transport=httpx.MockTransport(lambda r: reply("x", {})))).complete(
        "feedback_writer", "s", "u").text == "x"


@pytest.mark.parametrize("x,count", [
    (5, 5), (0, 0), ("12", 12), (" 7 ", 7), (3.9, 3), (-1, None), (True, None), (None, None), ("", None), ("-5", None),
    ("1.5", None), ("²", None), ("١٢٣", None), ("12abc", None),            # str.isdigit() accepts "²"; int() does not
    (float("nan"), None), (float("inf"), None), (1e300, usage.USAGE_MAX_COUNT), (2**70, usage.USAGE_MAX_COUNT),
    ("9" * 5000, None),                                                      # past Python's own limit on int(str)
    (2**62, 2**62), ([1], None), ({"n": 1}, None),
])
def test_a_count_is_clean_clamped_or_none_and_never_raises(x, count):
    assert usage.count_of(x) == count


@pytest.mark.parametrize("x,cost", [(0, 0.0), (1.5, 1.5), ("2", 2.0), (-0.1, None), (True, None), (None, None),
                                    ("x", None), (float("nan"), None), (float("inf"), None), (10**400, None), (2e9, None)])
def test_a_cost_is_clean_or_none_and_never_raises(x, cost):
    assert usage.money_of(x) == cost


@pytest.mark.parametrize("x,model", [
    ("claude-haiku-5-5", "claude-haiku-5-5"), ("gemini/gemini-3.1-pro-preview", "gemini/gemini-3.1-pro-preview"),
    ("opus[1m]", "opus-1m"), ("a+b", "a-b"), ("", "unknown"), ("two words", "unknown"), ("x" * 121, "unknown"),
    ("<img src=x>", "unknown"), ("naïve", "unknown"), (None, "unknown"), (5, "unknown"),
])
def test_a_model_id_fits_the_identifier_rule_or_is_unknown(x, model):
    assert usage.model_of(x) == model


def test_what_the_writers_clean_to_is_always_what_the_store_accepts(db, store):
    """The writers and the table share one rule (models.USAGE_IDENT): nothing a cleaning function lets through is rejected
    on insert, so no row is lost to a mismatch between the two."""
    odd = ["claude-haiku-5-5", "opus[1m]", "gemini/gemini-3.1-pro-preview", "a b", "x" * 200, "ü", "day:2026-10-18:m",
           "a.jsonl", "q@r", "", "-", "[]", "+"]
    with usage.session(db, "todo"):
        for m in odd:
            usage.record("claude", m, step=m, job=m, ref=m)
    assert len(rows(store)) == len(odd)


# -- Claude --------------------------------------------------------------------------------------------------------------

class FakeRunner:
    def __init__(self, stdout="", returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc

    def __call__(self, cmd, **kwargs):
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def claude(tmp_path):
    """run_claude with a fake runner and its home in tmp_path: nothing real is touched."""
    return partial(run_claude, "SECRET-PROMPT-TEXT", cwd=tmp_path / "home", user_settings=tmp_path / "settings.json")


def stream(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def result_event(**kw):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0, **kw}


def test_a_gateway_run_is_recorded_with_the_real_stream_counts(claude, db, store):
    with usage.session(db, None):
        res = claude(model="haiku", allowed_tools=[PROFILE_TOOL], runner=FakeRunner(CLAUDE_STREAM))
    assert res.ok
    [r] = rows(store)
    assert (r.provider, r.model, r.job, r.step, r.source) == ("claude", "claude-haiku-5-5", "slack",
                                                              "slack_read_user_profile", "live")
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (6, 389, 97451, 34280)
    assert (r.cost_usd, r.cost_basis, r.ok) == (pytest.approx(0.00802561), "list", True)
    assert r.ref == "ece52a2e-6925-4fd6-9fb8-e5287589abe8.jsonl"  # the transcript's file name: the backfill's ref too
    assert r.latency_ms is not None and r.latency_ms >= 0


def test_a_gateway_run_inside_a_job_keeps_that_job_and_names_the_tool_as_its_step(claude, store):
    with events.step(store, "todo", "find_huddle"):
        claude(model="haiku", allowed_tools=[PROFILE_TOOL], runner=FakeRunner(CLAUDE_STREAM))
    [r] = rows(store)
    assert (r.job, r.step) == ("todo", "slack_read_user_profile")


def test_a_research_run_is_attributed_by_its_step(claude, store):
    with events.step(store, "insights", "research", "sonnet"):
        claude(model="sonnet", allowed_tools=["WebSearch", "WebFetch"], runner=FakeRunner(CLAUDE_STREAM))
    [r] = rows(store)
    assert (r.job, r.step) == ("insights", "research")


def test_a_run_with_two_models_leaves_a_row_each_and_the_wall_time_on_the_main_one(claude, store):
    ev = result_event(total_cost_usd=0.5, session_id="d5a75199-eef1-4d55-824b-a0b8f56a4f3f",
                      usage={"input_tokens": 10, "output_tokens": 70, "cache_read_input_tokens": 5,
                             "cache_creation_input_tokens": 1},
                      modelUsage={"claude-haiku-5-5": {"inputTokens": 4, "outputTokens": 20, "cacheReadInputTokens": 2,
                                                       "cacheCreationInputTokens": 0, "costUSD": 0.01},
                                  "claude-sonnet-5-5": {"inputTokens": 6, "outputTokens": 50, "cacheReadInputTokens": 3,
                                                        "cacheCreationInputTokens": 1, "costUSD": 0.49}})
    with events.step(store, "insights", "research"):
        claude(model="sonnet", allowed_tools=["WebSearch"], runner=FakeRunner(stream(ev)))
    main, side = rows(store)
    assert (main.model, main.cost_usd, main.output_tokens) == ("claude-sonnet-5-5", 0.49, 50)
    assert (side.model, side.cost_usd, side.output_tokens) == ("claude-haiku-5-5", 0.01, 20)
    assert main.latency_ms is not None and side.latency_ms is None
    assert main.ref == side.ref == "d5a75199-eef1-4d55-824b-a0b8f56a4f3f.jsonl"


def test_a_result_without_model_usage_is_counted_from_usage_and_the_total_cost(claude, store):
    init = {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "permissionMode": "dontAsk", "tools": []}
    ev = result_event(total_cost_usd=0.25, usage={"input_tokens": 7, "output_tokens": 9, "cache_read_input_tokens": 100,
                                                  "cache_creation_input_tokens": 50})
    with events.step(store, "feedback", "write_draft"):
        claude(model="sonnet", allowed_tools=[], runner=FakeRunner(stream(init, ev)))
        claude(model="haiku", allowed_tools=[], runner=FakeRunner(stream(ev)))  # no init event: the alias it was asked for
    a, b = rows(store)
    assert (a.model, a.input_tokens, a.output_tokens, a.cache_read_tokens, a.cache_write_tokens, a.cost_usd,
            a.cost_basis) == ("claude-sonnet-5-5", 7, 9, 100, 50, 0.25, "list")
    assert b.model == "haiku"


def test_a_run_that_timed_out_is_a_failed_row_with_no_counts_and_no_cost(claude, store):
    with events.step(store, "insights", "research"):
        res = claude(model="sonnet", allowed_tools=["WebSearch"], timeout_s=5,
                     runner=FakeRunner(exc=subprocess.TimeoutExpired("claude", 5)))
    assert not res.ok
    [r] = rows(store)
    assert (r.model, r.ok, r.input_tokens, r.output_tokens, r.cost_usd, r.cost_basis) == (
        "sonnet", False, None, None, None, "unknown")
    assert r.ref is None


def test_a_failed_run_keeps_the_counts_it_has(claude, store):
    ev = result_event(is_error=True, subtype="error_during_execution", total_cost_usd=0.02,
                      usage={"input_tokens": 3, "output_tokens": 4})
    with events.step(store, "feedback", "write_draft"):
        res = claude(model="haiku", allowed_tools=[], runner=FakeRunner(stream(ev)))
        res2 = claude(model="haiku", allowed_tools=[], runner=FakeRunner(stream(result_event()), returncode=1))
    assert not res.ok and not res2.ok
    a, b = rows(store)
    assert (a.ok, a.input_tokens, a.output_tokens, a.cost_usd) == (False, 3, 4, 0.02)
    assert (b.ok, b.cost_usd) == (False, 0.0)


def test_a_run_that_never_started_is_no_call(claude, store):
    with events.step(store, "feedback", "write_draft"):
        res = claude(model="haiku", allowed_tools=[], runner=FakeRunner(exc=FileNotFoundError("claude")))
    assert not res.ok and rows(store) == []


def test_recording_changes_nothing_about_the_result(claude, db, store):
    plain = claude(model="haiku", allowed_tools=[PROFILE_TOOL], runner=FakeRunner(CLAUDE_STREAM))
    with usage.session(db, "todo"):
        recorded = claude(model="haiku", allowed_tools=[PROFILE_TOOL], runner=FakeRunner(CLAUDE_STREAM))
    assert recorded == plain and len(rows(store)) == 1


def test_a_run_survives_a_database_that_fails(claude, tmp_path):
    with usage.session(tmp_path, "todo"):  # not a database
        res = claude(model="haiku", allowed_tools=[PROFILE_TOOL], runner=FakeRunner(CLAUDE_STREAM))
    assert res.ok and res.cost_usd == pytest.approx(0.00802561)


def test_no_prompt_and_no_answer_text_reaches_the_database(claude, db):
    ev = result_event(result="SECRET-ANSWER-TEXT", usage={"input_tokens": 1})
    msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": "SECRET-ASSISTANT-TEXT"}]}}
    with usage.session(db, "todo"):
        claude(model="haiku", allowed_tools=[], runner=FakeRunner(stream(msg, ev)))
        claude(model="haiku", allowed_tools=[PROFILE_TOOL], runner=FakeRunner(CLAUDE_STREAM))
    raw = b"".join(p.read_bytes() for p in db.parent.glob("agent.sqlite*"))
    for secret in (b"SECRET-PROMPT-TEXT", b"SECRET-ANSWER-TEXT", b"SECRET-ASSISTANT-TEXT", b"Haseeb Zafar"):
        assert secret not in raw


# -- LiteLLM -------------------------------------------------------------------------------------------------------------

def reply(content="ok", usage_block=None, headers=None, status=200):
    body = {"choices": [{"message": {"content": content}}]}
    if usage_block is not None:
        body["usage"] = usage_block
    return httpx.Response(status, json=body, headers=headers or {})


def llm(handler, **models) -> LLM:
    return LLM(make_settings(**models), http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_proxy_call_is_recorded_with_its_counts_cost_and_step(store):
    def handler(req):
        return reply("hello SECRET-ANSWER", {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
                     {"x-litellm-response-cost": "0.00123", "x-litellm-call-id": "req-abc-1"})

    with events.step(store, "feedback", "write_draft"):
        out = llm(handler).complete("feedback_writer", "SYS", "USR")
    assert out.text.startswith("hello")
    [r] = rows(store)
    assert (r.provider, r.model, r.job, r.step, r.source, r.ok) == ("litellm", "deepseek-v4-pro", "feedback",
                                                                    "feedback_writer", "live", True)
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (120, 30, None, None)
    assert (r.cost_usd, r.cost_basis, r.ref) == (0.00123, "proxy", "req-abc-1")
    assert r.latency_ms is not None


def test_the_model_step_is_found_whether_it_is_passed_by_position_or_by_name(db, store):
    ok = lambda req: reply("x", {"prompt_tokens": 1, "completion_tokens": 1})  # noqa: E731
    with usage.session(db, "insights"):
        llm(ok, judge=["litellm:gemini/gemini-3.1-pro-preview"]).complete("judge", "s", "u")
        llm(ok).complete(step="feedback_writer", system="s", user="u")
    assert [r.step for r in rows(store)] == ["judge", "feedback_writer"]


def test_the_model_step_names_the_row_even_without_a_job_step(db, store):
    with usage.session(db, "insights"):
        llm(lambda req: reply("x", {"prompt_tokens": 1, "completion_tokens": 1})).complete("feedback_writer", "s", "u")
    [r] = rows(store)
    assert (r.job, r.step) == ("insights", "feedback_writer")


def test_cached_prompt_tokens_come_out_of_the_fresh_input(store):
    u = {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}
    with events.step(store, "feedback", "write_draft"):
        llm(lambda req: reply("x", u)).complete("feedback_writer", "s", "u")
    [r] = rows(store)
    assert (r.input_tokens, r.cache_read_tokens, r.cache_write_tokens, r.output_tokens) == (60, 40, None, 10)


def test_anthropic_style_cache_counts_through_the_proxy_are_taken_out_too(store):
    u = {"prompt_tokens": 100, "completion_tokens": 10, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 20}
    with events.step(store, "feedback", "write_draft"):
        llm(lambda req: reply("x", u)).complete("feedback_writer", "s", "u")
    [r] = rows(store)
    assert (r.input_tokens, r.cache_read_tokens, r.cache_write_tokens) == (50, 30, 20)


def test_cached_counts_larger_than_the_prompt_are_clamped(store):
    u = {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 99}}
    with events.step(store, "feedback", "write_draft"):
        llm(lambda req: reply("x", u)).complete("feedback_writer", "s", "u")
    [r] = rows(store)
    assert (r.input_tokens, r.cache_read_tokens) == (0, 10)


@pytest.mark.parametrize("header,cost,basis", [
    (None, None, "unknown"), ("0.0", 0.0, "proxy"), ("0.5", 0.5, "proxy"), ("1e-05", 1e-05, "proxy"),
    ("", None, "unknown"), ("abc", None, "unknown"), ("-1", None, "unknown"), ("nan", None, "unknown"),
    ("inf", None, "unknown"),
])
def test_the_cost_is_the_proxys_own_header_or_unknown_never_invented(store, header, cost, basis):
    headers = {"x-litellm-response-cost": header} if header is not None else {}
    with events.step(store, "feedback", "write_draft"):
        llm(lambda req: reply("x", {"prompt_tokens": 5, "completion_tokens": 5}, headers)).complete(
            "feedback_writer", "s", "u")
    [r] = rows(store)
    assert (r.cost_usd, r.cost_basis) == (cost, basis)


def test_an_answer_with_no_usage_block_is_a_row_with_no_counts(store):
    with events.step(store, "feedback", "write_draft"):
        llm(lambda req: reply("x")).complete("feedback_writer", "s", "u")
    [r] = rows(store)
    assert (r.ok, r.input_tokens, r.output_tokens, r.cost_usd, r.cost_basis) == (True, None, None, None, "unknown")


def test_every_attempt_of_a_fallback_chain_is_a_row(store):
    def handler(req):
        if json.loads(req.content)["model"] == "deepseek-v4-pro":
            return httpx.Response(500, text="boom")
        return reply("ok", {"prompt_tokens": 7, "completion_tokens": 3})

    with events.step(store, "feedback", "write_draft"):
        llm(handler).complete("feedback_writer", "s", "u")
    bad, good = rows(store)
    assert (bad.model, bad.ok, bad.input_tokens) == ("deepseek-v4-pro", False, None)
    assert (good.model, good.ok, good.input_tokens) == ("glm-5.3", True, 7)


def test_a_timeout_and_a_connection_error_are_failed_rows(store):
    def handler(req):
        if json.loads(req.content)["model"] == "deepseek-v4-pro":
            raise httpx.ReadTimeout("slow", request=req)
        raise httpx.ConnectError("no route", request=req)

    with events.step(store, "feedback", "write_draft"):
        with pytest.raises(LLMError):
            llm(handler).complete("feedback_writer", "s", "u")
    a, b = rows(store)
    assert [(r.model, r.ok, r.input_tokens, r.cost_usd) for r in (a, b)] == [
        ("deepseek-v4-pro", False, None, None), ("glm-5.3", False, None, None)]


def test_the_json_mode_retry_is_a_second_row(store):
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if "response_format" in calls[-1]:
            return httpx.Response(400, text="no response_format")
        return reply("{}", {"prompt_tokens": 9, "completion_tokens": 1})

    with events.step(store, "insights", "write"):
        llm(handler).complete("feedback_writer", "s", "u", json_mode=True)
    first, second = rows(store)
    assert (first.ok, second.ok, second.input_tokens) == (False, True, 9)


class FakePoster:
    """Something with a `_post(self, path, payload, ...)` like LLM's, as a variant of the call (an image call, say) has."""
    def __init__(self, outcome):
        self.outcome = outcome

    @usage.recorded_post
    def _post(self, path, payload, timeout_s=None, extra=None):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_whatever_goes_through_post_is_recorded_whatever_else_it_takes(store):
    answer = httpx.Response(200, json={"usage": {"prompt_tokens": 7, "completion_tokens": 2}},
                            headers={"x-litellm-response-cost": "0.5"})
    with events.step(store, "site_review", "look"):
        assert FakePoster(answer)._post("/v1/chat/completions", {"model": "gemini/gemini-3.1-pro-preview"}, 30.0,
                                        extra=1) is answer
    [r] = rows(store)
    assert (r.provider, r.model, r.job, r.step, r.input_tokens, r.output_tokens, r.cost_usd, r.ok) == (
        "litellm", "gemini/gemini-3.1-pro-preview", "site_review", "look", 7, 2, 0.5, True)


def test_a_request_that_raises_is_a_failed_row_and_the_exception_goes_on(store):
    boom = RuntimeError("no route")
    with events.step(store, "feedback", "write_draft"):
        with pytest.raises(RuntimeError) as caught:
            FakePoster(boom)._post("/v1/chat/completions", {"model": "glm-5.3"})
    assert caught.value is boom
    [r] = rows(store)
    assert (r.model, r.ok, r.input_tokens, r.cost_usd) == ("glm-5.3", False, None, None)


def test_the_guard_saying_no_request_was_made_leaves_no_row(db, store):
    real = LLM(make_settings(), http=httpx.Client())  # not a mock transport: a real client, which the guard refuses
    with usage.session(db, "feedback"):
        with pytest.raises(OfflineError):
            real._post("/v1/chat/completions", {"model": "glm-5.3"})
    assert rows(store) == []


def test_a_payload_that_is_not_a_dict_still_records_a_row_with_an_unknown_model(store):
    with events.step(store, "feedback", "write_draft"):
        FakePoster(httpx.Response(200, json={}))._post("/x", ["not", "a", "dict"])
    assert rows(store)[0].model == "unknown"


def test_embeddings_record_the_prompt_tokens_under_the_embed_step(store):
    def handler(req):
        assert req.url.path == "/v1/embeddings"
        data = [{"index": i, "embedding": [0.1, 0.2]} for i in range(2)]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 17, "total_tokens": 17}},
                              headers={"x-litellm-response-cost": "0.000002"})

    with events.step(store, "insights", "dedupe"):
        assert len(llm(handler).embed(["a", "b"])) == 2
    [r] = rows(store)
    assert (r.provider, r.model, r.job, r.step) == ("litellm", "qwen3-embedding", "insights", "embed")
    assert (r.input_tokens, r.output_tokens, r.cost_usd, r.cost_basis) == (17, None, 0.000002, "proxy")


def test_embeddings_with_no_job_step_are_still_the_embed_step(db, store):
    data = [{"index": 0, "embedding": [0.1]}]
    with usage.session(db, "insights"):
        llm(lambda req: httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 3}})).embed(["a"])
    [r] = rows(store)
    assert (r.job, r.step, r.input_tokens) == ("insights", "embed", 3)


def test_a_claude_model_in_a_chain_is_recorded_once_by_run_claude_under_the_chains_step(tmp_path, store):
    runner = FakeRunner(stream(result_event(total_cost_usd=0.01, usage={"input_tokens": 2, "output_tokens": 3})))
    real = partial(run_claude, cwd=tmp_path / "home", user_settings=tmp_path / "settings.json", runner=runner)
    model = LLM(make_settings(), http=httpx.Client(transport=httpx.MockTransport(lambda r: reply("x"))), claude=real)
    with events.step(store, "insights", "write"):
        assert model.complete("mixed", "s", "u").model == "haiku"
    [r] = rows(store)  # the chain's first model answered: one claude row, no litellm row
    assert (r.provider, r.job, r.step, r.input_tokens, r.output_tokens) == ("claude", "insights", "mixed", 2, 3)


def test_a_fake_claude_runner_records_nothing(store):
    from haseeb_agent.claude_cli import ClaudeResult
    fake = lambda prompt, *, model, allowed_tools, **kw: ClaudeResult("x", [], 0.0, True, None)  # noqa: E731
    model = LLM(make_settings(), http=httpx.Client(transport=httpx.MockTransport(lambda r: reply("x"))), claude=fake)
    with events.step(store, "insights", "write"):
        model.complete("mixed", "s", "u")
    assert rows(store) == []


# -- Codex ---------------------------------------------------------------------------------------------------------------

def test_codex_counts_come_from_the_last_turn_completed_event():
    assert usage.codex_figures(CODEX_STREAM) == {"input_tokens": 22546 - 14208, "output_tokens": 197,
                                                 "cache_read_tokens": 14208, "cache_write_tokens": 0}
    assert usage.codex_figures("") is None and usage.codex_figures(None) is None
    assert usage.codex_figures('{"type":"turn.started"}\nnot json turn.completed\n') is None
    two = ('{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1}}\n'
           '{"type":"turn.completed","usage":{"input_tokens":9,"cached_input_tokens":4,"output_tokens":2}}\n')
    assert usage.codex_figures(two) == {"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 4,
                                        "cache_write_tokens": None}


def test_the_model_a_codex_run_asks_for():
    assert usage.codex_model(["codex", "exec", "-m", "gpt-6.1-sol", "--json", "prompt"]) == "gpt-6.1-sol"
    assert usage.codex_model(["codex", "exec", "--model", "gpt-6-astra", "p"]) == "gpt-6-astra"
    assert usage.codex_model(["codex", "exec", "--json", "p"]) == "codex-default"  # the cover run asks for none


class FakeCodex:
    def __init__(self, stdout=CODEX_STREAM, returncode=0, exc=None):
        self.stdout, self.returncode, self.exc = stdout, returncode, exc

    def __call__(self, argv, **kw):
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


@pytest.fixture
def codex_checks_pass(monkeypatch):
    from haseeb_agent.deck import codexcheck
    monkeypatch.setattr(codexcheck, "codex_check", lambda *a, **k: None)


def cover(tmp_path, fake):
    return codex_cover("AI: Deep Review", tmp_path / "out" / "cover.png", codex="/opt/bin/codex", runner=fake,
                       codex_home=tmp_path / "codex-home", images_root=tmp_path / "images")


def test_a_cover_run_is_recorded_as_a_subscription_call_with_its_tokens(tmp_path, store, codex_checks_pass):
    with events.step(store, "decks", "design"):
        cover(tmp_path, FakeCodex())  # no image comes of it: the run still used its tokens
    [r] = rows(store)
    assert (r.provider, r.model, r.job, r.step, r.source, r.ok) == ("codex", "codex-default", "decks", "cover", "live", True)
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens) == (8338, 197, 14208)
    assert (r.cost_usd, r.cost_basis) == (None, "subscription")


def test_a_cover_run_with_no_counts_in_its_stream_still_leaves_a_row(tmp_path, store, codex_checks_pass):
    with events.step(store, "decks", "design"):
        cover(tmp_path, FakeCodex(stdout='{"type":"thread.started","thread_id":"x"}\n', returncode=1))
    [r] = rows(store)
    assert (r.ok, r.input_tokens, r.output_tokens, r.cost_basis) == (False, None, None, "subscription")


def test_a_cover_run_that_timed_out_is_a_failed_row(tmp_path, store, codex_checks_pass):
    with events.step(store, "decks", "design"):
        assert cover(tmp_path, FakeCodex(exc=subprocess.TimeoutExpired("codex", 5))) is None
    [r] = rows(store)
    assert (r.step, r.ok, r.input_tokens) == ("cover", False, None)


def test_a_cover_run_that_could_not_start_is_no_call(tmp_path, store, codex_checks_pass):
    with events.step(store, "decks", "design"):
        cover(tmp_path, FakeCodex(exc=FileNotFoundError("codex")))
    assert rows(store) == []


def test_a_designer_turn_is_recorded_with_the_model_it_asked_for(tmp_path, store):
    work = tmp_path / "work"
    work.mkdir()
    brief = work / "brief.md"
    brief.write_text("the brief", encoding="utf-8")
    tids: list = []
    with events.step(store, "decks", "design"):
        argv = designer.designer_argv("/opt/bin/codex", "gpt-6.1-sol", "low", work)
        designer._codex_turn(argv, brief, work, "/opt/bin/codex", FakeCodex(), 60, tids)
        argv = designer.designer_argv("/opt/bin/codex", "gpt-6-astra", "low", work, repair=True, images=False)
        designer._codex_turn(argv, brief, work, "/opt/bin/codex", FakeCodex(), 60, tids)
    first, repair = rows(store)
    assert (first.model, first.step, first.job, first.provider) == ("gpt-6.1-sol", "designer", "decks", "codex")
    assert (repair.model, repair.step) == ("gpt-6-astra", "designer_repair")
    assert (first.input_tokens, first.output_tokens, first.cost_basis) == (8338, 197, "subscription")


def test_a_designer_turn_that_breaks_the_lockdown_is_still_recorded(tmp_path, store):
    work = tmp_path / "work"
    work.mkdir()
    brief = work / "brief.md"
    brief.write_text("x", encoding="utf-8")
    bad = '{"type":"item.completed","item":{"type":"command_execution"}}\n' + CODEX_STREAM
    with events.step(store, "deck_test", "design"):
        with pytest.raises(designer._Fail):
            designer._codex_turn(designer.designer_argv("/opt/bin/codex", "m", "low", work), brief, work,
                                 "/opt/bin/codex", FakeCodex(stdout=bad), 60, [])
    [r] = rows(store)  # the deck was discarded, but the run used its tokens
    assert (r.job, r.step, r.output_tokens) == ("deck_test", "designer", 197)


def test_a_designer_turn_that_timed_out_is_a_failed_row(tmp_path, store):
    work = tmp_path / "work"
    work.mkdir()
    brief = work / "brief.md"
    brief.write_text("x", encoding="utf-8")
    with events.step(store, "decks", "design"):
        with pytest.raises(designer._Fail):
            designer._codex_turn(designer.designer_argv("/opt/bin/codex", "m", "low", work), brief, work,
                                 "/opt/bin/codex", FakeCodex(exc=subprocess.TimeoutExpired("codex", 5)), 5, [])
    [r] = rows(store)
    assert (r.ok, r.input_tokens) == (False, None)
