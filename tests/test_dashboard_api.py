"""Task 7: the dashboard JSON API. TestClient and tmp_path stores/logs only: no server, no real sips, no real logs."""
import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from haseeb_agent import channels, controls
from haseeb_agent.commands import LEADERSHIP_ONLY
from haseeb_agent.config import load_settings
from haseeb_agent.dashboard.app import create_app
from haseeb_agent.models import Item, StoredMessage
from haseeb_agent.page_commands import body_hash
from haseeb_agent.store import Store
from tests.test_dashboard_security import BASE, KEY, do_login

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=KW)  # a Monday
HASEEB = "U08CZCGJW3E"
CH = "C0CHANNEL"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return replace(load_settings(ROOT / "tests" / "fixtures" / "config.toml", env), db_path=str(tmp_path / "agent.sqlite"),
                   channel_id=CH, haseeb_id=HASEEB)


@pytest.fixture
def clock():
    return Clock(NOW.timestamp())


@pytest.fixture
def store(settings):
    s = Store(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def kicks():
    return []


@pytest.fixture
def sips_calls():
    return []


@pytest.fixture
def spawned():
    """Every job the dashboard started, as the args after `python -m haseeb_agent.cli`. No test spawns anything real."""
    return []


@pytest.fixture
def make_app(settings, clock, kicks, tmp_path, sips_calls, spawned):
    def make(**kw):
        def sips(argv):
            sips_calls.append(argv)
            Path(argv[argv.index("--out") + 1]).write_bytes(b"\x89PNG-thumb")
            return 0
        opts = dict(clock=clock, login_dir=tmp_path / "login", logs_dir=tmp_path / "logs",
                    decks_dir=tmp_path / "decks", sips=sips, spawn=lambda args: spawned.append(list(args)),
                    started_at=NOW.timestamp() - 86400)  # a dashboard that has been up a day: its checks are newer
        opts.update(kw)
        return create_app(settings, lambda: Store(settings.db_path), KEY, lambda: kicks.append(1), **opts)
    return make


async def _no_sleep(_s):
    return None


@pytest.fixture
def app(make_app):
    return make_app(stream_poll_s=0, stream_max_polls=1, stream_sleep=_no_sleep)  # a finite stream, whoever reads it


@pytest.fixture
def session(app, tmp_path, clock):
    return do_login(app, tmp_path / "login", clock)


@pytest.fixture
def c(session):
    client, token = session
    client.headers.update({"X-Session-Token": token, "Origin": BASE})
    return client


def msg(ts, text="hi", user_id="U1", user_name="Ann", kind="other", thread_ts=None, day=date(2026, 10, 19),
        topic=None, channel=CH):
    return StoredMessage(channel=channel, ts=ts, thread_ts=thread_ts, user_id=user_id, user_name=user_name,
                         text=text, kind=kind, day=day, topic=topic)


def insight_item(iid="insight-2026-10-19-1", status="delivered", day=date(2026, 10, 19), dm_ts="1.1"):
    body = json.dumps({"v": 1, "insight": {"title": "T", "what_happened": "W", "why_it_matters": "Y",
                                           "silicon_valley": "S"}})
    return Item(id=iid, kind="insight", day=day, status=status, body=body, title="T title", dm_ts=dm_ts,
                source_ts=None, score=90.0, models={}, attempts=0, edited=False, created_at=NOW, acted_at=None)


def todo_item(iid="todo-2026-10-19", status="delivered"):
    return replace(insight_item(iid, status), kind="todo", body="Next todo: ship it", title="Todo")


def message_item(iid="message-1792400000.000001", status="delivered", body="Standup moved to 3 pm.", dm_ts="5.5"):
    """A polished message of the composer: no score, no source post, and its own words in cursors."""
    return replace(insight_item(iid, status, dm_ts=dm_ts), kind="message", body=body, title="Message to the channel",
                   score=None)


# ---------------------------------------------------------------- auth

ENDPOINTS = ["/api/live", "/api/inbox", "/api/history", "/api/people", "/api/weeks", "/api/decks", "/api/logs",
             "/api/score", "/api/command/1", "/api/decks/x.pdf", "/api/decks/x/thumb.png", "/api/stream",
             "/api/jobs", "/api/teamposts", "/api/channels"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_api_needs_session_and_token(app, session, path):
    anon = TestClient(app, base_url=BASE, follow_redirects=False)
    assert anon.get(path).status_code == 401
    client, token = session
    assert client.get(path).status_code == 403  # cookie but no token
    assert client.get(path, headers={"X-Session-Token": "0" * 64}).status_code == 403  # wrong token
    assert client.get(path, headers={"X-Session-Token": token}).status_code != 403


def test_post_needs_token_and_origin(app, session):
    client, token = session
    body = {"item_id": "x", "verb": "post", "shown_hash": "0" * 64}
    assert client.post("/api/action", json=body, headers={"Origin": BASE}).status_code == 403
    assert client.post("/api/action", json=body, headers={"X-Session-Token": token}).status_code == 403
    assert client.post("/api/action", json=body, headers={"Origin": BASE, "X-Session-Token": "bad"}).status_code == 403


@pytest.mark.parametrize("path,body", [("/api/run", {"job": "weekly"}), ("/api/pause", {"paused": True}),
                                       ("/api/compose", {"text": "standup at 3"}), ("/api/channels/refresh", {})])
def test_run_controls_post_need_session_token_origin_and_fetch_metadata(app, session, spawned, kicks, path, body):
    """The run controls sit behind the same middleware as every route: nothing is spawned, kicked or paused by a
    request that fails it."""
    client, token = session
    anon = TestClient(app, base_url=BASE, follow_redirects=False)
    assert anon.post(path, json=body, headers={"Origin": BASE}).status_code == 401  # no session
    assert client.post(path, json=body, headers={"Origin": BASE}).status_code == 403  # cookie, no token
    assert client.post(path, json=body, headers={"X-Session-Token": token}).status_code == 403  # no Origin
    assert client.post(path, json=body, headers={"Origin": "http://evil.example", "X-Session-Token": token}
                       ).status_code == 403  # another origin
    assert client.post(path, json=body, headers={"Origin": BASE, "X-Session-Token": token,
                                                 "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(path, json=body, headers={"Origin": BASE, "X-Session-Token": "0" * 64}).status_code == 403
    assert spawned == [] and kicks == []
    assert client.get(path, headers={"X-Session-Token": token}).status_code in (404, 405)  # actions are POST only
    assert client.put(path, json=body, headers={"Origin": BASE, "X-Session-Token": token}).status_code in (404, 405)


def test_unknown_api_path_is_json_404(c):
    r = c.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_json_responses_are_no_store(c):
    for path in ("/api/live", "/api/people", "/api/history", "/api/inbox", "/api/nope"):
        assert c.get(path).headers["cache-control"] == "no-store", path


# ---------------------------------------------------------------- empty DB

def test_live_empty_db(c):
    d = c.get("/api/live").json()
    assert set(d) == {"agent", "disk_free_gb", "now", "upcoming", "today"}
    assert d["now"] is None and d["today"] == []
    assert d["agent"] == {"running": False, "last_tick": None, "mac_awake": False}
    assert isinstance(d["disk_free_gb"], (int, float))
    assert {u["label"] for u in d["upcoming"]} >= {"Huddle", "Insights"}


def test_empty_db_every_endpoint(c):
    assert c.get("/api/inbox").json() == []
    assert c.get("/api/people").json() == []
    assert c.get("/api/weeks").json() == []
    assert c.get("/api/decks").json() == []
    assert c.get("/api/logs").json() == []
    assert c.get("/api/history").json()["groups"] == []
    assert c.get("/api/history?view=person").json()["groups"] == []
    assert c.get("/api/history?view=topic&q=hello").json()["groups"] == []
    assert len(c.get("/api/score?weeks=3").json()) == 3
    assert c.get("/api/command/999").status_code == 404
    d = c.get("/api/channels").json()  # no list yet: the leadership channel alone, by its id
    assert [x["id"] for x in d["channels"]] == [CH] and d["fetched_at"] is None


# ---------------------------------------------------------------- live

def test_live_running_awake_and_today(c, store):
    store.record_run("process_dm", True, "ok")  # record_run stamps the real clock: far from NOW
    eid = store.add_event("insights", "research", model="m1", started=NOW - timedelta(seconds=90), ended=None,
                          ok=None, kind=None)
    store.add_event("decks", "render", model=None, started=NOW - timedelta(hours=1), ended=NOW, ok=False, kind="X")
    d = c.get("/api/live").json()
    # no tick_seen, but a job is running right now, so the Mac is awake (a long tick holds the lock)
    assert d["agent"]["running"] is True and d["agent"]["mac_awake"] is True
    assert d["now"] == {"job": "insights", "step": "research", "model": "m1", "elapsed_s": 90}
    assert [t["title"] for t in d["today"]] == ["decks render", "insights research"]
    assert d["today"][0]["ok"] is False and d["today"][0]["icon"] == "deck"
    assert eid


def test_live_old_open_event_is_not_running(c, store):
    store.add_event("insights", "research", model=None, started=NOW - timedelta(minutes=16), ended=None, ok=None,
                    kind=None)
    assert c.get("/api/live").json()["now"] is None


def test_live_mac_awake_uses_tick_seen(c, store, clock):
    store.set_cursor("tick_seen", (NOW - timedelta(seconds=30)).isoformat())
    d = c.get("/api/live").json()
    assert d["agent"]["mac_awake"] is True and d["agent"]["last_tick"] == (NOW - timedelta(seconds=30)).isoformat()
    clock.t += 300
    assert c.get("/api/live").json()["agent"]["mac_awake"] is False


def test_live_healthy_quiet_tick_without_runs(c, store):
    # The reviewer's probe: a quiet tick writes cursors but no `runs` row.
    for job in ("process_dm", "deliver_pending", "run_todo", "run_feedback"):
        store.set_cursor(f"tick_last:{job}", (NOW - timedelta(seconds=30)).isoformat())
    store.set_cursor("tick_seen", (NOW - timedelta(seconds=30)).isoformat())
    d = c.get("/api/live").json()
    assert d["agent"]["mac_awake"] is True
    fb = {u["label"]: u["at"] for u in d["upcoming"]}["Feedback scan"]
    assert fb == (NOW - timedelta(seconds=30) + timedelta(seconds=600)).isoformat()  # workday daytime: 600 s


def test_live_feedback_scan_waits_out_the_huddle(c, store, clock):
    clock.t = datetime(2026, 10, 19, 14, 40, tzinfo=KW).timestamp()
    store.set_cursor("tick_last:run_feedback", datetime(2026, 10, 19, 14, 35, tzinfo=KW).isoformat())
    fb = {u["label"]: u["at"] for u in c.get("/api/live").json()["upcoming"]}["Feedback scan"]
    assert fb == "2026-10-19T16:05:00+03:00"


def test_tick_writes_tick_seen_even_when_quiet(settings, store):
    from haseeb_agent import cli
    cli.run_tick(settings, store, {}, clock=lambda: NOW)
    assert store.get_cursor("tick_seen") == NOW.isoformat()


def test_live_upcoming_fixed_clock(c, clock):
    up = {u["label"]: u["at"] for u in c.get("/api/live").json()["upcoming"]}
    assert up["Huddle"] == "2026-10-19T14:30:00+03:00"
    assert up["Insights"] == "2026-10-20T08:15:00+03:00"  # 10:00 is past today's 08:15
    clock.t = datetime(2026, 10, 19, 15, 0, tzinfo=KW).timestamp()  # after the huddle time (session still fresh)
    up = {u["label"]: u["at"] for u in c.get("/api/live").json()["upcoming"]}
    assert up["Huddle"] == "2026-10-20T14:30:00+03:00"


def test_next_at_skips_the_weekend(settings):
    from haseeb_agent.dashboard.api import _next_at
    fri = datetime(2026, 10, 23, 16, 0, tzinfo=KW)
    assert _next_at(fri, settings.huddle_time, settings).isoformat() == "2026-10-26T14:30:00+03:00"
    assert _next_at(fri, datetime(2026, 1, 1, 8, 15).time(), settings).isoformat() == "2026-10-26T08:15:00+03:00"


# ---------------------------------------------------------------- history

def seed_day(store):
    store.upsert_person("U1", "Ann")
    store.upsert_person(HASEEB, "Haseeb")
    store.upsert_message(msg("1000.1", "notes text", user_id="USLACK", user_name="Slackbot", kind="huddle_notes"))
    store.upsert_message(msg("1000.2", "Next todo: A", user_id=HASEEB, user_name="Haseeb", kind="todo"))
    store.upsert_message(msg("1000.3", "Next todo: B", user_id="U1", kind="todo"))
    store.upsert_message(msg("2000.1", "Daily insight pricing", user_id=HASEEB, user_name="Haseeb", kind="insight"))
    store.upsert_message(msg("2000.2", "nice pricing", kind="feedback", thread_ts="2000.1"))
    store.upsert_message(msg("2000.3", "agree", user_id="U2", user_name="Bob", kind="feedback", thread_ts="2000.1"))
    store.upsert_message(msg("3000.1", "random chat", user_id="U2", user_name="Bob", kind="other", topic="chat"))


def test_history_day_grouping(c, store):
    seed_day(store)
    g = c.get("/api/history?view=day&week=2026-10-19").json()["groups"]
    assert len(g) == 1 and g[0]["day"] == "2026-10-19"
    h = g[0]
    assert h["huddle"]["notes"] == "notes text"
    assert [(t["user"], t["text"]) for t in h["huddle"]["todos"]] == [("Haseeb", "Next todo: A"), ("Ann", "Next todo: B")]
    # Task 9: each todo and each notes message carries the message itself, so the page can link it to Slack,
    # re-label it and outline Haseeb's own
    me, ann = h["huddle"]["todos"]
    assert (me["ts"], me["channel"], me["user_id"], me["mine"], me["kind"]) == ("1000.2", CH, HASEEB, True, "todo")
    assert ann["mine"] is False and ann["permalink"].endswith(f"/archives/{CH}/p10003") and "color" in ann
    (notes,) = h["huddle"]["messages"]
    assert (notes["ts"], notes["text"], notes["kind"], notes["user_name"]) == ("1000.1", "notes text", "huddle_notes", "Slackbot")
    assert notes["permalink"].endswith(f"/archives/{CH}/p10001")
    assert len(h["insights"]) == 1 and h["insights"][0]["msg"]["text"] == "Daily insight pricing"
    assert [f["text"] for f in h["insights"][0]["feedback"]] == ["nice pricing", "agree"]
    assert [m["text"] for m in h["other"]] == ["random chat"]
    ins = h["insights"][0]["msg"]
    assert ins["mine"] is True and ins["user_name"] == "Haseeb" and "color" in ins
    assert h["insights"][0]["feedback"][0]["mine"] is False


def test_history_day_defaults_to_current_week_and_skips_other_weeks(c, store):
    store.upsert_message(msg("1.1", "this week"))
    store.upsert_message(msg("2.1", "old", day=date(2026, 9, 1)))
    texts = [m["text"] for g in c.get("/api/history").json()["groups"] for m in g["other"]]
    assert texts == ["this week"]


def test_history_person_view(c, store):
    seed_day(store)
    g = c.get("/api/history?view=person").json()["groups"]
    by = {x["person"]["user_id"]: x for x in g}
    assert by["U1"]["count"] == 2 and by["U2"]["count"] == 2
    assert by["U1"]["person"]["name"] == "Ann"
    only = c.get("/api/history?view=person&person=U1").json()["groups"]
    assert [x["person"]["user_id"] for x in only] == ["U1"]


def test_history_topic_view(c, store):
    seed_day(store)
    g = c.get("/api/history?view=topic").json()["groups"]
    by = {x["topic"]: x["count"] for x in g}
    assert by == {None: 6, "chat": 1}
    assert g[0]["topic"] is None  # biggest first


def test_search_endpoint_filters(c, store):
    seed_day(store)
    r = c.get("/api/history?view=person&q=pricing").json()["groups"]
    assert sorted(m["text"] for g in r for m in g["messages"]) == ["Daily insight pricing", "nice pricing"]
    r = c.get("/api/history?view=person&q=pricing&kind=insight").json()["groups"]
    assert [m["text"] for g in r for m in g["messages"]] == ["Daily insight pricing"]
    r = c.get("/api/history?view=person&q=pricing&person=U1").json()["groups"]
    assert [m["text"] for g in r for m in g["messages"]] == ["nice pricing"]
    assert c.get("/api/history?q=%22unbalanced").status_code == 200


def test_person_chip_is_an_exact_id_and_typed_person_a_name_prefix(c, store):
    """A people chip sends a user id: under it, only that id. A typed person: also matches the start of a name."""
    store.upsert_message(msg("1.1", "pricing a", user_id="U1", user_name="Ann"))
    store.upsert_message(msg("1.2", "pricing b", user_id="U9", user_name="U1 fan"))  # a name that starts like an id
    chip = c.get("/api/history?view=person&q=pricing&person=U1").json()["groups"]
    assert [m["text"] for g in chip for m in g["messages"]] == ["pricing a"]
    typed = c.get("/api/history?view=person&q=pricing%20person%3AU1").json()["groups"]
    assert sorted(m["text"] for g in typed for m in g["messages"]) == ["pricing a", "pricing b"]
    assert [m.ts for m in store.search("pricing", user_id="U1")] == ["1.1"]


def test_permalink_from_ids_only(c, store, settings):
    hostile = "<script>x</script> p999 ?evil=1"
    store.upsert_message(msg("1700000000.000100", hostile, kind="insight"))
    store.upsert_message(msg("1700000001.000200", "reply", kind="feedback", thread_ts="1700000000.000100"))
    g = c.get("/api/history?week=2026-10-19").json()["groups"][0]
    top = g["insights"][0]
    assert top["msg"]["permalink"] == f"{settings.workspace_url}/archives/{CH}/p1700000000000100"
    assert top["msg"]["text"] == hostile  # returned as data
    assert top["feedback"][0]["permalink"] == (
        f"{settings.workspace_url}/archives/{CH}/p1700000001000200?thread_ts=1700000000.000100&cid={CH}")


def test_permalink_refuses_unsafe_ids(c, store):
    store.upsert_message(msg("1.1", "x", channel="C/../bad"))
    store.upsert_message(msg("1.2", "y"))
    groups = c.get("/api/history?view=person").json()["groups"]
    links = {m["text"]: m["permalink"] for g in groups for m in g["messages"]}
    assert links["x"] is None and links["y"]


@pytest.mark.parametrize("qs", ["view=nope", "kind=bogus", "week=notadate", "person=%27%3B--", "q=" + "a" * 300])
def test_history_bad_params_400(c, qs):
    r = c.get(f"/api/history?{qs}")
    assert r.status_code == 400 and set(r.json()) == {"error"}


def test_people_and_weeks(c, store):
    store.upsert_person("U1", "Ann")
    store.upsert_message(msg("1.1", day=date(2026, 10, 5)))
    store.upsert_message(msg("1.2", day=date(2026, 10, 5)))
    store.upsert_message(msg("1.3", day=date(2026, 10, 14)))
    assert c.get("/api/people").json() == [{"user_id": "U1", "name": "Ann", "color": 0}]
    w = c.get("/api/weeks").json()
    assert [x["week"] for x in w] == ["2026-10-05", "2026-10-12", "2026-10-19"]
    assert w[0]["days"] == [2, 0, 0, 0, 0, 0, 0] and w[1]["days"][2] == 1 and w[2]["total"] == 0


# ---------------------------------------------------------------- inbox, action, label, command

def test_inbox_lists_open_items(c, store):
    it = insight_item()
    store.upsert_item(it)
    store.upsert_item(todo_item())
    store.set_cursor("autopost:todo-2026-10-19", "2026-10-19T14:32:00+03:00")
    store.upsert_item(insight_item("insight-2026-10-19-2", status="posted"))
    rows = {r["item_id"]: r for r in c.get("/api/inbox").json()}
    assert set(rows) == {"insight-2026-10-19-1", "todo-2026-10-19"}
    r = rows["insight-2026-10-19-1"]
    assert r["kind"] == "insight" and r["shown_hash"] == body_hash(it) and r["body"].startswith("T\n\nW")
    assert r["autopost_at"] is None
    assert rows["todo-2026-10-19"]["autopost_at"] == "2026-10-19T14:32:00+03:00"
    assert rows["todo-2026-10-19"]["body"] == "Next todo: ship it"


# ---- ideas (2026-10-22): an item whose body is schema v2 is an idea. Its kind is still "insight"; the card says "Idea".

def idea_item(iid="insight-2026-10-19-1", status="delivered", **fields):
    """A delivered idea: the body the insights job saves now (schema v2), under the kind "insight". `fields` change
    the idea's own fields."""
    from tests.test_insights import good_idea_meta
    meta = good_idea_meta(batch=iid)
    meta["insight"].update(fields)
    return replace(insight_item(iid, status), body=json.dumps(meta), title="Topic alpha")


def inbox_row(c, iid="insight-2026-10-19-1"):
    return next(r for r in c.get("/api/inbox").json() if r["item_id"] == iid)


def test_inbox_text_for_an_idea(c, store):
    from tests.test_insights import good_idea_meta
    store.upsert_item(idea_item())
    d = good_idea_meta()["insight"]
    body = inbox_row(c)["body"]
    # the header ("title · price"), then the idea, why now, who pays, the build and the first step: a paragraph each
    assert body == "\n\n".join([f"{d['title']} · {d['price']}", d["idea"], d["why_now"], d["who_pays"],
                                d["build_with_ai"], d["first_step"]])
    assert body.startswith("Topic alpha · Rs 15k/month\n\nThe pain behind Topic alpha. We build the fix.\n\n")
    # nothing of the v1 card, and not the evidence, the risks or the "not verified" list
    for left_out in ("What happened", "Silicon Valley", d["risks"][0], d["not_verified"][0], "alpha source"):
        assert left_out not in body


def test_inbox_text_for_an_idea_holds_hostile_text_as_text(c, store):
    """The API gives the fields as they are stored (the job cleans them when it saves); the page shows them as text."""
    hostile = "<img src=x onerror=alert(1)> </script> {{title}}"
    store.upsert_item(idea_item(title=hostile, idea=f"<!channel> {hostile}"))
    body = inbox_row(c)["body"]
    assert body.startswith(f"{hostile} · Rs 15k/month\n\n<!channel> {hostile}\n\n")


V1_BODIES = [
    {"v": 1, "insight": {"title": "T", "what_happened": "W", "why_it_matters": "Y", "silicon_valley": "S"}},
    {"v": 1, "insight": {"title": "T", "what_happened": "W"}},                       # fields missing
    {"v": 1, "insight": {"title": "T", "what_happened": 5, "silicon_valley": ["x"], "why_it_matters": "Y"}},  # not text
    {"v": 1, "insight": {"title": "T"}},
    {"v": 1, "insight": {}},
    {"v": 1, "insight": {"title": "Título", "what_happened": "line one\nline two", "why_it_matters": "  Y  ",
                         "silicon_valley": "", "evidence": [{"title": "x", "url": "https://x.example"}]}},
    {"v": 1},
]


def test_inbox_text_for_a_v1_insight_unchanged(c, store):
    """A v1 insight (today's three, and every one stored before ideas) shows exactly the text the dashboard built before:
    its title, what happened, why it matters and Silicon Valley, a paragraph each."""
    from haseeb_agent.dashboard.api import _body_text
    from tests.test_insights import good_meta, old_body_text
    store.upsert_item(insight_item())
    row = inbox_row(c)
    assert row["body"] == "T\n\nW\n\nY\n\nS" and row["body"] == old_body_text(insight_item().body)
    assert row["format"] == "insight"  # and it keeps the label "Insight"
    full = good_meta(batch="insight-2026-10-19-3")
    store.upsert_item(replace(insight_item("insight-2026-10-19-3"), body=json.dumps(full)))
    assert inbox_row(c, "insight-2026-10-19-3")["body"] == "\n\n".join(
        full["insight"][k] for k in ("title", "what_happened", "why_it_matters", "silicon_valley"))
    for meta in V1_BODIES:
        body = json.dumps(meta, ensure_ascii=False)
        assert _body_text("insight", body) == old_body_text(body), meta
    assert _body_text("insight", json.dumps(V1_BODIES[0])) == "T\n\nW\n\nY\n\nS"


@pytest.mark.parametrize("body", [
    "not json", "", "null", "[]", '"text"', "5", "{}",
    '{"insight": {"title": "T"}}',                                    # no version
    '{"v": 3, "insight": {"title": "T", "what_happened": "W"}}',       # a version nothing reads
    '{"v": true, "insight": {"title": "T"}}', '{"v": 2.0, "insight": {"title": "T"}}', '{"v": "1", "insight": {}}',
    '{"v": 2}', '{"v": 2, "insight": "x"}', '{"v": 1, "insight": null}',
])
def test_inbox_text_of_an_unreadable_insight_is_empty(c, store, body):
    store.upsert_item(replace(insight_item(), body=body))
    row = inbox_row(c)
    assert row["body"] == "" and row["format"] == "insight"  # still a card: the label stays the old one


def test_inbox_format_tells_an_idea_from_an_insight(c, store):
    """`format` is what the card calls itself: "idea" for a v2 insight, "insight" for a v1 one, nothing for other kinds."""
    store.upsert_item(idea_item("insight-2026-10-19-2"))
    store.upsert_item(insight_item("insight-2026-10-19-1"))
    store.upsert_item(todo_item())
    store.upsert_item(message_item())
    store.upsert_item(replace(insight_item("feedback-1.1"), kind="feedback", body="Great post."))
    rows = {r["item_id"]: r for r in c.get("/api/inbox").json()}
    assert rows["insight-2026-10-19-2"]["format"] == "idea" and rows["insight-2026-10-19-2"]["kind"] == "insight"
    assert rows["insight-2026-10-19-1"]["format"] == "insight" and rows["insight-2026-10-19-1"]["kind"] == "insight"
    for iid in ("todo-2026-10-19", "message-1792400000.000001", "feedback-1.1"):
        assert rows[iid]["format"] is None, iid


def test_an_idea_card_carries_its_hash_and_its_deck_like_an_insight_card(c, store):
    """The kind is still "insight": the card's hash is the SHA of the stored body, and the deck strip's data is there
    (the deck of an idea is read the same way as an insight's)."""
    it = idea_item()
    store.upsert_item(it)
    row = inbox_row(c)
    assert row["shown_hash"] == body_hash(it)
    assert row["deck"] is not None and row["deck"]["state"] in ("done", "pending", "gave_up", "off")


# ---- grouped by day, newest first (Haseeb: "new date data comes first and add a proper date categorized")

def at_day(iid, day, dm_ts, kind="insight", status="delivered"):
    return replace(insight_item(iid, status, day=day, dm_ts=dm_ts), kind=kind)


def inbox_ids(c):
    return [r["item_id"] for r in c.get("/api/inbox").json()]


def test_inbox_lists_the_newest_day_first_and_the_newest_draft_first_within_a_day(c, store):
    """Newest day first (the item's own day); inside a day the newest DM first, the kinds mixed in that order."""
    d19, d18, d17 = date(2026, 10, 19), date(2026, 10, 18), date(2026, 10, 17)
    for it in (at_day("insight-2026-10-19-1", d19, "100.1"), at_day("insight-2026-10-19-2", d19, "100.2"),
               at_day("todo-2026-10-19", d19, "100.4", kind="todo"), at_day("feedback-x", d19, "100.3", kind="feedback"),
               at_day("insight-2026-10-18-1", d18, "90.1"),
               at_day("message-999.5", d18, "999.5", kind="message"),  # the latest DM of all, but of an older day
               at_day("insight-2026-10-17-1", d17, "80.1")):
        store.upsert_item(it)
    assert inbox_ids(c) == ["todo-2026-10-19", "feedback-x", "insight-2026-10-19-2", "insight-2026-10-19-1",
                            "message-999.5", "insight-2026-10-18-1", "insight-2026-10-17-1"]
    rows = c.get("/api/inbox").json()
    assert [r["day"] for r in rows] == ["2026-10-19"] * 4 + ["2026-10-18"] * 2 + ["2026-10-17"]
    assert [r["kind"] for r in rows[:4]] == ["todo", "feedback", "insight", "insight"]  # the kinds stay mixed


def test_inbox_days_are_labelled_in_the_settings_timezone(c, store):
    """"Today · Mon 19 Oct", "Yesterday · Sun 18 Oct", then "Sat 17 Oct" (the clock is Monday 10:00 in Kuwait)."""
    for n, day in enumerate((date(2026, 10, 19), date(2026, 10, 18), date(2026, 10, 17)), 1):
        store.upsert_item(at_day(f"insight-{day.isoformat()}-1", day, f"{n}.1"))
    labels = {r["day"]: r["day_label"] for r in c.get("/api/inbox").json()}
    assert labels == {"2026-10-19": "Today · Mon 19 Oct", "2026-10-18": "Yesterday · Sun 18 Oct",
                      "2026-10-17": "Sat 17 Oct"}


def test_just_after_midnight_in_kuwait_it_is_already_the_new_day(settings, store, tmp_path):
    """00:30 in Kuwait on Tuesday 20 Oct is still Monday 19 Oct in UTC. The label follows Kuwait (settings.tz)."""
    clock = Clock(datetime(2026, 10, 20, 0, 30, tzinfo=KW).timestamp())
    app = create_app(settings, lambda: Store(settings.db_path), KEY, lambda: None, clock=clock,
                     login_dir=tmp_path / "login", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks")
    client = stream_client(app, tmp_path, clock)
    store.upsert_item(at_day("insight-2026-10-20-1", date(2026, 10, 20), "5.1"))
    store.upsert_item(at_day("insight-2026-10-19-1", date(2026, 10, 19), "4.1"))
    assert {r["day"]: r["day_label"] for r in client.get("/api/inbox").json()} == {
        "2026-10-20": "Today · Tue 20 Oct", "2026-10-19": "Yesterday · Mon 19 Oct"}


@pytest.mark.parametrize("today,day,want", [
    (date(2026, 10, 21), date(2026, 10, 21), "Today · Wed 21 Oct"),
    (date(2026, 10, 21), date(2026, 10, 20), "Yesterday · Tue 20 Oct"),
    (date(2026, 10, 21), date(2026, 10, 19), "Mon 19 Oct"),
    (date(2026, 10, 21), date(2026, 1, 5), "Mon 5 Jan"),  # an older day of the same year: no year
    (date(2026, 1, 1), date(2026, 1, 1), "Today · Thu 1 Jan"),
    (date(2026, 1, 1), date(2025, 12, 31), "Yesterday · Wed 31 Dec 2025"),  # the year differs: it is added
    (date(2026, 1, 1), date(2025, 12, 30), "Tue 30 Dec 2025"),
    (date(2026, 10, 21), date(2026, 10, 22), "Thu 22 Oct"),  # a day ahead of the clock: plain
])
def test_the_day_label_has_the_year_only_when_it_differs_from_this_year(today, day, want):
    from haseeb_agent.dashboard.api import _day_label
    assert _day_label(day, today) == want


def test_inbox_within_a_day_the_dm_time_decides_then_the_number_at_the_end_of_the_id(c, store):
    """The DM delivery time when there is one; else (or on a tie) the id's numeric suffix, higher first, so
    insight-...-6 comes before insight-...-5. A DM time that is not a time is not a time."""
    d = date(2026, 10, 19)
    for it in (at_day("insight-2026-10-19-5", d, "50.0"), at_day("insight-2026-10-19-6", d, "50.0"),  # a tie
               at_day("insight-2026-10-19-9", d, "60.0"), at_day("insight-2026-10-19-10", d, "60.0"),  # a tie: 10 > 9
               at_day("insight-2026-10-19-7", d, "40.0"),  # delivered earlier, though numbered later
               at_day("insight-2026-10-19-3", d, "0"),  # a time, even if a poor one
               at_day("insight-2026-10-19-8", d, "soon"), at_day("insight-2026-10-19-4", d, "later"),  # no usable time
               at_day("insight-2026-10-19-2", d, "-5")):  # nor is a time before 1970
        store.upsert_item(it)
    assert inbox_ids(c) == ["insight-2026-10-19-10", "insight-2026-10-19-9", "insight-2026-10-19-6",
                            "insight-2026-10-19-5", "insight-2026-10-19-7", "insight-2026-10-19-3",
                            "insight-2026-10-19-8", "insight-2026-10-19-4", "insight-2026-10-19-2"]


def test_inbox_order_is_a_key_that_sorts_the_same_way_as_a_plain_string(c, store):
    """`order` lets a page that already shows some drafts put a new one (or an old one that is leaving) in place
    without knowing the rules: bigger is newer, compared as plain strings."""
    d19, d18 = date(2026, 10, 19), date(2026, 10, 18)
    for it in (at_day("insight-2026-10-18-1", d18, "9.1"), at_day("insight-2026-10-19-2", d19, "100.2"),
               at_day("insight-2026-10-19-1", d19, "100.1"), at_day("todo-2026-10-19", d19, "100.4", kind="todo"),
               at_day("insight-2026-10-19-3", d19, "soon"),
               at_day("feedback-a-5", d19, "7.0", kind="feedback"), at_day("feedback-b-5", d19, "7.0", kind="feedback")):
        store.upsert_item(it)
    rows = c.get("/api/inbox").json()
    assert [r["item_id"] for r in rows] == [r["item_id"] for r in sorted(rows, key=lambda r: r["order"], reverse=True)]
    assert len({r["order"] for r in rows}) == len(rows)  # even for two drafts with the same day, time and number
    ids = [r["item_id"] for r in rows]
    assert ids.index("feedback-b-5") < ids.index("feedback-a-5")  # ...which the id then settles


def test_inbox_pending_and_last_result(c, store):
    """The page's single source of truth for a card's command state (review I2, I3)."""
    from haseeb_agent.commands import mark_post_failed
    it = insight_item()
    store.upsert_item(it)
    first = store.enqueue_page_command(it.id, "shorter", None, body_hash(it), NOW)
    store.set_page_command(first, "done", "✏️ Revised below")
    second = store.enqueue_page_command(it.id, "post", None, body_hash(it), NOW)
    (r,) = c.get("/api/inbox").json()
    assert r["pending"] == {"command_id": second, "verb": "post", "state": "queued"}
    assert r["last_result"] == {"command_id": first, "verb": "shorter", "state": "done", "result": "✏️ Revised below"}
    assert r["post_may_have_gone_out"] is False
    store.set_page_command(second, "running")
    assert c.get("/api/inbox").json()[0]["pending"]["state"] == "running"
    # the post fails the way the runner records it (C1 first, then the item), and the inbox says so for good
    store.set_page_command(second, "failed", "⚠️ Post may have gone out — check the channel before re-posting (X)")
    mark_post_failed(store, it.id, NOW)
    store.mark(it.id, "failed", attempts=1)
    (r,) = c.get("/api/inbox").json()
    assert r["status"] == "failed" and r["pending"] is None and r["post_may_have_gone_out"] is True
    assert r["last_result"]["state"] == "failed" and r["last_result"]["result"].startswith("⚠️ Post may have gone out")


def test_inbox_failed_without_a_post_failure_is_not_flagged(c, store):
    store.upsert_item(insight_item(status="failed"))  # e.g. three failed revisions: nothing may have gone out
    (r,) = c.get("/api/inbox").json()
    assert r["post_may_have_gone_out"] is False and r["pending"] is None and r["last_result"] is None


def refused_post_row(store, it, why):
    """A `post` row that ended `failed`, whatever the reason (the text is the row's result)."""
    cid = store.enqueue_page_command(it.id, "post", None, body_hash(it), NOW)
    store.set_page_command(cid, "failed", why)
    return cid


@pytest.mark.parametrize("why", [
    "⚠️ #survive-ai is not one of your channels (list from 2026-10-22)",  # a channel he left, or a stale page (Declined)
    "⚠️ that is not one of your channels (no list yet)",
    "⚠️ this draft may already be in #survive-ai",                         # a retry pinned to another channel (Declined)
    "⚠️ this draft posts only to its thread in the leadership channel",    # a channel on feedback or the todo (Declined)
    "draft changed, reload",                                                # N2: nothing was sent
    "Expired — this click waited too long (the agent wasn't running). Click again if you still want it.",
    "expired: queued before the pause",
    "paused: nothing was sent",                                              # commands.PAUSED_NOTHING_SENT
])
def test_a_post_that_our_own_refusal_ended_is_not_a_post_that_may_have_gone_out(c, store, why):
    """Task 2's review, minor 5: the banner "Post may have gone out" is for an attempt that may have reached Slack. A `post` row
    that ended `failed` because the runner refused it (a channel that is not his, a draft that changed, an expired or paused
    click) sent nothing, so on an item that is `failed` for another reason (three failed revisions) it must not light the banner
    or turn Confirm into "I checked - post again". The card still shows why the click failed."""
    it = insight_item(status="failed")
    store.upsert_item(it)
    refused_post_row(store, it, why)
    (r,) = c.get("/api/inbox").json()
    assert r["status"] == "failed" and r["post_may_have_gone_out"] is False
    assert r["last_result"] == {"command_id": r["last_result"]["command_id"], "verb": "post", "state": "failed", "result": why}


def test_the_runners_refusal_of_a_channel_that_is_not_his_does_not_light_the_banner(c, store, settings):
    """The same, end to end: a real page runner, a click that names a channel the list does not hold, an item that failed before."""
    from haseeb_agent.page_commands import process_page
    from tests.fakes import FakeSlack
    it = insight_item(status="failed")
    store.upsert_item(it)
    cid = store.enqueue_page_command(it.id, "post", "C093L3Y8UM6", body_hash(it), NOW)
    assert process_page(FakeSlack(), store, settings, {}, NOW) == 1
    row = store.page_command(cid)
    assert row.state == "failed" and "not one of your channels" in row.result
    (r,) = c.get("/api/inbox").json()
    assert r["status"] == "failed" and r["post_may_have_gone_out"] is False


def test_an_attempt_that_may_have_reached_slack_still_lights_the_banner(c, store):
    """The failure time (C1), or the in-flight marker when that time could not be recorded (CB2): either says an attempt started
    and its outcome is not known. A refusal that comes after does not take the banner away."""
    from haseeb_agent.commands import mark_post_failed
    timed = insight_item("insight-2026-10-19-1", "failed", dm_ts="1.1")
    marked = insight_item("insight-2026-10-19-2", "failed", dm_ts="2.2")
    clean = insight_item("insight-2026-10-19-3", "failed", dm_ts="3.3")
    for it in (timed, marked, clean):
        store.upsert_item(it)
    mark_post_failed(store, timed.id, NOW)
    store.set_cursor(f"inflight:{marked.id}", NOW.isoformat())
    refused_post_row(store, timed, "⚠️ this draft may already be in #survive-ai")
    refused_post_row(store, marked, "⚠️ #x is not one of your channels (no list yet)")
    refused_post_row(store, clean, "⚠️ #x is not one of your channels (no list yet)")
    flags = {r["item_id"]: r["post_may_have_gone_out"] for r in c.get("/api/inbox").json()}
    assert flags == {timed.id: True, marked.id: True, clean.id: False}
    store.set_cursor(f"inflight:{marked.id}", "")  # the marker is cleared once an outcome is recorded
    assert {r["item_id"]: r["post_may_have_gone_out"] for r in c.get("/api/inbox").json()}[marked.id] is False


def test_inbox_names_the_channel_a_post_may_have_gone_to(c, store):
    """The banner of a post that may have gone out says where to check (post-channel design, ruling 2): the channel the draft's post
    was sent to, recorded before the send and named from the cached list. A draft with no record has none, and its banner keeps the
    old words ("the channel"); a channel the list no longer holds is named by its id."""
    from haseeb_agent import channels
    from haseeb_agent.commands import mark_post_failed
    it = insight_item(status="failed")
    store.upsert_item(it)
    mark_post_failed(store, it.id, NOW)
    (r,) = c.get("/api/inbox").json()
    assert r["post_may_have_gone_out"] is True and (r["post_channel"], r["post_channel_id"]) == (None, None)
    channels.record_post_channel(store, it.id, "C093L3Y8UM6")
    (r,) = c.get("/api/inbox").json()
    assert (r["post_channel"], r["post_channel_id"]) == ("channel C093L3Y8UM6", "C093L3Y8UM6")  # no list cached yet

    def cache(*rows):
        store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": NOW.isoformat(), "channels": [
            {"id": i, "name": n, "private": False} for i, n in rows]}))
    cache(("C093L3Y8UM6", "survive-ai"), (CH, "tf-leadership"))
    (r,) = c.get("/api/inbox").json()
    assert (r["post_channel"], r["post_channel_id"]) == ("#survive-ai", "C093L3Y8UM6")
    channels.record_post_channel(store, it.id, CH)
    (r,) = c.get("/api/inbox").json()
    assert (r["post_channel"], r["post_channel_id"]) == ("#tf-leadership", CH)
    cache(("C093L3Y8UM6", "survive-ai"))  # the leadership channel's name is not known: it is still said
    (r,) = c.get("/api/inbox").json()
    assert (r["post_channel"], r["post_channel_id"]) == ("the leadership channel", CH)


def test_inbox_post_channel_is_only_for_ideas_and_messages_that_have_a_record(c, store):
    from haseeb_agent import channels
    fresh, todo, note = insight_item("insight-2026-10-19-1"), todo_item(), message_item()
    for it in (fresh, todo, note):
        store.upsert_item(it)
    channels.record_post_channel(store, note.id, "C093L3Y8UM6")
    channels.record_post_channel(store, todo.id, "C093L3Y8UM6")  # (never written for a todo; it would be ignored)
    got = {r["item_id"]: (r["post_channel"], r["post_channel_id"]) for r in c.get("/api/inbox").json()}
    assert got == {fresh.id: (None, None), todo.id: (None, None), note.id: ("channel C093L3Y8UM6", "C093L3Y8UM6")}


def test_store_last_page_command(store):
    a = store.enqueue_page_command("x", "skip", None, "h", NOW)
    b = store.enqueue_page_command("x", "post", None, "h", NOW)
    store.enqueue_page_command("y", "post", None, "h", NOW)
    assert store.last_page_command("x", ("queued",)).id == b
    store.set_page_command(b, "done", "ok")
    assert store.last_page_command("x", ("queued", "running")).id == a
    assert store.last_page_command("x", ("done", "failed")).id == b
    assert store.last_page_command("z", ("queued",)) is None
    with pytest.raises(ValueError):
        store.last_page_command("x", ("nope",))


def test_action_refuses_a_second_command_while_one_is_pending(c, store):
    """One command per item at a time: a second tab (or a page that hasn't seen the first yet) gets 409 and the id of
    the command in flight, never a second row."""
    it = insight_item()
    store.upsert_item(it)
    first = post_action(c, item_id=it.id, verb="post", shown_hash=body_hash(it))
    assert first.status_code == 200
    cid = first.json()["command_id"]
    for verb in ("post", "skip", "shorter"):
        r = post_action(c, item_id=it.id, verb=verb, shown_hash=body_hash(it))
        assert r.status_code == 409, verb
        assert r.json() == {"error": "already pending", "command_id": cid, "verb": "post", "state": "queued"}
    store.set_page_command(cid, "running")
    assert post_action(c, item_id=it.id, verb="skip", shown_hash=body_hash(it)).json()["state"] == "running"
    assert [x.id for x in store.recent_page_commands(50)] == [cid]  # still exactly one row
    store.set_page_command(cid, "done", "ok")
    assert post_action(c, item_id=it.id, verb="skip", shown_hash=body_hash(it)).status_code == 200  # settled: free again


def post_action(c, **body):
    return c.post("/api/action", json=body)


def test_action_enqueues_and_kicks(c, store, kicks):
    it = insight_item()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="edit", arg="make it shorter please", shown_hash=body_hash(it))
    assert r.status_code == 200
    cmd = store.page_command(r.json()["command_id"])
    assert (cmd.item_id, cmd.verb, cmd.arg, cmd.shown_hash, cmd.state) == (
        it.id, "edit", "make it shorter please", body_hash(it), "queued")
    assert len(kicks) == 1
    assert c.get(f"/api/command/{cmd.id}").json() == {"id": cmd.id, "item_id": it.id, "verb": "edit",
                                                      "state": "queued", "result": None}


