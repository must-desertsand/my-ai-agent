"""The deck on an insight's card (2026-10-21): the frontend. Static checks read the files under dashboard/static. The
render checks start a real uvicorn on 127.0.0.1 against a tmp store (nothing ever runs: the thumbnail runner is a fake
that writes a tiny PNG or fails), log in as `dashboard open` does, and drive headless Chrome over CDP, as the Task 8,
run-controls and composer tests do. They are skipped when Chrome is missing. Offline guard on; tmp data only.

The deck job is played by the test: it writes the deck's cursor (and its PDF) into the tmp store the page is reading,
and adds a `decks` step event, which is what the real job leaves."""
from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from haseeb_agent.dashboard.app import STATIC_DIR
from haseeb_agent.jobs.decks import content_hash
from haseeb_agent.store import Store
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    CARD, HOSTILE_BIDI, HOSTILE_IMG, HOSTILE_SCRIPT, IMG_LOADS, OPEN_SPY, PWN, VIEW_SAFE, _code, _commands, _contrast,
    _item, _rgb, _selectors, _surfaces, cdp, fake_sips, live_server, login_url, needs_chrome, tiny_pdf,
)
from tests.test_insights import good_meta

DM_LINE = "The deck isn't in your DM yet: post now and it won't be attached (you can attach it later)."
EDIT_LINE = ("Your edit needs a new deck. It isn't ready yet, so it won't be attached now "
             "(you can attach it when it's ready).")
OPEN_FAILED = "Couldn't open the PDF. Try again in a moment."
OPEN_BLOCKED = "The browser blocked the new tab. Allow pop-ups for this page, then try again."

# every thumbnail and every inbox request the page makes
THUMB_REC = """(() => { const f = window.fetch; window.__thumbs = [];
  window.__inbox = 0;
  window.fetch = function (u) { const url = String(u);
    if (url.includes('/thumb.png')) window.__thumbs.push(url);
    if (url.startsWith('/api/inbox')) window.__inbox += 1;
    return f.apply(this, arguments); }; })();"""
# the inbox's 30 s refresh never fires: only the stream (a `decks` step event) can bring a deck's news to a card
# the arguments window.open was called with, after the url
OPEN_ARGS = """(() => { const o = window.open; window.__args = [];
  window.open = function () { window.__args.push([...arguments].slice(1)); return o.apply(this, arguments); }; })();"""
NO_REFRESH = """(() => { const si = window.setInterval;
  window.setInterval = function (fn, ms, ...rest) { return ms === 30000 ? 0 : si.call(this, fn, ms, ...rest); }; })();"""


# ---------------------------------------------------------------- seeding

def iid(n: int) -> str:
    return f"insight-t3-{n}"


def card(n: int) -> str:
    return f"document.querySelector('article.icard[data-item=\"{iid(n)}\"]')"


def strip(n: int, rest: str = "") -> str:
    return f"{card(n)}.querySelector('.dstrip'){rest}"


def part(n: int, css: str, prop: str = "") -> str:
    """A part of insight n's strip (css without quotes), and optionally a property of it: `.hidden`, `.textContent`."""
    return f"{card(n)}.querySelector('.dstrip {css}'){prop}"


def words(n: int) -> str:
    return strip(n, ".querySelector('.dstrip-words').textContent")


def post_btn(n: int) -> str:
    return f"[...{card(n)}.querySelectorAll('.actions button')].find(b => b.textContent.trim() === 'Post')"


def note_of(n: int) -> str:
    """The Post confirm's line about the deck, or '' when the confirm is open without one (null: no confirm)."""
    return (f"(() => {{ const c = {card(n)}; if (!c || !c.querySelector('.actions.confirm')) return null; "
            "const x = c.querySelector('.confirm-note'); return x && !x.hidden ? x.textContent.trim() : ''; })()")


def put_insight(store, settings, n: int, *, title: str | None = None, text: dict | None = None,
                status: str = "delivered", dm_ts: str | None = None) -> dict:
    """A delivered insight of today with a body the deck job can read. Returns its saved meta."""
    meta = good_meta(batch=iid(n))
    if text:
        meta["insight"] = {**meta["insight"], **text}
    now = datetime.now().astimezone()
    item = _item(iid(n), "insight", title or f"Topic {n}", json.dumps(meta), datetime.now(settings.tz).date(),
                 dm_ts or f"{20 + n}.1")
    store.upsert_item(replace(item, status=status, created_at=now))
    return meta


def put_deck_state(store, settings, n: int, meta: dict, *, pages: int = 3, model: str = "codex gpt-6.1-sol",
                   dm_at: datetime | None = None, sha: str | None = None, **more) -> Path:
    """The deck job's cursor for insight n: a deck for `meta`'s text (or for `sha`) with a real PDF on disk."""
    pdf = Path(settings.db_path).parent / "decks" / iid(n) / f"{iid(n)}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(tiny_pdf(pages))
    st = {"state": "done", "pdf": str(pdf), "model": model, "sha": sha or content_hash(meta["insight"]), **more}
    if dm_at is not None:
        st["dm_at"] = f"{dm_at.timestamp():.6f}"
    store.set_cursor(f"deck:{iid(n)}", json.dumps(st))
    return pdf


