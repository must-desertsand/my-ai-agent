"""The composer (2026-10-20): the frontend. Static checks read the files under dashboard/static. The render checks start
a real uvicorn on 127.0.0.1 against a tmp store, with a recorder for the tick kick (nothing ever runs), log in as
`dashboard open` does, and drive headless Chrome over CDP, as the Task 8 and run-controls tests do. They are skipped
when Chrome is missing. Offline guard on; tmp data only.

The tick is played by the test: it claims the page command, makes the `message` item (and its cursors) and only then
ends the command, in the order the real tick does, by writing to the tmp store the page is reading."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime

import pytest

from haseeb_agent import controls
from haseeb_agent.dashboard.app import STATIC_DIR
from haseeb_agent.models import Item
from haseeb_agent.store import Store
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    CARD, HOSTILE_BIDI, HOSTILE_IMG, HOSTILE_SCRIPT, JS, _btn, _code, _commands, _contrast, _rgb, _selectors,
    _surfaces, cdp, live_server, login_url, needs_chrome, render_dom, section,
)

ROUGH = "team standup moved to 3pm pls update ur notes b4 that"
POLISHED = "Standup moved to 3 pm. Please update your notes before then."
NOTE = "Fixed grammar and made it shorter."
COMPOSED = "✅ Polished · the draft is below and in your DM"
PAUSED = "Paused: resume the agent first"
MID = "message-1792400000.000001"

BOX = "document.querySelector('.composer')"
TA = BOX + ".querySelector('textarea')"
BTN = BOX + ".querySelector('.composer-btn')"
STATUS = BOX + ".querySelector('.cstatus')"
COUNT = BOX + ".querySelector('.counter')"
MSG = CARD.format(kind="Message")


def typed(text: str) -> str:
    """JS that puts `text` in the box the way typing does (the input event the page listens for)."""
    return f"{{ const t = {TA}; t.value = {json.dumps(text)}; t.dispatchEvent(new Event('input')); }}"


def message_item(settings, iid: str = MID, body: str = POLISHED, title: str = "Message to the channel",
                 dm_ts: str = "30.1") -> Item:
    now = datetime.now(settings.tz)
    return Item(id=iid, kind="message", day=now.date(), status="delivered", body=body, title=title, dm_ts=dm_ts,
                source_ts=None, score=None, models={"wrote": "m"}, attempts=0, edited=False, created_at=now,
                acted_at=None)


def put_message(store: Store, settings, iid: str = MID, body: str = POLISHED, note: str = NOTE, raw: str = ROUGH,
                title: str = "Message to the channel", dm_ts: str = "30.1") -> None:
    """What the tick leaves when it has polished a message: the item, and the cursors with its note and his words."""
    store.upsert_item(message_item(settings, iid, body, title, dm_ts))
    store.set_cursor(f"message_note:{iid}", note)
    store.set_cursor(f"message_raw:{iid}", raw)


def seed_message(store: Store, settings) -> None:
    put_message(store, settings)


def tick(settings, fn) -> None:
    """Do something to the page's store the way the tick does (its own connection, closed at once)."""
    st = Store(settings.db_path)
    try:
        fn(st)
    finally:
        st.close()


def compose_commands(settings) -> list:
    return [c for c in _commands(settings) if c.verb == "compose"]


def click_polish(cdp) -> None:
    cdp.wait(f"!!({TA})")
    cdp.js(typed(ROUGH))
    cdp.js(f"{{ const b = {BTN}; b.click(); b.click(); }}")  # a double click


# ---------------------------------------------------------------- static checks

def test_the_composer_reaches_the_api_only_through_the_client():
    code = _code((STATIC_DIR / "compose.js").read_text())
    assert not re.search(r"\bfetch\(", code) and "XMLHttpRequest" not in code
    assert re.findall(r'"/api/[a-z/]+"', code) == ['"/api/compose"']  # the one call it makes


def test_the_composer_posts_nothing_and_calls_no_model():
    """compose.js asks the tick to polish; the only verb it uses is its own, and it never reaches /api/action."""
    code = _code((STATIC_DIR / "compose.js").read_text())
    assert "/api/action" not in code and "/api/run" not in code
    assert '"post"' not in code and "act(" not in code