@pytest.mark.parametrize("kind_item,verb,ok", [
    ("insight", "cancel", False), ("insight", "attach", True), ("insight", "post", True),
    ("todo", "cancel", True), ("todo", "attach", False), ("todo", "post", True), ("todo", "bogus", False),
    ("message", "post", True), ("message", "shorter", True), ("message", "skip", True),
    ("message", "cancel", False), ("message", "attach", False), ("message", "compose", False),
])
def test_action_verbs_per_kind(c, store, kind_item, verb, ok):
    it = {"insight": insight_item, "todo": todo_item, "message": message_item}[kind_item]()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb=verb, shown_hash=body_hash(it))
    assert r.status_code == (200 if ok else 400)


def test_action_rejects_bad_verb(c, store, kicks):
    it = insight_item()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="cancel")
    assert r.status_code == 400 and r.json() == {"error": "bad verb"}
    assert kicks == [] and store.next_page_commands() == []


def test_action_edit_too_long_400(c, store):
    it = insight_item()
    store.upsert_item(it)
    secret = "SECRET" * 400
    r = post_action(c, item_id=it.id, verb="edit", arg=secret, shown_hash=body_hash(it))
    assert r.status_code == 400 and "SECRET" not in r.text
    assert post_action(c, item_id=it.id, verb="edit", arg="x" * 2000, shown_hash=body_hash(it)).status_code == 200
    assert post_action(c, item_id=it.id, verb="edit", arg="", shown_hash=body_hash(it)).status_code == 400
    assert post_action(c, item_id=it.id, verb="edit", shown_hash=body_hash(it)).status_code == 400


