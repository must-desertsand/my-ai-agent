from datetime import date, datetime, timedelta, timezone

import pytest

from haseeb_agent.models import Item
from haseeb_agent.store import UNCOUNTED_JOBS, Store

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)


def make_item(id="i1", kind="insight", day=date(2026, 10, 19), **kw):
    base = dict(
        id=id, kind=kind, day=day, status="drafted", body="body", title="title",
        dm_ts=None, source_ts=None, score=None, models={}, attempts=0,
        edited=False, created_at=NOW, acted_at=None,
    )
    base.update(kw)
    return Item(**base)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "sub" / "agent.sqlite")


def test_store_roundtrip_and_item_by_dm_ts(store):
    item = make_item(
        dm_ts="1760000000.000100", source_ts="1759999999.000200", score=88.89,
        models={"wrote": "claude:sonnet", "judged": "kimi-k3"}, attempts=1,
        edited=True, acted_at=NOW + timedelta(minutes=5),
    )
    store.upsert_item(item)
    assert store.get_item("i1") == item
    assert store.item_by_dm_ts("1760000000.000100") == item
    assert store.item_by_dm_ts("nope") is None
    assert store.get_item("missing") is None


def test_upsert_replaces_existing(store):
    store.upsert_item(make_item(body="v1"))
    store.upsert_item(make_item(body="v2"))
    assert store.get_item("i1").body == "v2"


def test_insert_item_never_overwrites_an_existing_row(store):
    """C1: two runs that both draft one post must not write the later draft over the earlier item, a posted one
    included."""
    assert store.insert_item(make_item(body="first", dm_ts="1.1")) is True
    store.mark("i1", "posted", acted_at=NOW)
    assert store.insert_item(make_item(body="second", dm_ts="2.2")) is False
    got = store.get_item("i1")
    assert (got.status, got.body, got.dm_ts, got.acted_at) == ("posted", "first", "1.1", NOW)


def test_mark_updates_status_and_fields(store):
    store.upsert_item(make_item())
    store.mark("i1", "delivered", dm_ts="111.222", models={"wrote": "m"}, attempts=2)
    got = store.get_item("i1")
    assert (got.status, got.dm_ts, got.models, got.attempts) == (
        "delivered", "111.222", {"wrote": "m"}, 2,
    )
    store.mark("i1", "posted", edited=True, acted_at=NOW)
    got = store.get_item("i1")
    assert (got.status, got.edited, got.acted_at) == ("posted", True, NOW)
    assert got.dm_ts == "111.222"


def test_mark_rejects_unknown_field_and_status(store):
    store.upsert_item(make_item())
    with pytest.raises(ValueError):
        store.mark("i1", "posted", nonsense=1)
    with pytest.raises(ValueError):
        store.mark("i1", "bogus")
    assert store.get_item("i1").status == "drafted"


def test_items_for_day_filters_kind_and_day(store):
    d = date(2026, 10, 19)
    store.upsert_item(make_item("a", "insight", d))
    store.upsert_item(make_item("b", "insight", d))
    store.upsert_item(make_item("c", "todo", d))
    store.upsert_item(make_item("d", "insight", d - timedelta(days=1)))
    assert [i.id for i in store.items_for_day("insight", d)] == ["a", "b"]
    assert [i.id for i in store.items_for_day("todo", d)] == ["c"]


def test_recent_items_window(store):
    today = date(2026, 10, 19)
    for n, back in enumerate([0, 3, 7, 8]):
        store.upsert_item(make_item(f"x{n}", day=today - timedelta(days=back)))
    store.upsert_item(make_item("t", "todo", today))
    got = store.recent_items("insight", 7, today=today)
    assert [i.id for i in got] == ["x0", "x1", "x2"]  # newest first, 7 days inclusive


def test_cursor_set_get(store):
    assert store.get_cursor("seen:1") is None
    store.set_cursor("seen:1", "100")
    assert store.get_cursor("seen:1") == "100"
    store.set_cursor("seen:1", "200")
    assert store.get_cursor("seen:1") == "200"


def test_prefs_newest_first(store):
    for t in ["too long", "make it about Pakistan", "skipped: covered"]:
        store.add_pref(t)
    assert store.prefs() == ["skipped: covered", "make it about Pakistan", "too long"]
    assert store.prefs(limit=2) == ["skipped: covered", "make it about Pakistan"]


