import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import pytest

from haseeb_agent.models import StoredMessage
from haseeb_agent.store import Store

NOW = datetime(2026, 10, 17, 22, 5)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agent.sqlite")
    yield s
    s.close()


def msg(ts="100.1", text="hello", user_id="U1", user_name="Ann", kind="other", channel="C1", topic=None, thread_ts=None):
    return StoredMessage(
        channel=channel, ts=ts, thread_ts=thread_ts, user_id=user_id, user_name=user_name,
        text=text, kind=kind, day=date(2026, 10, 16), topic=topic,
    )


def test_wal_backup_once(tmp_path):
    s = Store(tmp_path / "agent.sqlite")
    s.add_pref("keep me")
    backup = s.enable_wal(tmp_path / "bak", NOW)
    assert backup is not None and backup.exists()
    assert backup.name == "agent.sqlite.bak-20261017220500"
    assert s._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # The backup is a usable copy taken before the switch.
    assert sqlite3.connect(backup).execute("SELECT text FROM prefs").fetchone()[0] == "keep me"
    assert s.enable_wal(tmp_path / "bak", NOW + timedelta(minutes=1)) is None
    assert len(list((tmp_path / "bak").iterdir())) == 1
    s.close()


def test_wal_backup_never_overwrites(tmp_path):
    (tmp_path / "bak").mkdir()
    (tmp_path / "bak" / "agent.sqlite.bak-20261017220500").write_text("earlier backup")
    (tmp_path / "bak" / "agent.sqlite.bak-20261017220500-1").write_text("earlier backup 2")
    s = Store(tmp_path / "agent.sqlite")
    backup = s.enable_wal(tmp_path / "bak", NOW)
    assert backup.name == "agent.sqlite.bak-20261017220500-2"
    assert (tmp_path / "bak" / "agent.sqlite.bak-20261017220500").read_text() == "earlier backup"
    assert (tmp_path / "bak" / "agent.sqlite.bak-20261017220500-1").read_text() == "earlier backup 2"
    s.close()


def test_wal_in_memory_is_noop(tmp_path):
    s = Store(":memory:")
    assert s.enable_wal(tmp_path / "bak", NOW) is None
    assert not (tmp_path / "bak").exists()


def test_busy_timeout_set(store):
    assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_upsert_message_idempotent(store):
    assert store.upsert_message(msg()) is True
    assert store.upsert_message(msg()) is False
    assert len(store.messages()) == 1
    assert store.upsert_message(msg(text="changed")) is True
    rows = store.messages()
    assert [m.text for m in rows] == ["changed"]
    # The same ts in another channel is another message.
    assert store.upsert_message(msg(channel="C2")) is True
    assert len(store.messages()) == 2


def test_upsert_message_keeps_search_in_sync(store):
    store.upsert_message(msg(text="pricing page"))
    store.upsert_message(msg(text="roadmap"))
    assert store.search("pricing") == []
    assert [m.text for m in store.search("roadmap")] == ["roadmap"]


def test_search_person_and_kind_filters(store):
    store.upsert_message(msg("1.1", "pricing idea", "U1", "Ann", "insight"))
    store.upsert_message(msg("1.2", "pricing todo", "U1", "Ann", "todo"))
    store.upsert_message(msg("1.3", "pricing from bob", "U2", "Bob", "insight"))
    store.upsert_message(msg("1.4", "other insight", "U1", "Ann", "insight"))
    assert [m.ts for m in store.search("pricing person:U1 kind:insight")] == ["1.1"]
    assert [m.ts for m in store.search("pricing person:bob")] == ["1.3"]
    # Filters only: no FTS query.
    assert {m.ts for m in store.search("person:U1 kind:insight")} == {"1.1", "1.4"}
    assert store.search("") == []  # nothing asked, nothing returned