def test_action_shown_hash_required_except_cancel_attach(c, store):
    it = insight_item()
    store.upsert_item(it)
    assert post_action(c, item_id=it.id, verb="post").status_code == 400
    assert post_action(c, item_id=it.id, verb="post", shown_hash="nothex").status_code == 400
    assert post_action(c, item_id=it.id, verb="attach").status_code == 200


def test_action_unknown_item_404_and_garbage_400(c, kicks):
    assert post_action(c, item_id="nope-1", verb="post", shown_hash="0" * 64).status_code == 404
    assert c.post("/api/action", content=b"not json").status_code == 400
    assert c.post("/api/action", json=["x"]).status_code == 400
    assert c.post("/api/action", json={"item_id": 5, "verb": "post"}).status_code == 400
    assert c.post("/api/action", content=b"{" + b" " * 30000 + b"}").status_code == 400
    assert kicks == []


def test_label_sets_and_validates(c, store):
    store.upsert_message(msg("1.1", kind="other"))
    assert c.post("/api/label", json={"channel": CH, "ts": "1.1", "kind": "insight"}).json() == {"ok": True}
    assert store.messages(kind="insight")[0].ts == "1.1"
    assert c.post("/api/label", json={"channel": CH, "ts": "1.1", "kind": "bogus"}).status_code == 400
    assert c.post("/api/label", json={"channel": CH, "ts": "9.9", "kind": "insight"}).status_code == 404
    assert c.post("/api/label", json={"channel": CH}).status_code == 400


def test_command_id_not_numeric_404(c):
    assert c.get("/api/command/abc").status_code == 404


# ---------------------------------------------------------------- stream

def sse_events(text):
    out = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        if lines:
            out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


@pytest.fixture
def stream_app(make_app):
    async def nosleep(_s):
        return None
    return lambda **kw: make_app(stream_poll_s=0, stream_max_polls=kw.pop("polls", 2), stream_sleep=kw.pop("sleep", nosleep), **kw)


def stream_client(app, tmp_path, clock):
    client, token = do_login(app, tmp_path / "login", clock)
    client.headers.update({"X-Session-Token": token})
    return client


def test_stream_emits_new_event(stream_app, store, tmp_path, clock):
    store.add_event("insights", "research", model="m", started=NOW, ended=NOW, ok=True, kind=None)
    client = stream_client(stream_app(), tmp_path, clock)
    with client.stream("GET", "/api/stream?since=0") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-store"
        body = "".join(r.iter_text())
    ev = sse_events(body)
    assert len(ev) == 1 and ev[0][0] == "event"
    assert ev[0][1]["job"] == "insights" and ev[0][1]["step"] == "research" and ev[0][1]["ok"] is True


def test_stream_skips_old_events_and_sees_new_ones(stream_app, store, settings, tmp_path, clock):
    store.add_event("old", "s", model=None, started=NOW, ended=NOW, ok=True, kind=None)
    calls = []

    async def sleep(_s):
        calls.append(1)
        if len(calls) == 1:  # the stream runs in another thread: write through a connection of its own
            Store(settings.db_path).add_event("new", "s", model=None, started=NOW, ended=None, ok=None, kind=None)
    client = stream_client(stream_app(sleep=sleep), tmp_path, clock)
    with client.stream("GET", "/api/stream") as r:
        ev = sse_events("".join(r.iter_text()))
    assert [e[1]["job"] for e in ev] == ["new"]


def test_stream_emits_command_changes_and_heartbeat(stream_app, store, settings, tmp_path, clock):
    it = insight_item()
    store.upsert_item(it)
    cid = store.enqueue_page_command(it.id, "skip", None, body_hash(it), NOW)
    calls = []

    async def sleep(_s):
        calls.append(1)
        if len(calls) == 1:
            Store(settings.db_path).set_page_command(cid, "done", "Skipped")
        clock.t += 16  # a quiet poll: the next one is a heartbeat
    client = stream_client(stream_app(sleep=sleep, polls=3), tmp_path, clock)
    with client.stream("GET", "/api/stream") as r:
        body = "".join(r.iter_text())
    ev = sse_events(body)
    assert ev == [("command", {"id": cid, "item_id": it.id, "verb": "skip", "state": "done", "result": "Skipped"})]
    assert ": heartbeat" in body


def test_stream_stops_when_client_disconnects(make_app, tmp_path, clock):
    import asyncio

    from haseeb_agent.dashboard.api import ApiConfig, stream

    class Req:
        query_params = {}
        n = 0

        async def is_disconnected(self):
            self.n += 1
            return self.n > 2

    cfg = ApiConfig(None, lambda: Store(tmp_path / "s.sqlite"), lambda: None, clock, stream_poll_s=0)

    async def run():
        resp = await stream(cfg, Req())
        return [chunk async for chunk in resp.body_iterator]
    chunks = asyncio.run(run())  # no max_polls: it ends only because is_disconnected turned true
    assert chunks == [b": connected\n\n"]


# ---------------------------------------------------------------- decks

def make_deck(store, settings, tmp_path, iid="insight-2026-10-19-1", status="posted", pdf_at=None, state="done"):
    it = insight_item(iid, status=status)
    store.upsert_item(it)
    folder = tmp_path / "decks" / iid
    folder.mkdir(parents=True)
    pdf = pdf_at or folder / f"{iid}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n<< /Type /Page >>\n<< /Type /Page >>\n<< /Type /Pages >>\n")
    store.set_cursor(f"deck:{iid}", json.dumps({"state": state, "pdf": str(pdf), "model": "codex gpt"}))
    return it, pdf


def test_decks_list_and_pdf_bytes(c, store, settings, tmp_path):
    it, pdf = make_deck(store, settings, tmp_path)
    d = c.get("/api/decks").json()
    assert d == [{"item_id": it.id, "title": "T title", "model": "codex gpt", "seconds": None, "pages": 2,
                  "images": None, "factcheck": "passed", "attachable": False, "available": True,
                  "day": "2026-10-19", "at": None, "pending": None}]
    r = c.get(f"/api/decks/{it.id}.pdf")
    assert r.status_code == 200 and r.content == pdf.read_bytes()
    assert r.headers["content-type"] == "application/pdf" and r.headers["cache-control"] == "no-store"