def write(settings, fn) -> None:
    """Do something to the tmp store while the page is open (the deck job's side)."""
    st = Store(settings.db_path)
    try:
        fn(st, settings)
    finally:
        st.close()


def deck_event(settings, step: str = "upload") -> None:
    """The step event the deck job leaves when a step ends: the stream carries it to the page."""
    now = datetime.now(settings.tz)
    write(settings, lambda st, _s: st.add_event("decks", step, model=None, started=now, ended=now, ok=True, kind=None))


A, B, C, D, E, F = 1, 2, 3, 4, 5, 6


def seed_states(store, settings) -> None:
    """One insight card per state of the deck."""
    now = datetime.now(settings.tz)
    ma = put_insight(store, settings, A, title="Gemini Argon's launch price ends Nov 30")
    put_deck_state(store, settings, A, ma, pages=11, model="codex gpt-6.1-sol (repaired)", dm_at=now)
    mb = put_insight(store, settings, B)
    put_deck_state(store, settings, B, mb, pages=6, model="template (fact-check)", dm_at=now)
    mc = put_insight(store, settings, C)
    put_deck_state(store, settings, C, mc, pages=3)  # built, but its DM upload has no time yet
    put_insight(store, settings, D)  # the deck job has not been at it
    me = put_insight(store, settings, E, text={"title": "A sharper title after the edit"})
    put_deck_state(store, settings, E, good_meta(batch=iid(E)), pages=7, dm_at=now)  # the deck of the text before the edit
    assert content_hash(me["insight"]) != content_hash(good_meta(batch=iid(E))["insight"])
    put_insight(store, settings, F)
    store.set_cursor(f"deck:{iid(F)}", json.dumps({"sha": content_hash(good_meta(batch=iid(F))["insight"]),
                                                   "tries": 3, "state": "gave_up"}))


def ready(count: int = 6) -> str:
    return (f"document.querySelectorAll('article.icard').length === {count} && "
            "[...document.querySelectorAll('.dstrip')].every(s => s.querySelector('.dstrip-words').textContent !== '')")


# ---------------------------------------------------------------- static checks

def test_the_deck_modules_exist_and_the_page_logic_lives_in_one_place():
    for name in ("deckview.js", "deckstrip.js"):
        assert (STATIC_DIR / name).is_file(), name
    code = {n: _code((STATIC_DIR / n).read_text()) for n in ("deckview.js", "deckstrip.js", "decks.js", "inbox.js")}
    # the two things that need the session header, and the PDF's new tab, are written once: in deckview.js
    for name in ("deckstrip.js", "decks.js", "inbox.js"):
        assert "window.open(" not in code[name], name
        assert "thumb.png" not in code[name] and ".pdf`" not in code[name], name
        assert "apiBlob" not in code[name] and "createObjectURL" not in code[name], name
    assert code["deckview.js"].count("window.open(") == 1 and code["deckview.js"].count("apiBlob(") == 2
    # the fact-check badge table is not copied either
    assert "Passed" in code["deckview.js"] and "Passed" not in code["decks.js"] + code["deckstrip.js"]
    # and both pages use the shared module
    assert 'from "./deckview.js"' in (STATIC_DIR / "decks.js").read_text()
    assert 'from "./deckview.js"' in (STATIC_DIR / "deckstrip.js").read_text()
    assert 'from "./deckstrip.js"' in (STATIC_DIR / "inbox.js").read_text()


def test_the_post_confirm_lines_are_the_rulings_words():
    js = (STATIC_DIR / "deckview.js").read_text()
    assert DM_LINE in js and EDIT_LINE in js
    assert "New deck being made for your edit…" in js and "for the previous text" in js and "Designing…" in js


def test_deck_strip_css_sits_under_its_own_names():
    """The new rules redefine nothing the other pages share: the strip and the confirm's line only. The Decks page's
    block (all under `.decks`) stays as it is."""
    css = (STATIC_DIR / "app.css").read_text()
    start, end = "/* ---------- the deck on an insight's card", "/* ---------- composer:"
    sels = _selectors(css[css.index(start):css.index(end)])
    own = re.compile(r"^(?:\.dstrip|\.confirm-note|\.actions\.confirm\.has-note \.(?:confirm-note|btn))")
    loose = [x for x in sels if not own.match(x)]
    assert len(sels) >= 20 and not loose, loose
    assert css.index(start) > css.index("/* ---------- inbox */")