def test_search_user_input_cannot_inject_fts_syntax(store):
    store.upsert_message(msg("1.1", "pricing idea"))
    for q in ['"', 'pricing*', 'NEAR(pricing idea)', 'pricing OR', 'a" OR "b', "col:pricing", "(", "-pricing", "^x"]:
        store.search(q)  # must not raise
    assert store.search("pri*") == store.search("pri")  # a typed * is no operator: it adds nothing to the prefix
    assert [m.ts for m in store.search("pricing OR nothing")] == []  # OR is a literal word


def test_search_last_word_is_a_prefix(store):
    """While typing, the last word matches as a prefix ("pric" finds "pricing"); the words before it stay whole."""
    store.upsert_message(msg("1.1", "pricing idea"))
    store.upsert_message(msg("1.2", "the price doubles"))
    store.upsert_message(msg("1.3", "roadmap"))
    assert [m.ts for m in store.search("pricing")] == ["1.1"]
    assert {m.ts for m in store.search("pric")} == {"1.1", "1.2"}
    assert [m.ts for m in store.search("pricing ide")] == ["1.1"]
    assert store.search("pric idea") == []  # only the last word is a prefix
    for q in ['"', "(", "_", "__", "pricing _", "*", 'pric"', "x" * 300]:
        store.search(q)  # must not raise


def test_search_person_by_name_prefix(store):
    """person: takes an id (exactly) or the start of a name, in any case; LIKE wildcards in it are plain characters."""
    store.upsert_message(msg("1.1", "pricing idea", "U1", "Sara Ali", "insight"))
    store.upsert_message(msg("1.2", "pricing todo", "U2", "Sarah Khan", "todo"))
    store.upsert_message(msg("1.3", "pricing note", "U3", "Bob", "other"))
    store.upsert_message(msg("1.4", "pricing 100", "U4", "%_x", "other"))
    assert {m.ts for m in store.search("pricing person:Sara")} == {"1.1", "1.2"}
    assert [m.ts for m in store.search('pricing person:"sara ali"')] == ["1.1"]
    assert [m.ts for m in store.search("person:sarah")] == ["1.2"]
    assert [m.ts for m in store.search("person:U3")] == ["1.3"]  # an id matches exactly
    assert store.search("person:U") == []  # ... never as a prefix
    assert [m.ts for m in store.search("person:%")] == ["1.4"]  # % is a literal character, not "anything"
    assert [m.ts for m in store.search("person:%_")] == ["1.4"]
    assert store.search("person:_ara") == []  # _ is not "any one character"
    assert store.search("person:") == [] and store.search("kind:") == []  # an empty filter asks nothing
    assert {m.ts for m in store.search("pricing person:")} == {"1.1", "1.2", "1.3", "1.4"}  # ... and filters nothing


def test_concurrent_reader_during_write(tmp_path):
    path = tmp_path / "agent.sqlite"
    writer = Store(path)
    writer.enable_wal(tmp_path / "bak", NOW)
    writer.upsert_message(msg("1.1", "committed"))
    reader = Store(path)
    writer._db.execute("BEGIN IMMEDIATE")
    writer._db.execute(
        "INSERT INTO messages (channel, ts, user_id, user_name, text, kind, day) "
        "VALUES ('C1', '9.9', 'U1', 'Ann', 'in flight', 'other', '2026-10-16')"
    )
    started = time.monotonic()
    rows = reader.messages()
    assert time.monotonic() - started < 5
    assert [m.ts for m in rows] == ["1.1"]  # only the committed row
    writer._db.rollback()
    reader.close()
    writer.close()


def test_person_color_stable(store):
    for i in range(14):
        store.upsert_person(f"U{i}", f"Name{i}")
    people = store.people()
    # Ordered by name, then user_id; the color follows the order of first insert.
    assert [p.name for p in people] == sorted(f"Name{i}" for i in range(14))
    assert {p.user_id: p.color for p in people} == {f"U{i}": i % 12 for i in range(14)}
    before = {p.user_id: p.color for p in people}
    store.upsert_person("U3", "Renamed")
    after = {p.user_id: p.color for p in store.people()}
    assert after == before
    assert next(p for p in store.people() if p.user_id == "U3").name == "Renamed"