def test_deck_pdf_path_traversal_404(c, store, settings, tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=hunter2")
    for bad in ("../../.env", "..%2f..%2f.env", "%2e%2e%2f%2e%2e%2f.env", "....//.env", "nope-1"):
        for tail in (".pdf", "/thumb.png"):
            r = c.get(f"/api/decks/{bad}{tail}")
            assert r.status_code == 404 and "hunter2" not in r.text, bad
    # a store item whose recorded pdf path points outside the decks dir is refused too
    it, _ = make_deck(store, settings, tmp_path, iid="insight-2026-10-19-2", pdf_at=tmp_path / "outside" / "x.pdf")
    assert c.get(f"/api/decks/{it.id}.pdf").status_code == 404
    assert c.get(f"/api/decks/{it.id}/thumb.png").status_code == 404
    store.set_cursor(f"deck:{it.id}", json.dumps({"state": "done", "pdf": str(tmp_path / "decks" / ".." / ".env")}))
    assert c.get(f"/api/decks/{it.id}.pdf").status_code == 404


def test_deck_pdf_symlink_out_of_decks_dir_404(c, store, settings, tmp_path):
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF secret")
    it = insight_item()
    store.upsert_item(it)
    link = tmp_path / "decks" / it.id / f"{it.id}.pdf"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    store.set_cursor(f"deck:{it.id}", json.dumps({"state": "done", "pdf": str(link)}))
    assert c.get(f"/api/decks/{it.id}.pdf").status_code == 404


def test_deck_thumb_runs_sips_once_and_caches(c, store, settings, tmp_path, sips_calls):
    it, pdf = make_deck(store, settings, tmp_path)
    r = c.get(f"/api/decks/{it.id}/thumb.png")
    assert r.status_code == 200 and r.content == b"\x89PNG-thumb" and r.headers["content-type"] == "image/png"
    assert sips_calls == [["sips", "-s", "format", "png", "--resampleHeightWidthMax", "480", str(pdf.resolve()),
                           "--out", str(pdf.resolve().with_name(pdf.stem + ".thumb.png"))]]
    assert pdf.with_name(pdf.stem + ".thumb.png").is_file()  # cached next to the PDF
    assert c.get(f"/api/decks/{it.id}/thumb.png").status_code == 200
    assert len(sips_calls) == 1


def test_deck_thumb_sips_failure_404(make_app, store, settings, tmp_path, clock):
    it, pdf = make_deck(store, settings, tmp_path)
    for runner in (lambda argv: 1, lambda argv: (_ for _ in ()).throw(OSError("no sips"))):
        client = stream_client(make_app(sips=runner), tmp_path, clock)
        assert client.get(f"/api/decks/{it.id}/thumb.png").status_code == 404
    assert not pdf.with_name(pdf.stem + ".thumb.png").exists()


def test_deck_thumb_default_runner_is_offline_guarded(make_app, store, settings, tmp_path, clock):
    from haseeb_agent.dashboard.api import run_sips
    it, _ = make_deck(store, settings, tmp_path)
    client = stream_client(make_app(sips=run_sips), tmp_path, clock)  # HASEEB_AGENT_OFFLINE=1: it must not run sips
    assert client.get(f"/api/decks/{it.id}/thumb.png").status_code == 404


def test_decks_carry_their_time_and_pending_attach(c, store, settings, tmp_path):
    """Task 10: a tile shows when the deck reached the DM (the deck job's dm_at, R50) and follows an attach that is
    already queued, after a reload too (like the inbox's `pending`)."""
    it, pdf = make_deck(store, settings, tmp_path)
    at = datetime(2026, 10, 19, 8, 41, 5, tzinfo=KW)
    store.set_cursor(f"deck:{it.id}", json.dumps({"state": "done", "pdf": str(pdf), "model": "codex gpt",
                                                  "dm_at": f"{at.timestamp():.6f}"}))
    cid = store.enqueue_page_command(it.id, "attach", None, "", NOW)
    (d,) = c.get("/api/decks").json()
    assert d["day"] == "2026-10-19" and d["at"] == at.isoformat()
    assert d["pending"] == {"command_id": cid, "verb": "attach", "state": "queued"}
    store.set_page_command(cid, "done", "✅ Attached")
    assert c.get("/api/decks").json()[0]["pending"] is None
    store.set_cursor(f"deck:{it.id}", json.dumps({"state": "done", "pdf": str(pdf), "dm_at": "soon"}))
    assert c.get("/api/decks").json()[0]["at"] is None  # a broken stamp is no time, never an error


def test_decks_attachable_matches_what_attach_accepts(c, store, settings, tmp_path):
    """R50: the page offers Attach exactly when `attach` (attach_now) would work: a posted insight whose post's ts is
    known and whose deck is for the posted text, with no attach tried yet, or one the deck job held back as late
    (it finished after the post). A started, failed or done attach is never offered again (R16)."""
    from haseeb_agent.jobs.decks import content_hash
    from tests.test_insights import good_meta
    meta = good_meta()
    it, pdf = make_deck(store, settings, tmp_path)
    store.upsert_item(replace(it, body=json.dumps(meta)))
    st = {"state": "done", "pdf": str(pdf), "model": "codex gpt", "sha": content_hash(meta["insight"])}
    store.set_cursor(f"deck:{it.id}", json.dumps(st))

    def attachable():
        return c.get("/api/decks").json()[0]["attachable"]
    assert attachable() is False  # posted, but the post's ts isn't known
    store.set_cursor(f"post_ts:{it.id}", "1760000000.000100")
    assert attachable() is True  # no attach tried yet
    for state, want in (("late", True), ("started", False), ("failed", False), ("done", False)):
        store.set_cursor(f"attach:{it.id}", state)
        assert attachable() is want, state
    store.set_cursor(f"attach:{it.id}", "late")
    store.set_cursor(f"deck:{it.id}", json.dumps({**st, "sha": "0" * 16}))
    assert attachable() is False  # the deck is for an older text
    store.set_cursor(f"deck:{it.id}", json.dumps(st))
    store.mark(it.id, "delivered")
    assert attachable() is False  # not posted


@pytest.mark.parametrize("model,want", [("codex gpt-6.1-sol", "passed"), ("codex gpt-6.1-sol (repaired)", "passed"),
                                        ("template (fact-check)", "refused"), ("template", None), ("codex", None)])
def test_decks_factcheck_only_when_one_ran(c, store, settings, tmp_path, model, want):
    """The deck job's model label says whether a fact check ran: the designer's deck passed it ("codex <model>",
    repaired or not) or it refused it ("template (fact-check)"). The template, or the template with a Codex cover
    (a bare "codex"), had none."""
    it, pdf = make_deck(store, settings, tmp_path)
    store.set_cursor(f"deck:{it.id}", json.dumps({"state": "done", "pdf": str(pdf), "model": model}))
    assert c.get("/api/decks").json()[0]["factcheck"] == want


def test_deck_not_done_is_404(c, store, settings, tmp_path):
    it, _ = make_deck(store, settings, tmp_path, state="pending")
    assert c.get(f"/api/decks/{it.id}.pdf").status_code == 404
    assert c.get("/api/decks").json() == []


# ---------------------------------------------------------------- the deck on an insight's card (/api/inbox)

PDF2 = b"%PDF-1.4\n<< /Type /Page >>\n<< /Type /Page >>\n<< /Type /Pages >>\n"
NO_DECK_YET = {"stale": False, "model": None, "pages": None, "factcheck": None, "available": False, "dm_at": None}


def card_deck(client, iid="insight-2026-10-19-1"):
    """The `deck` of an insight, as /api/inbox gives it."""
    return {r["item_id"]: r for r in client.get("/api/inbox").json()}[iid]["deck"]


def readable_insight(store, iid="insight-2026-10-19-1", **kw):
    """An insight whose body is what the insights job saves, so the deck job (and the card) can read it."""
    from tests.test_insights import good_meta
    it = replace(insight_item(iid, **kw), body=json.dumps(good_meta(batch=iid)))
    store.upsert_item(it)
    return it


def deck_cursor(store, tmp_path, it, *, pdf=True, sha=None, **fields):
    """The deck job's cursor for `it`: for its current text (or for `sha`), with a real file for the PDF."""
    from haseeb_agent.jobs.decks import content_hash
    st = {"sha": sha or content_hash(json.loads(it.body)["insight"]), **fields}
    if pdf:
        path = tmp_path / "decks" / it.id / f"{it.id}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PDF2)
        st["pdf"] = str(path)
    store.set_cursor(f"deck:{it.id}", json.dumps(st))
    return st


def test_an_insight_in_the_inbox_carries_its_finished_deck(c, store, tmp_path):
    it = readable_insight(store)
    at = datetime(2026, 10, 19, 8, 41, 5, tzinfo=KW)
    deck_cursor(store, tmp_path, it, state="done", model="codex gpt-6.1-sol", dm_at=f"{at.timestamp():.6f}")
    assert card_deck(c) == {"state": "done", "stale": False, "model": "codex gpt-6.1-sol", "pages": 2,
                            "factcheck": "passed", "available": True, "dm_at": at.isoformat()}


def test_a_finished_deck_without_a_dm_time_says_so(c, store, tmp_path):
    """Done, but the deck job's record has no (readable) upload time: the card must not claim it is in his DM."""
    it = readable_insight(store)
    deck_cursor(store, tmp_path, it, state="done", model="template")
    deck = card_deck(c)
    assert (deck["state"], deck["available"], deck["dm_at"], deck["factcheck"]) == ("done", True, None, None)
    deck_cursor(store, tmp_path, it, state="done", model="template", dm_at="soon")
    assert card_deck(c)["dm_at"] is None  # a broken stamp is no time, never an error


@pytest.mark.parametrize("model", ["codex gpt-6.1-sol", "codex gpt-6.1-sol (repaired)", "template (fact-check)",
                                   "template", "codex", None])
def test_the_card_and_the_decks_page_read_a_deck_the_same_way(c, store, tmp_path, model):
    """No second reading of a deck: the card's model, page count, fact check, file and time are the Decks page's."""
    it = readable_insight(store)
    deck_cursor(store, tmp_path, it, state="done", dm_at="1760000000.0", **({"model": model} if model else {}))
    (page,) = c.get("/api/decks").json()
    deck = card_deck(c)
    assert deck["state"] == "done" and deck["pages"] == 2 and deck["dm_at"] is not None
    assert (deck["model"], deck["pages"], deck["factcheck"], deck["available"], deck["dm_at"]) == \
        (page["model"], page["pages"], page["factcheck"], page["available"], page["at"])


@pytest.mark.parametrize("fields", [None, {"state": "pending", "tries": 0}, {"state": "pending", "tries": 2},
                                    {"state": "pending", "tries": 3},  # the last attempt, while it runs
                                    {"state": "pending", "tries": 1, "pdf": True, "model": "codex gpt"}])
def test_a_deck_the_job_still_has_to_make_is_pending(c, store, tmp_path, fields):
    """Today's delivered insight, tries left: "Designing…". A deck built but not yet uploaded is pending too, and
    gives the card nothing to open or preview."""
    it = readable_insight(store)
    if fields is not None:
        deck_cursor(store, tmp_path, it, **{"pdf": False, **fields})
    assert card_deck(c) == {"state": "pending", **NO_DECK_YET}


@pytest.mark.parametrize("fields", [{"state": "gave_up", "tries": 3}, {"state": "gave_up", "tries": 1}])
def test_a_deck_the_job_gave_up_on_is_gave_up(c, store, tmp_path, fields):
    it = readable_insight(store)
    deck_cursor(store, tmp_path, it, pdf=False, **fields)
    assert card_deck(c) == {"state": "gave_up", **NO_DECK_YET}


def test_a_deck_for_a_draft_still_being_delivered_is_pending(c, store):
    """Its later DM parts are still going out: the deck job takes it as soon as it is delivered, so "No deck" would
    be wrong for those moments."""
    readable_insight(store, status="drafted")
    assert card_deck(c)["state"] == "pending"


def test_an_insight_of_an_earlier_day_gets_no_deck_any_more(c, store, tmp_path):
    """The deck job builds today's insights only."""
    old = readable_insight(store, "insight-2026-10-18-1", day=date(2026, 10, 18))
    assert card_deck(c, old.id) == {"state": "gave_up", **NO_DECK_YET}
    deck_cursor(store, tmp_path, old, state="done", model="template", dm_at="1760000000.0")
    assert card_deck(c, old.id)["state"] == "done"  # one built while it was today still shows


OLD_SHA = "0" * 16  # the text hash of the text before an edit


def old_deck(store, tmp_path, it):
    """The deck of the text before an edit: its file on disk, and the record the deck job carries as `prev`."""
    st = deck_cursor(store, tmp_path, it, state="done", model="codex gpt-6.1-sol", dm_at="1760000000.0", sha=OLD_SHA)
    return {"sha": OLD_SHA, "pdf": st["pdf"], "model": "codex gpt-6.1-sol"}


EDITED = {"state": "pending", "stale": True, "model": "codex gpt-6.1-sol", "pages": 2, "factcheck": "passed",
          "available": True, "dm_at": None}  # waiting for the new deck, with the previous one still to show


def test_an_edited_text_shows_the_deck_of_the_text_before_as_stale(c, store, tmp_path):
    """The live incident: Haseeb edited an insight, and its deck in his DM was for the text before. The deck for the
    CURRENT text is not there, so the card is `pending` + `stale`, shows the previous deck as that, and never claims a
    time in his DM for it. (The deck job has not started on the new text yet: the cursor still holds the old record.)"""
    it = readable_insight(store)
    old_deck(store, tmp_path, it)
    assert card_deck(c) == EDITED


def test_it_stays_stale_while_the_deck_job_makes_the_new_deck(c, store, tmp_path):
    """The job has started: its state for the new text is fresh, and carries the previous deck as `prev`."""
    from haseeb_agent.jobs.decks import content_hash
    it = readable_insight(store)
    prev = old_deck(store, tmp_path, it)
    new = content_hash(json.loads(it.body)["insight"])
    store.set_cursor(f"deck:{it.id}", json.dumps({"sha": new, "tries": 1, "state": "pending", "prev": prev}))
    assert card_deck(c) == EDITED
    # the new deck is built (not yet in his DM): its build has replaced the file of the previous one
    store.set_cursor(f"deck:{it.id}", json.dumps({"sha": new, "tries": 1, "state": "pending", "prev": prev,
                                                  "built": new, "pdf": prev["pdf"], "model": "template"}))
    assert card_deck(c) == {"state": "pending", **{**NO_DECK_YET, "stale": True}}  # an edit is waiting; nothing to show
    # the new deck is in his DM: done, for the current text, with its time, and no longer stale
    store.set_cursor(f"deck:{it.id}", json.dumps({"sha": new, "tries": 1, "state": "done", "built": new,
                                                  "pdf": prev["pdf"], "model": "template", "dm_at": "1760000100.0"}))
    deck = card_deck(c)
    assert (deck["state"], deck["stale"], deck["model"], deck["dm_at"] is not None) == ("done", False, "template", True)


def test_the_previous_deck_can_be_opened_until_a_build_replaces_it(c, store, tmp_path):
    """The card's thumbnail and Open deck go through the same two routes: they serve the previous deck while the new
    one is made, and nothing once its file has been replaced."""
    from haseeb_agent.jobs.decks import content_hash
    it = readable_insight(store)
    prev = old_deck(store, tmp_path, it)
    new = content_hash(json.loads(it.body)["insight"])
    pdf_bytes = Path(prev["pdf"]).read_bytes()
    for st in ({"sha": OLD_SHA, "state": "done", "pdf": prev["pdf"], "model": "codex gpt-6.1-sol"},  # job not started
               {"sha": new, "tries": 1, "state": "pending", "prev": prev}):  # job at work
        store.set_cursor(f"deck:{it.id}", json.dumps(st))
        r = c.get(f"/api/decks/{it.id}.pdf")
        assert r.status_code == 200 and r.content == pdf_bytes and r.headers["content-type"] == "application/pdf"
        assert c.get(f"/api/decks/{it.id}/thumb.png").status_code == 200
    store.set_cursor(f"deck:{it.id}", json.dumps({"sha": new, "tries": 1, "state": "pending", "prev": prev,
                                                  "built": new, "pdf": prev["pdf"]}))  # the file is the new deck's now
    assert c.get(f"/api/decks/{it.id}.pdf").status_code == 404
    assert c.get(f"/api/decks/{it.id}/thumb.png").status_code == 404
    outside = tmp_path / "outside" / "x.pdf"  # a previous deck outside the decks dir, a real file, is refused too
    outside.parent.mkdir()
    outside.write_bytes(b"%PDF secret")
    store.set_cursor(f"deck:{it.id}", json.dumps({"sha": new, "tries": 0, "state": "pending", "prev": {
        **prev, "pdf": str(outside)}}))
    r = c.get(f"/api/decks/{it.id}.pdf")
    assert r.status_code == 404 and b"secret" not in r.content
    assert c.get(f"/api/decks/{it.id}/thumb.png").status_code == 404


def test_an_edit_that_no_deck_will_follow_is_not_stale(c, store, tmp_path):
    """Stale is for an edit the deck job is making a new deck for. When it gave up, or it is another day's insight, no
    deck is coming: "No deck", and the card does not offer the previous one as if it were."""
    from haseeb_agent.jobs.decks import content_hash
    it = readable_insight(store)
    prev = old_deck(store, tmp_path, it)
    new = content_hash(json.loads(it.body)["insight"])
    store.set_cursor(f"deck:{it.id}", json.dumps({"sha": new, "tries": 3, "state": "gave_up", "prev": prev}))
    assert card_deck(c) == {"state": "gave_up", **NO_DECK_YET}
    old = readable_insight(store, "insight-2026-10-18-1", day=date(2026, 10, 18))
    old_deck(store, tmp_path, old)  # an earlier day's insight, edited today: the deck job does not work on it
    assert card_deck(c, old.id) == {"state": "gave_up", **NO_DECK_YET}


def test_a_deck_whose_pdf_is_gone_has_nothing_to_open(c, store, tmp_path):
    it = readable_insight(store)
    st = deck_cursor(store, tmp_path, it, state="done", model="codex gpt", dm_at="1760000000.0")
    Path(st["pdf"]).unlink()
    assert card_deck(c) == {"state": "gave_up", **NO_DECK_YET}


def test_an_insight_the_deck_job_cannot_read_gets_no_deck(c, store):
    store.upsert_item(insight_item())  # the body is not the insights job's saved shape
    assert card_deck(c) == {"state": "gave_up", **NO_DECK_YET}


def test_decks_turned_off_in_the_config_show_off_unless_one_was_built(settings, store, tmp_path, clock):
    app = create_app(replace(settings, decks_enabled=False), lambda: Store(settings.db_path), KEY, lambda: None,
                     clock=clock, login_dir=tmp_path / "login", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks")
    client = stream_client(app, tmp_path, clock)
    readable_insight(store)
    built = readable_insight(store, "insight-2026-10-19-2", dm_ts="2.2")
    deck_cursor(store, tmp_path, built, state="done", model="template", dm_at="1760000000.0")
    assert card_deck(client) == {"state": "off", **NO_DECK_YET}
    assert card_deck(client, built.id)["state"] == "done"  # built before decks were turned off


def test_only_an_insight_carries_a_deck(c, store):
    store.upsert_item(todo_item())
    store.upsert_item(message_item())
    assert {r["kind"]: r["deck"] for r in c.get("/api/inbox").json()} == {"todo": None, "message": None}


# ---------------------------------------------------------------- logs

@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "tick.log").write_text(
        "2026-10-19T09:00:00+03:00 process_dm ok 0 command(s) (1.0s)\n"
        "2026-10-19T09:05:00+03:00 process_dm fail slack_read_channel error (7.7s)\n"
        "2026-10-19T09:06:00+03:00 run_todo ok today's todo: none (0.0s)\n")
    (d / "tick.err.log").write_text(
        "2026-10-19 09:05:01,100 WARNING haseeb_agent.slack_gateway: slack unavailable: claude exit 1\n"
        "Traceback (most recent call last):\n  File secret text\n"
        "2026-10-19 09:07:00,000 INFO haseeb_agent.litellm: model call ok\n")
    (d / "insights.log").write_text("2026-10-19T08:20:41+03:00 insights ok: 0 delivered; 0 deck(s) (341s)\n")
    (d / "insights.err.log").write_text(
        "2026-10-19 08:17:53,245 ERROR haseeb_agent.jobs.decks: deck build failed\n")
    return d


def test_logs_include_the_lines_of_jobs_the_dashboard_started(c, logs):
    """A job started from the run controls is detached from the dashboard, so its output goes to the dashboard's own
    log files. Its lines show on the Logs page; the server's warnings (another format) do not."""
    (logs / "dashboard.log").write_text(
        "2026-10-19T10:00:00+03:00 weekly ok: week of 2026-10-19, 3 posted\n"
        "INFO:     Started server process [4242]\n")
    (logs / "dashboard.err.log").write_text(
        "2026-10-19 10:01:00,000 WARNING haseeb_agent.jobs.feedback: feedback: models unreachable\n"
        "WARNING:  Invalid HTTP request received.\n"
        "ERROR:    Exception in ASGI application\n")
    rows = c.get("/api/logs").json()
    assert [(r["source"], r["text"][:24]) for r in rows if r["at"][11:19] in ("10:00:00", "10:01:00")] == [
        ("weekly", "ok: week of 2026-10-19, "), ("feedback", "feedback: models unreach")]
    assert not any("Started server" in r["text"] or "Invalid HTTP" in r["text"] for r in rows)


def test_logs_parse_and_order(c, logs):
    rows = c.get("/api/logs").json()
    assert [r["at"][11:19] for r in rows] == ["08:17:53", "08:20:41", "09:00:00", "09:05:00", "09:05:01",
                                              "09:06:00", "09:07:00"]
    first = rows[0]
    assert set(first) == {"at", "level", "source", "text"}
    assert first["level"] == "ERROR" and first["source"] == "decks" and first["at"].endswith("+03:00")
    assert "secret text" not in json.dumps(rows)  # traceback continuation lines are dropped


def test_logs_filter_problems(c, logs):
    rows = c.get("/api/logs?filter=problems").json()
    assert [r["at"][11:19] for r in rows] == ["08:17:53", "09:05:00", "09:05:01"]
    assert {r["level"] for r in rows} <= {"WARNING", "ERROR"}


def test_logs_other_filters_q_and_before(c, logs):
    assert [r["source"] for r in c.get("/api/logs?filter=slack").json()] == [
        "process_dm", "process_dm", "slack_gateway", "run_todo"]
    assert [r["source"] for r in c.get("/api/logs?filter=models").json()] == ["slack_gateway", "litellm"]
    assert [r["source"] for r in c.get("/api/logs?filter=decks").json()] == ["decks", "insights"]
    assert len(c.get("/api/logs?q=TODO").json()) == 1
    before = c.get("/api/logs?before=2026-10-19T09:05:00%2B03:00").json()
    assert [r["at"][11:19] for r in before] == ["08:17:53", "08:20:41", "09:00:00"]
    assert c.get("/api/logs?filter=bogus").status_code == 400
    assert c.get("/api/logs?before=nope").status_code == 400


def test_logs_returns_last_200_of_a_long_file(c, tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "tick.log").write_text("".join(f"2026-10-19T09:{i // 60:02d}:{i % 60:02d}+03:00 run_todo ok n{i}\n"
                                        for i in range(3000)))
    rows = c.get("/api/logs").json()
    assert len(rows) == 200 and rows[-1]["text"] == "ok n2999"


def test_logs_never_read_real_home(make_app, tmp_path, clock, monkeypatch):
    from haseeb_agent.dashboard import api
    assert api.LOGS_DIR == Path.home() / "Library" / "Logs" / "haseeb-agent"
    # the fixture app uses tmp logs_dir, so an empty tmp dir gives no lines regardless of the real logs
    client = stream_client(make_app(), tmp_path, clock)
    assert client.get("/api/logs").json() == []


# ---------------------------------------------------------------- score

def test_score_weeks(c, store):
    posted = replace(insight_item(status="posted"), edited=True)
    store.upsert_item(posted)
    store.upsert_item(replace(insight_item("insight-2026-10-12-1", status="posted", day=date(2026, 10, 13))))
    rows = c.get("/api/score?weeks=3").json()
    assert [r["week_start"] for r in rows] == ["2026-10-05", "2026-10-12", "2026-10-19"]
    assert rows[2]["posted"] == 1 and rows[2]["edited_then_posted"] == 1 and rows[2]["intervention_rate"] == 1.0
    assert rows[1]["posted"] == 1 and rows[0]["posted"] == 0 and rows[0]["intervention_rate"] is None
    assert len(c.get("/api/score").json()) == 8
    assert c.get("/api/score?weeks=0").status_code == 400
    assert c.get("/api/score?weeks=x").status_code == 400


def test_score_next_todo_within_five_minutes(c, store):
    """D16: a Next Todo is on time when it reached the huddle thread within 5 minutes of the huddle notes (the
    Slackbot notes message's ts, `notes:{todo id}`, from the todo job's _deliver_notes), or by 16:05 when that time
    isn't known (`todo_by_1605` counts those days). A workday with no todo item (no huddle) is never due; one with
    an item is due once its deadline has passed, or once it is out."""
    def todo(day, status, acted, notes=None):
        it = replace(todo_item(f"todo-{day.isoformat()}", status), day=day,
                     acted_at=datetime.combine(day, acted, tzinfo=KW) if acted else None)
        store.upsert_item(it)
        if notes:
            store.set_cursor(f"notes:{it.id}", f"{datetime.combine(day, notes, tzinfo=KW).timestamp():.6f}")
        return it
    mon = date(2026, 10, 12)
    todo(mon, "posted", dtime(15, 2), notes=dtime(14, 58))                      # 4 min after the notes: on time
    todo(mon + timedelta(days=1), "posted", dtime(15, 10), notes=dtime(15, 4))  # 6 min after: late (16:05 would pass it)
    own = todo(mon + timedelta(days=2), "skipped", dtime(15, 30))               # his own post, no notes time: by 16:05
    store.set_cursor(f"manual_todo:{own.id}", "2026-10-14T15:30:00+03:00")
    # Thursday: no todo item at all (no huddle that day): not due
    todo(mon + timedelta(days=4), "delivered", None, notes=dtime(15, 0))        # never posted: due, and missed
    todo(mon + timedelta(days=5), "posted", dtime(15, 0))                       # Saturday: not a workday
    rows = c.get("/api/score?weeks=2").json()
    assert (rows[0]["todo_on_time"], rows[0]["todo_due"], rows[0]["todo_by_1605"]) == (2, 4, 1)
    assert (rows[1]["todo_on_time"], rows[1]["todo_due"], rows[1]["todo_by_1605"]) == (0, 0, 0)  # Monday 10:00
    todo(date(2026, 10, 19), "delivered", None, notes=dtime(9, 50))  # the notes at 09:50: due from 09:55
    rows = c.get("/api/score?weeks=2").json()
    assert (rows[1]["todo_on_time"], rows[1]["todo_due"], rows[1]["todo_by_1605"]) == (0, 1, 0)
    store.mark("todo-2026-10-19", "posted", acted_at=datetime(2026, 10, 19, 9, 55, tzinfo=KW))  # right at 5 min
    assert c.get("/api/score?weeks=1").json()[0]["todo_on_time"] == 1


def test_score_pending_today(c, store, make_app, tmp_path):
    """M4: today, a workday, is pending (not missed) until its insight is posted or the day ends. The API says so
    (`pending_today`, this week only) and the page leaves it out; metrics.py (the weekly DM) is unchanged."""
    rows = c.get("/api/score?weeks=2").json()
    assert rows[1]["pending_today"] is True and rows[0]["pending_today"] is False
    assert rows[1]["missed_days"] == 1  # still a miss to metrics.py: the page subtracts it
    store.upsert_item(insight_item(status="posted"))  # today's insight goes out
    rows = c.get("/api/score?weeks=2").json()
    assert rows[1]["pending_today"] is False and rows[1]["insight_days"] == 1
    saturday = Clock(datetime(2026, 10, 24, 10, 0, tzinfo=KW).timestamp())
    client = stream_client(make_app(clock=saturday), tmp_path, saturday)
    assert client.get("/api/score?weeks=1").json()[0]["pending_today"] is False  # not a workday


# ---------------------------------------------------------------- review fixes

@pytest.mark.parametrize("since", ["-5", "abc", str(2**63), "9" * 30])
def test_stream_bad_since_400(c, since):
    assert c.get(f"/api/stream?since={since}").status_code == 400


def test_action_cancel_attach_ignore_shown_hash(c, store):
    t, i = todo_item(), insight_item()
    store.upsert_item(t)
    store.upsert_item(i)
    assert post_action(c, item_id=t.id, verb="cancel", shown_hash="x" * 19000).status_code == 200
    assert post_action(c, item_id=i.id, verb="attach", shown_hash="junk").status_code == 200
    assert [x.shown_hash for x in store.next_page_commands()] == ["", ""]


def test_action_body_cap(c, kicks):
    assert c.post("/api/action", content=b"x" * 5_000_000, headers={"content-type": "application/json"}).status_code == 400
    def chunks():  # no content-length: the stream is cut off at the cap
        for _ in range(10):
            yield b" " * 5000
    assert c.post("/api/action", content=chunks(), headers={"content-type": "application/json"}).status_code == 400
    assert kicks == []


def test_api_without_slash_is_json_404(c):
    r = c.get("/api")
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_weeks_floor_120_days(c, store):
    store.upsert_message(msg("1.1", day=date(1, 1, 1)))
    store.upsert_message(msg("1.2", day=date(2026, 10, 19)))
    w = c.get("/api/weeks").json()
    assert w[0]["week"] >= "2026-06-15" and len(w) <= 19 and w[-1]["week"] == "2026-10-19"


def test_search_person_kind_applied_in_store_and_week_only_when_explicit(c, store):
    for i in range(3):
        store.upsert_message(msg(f"{i + 1}.1", "pricing", user_id="U9", user_name="Zed", day=date(2026, 9, 1)))
    store.upsert_message(msg("9.1", "pricing", user_id="U1", kind="insight"))
    g = c.get("/api/history?q=pricing&person=U1&kind=insight").json()
    assert g["week"] is None and [m["text"] for x in g["groups"] for m in x["insights"] and [x["insights"][0]["msg"]]] == ["pricing"]
    old = c.get("/api/history?q=pricing&person=U9").json()["groups"]  # an old week still found without `week`
    assert old and old[0]["day"] == "2026-09-01"
    assert c.get("/api/history?q=pricing&person=U9&week=2026-10-19").json()["groups"] == []


def test_page_count_is_cached(tmp_path):
    from haseeb_agent.dashboard import api
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"/Type /Page /Type /Page")
    assert api._page_count(pdf) == 2
    assert api._page_count(pdf) == 2 and (str(pdf), pdf.stat().st_mtime) in api._PAGES


