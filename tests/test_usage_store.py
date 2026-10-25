"""The `model_usage` table (store.py): one row per model call, text-free, added without touching an older database."""
import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from haseeb_agent.models import UsageRow
from haseeb_agent.store import Store

KW = timezone(timedelta(hours=3))
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=KW)
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# The columns the design names (docs/superpowers/specs/2026-10-21-usage-tab-design.md), and nothing else: in particular
# no column that could hold text.
COLUMNS = {"id", "at", "provider", "model", "job", "step", "input_tokens", "output_tokens", "cache_read_tokens",
           "cache_write_tokens", "cost_usd", "cost_basis", "latency_ms", "ok", "source", "ref"}


def row(**kw) -> UsageRow:
    return UsageRow(**{"at": T0, "provider": "claude", "model": "claude-haiku-5-5", **kw})


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agent.sqlite")
    yield s
    s.close()


def test_the_table_has_the_designed_columns_and_no_text_column(store, tmp_path):
    con = sqlite3.connect(tmp_path / "agent.sqlite")
    assert {r[1] for r in con.execute("PRAGMA table_info(model_usage)")} == COLUMNS
    assert {f.name for f in dataclasses.fields(UsageRow)} == COLUMNS  # the row object cannot carry text either
    con.close()


def test_a_row_round_trips(store):
    r = row(job="insights", step="research", input_tokens=6, output_tokens=389, cache_read_tokens=97451,
            cache_write_tokens=34280, cost_usd=0.00802561, cost_basis="list", latency_ms=26590, ok=False,
            source="backfill", ref="abc.jsonl")
    assert store.add_usage(r) is True
    [got] = store.usage_since(EPOCH)
    assert got.id is not None
    assert dataclasses.replace(got, id=None) == r
    assert got.at.utcoffset() == timedelta(hours=3)  # the offset is kept, so the day is the one it was written on


def test_a_database_made_before_the_table_gets_it_without_losing_anything(tmp_path):
    path = tmp_path / "old.sqlite"
    s = Store(path)
    s.set_cursor("keep", "me")
    s.record_run("insights", True, "fine")
    s.close()
    con = sqlite3.connect(path)  # what a database made before this change looks like: no model_usage
    con.execute("DROP TABLE model_usage")
    con.commit()
    con.close()
    s = Store(path)
    try:
        assert s.get_cursor("keep") == "me" and len(s.runs()) == 1
        assert s.usage_count() == 0
        assert s.add_usage(row())
        assert s.usage_count() == 1
    finally:
        s.close()
    s = Store(path)  # opening it again changes nothing
    try:
        assert s.usage_count() == 1
    finally:
        s.close()


def test_a_ref_is_added_once_per_provider_and_model(store):
    assert store.add_usage(row(ref="s1.jsonl", source="backfill"))
    assert not store.add_usage(row(ref="s1.jsonl", source="backfill"))   # the same run again
    assert not store.add_usage(row(ref="s1.jsonl", source="live"))       # ... or the live record of it
    assert store.add_usage(row(ref="s1.jsonl", model="claude-sonnet-5-5"))  # another model of the same run
    assert store.add_usage(row(ref="s1.jsonl", provider="litellm", model="claude-haiku-5-5"))
    assert store.usage_count() == 3


def test_rows_without_a_ref_are_never_taken_for_repeats(store):
    assert store.add_usage(row()) and store.add_usage(row())
    assert store.usage_count() == 2


def test_add_usage_rows_counts_what_went_in(store):
    rows = [row(ref="a"), row(ref="b"), row(ref="a"), row()]
    assert store.add_usage_rows(rows) == 3
    assert store.add_usage_rows(rows) == 1  # only the one without a ref goes in again
    assert store.add_usage_rows([]) == 0


@pytest.mark.parametrize("field,value", [
    ("model", "a sentence about a draft"), ("model", ""), ("model", "x" * 121), ("model", "<script>"), ("model", "line\nbreak"),
    ("model", "naïve"), ("model", None), ("job", "two words"), ("step", "x" * 121), ("step", "é"), ("ref", "a b"),
    ("ref", "x" * 121), ("ref", ""), ("ref", "tab\there"),
])
def test_an_identifier_that_could_hold_text_is_rejected_on_insert(store, field, value):
    """Review M8: the table enforces "no text" itself. Identifiers take [A-Za-z0-9._:/@-], at most 120 of them."""
    bad = row(**{field: value})
    with pytest.raises(ValueError) as caught:
        store.add_usage(bad)
    assert str(value)[:10] not in str(caught.value) or value in ("", None)  # the error names the column, never the value
    done = store.import_usage_rows([bad, row(ref="fine")])
    assert (done.added, done.rejected) == (1, 1) and store.usage_count() == 1