def _strip_surfaces(theme: str):
    """The strip sits on the card's sunken surface; the confirm row's tint sits on it too."""
    t, over, card_bg, sunken = _surfaces(theme)
    panel = over("--panel", sunken)  # the strip
    return t, over, sunken, panel


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_deck_strip_text_colours_meet_aa_where_they_sit(theme):
    t, over, sunken, panel = _strip_surfaces(theme)
    fg = lambda name: _rgb(t[name])[0]  # noqa: E731
    checks = [
        ("the status line", "--text2", panel),
        ("a line with no deck", "--muted", panel),
        ("the previous deck's line", "--muted", panel),
        ("the fact-check badge, passed", "--ok", over("--ok-soft", panel)),
        ("the fact-check badge, repaired", "--a2-text", over("--a2-soft", panel)),
        ("the fact-check badge, template", "--text2", over("--panel2", panel)),
        ("the fact-check badge, refused", "--warn", over("--warn-soft", panel)),
        ("the fact-check badge, no check", "--muted", over("--panel2", panel)),
        ("the failure note", "--warn", over("--warn-soft", panel)),
        ("the Post confirm's line", "--text2", over("--a1-soft", sunken)),
    ]
    for name, token, surface in checks:
        assert _contrast(fg(token), surface) >= 4.5, (theme, name)


# ---------------------------------------------------------------- the strip in each state

@needs_chrome
def test_the_strip_says_what_the_deck_is_in_each_state(tmp_path, cdp):
    with live_server(tmp_path, seed_states, stream_polls=200, sips=fake_sips) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=THUMB_REC + OPEN_SPY)
        cdp.wait(ready())
        got = cdp.js("""Object.fromEntries([...document.querySelectorAll('article.icard')].map(c => [c.dataset.item, {
            state: c.querySelector('.dstrip').dataset.state, words: c.querySelector('.dstrip-words').textContent,
            badge: c.querySelector('.dstrip-line .fc')?.textContent.trim() ?? null,
            prev: c.querySelector('.dstrip-prev').hidden ? null : c.querySelector('.dstrip-prev').textContent.trim(),
            open: c.querySelector('.dstrip-open').hidden ? null : c.querySelector('.dstrip-open').textContent.trim()}]))""")
        assert got == {
            iid(A): {"state": "done", "words": "Codex · 11 pages · in your DM", "badge": "Repaired", "prev": None,
                     "open": "Open deck"},
            iid(B): {"state": "done", "words": "Template · in your DM", "badge": "Template", "prev": None,
                     "open": "Open deck"},
            iid(C): {"state": "done", "words": "Codex · 3 pages", "badge": "Passed", "prev": None,
                     "open": "Open deck"},  # built, but not (yet) in his DM: it does not say so
            iid(D): {"state": "pending", "words": "Designing…", "badge": None, "prev": None, "open": None},
            iid(E): {"state": "pending", "words": "New deck being made for your edit…", "badge": None,
                     "prev": "Passed Codex · 7 pages · for the previous text", "open": "Open deck"},
            iid(F): {"state": "gave_up", "words": "No deck", "badge": None, "prev": None, "open": None},
        }
        # the strip sits between the draft and the buttons, on insight cards only; the one thing between it and them is the card's
        # "Post to" choice (post-channel design: an idea and an insight take a channel)
        assert cdp.js(f"{card(A)}.querySelector('.dstrip').nextElementSibling === {card(A)}.querySelector('.post-to')"
                      f" && {card(A)}.querySelector('.post-to').nextElementSibling === {card(A)}.querySelector('.actions')")
        assert cdp.js("document.querySelectorAll('.dstrip').length") == 6
        cdp.close()


@needs_chrome
def test_a_card_that_is_not_an_insight_has_no_strip(tmp_path, cdp):
    from tests.test_dashboard_frontend import seed_two
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 2")
        assert cdp.js("document.querySelectorAll('.dstrip').length") == 0
        cdp.close()


@needs_chrome
def test_thumbnails_are_asked_for_only_by_decks_that_have_a_page_to_show(tmp_path, cdp):
    with live_server(tmp_path, seed_states, stream_polls=200, sips=fake_sips) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=THUMB_REC + OPEN_SPY)
        cdp.wait(ready())
        # A, B, C (done) and E (the previous deck): one each. D (pending) and F (gave up) ask for nothing.
        cdp.wait("window.__thumbs.length === 4 && document.querySelectorAll('.dstrip img').length === 4")
        asked = sorted(cdp.js("window.__thumbs"))
        assert asked == sorted(f"/api/decks/{iid(n)}/thumb.png" for n in (A, B, C, E))
        srcs = cdp.js("[...document.querySelectorAll('.dstrip img')].map(i => i.getAttribute('src'))")
        assert len(srcs) == 4 and all(s.startswith("blob:") for s in srcs)  # fetched with the header, never a link
        cdp.wait("[...document.querySelectorAll('.dstrip img')].every(i => i.complete && i.naturalWidth === 16)")
        assert cdp.js("document.querySelectorAll('#view a').length") == 0
        assert cdp.js(IMG_LOADS % json.dumps(srcs[0])) == "loaded"
        # nothing is asked again, and nothing is repainted, by a refresh that changes nothing (a `decks` step event
        # makes the inbox look again)
        cdp.js("document.querySelectorAll('.dstrip, .dstrip-words > span, .dstrip img').forEach(n => n.__kept = 1)")
        looked = cdp.js("window.__inbox")
        deck_event(settings)
        cdp.wait(f"window.__inbox > {looked}")
        time.sleep(0.4)
        assert len(cdp.js("window.__thumbs")) == 4
        assert cdp.js("[...document.querySelectorAll('.dstrip, .dstrip-words > span, .dstrip img')].every(n => n.__kept === 1)")
        # leaving the page revokes every thumbnail's URL
        cdp.js("location.hash = '#/history'")
        cdp.wait("!document.querySelector('.page-live')")
        assert set(srcs) <= set(cdp.js("window.__revoked"))
        assert cdp.js(IMG_LOADS % json.dumps(srcs[0])) == "gone"
        cdp.close()