# ---------------------------------------------------------------- run controls (2026-10-20)

HOLDER = "U0A3S7LUZ41"
TS_A, TS_B = "1792000000.000100", "1792000100.000200"  # 10 digits, a dot, 6 digits


def hold_lock(settings, name):
    import fcntl
    path = Path(settings.db_path).parent / name
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")  # noqa: SIM115 - held open: the caller closes it to release the lock
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def run(c, job, **extra):
    return c.post("/api/run", json={"job": job, **extra})


def jobs_by_name(c):
    return {j["name"]: j for j in c.get("/api/jobs").json()["jobs"]}


def team_post(store, ts=TS_A, text="A teammate's post", user_id=HOLDER, user_name="Iqbal", day=None, **kw):
    store.upsert_message(msg(ts, text, user_id=user_id, user_name=user_name, day=day or date(2026, 10, 19), **kw))
    return ts


def test_jobs_empty_database(c):
    d = c.get("/api/jobs").json()
    assert set(d) == {"paused", "paused_at", "slack_identity", "compose_hold", "jobs"}
    assert (d["paused"], d["paused_at"], d["slack_identity"], d["compose_hold"]) == (False, None, None, False)
    assert [j["name"] for j in d["jobs"]] == ["tick", "insights", "decks", "weekly", "history_import", "enrich",
                                              "slack_check", "feedback"]
    assert all(set(j) == {"name", "running", "started_at", "last"} for j in d["jobs"])
    assert all(j["running"] is False and j["started_at"] is None and j["last"] is None for j in d["jobs"])


def test_jobs_say_when_the_composer_is_held_back_for_the_huddle(c, store):
    """A tick stamps the huddle hold every minute (cursor `message_held_at`); a fresh stamp means rows wait right now."""
    from haseeb_agent.commands import HELD_KEY
    assert c.get("/api/jobs").json()["compose_hold"] is False  # never held
    store.set_cursor(HELD_KEY, (NOW - timedelta(seconds=45)).isoformat())
    assert c.get("/api/jobs").json()["compose_hold"] is True
    store.set_cursor(HELD_KEY, (NOW - timedelta(minutes=3)).isoformat())  # no tick has held since: it is over
    assert c.get("/api/jobs").json()["compose_hold"] is False
    store.set_cursor(HELD_KEY, (NOW + timedelta(minutes=5)).isoformat())  # a stamp from the future is not a hold
    assert c.get("/api/jobs").json()["compose_hold"] is False
    store.set_cursor(HELD_KEY, "garbage")
    assert c.get("/api/jobs").json()["compose_hold"] is False


def test_jobs_report_the_pause_the_identity_and_each_last_run(c, store):
    controls.set_paused(store, True, NOW)
    controls.record_identity(store, HASEEB, True, NOW - timedelta(minutes=5), None)
    controls.record_last(store, "decks", NOW - timedelta(hours=1), False, "failed (DeckError)")
    d = c.get("/api/jobs").json()
    assert (d["paused"], d["paused_at"]) == (True, NOW.isoformat())
    assert d["slack_identity"] == {"user_id": HASEEB, "ok": True, "at": (NOW - timedelta(minutes=5)).isoformat(),
                                   "error": None, "stale": False}
    assert jobs_by_name(c)["decks"]["last"] == {"at": (NOW - timedelta(hours=1)).isoformat(), "ok": False,
                                                "summary": "failed (DeckError)"}
    assert jobs_by_name(c)["weekly"]["last"] is None


# -- M4: a Slack check that is too old to trust is "stale", and the page then says "check again"

@pytest.mark.parametrize("age,stale", [
    (timedelta(minutes=5), False), (timedelta(hours=5, minutes=59), False), (timedelta(hours=6), False),
    (timedelta(hours=6, seconds=1), True), (timedelta(days=2), True), (timedelta(seconds=-30), False),
])
def test_a_slack_check_older_than_six_hours_is_stale(c, store, age, stale):
    controls.record_identity(store, HASEEB, True, NOW - age, None)
    assert c.get("/api/jobs").json()["slack_identity"]["stale"] is stale


def test_a_slack_check_from_before_this_dashboard_started_is_stale(make_app, settings, store, tmp_path, clock):
    """A restart is how a changed setup is loaded: a check from before it says nothing about what runs now."""
    app = make_app(started_at=NOW.timestamp() - 600, login_dir=tmp_path / "login8")  # up for ten minutes
    client = login_as(app, tmp_path, clock, "login8")
    for age, stale in ((timedelta(minutes=9), False), (timedelta(minutes=11), True), (timedelta(hours=3), True)):
        controls.record_identity(store, HASEEB, True, NOW - age, None)
        assert client.get("/api/jobs").json()["slack_identity"]["stale"] is stale, age


@pytest.mark.parametrize("raw", [
    {"user_id": HASEEB, "ok": True, "at": "yesterday"}, {"user_id": HASEEB, "ok": True},
    {"user_id": HASEEB, "ok": True, "at": 7},
])
def test_a_slack_check_without_a_readable_time_is_stale(c, store, raw):
    store.set_cursor("slack_identity", json.dumps(raw))
    assert c.get("/api/jobs").json()["slack_identity"]["stale"] is True


@pytest.mark.parametrize("job,lock", [("tick", "tick.lock"), ("insights", "insights.lock"), ("decks", "decks.lock"),
                                      ("weekly", "weekly.lock"), ("history_import", "history_import.lock"),
                                      ("enrich", "enrich.lock"), ("slack_check", "slack_check.lock"),
                                      ("feedback", "feedback.lock")])
def test_a_held_lock_is_a_running_job(c, settings, job, lock):
    f = hold_lock(settings, lock)
    try:
        running = [n for n, j in jobs_by_name(c).items() if j["running"]]
    finally:
        f.close()
    assert running == [job]
    assert not any(j["running"] for j in jobs_by_name(c).values())


@pytest.mark.parametrize("job,argv", [
    ("insights", ["insights", "--force"]), ("decks", ["decks", "--retry"]), ("weekly", ["weekly"]),
    ("history_import", ["history-import"]), ("enrich", ["history-enrich"]), ("slack_check", ["slack-check"]),
])
def test_run_spawns_exactly_the_fixed_command(c, store, spawned, kicks, clock, job, argv):
    r = run(c, job)
    assert r.status_code == 200 and r.json() == {"ok": True, "job": job, "started_at": NOW.isoformat()}
    assert spawned == [argv] and kicks == []
    j = jobs_by_name(c)[job]  # the spawn is recorded first, so it shows as running before it holds its lock
    assert j["running"] is True and j["started_at"] == NOW.isoformat()
    assert [n for n, x in jobs_by_name(c).items() if x["running"]] == [job]


def test_run_tick_uses_the_existing_kick_not_a_spawn(c, spawned, kicks):
    r = run(c, "tick")
    assert r.status_code == 200 and r.json()["job"] == "tick"
    assert kicks == [1] and spawned == []
    assert jobs_by_name(c)["tick"]["running"] is True  # between the kick and the tick's own lock


