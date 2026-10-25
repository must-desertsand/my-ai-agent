"""The channel choice on an idea's or a message's card (post-channel design, Task 3). The card has a "Post to" choice: the leadership
channel first, as the default, then his other channels A-Z with the private ones marked, the age of the list and a "Refresh list" link.
The second click names the channel ("Post to #survive-ai as you?") and the click sends its id. Feedback and the Next Todo cards have
none: they post only to their thread in the leadership channel. A channel's name comes from Slack and is only ever shown as text.

Static checks read the files under dashboard/static. The render checks start a real uvicorn on 127.0.0.1 against a tmp store, log in
as `dashboard open` does and drive headless Chrome over CDP, as the other dashboard tests do (skipped when Chrome is missing).
Offline guard on; tmp data only. The tick is played by the test, through its own connection to the page's store."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from haseeb_agent import channels, controls
from haseeb_agent.commands import mark_post_failed
from haseeb_agent.dashboard.app import STATIC_DIR
from haseeb_agent.store import Store
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    _code, _commands, _contrast, _item, _rgb, _selectors, _surfaces, cdp, live_server, login_url, needs_chrome,
)
from tests.test_insights import good_idea_meta, good_meta

LEAD, SURVIVE, CHAT, OPS, FRESH = "C0C62C128D8", "C093L3Y8UM6", "C01AAAAAAAA", "G01EEEEEEEE", "C0NEWCHAN01"
IDEA, OLD, MSG, PING, FB, TODO = ("insight-pc-1", "insight-pc-2", "message-1792400000.000001", "message-1792400000.000002",
                                  "feedback-pc-1", "todo-pc-1")
ROWS = [(LEAD, "tf-leadership-q4-2026", False), (SURVIVE, "survive-ai", False), (OPS, "ops_eu.alerts", True),
        (CHAT, "chat-random", False)]
OPTIONS = ["#tf-leadership-q4-2026 (default)", "#chat-random", "#ops_eu.alerts (private)", "#survive-ai"]  # the leadership channel, then A-Z


# ---------------------------------------------------------------- the world

def cache_list(store: Store, rows=ROWS, *, age=timedelta(hours=3)) -> None:
    """The list the way the tick caches it: read `age` ago."""
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": (datetime.now().astimezone() - age).isoformat(), "channels": [
        {"id": i, "name": n, "private": p} for i, n, p in rows]}))


def put(store: Store, settings, iid: str, kind: str, title: str, body: str, dm_ts: str, *, status: str = "delivered") -> None:
    item = _item(iid, kind, title, body, datetime.now(settings.tz).date(), dm_ts)
    store.upsert_item(item if status == "delivered" else _failed(item))


def _failed(item):
    return replace(item, status="failed", attempts=1)


def seed(store: Store, settings) -> None:
    put(store, settings, IDEA, "insight", "Topic alpha", json.dumps(good_idea_meta(batch=IDEA)), "301.1")  # an idea (v2)
    put(store, settings, OLD, "insight", "Topic beta", json.dumps(good_meta(batch=OLD, name="beta")), "301.2")  # an insight (v1)
    put(store, settings, MSG, "message", "Message to the channel", "Standup moved to 3 pm.", "301.3")
    put(store, settings, FB, "feedback", "Feedback for Sara", "Great post, Sara. One thought: lead with the number.", "301.4")
    put(store, settings, TODO, "todo", "Next Todo", "Next todo: ship the shortlist", "301.5")
    cache_list(store)


def seed_no_list(store: Store, settings) -> None:
    put(store, settings, IDEA, "insight", "Topic alpha", json.dumps(good_idea_meta(batch=IDEA)), "301.1")


def seed_pings(store: Store, settings) -> None:
    put(store, settings, PING, "message", "Message with a ping", "@channel Standup is at 3 pm.", "302.1")
    store.set_cursor(f"message_raw:{PING}", "@channel standup at 3pm")  # typed by him: the post really notifies the channel
    put(store, settings, MSG, "message", "Message to the channel", "Standup moved to 3 pm.", "302.2")
    cache_list(store)


def seed_locked(store: Store, settings) -> None:
    """Two posts that may have gone out, each to the channel recorded before the send: an idea to #survive-ai and a message to the
    leadership channel."""
    put(store, settings, IDEA, "insight", "Topic alpha", json.dumps(good_idea_meta(batch=IDEA)), "301.1", status="failed")
    put(store, settings, MSG, "message", "Message to the channel", "Standup moved to 3 pm.", "301.3", status="failed")
    for iid, where in ((IDEA, SURVIVE), (MSG, LEAD)):
        mark_post_failed(store, iid)
        channels.record_post_channel(store, iid, where)
    cache_list(store)


def seed_paused(store: Store, settings) -> None:
    seed(store, settings)
    controls.set_paused(store, True, datetime.now(settings.tz))


def tick(settings, fn) -> None:
    """Do something to the page's store the way the tick does (its own connection, closed at once)."""
    st = Store(settings.db_path)
    try:
        fn(st)
    finally:
        st.close()