@pytest.mark.parametrize("value", ["a", "gemini/gemini-3.1-pro-preview", "day:2026-10-18:glm-5.3", "x" * 120, "a@b", "9-_.:/@",
                                   "ece52a2e-6925-4fd6-9fb8-e5287589abe8.jsonl", "approx:abc.jsonl"])
def test_the_identifiers_the_system_really_uses_are_accepted(store, value):
    assert store.add_usage(row(model=value, job=value, step=value, ref=value))


@pytest.mark.parametrize("field,value", [
    ("input_tokens", -1), ("input_tokens", 1.5), ("input_tokens", True), ("input_tokens", "5"), ("input_tokens", 2**62 + 1),
    ("output_tokens", 2**70), ("cache_read_tokens", -5), ("cache_write_tokens", "x"), ("latency_ms", -1),
    ("cost_usd", -0.01), ("cost_usd", float("nan")), ("cost_usd", float("inf")), ("cost_usd", True), ("cost_usd", "1"),
    ("cost_usd", 2e9), ("at", datetime(2026, 10, 20, 9, 0)), ("at", "2026-10-20T09:00:00+03:00"), ("at", None),
])
def test_a_number_out_of_range_or_a_time_with_no_offset_is_rejected_on_insert(store, field, value):
    bad = row(**{field: value})
    with pytest.raises(ValueError):
        store.add_usage(bad)
    done = store.import_usage_rows([bad, row(ref="fine")])  # the import counts it and goes on (it never raises)
    assert (done.added, done.rejected) == (1, 1)


def test_the_largest_values_the_table_takes_still_go_in(store):
    assert store.add_usage(row(input_tokens=2**62, output_tokens=0, latency_ms=2**62, cost_usd=1e9))


def test_a_bad_row_adds_nothing_at_all(store):
    for bad in (row(provider="openai"), row(cost_basis="guess"), row(source="import")):
        with pytest.raises(ValueError):
            store.add_usage_rows([row(ref="fine"), bad])
    assert store.usage_count() == 0


def test_usage_since_compares_times_across_offsets_and_orders_oldest_first(store):
    utc = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # 11:00 in Kuwait
    store.add_usage(row(at=T0 + timedelta(hours=3), ref="late"))       # 12:00 +03:00
    store.add_usage(row(at=utc, ref="mid"))                            # 08:00Z = 11:00 +03:00
    store.add_usage(row(at=T0 - timedelta(days=2), ref="old"))
    got = store.usage_since(T0 + timedelta(hours=1))                    # 10:00 +03:00
    assert [r.ref for r in got] == ["mid", "late"]
    assert [r.ref for r in store.usage_since(EPOCH)] == ["old", "mid", "late"]