def test_check_round_answers_409_when_launchctl_did_not_kick_the_tick(settings, clock, tmp_path, kicks):
    """M3: launchctl exits non-zero when the tick is not loaded in launchd (113). Nothing ran, so the control must not
    show "Running" for 2 minutes: the spawn record is taken back and the answer says why."""
    code = {"rc": 113}

    def kick():
        kicks.append(code["rc"])
        return code["rc"]
    app = create_app(settings, lambda: Store(settings.db_path), KEY, kick, clock=clock,
                     login_dir=tmp_path / "login6", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks")
    client = login_as(app, tmp_path, clock, "login6")
    r = run(client, "tick")
    assert r.status_code == 409 and r.json() == {"error": "launchd did not start the tick"}
    assert kicks == [113] and jobs_by_name(client)["tick"]["running"] is False
    st = Store(settings.db_path)
    assert controls.read_spawned(st, "tick") is None  # the record is gone: the next click is not "already running"
    st.close()
    code["rc"] = 0  # the tick gets loaded; the same click now works, and shows running
    r = run(client, "tick")
    assert r.status_code == 200 and jobs_by_name(client)["tick"]["running"] is True


def test_a_page_click_keeps_its_fire_and_forget_kick(settings, clock, tmp_path):
    """The kick of a page click (/api/action) ignores launchctl's code: the queued command is run by the next tick."""
    app = create_app(settings, lambda: Store(settings.db_path), KEY, lambda: 113, clock=clock,
                     login_dir=tmp_path / "login7", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks")
    client = login_as(app, tmp_path, clock, "login7")
    st = Store(settings.db_path)
    st.upsert_item(insight_item())
    st.close()
    r = client.post("/api/action", json={"item_id": "insight-2026-10-19-1", "verb": "skip", "shown_hash": "0" * 64})
    assert r.status_code == 200 and "command_id" in r.json()


def test_a_second_request_gets_409_with_the_jobs_state(c, spawned, clock):
    assert run(c, "weekly").status_code == 200
    clock.t += 30
    r = run(c, "weekly")
    assert r.status_code == 409
    assert r.json() == {"error": "already running", "job": "weekly", "running": True,
                        "started_at": NOW.isoformat(), "last": None}
    assert spawned == [["weekly"]]  # not started twice
    assert run(c, "slack_check").status_code == 200  # other jobs are not held up by it


def test_the_two_minute_window_ends_and_so_does_a_finished_run(c, store, spawned, clock):
    run(c, "weekly")
    clock.t += 125  # the process never took its lock (it failed to start): after 2 minutes it is free again
    assert jobs_by_name(c)["weekly"]["running"] is False
    assert run(c, "weekly").status_code == 200 and len(spawned) == 2
    controls.record_last(store, "weekly", NOW + timedelta(seconds=140), True, "done")  # it ended 15 s after the spawn
    clock.t += 15
    j = jobs_by_name(c)["weekly"]
    assert j["running"] is False and j["last"]["summary"] == "done"
    assert run(c, "weekly").status_code == 200 and len(spawned) == 3


@pytest.mark.parametrize("job,lock", [("insights", "insights.lock"), ("decks", "decks.lock"),
                                      ("enrich", "enrich.lock"), ("history_import", "history_import.lock")])
def test_a_job_whose_lock_is_held_is_refused_and_not_spawned(c, settings, spawned, job, lock):
    f = hold_lock(settings, lock)
    try:
        r = run(c, job)
    finally:
        f.close()
    assert r.status_code == 409 and r.json()["error"] == "already running" and r.json()["running"] is True
    assert spawned == []
    assert run(c, job).status_code == 200  # free again once it ended


def test_the_tick_lock_blocks_the_check_round_but_the_import_has_its_own_lock(c, settings, spawned, kicks):
    """history-import takes the tick lock too, but the dashboard tells the two apart by the import's own lock."""
    f = hold_lock(settings, "tick.lock")
    try:
        assert run(c, "tick").status_code == 409 and kicks == []
        assert run(c, "history_import").status_code == 200  # it will say "a tick is running" itself
    finally:
        f.close()
    assert spawned == [["history-import"]]


def test_the_import_holding_the_tick_lock_shows_as_the_import_running(c, settings):
    f1, f2 = hold_lock(settings, "history_import.lock"), hold_lock(settings, "tick.lock")
    try:
        assert {n for n, j in jobs_by_name(c).items() if j["running"]} == {"history_import", "tick"}
    finally:
        f1.close()
        f2.close()


@pytest.mark.parametrize("body", [
    {}, {"job": None}, {"job": 5}, {"job": ["weekly"]}, {"job": "nope"}, {"job": "WEEKLY"}, {"job": "weekly "},
    {"job": "weekly; rm -rf ~"}, {"job": "../../bin/sh"}, {"job": "weekly", "args": ["--evil"]},
    {"job": "weekly", "argv": ["x"]}, {"job": "insights", "force": False}, {"job": "weekly", "ts": TS_A},
    {"job": "tick", "ts": TS_A}, {"job": "weekly", "ts": None, "x": 1}, {"ts": TS_A},
])
def test_run_refuses_anything_off_the_allowlist(c, spawned, kicks, body):
    r = c.post("/api/run", json=body)
    assert r.status_code == 400 and set(r.json()) == {"error"}
    assert spawned == [] and kicks == []


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'"weekly"', b"null", b"", b"{" + b" " * 30000 + b"}"])
def test_run_refuses_garbage_bodies(c, spawned, kicks, raw):
    assert c.post("/api/run", content=raw).status_code == 400
    assert spawned == [] and kicks == []


def test_run_does_not_echo_what_it_was_sent(c):
    r = run(c, "SECRET-job-name")
    assert r.status_code == 400 and "SECRET" not in r.text


# -- Draft feedback: the one client-supplied value is a post's ts ------------------------------------------

def test_feedback_run_spawns_with_the_ts_and_names_the_post_it_drafts(c, store, spawned):
    team_post(store)
    r = run(c, "feedback", ts=TS_A)
    assert r.status_code == 200 and spawned == [["feedback", "--ts", TS_A]]
    post = c.get("/api/teamposts").json()[0]
    assert post["ts"] == TS_A and post["feedback"] == "drafting"  # the list shows it while the job runs
    assert run(c, "feedback", ts=TS_A).status_code == 409


def test_one_draft_at_a_time(c, store, spawned):
    team_post(store, TS_A)
    team_post(store, TS_B)
    assert run(c, "feedback", ts=TS_A).status_code == 200
    r = run(c, "feedback", ts=TS_B)
    assert r.status_code == 409 and r.json()["error"] == "already running"
    assert spawned == [["feedback", "--ts", TS_A]]


@pytest.mark.parametrize("ts", [
    "", "abc", "1792000000", "1792000000.0001", "179200000.000100", "17920000000.000100", "1792000000.000100\n",
    " 1792000000.000100", "1792000000.000100 ", "1792000000,000100", "-1792000000.000100", "1792000000.000100;x",
    "1792000000.000100/../x", "１７９２０００００００.000100", 1792000000.0001, ["1792000000.000100"], {"ts": TS_A},
    True,
])
def test_feedback_run_needs_a_well_formed_ts(c, store, spawned, ts):
    team_post(store)
    r = run(c, "feedback", ts=ts)
    assert r.status_code == 400 and r.json() == {"error": "bad ts"}
    assert spawned == []


def test_feedback_run_without_a_ts_is_refused(c, store, spawned):
    team_post(store)
    assert run(c, "feedback").status_code == 400 and run(c, "feedback", ts=None).status_code == 400
    assert spawned == []


def test_feedback_run_refuses_a_post_the_store_does_not_know(c, spawned):
    r = run(c, "feedback", ts=TS_A)
    assert r.status_code == 404 and r.json() == {"error": "unknown post"} and spawned == []


def test_feedback_run_refuses_a_post_in_another_channel(c, store, spawned):
    team_post(store, channel="C0OTHER")
    assert run(c, "feedback", ts=TS_A).status_code == 404 and spawned == []


@pytest.mark.parametrize("kw,why", [
    (dict(user_id=HASEEB, user_name="Haseeb"), "your own post"),
    (dict(user_id="USLACKBOT", user_name="Slackbot"), "not a teammate's post"),
    (dict(thread_ts="1791999999.000001"), "a reply, not a post"),
    (dict(kind="huddle_notes"), "huddle notes, not a post"),
])
def test_feedback_run_refuses_what_is_not_a_teammates_post(c, store, spawned, kw, why):
    team_post(store, **kw)
    r = run(c, "feedback", ts=TS_A)
    assert r.status_code == 400 and r.json() == {"error": why}
    assert spawned == []


def test_feedback_run_goes_by_the_kind_the_lists_show_labels_included(c, store, spawned):
    """M5: Haseeb's own label over the classifier's kind decides, in the lists and in the refusal alike."""
    team_post(store, TS_A, "was huddle notes to the classifier", kind="huddle_notes")
    team_post(store, TS_B, "was an ordinary post to the classifier")
    assert run(c, "feedback", ts=TS_A).json() == {"error": "huddle notes, not a post"}  # no label: the classifier's
    store.set_label(CH, TS_A, "other")  # he says it is not huddle notes
    store.set_label(CH, TS_B, "huddle_notes")  # and the other one is
    assert run(c, "feedback", ts=TS_B).json() == {"error": "huddle notes, not a post"}
    assert [p["text"] for p in c.get("/api/teamposts").json()] == ["was huddle notes to the classifier"]
    assert run(c, "feedback", ts=TS_A).status_code == 200 and spawned == [["feedback", "--ts", TS_A]]


def test_a_channel_join_notice_is_refused_and_not_listed(c, store, spawned):
    team_post(store, TS_A, "<@U0A3S7LUZ41> has joined the channel")
    team_post(store, TS_B, "A real post")
    assert run(c, "feedback", ts=TS_A).json() == {"error": "a channel-join notice, not a post"}
    assert spawned == []
    assert [p["text"] for p in c.get("/api/teamposts").json()] == ["A real post"]


def test_feedback_run_refuses_a_post_that_already_has_a_draft(c, store, spawned):
    team_post(store)
    store.upsert_item(replace(insight_item(f"feedback-{TS_A}", "delivered"), kind="feedback", body="draft"))
    r = run(c, "feedback", ts=TS_A)
    assert r.status_code == 409 and r.json() == {"error": "it already has a feedback draft", "state": "delivered"}
    assert spawned == []


def claim(store, ts, minutes_ago):
    """A run's claim on a post (jobs.feedback.claim_key), made that long before the API's clock."""
    store.set_cursor(f"feedback_claim:{ts}", (NOW - timedelta(minutes=minutes_ago)).isoformat())


def test_feedback_run_refuses_a_post_a_run_is_drafting_right_now(c, store, spawned):
    """C1: the tick's own scan claims a post while it drafts it; a click on the same post would only be refused by the
    job, so the API says so at once and starts nothing."""
    team_post(store)
    claim(store, TS_A, 3)
    r = run(c, "feedback", ts=TS_A)
    assert r.status_code == 409 and r.json() == {"error": "a draft is already being written", "state": "drafting"}
    assert spawned == [] and controls.read_spawned(store, "feedback") is None  # nothing was spawned or recorded


def test_feedback_run_takes_a_stale_claim_as_gone(c, store, spawned):
    team_post(store)
    claim(store, TS_A, 25)  # the claimer died
    assert run(c, "feedback", ts=TS_A).status_code == 200 and spawned == [["feedback", "--ts", TS_A]]


def test_the_parent_of_a_thread_is_a_post(c, store, spawned):
    team_post(store, thread_ts=TS_A)
    assert run(c, "feedback", ts=TS_A).status_code == 200


# -- failures ------------------------------------------------------------------------------------------------

def login_as(app, tmp_path, clock, name):
    client, token = do_login(app, tmp_path / name, clock)
    client.headers.update({"X-Session-Token": token, "Origin": BASE})
    return client


def test_a_spawn_that_fails_is_a_500_and_leaves_nothing_running(make_app, clock, tmp_path):
    def broken(args):
        raise OSError("fork failed")
    client = login_as(make_app(spawn=broken, login_dir=tmp_path / "login5"), tmp_path, clock, "login5")
    r = run(client, "weekly")
    assert r.status_code == 500 and r.json() == {"error": "could not start"}
    assert jobs_by_name(client)["weekly"]["running"] is False  # the record of the spawn was taken back