@needs_chrome
def test_a_deck_without_a_preview_stops_loading(tmp_path, cdp):
    """The default thumbnail runner fails (no sips): the placeholder of a finished deck goes still instead of
    breathing for ever, and the deck can still be opened. A deck that is not coming never breathed."""
    with live_server(tmp_path, seed_states, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login), init_js=THUMB_REC)
        cdp.wait(ready())
        cdp.wait("window.__thumbs.length === 4")
        cdp.wait("document.querySelectorAll('.dstrip-ph.loading').length === 1")  # only D, the deck being made
        loading = cdp.js("[...document.querySelectorAll('.dstrip')].filter(s => s.querySelector('.dstrip-ph.loading'))"
                         ".map(s => s.closest('article').dataset.item)")
        assert loading == [iid(D)]
        assert cdp.js("document.querySelectorAll('.dstrip img').length") == 0
        names = cdp.js("Object.fromEntries([...document.querySelectorAll('.dstrip')].map(s => [s.closest('article')"
                       ".dataset.item, getComputedStyle(s.querySelector('.dstrip-ph .slide')).animationName]))")
        assert names[iid(D)] == "deck-breathe" and all(v == "none" for k, v in names.items() if k != iid(D)), names
        assert cdp.js(part(A, ".dstrip-open", ".disabled")) is False
        cdp.close()


def _with_decks_off(store, settings):
    put_insight(store, settings, A)
    ma = put_insight(store, settings, B)
    put_deck_state(store, settings, B, ma, dm_at=datetime.now(settings.tz))  # built before decks were turned off


@needs_chrome
def test_with_decks_turned_off_the_strip_says_no_deck_and_does_not_load(tmp_path, cdp):
    with live_server(tmp_path, _with_decks_off, stream_polls=200, patch={"decks_enabled": False}) as (base, _s, login):
        cdp.open(login_url(base, login), init_js=THUMB_REC)
        cdp.wait("document.querySelectorAll('.dstrip').length === 2")
        cdp.wait(f"{words(A)} === 'No deck'")
        assert cdp.js(f"{strip(A)}.dataset.state") == "off"
        assert cdp.js(part(A, ".dstrip-ph", ".classList.contains('loading')")) is False
        assert cdp.js(part(A, ".dstrip-open", ".hidden")) is True
        assert cdp.js(f"{words(B)}") == "Codex · 3 pages · in your DM"  # a deck that was built still shows
        time.sleep(0.5)
        assert cdp.js("window.__thumbs") == [f"/api/decks/{iid(B)}/thumb.png"]
        cdp.close()


# ---------------------------------------------------------------- Open deck

@needs_chrome
def test_open_deck_opens_the_pdf_from_a_blob_url_and_the_previous_one_too(tmp_path, cdp):
    with live_server(tmp_path, seed_states, stream_polls=200, sips=fake_sips) as (base, _s, login):
        cdp.open(login_url(base, login), init_js=OPEN_SPY + OPEN_ARGS)
        cdp.wait(ready())
        for n, pages in ((A, 11), (E, 7)):  # E: the deck of the text before the edit
            before = len(cdp.js("window.__opened"))
            cdp.js(part(n, ".dstrip-open", ".click()"))
            url = cdp.wait(f"window.__opened[{before}]")
            assert url.startswith("blob:")  # fetched with the session header, opened from a blob: URL
            assert cdp.js(f"window.__blobs[{json.dumps(url)}]") == {"type": "application/pdf", "size": len(tiny_pdf(pages))}
            assert url not in cdp.js("window.__revoked")  # kept a while, so the new tab can read it
        assert cdp.js("document.querySelectorAll('#view a').length") == 0
        assert cdp.js(part(A, ".dstrip-open", ".textContent.trim()")) == "Open deck"
        # a new tab named "_blank" and nothing else: a "noopener" feature would make window.open return null, and a
        # blocked pop-up could no longer be told from a tab that opened
        assert cdp.js("window.__args") == [["_blank"], ["_blank"]]
        cdp.close()


@needs_chrome
def test_open_deck_says_so_when_the_pdf_cannot_be_opened(tmp_path, cdp):
    with live_server(tmp_path, seed_states, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=OPEN_SPY)
        cdp.wait(ready())
        write(settings, lambda st, _s: Path(json.loads(st.get_cursor(f"deck:{iid(A)}"))["pdf"]).unlink())  # off the disk
        cdp.js(part(A, ".dstrip-open", ".click()"))
        text = cdp.wait(f"{part(A, '.dstrip-note', '.hidden')} === false && {part(A, '.dstrip-note', '.textContent')}")
        assert text == OPEN_FAILED
        assert cdp.js("window.__opened") == [] and cdp.js(part(A, ".dstrip-note", ".getAttribute('role')")) == "alert"
        cdp.close()


