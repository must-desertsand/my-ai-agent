"""history.classify (rules) and history.capture (what the jobs store from their channel reads)."""
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import history
from haseeb_agent.commands import process_dm
from haseeb_agent.config import load_settings
from haseeb_agent.jobs.feedback import run_feedback
from haseeb_agent.jobs.todo import run_todo
from haseeb_agent.models import FileRef, Item, Msg
from haseeb_agent.slack_format import parse_thread
from haseeb_agent.store import Store
from tests.fakes import FakeSlack
from tests.test_feedback import CH, LONG, NOW, FakeLLM, post, ts_ago

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"
KW = ZoneInfo("Asia/Kuwait")


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agent.sqlite")
    yield s
    s.close()


def msg(ts="1792200000.000001", text="hello", user="U1", name="Ann", thread_ts=None, files=(), bot=False):
    return Msg(ts=ts, user_id=user, user_name=name, text=text, thread_ts=thread_ts, reply_count=0,
               latest_reply=None, files=list(files), is_bot=bot)


def test_classify_rules(settings):
    # Rule 1, from the real huddle thread fixture: Slackbot's "AI huddle notes are ready" reply.
    parent, replies = parse_thread((FIX / "thread.txt").read_text(encoding="utf-8"))
    notes = next(r for r in replies if "AI huddle notes are ready" in r.text)
    assert parent.is_bot and notes.is_bot
    assert history.classify(notes, parent, settings) == "huddle_notes"
    assert history.classify(parent, None, settings) == "other"  # "A huddle started"
    # A person quoting the phrase is not the notes message.
    assert history.classify(msg(text="AI huddle notes are ready", thread_ts=parent.ts), parent, settings) == "other"
    # Rule 2, also from the fixture: the teammates' "Next Todo:" replies.
    todos = [r for r in replies if r.text.startswith("Next Todo")]
    assert todos and all(history.classify(r, parent, settings) == "todo" for r in todos)
    for text in ("next todo: x", "  🚀 Next todo\n1. a", ":memo: NEXT TODO", "*Next Todo*"):
        assert history.classify(msg(text=text), None, settings) == "todo", text
    assert history.classify(msg(text="My next todo is x"), None, settings) == "other"
    # Rule 3: a top-level PDF, or a first line "Daily insight" / "Insight".
    pdf = FileRef("F1", "deck.pdf", "application/pdf")
    assert history.classify(msg(text="see attached", files=[pdf]), None, settings) == "insight"
    assert history.classify(msg(text="x", files=[FileRef("F2", "r.PDF", "application/octet-stream")]),
                            None, settings) == "insight"
    assert history.classify(msg(text="Daily insight - Gemini 4\nbody"), None, settings) == "insight"
    assert history.classify(msg(text="\n📌 Insight: pricing"), None, settings) == "insight"
    assert history.classify(msg(text="Insights are fun"), None, settings) == "other"
    assert history.classify(msg(text="intro\nInsight: no"), None, settings) == "other"  # first line only
    # An insight with replies is still top-level (thread_ts == ts).
    assert history.classify(msg(ts="5.1", text="Insight: x", thread_ts="5.1"), None, settings) == "insight"
    assert history.classify(msg(ts="6.1", text="Insight: x", thread_ts="5.1"), None, settings) == "other"
    # Rule 4: a reply to an insight is feedback; a reply to anything else is not.
    top = msg(ts="5.1", text="Insight: x", thread_ts="5.1")
    assert history.classify(msg(ts="5.2", text="nice", thread_ts="5.1"), top, settings) == "feedback"
    assert history.classify(msg(ts="5.2", text="nice", thread_ts="5.1"), msg(ts="5.1", text="chat"), settings) == "other"
    # Rule order: "Next todo" beats feedback.
    assert history.classify(msg(ts="5.3", text="Next todo: a", thread_ts="5.1"), top, settings) == "todo"
    assert history.classify(msg(text="just chatting"), None, settings) == "other"