def test_a_kick_that_fails_is_a_500_too(settings, clock, tmp_path):
    def broken():
        raise OSError("launchctl")
    app = create_app(settings, lambda: Store(settings.db_path), KEY, broken, clock=clock,
                     login_dir=tmp_path / "login2", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks")
    client = login_as(app, tmp_path, clock, "login2")
    r = run(client, "tick")
    assert r.status_code == 500 and jobs_by_name(client)["tick"]["running"] is False


def test_the_default_spawn_is_blocked_by_the_offline_guard(settings, clock, tmp_path, monkeypatch):
    """No test reaches the real spawn: the default one runs cli.spawn_job, which checks the guard first."""
    import subprocess
    from haseeb_agent.dashboard.api import default_spawn
    from haseeb_agent.offline import OfflineError
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(OfflineError):
        default_spawn(["weekly"])
    app = create_app(settings, lambda: Store(settings.db_path), KEY, lambda: None, clock=clock,
                     login_dir=tmp_path / "login3", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks")
    r = run(login_as(app, tmp_path, clock, "login3"), "weekly")  # the app's own default spawn
    assert r.status_code == 500 and ran == []


def test_decks_turned_off_in_the_config_is_refused(settings, tmp_path, clock, spawned):
    app = create_app(replace(settings, decks_enabled=False), lambda: Store(settings.db_path), KEY, lambda: None,
                     clock=clock, login_dir=tmp_path / "login4", logs_dir=tmp_path / "logs", decks_dir=tmp_path / "decks",
                     spawn=lambda a: spawned.append(a))
    r = run(login_as(app, tmp_path, clock, "login4"), "decks")
    assert r.status_code == 409 and "turned off" in r.json()["error"] and spawned == []


def test_concurrent_requests_start_a_job_once(settings, clock):
    """Two clicks in two tabs, at the same moment: one process."""
    import threading
    import time
    from haseeb_agent.dashboard.api import ApiConfig, run_job
    started = []

    def slow_spawn(args):
        time.sleep(0.05)  # the window in which a second request could pass the check
        started.append(args)
    cfg = ApiConfig(settings, lambda: Store(settings.db_path), lambda: None, clock, spawn=slow_spawn)
    statuses = []
    body = json.dumps({"job": "weekly"}).encode()
    threads = [threading.Thread(target=lambda: statuses.append(run_job(cfg, body).status_code)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200] + [409] * 7 and started == [["weekly"]]


# -- Pause ----------------------------------------------------------------------------------------------------

def test_pause_and_resume(c, store):
    r = c.post("/api/pause", json={"paused": True})
    assert r.status_code == 200 and r.json() == {"paused": True, "paused_at": NOW.isoformat()}
    assert store.get_cursor("agent_paused") == NOW.isoformat()
    d = c.get("/api/jobs").json()
    assert (d["paused"], d["paused_at"]) == (True, NOW.isoformat())
    r = c.post("/api/pause", json={"paused": True})  # again: the first time stays
    assert r.json() == {"paused": True, "paused_at": NOW.isoformat()}
    assert c.post("/api/pause", json={"paused": False}).json() == {"paused": False, "paused_at": None}
    assert c.get("/api/jobs").json()["paused"] is False and store.get_cursor("agent_paused") == ""
    assert c.post("/api/pause", json={"paused": False}).json() == {"paused": False, "paused_at": None}


@pytest.mark.parametrize("body", [{}, {"paused": "true"}, {"paused": 1}, {"paused": None}, {"paused": [True]},
                                  {"paused": True, "extra": 1}, {"Paused": True}, []])
def test_pause_needs_a_real_boolean(c, store, body):
    assert c.post("/api/pause", json=body).status_code == 400
    assert controls.paused_since(store) is None


def test_pause_garbage_body(c):
    assert c.post("/api/pause", content=b"nope").status_code == 400


def test_everything_but_resume_and_check_slack_answers_409_paused(c, store, spawned, kicks):
    it = insight_item()
    store.upsert_item(it)
    team_post(store)
    c.post("/api/pause", json={"paused": True})
    want = {"error": "paused", "paused_at": NOW.isoformat()}
    for job in ("tick", "insights", "decks", "weekly", "history_import", "enrich"):
        r = run(c, job)
        assert r.status_code == 409 and r.json() == want, job
    r = run(c, "feedback", ts=TS_A)
    assert r.status_code == 409 and r.json() == want
    r = post_action(c, item_id=it.id, verb="post", shown_hash=body_hash(it))
    assert r.status_code == 409 and r.json() == want
    assert spawned == [] and kicks == [] and store.next_page_commands() == []
    # a refusal for a bad request still comes first as a 400, and the unknown job is not a "paused" either
    assert run(c, "nope").status_code == 400
    # Check Slack account only reads, so it still runs, and so does Resume
    assert run(c, "slack_check").status_code == 200 and spawned == [["slack-check"]]
    assert c.post("/api/pause", json={"paused": False}).status_code == 200
    assert post_action(c, item_id=it.id, verb="post", shown_hash=body_hash(it)).status_code == 200
    assert run(c, "weekly").status_code == 200


def test_labels_are_local_so_a_pause_does_not_stop_them(c, store):
    store.upsert_message(msg("1.1", kind="other"))
    c.post("/api/pause", json={"paused": True})
    assert c.post("/api/label", json={"channel": CH, "ts": "1.1", "kind": "insight"}).json() == {"ok": True}


# -- N1: Pause and Resume themselves expire the clicks queued before the pause ---------------------------------------

def test_pausing_expires_the_clicks_queued_before_it_at_once(c, store):
    """No paused tick has to run first: a click queued while a long tick was busy would otherwise wake up after a short
    pause. One created after the pause time (a click in flight when it landed) is left to the tick."""
    it = insight_item()
    store.upsert_item(it)
    before = store.enqueue_page_command(it.id, "post", None, body_hash(it), NOW - timedelta(minutes=1))
    after = store.enqueue_page_command(it.id, "skip", None, body_hash(it), NOW + timedelta(seconds=1))
    assert c.post("/api/pause", json={"paused": True}).json() == {"paused": True, "paused_at": NOW.isoformat()}
    expired = store.page_command(before)
    assert (expired.state, expired.result) == ("failed", "expired: queued before the pause")
    assert store.page_command(after).state == "queued"
    # the card shows the failure it is, and a Resume before any tick ran leaves only the click made after the pause time
    assert c.get(f"/api/command/{before}").json()["result"] == "expired: queued before the pause"
    assert c.post("/api/pause", json={"paused": False}).status_code == 200
    assert [x.id for x in store.next_page_commands()] == [after]


def test_resuming_expires_what_was_queued_before_a_pause_set_by_hand(c, store):
    """The CLI or the documented one-liner can set the pause too, and no paused tick may have run: Resume expires the
    clicks queued before it, before the cursor is cleared."""
    it = insight_item()
    store.upsert_item(it)
    controls.set_paused(store, True, NOW - timedelta(minutes=10))
    older = store.enqueue_page_command(it.id, "post", None, body_hash(it), NOW - timedelta(minutes=20))
    newer = store.enqueue_page_command(it.id, "skip", None, body_hash(it), NOW - timedelta(minutes=5))
    assert c.post("/api/pause", json={"paused": False}).json() == {"paused": False, "paused_at": None}
    assert (store.page_command(older).state, store.page_command(older).result) == (
        "failed", "expired: queued before the pause")
    assert store.page_command(newer).state == "queued" and controls.paused_since(store) is None


def test_a_pause_with_no_readable_time_expires_every_queued_click_on_resume(c, store):
    it = insight_item()
    store.upsert_item(it)
    store.set_cursor("agent_paused", "garbage")  # any non-empty value counts as paused; its time cannot be told
    cid = store.enqueue_page_command(it.id, "post", None, body_hash(it), NOW - timedelta(seconds=1))
    assert c.post("/api/pause", json={"paused": False}).status_code == 200
    assert store.page_command(cid).state == "failed"


def test_resuming_when_not_paused_touches_no_click(c, store):
    it = insight_item()
    store.upsert_item(it)
    cid = store.enqueue_page_command(it.id, "post", None, body_hash(it), NOW - timedelta(minutes=1))
    assert c.post("/api/pause", json={"paused": False}).json() == {"paused": False, "paused_at": None}
    assert store.page_command(cid).state == "queued"


def test_the_dashboard_leaves_the_autopost_to_the_paused_tick(c, store):
    """The dashboard writes no todo state: the auto-post is cancelled by the paused tick (cli.pause_housekeeping)."""
    store.upsert_item(todo_item())
    due = (NOW + timedelta(minutes=5)).isoformat()
    store.set_cursor("autopost:todo-2026-10-19", due)
    c.post("/api/pause", json={"paused": True})
    assert store.get_cursor("autopost:todo-2026-10-19") == due and store.get_cursor("todo_notice:todo-2026-10-19") is None


def test_a_pause_stays_on_if_the_expiry_fails_and_a_resume_then_stays_paused(c, store, monkeypatch):
    from haseeb_agent.dashboard import api as api_mod
    real = api_mod.expire_for_pause

    def broken(*args, **kwargs):
        raise OSError("database is locked")
    monkeypatch.setattr(api_mod, "expire_for_pause", broken)
    assert c.post("/api/pause", json={"paused": True}).json() == {"paused": True, "paused_at": NOW.isoformat()}
    r = c.post("/api/pause", json={"paused": False})  # armed clicks must not outlive the pause: it stays on
    assert r.status_code == 500 and r.json() == {"error": "could not resume"} and controls.paused_since(store) is not None
    monkeypatch.setattr(api_mod, "expire_for_pause", real)  # the database is fine again: Resume works
    assert c.post("/api/pause", json={"paused": False}).json() == {"paused": False, "paused_at": None}


# -- Team posts -----------------------------------------------------------------------------------------------

def test_teamposts_empty(c):
    assert c.get("/api/teamposts").json() == []


def test_teamposts_are_teammates_top_level_posts_of_the_last_seven_days(c, store, settings):
    store.upsert_person(HOLDER, "Iqbal")
    today = date(2026, 10, 19)
    team_post(store, "1792000300.000300", "newest", day=today)
    team_post(store, "1791999000.000100", "six days ago", day=today - timedelta(days=6))
    team_post(store, "1791990000.000100", "seven days ago", day=today - timedelta(days=7))  # out
    team_post(store, "1792000200.000100", "hello", user_id=HASEEB, user_name="Haseeb")  # his own
    team_post(store, "1792000250.000100", "notes", user_id="USLACKBOT", user_name="Slackbot")
    team_post(store, "1792000260.000100", "a reply", thread_ts="1792000000.000100")
    team_post(store, "1792000270.000100", "huddle", kind="huddle_notes")
    team_post(store, "1792000280.000100", "elsewhere", channel="C0OTHER")
    team_post(store, "1792000290.000100", "a thread parent", thread_ts="1792000290.000100", user_id="U0B2C3D4E5F",
              user_name="Bea")
    posts = c.get("/api/teamposts").json()
    assert [p["text"] for p in posts] == ["newest", "a thread parent", "six days ago"]  # newest first
    first = posts[0]
    assert set(first) == {"ts", "channel", "user_id", "user_name", "color", "text", "day", "permalink", "feedback"}
    assert (first["ts"], first["channel"], first["user_id"], first["user_name"], first["day"], first["feedback"]) == (
        "1792000300.000300", CH, HOLDER, "Iqbal", "2026-10-19", "none")
    assert first["permalink"] == f"{settings.workspace_url}/archives/{CH}/p1792000300000300"  # from ids only


def test_teamposts_show_each_posts_feedback_state(c, store):
    ts = {s: team_post(store, f"17920{i}0000.000100", s) for i, s in enumerate(["none", "delivered", "posted", "skipped"])}
    for status in ("delivered", "posted", "skipped"):
        store.upsert_item(replace(insight_item(f"feedback-{ts[status]}", status), kind="feedback", body="d"))
    states = {p["text"]: p["feedback"] for p in c.get("/api/teamposts").json()}
    assert states == {"none": "none", "delivered": "delivered", "posted": "posted", "skipped": "skipped"}


def test_teamposts_say_drafting_for_a_post_a_run_has_claimed(c, store):
    """C1: whichever path drafts a post (the tick's scan or a dashboard click) holds its claim, so the list shows it
    as being drafted and the page offers no second draft. A claim that went stale is gone; an item wins over a claim."""
    held, dead, done, free = (team_post(store, f"17920{i}0000.000100", name) for i, name in
                              enumerate(["held", "dead", "done", "free"]))
    claim(store, held, 3)
    claim(store, dead, 21)
    claim(store, done, 1)
    store.upsert_item(replace(insight_item(f"feedback-{done}", "delivered"), kind="feedback", body="d"))
    states = {p["text"]: p["feedback"] for p in c.get("/api/teamposts").json()}
    assert states == {"held": "drafting", "dead": "none", "done": "delivered", "free": "none"}
    store.set_cursor(f"feedback_claim:{held}", "")  # given back
    assert {p["text"]: p["feedback"] for p in c.get("/api/teamposts").json()}["held"] == "none"


def test_teamposts_text_is_data_and_capped(c, store):
    hostile = "<img src=x onerror=alert(1)> " + "x" * 800
    team_post(store, text=hostile)
    (post,) = c.get("/api/teamposts").json()
    assert post["text"] == hostile[:500]


def test_teamposts_at_most_100(c, store):
    for i in range(130):
        team_post(store, f"{1792000000 + i}.000100", f"post {i}")
    posts = c.get("/api/teamposts").json()
    assert len(posts) == 100 and posts[0]["text"] == "post 129"


# -- History carries the same state, for the button on a post --------------------------------------------------

def test_history_marks_the_posts_that_can_get_a_draft(c, store):
    ann = team_post(store, "1792000300.000100", "Daily insight pricing", kind="insight", user_id="U1", user_name="Ann")
    team_post(store, "1792000200.000100", "mine", user_id=HASEEB, user_name="Haseeb", kind="insight")
    team_post(store, "1792000250.000100", "reply", thread_ts=ann, kind="feedback", user_id="U2", user_name="Bob")
    store.upsert_item(replace(insight_item(f"feedback-{ann}", "delivered"), kind="feedback", body="d"))
    g = c.get("/api/history?view=person&week=2026-10-19").json()["groups"]
    by = {m["text"]: m["feedback"] for grp in g for m in grp["messages"]}
    assert by == {"Daily insight pricing": "delivered", "mine": None, "reply": None}
    day = c.get("/api/history?view=day&week=2026-10-19").json()["groups"][0]
    cards = {x["msg"]["text"]: x for x in day["insights"]}
    assert cards["Daily insight pricing"]["msg"]["feedback"] == "delivered" and cards["mine"]["msg"]["feedback"] is None
    assert cards["Daily insight pricing"]["feedback"][0]["feedback"] is None  # a reply never gets one


def test_history_says_none_for_a_post_without_a_draft_and_drafting_while_one_runs(c, store, spawned):
    team_post(store, kind="insight")
    msg_of = lambda: c.get("/api/history?view=person&week=2026-10-19").json()["groups"][0]["messages"][0]  # noqa: E731
    assert msg_of()["feedback"] == "none"
    run(c, "feedback", ts=TS_A)
    assert msg_of()["feedback"] == "drafting"


# ---------------------------------------------------------------- the composer: POST /api/compose, and message cards

def compose(c, text="standup moved to 3pm pls update ur notes", **extra):
    return c.post("/api/compose", json={"text": text, **extra})


def test_compose_queues_a_compose_command_and_kicks_the_tick(c, store, kicks):
    r = compose(c)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"command_id", "item_id"} and re.fullmatch(r"message-\d{10}\.\d{6}", body["item_id"])
    cmd = store.page_command(body["command_id"])
    assert (cmd.item_id, cmd.verb, cmd.arg, cmd.shown_hash, cmd.state) == (
        body["item_id"], "compose", "standup moved to 3pm pls update ur notes", "", "queued")
    assert len(kicks) == 1
    assert store.get_item(body["item_id"]) is None  # the tick makes the item; the dashboard makes nothing
    assert c.get(f"/api/command/{cmd.id}").json() == {"id": cmd.id, "item_id": body["item_id"], "verb": "compose",
                                                      "state": "queued", "result": None}


def test_compose_makes_no_model_or_slack_call(c, store, monkeypatch):
    """The dashboard only queues: the offline guard would refuse any real call, and a fake one would be seen."""
    import haseeb_agent.litellm as litellm
    import haseeb_agent.slack_gateway as gateway
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a real call from the dashboard"))  # noqa: E731
    monkeypatch.setattr(litellm.LLM, "complete", boom)
    monkeypatch.setattr(gateway.ClaudeConnectorSlack, "send", boom)
    monkeypatch.setattr(gateway.ClaudeConnectorSlack, "send_dm", boom, raising=False)
    assert compose(c).status_code == 200


def test_compose_cleans_the_text_the_way_the_tick_will_read_it(c, store):
    r = compose(c, "  line one\r\nline\x00 two\u202e\n\n\n\n<!channel> and <!here|here>  ")
    assert r.status_code == 200
    assert store.page_command(r.json()["command_id"]).arg == "line one\nline two\n\n@channel and @here"


@pytest.mark.parametrize("text", ["", "   ", "\n\t\n", "\u200b\u200b", "\x00\x01"])
def test_compose_refuses_an_empty_message(c, store, kicks, text):
    r = compose(c, text)
    assert r.status_code == 400 and r.json() == {"error": "empty message"}
    assert kicks == [] and store.next_page_commands() == []


def test_compose_takes_2000_characters_and_refuses_one_more_without_echoing_it(c, store, kicks):
    assert compose(c, "x" * 2000).status_code == 200
    secret = "SECRET" * 400 + "!"
    r = compose(c, secret)
    assert r.status_code == 400 and r.json() == {"error": "too long"} and "SECRET" not in r.text
    assert len(store.next_page_commands()) == 1 and len(kicks) == 1
    # emoji count once each, as people (and the tick) count them
    assert compose(c, "\U0001f600" * 2000).status_code == 200
    assert compose(c, "\U0001f600" * 2001).status_code == 400


@pytest.mark.parametrize("body", [{}, {"text": 5}, {"text": None}, {"text": ["x"]}, {"txt": "x"},
                                  {"text": "x", "item_id": "message-1"}, {"text": "x", "verb": "post"}, []])
def test_compose_refuses_a_malformed_request(c, store, kicks, body):
    r = c.post("/api/compose", json=body)
    assert r.status_code == 400 and r.json() == {"error": "bad request"}
    assert kicks == [] and store.next_page_commands() == []


@pytest.mark.parametrize("raw", [b"not json", b"", b'{"text": "x"', b"{" + b" " * 30000 + b"}"])
def test_compose_refuses_garbage_and_oversized_bodies(c, store, kicks, raw):
    assert c.post("/api/compose", content=raw).status_code == 400
    assert kicks == [] and store.next_page_commands() == []


def test_compose_is_refused_while_the_agent_is_paused(c, store, kicks):
    c.post("/api/pause", json={"paused": True})
    r = compose(c)
    assert r.status_code == 409 and r.json() == {"error": "paused", "paused_at": NOW.isoformat()}
    assert kicks == [] and store.next_page_commands() == []
    c.post("/api/pause", json={"paused": False})
    assert compose(c).status_code == 200


def test_a_second_click_on_the_same_text_follows_the_first_command(c, store, kicks):
    first = compose(c, "same words").json()
    again = compose(c, "same words  ")  # the same text once cleaned
    assert again.status_code == 409
    assert again.json() == {"error": "already pending", "command_id": first["command_id"], "verb": "compose",
                            "state": "queued"}
    store.set_page_command(first["command_id"], "running")
    assert compose(c, "same words").json()["state"] == "running"
    other = compose(c, "other words")  # different text is a different message
    assert other.status_code == 200 and other.json()["command_id"] != first["command_id"]
    assert len(kicks) == 2
    store.set_page_command(first["command_id"], "failed", "⚠️ no")
    assert compose(c, "same words").status_code == 200  # settled: free again


def test_compose_ids_differ_per_click(c, clock):
    a = compose(c, "one").json()["item_id"]
    clock.t += 0.001
    b = compose(c, "two").json()["item_id"]
    assert a != b


def test_two_clicks_in_the_same_microsecond_are_two_messages_with_two_ids(c, store):
    """The clock is frozen here: the second id is bumped, never shared (the tick refuses a repeated id)."""
    a = compose(c, "one").json()
    b = compose(c, "two").json()
    assert a["item_id"] != b["item_id"] and a["command_id"] != b["command_id"]
    for made in (a, b):  # the tick made both items; an id that already has an item is skipped too
        store.upsert_item(message_item(made["item_id"]))
        store.set_page_command(made["command_id"], "done", "ok")
    third = compose(c, "three").json()["item_id"]
    assert third not in (a["item_id"], b["item_id"])


def test_a_message_item_reaches_the_inbox_with_what_changed_and_his_own_words(c, store):
    it = message_item()
    store.upsert_item(it)
    store.set_cursor(f"message_note:{it.id}", "Fixed grammar and made it shorter.")
    store.set_cursor(f"message_raw:{it.id}", "standup moved to 3pm pls update ur notes")
    store.upsert_item(todo_item())
    rows = {r["item_id"]: r for r in c.get("/api/inbox").json()}
    r = rows[it.id]
    assert (r["kind"], r["title"], r["body"], r["status"]) == ("message", "Message to the channel",
                                                               "Standup moved to 3 pm.", "delivered")
    assert r["shown_hash"] == body_hash(it)
    assert r["note"] == "Fixed grammar and made it shorter." and r["original"] == "standup moved to 3pm pls update ur notes"
    assert r["pings"] == [] and r["autopost_at"] is None and r["pending"] is None
    other = rows["todo-2026-10-19"]
    assert other["note"] is None and other["original"] is None and other["pings"] == []


def test_a_message_item_without_its_cursors_still_lists(c, store):
    store.upsert_item(message_item())
    (r,) = c.get("/api/inbox").json()
    assert r["note"] is None and r["original"] is None and r["pings"] == []


@pytest.mark.parametrize("raw,body,want", [
    ("@channel standup at 3", "@channel Standup at 3.", ["channel"]),
    ("<!channel> standup at 3", "@channel Standup at 3.", []),            # the cursor holds what normalize_raw left: text
    ("see https://x.com/@channel", "See https://x.com/@channel.", []),     # inside his link, not a mention
    ("@here quick one", "@here Quick one.", ["here"]),
    ("@channel @here both", "@here @channel Both.", ["channel", "here"]),
    ("standup at 3", "@channel Standup at 3.", []),                        # the model added it: not a ping
    ("standup at 3", "@here @channel Standup at 3.", []),
    ("@here hi", "@here Hi. @channel", ["here"]),                          # only the one he typed
    ("@channel hi", "Hi, all.", []),                                       # he typed it but the draft dropped it
    ("@everyone hi", "@everyone Hi.", []),                                 # never allowed
])
def test_the_inbox_says_which_mass_mentions_a_post_would_really_send(c, store, raw, body, want):
    it = message_item(body=body)
    store.upsert_item(it)
    store.set_cursor(f"message_raw:{it.id}", raw)
    assert c.get("/api/inbox").json()[0]["pings"] == want


def test_the_composers_own_row_is_not_a_pending_command_of_its_card(c, store):
    """The row that made the item ends a moment after the item exists; the card must not show it as working."""
    it = message_item()
    store.upsert_item(it)
    cid = store.enqueue_page_command(it.id, "compose", "rough", "", NOW)
    store.set_page_command(cid, "running")
    assert c.get("/api/inbox").json()[0]["pending"] is None
    post = store.enqueue_page_command(it.id, "post", None, body_hash(it), NOW)
    assert c.get("/api/inbox").json()[0]["pending"] == {"command_id": post, "verb": "post", "state": "queued"}


def test_a_message_item_takes_the_four_verbs_with_the_hash_check(c, store, kicks):
    it = message_item()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="edit", arg="make it warmer", shown_hash=body_hash(it))
    assert r.status_code == 200
    cmd = store.page_command(r.json()["command_id"])
    assert (cmd.verb, cmd.arg, cmd.shown_hash) == ("edit", "make it warmer", body_hash(it))
    assert post_action(c, item_id=it.id, verb="post", shown_hash=body_hash(it)).status_code == 409  # one at a time
    store.set_page_command(cmd.id, "done", "ok")
    assert post_action(c, item_id=it.id, verb="post").status_code == 400  # and a post needs the hash it was shown
    assert post_action(c, item_id=it.id, verb="post", shown_hash=body_hash(it)).status_code == 200


def test_history_says_drafting_for_a_post_a_run_has_claimed(c, store):
    team_post(store, kind="insight")
    msg_of = lambda: c.get("/api/history?view=person&week=2026-10-19").json()["groups"][0]["messages"][0]  # noqa: E731
    claim(store, TS_A, 2)
    assert msg_of()["feedback"] == "drafting"
    claim(store, TS_A, 30)
    assert msg_of()["feedback"] == "none"


# ---------------------------------------------------------------- the site review's line on a feedback card

def feedback_with_line(iid="feedback-1.1", dm_ts="1.1"):
    return replace(insight_item(iid, "delivered", dm_ts=dm_ts), kind="feedback", body="draft", title="Feedback: Sara")


def test_a_feedback_draft_in_the_inbox_carries_what_the_site_review_looked_at(c, store):
    store.upsert_item(feedback_with_line())
    store.set_cursor("feedback_review:feedback-1.1", "Reviewed: reach-poc.vercel.app (desktop + phone)")
    (row,) = c.get("/api/inbox").json()
    assert row["kind"] == "feedback" and row["review"] == "Reviewed: reach-poc.vercel.app (desktop + phone)"


def test_a_feedback_draft_that_was_not_reviewed_has_no_line(c, store):
    store.upsert_item(feedback_with_line())
    (row,) = c.get("/api/inbox").json()
    assert row["review"] is None
    store.set_cursor("feedback_review:feedback-1.1", "")  # an emptied cursor reads the same
    (row,) = c.get("/api/inbox").json()
    assert row["review"] is None


def test_only_a_feedback_draft_carries_a_review_line(c, store):
    """The line belongs to feedback: the other kinds never show one, whatever the cursor holds."""
    for item in (insight_item(), todo_item(), message_item()):
        store.upsert_item(item)
        store.set_cursor(f"feedback_review:{item.id}", "Reviewed: x.example (desktop + phone)")
    rows = c.get("/api/inbox").json()
    assert len(rows) == 3 and all(r["review"] is None for r in rows)


def test_the_review_line_travels_as_data(c, store):
    """The page puts it in as text (the frontend tests prove that); here it is only the cursor's own string."""
    hostile = "Reviewed: <img src=x onerror=alert(1)> \u202egnp.exe (desktop + phone)"
    store.upsert_item(feedback_with_line())
    store.set_cursor("feedback_review:feedback-1.1", hostile)
    (row,) = c.get("/api/inbox").json()
    assert row["review"] == hostile


# ---------------------------------------------------------------- the answers to a teammate's reply (the thread watch)

def test_a_feedback_item_that_answers_a_reply_says_which_kind_of_answer_it_is(c, store):
    ids = {"followback-1792581205.027989": "followback", "threadfb-1792581035.472729": "own_post",
           "insreply-1792581100.000100": "insight_reply", "feedback-1792574403.734689": None}
    for n, iid in enumerate(ids, 1):
        store.upsert_item(replace(insight_item(iid, "delivered", dm_ts=f"{n}.1"), kind="feedback", body="draft",
                                  title="t"))
    rows = {r["item_id"]: r["variant"] for r in c.get("/api/inbox").json()}
    assert rows == ids


def test_only_a_feedback_item_has_a_variant(c, store):
    store.upsert_item(replace(insight_item("followback-9.1", "delivered"), kind="insight"))
    store.upsert_item(replace(todo_item("insreply-9.2"), id="insreply-9.2"))
    assert [r["variant"] for r in c.get("/api/inbox").json()] == [None, None]


# ---------------------------------------------------------------- the channels a post may go to (post-channel, Task 3)
# GET /api/channels lists them (the leadership channel first, then his others A-Z, with the list's age), POST /api/channels/refresh asks
# the tick for a new read (at most once in 10 minutes), and `post` in /api/action may carry the channel id he chose.

SURVIVE = "C093L3Y8UM6"
LEFT = "C0LEFTLEFT1"


def cache_channels(store, *rows, at=NOW):
    """Cache a list the way the tick does: (id, name, private) rows, and when it was read."""
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": at.isoformat(), "channels": [
        {"id": i, "name": n, "private": p} for i, n, p in rows]}))