# ---------------------------------------------------------------- JS helpers

def card(iid: str) -> str:
    return f"document.querySelector('article.icard[data-item=\"{iid}\"]')"


def sel(iid: str) -> str:
    return card(iid) + ".querySelector('select.post-to-sel')"


def labels(iid: str) -> str:
    return f"[...{sel(iid)}.options].map(o => o.textContent)"


def choose(iid: str, channel_id: str) -> str:
    """What a click on an option does: the select's value changes and it says so."""
    return (f"{{ const s = {sel(iid)}; s.value = {json.dumps(channel_id)}; "
            f"s.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")


def button(iid: str, label: str) -> str:
    return f"[...{card(iid)}.querySelectorAll('.actions button')].find(b => b.textContent.trim() === {json.dumps(label)})"


def question(iid: str) -> str:
    return f"{card(iid)}.querySelector('.confirm-q')?.textContent"


def meta(iid: str) -> str:
    return f"{card(iid)}.querySelector('.post-to-meta')"


def refresh_link(iid: str) -> str:
    return f"{card(iid)}.querySelector('.post-to-meta button')"


# ---------------------------------------------------------------- static checks

def test_the_choice_reaches_the_api_only_through_the_client_and_never_acts_itself():
    code = _code((STATIC_DIR / "postto.js").read_text())
    assert not re.search(r"\bfetch\(", code) and "XMLHttpRequest" not in code
    assert re.findall(r'"/api/[a-z/]+"', code) == ['"/api/channels"', '"/api/channels/refresh"']  # the two calls it makes
    assert "/api/action" not in code and "act(" not in code and '"post"' not in code  # the card's Confirm posts, through actions.js


def test_a_channel_name_is_only_ever_set_as_text():
    code = _code((STATIC_DIR / "postto.js").read_text())
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment"):
        assert sink not in code, sink
    assert re.search(r'el\("option", \{ text:', code)  # an option's label is its text, never markup


def test_the_inbox_takes_the_choice_from_the_one_module_and_sends_what_it_picks():
    code = _code((STATIC_DIR / "inbox.js").read_text())
    assert 'from "./postto.js"' in code and "buildPostTo(" in code and "createChannelList(" in code
    assert "pick.arg" in code and 'run(st, "post"' in code