@pytest.mark.parametrize("text", [":+" * 200 + ":", "|:---:" * 200 + "|", "!" * 4000, ":a:" * 1500, " :" * 2000])
def test_classify_is_linear_on_hostile_text(settings, text):
    import time
    for t in (text, text + "x", text + " next todo"):
        start = time.perf_counter()
        history.classify(msg(text=t), None, settings)
        assert time.perf_counter() - start < 0.1


def test_capture_writes_a_person_only_when_new_or_renamed(store, settings, monkeypatch):
    calls = []
    real = store.upsert_person
    monkeypatch.setattr(store, "upsert_person", lambda *a: (calls.append(a), real(*a))[1])
    a = msg("1792200000.000001", "first")
    history.capture(store, settings, [a], source_channel=settings.channel_id)
    history.capture(store, settings, [a], source_channel=settings.channel_id)
    assert calls == [("U1", "Ann")]
    history.capture(store, settings, [msg(a.ts, "first", name="Ann B")], source_channel=settings.channel_id)
    assert calls == [("U1", "Ann"), ("U1", "Ann B")]


def test_capture_stores_channel_messages(store, settings):
    a, b = msg("1792200000.000001", "first"), msg("1792200100.000001", "Insight: second", user="U2", name="Bo")
    assert history.capture(store, settings, [a, b], source_channel=settings.channel_id) == 2
    rows = {m.ts: m for m in store.messages()}
    assert set(rows) == {a.ts, b.ts}
    assert rows[a.ts].channel == settings.channel_id and rows[a.ts].topic is None
    assert rows[b.ts].kind == "insight" and rows[a.ts].kind == "other"
    expected = datetime.fromtimestamp(float(a.ts), KW).date()
    assert rows[a.ts].day == expected and isinstance(expected, date)
    assert {p.user_id: p.name for p in store.people()} == {"U1": "Ann", "U2": "Bo"}


def test_capture_day_is_local_to_settings_tz(store, settings):
    # 2026-10-17 21:30 UTC is 00:30 on the 18th in Kuwait (UTC+3).
    ts = f"{int(datetime(2026, 10, 17, 21, 30, tzinfo=ZoneInfo('UTC')).timestamp())}.000001"
    history.capture(store, settings, [msg(ts)], source_channel=settings.channel_id)
    assert store.messages()[0].day == date(2026, 10, 18)


def test_capture_is_idempotent_and_keeps_later_work(store, settings):
    a = msg("1792200000.000001", "first")
    assert history.capture(store, settings, [a], source_channel=settings.channel_id) == 1
    assert history.capture(store, settings, [a], source_channel=settings.channel_id) == 0
    # A topic set later, and a kind relabelled from "other", survive the next tick's re-read.
    store._db.execute("UPDATE messages SET topic='pricing', kind='insight'")
    store._db.commit()
    assert history.capture(store, settings, [a], source_channel=settings.channel_id) == 0
    row = store.messages()[0]
    assert (row.topic, row.kind) == ("pricing", "insight")
    # An edit is stored and counted.
    assert history.capture(store, settings, [msg(a.ts, "first, edited")], source_channel=settings.channel_id) == 1
    assert store.messages()[0].text == "first, edited"


def test_capture_thread_stores_parent_and_replies(store, settings):
    parent = msg("1792200000.000001", "Daily insight: x", thread_ts="1792200000.000001")
    replies = [msg("1792200050.000001", "good", user="U2", name="Bo", thread_ts=parent.ts),
               msg("1792200060.000001", "Next todo: z", user="U3", name="Cy", thread_ts=parent.ts)]
    assert history.capture(store, settings, replies, parent=parent, source_channel=settings.channel_id) == 3
    kinds = {m.ts: m.kind for m in store.messages()}
    assert kinds == {parent.ts: "insight", replies[0].ts: "feedback", replies[1].ts: "todo"}