def feedback_item(iid="feedback-1.1", dm_ts="4.4"):
    return replace(insight_item(iid, dm_ts=dm_ts), kind="feedback", body="Great post, Sara.", title="Feedback for Sara")


def refresh(c, **kw):
    return c.post("/api/channels/refresh", json={}, **kw)


def test_channels_without_a_list_is_the_leadership_channel_alone(c):
    """A page with no list still has somewhere to post: the leadership channel, by its id. Its name is not known, so it is null (the
    page says "the program channel"), never the id passed off as a name."""
    assert c.get("/api/channels").json() == {
        "channels": [{"id": CH, "name": None, "private": False, "leadership": True}],
        "fetched_at": None, "age_s": None, "refresh_pending": False, "requested_at": None}


def test_channels_are_the_leadership_channel_first_then_his_others_a_to_z_with_the_lists_age(c, store):
    cache_channels(store, ("C0ZZZZZZZZ", "zeta", False), (SURVIVE, "survive-ai", False), ("C0AAAAAAAA", "alpha", False),
                   (CH, "tf-leadership", False), ("G0PRIVATE01", "secret-room", True), at=NOW - timedelta(hours=3))
    d = c.get("/api/channels").json()
    assert set(d) == {"channels", "fetched_at", "age_s", "refresh_pending", "requested_at"}
    assert all(set(x) == {"id", "name", "private", "leadership"} for x in d["channels"])
    assert [(x["name"], x["id"], x["private"], x["leadership"]) for x in d["channels"]] == [
        ("tf-leadership", CH, False, True), ("alpha", "C0AAAAAAAA", False, False), ("secret-room", "G0PRIVATE01", True, False),
        ("survive-ai", SURVIVE, False, False), ("zeta", "C0ZZZZZZZZ", False, False)]
    assert d["fetched_at"] == (NOW - timedelta(hours=3)).isoformat() and d["age_s"] == 3 * 3600


def test_the_leadership_channel_comes_first_even_when_the_list_lacks_it(c, store):
    cache_channels(store, (SURVIVE, "survive-ai", False), ("C0AAAAAAAA", "alpha", False))
    d = c.get("/api/channels").json()
    assert [(x["id"], x["name"], x["leadership"]) for x in d["channels"]] == [
        (CH, None, True), ("C0AAAAAAAA", "alpha", False), (SURVIVE, "survive-ai", False)]


def test_the_age_is_the_servers_clock_and_never_negative(c, store, clock):
    cache_channels(store, (SURVIVE, "survive-ai", False), at=NOW - timedelta(minutes=5, seconds=30))
    assert c.get("/api/channels").json()["age_s"] == 330
    clock.t += 7200
    assert c.get("/api/channels").json()["age_s"] == 330 + 7200
    cache_channels(store, (SURVIVE, "survive-ai", False), at=NOW + timedelta(hours=5))  # a clock that went back
    assert c.get("/api/channels").json()["age_s"] == 0


def test_a_list_whose_time_cannot_be_read_still_lists_and_has_no_age(c, store):
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": "last tuesday", "channels": [
        {"id": SURVIVE, "name": "survive-ai", "private": False}]}))
    d = c.get("/api/channels").json()
    assert [x["id"] for x in d["channels"]] == [CH, SURVIVE] and d["fetched_at"] is None and d["age_s"] is None


def test_a_damaged_cache_never_reaches_the_page(c, store):
    """The cache is read back through the shape rules of the page it came from: markup, a very long name, a non-ASCII name, a DM id
    and a row that is not a channel are dropped, so the page is never handed any of it."""
    hostile = "<img src=x onerror=alert(1)>"
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": NOW.isoformat(), "channels": [
        {"id": SURVIVE, "name": "survive-ai", "private": False},
        {"id": "C0HOSTILE01", "name": hostile, "private": False},
        {"id": "C0LONGNAME1", "name": "x" * 200, "private": False},
        {"id": "D08D9HFB8BB", "name": "a-dm", "private": False},
        {"id": "C0NONASCII1", "name": "teamsync-\u00e9", "private": False},
        {"id": "C0BADFLAG01", "name": "flagged", "private": "yes"},
        "not a row", None, 5]}))
    r = c.get("/api/channels")
    assert [x["id"] for x in r.json()["channels"]] == [CH, SURVIVE]
    assert "onerror" not in r.text and "xxxx" not in r.text and "a-dm" not in r.text and "flagged" not in r.text


def test_channels_need_no_slack_call_and_are_read_while_paused(c, store, monkeypatch):
    import haseeb_agent.slack_gateway as gateway
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a real call from the dashboard"))  # noqa: E731
    monkeypatch.setattr(gateway.ClaudeConnectorSlack, "list_channels", boom)
    cache_channels(store, (SURVIVE, "survive-ai", False))
    assert c.post("/api/pause", json={"paused": True}).status_code == 200
    assert [x["id"] for x in c.get("/api/channels").json()["channels"]] == [CH, SURVIVE]


# -- the refresh request

def test_a_refresh_request_sets_the_cursor_and_kicks_the_tick(c, store, kicks):
    r = refresh(c)
    assert r.status_code == 200 and r.json() == {"requested_at": NOW.isoformat(), "refresh_pending": True}
    assert channels.requested(store) is True and store.get_cursor(channels.REFRESH_KEY) == NOW.isoformat()
    assert kicks == [1]
    d = c.get("/api/channels").json()
    assert d["refresh_pending"] is True and d["requested_at"] == NOW.isoformat()
    assert channels.take_request(store) is True  # the tick takes it
    d = c.get("/api/channels").json()
    assert d["refresh_pending"] is False and d["requested_at"] is None


def test_a_refresh_request_that_fails_the_lock_asks_for_nothing(app, session, store, kicks):
    """It sits behind the same middleware as every other write: no session, no token, another origin or a cross-site fetch is refused
    before the route runs, so no request is recorded and the tick is not kicked."""
    client, token = session
    anon = TestClient(app, base_url=BASE, follow_redirects=False)
    path = "/api/channels/refresh"
    assert anon.post(path, json={}, headers={"Origin": BASE}).status_code == 401
    assert client.post(path, json={}, headers={"Origin": BASE}).status_code == 403
    assert client.post(path, json={}, headers={"X-Session-Token": token}).status_code == 403
    assert client.post(path, json={}, headers={"Origin": "http://evil.example", "X-Session-Token": token}).status_code == 403
    assert client.post(path, json={}, headers={"Origin": BASE, "X-Session-Token": token, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert channels.requested(store) is False and store.get_cursor(channels.ASKED_KEY) is None and kicks == []
    ok = client.post(path, json={}, headers={"Origin": BASE, "X-Session-Token": token})  # the same request, with all of it
    assert ok.status_code == 200 and channels.requested(store) is True and kicks == [1]


def test_a_refresh_request_makes_no_slack_call(c, store, monkeypatch):
    """The dashboard only asks: the tick reads (cli.CHANNELS_JOB). A real call would be seen."""
    import haseeb_agent.slack_gateway as gateway
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a real call from the dashboard"))  # noqa: E731
    monkeypatch.setattr(gateway.ClaudeConnectorSlack, "list_channels", boom)
    monkeypatch.setattr(gateway.ClaudeConnectorSlack, "send", boom)
    assert refresh(c).status_code == 200


def test_a_second_request_inside_ten_minutes_is_a_429_that_changes_nothing(c, store, kicks, clock):
    assert refresh(c).status_code == 200
    channels.take_request(store)  # the tick read the list meanwhile
    clock.t = NOW.timestamp() + 5 * 60
    r = refresh(c)
    assert r.status_code == 429
    assert r.json() == {"error": "asked a moment ago", "requested_at": NOW.isoformat(), "retry_after_s": 300}
    assert r.headers["retry-after"] == "300" and r.headers["cache-control"] == "no-store"
    assert channels.requested(store) is False and kicks == [1]  # nothing was requested again, and the tick was not kicked again
    clock.t = NOW.timestamp() + 10 * 60 - 1
    assert refresh(c).json()["retry_after_s"] == 1
    clock.t = NOW.timestamp() + 10 * 60
    assert refresh(c).status_code == 200 and channels.requested(store) is True and kicks == [1, 1]


def test_a_request_the_tick_has_not_taken_is_not_made_again_inside_ten_minutes(c, store, kicks, clock):
    assert refresh(c).status_code == 200  # a pause or the huddle hold keeps it waiting
    clock.t = NOW.timestamp() + 120
    r = refresh(c)
    assert r.status_code == 429 and r.json()["requested_at"] == NOW.isoformat()
    assert store.get_cursor(channels.REFRESH_KEY) == NOW.isoformat() and kicks == [1]  # still the first request's time


def test_a_refresh_request_is_refused_while_the_agent_is_paused_and_does_not_use_up_the_window(c, store, kicks, clock):
    assert c.post("/api/pause", json={"paused": True}).status_code == 200
    r = refresh(c)
    assert r.status_code == 409 and r.json() == {"error": "paused", "paused_at": NOW.isoformat()}
    assert channels.requested(store) is False and store.get_cursor(channels.ASKED_KEY) is None and kicks == []
    assert c.post("/api/pause", json={"paused": False}).status_code == 200
    assert refresh(c).status_code == 200  # no wait after the resume: the refused one counted for nothing


@pytest.mark.parametrize("kw", [{}, {"json": {}}, {"content": b""}, {"content": b"  {}  "}])
def test_a_refresh_request_takes_no_body_or_an_empty_one(c, store, kw):
    assert c.post("/api/channels/refresh", **kw).status_code == 200 and channels.requested(store) is True


@pytest.mark.parametrize("kw", [{"json": {"x": 1}}, {"json": {"requested_at": "2026-01-01T00:00:00+00:00"}}, {"json": ["x"]},
                                {"json": 5}, {"content": b"nope"}, {"content": b"{" + b" " * 30000 + b"}"}])
def test_a_refresh_request_with_anything_else_in_it_is_refused(c, store, kicks, kw):
    r = c.post("/api/channels/refresh", **kw)
    assert r.status_code == 400 and r.json() == {"error": "bad request"}
    assert channels.requested(store) is False and store.get_cursor(channels.ASKED_KEY) is None and kicks == []


# -- /api/action: a post may carry the channel he chose

@pytest.mark.parametrize("make", [insight_item, message_item])
def test_a_post_carries_the_channel_id_the_page_chose(c, store, kicks, make):
    it = make()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="post", arg=SURVIVE, shown_hash=body_hash(it))
    assert r.status_code == 200
    cmd = store.page_command(r.json()["command_id"])
    assert (cmd.item_id, cmd.verb, cmd.arg, cmd.shown_hash, cmd.state) == (it.id, "post", SURVIVE, body_hash(it), "queued")
    assert len(kicks) == 1


@pytest.mark.parametrize("arg", [None, "G0PRIVATE01", "C0C62C128D8", CH])
def test_a_post_may_name_no_channel_or_a_channel_id_whether_or_not_it_is_in_the_list(c, store, arg):
    """The shape is the page's check; whether the channel is his is the runner's (it looks it up again just before the send)."""
    it = insight_item()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="post", arg=arg, shown_hash=body_hash(it))
    assert r.status_code == 200 and store.page_command(r.json()["command_id"]).arg == arg


@pytest.mark.parametrize("bad", ["survive-ai", "#survive-ai", "<#C093L3Y8UM6|survive-ai>", "<#C093L3Y8UM6>", "c093l3y8um6",
                                 "C093L3Y8UM6 ", " C093L3Y8UM6", "C093L3Y8UM6\n", "D08D9HFB8BB", "U08CZCGJW3E", "W08CZCGJW3E", "",
                                 "C1", "C" + "A" * 13, "C093L3Y8UM6,C0C62C128D8", "<script>alert(1)</script>", "C093L3Y8UM\u0421"])
def test_a_post_with_anything_but_a_channel_id_is_refused_and_queues_nothing(c, store, kicks, bad):
    it = insight_item()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="post", arg=bad, shown_hash=body_hash(it))
    assert r.status_code == 400 and r.json() == {"error": "bad channel"}  # a fixed line: the input is not echoed
    assert kicks == [] and store.next_page_commands() == []


@pytest.mark.parametrize("arg", [5, ["C093L3Y8UM6"], {"id": SURVIVE}, True])
def test_a_channel_that_is_not_a_string_is_a_bad_request(c, store, kicks, arg):
    it = insight_item()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="post", arg=arg, shown_hash=body_hash(it))
    assert r.status_code == 400 and kicks == [] and store.next_page_commands() == []


@pytest.mark.parametrize("make", [feedback_item, todo_item])
def test_a_channel_on_feedback_or_the_todo_is_refused_before_it_is_queued(c, store, kicks, make):
    """They post only to their thread in the leadership channel (the runner refuses a channel on them too); a plain post is as before."""
    it = make()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb="post", arg=SURVIVE, shown_hash=body_hash(it))
    assert r.status_code == 400 and r.json() == {"error": LEADERSHIP_ONLY}
    assert kicks == [] and store.next_page_commands() == []
    plain = post_action(c, item_id=it.id, verb="post", shown_hash=body_hash(it))
    assert plain.status_code == 200 and store.page_command(plain.json()["command_id"]).arg is None


@pytest.mark.parametrize("verb,make", [("skip", insight_item), ("shorter", insight_item), ("attach", insight_item),
                                       ("cancel", todo_item), ("skip", message_item)])
def test_only_post_and_edit_keep_an_arg(c, store, verb, make):
    it = make()
    store.upsert_item(it)
    r = post_action(c, item_id=it.id, verb=verb, arg=SURVIVE, shown_hash=body_hash(it))
    assert r.status_code == 200 and store.page_command(r.json()["command_id"]).arg is None


def test_a_post_still_needs_the_hash_the_page_showed_and_the_agent_awake(c, store):
    it = insight_item()
    store.upsert_item(it)
    assert post_action(c, item_id=it.id, verb="post", arg=SURVIVE).status_code == 400  # shown_hash required
    assert c.post("/api/pause", json={"paused": True}).status_code == 200
    r = post_action(c, item_id=it.id, verb="post", arg=SURVIVE, shown_hash=body_hash(it))
    assert r.status_code == 409 and r.json()["error"] == "paused"
    assert store.next_page_commands() == []