def test_the_composer_has_a_hard_stop_for_a_runaway_paste_and_the_servers_limit():
    code = _code((STATIC_DIR / "compose.js").read_text())
    assert "export const COMPOSE_MAX = 2000;" in code and "SAFETY_MAX = 20000" in code


def test_composer_css_sits_under_its_own_names():
    """The new rules redefine nothing the other pages share: composer, message-card and its mention warning only."""
    css = (STATIC_DIR / "app.css").read_text()
    start, end = "/* ---------- composer:", "/* ---------- live log */"
    sels = _selectors(css[css.index(start):css.index(end)])
    own = re.compile(r"^(?:\.composer|\.counter-over|\.ping-note|\.msg-)")
    loose = [x for x in sels if not own.match(x)]
    assert len(sels) >= 15 and not loose, loose
    assert ".icard.k-message" in css and ".kind.k-message" in css


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_composer_text_colours_meet_aa_where_they_sit(theme):
    """The colours the composer and a message card use for text, over the surfaces they really sit on."""
    t, over, card, sunken = _surfaces(theme)
    box = over("--panel", card)  # the composer's own surface, on the card
    draft = over("--panel", sunken)  # "Your words", on a message card
    checks = [
        ("hint and heading", "--muted", (box,)),
        ("the counter", "--muted", (box,)),
        ("a full counter", "--warn", (box,)),
        ("an over-limit counter", "--bad", (box,)),
        ("the note", "--text2", (sunken,)),
        ("the note's label", "--muted", (sunken,)),
        ("Your words (link)", "--violet-text", (sunken,)),
        ("Your words (text)", "--text2", (draft,)),
        ("the Message kind", "--violet-text", (sunken,)),
    ]
    for name, token, surfaces in checks:
        for surface in surfaces:
            assert _contrast(_rgb(t[token])[0], surface) >= 4.5, (theme, name)
    # the mention warning: warn on its own tint over the card's sunken tile
    assert _contrast(_rgb(t["--warn"])[0], over("--warn-soft", sunken)) >= 4.5


# ---------------------------------------------------------------- the composer on the Live page