def test_capture_skips_a_bad_message_and_never_raises(store, settings, caplog):
    bad = msg("not-a-ts", "secret post text")
    assert history.capture(store, settings, [bad, msg("1792200000.000001", "ok")], source_channel=settings.channel_id) == 1
    assert "secret post text" not in caplog.text
    store.close()  # a dead database must not reach the job either
    assert history.capture(store, settings, [msg("1792200200.000001", "later")], source_channel=settings.channel_id) == 0
    assert "secret post text" not in caplog.text and "later" not in caplog.text


def test_capture_from_feedback_read(store, settings):
    slack = FakeSlack()
    for i, text in enumerate((LONG, "short", "Insight: a third")):
        post(slack, ts_ago(900 - i * 100), text)
    run_feedback(slack, FakeLLM(), store, settings, NOW)
    assert len(store.messages()) == 3
    assert {m.channel for m in store.messages()} == {CH}


def test_capture_from_todo_huddle_reads(store, settings):
    from tests.test_todo import CH as TCH, HUDDLE_TS, NOTES_TS, notes_reply, seed_huddle
    slack = FakeSlack()
    seed_huddle(slack, [notes_reply()])
    from tests import test_todo
    run_todo(slack, test_todo.FakeLLM(), store, settings, test_todo.at(15, 0))
    assert [(e.job, e.step) for e in store.events_since(0)][:1] == [("todo", "find_huddle")]
    stored = {m.ts: m for m in store.messages()}
    assert HUDDLE_TS in stored and NOTES_TS in stored  # the page message (the huddle) and the thread reply
    assert stored[NOTES_TS].kind == "huddle_notes" and {m.channel for m in stored.values()} == {TCH}


def test_capture_never_stores_dm(store, settings):
    slack = FakeSlack()
    t = ts_ago(300)
    slack.add_message(settings.self_dm_id, t, "DM draft", user_id=settings.haseeb_id, reply_count=1,
                      latest_reply=ts_ago(100))
    store.upsert_item(Item(
        id="feedback-x", kind="feedback", day=NOW.date(), status="delivered", body="b", title="t", dm_ts=t,
        source_ts=None, score=None, models={}, attempts=0, edited=False, created_at=NOW, acted_at=None))
    slack.add_thread(settings.self_dm_id, slack.pages[settings.self_dm_id][0], [])
    process_dm(slack, store, settings, {}, NOW)
    assert store.messages() == []
    assert store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    # ... but the DM read was recorded as a text-free step event.
    steps = [(e.job, e.step) for e in store.events_since(0)]
    assert ("process_dm", "read_dm") in steps


# -- D15: capture notes the threads that got new replies, for the nightly refresh ------------------------------------


def top(ts, latest, count=2, text="Daily insight: pricing", bot=False, user="U1", name="Ann"):
    """A top-level post as a channel page shows it: how many replies, and when the newest came."""
    return Msg(ts=ts, user_id=user, user_name=name, text=text, thread_ts=ts if count else None, reply_count=count,
               latest_reply=latest, is_bot=bot)


def queued(store):
    return json.loads(store.get_cursor(history.REFRESH_QUEUE) or "[]")


def test_capture_queues_a_thread_when_its_latest_reply_advances(store, settings):
    a = top("1792200000.000001", "1792200100")
    history.capture(store, settings, [a], source_channel=settings.channel_id)
    assert queued(store) == [a.ts]
    assert store.get_cursor(f"{history.REFRESH_SEEN}{a.ts}") == "1792200100"
    store.set_cursor(history.REFRESH_QUEUE, "[]")  # the nightly refresh read it
    history.capture(store, settings, [a], source_channel=settings.channel_id)  # the same page again: nothing new
    history.capture(store, settings, [top(a.ts, "1792200100.900000")], source_channel=settings.channel_id)  # the same second
    history.capture(store, settings, [top(a.ts, "1792200050")], source_channel=settings.channel_id)  # an older time never queues
    assert queued(store) == []
    history.capture(store, settings, [top(a.ts, "1792200160", count=3)], source_channel=settings.channel_id)  # a new reply
    assert queued(store) == [a.ts]
    assert store.get_cursor(f"{history.REFRESH_SEEN}{a.ts}") == "1792200160"


