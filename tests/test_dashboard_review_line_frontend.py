"""The site review on the dashboard (2026-10-21): the line on a feedback card, and its step in the Live pipeline. Static checks read the files under
dashboard/static. The render checks start a real uvicorn on 127.0.0.1 against a tmp store (nothing ever runs), log in as
`dashboard open` does, and drive headless Chrome over CDP, as the other frontend tests do. They are skipped when Chrome is
missing. Offline guard on; tmp data only.

The line is what the DM card says about the site review: "Reviewed: <host> (desktop + phone)" or "Couldn't review <host>:
<reason>". The job keeps it in the cursor `feedback_review:<item id>`; the inbox carries it as `review`; the card shows it
under its title, as text, in the warning colour when something could not be reviewed. The review is also a step of a
feedback run ("Site review"), shown in the Live pipeline while it runs and once it has run, not in a run that skipped it."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from haseeb_agent.dashboard.app import STATIC_DIR
from tests.test_dashboard_deck_cards_frontend import deck_event, write
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    HOSTILE_BIDI, HOSTILE_IMG, PWN, _code, _contrast, _done, _item, _rgb, _selectors, _surfaces, cdp, live_server,
    login_url, needs_chrome, render_dom, section,
)
from tests.test_insights import good_meta

OK = "Reviewed: reach-poc.vercel.app (desktop + phone)"
BAD = "Couldn't review demo.example: daily limit"
BOTH = ("Reviewed: reach-poc.vercel.app (desktop + phone) · "
        "Couldn't review reach-io-poc-21-oct.html: the file is too large")
HOSTILE = f"Reviewed: {PWN} {HOSTILE_IMG} {HOSTILE_BIDI} (desktop + phone)"
LONG = "Couldn't review " + "w" * 150 + ".example: ran out of time " + "x" * 40  # a host with no place to break

LINES = {1: OK, 2: BAD, 3: BOTH, 5: HOSTILE, 6: LONG}  # card 4 was not reviewed


def fid(n: int) -> str:
    return f"feedback-t4-{n}"


def card(item: str) -> str:
    return f"document.querySelector('article.icard[data-item=\"{item}\"]')"


def line(n: int) -> str:
    return f"{card(fid(n))}.querySelector('.review-line')"


def words(n: int) -> str:
    return f"{line(n)}.querySelector('span:not(.ico)').textContent"


def seed(store, settings) -> None:
    today = datetime.now(settings.tz).date()
    for n in range(1, 7):
        store.upsert_item(_item(fid(n), "feedback", f"Feedback: Person {n}", f"✅ A strength {n}.\n❓ A question?",
                                today, f"{30 + n}.1"))
        if n in LINES:
            store.set_cursor(f"feedback_review:{fid(n)}", LINES[n])
    # the other kinds never show one, whatever a cursor holds
    store.upsert_item(_item("insight-t4", "insight", "An insight", json.dumps(good_meta(batch="insight-t4")), today, "40.1"))
    store.upsert_item(_item("todo-t4", "todo", "Next Todo", "Next todo: ship it", today, "41.1"))
    store.set_cursor("feedback_review:insight-t4", OK)
    store.set_cursor("feedback_review:todo-t4", OK)


READY = "document.querySelectorAll('article.icard').length === 8"


# ---------------------------------------------------------------- static checks

def test_the_review_line_is_made_for_feedback_cards_only_and_filled_as_text():
    code = _code((STATIC_DIR / "inbox.js").read_text())
    assert 'it.kind === "feedback" ? el("p", { class: "review-line"' in code
    assert code.count("paintReview(") == 2  # defined once, used once (in sync)
    fn = code[code.index("function paintReview("):][:700]
    assert "span.textContent = line" in fn and "HTML" not in fn
    sync = code[code.index("function sync("):][:900]
    assert "if (st.review) paintReview(st, typeof it.review === \"string\" ? it.review : \"\")" in sync


def test_the_review_line_css_sits_under_its_own_names():
    css = (STATIC_DIR / "app.css").read_text()
    start, end = "/* ---------- the site review's line on a feedback card", "/* ---------- the inbox, grouped by day"
    block = css[css.index(start):css.index(end)]
    sels = _selectors(block)
    loose = [x for x in sels if not x.startswith(".review-line")]
    assert len(sels) == 4 and not loose, loose
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block)  # the theme's tokens only
    assert css.index(start) > css.index("/* ---------- inbox */")


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_the_review_line_colours_meet_aa_where_they_sit(theme):
    """The line sits on the card. Its text is --muted, or --warn when something could not be reviewed (4.5:1); its icon
    is --a2-text, or --warn (3:1, as a graphic)."""
    t, _over, card_bg, _sunken = _surfaces(theme)
    fg = lambda name: _rgb(t[name])[0]  # noqa: E731
    assert _contrast(fg("--muted"), card_bg) >= 4.5
    assert _contrast(fg("--warn"), card_bg) >= 4.5
    assert _contrast(fg("--a2-text"), card_bg) >= 3.0


# ---------------------------------------------------------------- on the page

@needs_chrome
def test_a_feedback_card_shows_what_the_site_review_looked_at_under_its_title(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(READY)
        got = cdp.js("""Object.fromEntries([...document.querySelectorAll('article.icard')].map(c => {
            const r = c.querySelector('.review-line');
            return [c.dataset.item, r && { shown: getComputedStyle(r).display !== 'none',
              text: r.querySelector('span:not(.ico)').textContent, bad: r.classList.contains('bad'),
              icon: !!r.querySelector('.ico.i-eye'), above: r.previousElementSibling.className,
              below: r.nextElementSibling.className }]; }))""")
        under = {"above": "icard-title", "below": "draft", "icon": True}
        assert got[fid(1)] == {"shown": True, "text": OK, "bad": False, **under}
        assert got[fid(2)] == {"shown": True, "text": BAD, "bad": True, **under}
        assert got[fid(3)] == {"shown": True, "text": BOTH, "bad": True, **under}  # one target failed: it reads as a warning
        assert got[fid(5)]["text"] == HOSTILE and got[fid(6)]["text"] == LONG
        assert got[fid(4)] == {"shown": False, "text": "", "bad": False, **under}  # not reviewed: nothing to say
        assert got["insight-t4"] is None and got["todo-t4"] is None  # only a feedback draft has one
        cdp.close()


@needs_chrome
def test_a_hostile_line_is_text_and_nothing_else(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(READY)
        assert cdp.js(words(5)) == HOSTILE
        assert cdp.js(f"{line(5)}.querySelectorAll('img, a, script').length") == 0
        assert cdp.js(f"{line(5)}.children.length") == 2  # the icon and the text: nothing was made from the text
        assert cdp.js("window.__pwned") is None
        cdp.close()


@needs_chrome
def test_the_line_follows_its_cursor_in_place_without_rebuilding_the_cards(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(READY)
        cdp.js("document.querySelectorAll('article.icard').forEach(n => { n.__kept = 1; "
               "n.querySelectorAll('.review-line').forEach(l => { l.__kept = 1; }); })")

        def change(st, _s):
            st.set_cursor(f"feedback_review:{fid(4)}", OK)          # a line appears
            st.set_cursor(f"feedback_review:{fid(1)}", BAD)         # a line changes
            st.set_cursor(f"feedback_review:{fid(2)}", "")          # a line goes
        write(settings, change)
        deck_event(settings)  # a step on the stream makes the inbox look again
        cdp.wait(f"{words(4)} === {json.dumps(OK)}")
        assert cdp.js(f"getComputedStyle({line(4)}).display") != "none"
        assert cdp.js(words(1)) == BAD and cdp.js(f"{line(1)}.classList.contains('bad')") is True
        assert cdp.js(f"getComputedStyle({line(2)}).display") == "none"
        assert cdp.js(f"{line(4)}.classList.contains('bad')") is False
        assert cdp.js("[...document.querySelectorAll('article.icard')].every(n => n.__kept === 1)")
        assert cdp.js("[...document.querySelectorAll('.review-line')].every(l => l.__kept === 1)")
        cdp.close()


@needs_chrome
@pytest.mark.parametrize("dark", [True, False])
def test_the_line_has_the_themes_colours_and_fits_a_phone(tmp_path, cdp, dark):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login), dark=dark, width=380)
        cdp.wait(READY)
        cdp.wait(f"{words(6)} !== ''")
        colours = cdp.js(f"""(() => {{
            const probe = (el, token) => {{ const p = document.createElement('span'); p.style.color = 'var(' + token + ')';
              el.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; }};
            const ok = {line(1)}, bad = {line(2)};
            return {{ ok: [getComputedStyle(ok).color, probe(ok, '--muted')],
                      bad: [getComputedStyle(bad).color, probe(bad, '--warn')],
                      okIcon: [getComputedStyle(ok.querySelector('.ico')).color, probe(ok, '--a2-text')],
                      badIcon: [getComputedStyle(bad.querySelector('.ico')).color, probe(bad, '--warn')] }};
        }})()""")
        for name, (got, want) in colours.items():
            assert got == want, (name, colours)
        assert colours["ok"][0] != colours["bad"][0]
        # no sideways scroll, and a long unbroken host wraps inside its card instead of pushing the page out
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        fits = cdp.js(f"""(() => {{ const l = {line(6)}, t = l.querySelector('span:not(.ico)').getBoundingClientRect(),
            c = {card(fid(6))}.getBoundingClientRect();
            return {{ inside: t.left >= c.left && t.right <= c.right, nothing_cut: l.scrollWidth <= l.clientWidth,
                     lines: Math.round(t.height / 18) }}; }})()""")
        assert fits == {"inside": True, "nothing_cut": True, "lines": fits["lines"]} and fits["lines"] >= 3, fits
        cdp.close()


# ---------------------------------------------------------------- the step in the Live pipeline

def _run(store, settings, *, reviewed: bool | None, now_step: str) -> None:
    """A feedback run in progress: it has read the channel, maybe looked at a site (reviewed: True = it did, None = it is
    looking now, False = the post had nothing to look at), and is at `now_step`."""
    now = datetime.now(settings.tz)
    store.set_cursor("tick_seen", (now - timedelta(seconds=20)).isoformat())
    _done(store, "feedback", "read_channel", now - timedelta(seconds=90), 5)
    if reviewed:
        _done(store, "feedback", "site_review", now - timedelta(seconds=60), 31)
    if now_step == "site_review":
        store.add_event("feedback", "site_review", model=None, started=now - timedelta(seconds=20), ended=None, ok=None,
                        kind=None)
    else:
        store.add_event("feedback", now_step, model="deepseek-v4-pro", started=now - timedelta(seconds=20), ended=None,
                        ok=None, kind=None)


def _pipeline(tmp_path, seed) -> str:
    with live_server(tmp_path, seed) as (base, _settings, login):
        return section(render_dom(tmp_path, base, login), "Right now")


@needs_chrome
def test_a_run_that_is_looking_at_a_site_shows_the_step_in_its_place(tmp_path):
    now = _pipeline(tmp_path, lambda st, se: _run(st, se, reviewed=None, now_step="site_review"))
    assert re.search(r'class="step done".*?Read channel', now, re.S)
    assert re.search(r'class="step live"[^>]*aria-current="step".*?Site review', now, re.S)
    assert re.search(r'class="step live"[^>]*>\s*<div class="node"><span class="ico i-eye"', now, re.S)  # its own icon
    assert re.search(r'class="step todo".*?Write draft', now, re.S) and re.search(r'class="step todo".*?Your DM', now, re.S)
    assert "step 2 of 4" in now
    assert re.findall(r'class="step-l">([^<]*)<', now) == ["Read channel", "Site review", "Write draft", "Your DM"]


@needs_chrome
def test_a_run_that_has_looked_at_a_site_keeps_the_step_behind_it(tmp_path):
    now = _pipeline(tmp_path, lambda st, se: _run(st, se, reviewed=True, now_step="write_draft"))
    assert re.search(r'class="step done".*?Site review', now, re.S)
    assert re.search(r'class="step live"[^>]*aria-current="step".*?Write draft', now, re.S)
    assert re.findall(r'class="step-l">([^<]*)<', now) == ["Read channel", "Site review", "Write draft", "Your DM"]
    assert "step 3 of 4" in now


@needs_chrome
def test_a_run_with_nothing_to_look_at_does_not_show_the_step(tmp_path):
    now = _pipeline(tmp_path, lambda st, se: _run(st, se, reviewed=False, now_step="write_draft"))
    assert re.findall(r'class="step-l">([^<]*)<', now) == ["Read channel", "Write draft", "Your DM"]
    assert "Site review" not in now and "step 2 of 3" in now
    assert re.search(r'class="step live"[^>]*aria-current="step".*?Write draft', now, re.S)