@needs_chrome
def test_open_deck_says_so_when_the_browser_blocks_the_tab(tmp_path, cdp):
    blocked = "(() => { window.open = () => null; })();"
    with live_server(tmp_path, seed_states, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login), init_js=blocked)
        cdp.wait(ready())
        cdp.js(part(A, ".dstrip-open", ".click()"))
        text = cdp.wait(f"{part(A, '.dstrip-note', '.hidden')} === false && {part(A, '.dstrip-note', '.textContent')}")
        assert text == OPEN_BLOCKED
        cdp.close()


# ---------------------------------------------------------------- the shared helpers

@needs_chrome
def test_the_thumbnail_and_pdf_helpers_do_nothing_for_a_page_that_has_gone(tmp_path, cdp):
    """`alive()` is asked before a thumbnail is requested and again when its bytes are back, and after a PDF has been
    fetched: a card that was replaced, or a page that was left, gets no request, no blob URL and no new tab."""
    with live_server(tmp_path, seed_states, stream_polls=200, sips=fake_sips) as (base, _s, login):
        cdp.open(login_url(base, login), init_js=THUMB_REC + OPEN_SPY)
        cdp.wait(ready())
        cdp.wait("window.__thumbs.length === 4")
        before = cdp.js("[window.__thumbs.length, Object.keys(window.__blobs).length]")
        got = cdp.js(f"""import('/deckview.js').then(async (m) => {{
          const gone = () => false;
          return {{thumb: await m.fetchDeckThumb({json.dumps(iid(A))}, gone), pdf: await m.openDeckPdf({json.dumps(iid(A))}, gone),
                  asked: window.__thumbs.length, blobs: Object.keys(window.__blobs).length, opened: window.__opened.length}}; }})""")
        assert got["thumb"] is None and got["pdf"] == "gone"
        assert (got["asked"], got["blobs"] - before[1], got["opened"]) == (before[0], 0, 0)  # no request, no URL, no tab
        got = cdp.js(f"""import('/deckview.js').then(async (m) => {{
          let n = 0;
          const flaky = () => n++ === 0;  // there at its turn, gone when the bytes are back
          const u = await m.fetchDeckThumb({json.dumps(iid(A))}, flaky);
          return {{url: u, asked: window.__thumbs.length, blobs: Object.keys(window.__blobs).length}}; }})""")
        assert got["url"] is None and got["asked"] == before[0] + 1 and got["blobs"] == before[1] + 0  # asked, but no URL
        got = cdp.js(f"""import('/deckview.js').then(async (m) => {{
          const live = () => true;
          const u = await m.fetchDeckThumb({json.dumps(iid(A))}, live);
          return {{url: u, asked: window.__thumbs.length, result: await m.openDeckPdf({json.dumps(iid(A))}, live),
                  opened: window.__opened.length}}; }})""")
        assert got["url"].startswith("blob:") and got["asked"] == before[0] + 2 and got["result"] == "ok"
        assert got["opened"] == 1
        cdp.close()


@needs_chrome
def test_an_edit_whose_new_build_has_replaced_the_old_file_shows_no_old_deck(tmp_path, cdp):
    """The new deck is built (its file has taken the old one's place) but is not in his DM yet: there is no previous
    deck left to open or show, only the news that a new one is coming, and a Post now would not carry it."""
    def seed(store, settings):
        before = good_meta(batch=iid(D))
        edited = put_insight(store, settings, D, text={"title": "Edited: the headline after his edit"})
        old = put_deck_state(store, settings, D, before, pages=7, dm_at=datetime.now(settings.tz))
        prev = json.loads(store.get_cursor(f"deck:{iid(D)}"))
        new = content_hash(edited["insight"])
        store.set_cursor(f"deck:{iid(D)}", json.dumps({
            "sha": new, "tries": 1, "state": "pending", "built": new, "pdf": str(old), "model": "template",
            "prev": {"sha": prev["sha"], "pdf": prev["pdf"], "model": prev["model"]}}))
    with live_server(tmp_path, seed, stream_polls=200, sips=fake_sips) as (base, _s, login):
        cdp.open(login_url(base, login), init_js=THUMB_REC)
        cdp.wait(f"{words(D)} === 'New deck being made for your edit…'")
        assert cdp.js(part(D, ".dstrip-prev", ".hidden")) is True and cdp.js(part(D, ".dstrip-open", ".hidden")) is True
        assert cdp.js(f"{strip(D)}.classList.contains('is-previous')") is False
        assert cdp.js(part(D, ".dstrip-ph", ".classList.contains('loading')")) is True  # a new deck is on its way
        time.sleep(0.5)
        assert cdp.js("window.__thumbs") == [] and cdp.js(f"{strip(D)}.querySelectorAll('img').length") == 0
        cdp.js(f"{post_btn(D)}.click()")
        cdp.wait(f"{card(D)}.querySelector('.confirm-q')")
        assert cdp.js(note_of(D)) == EDIT_LINE
        cdp.close()