def test_the_time_reads_use_an_index_on_the_time_as_they_compare_it(store):
    """They compare and order by julianday(at), so an index on the text of `at` could never be used by them (review M11)."""
    reads = (("SELECT * FROM model_usage WHERE julianday(at) >= julianday(?) ORDER BY julianday(at), id",
              ("2026-10-01T00:00:00+03:00",)),
             ("SELECT at FROM model_usage ORDER BY julianday(at), id LIMIT 1", ()))
    for sql, args in reads:
        plan = " ".join(r[3] for r in store._db.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "model_usage_time" in plan, plan


def test_a_database_with_the_first_index_gets_the_better_one_and_loses_the_other(tmp_path):
    path = tmp_path / "old.sqlite"
    Store(path).close()
    con = sqlite3.connect(path)  # made by the first version of the table: its index was on the text of `at`
    con.execute("DROP INDEX model_usage_time")
    con.execute("CREATE INDEX model_usage_at ON model_usage(at)")
    con.commit()
    con.close()
    s = Store(path)
    try:
        names = {r[1] for r in s._db.execute("PRAGMA index_list(model_usage)")}
        assert "model_usage_time" in names and "model_usage_at" not in names
        assert s.add_usage(row())
        assert s.usage_count() == 1
    finally:
        s.close()


def test_first_usage_at_by_source_and_provider(store):
    assert store.first_usage_at() is None
    store.add_usage(row(at=T0, source="backfill", ref="b"))
    store.add_usage(row(at=T0 + timedelta(hours=2), source="live", provider="litellm"))
    store.add_usage(row(at=T0 + timedelta(hours=1), source="live", provider="claude"))
    assert store.first_usage_at() == T0 + timedelta(hours=1)
    assert store.first_usage_at("live", "litellm") == T0 + timedelta(hours=2)
    assert store.first_usage_at("live", "codex") is None
    assert store.first_usage_at(None) == T0
    assert store.first_usage_at("backfill") == T0
    assert store.usage_count("live") == 2 and store.usage_count() == 3


# -- the backfill's way in ----------------------------------------------------------------------------------------------

def daily(day: str, model="glm-5.3", cost=3.0, **kw) -> UsageRow:
    """A cost-only daily total of the proxy: at the start of its UTC day (here shown in Kuwait's offset)."""
    at = datetime.fromisoformat(f"{day}T00:00:00+00:00").astimezone(KW)
    return row(at=at, provider="litellm", model=model, job="other", step="daily_total", cost_usd=cost, cost_basis="proxy",
               source="backfill", ref=f"day:{day}:{model}", **kw)


def test_importing_rows_can_replace_the_daily_totals_of_some_days_in_the_same_step(store):
    store.add_usage_rows([daily("2026-10-17"), daily("2026-10-18"), daily("2026-10-18", model="kimi-k3", cost=1.0)])
    keep = [row(provider="litellm", model="glm-5.3", step="daily_total", source="live", ref="live-day"),   # not a backfill
            row(provider="claude", model="glm-5.3", step="daily_total", source="backfill", ref="other-provider"),
            row(provider="litellm", model="glm-5.3", step="other", source="backfill", ref="a-request",
                at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))]                                              # not a day total
    store.add_usage_rows(keep)
    done = store.import_usage_rows([row(provider="litellm", model="glm-5.3", ref="req-1", source="backfill",
                                        at=datetime(2026, 10, 18, 9, 0, tzinfo=UTC))], replace_daily=["2026-10-18"])
    assert (done.added, done.replaced, done.rejected) == (1, 2, 0)
    assert sorted(r.ref for r in store.usage_since(EPOCH)) == sorted(
        ["day:2026-10-17:glm-5.3", "live-day", "other-provider", "a-request", "req-1"])


def test_replaced_counts_both_the_refs_and_the_daily_totals_it_deleted(store):
    store.add_usage_rows([daily("2026-10-18"), row(provider="litellm", model="glm-5.3", ref="approx:run-1",
                                                   source="backfill", at=datetime(2026, 10, 18, 9, 0, tzinfo=UTC))])
    done = store.import_usage_rows([], replace_daily=["2026-10-18"], replace_refs=[("litellm", "approx:run-1")])
    assert (done.added, done.replaced, done.rejected) == (0, 2, 0) and store.usage_count() == 0

def test_a_day_is_a_utc_day_whatever_offset_the_time_is_kept_in(store):
    store.add_usage_rows([row(at=datetime(2026, 10, 18, 2, 0, tzinfo=KW), provider="litellm", ref="a", source="backfill"),   # 23:00Z on the 17th
                          row(at=datetime(2026, 10, 18, 4, 0, tzinfo=KW), provider="litellm", ref="b", source="backfill"),   # 01:00Z on the 18th
                          row(at=T0, provider="litellm", ref="c", source="live"),
                          daily("2026-10-20")])
    assert store.usage_utc_days("litellm", "backfill", step_not="daily_total") == {"2026-10-17", "2026-10-18"}
    assert store.usage_utc_days("litellm", "backfill") == {"2026-10-17", "2026-10-18", "2026-10-20"}
    assert store.usage_utc_days("claude", "backfill") == set()
    rep = store.import_usage_rows([], replace_daily=["2026-10-17"])  # nothing of that day is a daily total: nothing goes
    assert (rep.added, rep.replaced) == (0, 0) and store.usage_count() == 4


def test_a_row_that_cannot_go_in_is_left_out_and_counted_not_the_end_of_the_import(store):
    rows = [row(ref="ok-1"), row(provider="openai", ref="bad"), row(ref="ok-2"), row(cost_basis="guess", ref="bad-2")]
    done = store.import_usage_rows(rows)
    assert (done.added, done.rejected) == (2, 2)
    assert sorted(r.ref for r in store.usage_since(EPOCH)) == ["ok-1", "ok-2"]