def test_the_choice_css_sits_under_its_own_names():
    css = (STATIC_DIR / "app.css").read_text()
    start, end = "/* ---------- the channel choice", "/* ---------- the site review's line"
    sels = _selectors(css[css.index(start):css.index(end)])
    loose = [x for x in sels if not re.search(r"\.post-to", x)]
    assert len(sels) >= 8 and not loose, loose
    assert css.index("/* ---------- inbox */") < css.index(start)  # in the inbox's own part of the file, ahead of its later sections


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_the_choices_text_colours_meet_aa_where_they_sit(theme):
    t, over, card_, sunken = _surfaces(theme)
    box = over("--panel2", sunken)  # the select sits on the card's panel2 fill
    for name, token, surface in (("the label", "--text2", sunken), ("the age and the notes", "--muted", sunken),
                                 ("the note", "--text2", sunken), ("the select's text", "--text", box),
                                 ("the link", "--a2-text", sunken)):
        assert _contrast(_rgb(t[token])[0], surface) >= 4.5, (theme, name)


# ---------------------------------------------------------------- the choice on the cards

@needs_chrome
def test_the_choice_is_on_ideas_and_messages_only_with_the_leadership_channel_preselected(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"document.querySelectorAll('article.icard').length === 5 && !!{sel(IDEA)} && !!{sel(OLD)} && !!{sel(MSG)}")
        for iid in (FB, TODO):  # they post only to their thread in the leadership channel: nothing to choose
            assert cdp.js(f"{card(iid)}.querySelector('.post-to')") is None, iid
            assert cdp.js(f"{card(iid)}.querySelector('select')") is None, iid
        for iid in (IDEA, OLD, MSG):  # an idea, an insight from before the switch, and a message of his own
            assert cdp.js(f"{sel(iid)}.value") == LEAD, iid
            assert cdp.js(labels(iid)) == OPTIONS, iid  # the leadership channel first, then A-Z, private ones marked
            assert cdp.js(f"{sel(iid)}.selectedOptions[0].textContent") == "#tf-leadership-q4-2026 (default)", iid
            assert cdp.js(f"{sel(iid)}.disabled") is False, iid
            assert cdp.js(f"{card(iid)}.querySelector('label.post-to-l').textContent") == "Post to", iid
            assert cdp.js(f"{card(iid)}.querySelector('label.post-to-l').htmlFor === {sel(iid)}.id && !!{sel(iid)}.id"), iid
            # the choice sits just above the buttons, which are what they were
            assert cdp.js(f"{card(iid)}.querySelector('.post-to').nextElementSibling === {card(iid)}.querySelector('.actions')"), iid
            assert cdp.js(f"[...{card(iid)}.querySelectorAll('.actions button')].map(b => b.textContent.trim())") == [
                "Post", "Edit", "Shorter", "Skip"], iid
        assert cdp.js("document.querySelector('.count-badge').textContent") == "5"
        cdp.close()