# ---------------------------------------------------------------- it follows the deck, without a reload

@needs_chrome
def test_a_finished_deck_appears_on_its_card_without_a_reload(tmp_path, cdp):
    def seed(store, settings):
        put_insight(store, settings, D)
    with live_server(tmp_path, seed, stream_polls=400, sips=fake_sips) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=NO_REFRESH + THUMB_REC)  # no 30 s refresh: only the stream can tell it
        cdp.wait(f"{words(D)} === 'Designing…'")
        assert cdp.js("window.__thumbs") == [] and cdp.js(f"{strip(D)}.querySelector('.dstrip-ph.loading') !== null")
        cdp.js("window.__same_page = 1; document.querySelector('article.icard').__same_card = 1")
        meta = good_meta(batch=iid(D))
        write(settings, lambda st, s: put_deck_state(st, s, D, meta, pages=9, dm_at=datetime.now(s.tz)))
        deck_event(settings)  # the deck job's upload step ended
        text = cdp.wait(f"{words(D)} !== 'Designing…' && {words(D)}", timeout=10)
        assert text == "Codex · 9 pages · in your DM"
        cdp.wait(f"{strip(D)}.querySelector('img.on') !== null")  # its first page, fetched now
        assert cdp.js("window.__thumbs") == [f"/api/decks/{iid(D)}/thumb.png"]
        assert cdp.js(part(D, ".dstrip-open", ".hidden")) is False
        assert cdp.js(part(D, ".dstrip-ph", ".hidden")) is True  # the picture covers the placeholder
        assert cdp.js(f"{strip(D)}.querySelector('.fc').textContent.trim()") == "Passed"
        assert cdp.js("window.__same_page === 1 && document.querySelector('article.icard').__same_card === 1")  # no reload
        cdp.close()


@needs_chrome
def test_a_deck_that_gives_up_stops_breathing_on_its_card(tmp_path, cdp):
    def seed(store, settings):
        put_insight(store, settings, D)
    with live_server(tmp_path, seed, stream_polls=400, sips=fake_sips) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=NO_REFRESH + THUMB_REC)
        cdp.wait(f"{words(D)} === 'Designing…'")
        meta = good_meta(batch=iid(D))
        write(settings, lambda st, s: st.set_cursor(
            f"deck:{iid(D)}", json.dumps({"sha": content_hash(meta["insight"]), "tries": 3, "state": "gave_up"})))
        deck_event(settings, "design")
        cdp.wait(f"{words(D)} === 'No deck'", timeout=10)
        assert cdp.js(f"{strip(D)}.querySelector('.dstrip-ph.loading')") is None
        assert cdp.js(f"getComputedStyle({strip(D)}.querySelector('.slide')).animationName") == "none"
        assert cdp.js("window.__thumbs") == [] and cdp.js(part(D, ".dstrip-open", ".hidden")) is True
        cdp.close()


# ---------------------------------------------------------------- the Post confirm

@needs_chrome
def test_the_post_confirm_says_when_a_post_now_would_not_carry_the_deck(tmp_path, cdp):
    with live_server(tmp_path, seed_states, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait(ready())
        want = {A: "", B: "", C: DM_LINE, D: DM_LINE, E: EDIT_LINE, F: ""}  # '': the confirm opens without the line
        for n, line in want.items():
            cdp.js(f"{post_btn(n)}.click()")
            cdp.wait(f"{card(n)}.querySelector('.confirm-q')")
            assert cdp.js(note_of(n)) == line, n
            assert cdp.js(f"{card(n)}.querySelector('.confirm-q').textContent") == "Post to the program channel as you?"
            if line:  # it is read with the two buttons
                box_id = cdp.js(f"{card(n)}.querySelector('.confirm-note').id")
                assert cdp.js(f"[...{card(n)}.querySelectorAll('.actions.confirm button')]"
                              f".every(b => b.getAttribute('aria-describedby') === {json.dumps(box_id)})")
            else:
                assert cdp.js(f"{card(n)}.querySelector('.actions.confirm').classList.contains('has-note')") is False
            cdp.js(f"[...{card(n)}.querySelectorAll('button')].find(b => b.textContent.trim() === 'Cancel').click()")
            cdp.wait(f"!{card(n)}.querySelector('.confirm-q')")
        assert _commands(_s) == []  # Cancel posts nothing
        cdp.close()


@needs_chrome
def test_a_post_confirm_with_decks_turned_off_has_no_line(tmp_path, cdp):
    def seed(store, settings):
        put_insight(store, settings, A)
    with live_server(tmp_path, seed, stream_polls=200, patch={"decks_enabled": False}) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"{words(A)} === 'No deck'")
        cdp.js(f"{post_btn(A)}.click()")
        cdp.wait(f"{card(A)}.querySelector('.confirm-q')")
        assert cdp.js(note_of(A)) == ""
        cdp.close()