def test_people_ordered_by_name_then_user_id(store):
    store.upsert_person("U9", "Zed")
    store.upsert_person("U2", "Ann")
    store.upsert_person("U1", "Ann")
    assert [(p.name, p.user_id) for p in store.people()] == [("Ann", "U1"), ("Ann", "U2"), ("Zed", "U9")]


def test_page_command_queue_order(store):
    a = store.enqueue_page_command("i1", "post", None, "h1", NOW)
    b = store.enqueue_page_command("i2", "edit", "new text", "h2", NOW)
    c = store.enqueue_page_command("i3", "skip", None, "h3", NOW)
    assert [p.id for p in store.next_page_commands()] == [a, b, c]
    store.set_page_command(b, "running")
    assert [p.id for p in store.next_page_commands()] == [a, c]
    assert [p.id for p in store.next_page_commands(limit=1)] == [a]
    store.set_page_command(a, "done", "posted")
    got = store.page_command(a)
    assert (got.state, got.result, got.verb, got.item_id, got.shown_hash, got.created_at) == ("done", "posted", "post", "i1", "h1", NOW)
    assert store.page_command(b).arg == "new text"
    assert store.page_command(999) is None


def test_page_command_rejects_unknown_values(store):
    with pytest.raises(ValueError):
        store.enqueue_page_command("i1", "nuke", None, "h", NOW)
    cid = store.enqueue_page_command("i1", "post", None, "h", NOW)
    with pytest.raises(ValueError):
        store.set_page_command(cid, "weird")


def test_label_overrides_kind(store):
    store.upsert_message(msg("1.1", "pricing idea", kind="other"))
    store.set_label("C1", "1.1", "insight")
    assert [m.ts for m in store.messages(kind="insight")] == ["1.1"]
    assert store.messages(kind="other") == []
    assert store.messages()[0].kind == "insight"
    assert [m.ts for m in store.search("pricing kind:insight")] == ["1.1"]
    # A later classifier re-run does not undo the label.
    store.upsert_message(msg("1.1", "pricing idea", kind="todo"))
    assert store.messages()[0].kind == "insight"
    with pytest.raises(ValueError):
        store.set_label("C1", "1.1", "bogus")


def test_messages_filters(store):
    store.upsert_message(msg("1.1", "a", "U1", "Ann", "insight", topic="pricing"))
    store.upsert_message(msg("1.2", "b", "U2", "Bob", "insight", topic="hiring"))
    m = StoredMessage("C1", "2.1", None, "U1", "Ann", "c", "todo", date(2026, 10, 17), "pricing")
    store.upsert_message(m)
    assert [x.ts for x in store.messages(day=date(2026, 10, 17))] == ["2.1"]
    assert {x.ts for x in store.messages(user_id="U1")} == {"1.1", "2.1"}
    assert {x.ts for x in store.messages(topic="pricing")} == {"1.1", "2.1"}
    assert [x.ts for x in store.messages(limit=1)] == ["2.1"]  # newest first


def test_events_roundtrip(store):
    t0 = NOW
    e1 = store.add_event("tick", "fetch", model=None, started=t0, ended=None, ok=None, kind=None)
    e2 = store.add_event("tick", "draft", model="sonnet", started=t0, ended=t0 + timedelta(seconds=2), ok=True, kind="todo")
    assert e2 > e1
    store.finish_event(e1, t0 + timedelta(seconds=1), False, "timeout")
    evs = store.events_since(0)
    assert [e.id for e in evs] == [e1, e2]
    assert (evs[0].ok, evs[0].kind, evs[0].ended) == (False, "timeout", t0 + timedelta(seconds=1))
    assert evs[1].model == "sonnet" and evs[1].ok is True
    assert [e.id for e in store.events_since(e1)] == [e2]
    assert len(store.events_since(0, limit=1)) == 1