def test_capture_queues_only_insight_and_huddle_threads_with_replies(store, settings):
    insight = top("1792200000.000001", "1792200100")
    huddle = top("1792200200.000001", "1792200300", text="A huddle started", bot=True, user="USLACKBOT",
                 name="Slackbot")
    notes = top("1792200400.000001", "1792200500", text="AI huddle notes are ready", bot=True, user="USLACKBOT",
                name="Slackbot")
    pdf = Msg(ts="1792200450.000001", user_id="U1", user_name="Ann", text="see attached", thread_ts="1792200450.000001",
              reply_count=1, latest_reply="1792200460", files=[FileRef("F1", "deck.pdf", "application/pdf")])
    chat = top("1792200600.000001", "1792200700", text="just chatting")
    person_huddle = top("1792200650.000001", "1792200660", text="A huddle started")  # not the bot
    quiet = top("1792200800.000001", None, count=0)
    no_time = top("1792200900.000001", None, count=2)
    reply = msg("1792201000.000001", "nice", thread_ts=insight.ts)
    history.capture(store, settings, [insight, huddle, notes, pdf, chat, person_huddle, quiet, no_time, reply], source_channel=settings.channel_id)
    assert queued(store) == [insight.ts, huddle.ts, notes.ts, pdf.ts]
    assert len(store.messages()) == 9  # all of them are stored, as before


def test_the_refresh_queue_has_no_duplicates_and_keeps_the_newest_200(store, settings):
    posts = [top(f"{1792200000 + i * 10}.000001", str(1792200005 + i * 10)) for i in range(205)]
    history.capture(store, settings, posts, source_channel=settings.channel_id)
    assert queued(store) == [p.ts for p in posts[5:]]  # the five oldest are dropped
    before = queued(store)
    history.capture(store, settings, [top(posts[100].ts, "1792299999")], source_channel=settings.channel_id)  # a queued thread gets a newer reply
    assert queued(store) == before  # not twice, and it keeps its place


def test_a_garbled_queue_cursor_is_replaced(store, settings):
    for garbage in ("{not json", '{"a": 1}', '"text"', "[1, null, {}]"):
        store.set_cursor(history.REFRESH_QUEUE, garbage)
        store.set_cursor(f"{history.REFRESH_SEEN}1792200000.000001", "0")
        history.capture(store, settings, [top("1792200000.000001", "1792200100")], source_channel=settings.channel_id)
        assert queued(store) == ["1792200000.000001"], garbage


def test_a_thread_read_marks_it_seen_so_the_next_page_does_not_queue_it(store, settings):
    parent = top("1792200000.000001", "1792200100")
    reply = msg("1792200100.000001", "good", user="U2", name="Bo", thread_ts=parent.ts)
    history.capture(store, settings, [reply], parent=parent, source_channel=settings.channel_id)
    assert queued(store) == [] and store.get_cursor(f"{history.REFRESH_SEEN}{parent.ts}") == "1792200100"
    history.capture(store, settings, [parent], source_channel=settings.channel_id)  # a page showing the same newest reply
    assert queued(store) == []
    history.capture(store, settings, [top(parent.ts, "1792200200")], source_channel=settings.channel_id)
    assert queued(store) == [parent.ts]
    # a thread read never moves the mark back
    history.capture(store, settings, [reply], parent=top(parent.ts, "1792200100"), source_channel=settings.channel_id)
    assert store.get_cursor(f"{history.REFRESH_SEEN}{parent.ts}") == "1792200200"