def test_prefs_of_a_kind_are_kept_apart_from_the_general_ones(store):
    """What Haseeb asks of his own messages steers messages only, and the general list never carries it."""
    store.add_pref("general one")
    store.add_pref("message only", kind="message")
    store.add_pref("another general")
    store.add_pref("message two", kind="message")
    assert store.prefs() == ["another general", "general one"]
    assert store.prefs(kind="message") == ["message two", "message only"]
    assert store.prefs(limit=1, kind="message") == ["message two"]
    assert store.prefs(kind="insight") == []


def test_a_database_made_before_prefs_had_a_kind_is_migrated_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE prefs (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL);"
                     "INSERT INTO prefs (text) VALUES ('old pref');")
    db.commit()
    db.close()
    first = Store(path)
    assert first.prefs() == ["old pref"]  # what was there is general
    first.add_pref("new", kind="message")
    assert first.prefs(kind="message") == ["new"] and first.prefs() == ["old pref"]
    first.close()
    second = Store(path)  # opening it again changes nothing and does not fail
    assert second.prefs() == ["old pref"] and second.prefs(kind="message") == ["new"]
    second.close()


def test_the_old_way_of_adding_a_pref_still_works_on_a_migrated_database(tmp_path):
    """A tick that is still the old code (a pref insert with no kind) must keep working after the migration."""
    import sqlite3
    path = tmp_path / "agent.sqlite"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO prefs (text) VALUES ('from old code')")
    db.commit()
    db.close()
    store = Store(path)
    assert store.prefs() == ["from old code"]
    store.close()


def test_record_run(store):
    store.record_run("tick", True, "ok")
    store.record_run("insights", False, "boom")
    rows = store.runs()
    assert [(r[0], r[1], r[2]) for r in rows] == [("insights", False, "boom"), ("tick", True, "ok")]


def test_persists_across_instances(tmp_path):
    p = tmp_path / "a.sqlite"
    Store(p).upsert_item(make_item())
    assert Store(p).get_item("i1") is not None


def test_set_model_changes_only_that_models_key(store):
    """Designer decks: the detached deck process records its model without ever touching status (no revert race)."""
    store.upsert_item(make_item(status="delivered", models={"wrote": "sonnet", "judged": "gemini"}))
    store.mark("i1", "posted")  # a post recorded by another process
    store.set_model("i1", "deck", "codex gpt-6.1-sol")
    got = store.get_item("i1")
    assert got.status == "posted"
    assert got.models == {"wrote": "sonnet", "judged": "gemini", "deck": "codex gpt-6.1-sol"}
    store.set_model("i1", "deck", "template (fact-check)")
    assert store.get_item("i1").models["deck"] == "template (fact-check)"
    store.set_model("missing", "deck", "x")  # no row: no error, nothing created
    assert store.get_item("missing") is None


def test_a_failed_composer_run_is_not_a_failed_job_run_of_the_week(store):
    """Review M8: a message is Haseeb's own text, in none of the weekly numbers; its failures are not either."""
    store.record_run("compose", False, "say:1.1: the draft could not be sent, gave up")
    store.record_run("compose", True, "sent message-1 (writer-m)")
    store.record_run("tick", False, "SlackError")
    store.record_run("process_dm", False, "x")
    today = datetime.now().astimezone().date()
    assert store.failed_runs_on([today]) == 2  # the tick's and the DM job's, not the composer's
    assert [r[0] for r in store.runs() if not r[1]] == ["process_dm", "tick", "compose"]  # but the log still has it


def test_a_failed_channel_list_refresh_is_not_a_failed_job_run_of_the_week(store):
    """Task 1's review, minor 4: the refresh fails whenever the page cannot be trusted (a forged `### #` line in anybody's Purpose
    makes the sections not add up), and it is tried again every 6 hours. It is not one of the jobs whose failure the weekly
    "Failed job runs" is about, so it is left out of the count, like the composer; the run log still has every one."""
    assert "refresh_channels" in UNCOUNTED_JOBS
    for _ in range(4):  # a bad day: the daily try and three more
        store.record_run("refresh_channels", False, "slack read failed: slack_list_user_channels error")
    store.record_run("refresh_channels", True, "7 channel(s)")
    store.record_run("run_feedback", False, "slack read failed: slack_read_channel server_error")
    today = datetime.now().astimezone().date()
    assert store.failed_runs_on([today]) == 1  # the feedback job's, and nothing else
    assert [r[0] for r in store.runs() if not r[1]].count("refresh_channels") == 4  # but the log still has them


# ---------------------------------------------------------------- claims (C1: one run per post)