def test_finish_event_can_set_the_model_late(store):
    eid = store.add_event("j", "s", model="first", started=NOW, ended=None, ok=None, kind=None)
    store.finish_event(eid, NOW, True, None)
    assert store.events_since(0)[0].model == "first"  # no model given: unchanged
    store.finish_event(eid, NOW, True, None, model="second")
    assert store.events_since(0)[0].model == "second"


def test_get_message_is_the_classifiers_row(store):
    assert store.get_message("C1", "100.1") is None
    store.upsert_message(msg(kind="insight", topic="pricing"))
    store.set_label("C1", "100.1", "other")  # a person's label does not change the stored row
    got = store.get_message("C1", "100.1")
    assert (got.kind, got.topic, got.text) == ("insight", "pricing", "hello")
    assert store.get_message("C2", "100.1") is None


def test_get_message_effective_is_the_row_as_the_pages_show_it(store):
    """M5: the same row, but with Haseeb's own label over the classifier's kind (as `messages` and `team_posts` do)."""
    assert store.get_message_effective("C1", "100.1") is None
    store.upsert_message(msg(kind="huddle_notes", topic="pricing"))
    assert store.get_message_effective("C1", "100.1").kind == "huddle_notes"  # no label: the classifier's
    store.set_label("C1", "100.1", "other")
    got = store.get_message_effective("C1", "100.1")
    assert (got.kind, got.topic, got.text) == ("other", "pricing", "hello")
    assert store.get_message("C1", "100.1").kind == "huddle_notes"  # the classifier's row is still its own
    assert store.get_message_effective("C2", "100.1") is None


def test_update_cursor_sets_from_the_old_value_and_returns_the_new_one(store):
    seen = []
    assert store.update_cursor("q", lambda old: (seen.append(old), "1")[1]) == "1"
    assert store.update_cursor("q", lambda old: (seen.append(old), old + "2")[1]) == "12"
    assert seen == [None, "1"] and store.get_cursor("q") == "12"


def test_update_cursor_changes_nothing_when_the_update_raises(store):
    store.set_cursor("q", "keep")

    def boom(old):
        raise RuntimeError("no")
    with pytest.raises(RuntimeError):
        store.update_cursor("q", boom)
    assert store.get_cursor("q") == "keep"
    store.set_cursor("q", "again")  # the connection is usable: no transaction was left open
    assert store.get_cursor("q") == "again"


def test_update_cursor_waits_for_another_writer_instead_of_overwriting_it(tmp_path):
    """D15: the tick's capture and the nightly refresh both edit the refresh queue, in different processes. An update
    that read the old value before the other writer committed would write it back and lose that change."""
    path = tmp_path / "agent.sqlite"
    first = Store(path)
    first._db.execute("BEGIN IMMEDIATE")  # the other process is in the middle of its write
    first._db.execute("INSERT INTO cursors (key, value) VALUES ('q', 'a')")
    done = []

    def second():
        other = Store(path)  # a connection belongs to the thread that made it
        try:
            done.append(other.update_cursor("q", lambda old: (old or "") + "b"))
        finally:
            other.close()

    worker = threading.Thread(target=second)
    worker.start()
    time.sleep(0.3)
    assert done == []  # still waiting for the write lock, not working from a stale read
    first._db.commit()
    worker.join(10)
    assert done == ["ab"] and first.get_cursor("q") == "ab"
    first.close()


# -- the final review (M7): open events are found by a partial index, and events older than 30 days are pruned ----

