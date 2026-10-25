"""`GET /api/usage` (dashboard/api.py, usage_report.py): the numbers behind the Usage tab. TestClient and tmp stores only: no
server, nothing real. Rows are invented; days are Kuwait's."""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from haseeb_agent import usage_report
from haseeb_agent.config import load_settings
from haseeb_agent.dashboard.app import create_app
from haseeb_agent.models import UsageRow
from haseeb_agent.store import Store
from tests.test_dashboard_security import BASE, KEY, do_login

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=KW)  # a Wednesday


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return replace(load_settings(ROOT / "tests" / "fixtures" / "config.toml", env), db_path=str(tmp_path / "agent.sqlite"))


@pytest.fixture
def store(settings):
    s = Store(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def app(settings, tmp_path):
    return create_app(settings, lambda: Store(settings.db_path), KEY, lambda: None, clock=Clock(NOW.timestamp()),
                      login_dir=tmp_path / "login", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks",
                      spawn=lambda args: None, started_at=NOW.timestamp() - 86400)


@pytest.fixture
def session(app, tmp_path):
    return do_login(app, tmp_path / "login", Clock(NOW.timestamp()))


@pytest.fixture
def c(session):
    client, token = session
    client.headers.update({"X-Session-Token": token, "Origin": BASE})
    return client


def put(store, day_offset=0, hour=9, **kw):
    """A usage row `day_offset` days before NOW's day, at `hour` Kuwait time."""
    at = (NOW + timedelta(days=day_offset)).replace(hour=hour, minute=0)
    base = {"at": at, "provider": "claude", "model": "claude-haiku-5-5", "job": "todo", "step": "slack_read_channel",
            "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01, "cost_basis": "list", "latency_ms": 1000}
    store.add_usage(UsageRow(**{**base, **kw}))


# ---------------------------------------------------------------- the door

def test_the_route_needs_a_session_and_a_token(app, session):
    anon = TestClient(app, base_url=BASE, follow_redirects=False)
    assert anon.get("/api/usage").status_code == 401
    client, token = session
    assert client.get("/api/usage").status_code == 403  # a cookie but no token
    assert client.get("/api/usage", headers={"X-Session-Token": "0" * 64}).status_code == 403
    assert client.get("/api/usage", headers={"X-Session-Token": token}).status_code == 200
    assert client.get("/api/usage", headers={"X-Session-Token": token}).headers["cache-control"] == "no-store"


def test_it_only_reads(c, store):
    put(store)
    before = store.usage_count()
    assert c.get("/api/usage").status_code == 200
    for method in (c.post, c.put, c.delete):
        assert method("/api/usage").status_code in (404, 405)
    assert store.usage_count() == before


@pytest.mark.parametrize("bad", ["", "1d", "14d", "week", "7D", "7d;", "../", "0", "<script>"])
def test_a_bad_range_is_a_short_fixed_error(c, bad):
    r = c.get("/api/usage", params={"range": bad})
    assert r.status_code == 400 and r.json() == {"error": "bad range"}


def test_the_default_range_is_seven_days(c):
    assert c.get("/api/usage").json()["range"] == "7d"


# ---------------------------------------------------------------- an empty database

def test_an_empty_database_gives_zeros_and_empty_lists_not_an_error(c):
    d = c.get("/api/usage").json()
    assert d["has_data"] is False
    assert d["totals"] == {"cost_by_basis": {}, "by_provider": [], "input_tokens": 0, "output_tokens": 0,
                           "cache_read_tokens": 0, "cache_write_tokens": 0, "calls": 0, "failures": 0,
                           "unpriced_calls": 0, "approx_calls": 0}
    assert set(d["ranges"]) == {"today", "7d", "30d"} and d["ranges"]["30d"] == d["totals"]
    assert d["by_model"] == [] and d["by_job"] == [] and d["notes"] == [] and d["chart_models"] == []
    assert len(d["daily"]) == 14 and all(x["tokens"] == 0 and x["cost_by_basis"] == {} and x["models"] == []
                                         for x in d["daily"])
    assert d["history"] == {"first_live": None, "litellm": {"state": "not_run", "counted_from": None},
                            "claude": {"from": None, "to": None}}


# ---------------------------------------------------------------- the ranges

def test_today_7d_and_30d_are_whole_kuwait_days_with_today_included(c, store):
    put(store, 0, hour=0)         # 00:00 today
    put(store, -6)                # the 15th: the 7-day window's first day
    put(store, -7)                # the 14th: one day too old for 7d
    put(store, -29)               # Sep 22: the 30-day window's first day
    put(store, -30)               # Sep 21: too old for 30d
    put(store, 1)                 # tomorrow: not counted anywhere
    calls = {r: c.get("/api/usage", params={"range": r}).json()["totals"]["calls"] for r in ("today", "7d", "30d")}
    assert calls == {"today": 1, "7d": 2, "30d": 4}
    d = c.get("/api/usage", params={"range": "7d"}).json()
    assert {k: v["calls"] for k, v in d["ranges"].items()} == {"today": 1, "7d": 2, "30d": 4}  # the cards, all at once
    assert (d["days"], d["since"], d["today"]) == (7, "2026-10-15", "2026-10-21")


def test_a_row_belongs_to_the_day_it_has_in_kuwait_not_in_utc(c, store):
    # 23:30 UTC on the 20th is 02:30 on the 21st in Kuwait
    store.add_usage(UsageRow(at=datetime(2026, 10, 20, 23, 30, tzinfo=ZoneInfo("UTC")), provider="claude", model="m"))
    assert c.get("/api/usage", params={"range": "today"}).json()["totals"]["calls"] == 1


# ---------------------------------------------------------------- the tables

def test_by_model_adds_calls_tokens_failures_and_known_costs(c, store):
    put(store, input_tokens=10, cache_read_tokens=90, cache_write_tokens=20, output_tokens=5, cost_usd=0.5, latency_ms=100)
    put(store, input_tokens=2, output_tokens=1, cost_usd=0.25, latency_ms=300)
    put(store, ok=False, input_tokens=None, output_tokens=None, cost_usd=None, cost_basis="unknown", latency_ms=500)
    [m] = c.get("/api/usage").json()["by_model"]
    assert m == {"provider": "claude", "model": "claude-haiku-5-5", "calls": 3, "failures": 1,
                 "input_tokens": 10 + 90 + 20 + 2,   # the whole prompt: fresh, cached and written to the cache
                 "output_tokens": 6, "cache_read_tokens": 90, "cache_write_tokens": 20, "tokens": 128,
                 "cost_usd": 0.75, "cost_basis": "list", "cost_by_basis": {"list": 0.75},
                 "unpriced_calls": 1, "approx_calls": 0, "latency_ms": 300}  # the median of 100, 300, 500


def test_a_model_with_no_known_cost_has_none_not_zero(c, store):
    put(store, provider="codex", model="gpt-6.1-sol", cost_usd=None, cost_basis="subscription", input_tokens=100)
    put(store, provider="litellm", model="glm-5.3", cost_usd=None, cost_basis="unknown", input_tokens=50)
    d = c.get("/api/usage").json()
    by = {m["model"]: m for m in d["by_model"]}
    assert (by["gpt-6.1-sol"]["cost_usd"], by["gpt-6.1-sol"]["cost_basis"]) == (None, "subscription")
    assert (by["glm-5.3"]["cost_usd"], by["glm-5.3"]["cost_basis"]) == (None, "unknown")
    assert d["totals"]["cost_by_basis"] == {} and d["totals"]["unpriced_calls"] == 2
    assert [(p["provider"], p["cost_usd"]) for p in d["totals"]["by_provider"]] == [("litellm", None), ("codex", None)]
    assert d["totals"]["input_tokens"] == 150


def test_free_is_a_cost_of_zero_not_an_unknown_one(c, store):
    put(store, provider="litellm", model="free-model", cost_usd=0.0, cost_basis="proxy")
    d = c.get("/api/usage").json()
    assert d["totals"]["cost_by_basis"] == {"proxy": 0.0} and d["totals"]["unpriced_calls"] == 0
    assert d["totals"]["by_provider"][0]["cost_usd"] == 0.0 and d["by_model"][0]["cost_usd"] == 0.0


def test_no_total_adds_costs_of_different_bases_each_provider_is_totalled_on_its_own(c, store):
    put(store, cost_usd=1.5, cost_basis="list", input_tokens=100, output_tokens=10, cache_read_tokens=50,
        cache_write_tokens=5)
    put(store, provider="litellm", model="glm-5.3", cost_usd=0.25, cost_basis="proxy")
    put(store, provider="litellm", model="glm-5.3", cost_usd=0.5, cost_basis="proxy")
    put(store, provider="codex", model="gpt-6.1-sol", cost_usd=None, cost_basis="subscription", input_tokens=700)
    d = c.get("/api/usage").json()
    for t in (d["totals"], *d["ranges"].values()):
        assert "cost_usd" not in t                      # there is no single cost of the whole
        assert t["cost_by_basis"] == {"list": 1.5, "proxy": 0.75}
        assert [(p["provider"], p["cost_usd"], p["cost_basis"], p["calls"]) for p in t["by_provider"]] == [
            ("claude", 1.5, "list", 1), ("litellm", 0.75, "proxy", 2), ("codex", None, "subscription", 1)]
    claude = d["totals"]["by_provider"][0]
    assert (claude["input_tokens"], claude["output_tokens"], claude["cache_read_tokens"],
            claude["cache_write_tokens"]) == (155, 10, 50, 5)
    assert d["totals"]["by_provider"][2]["input_tokens"] == 700 and d["totals"]["by_provider"][2]["unpriced_calls"] == 1


def test_a_model_whose_priced_calls_differ_in_basis_is_mixed_and_an_unpriced_call_does_not_make_it_so(c, store):
    put(store, model="a", cost_usd=1.0, cost_basis="list")
    put(store, model="a", cost_usd=None, cost_basis="unknown", ok=False)  # a run that ended with no result
    put(store, model="b", cost_usd=1.0, cost_basis="list")
    put(store, model="b", cost_usd=1.0, cost_basis="proxy")
    by = {m["model"]: m for m in c.get("/api/usage").json()["by_model"]}
    assert (by["a"]["cost_basis"], by["b"]["cost_basis"]) == ("list", "mixed")
    assert (by["a"]["cost_usd"], by["a"]["cost_by_basis"]) == (1.0, {"list": 1.0})
    assert (by["b"]["cost_usd"], by["b"]["cost_by_basis"]) == (None, {"list": 1.0, "proxy": 1.0})  # no sum across bases


def test_the_tables_list_the_dearest_first_then_the_most_tokens_then_the_name(c, store):
    put(store, model="cheap", cost_usd=0.1, input_tokens=1)
    put(store, model="dear", cost_usd=2.0, input_tokens=1)
    put(store, provider="codex", model="b-free", cost_usd=None, cost_basis="subscription", input_tokens=500)
    put(store, provider="codex", model="a-free", cost_usd=None, cost_basis="subscription", input_tokens=500)
    put(store, provider="codex", model="z-big", cost_usd=None, cost_basis="subscription", input_tokens=900)
    assert [m["model"] for m in c.get("/api/usage").json()["by_model"]] == ["dear", "cheap", "z-big", "a-free", "b-free"]


def test_by_job_adds_each_job_up(c, store):
    put(store, job="todo", cost_usd=0.5, input_tokens=10, output_tokens=5)
    put(store, job="todo", cost_usd=0.5, input_tokens=10, output_tokens=5, ok=False)
    put(store, job="insights", cost_usd=3.0, input_tokens=100, output_tokens=50, provider="litellm", cost_basis="proxy")
    put(store, job="deck_test", cost_usd=None, cost_basis="subscription", provider="codex", input_tokens=7)
    jobs = c.get("/api/usage").json()["by_job"]
    assert [j["job"] for j in jobs] == ["insights", "todo", "deck_test"]
    assert jobs[1] == {"job": "todo", "calls": 2, "failures": 1, "input_tokens": 20, "output_tokens": 10,
                       "cache_read_tokens": 0, "cache_write_tokens": 0, "tokens": 30, "cost_by_basis": {"list": 1.0},
                       "unpriced_calls": 0, "approx_calls": 0}
    assert jobs[0]["cost_by_basis"] == {"proxy": 3.0} and jobs[2]["cost_by_basis"] == {}
    assert all("cost_usd" not in j for j in jobs)  # a job can have both a list price and a proxy cost: never one figure


def test_the_tables_follow_the_chosen_range_while_the_cards_do_not(c, store):
    put(store, 0, model="today-model")
    put(store, -3, model="week-model")
    put(store, -20, model="month-model")
    d = c.get("/api/usage", params={"range": "today"}).json()
    assert [m["model"] for m in d["by_model"]] == ["today-model"]
    assert [d["ranges"][k]["calls"] for k in ("today", "7d", "30d")] == [1, 2, 3]
    assert {m["model"] for m in c.get("/api/usage", params={"range": "30d"}).json()["by_model"]} == {
        "today-model", "week-model", "month-model"}


# ---------------------------------------------------------------- the chart

def test_the_daily_series_is_14_days_oldest_first_with_each_models_share(c, store):
    put(store, 0, model="a", input_tokens=10, output_tokens=0, cost_usd=0.5)
    put(store, 0, model="b", provider="litellm", input_tokens=30, output_tokens=0, cost_usd=None, cost_basis="unknown")
    put(store, -2, model="a", input_tokens=5, output_tokens=5, cost_usd=0.25)
    put(store, -13, model="a", input_tokens=1, output_tokens=0, cost_usd=0.01)
    put(store, -14, model="a", input_tokens=99)  # one day too old
    d = c.get("/api/usage").json()
    daily = d["daily"]
    assert [x["day"] for x in daily] == [(NOW.date() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    assert daily[0]["tokens"] == 1 and daily[-1]["tokens"] == 40 and daily[-1]["cost_by_basis"] == {"list": 0.5}
    assert daily[-1]["models"] == [{"key": "claude:a", "cost_by_basis": {"list": 0.5}, "tokens": 10},
                                   {"key": "litellm:b", "cost_by_basis": {}, "tokens": 30}]
    assert daily[-3]["cost_by_basis"] == {"list": 0.25} and daily[-2]["models"] == []
    # the models in the order the chart colours them: most tokens in the 14 days first
    assert [m["key"] for m in d["chart_models"]] == ["litellm:b", "claude:a"]
    assert d["chart_models"][0]["tokens"] == 30 and d["chart_max_models"] == usage_report.CHART_MODELS
    assert d["chart_models"][1] == {"key": "claude:a", "provider": "claude", "model": "a", "tokens": 21,
                                    "cost_by_basis": {"list": 0.76}}  # the 14 days, not only today


def test_the_chart_does_not_depend_on_the_chosen_range(c, store):
    put(store, -9, model="a")
    assert c.get("/api/usage", params={"range": "today"}).json()["daily"] == c.get(
        "/api/usage", params={"range": "30d"}).json()["daily"]


# ---------------------------------------------------------------- what the numbers mean

def test_a_note_for_each_kind_of_cost_in_the_range_and_no_more(c, store):
    put(store, 0)                                                                    # claude, list
    put(store, 0, provider="litellm", model="glm-5.3", cost_usd=0.1, cost_basis="proxy")
    put(store, 0, provider="litellm", model="kimi-k3", cost_usd=None, cost_basis="unknown")
    put(store, 0, provider="codex", model="gpt-6.1-sol", cost_usd=None, cost_basis="subscription")
    put(store, -20, provider="codex", model="old", cost_usd=None, cost_basis="unknown")  # outside the 7 days
    notes = c.get("/api/usage").json()["notes"]
    assert [(n["provider"], n["basis"]) for n in notes] == [("claude", "list"), ("litellm", "proxy"),
                                                           ("litellm", "unknown"), ("codex", "subscription"),
                                                           ("", "runs")]
    assert notes[0]["text"] == "List price, paid from your Claude subscription."
    assert notes[1]["text"] == "Company proxy cost."
    assert notes[3]["text"] == "ChatGPT Pro (tokens only)."
    assert ("codex", "unknown") in [(n["provider"], n["basis"]) for n in c.get("/api/usage", params={"range": "30d"}).json()["notes"]]


# ---------------------------------------------------------------- where the numbers begin

def test_history_says_where_the_counting_began_and_what_the_proxy_import_did(c, store):
    put(store, -3, provider="claude", source="live")
    put(store, -2, provider="litellm", model="glm-5.3", source="live")
    put(store, -9, source="backfill", ref="old.jsonl")
    h = c.get("/api/usage").json()["history"]
    assert h == {"first_live": "2026-10-18", "litellm": {"state": "not_run", "counted_from": "2026-10-19"},
                 "claude": {"from": "2026-10-12", "to": "2026-10-12"}}
    for state in ("imported", "unavailable", "skipped"):
        store.set_cursor("usage_backfill:litellm", json.dumps({"state": state, "reason": "404"}))
        assert c.get("/api/usage").json()["history"]["litellm"]["state"] == state
    store.set_cursor("usage_backfill:litellm", "not json")
    assert c.get("/api/usage").json()["history"]["litellm"]["state"] == "not_run"
    store.set_cursor("usage_backfill:litellm", json.dumps({"state": "who knows"}))
    assert c.get("/api/usage").json()["history"]["litellm"]["state"] == "not_run"


def test_the_proxy_date_falls_back_to_the_first_live_record_of_any_provider(c, store):
    put(store, -4, source="live")
    assert c.get("/api/usage").json()["history"]["litellm"]["counted_from"] == "2026-10-17"


def test_a_daily_total_adds_cost_but_no_call_no_tokens_and_no_time(c, store):
    put(store, -9, provider="litellm", model="glm-5.3", job="other", step="daily_total", input_tokens=None,
        output_tokens=None, cost_usd=2.0, cost_basis="proxy", latency_ms=None, source="backfill",
        ref="day:2026-10-12:glm-5.3")
    put(store, 0, provider="litellm", model="glm-5.3", cost_usd=0.5, cost_basis="proxy", input_tokens=10, output_tokens=5,
        latency_ms=700)
    d = c.get("/api/usage", params={"range": "30d"}).json()
    t = d["totals"]
    assert (t["cost_by_basis"], t["input_tokens"], t["output_tokens"], t["calls"], t["failures"], t["unpriced_calls"]) == (
        {"proxy": 2.5}, 10, 5, 1, 0, 0)
    [p] = t["by_provider"]
    assert (p["provider"], p["cost_usd"], p["calls"], p["unpriced_calls"]) == ("litellm", 2.5, 1, 0)
    [m] = d["by_model"]
    assert (m["calls"], m["cost_usd"], m["tokens"], m["latency_ms"], m["unpriced_calls"]) == (1, 2.5, 15, 700, 0)
    assert [(j["job"], j["calls"], j["cost_by_basis"]) for j in d["by_job"]] == [
        ("other", 0, {"proxy": 2.0}), ("todo", 1, {"proxy": 0.5})]
    chart_day = next(x for x in d["daily"] if x["day"] == "2026-10-12")
    assert (chart_day["cost_by_basis"], chart_day["tokens"]) == ({"proxy": 2.0}, 0)
    [note] = [n for n in d["notes"] if n["basis"] == "daily"]
    assert note == {"provider": "litellm", "basis": "daily", "text": usage_report.DAILY_NOTE}
    assert "UTC days" in note["text"]  # the proxy's days are not Kuwait's (review M4)
    week = c.get("/api/usage", params={"range": "7d"}).json()  # the day total is outside the 7 days: no note about it
    assert week["totals"]["cost_by_basis"] == {"proxy": 0.5} and all(n["basis"] != "daily" for n in week["notes"])


def test_nothing_but_daily_totals_is_still_data(c, store):
    put(store, -2, provider="litellm", model="glm-5.3", step="daily_total", input_tokens=None, output_tokens=None,
        cost_usd=1.0, cost_basis="proxy", latency_ms=None, source="backfill", ref="day:2026-10-19:glm-5.3")
    d = c.get("/api/usage").json()
    assert d["has_data"] is True and d["totals"]["calls"] == 0 and d["totals"]["cost_by_basis"] == {"proxy": 1.0}


def test_backfilled_rows_are_counted_like_live_ones(c, store):
    put(store, -5, source="backfill", ref="a.jsonl", model="claude-sonnet-5-5", cost_usd=2.0)
    put(store, 0, source="live", model="claude-sonnet-5-5", cost_usd=1.0)
    d = c.get("/api/usage").json()
    assert d["totals"]["cost_by_basis"] == {"list": 3.0} and d["by_model"][0]["calls"] == 2 and d["has_data"] is True


def test_has_data_means_any_rows_ever_even_when_none_is_in_the_last_30_days(c, store):
    put(store, -60)
    d = c.get("/api/usage").json()
    assert d["has_data"] is True and d["totals"]["calls"] == 0 and d["by_model"] == []


# ---------------------------------------------------------------- runs, not rows (review M6)

def test_a_run_that_used_two_models_is_one_run_in_a_total_and_one_in_each_models_row(c, store):
    put(store, model="a", ref="run-1.jsonl")
    put(store, model="b", ref="run-1.jsonl")                              # the same run, a second model
    put(store, model="a", ref="run-2.jsonl")
    put(store, provider="litellm", model="a", ref="run-1.jsonl", cost_basis="proxy")  # another provider: its own run
    put(store, provider="litellm", model="a", ref=None, cost_basis="proxy")           # no ref: a run of its own
    put(store, provider="litellm", model="a", ref=None, cost_basis="proxy")
    d = c.get("/api/usage").json()
    assert d["totals"]["calls"] == 5
    assert {p["provider"]: p["calls"] for p in d["totals"]["by_provider"]} == {"claude": 2, "litellm": 3}
    assert {(m["provider"], m["model"]): m["calls"] for m in d["by_model"]} == {
        ("claude", "a"): 2, ("claude", "b"): 1, ("litellm", "a"): 3}
    assert d["by_job"][0]["calls"] == 5
    assert any(n["basis"] == "runs" and n["text"] == usage_report.RUNS_NOTE for n in d["notes"])


def test_failures_and_unpriced_runs_are_counted_per_run_too(c, store):
    put(store, model="a", ref="r.jsonl", ok=False)
    put(store, model="b", ref="r.jsonl", ok=False, cost_usd=None, cost_basis="unknown")
    put(store, model="a", ref="s.jsonl", cost_usd=None, cost_basis="unknown")
    put(store, model="b", ref="s.jsonl", cost_usd=1.0)
    d = c.get("/api/usage").json()
    t = d["totals"]
    assert (t["calls"], t["failures"], t["unpriced_calls"]) == (2, 1, 0)   # a run with any priced row is priced
    by = {m["model"]: m for m in d["by_model"]}
    assert (by["a"]["calls"], by["a"]["failures"], by["a"]["unpriced_calls"]) == (2, 1, 1)
    assert (by["b"]["calls"], by["b"]["failures"], by["b"]["unpriced_calls"]) == (2, 1, 1)


def test_cache_writes_are_in_every_table(c, store):
    put(store, input_tokens=1, cache_read_tokens=2, cache_write_tokens=30, output_tokens=4, job="todo")
    put(store, input_tokens=1, cache_read_tokens=2, cache_write_tokens=30, output_tokens=4, job="insights")
    d = c.get("/api/usage").json()
    assert d["totals"]["cache_write_tokens"] == 60 and d["totals"]["input_tokens"] == 2 * (1 + 2 + 30)
    assert d["totals"]["by_provider"][0]["cache_write_tokens"] == 60
    assert d["by_model"][0]["cache_write_tokens"] == 60
    assert {j["job"]: j["cache_write_tokens"] for j in d["by_job"]} == {"todo": 30, "insights": 30}


# ---------------------------------------------------------------- approximate figures (review M1)

def test_approximate_runs_are_counted_wherever_they_are_and_noted(c, store):
    put(store, model="m1", source="backfill", ref="approx:a.jsonl")
    put(store, model="m2", source="backfill", ref="approx:a.jsonl")        # the same run, a second model
    put(store, model="m1", source="backfill", ref="b.jsonl")
    put(store, provider="litellm", model="glm-5.3", cost_basis="proxy")
    d = c.get("/api/usage").json()
    assert d["totals"]["calls"] == 3 and d["totals"]["approx_calls"] == 1
    assert {p["provider"]: p["approx_calls"] for p in d["totals"]["by_provider"]} == {"claude": 1, "litellm": 0}
    assert {(m["provider"], m["model"]): m["approx_calls"] for m in d["by_model"]} == {
        ("claude", "m1"): 1, ("claude", "m2"): 1, ("litellm", "glm-5.3"): 0}
    assert d["by_job"][0]["approx_calls"] == 1
    assert [n["text"] for n in d["notes"] if n["basis"] == "approx"] == [usage_report.APPROX_NOTE]
    assert "≈" in usage_report.APPROX_NOTE


def test_no_approximate_note_when_nothing_in_the_range_is(c, store):
    put(store, -20, source="backfill", ref="approx:old.jsonl")
    put(store, 0, source="backfill", ref="exact.jsonl")
    assert all(n["basis"] != "approx" for n in c.get("/api/usage").json()["notes"])
    assert any(n["basis"] == "approx" for n in c.get("/api/usage", params={"range": "30d"}).json()["notes"])


# ---------------------------------------------------------------- other accounts (review M11)

def test_the_note_about_other_claude_accounts_names_the_days_of_the_history(c, store):
    put(store, -4, source="backfill", ref="a.jsonl")        # 17 Oct
    put(store, -1, source="backfill", ref="b.jsonl")        # 20 Oct
    put(store, 0, source="live", ref="c.jsonl")             # a live run is not history
    put(store, -2, provider="litellm", model="glm-5.3", source="backfill", ref="req-1", cost_basis="proxy")
    d = c.get("/api/usage").json()
    assert d["history"]["claude"] == {"from": "2026-10-17", "to": "2026-10-20"}
    [n] = [n for n in d["notes"] if n["basis"] == "history"]
    assert n["text"] == "History includes runs on other Claude accounts used on this Mac (17–20 Oct)"
    assert [m["text"] for m in c.get("/api/usage", params={"range": "today"}).json()["notes"]
            if m["basis"] == "history"] == [n["text"]]   # about the page's history, whichever range is chosen


def test_no_note_about_accounts_without_claude_history(c, store):
    put(store, 0, source="live")
    put(store, -2, provider="litellm", model="glm-5.3", source="backfill", ref="req-1", cost_basis="proxy")
    d = c.get("/api/usage").json()
    assert d["history"]["claude"] == {"from": None, "to": None}
    assert all(n["basis"] != "history" for n in d["notes"])


@pytest.mark.parametrize("first, last, text", [
    ("2026-10-21", "2026-10-21", "21 Oct"),
    ("2026-10-17", "2026-10-21", "17–21 Oct"),
    ("2026-09-28", "2026-10-03", "28 Sep–3 Oct"),
    ("2026-12-30", "2027-01-02", "30 Dec–2 Jan"),
])
def test_the_span_of_days_reads_like_a_person_would_write_it(first, last, text):
    from datetime import date
    assert usage_report._span(date.fromisoformat(first), date.fromisoformat(last)) == text


def test_the_answer_is_plain_json_numbers_strings_and_nulls(c, store):
    put(store)
    text = c.get("/api/usage").text
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text)