def test_claim_cursor_is_won_once(store):
    stale = NOW - timedelta(minutes=20)
    assert store.claim_cursor("claim:a", NOW.isoformat(), stale) is True
    assert store.get_cursor("claim:a") == NOW.isoformat()
    later = NOW + timedelta(minutes=5)
    assert store.claim_cursor("claim:a", later.isoformat(), later - timedelta(minutes=20)) is False  # still held
    assert store.get_cursor("claim:a") == NOW.isoformat()  # the holder's value is untouched
    assert store.claim_cursor("claim:b", later.isoformat(), later - timedelta(minutes=20)) is True  # another key


def test_a_released_claim_can_be_taken_again(store):
    store.claim_cursor("claim:a", NOW.isoformat(), NOW - timedelta(minutes=20))
    assert store.release_cursor("claim:a", NOW.isoformat()) is True
    assert store.get_cursor("claim:a") is None  # gone: released claims leave no row behind
    assert store.release_cursor("claim:a", NOW.isoformat()) is False
    soon = NOW + timedelta(seconds=30)
    assert store.claim_cursor("claim:a", soon.isoformat(), soon - timedelta(minutes=20)) is True


def test_a_stale_claim_is_taken_over_and_the_old_holder_cannot_release_it(store):
    """A claimer that died is given up on after the stale time; if it was only slow, its release leaves the new
    holder's claim alone."""
    store.claim_cursor("claim:a", NOW.isoformat(), NOW - timedelta(minutes=20))
    later = NOW + timedelta(minutes=21)
    assert store.claim_cursor("claim:a", later.isoformat(), later - timedelta(minutes=20)) is True
    assert store.get_cursor("claim:a") == later.isoformat()
    assert store.release_cursor("claim:a", NOW.isoformat()) is False  # not its claim any more
    assert store.get_cursor("claim:a") == later.isoformat()


def test_a_claim_exactly_at_the_stale_time_is_still_held(store):
    store.claim_cursor("claim:a", NOW.isoformat(), NOW - timedelta(minutes=20))
    edge = NOW + timedelta(minutes=20)
    assert store.claim_cursor("claim:a", edge.isoformat(), edge - timedelta(minutes=20)) is False


def test_times_are_compared_as_times_across_offsets(store):
    """09:00+03:00 is 06:00 UTC: the same instant, whichever way each side writes it."""
    kuwait = timezone(timedelta(hours=3))
    store.claim_cursor("claim:a", datetime(2026, 10, 19, 9, 0, tzinfo=kuwait).isoformat(), NOW - timedelta(hours=1))
    stale = datetime(2026, 10, 19, 6, 1, tzinfo=timezone.utc)
    assert store.claim_cursor("claim:a", "x", stale) is True


def test_a_claim_that_is_not_a_time_is_given_up_on(store):
    store.set_cursor("claim:a", "garbage")
    assert store.claim_cursor("claim:a", NOW.isoformat(), NOW - timedelta(minutes=20)) is True


def test_two_connections_cannot_both_win_a_claim(tmp_path):
    first, second = Store(tmp_path / "agent.sqlite"), Store(tmp_path / "agent.sqlite")
    stale = NOW - timedelta(minutes=20)
    assert first.claim_cursor("claim:a", NOW.isoformat(), stale) is True
    soon = NOW + timedelta(minutes=1)
    assert second.claim_cursor("claim:a", soon.isoformat(), soon - timedelta(minutes=20)) is False


def test_count_thread_items_counts_by_prefix_and_thread_whatever_the_status(store):
    from datetime import date, datetime, timezone
    from haseeb_agent.models import Item

    def item(iid, thread, status="delivered", kind="feedback"):
        return Item(id=iid, kind=kind, day=date(2026, 10, 21), status=status, body="x", title="t", dm_ts=None, source_ts=thread,
                    score=None, models={}, attempts=0, edited=False, created_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
                    acted_at=None)
    for it in (item("followback-1.1", "T1"), item("followback-1.2", "T1", "skipped"), item("followback-1.3", "T2"),
               item("insreply-1.4", "T1"), item("feedback-T1", "T1"), item("followback_x-1", "T1"),
               item("followback-1.5", "T1", kind="todo")):
        store.upsert_item(it)
    assert store.count_thread_items("followback-", "T1") == 2 and store.count_thread_items("followback-", "T2") == 1
    assert store.count_thread_items("insreply-", "T1") == 1 and store.count_thread_items("threadfb-", "T1") == 0
    assert store.count_thread_items("follow_ack-", "T1") == 0 and store.count_thread_items("f%", "T1") == 0  # LIKE characters are literal