def test_open_events_have_a_partial_index_that_the_lookup_uses(store):
    sql = store._db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='events_open'").fetchone()
    assert sql is not None and "WHERE ended IS NULL" in sql[0]
    plan = " ".join(str(r[-1]) for r in store._db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events WHERE ended IS NULL ORDER BY id DESC LIMIT 1"))
    assert "events_open" in plan


def test_prune_events_drops_only_what_started_before_the_cutoff(store):
    from datetime import timezone
    now = datetime(2026, 10, 19, 21, 0).astimezone()
    store.add_event("tick", "a", model=None, started=now - timedelta(days=31), ended=now - timedelta(days=31),
                    ok=True, kind=None)
    store.add_event("tick", "b", model=None, started=now - timedelta(days=40), ended=None, ok=None, kind=None)  # open
    recent = store.add_event("tick", "c", model=None, started=now - timedelta(days=29), ended=None, ok=None, kind=None)
    # in UTC, 30 days and 1 hour back: the offset is compared as a time, not as text
    store.add_event("tick", "d", model=None, started=(now - timedelta(days=30, hours=1)).astimezone(timezone.utc),
                    ended=None, ok=None, kind=None)
    assert store.prune_events(now - timedelta(days=30)) == 3
    assert [e.id for e in store.events_since(0)] == [recent]


# -- the run controls' reads: the team posts list and the status of feedback items ---------------------------------

def test_team_posts_are_the_channels_top_level_posts_of_others_since_the_day(store):
    day = lambda n: date(2026, 10, n)  # noqa: E731
    def put(ts, d, **kw):
        m = msg(ts, **kw)
        m.day = d
        store.upsert_message(m)
    put("300.1", day(16), text="newest")
    put("200.1", day(15), text="parent of a thread", thread_ts="200.1")
    put("250.1", day(15), text="a reply", thread_ts="200.1")
    put("260.1", day(15), text="mine", user_id="UME")
    put("270.1", day(15), text="notes", kind="huddle_notes")
    put("280.1", day(15), text="elsewhere", channel="C2")
    put("100.1", day(9), text="too old")
    got = store.team_posts("C1", day(10), "UME")
    assert [m.text for m in got] == ["newest", "parent of a thread"]  # newest first
    assert [m.text for m in store.team_posts("C1", day(10), "UME", limit=1)] == ["newest"]
    assert [m.text for m in store.team_posts("C1", day(1), "UME")][-1] == "too old"
    store.set_label("C1", "300.1", "huddle_notes")  # a local label counts, like everywhere else
    assert [m.text for m in store.team_posts("C1", day(10), "UME")] == ["parent of a thread"]


def test_item_statuses_returns_the_ones_that_exist(store):
    from haseeb_agent.models import Item
    for i, status in enumerate(("delivered", "posted")):
        store.upsert_item(Item(id=f"feedback-{i}", kind="feedback", day=date(2026, 10, 16), status=status, body="b",
                               title="t", dm_ts=None, source_ts=None, score=None, models={}, attempts=0, edited=False,
                               created_at=NOW, acted_at=None))
    assert store.item_statuses(["feedback-0", "feedback-1", "feedback-9"]) == {"feedback-0": "delivered",
                                                                              "feedback-1": "posted"}
    assert store.item_statuses([]) == {}
    many = [f"feedback-x{i}" for i in range(1200)]  # more than one SQL batch
    assert store.item_statuses([*many, "feedback-1"]) == {"feedback-1": "posted"}


def test_fail_queued_page_commands_ends_only_the_queued_ones_in_one_update(store):
    a = store.enqueue_page_command("i1", "post", None, "0" * 64, NOW)
    b = store.enqueue_page_command("i2", "skip", None, "0" * 64, NOW)
    c = store.enqueue_page_command("i3", "post", None, "0" * 64, NOW)
    store.claim_page_command(b)  # running: a tick has it
    store.set_page_command(c, "done", "Posted")
    assert store.fail_queued_page_commands([a, b, c, 999], "expired") == 1
    assert [(x.state, x.result) for x in (store.page_command(a), store.page_command(b), store.page_command(c))] == [
        ("failed", "expired"), ("running", None), ("done", "Posted")]
    assert store.fail_queued_page_commands([], "x") == 0
    many = [store.enqueue_page_command("i", "skip", None, "0" * 64, NOW) for _ in range(1200)]  # past one chunk
    assert store.fail_queued_page_commands(many, "expired") == 1200