def test_a_reply_of_a_thread_read_never_queues_anything(store, settings):
    parent = top("1792200000.000001", "1792200100")
    odd = top("1792200100.000001", "1792200900", text="Insight: a reply that claims replies", count=3)
    odd.thread_ts = parent.ts
    history.capture(store, settings, [odd], parent=parent, source_channel=settings.channel_id)
    assert queued(store) == []


def test_a_caller_that_reads_the_threads_itself_can_skip_the_queue(store, settings):
    a = top("1792200000.000001", "1792200100")
    history.capture(store, settings, [a], queue_refresh=False, source_channel=settings.channel_id)
    assert queued(store) == [] and store.get_cursor(f"{history.REFRESH_SEEN}{a.ts}") is None
    assert [m.ts for m in store.messages()] == [a.ts]


def test_capture_makes_no_slack_call_in_a_live_job(store, settings):
    slack = FakeSlack()
    t = ts_ago(300)
    slack.add_message(CH, t, "Insight: short post", reply_count=2, latest_reply=ts_ago(100))  # too short for a draft
    calls = []
    for name in ("read_channel", "read_thread", "send", "react", "read_file", "upload_pdf"):
        real = getattr(slack, name)
        setattr(slack, name, lambda *a, _real=real, _name=name, **k: (calls.append(_name), _real(*a, **k))[1])
    run_feedback(slack, FakeLLM(), store, settings, NOW)
    assert calls == ["read_channel"]  # the job's own read, and nothing for the thread
    assert queued(store) == [t]


def test_capture_survives_a_failure_while_queueing(store, settings, monkeypatch, caplog):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked: secret post text")
    monkeypatch.setattr(store, "update_cursor", locked)
    a = top("1792200000.000001", "1792200100", text="Insight: secret post text")
    assert history.capture(store, settings, [a, msg("1792200200.000001", "second")], source_channel=settings.channel_id) == 2  # both stored as before
    assert [m.ts for m in store.messages()] == ["1792200200.000001", a.ts]
    assert queued(store) == [] and "secret post text" not in caplog.text
    assert "could not note" in caplog.text


def test_a_message_that_could_not_be_stored_is_not_queued(store, settings):
    history.capture(store, settings, [top("not-a-ts", "1792200100")], source_channel=settings.channel_id)
    assert queued(store) == [] and store.get_cursor(f"{history.REFRESH_SEEN}not-a-ts") is None


def test_a_thread_read_with_a_failed_reply_is_not_marked_read(store, settings):
    parent = top("1792200000.000001", "1792200100")
    bad = msg("not-a-ts", "reply that cannot be stored", thread_ts=parent.ts)
    good = msg("1792200100.000001", "good", thread_ts=parent.ts)
    assert history.capture(store, settings, [bad, good], parent=parent, source_channel=settings.channel_id) == 2  # the parent and the good reply
    assert store.get_cursor(f"{history.REFRESH_SEEN}{parent.ts}") is None  # a page showing it is still news


# -- the final review (M6): capture is told which channel was read, and stores only the program channel's ---------

@pytest.mark.parametrize("source", ["self_dm_id", "self_dm_channel", "other"])
def test_capture_refuses_a_read_of_any_other_channel(store, settings, source):
    channel = "C0SOMEOTHER" if source == "other" else getattr(settings, source)
    parent = msg("1792200000.000001", "DM draft")
    assert history.capture(store, settings, [msg("1792200100.000001", "secret")], source_channel=channel) == 0
    assert history.capture(store, settings, [msg("1792200200.000001", "r")], parent=parent,
                           source_channel=channel) == 0
    assert store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert history.capture(store, settings, [msg()], source_channel=settings.channel_id) == 1


def test_capture_needs_its_source_channel(store, settings):
    with pytest.raises(TypeError):
        history.capture(store, settings, [msg()])  # no default: every caller says which channel it read