@needs_chrome
def test_edit_then_post_warns_until_the_new_deck_is_in_the_dm(tmp_path, cdp):
    """The live incident: Haseeb edited an insight and posted from the dashboard. The deck in his DM was for the text
    before the edit, so R50 did not attach it, and nothing warned him. Now the card says the new deck is being made,
    shows the old one as the previous text's, and the Post confirm warns; once the new deck is in the DM, it does not."""
    def seed(store, settings):
        before = good_meta(batch=iid(A))
        put_insight(store, settings, A, text={"title": "Edited: the headline after his edit"})
        put_deck_state(store, settings, A, before, pages=7, dm_at=datetime.now(settings.tz) - timedelta(minutes=9))
    with live_server(tmp_path, seed, stream_polls=400, sips=fake_sips) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=NO_REFRESH + THUMB_REC + OPEN_SPY)
        cdp.wait(f"{words(A)} === 'New deck being made for your edit…'")
        assert "in your DM" not in cdp.js(f"{strip(A)}.textContent")  # the old deck's upload does not count for the edit
        assert cdp.js(part(A, ".dstrip-prev", ".textContent.trim()")) == "Passed Codex · 7 pages · for the previous text"
        cdp.wait(f"{strip(A)}.querySelector('img.on') !== null")  # the old deck's page is still shown
        assert cdp.js(f"{strip(A)}.classList.contains('is-previous')") is True
        # Post now: the warning fires
        cdp.js(f"{post_btn(A)}.click()")
        cdp.wait(f"{card(A)}.querySelector('.confirm-q')")
        assert cdp.js(note_of(A)) == EDIT_LINE
        cdp.js(f"[...{card(A)}.querySelectorAll('button')].find(b => b.textContent.trim() === 'Cancel').click()")
        cdp.wait(f"!{card(A)}.querySelector('.confirm-q')")
        # the deck job finishes the new deck and uploads it
        asked = len(cdp.js("window.__thumbs"))

        def new_deck(st, s):  # the deck job, for the text as it is now
            put_deck_state(st, s, A, json.loads(st.get_item(iid(A)).body), pages=8, model="template",
                           dm_at=datetime.now(s.tz))
        write(settings, new_deck)
        deck_event(settings)
        text = cdp.wait(f"{words(A)} !== 'New deck being made for your edit…' && {words(A)}", timeout=10)
        assert text == "Template · in your DM"
        assert cdp.js(part(A, ".dstrip-prev", ".hidden")) is True
        assert cdp.js(f"{strip(A)}.classList.contains('is-previous')") is False
        cdp.wait(f"window.__thumbs.length === {asked + 1}")  # the new deck's first page
        # Post now: the warning is gone
        cdp.js(f"{post_btn(A)}.click()")
        cdp.wait(f"{card(A)}.querySelector('.confirm-q')")
        assert cdp.js(note_of(A)) == ""
        cdp.close()


@needs_chrome
def test_an_open_post_confirm_follows_the_deck(tmp_path, cdp):
    """The confirm is open while the deck lands (or while the deck is made): its line comes and goes in place, and the
    row (and the focus on Cancel) is not rebuilt under his hand."""
    def seed(store, settings):
        put_insight(store, settings, D)
    with live_server(tmp_path, seed, stream_polls=400) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=NO_REFRESH)
        cdp.wait(f"{words(D)} === 'Designing…'")
        cdp.js(f"{post_btn(D)}.click()")
        cdp.wait(f"{card(D)}.querySelector('.confirm-q')")
        assert cdp.wait("document.activeElement.textContent.trim() === 'Cancel'")
        assert cdp.js(note_of(D)) == DM_LINE
        cdp.js(f"{card(D)}.querySelector('.actions').__kept = 1")
        meta = good_meta(batch=iid(D))
        write(settings, lambda st, s: put_deck_state(st, s, D, meta, dm_at=datetime.now(s.tz)))
        deck_event(settings)
        cdp.wait(f"{words(D)} === 'Codex · 3 pages · in your DM'", timeout=10)
        assert cdp.js(note_of(D)) == ""  # it saw the deck land
        assert cdp.js(f"{card(D)}.querySelector('.actions').__kept") == 1
        assert cdp.js("document.activeElement.textContent.trim()") == "Cancel"
        assert cdp.js(f"{card(D)}.querySelector('.actions.confirm').classList.contains('has-note')") is False
        cdp.close()


# ---------------------------------------------------------------- hostile text, themes, the phone

HOSTILE = f"{PWN} {HOSTILE_SCRIPT} {HOSTILE_BIDI}"


@needs_chrome
def test_hostile_titles_and_models_still_render_as_text_next_to_a_deck(tmp_path, cdp):
    def seed(store, settings):
        meta = put_insight(store, settings, A, title=HOSTILE, text={"title": HOSTILE_IMG, "what_happened": HOSTILE})
        put_deck_state(store, settings, A, meta, model=f"codex {HOSTILE_IMG}", dm_at=datetime.now(settings.tz))
        put_insight(store, settings, B, title=HOSTILE_IMG)
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('.dstrip').length === 2 && " + words(A) + " !== ''")
        cdp.js(f"{post_btn(B)}.click()")
        cdp.wait(f"{card(B)}.querySelector('.confirm-note')")
        s = cdp.js(VIEW_SAFE)
        assert (s["imgs"], s["scripts"], s["pwned"], s["tags"], s["links"]) == (0, 2, False, 0, 0)
        assert s["seen"] >= 1 and s["loose"] == 0, (s["seen"], s["loose"])
        assert cdp.js(f"{card(A)}.querySelector('.icard-title').textContent") == HOSTILE
        assert HOSTILE_IMG in cdp.js(f"{card(A)}.querySelector('.draft').textContent")
        assert f"codex {HOSTILE_IMG}" not in s["text"]  # the model's name is not shown on the card at all
        assert cdp.js(f"{words(A)}") == "Codex · 3 pages · in your DM"
        cdp.close()