@needs_chrome
def test_the_composer_is_the_first_thing_in_the_inbox_and_starts_idle(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({TA})")
        body = "document.querySelector('section[aria-label=\"Waiting for you\"] .card-b')"
        assert cdp.js(f"{body}.firstElementChild === {BOX}") is True  # on top: before the drafts and the team posts
        assert cdp.js(f"{BOX}.getAttribute('role')") == "group"
        assert cdp.js(f"{BOX}.getAttribute('aria-label')") == "Write a message"
        assert cdp.js(f"{BOX}.querySelector('h3').textContent") == "Write a message"
        hint = cdp.js(f"{BOX}.querySelector('.composer-hint').textContent")
        assert "tidies it up" in hint and "Nothing is posted until you press Post." in hint
        assert cdp.js(f"{TA}.value") == "" and cdp.js(f"{TA}.placeholder").startswith("Type it roughly")
        assert cdp.js(f"document.querySelector('label[for=compose-text]') !== null")  # the box has a name
        assert cdp.js(f"{TA}.getAttribute('aria-describedby')") == "compose-hint compose-count"
        assert cdp.js(f"{COUNT}.textContent") == "0 / 2000"
        assert cdp.js(f"{BTN}.textContent.trim()") == "Polish" and cdp.js(f"{BTN}.disabled") is True
        assert cdp.js(f"{BTN}.getAttribute('title')") == "Write something first"
        assert cdp.js(f"getComputedStyle({STATUS}).display") == "none"  # nothing to say yet
        assert "Nothing waiting for you" in cdp.wait("document.querySelector('.empty-t')?.textContent")
        assert _commands(settings) == []
        cdp.close()


@needs_chrome
def test_the_counter_the_limit_and_when_polish_is_on(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({TA})")
        cdp.js(typed("standup moved to 3pm"))
        assert cdp.js(f"{COUNT}.textContent") == "20 / 2000" and cdp.js(f"{BTN}.disabled") is False
        assert cdp.js(f"{BTN}.hasAttribute('title')") is False
        cdp.js(typed("   \n \t "))  # only white space is nothing
        assert cdp.js(f"{BTN}.disabled") is True and cdp.js(f"{BTN}.getAttribute('title')") == "Write something first"
        cdp.js(typed("x" * 2000))  # the limit itself is fine, and the counter warns
        assert cdp.js(f"{COUNT}.textContent") == "2000 / 2000" and cdp.js(f"{BTN}.disabled") is False
        assert cdp.js(f"{COUNT}.classList.contains('full')") is True
        cdp.js(typed("x" * 2001))  # one more is not: nothing is cut off behind his back, he is told
        assert cdp.js(f"{COUNT}.textContent") == "2001 / 2000" and cdp.js(f"{BTN}.disabled") is True
        assert cdp.js(f"{COUNT}.classList.contains('over')") is True
        assert cdp.js(f"{BOX}.querySelector('.counter-over').textContent") == "1 too many"
        assert cdp.js(f"{TA}.getAttribute('aria-invalid')") == "true"
        assert cdp.js(f"{BTN}.getAttribute('title')") == "Over the character limit"
        assert cdp.js(f"{TA}.value.length") == 2001  # still all there
        cdp.js(typed("\U0001f600" * 2000))  # people (and the server) count an emoji once
        assert cdp.js(f"{COUNT}.textContent") == "2000 / 2000" and cdp.js(f"{BTN}.disabled") is False
        assert _commands(settings) == []  # typing queues nothing
        cdp.close()


@needs_chrome
def test_polish_queues_one_command_and_the_draft_arrives_below(tmp_path, cdp):
    kicks = []
    with live_server(tmp_path, stream_polls=200, kick=lambda: kicks.append(1)) as (base, settings, login):
        cdp.open(login_url(base, login))
        click_polish(cdp)
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        assert "queued for the next tick" in cdp.js(f"{STATUS}.textContent")
        assert cdp.js(f"{BTN}.disabled") is True and cdp.js(f"{BTN}.textContent.trim()") == "Polishing…"
        assert cdp.js(f"{TA}.readOnly") is True and cdp.js(f"{TA}.value") == ROUGH  # kept while it works
        (cmd,) = compose_commands(settings)  # exactly one, however often it was clicked
        assert (cmd.arg, cmd.shown_hash, cmd.state) == (ROUGH, "", "queued")
        assert re.fullmatch(r"message-\d{10}\.\d{6}", cmd.item_id) and len(kicks) == 1
        # the tick takes it
        tick(settings, lambda st: st.set_page_command(cmd.id, "running"))
        cdp.wait(f"{STATUS}.classList.contains('s-running')")
        assert "👀" in cdp.js(f"{STATUS}.textContent") and "working on it" in cdp.js(f"{STATUS}.textContent")
        assert cdp.js(f"{BTN}.disabled") is True
        # it makes the item first and ends the command after, as the real tick does
        tick(settings, lambda st: (put_message(st, settings, cmd.item_id), st.set_page_command(cmd.id, "done", COMPOSED)))
        cdp.wait(f"{STATUS}.classList.contains('s-done')")
        text = cdp.js(f"{STATUS}.textContent")
        assert text.startswith("✅") and text.endswith("Polished · the draft is below and in your DM")
        assert cdp.js(f"{TA}.value") == "" and cdp.js(f"{TA}.readOnly") is False  # the text is spent: the box is free
        assert cdp.js(f"{COUNT}.textContent") == "0 / 2000"
        # the draft is a normal card under the composer, with what changed and his own words folded away
        cdp.wait(f"!!{MSG}", timeout=8)
        assert cdp.js(f"{MSG}.querySelector('.draft').textContent") == POLISHED
        assert cdp.js(f"{MSG}.querySelector('.icard-title').textContent") == "Message to the channel"
        assert cdp.js(f"{MSG}.querySelector('.kind').textContent.trim()") == "Message"
        assert cdp.js(f"{MSG}.querySelector('.msg-note').textContent") == f"What changed {NOTE}"
        assert cdp.js(f"{MSG}.querySelector('details.msg-orig').open") is False
        assert cdp.js(f"{MSG}.querySelector('details.msg-orig summary').textContent") == "Your words"
        assert cdp.js(f"{MSG}.querySelector('.msg-orig-text').textContent") == ROUGH
        assert cdp.js(f"[...{MSG}.querySelectorAll('.actions button')].map(b => b.textContent.trim())") == [
            "Post", "Edit", "Shorter", "Skip"]
        assert cdp.js(f"{BOX}.compareDocumentPosition({MSG}) & Node.DOCUMENT_POSITION_FOLLOWING") > 0
        assert cdp.js("document.querySelector('.count-badge').textContent") == "1"
        cdp.close()


@needs_chrome
def test_a_failed_polish_keeps_the_text_says_why_and_can_be_tried_again(tmp_path, cdp):
    why = "⚠️ The draft could not be sent to your DM. Try again"
    with live_server(tmp_path, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        click_polish(cdp)
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        (cmd,) = compose_commands(settings)
        tick(settings, lambda st: st.set_page_command(cmd.id, "failed", why))
        cdp.wait(f"{STATUS}.classList.contains('s-failed')")
        text = cdp.js(f"{STATUS}.textContent")
        assert text.startswith("⚠️") and text.endswith("The draft could not be sent to your DM. Try again")
        assert cdp.js(f"{TA}.value") == ROUGH and cdp.js(f"{TA}.readOnly") is False  # nothing he wrote is lost
        assert cdp.js(f"{BTN}.disabled") is False and cdp.js(f"{BTN}.textContent.trim()") == "Polish"
        assert cdp.js(f"document.activeElement === {TA}")  # back in the box, to fix it or just try again
        assert cdp.js(f"document.querySelectorAll('article.icard').length") == 0  # no card: nothing was made
        # he changes the text: the old reason is gone; a new Polish is a new command
        cdp.js(typed(ROUGH + " please"))
        assert cdp.js(f"getComputedStyle({STATUS}).display") == "none"
        cdp.js(f"{BTN}.click()")
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        assert [c.arg for c in compose_commands(settings)] == [ROUGH, ROUGH + " please"]  # oldest first
        cdp.close()


@needs_chrome
def test_a_polish_the_tick_refuses_shows_its_words(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        click_polish(cdp)
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        (cmd,) = compose_commands(settings)
        tick(settings, lambda st: st.set_page_command(cmd.id, "failed", "⚠️ that is 2500 characters; the limit is 2000"))
        assert cdp.wait(f"{STATUS}.classList.contains('s-failed') && {STATUS}.textContent").endswith(
            "that is 2500 characters; the limit is 2000")
        cdp.close()


@needs_chrome
def test_a_request_that_fails_is_said_in_plain_words_and_loses_nothing(tmp_path, cdp):
    gate = {"/api/compose": 500}
    with live_server(tmp_path, stream_polls=200, gate=gate) as (base, settings, login):
        cdp.open(login_url(base, login))
        click_polish(cdp)
        assert cdp.wait(f"{STATUS}.classList.contains('s-failed') && {STATUS}.textContent").endswith(
            "Couldn't send that. Try again.")
        assert cdp.js(f"{TA}.value") == ROUGH and cdp.js(f"{BTN}.disabled") is False
        assert _commands(settings) == []
        gate.clear()  # the next try goes through
        cdp.js(f"{BTN}.click()")
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        assert len(compose_commands(settings)) == 1
        cdp.close()


def seed_paused(store: Store, settings) -> None:
    controls.set_paused(store, True, datetime.now(settings.tz))


@needs_chrome
def test_while_paused_polish_is_off_and_says_why(tmp_path, cdp):
    with live_server(tmp_path, seed_paused, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({TA}) && {BTN}.getAttribute('title') === {json.dumps(PAUSED)}")
        cdp.js(typed(ROUGH))
        assert cdp.js(f"{BTN}.disabled") is True  # text in the box, and still it cannot be sent
        cdp.wait(f"{STATUS}.textContent.includes({json.dumps(PAUSED)})")
        assert cdp.js(f"{STATUS}.classList.contains('s-failed')") is True
        cdp.js(f"{BTN}.click()")  # a disabled button sends nothing, whatever calls click()
        time.sleep(0.4)
        assert _commands(settings) == []
        # resume: the box works again, with no reload
        tick(settings, lambda st: controls.set_paused(st, False, datetime.now(settings.tz)))
        cdp.wait(f"{BTN}.disabled === false", timeout=15)
        assert cdp.js(f"{TA}.value") == ROUGH
        cdp.close()


@needs_chrome
def test_ctrl_enter_polishes_like_the_button(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({TA})")
        cdp.js(typed(ROUGH))
        cdp.js(f"{TA}.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}}))")  # a plain Enter: a new line
        time.sleep(0.3)
        assert _commands(settings) == []
        cdp.js(f"{TA}.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', ctrlKey: true, bubbles: true}}))")
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        assert [c.arg for c in compose_commands(settings)] == [ROUGH]
        cdp.close()


@needs_chrome
def test_the_text_survives_a_page_change_and_a_polish_that_ends_while_away(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({TA})")
        cdp.js(typed("a half-written message"))
        cdp.js("location.hash = '#/history'")
        cdp.wait("!!document.querySelector('.page-history') && !document.querySelector('.composer')")
        cdp.js("location.hash = '#/live'")
        cdp.wait(f"!!({TA})")
        assert cdp.js(f"{TA}.value") == "a half-written message" and cdp.js(f"{COUNT}.textContent") == "22 / 2000"
        # a Polish that is still out when he leaves is still followed, and its end is not missed
        cdp.js(typed(ROUGH))
        cdp.js(f"{BTN}.click()")
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        (cmd,) = compose_commands(settings)
        cdp.js("location.hash = '#/history'")
        cdp.wait("!!document.querySelector('.page-history') && !document.querySelector('.composer')")
        tick(settings, lambda st: (put_message(st, settings, cmd.item_id), st.set_page_command(cmd.id, "done", COMPOSED)))
        time.sleep(1.5)  # the stream tells the page while another page is open
        cdp.js("location.hash = '#/live'")
        cdp.wait(f"!!({TA})")
        assert cdp.js(f"{TA}.value") == ""  # spent: the draft exists
        cdp.wait(f"!!{MSG}", timeout=8)
        assert cdp.js(f"{MSG}.querySelector('.draft').textContent") == POLISHED
        cdp.close()


# ---------------------------------------------------------------- a message card

HOSTILE_NOTE = f"{HOSTILE_IMG} changed {HOSTILE_BIDI}"
HOSTILE_RAW = f"my words {HOSTILE_IMG} {HOSTILE_SCRIPT} {HOSTILE_BIDI}"


def seed_hostile_message(store: Store, settings) -> None:
    put_message(store, settings, body=f"Draft {HOSTILE_IMG} {HOSTILE_SCRIPT} {HOSTILE_BIDI}", note=HOSTILE_NOTE,
                raw=HOSTILE_RAW)


@needs_chrome
def test_hostile_text_in_a_message_card_is_only_ever_text(tmp_path, cdp):
    init = "window.__xss = 0;"
    with live_server(tmp_path, seed_hostile_message, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=init)
        cdp.wait(f"!!{MSG}")
        draft = f"Draft {HOSTILE_IMG} {HOSTILE_SCRIPT} {HOSTILE_BIDI}"
        assert cdp.js(f"{MSG}.querySelector('.draft').textContent") == draft
        assert cdp.js(f"{MSG}.querySelector('.msg-note').textContent") == f"What changed {HOSTILE_NOTE}"
        assert cdp.js(f"{MSG}.querySelector('.msg-orig-text').textContent") == HOSTILE_RAW
        assert cdp.js(f"{MSG}.querySelectorAll('img, script, a, iframe, svg').length") == 0
        assert cdp.js("document.querySelectorAll('img').length") == 0 and cdp.js("window.__xss") == 0
        # every piece of his or the model's text sits in a <bdi>, so a bidi control can't reorder the card around it
        assert cdp.js(f"[...{MSG}.querySelectorAll('.draft, .msg-note, .msg-orig-text, .icard-title')]"
                      ".every(e => !!e.querySelector('bdi'))")
        # and nothing the card shows is a box he could type in by accident: the composer is the only textarea here
        assert cdp.js(f"{MSG}.querySelectorAll('textarea').length") == 0
        cdp.close()


def seed_mentions(store: Store, settings) -> None:
    put_message(store, settings, "message-1792400000.000010", "@channel Standup is at 3 pm.", raw="@channel standup at 3pm",
                title="Message A", dm_ts="30.2")
    put_message(store, settings, "message-1792400000.000011", "@channel Standup is at 3 pm.", raw="standup at 3pm",
                title="Message B", dm_ts="30.3")  # the model added it: not a ping
    put_message(store, settings, "message-1792400000.000012", "@here @channel Both.", raw="@here @channel both",  # what the cursor holds: normalize_raw turned Slack's tokens into these
                title="Message C", dm_ts="30.4")


def card(title: str) -> str:
    return f"document.querySelector('article[aria-label=\"Message: {title}\"]')"


def card_btn(title: str, label: str) -> str:
    return f"[...{card(title)}.querySelectorAll('button')].find(b => b.textContent.trim() === '{label}')"


@needs_chrome
def test_a_message_that_notifies_people_says_so_on_the_card_and_before_posting(tmp_path, cdp):
    with live_server(tmp_path, seed_mentions, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{card('Message A')} && !!{card('Message B')} && !!{card('Message C')}")
        # typed by him and kept: the card warns, with the words of what really goes out
        assert cdp.js(f"{card('Message A')}.querySelector('.ping-note').textContent") == (
            "Posting this notifies everyone in the channel (@channel).")
        assert cdp.js(f"{card('Message C')}.querySelector('.ping-note').textContent") == (
            "Posting this notifies everyone in the channel (@channel) and everyone who is online (@here).")
        # added by the model: it is defused at send time, so there is nothing to warn about
        assert cdp.js(f"{card('Message B')}.querySelector('.ping-note')") is None
        cdp.js(f"{card_btn('Message A', 'Post')}.click()")
        assert cdp.wait(f"{card('Message A')}.querySelector('.confirm-q')?.textContent") == (
            "Post this as a new message in the program channel, as you? "
            "It notifies everyone in the channel (@channel).")
        assert _commands(settings) == []  # nothing is queued until Confirm
        cdp.js(f"{card_btn('Message B', 'Post')}.click()")
        assert cdp.wait(f"{card('Message B')}.querySelector('.confirm-q')?.textContent") == (
            "Post this as a new message in the program channel, as you?")
        cdp.close()


@needs_chrome
def test_a_message_card_posts_edits_shortens_and_skips_through_the_usual_commands(tmp_path, cdp):
    with live_server(tmp_path, seed_message, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{MSG}")
        assert cdp.js(f"{MSG}.querySelector('.auto-chip')") is None  # nothing about a message posts by itself
        cdp.js(f"{_btn('Message', 'Post')}.click()")
        cdp.wait(f"!!({_btn('Message', 'Confirm')})")
        cdp.js(f"{_btn('Message', 'Confirm')}.click()")
        cdp.wait(f"{MSG}.querySelector('.cstatus.s-queued')")
        (cmd,) = _commands(settings)
        assert (cmd.item_id, cmd.verb, cmd.state, cmd.shown_hash) == (
            MID, "post", "queued", hashlib.sha256(POLISHED.encode()).hexdigest())  # tied to the draft he saw
        tick(settings, lambda st: st.set_page_command(cmd.id, "failed", "draft changed, reload"))
        cdp.wait(f"{MSG}.querySelector('.cstatus.s-failed')")
        cdp.js(f"{_btn('Message', 'Edit')}.click()")
        cdp.wait(f"!!{MSG}.querySelector('textarea')")
        cdp.js(f"{{ const t = {MSG}.querySelector('textarea'); t.value = 'make it warmer'; t.dispatchEvent(new Event('input')); }}")
        cdp.js(f"{_btn('Message', 'Revise')}.click()")
        cdp.wait(f"{MSG}.querySelector('.cstatus.s-queued')")
        edits = [c for c in _commands(settings) if c.verb == "edit"]
        assert [(c.arg, c.shown_hash) for c in edits] == [("make it warmer", hashlib.sha256(POLISHED.encode()).hexdigest())]
        cdp.close()


@needs_chrome
def test_a_revised_message_replaces_its_card_with_the_new_note(tmp_path, cdp):
    with live_server(tmp_path, seed_message, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!{MSG}")
        cdp.js(f"{_btn('Message', 'Shorter')}.click()")
        cdp.wait(f"{MSG}.querySelector('.cstatus.s-queued')")
        (cmd,) = _commands(settings)

        def revised(st):
            st.mark(MID, "delivered", body="Standup is at 3 pm.", edited=True)
            st.set_cursor(f"message_note:{MID}", "Shorter, as asked.")
            st.set_page_command(cmd.id, "done", "✏️ Revised below")
        tick(settings, revised)
        cdp.wait(f"{MSG}.querySelector('.draft')?.textContent === 'Standup is at 3 pm.'", timeout=10)
        assert cdp.js(f"{MSG}.querySelector('.msg-note').textContent") == "What changed Shorter, as asked."
        assert cdp.js(f"{MSG}.querySelector('.msg-orig-text').textContent") == ROUGH  # his words stay as he wrote them
        cdp.close()


@needs_chrome
@pytest.mark.parametrize("dark", [True, False], ids=["midnight", "daylight"])
def test_the_composer_and_a_message_card_fit_a_phone_in_both_themes(tmp_path, cdp, dark):
    def seed(store, settings):
        put_message(store, settings, body="w" * 1500, raw="r" * 1500)
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login), width=380, dark=dark)
        cdp.wait(f"!!({TA}) && !!{MSG}")
        cdp.js(typed("y" * 2000))  # a long line with no spaces
        time.sleep(0.8)  # the entrance animations settle
        assert cdp.js("document.documentElement.dataset.theme") == ("midnight" if dark else "daylight")
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        assert cdp.js(f"{BOX}.getBoundingClientRect().right") <= 380
        assert cdp.js(f"{MSG}.getBoundingClientRect().right") <= 380
        assert cdp.js(f"{BTN}.getBoundingClientRect().width") > 280  # the Polish button takes the whole row
        cdp.js(f"{MSG}.querySelector('details.msg-orig summary').click()")  # opening his words stays inside, too
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        # text colours really are the tokens the AA test checks, in this theme
        assert cdp.js(f"getComputedStyle({BOX}.querySelector('.composer-hint')).color") != \
            cdp.js(f"getComputedStyle({TA}).color")
        cdp.close()


def seed_compose_running(store: Store, settings) -> None:
    now = datetime.now(settings.tz)
    store.set_cursor("tick_seen", now.isoformat())
    store.add_event("compose", "polish", model="deepseek-v4-pro", started=now, ended=None, ok=None, kind=None)


@needs_chrome
def test_the_live_pipeline_names_the_composers_steps(tmp_path):
    with live_server(tmp_path, seed_compose_running) as (base, _settings, login):
        dom = render_dom(tmp_path, base, login)
    now = section(dom, "Right now")
    assert "Polishing your message" in now and "deepseek-v4-pro" in now
    assert re.search(r'class="step live"[^>]*aria-current="step".*?Polish', now, re.S)
    assert re.search(r'class="step todo".*?Your DM', now, re.S)


def test_the_composer_module_is_among_the_checked_scripts():
    """The shared static checks (no HTML sinks, no eval, no inline style) walk every script: this one is in the set."""
    assert STATIC_DIR / "compose.js" in JS


# ---------------------------------------------------------------- the huddle hold (review I1)

HELD_IDLE = "Polish waits until today's Next Todo is out"
HELD_QUEUED = "Waits until today's Next Todo is out"


def seed_held(store: Store, settings) -> None:
    store.set_cursor("message_held_at", datetime.now(settings.tz).isoformat())  # a tick held a moment ago


@needs_chrome
def test_while_a_tick_holds_the_composer_back_it_says_so_before_and_after_the_click(tmp_path, cdp):
    with live_server(tmp_path, seed_held, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({TA}) && {STATUS}.textContent.includes({json.dumps(HELD_IDLE)})")
        assert cdp.js(f"{STATUS}.classList.contains('s-held')") is True
        cdp.js(typed(ROUGH))
        assert cdp.js(f"{BTN}.disabled") is False  # it can still be asked: the row waits its turn
        cdp.js(f"{BTN}.click()")
        cdp.wait(f"{STATUS}.classList.contains('s-queued') && {STATUS}.textContent.includes({json.dumps(HELD_QUEUED)})")
        (cmd,) = compose_commands(settings)
        assert cmd.state == "queued"  # the tick leaves it alone: it is not claimed, so nothing in the page changes
        assert cdp.js(f"{BTN}.textContent.trim()") == "Polishing…" and cdp.js(f"{TA}.readOnly") is True
        cdp.close()


@needs_chrome
def test_without_a_hold_the_composer_says_nothing_about_the_huddle(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({TA})")
        time.sleep(0.8)
        assert cdp.js(f"getComputedStyle({STATUS}).display") == "none"
        cdp.js(typed(ROUGH))
        cdp.js(f"{BTN}.click()")
        cdp.wait(f"{STATUS}.classList.contains('s-queued')")
        assert "next tick" in cdp.js(f"{STATUS}.textContent") and "Next Todo" not in cdp.js(f"{STATUS}.textContent")
        cdp.close()