@needs_chrome
def test_private_channels_are_marked_and_the_others_are_not(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        got = cdp.js(f"Object.fromEntries([...{sel(IDEA)}.options].map(o => [o.value, o.textContent]))")
        assert got == {LEAD: "#tf-leadership-q4-2026 (default)", CHAT: "#chat-random", OPS: "#ops_eu.alerts (private)",
                       SURVIVE: "#survive-ai"}
        assert [v for v, text in got.items() if "private" in text] == [OPS]
        cdp.close()


@needs_chrome
def test_the_second_click_names_the_chosen_channel_and_the_click_sends_its_id(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        cdp.js(choose(IDEA, SURVIVE))
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        assert cdp.wait(question(IDEA)) == "Post to #survive-ai as you?"
        assert _commands(settings) == []  # nothing is queued until Confirm
        assert cdp.js(f"{sel(IDEA)}.disabled") is True  # and what he confirms is what he chose
        cdp.js(f"{button(IDEA, 'Cancel')}.click()")
        cdp.wait(f"{sel(IDEA)}.disabled === false")
        assert cdp.js(f"{sel(IDEA)}.value") == SURVIVE and _commands(settings) == []  # Cancel keeps the choice
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        cdp.wait(f"!!({button(IDEA, 'Confirm')})")
        cdp.js(f"{{ const b = {button(IDEA, 'Confirm')}; b.click(); b.click(); }}")  # a double click
        cdp.wait(f"{card(IDEA)}.querySelector('.cstatus.s-queued')")
        (cmd,) = _commands(settings)  # exactly one
        assert (cmd.item_id, cmd.verb, cmd.arg, cmd.state) == (IDEA, "post", SURVIVE, "queued")
        assert len(cmd.shown_hash) == 64  # still tied to the draft he saw
        assert cdp.js(f"{sel(IDEA)}.disabled") is True  # off while the command is in flight
        cdp.close()


@needs_chrome
def test_a_private_channel_can_be_chosen_and_its_id_is_sent(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(OLD)}")
        cdp.js(choose(OLD, OPS))
        cdp.js(f"{button(OLD, 'Post')}.click()")
        assert cdp.wait(question(OLD)) == "Post to #ops_eu.alerts as you?"
        cdp.js(f"{button(OLD, 'Confirm')}.click()")
        cdp.wait(f"{card(OLD)}.querySelector('.cstatus.s-queued')")
        assert [(c.item_id, c.arg) for c in _commands(settings)] == [(OLD, OPS)]
        cdp.close()


@needs_chrome
def test_the_default_names_the_leadership_channel_and_sends_no_channel(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        assert cdp.wait(question(IDEA)) == "Post to #tf-leadership-q4-2026 as you?"
        cdp.js(f"{button(IDEA, 'Confirm')}.click()")
        cdp.wait(f"{card(IDEA)}.querySelector('.cstatus.s-queued')")
        (cmd,) = _commands(settings)
        assert (cmd.item_id, cmd.verb, cmd.arg) == (IDEA, "post", None)  # none is the leadership channel, as it has always been
        cdp.close()


@needs_chrome
def test_feedback_and_the_todo_confirm_as_they_did(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{card(FB)} && !!{card(TODO)}")
        for iid, label in ((FB, "Post"), (TODO, "Post now")):
            cdp.js(f"{button(iid, label)}.click()")
            assert cdp.wait(question(iid)) == "Post to the program channel as you?", iid
            cdp.js(f"{button(iid, 'Confirm')}.click()")
            cdp.wait(f"{card(iid)}.querySelector('.cstatus.s-queued')")
        assert sorted((c.item_id, c.verb, c.arg) for c in _commands(settings)) == [(FB, "post", None), (TODO, "post", None)]
        cdp.close()


@needs_chrome
def test_a_message_confirm_names_the_channel_and_says_who_it_notifies(tmp_path, cdp):
    with live_server(tmp_path, seed_pings, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(PING)} && !!{sel(MSG)}")
        note = f"{card(PING)}.querySelector('.ping-note').textContent"
        assert cdp.js(note) == "Posting this notifies everyone in #tf-leadership-q4-2026 (@channel)."
        cdp.js(choose(PING, SURVIVE))  # the warning follows the choice: it is about the channel the message will go to
        assert cdp.js(note) == "Posting this notifies everyone in #survive-ai (@channel)."
        cdp.js(f"{button(PING, 'Post')}.click()")
        assert cdp.wait(question(PING)) == ("Post this as a new message in #survive-ai, as you? "
                                            "It notifies everyone in #survive-ai (@channel).")
        cdp.js(f"{button(PING, 'Confirm')}.click()")
        cdp.wait(f"{card(PING)}.querySelector('.cstatus.s-queued')")
        # a message that notifies nobody: just the channel
        cdp.js(choose(MSG, CHAT))
        cdp.js(f"{button(MSG, 'Post')}.click()")
        assert cdp.wait(question(MSG)) == "Post this as a new message in #chat-random, as you?"
        assert cdp.js(f"{card(MSG)}.querySelector('.ping-note')") is None
        assert [(c.item_id, c.arg) for c in _commands(settings)] == [(PING, SURVIVE)]
        cdp.close()


@needs_chrome
def test_with_no_list_yet_the_card_offers_the_leadership_channel_as_before(tmp_path, cdp):
    with live_server(tmp_path, seed_no_list, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        assert cdp.js(labels(IDEA)) == ["the program channel (default)"] and cdp.js(f"{sel(IDEA)}.value") == LEAD
        assert cdp.js(f"{meta(IDEA)}.textContent").startswith("No channel list yet")
        assert cdp.js(f"{refresh_link(IDEA)}.textContent") == "Refresh list"  # he can still ask for one
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        assert cdp.wait(question(IDEA)) == "Post to the program channel as you?"  # the words it always had
        cdp.js(f"{button(IDEA, 'Confirm')}.click()")
        cdp.wait(f"{card(IDEA)}.querySelector('.cstatus.s-queued')")
        assert [(c.item_id, c.arg) for c in _commands(settings)] == [(IDEA, None)]
        cdp.close()


@needs_chrome
def test_when_the_list_cannot_be_loaded_the_card_still_posts_to_the_leadership_channel(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200, gate={"/api/channels": 500}) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        assert cdp.js(labels(IDEA)) == ["the program channel (default)"]
        assert cdp.js(f"{meta(IDEA)}.textContent").startswith("Channel list unavailable")
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        assert cdp.wait(question(IDEA)) == "Post to the program channel as you?"
        cdp.js(f"{button(IDEA, 'Confirm')}.click()")
        cdp.wait(f"{card(IDEA)}.querySelector('.cstatus.s-queued')")
        assert [(c.item_id, c.arg) for c in _commands(settings)] == [(IDEA, None)]
        cdp.close()


# ---------------------------------------------------------------- hostile names, and a narrow screen

LONG = "x" * 300
HOSTILE_NAME = "<img src=x onerror=window.__pwned=1>"
FAKE_LIST = {"channels": [
    {"id": LEAD, "name": "tf-leadership-q4-2026", "private": False, "leadership": True},
    {"id": "C0HOSTILE01", "name": HOSTILE_NAME, "private": False, "leadership": False},
    {"id": "C0LONGNAME1", "name": LONG, "private": True, "leadership": False},
    {"id": "not-an-id", "name": "ignored", "private": False, "leadership": False}],
    "fetched_at": None, "age_s": 5, "refresh_pending": False, "requested_at": None}
# what the page would be handed if the API ever sent a name it should not: the answer of /api/channels is replaced before the page runs
FAKE_API = ("(() => { const real = window.fetch.bind(window); window.fetch = (u, o) => String(u) === '/api/channels' ? "
            "Promise.resolve(new Response(" + json.dumps(json.dumps(FAKE_LIST)) + ", { status: 200, "
            "headers: { 'Content-Type': 'application/json' } })) : real(u, o); })();")


@needs_chrome
def test_a_hostile_channel_name_is_shown_as_text(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=FAKE_API)
        cdp.wait(f"!!{sel(IDEA)}")
        got = cdp.js(labels(IDEA))
        assert got[:2] == ["#tf-leadership-q4-2026 (default)", "#" + HOSTILE_NAME]  # literal text, in the order the page was given
        assert got[2] == "#" + "x" * 80 + " (private)" and len(got) == 3  # a long name is cut, and an id that is not one is left out
        assert cdp.js(f"{card(IDEA)}.querySelectorAll('img').length") == 0 and cdp.js("window.__pwned === undefined")
        cdp.js(choose(IDEA, "C0HOSTILE01"))
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        assert cdp.wait(question(IDEA)) == f"Post to #{HOSTILE_NAME} as you?"
        assert cdp.js(f"{card(IDEA)}.querySelector('.confirm-q').children.length") == 0  # text, not markup
        assert cdp.js("document.querySelectorAll('img[src=\"x\"]').length") == 0 and cdp.js("window.__pwned === undefined")
        cdp.close()


@needs_chrome
def test_a_very_long_name_does_not_push_the_card_wider_than_a_phone(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login), width=380, init_js=FAKE_API)
        cdp.wait(f"!!{sel(IDEA)}")
        cdp.js(choose(IDEA, "C0LONGNAME1"))
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        cdp.wait(question(IDEA))
        assert cdp.js("document.documentElement.scrollWidth <= window.innerWidth")
        assert cdp.js(f"{sel(IDEA)}.getBoundingClientRect().right <= {card(IDEA)}.getBoundingClientRect().right")
        cdp.close()


# ---------------------------------------------------------------- a post that may have gone out: locked to where it went

@needs_chrome
def test_a_post_that_may_have_gone_out_locks_the_choice_to_the_channel_it_was_recorded_to(tmp_path, cdp):
    with live_server(tmp_path, seed_locked, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)} && !!{sel(MSG)}")
        assert cdp.js(labels(IDEA)) == ["#survive-ai"] and cdp.js(f"{sel(IDEA)}.value") == SURVIVE  # the other channels are not offered
        assert cdp.js(f"{sel(IDEA)}.disabled") is True
        assert cdp.js(f"{card(IDEA)}.querySelector('.post-to-note').hidden") is False
        assert cdp.js(f"{sel(IDEA)}.getAttribute('aria-describedby') === {card(IDEA)}.querySelector('.post-to-note').id")  # said to a screen reader
        assert "#survive-ai" in cdp.js(f"{card(IDEA)}.querySelector('.post-to-note').textContent")
        assert cdp.js(f"{meta(IDEA)}.hidden") is True  # the list's age and its refresh are of no use to a locked choice
        assert "check #survive-ai" in cdp.js(f"{card(IDEA)}.querySelector('.banner').textContent")
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        assert cdp.wait(question(IDEA)) == "The last post may have gone out. Check #survive-ai first."
        cdp.js(f"{button(IDEA, 'I checked — post again')}.click()")
        cdp.wait(f"{card(IDEA)}.querySelector('.cstatus.s-queued')")
        # the retry names the recorded channel: any other is refused by the runner, so the page never sends another
        assert [(c.item_id, c.verb, c.arg) for c in _commands(settings)] == [(IDEA, "post", SURVIVE)]
        # the one recorded in the leadership channel is locked to it too, and its retry is the plain leadership post
        assert cdp.js(labels(MSG)) == ["#tf-leadership-q4-2026"] and cdp.js(f"{sel(MSG)}.disabled") is True
        cdp.js(f"{button(MSG, 'Post')}.click()")
        cdp.wait(f"!!({button(MSG, 'I checked — post again')})")
        cdp.js(f"{button(MSG, 'I checked — post again')}.click()")
        cdp.wait(f"{card(MSG)}.querySelector('.cstatus.s-queued')")
        assert sorted((c.item_id, c.arg) for c in _commands(settings)) == [(IDEA, SURVIVE), (MSG, None)]
        cdp.close()


# ---------------------------------------------------------------- the list's age and "Refresh list"

@needs_chrome
def test_the_card_says_how_old_the_list_is_and_refresh_list_asks_once(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        assert cdp.js(f"{meta(IDEA)}.textContent").startswith("List from 3 h ago")
        assert cdp.js(f"{refresh_link(IDEA)}.textContent") == "Refresh list"
        cdp.js(f"{refresh_link(IDEA)}.click()")
        cdp.wait(f"{meta(IDEA)}.textContent.includes('Refresh asked')")
        assert cdp.js(f"{refresh_link(IDEA)}") is None  # asked: no second link while the tick has not read it
        st = Store(settings.db_path)
        try:  # the dashboard only asks: the request is a cursor for the tick, and nothing else happened
            assert channels.requested(st) is True and st.get_cursor(channels.ASKED_KEY)
            assert _commands(settings) == []
        finally:
            st.close()

        def the_tick_reads_the_list(store):  # what the refresh job does: take the request, keep the new list
            channels.take_request(store)
            cache_list(store, [*ROWS, (FRESH, "brand-new", False)], age=timedelta(seconds=0))
        tick(settings, the_tick_reads_the_list)
        cdp.wait(f"{labels(IDEA)}.includes('#brand-new')", timeout=15)  # the page looks again while a request is pending
        assert cdp.js(labels(IDEA)) == [OPTIONS[0], "#brand-new", *OPTIONS[1:]]  # A-Z among the others
        cdp.wait(f"{meta(IDEA)}.textContent.startsWith('List from just now')")
        # a second request so soon after the first is the server's 429: the page says when, and offers no link meanwhile
        cdp.wait(f"!!{refresh_link(IDEA)}")
        cdp.js(f"{refresh_link(IDEA)}.click()")
        cdp.wait(f"{meta(IDEA)}.textContent.includes('Asked a moment ago')")
        assert re.search(r"try again in (9|10) min", cdp.js(f"{meta(IDEA)}.textContent"))
        assert cdp.js(f"{refresh_link(IDEA)}") is None
        cdp.close()


@needs_chrome
def test_a_refresh_that_could_not_be_asked_says_so_and_can_be_tried_again(tmp_path, cdp):
    """The request never reached the dashboard (a server error stands in for any failure): the card says so, keeps the link, and asked for
    nothing, so a second try is a plain first request."""
    with live_server(tmp_path, seed, stream_polls=200, gate={"/api/channels/refresh": 500}) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{refresh_link(IDEA)}")
        cdp.js(f"{refresh_link(IDEA)}.click()")
        cdp.wait(f"{meta(IDEA)}.textContent.includes(\"Couldn't ask\")")
        assert cdp.js(f"{refresh_link(IDEA)}.textContent") == "Refresh list"  # still offered: try again
        assert "Refresh asked" not in cdp.js(f"{meta(IDEA)}.textContent")
        st = Store(settings.db_path)
        try:
            assert channels.requested(st) is False and not st.get_cursor(channels.ASKED_KEY)
        finally:
            st.close()
        cdp.close()


@needs_chrome
def test_the_choice_and_the_refresh_are_off_while_the_agent_is_paused(tmp_path, cdp):
    with live_server(tmp_path, seed_paused, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        cdp.wait(f"{sel(IDEA)}.disabled === true && {sel(MSG)}.disabled === true")
        assert cdp.js(f"{refresh_link(IDEA)}.disabled") is True
        assert cdp.js(f"{refresh_link(IDEA)}.title") == "Paused: resume the agent first"
        cdp.close()


# ---------------------------------------------------------------- the choice outlives a repaint

@needs_chrome
def test_the_choice_survives_a_revised_draft_that_replaces_the_card(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{sel(IDEA)}")
        cdp.js(choose(IDEA, SURVIVE))
        cdp.js(f"{button(IDEA, 'Shorter')}.click()")
        cdp.wait(f"{card(IDEA)}.querySelector('.cstatus.s-queued')")
        (cmd,) = _commands(settings)

        def revised(store):  # the tick revised the draft: a new body, so a new card
            meta_ = good_idea_meta(batch=IDEA)
            meta_["insight"]["idea"] = "A shorter idea. We build the fix."
            put(store, settings, IDEA, "insight", "Topic alpha", json.dumps(meta_), "301.1")
            store.set_page_command(cmd.id, "done", "✏️ Revised below")
        tick(settings, revised)
        cdp.wait(f"{card(IDEA)}.querySelector('.draft').textContent.includes('A shorter idea.')")
        cdp.wait(f"{sel(IDEA)}.disabled === false")
        assert cdp.js(f"{sel(IDEA)}.value") == SURVIVE  # the replacement card is the same choice
        cdp.js(f"{button(IDEA, 'Post')}.click()")
        assert cdp.wait(question(IDEA)) == "Post to #survive-ai as you?"
        cdp.close()