def _widest() -> str:
    return ("[...document.querySelectorAll('#view .icard, #view .dstrip, #view .dstrip *, #view .actions, "
            "#view .actions *')].every(e => e.getBoundingClientRect().right <= document.documentElement.clientWidth + .5)")


@needs_chrome
def test_the_strip_fits_a_phone_in_every_state_and_in_the_confirm(tmp_path, cdp):
    def seed(store, settings):
        seed_states(store, settings)
        long_meta = put_insight(store, settings, 7, title="W" * 300, text={"title": "T" * 200})
        put_deck_state(store, settings, 7, long_meta, model="codex " + "m" * 200, dm_at=datetime.now(settings.tz))
    with live_server(tmp_path, seed, stream_polls=200, sips=fake_sips) as (base, _s, login):
        cdp.open(login_url(base, login), width=380)
        cdp.wait(ready(7))
        time.sleep(0.9)  # the entrance settles
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        assert cdp.js(_widest())
        for n in (D, E, C):  # the lines of the confirm are the longest text of all
            cdp.js(f"{post_btn(n)}.click()")
            cdp.wait(f"{card(n)}.querySelector('.confirm-note') && !{card(n)}.querySelector('.confirm-note').hidden")
            assert cdp.js("document.documentElement.scrollWidth") <= 380, n
            assert cdp.js(_widest()), n
            # on a phone the line sits between the question and the buttons
            assert cdp.js(f"""(() => {{ const r = (e) => e.getBoundingClientRect().top; const c = {card(n)};
              return r(c.querySelector('.confirm-q')) < r(c.querySelector('.confirm-note')) &&
                r(c.querySelector('.confirm-note')) < r(c.querySelector('.actions.confirm button')); }})()""") is True
            # the strip's own button is as wide as the strip, under the words
            assert cdp.js(f"""(() => {{ const s = {strip(n)}; const o = s.querySelector('.dstrip-open');
              return o.hidden || o.getBoundingClientRect().width >= s.getBoundingClientRect().width - 30; }})()""")
        cdp.close()


def _rgb_of(v: str):  # "rgb(196, 200, 214)" or "#c4c8d6"
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", v)
    return tuple(int(x) for x in m.groups()) if m else _rgb(v)[0]


@needs_chrome
@pytest.mark.parametrize("dark,theme", [(True, "midnight"), (False, "daylight")])
def test_the_strip_shows_in_both_themes_with_the_token_colours(tmp_path, cdp, dark, theme):
    tokens = _surfaces(theme)[0]  # the colours the CSS file gives this theme
    with live_server(tmp_path, seed_states, stream_polls=200, sips=fake_sips) as (base, _s, login):
        cdp.open(login_url(base, login), dark=dark)
        cdp.wait(ready())
        cdp.wait("document.querySelectorAll('.dstrip img.on').length === 4")
        got = cdp.js("""(() => { const s = (n) => document.querySelector('article.icard[data-item="insight-t3-' + n + '"] .dstrip');
          const col = (e) => getComputedStyle(e).color;
          return {words: col(s(1).querySelector('.dstrip-words')), none: col(s(6).querySelector('.dstrip-words')),
                  prev: col(s(5).querySelector('.dstrip-prev')), ok: col(s(1).querySelector('.fc')),
                  repaired: col(s(1).querySelector('.fc')), bg: getComputedStyle(s(1)).backgroundColor,
                  line: getComputedStyle(s(1)).borderTopColor, visible: s(1).getBoundingClientRect().height > 40}; })()""")
        assert got["visible"] is True
        assert _rgb_of(got["words"]) == _rgb_of(tokens["--text2"])  # the status line is --text2 in this theme
        assert _rgb_of(got["none"]) == _rgb_of(tokens["--muted"])  # "No deck" is --muted
        assert _rgb_of(got["prev"]) == _rgb_of(tokens["--muted"])
        assert _rgb_of(got["ok"]) == _rgb_of(tokens["--a2-text"])  # A's badge is "Repaired"
        cdp.close()


@needs_chrome
def test_the_strip_respects_reduced_motion(tmp_path, cdp):
    with live_server(tmp_path, seed_states, stream_polls=200, sips=fake_sips) as (base, _s, login):
        cdp.open(login_url(base, login), reduced=True)
        cdp.wait(ready())
        names = cdp.js("[...document.querySelectorAll('.dstrip, .dstrip *')].map(e => getComputedStyle(e).animationName)")
        assert names and set(names) == {"none"}, set(names)
        cdp.close()
