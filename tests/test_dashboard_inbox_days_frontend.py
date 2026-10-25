"""The "Waiting for you" list grouped by day (2026-10-21): the newest day first, a header per day ("Today · Wed 21 Oct",
"Yesterday · Tue 20 Oct", then "Mon 19 Oct") with how many drafts it holds, the newest draft first inside a day. Static
checks read the files under dashboard/static. The render checks start a real uvicorn on 127.0.0.1 against a tmp store, log
in as `dashboard open` does, and drive headless Chrome over CDP, as the other dashboard tests do (skipped when Chrome is
missing). Offline guard on; tmp data only.

The server's clock is a moving one that starts at 00:20 in Kuwait three days back: at that moment it is still the day
before in UTC, so "Today" is only right when the day is worked out in the settings timezone (Kuwait), and the labels the
page shows are checked against this test's own working-out of the same moment."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta

import pytest

from haseeb_agent.dashboard.app import STATIC_DIR
from haseeb_agent.store import Store
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    HOSTILE_IMG, _code, _contrast, _item, _rgb, _selectors, _surfaces, _tz, cdp, live_server, login_url, needs_chrome,
)
from tests.test_dashboard_deck_cards_frontend import NO_REFRESH, deck_event, write

# the inbox's 30 s refresh never fires: only a stream event can make the page look again (like a real arrival)
GROUPS = """[...document.querySelectorAll('.inbox-day')].map(g => ({
  day: g.dataset.day, head: g.querySelector('.inbox-day-h').textContent.replace(/\\s+/g, ' ').trim(),
  title: g.querySelector('.inbox-day-t').textContent, count: g.querySelector('.inbox-day-n').textContent,
  ids: [...g.querySelectorAll('article.icard')].map(c => c.dataset.item)}))"""


class kuwait_clock:  # noqa: N801 - used like a function: kuwait_clock(tz, day) is the clock
    """A clock that starts at hh:mm on `day` in `tz` and then runs; `jump(seconds)` moves it on (a night passing).
    Always in the past of the real time, so the login nonce (made on the real clock) is still valid for it."""

    def __init__(self, tz, day: date, hh: int = 0, mm: int = 20):
        self.start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).timestamp()
        self.t0, self.skipped = time.time(), 0.0

    def __call__(self) -> float:
        return self.start + (time.time() - self.t0) + self.skipped

    def jump(self, seconds: float) -> None:
        self.skipped += seconds


def the_day(tz) -> date:
    """The date the test's clock starts on: three days before the real one, in Kuwait."""
    return (datetime.now(tz) - timedelta(days=3)).date()


def label(day: date, today: date) -> str:
    """The header of a day, worked out here for the check: "Today · Wed 21 Oct", "Yesterday · Tue 20 Oct", "Mon 19 Oct";
    the year only when it is not this year's."""
    plain = f"{day.strftime('%a')} {day.day} {day.strftime('%b')}" + ("" if day.year == today.year else f" {day.year}")
    if day == today:
        return f"Today · {plain}"
    if day == today - timedelta(days=1):
        return f"Yesterday · {plain}"
    return plain


def put(store, iid: str, day: date, ts: str, kind: str = "insight", *, title: str | None = None,
        body: str | None = None, status: str = "delivered") -> None:
    from dataclasses import replace
    item = _item(iid, kind, title or {"insight": "An insight", "todo": "Next Todo", "feedback": "Feedback for Sara",
                                       "message": "Message to the channel"}[kind],
                 body or f"The text of {iid}", day, ts)
    store.upsert_item(replace(item, status=status))


def seed_three_days(today: date):
    d1, d2 = today - timedelta(days=1), today - timedelta(days=2)

    def seed(store, settings):
        for n, ts in ((1, "301.1"), (2, "301.2"), (3, "301.3")):
            put(store, f"insight-{today.isoformat()}-{n}", today, ts)
        put(store, f"todo-{today.isoformat()}", today, "301.4", kind="todo")
        put(store, f"insight-{d1.isoformat()}-1", d1, "201.1")
        put(store, f"insight-{d1.isoformat()}-2", d1, "201.2")
        put(store, "feedback-201.5", d1, "201.5", kind="feedback")
        put(store, f"insight-{d2.isoformat()}-1", d2, "101.1")
        put(store, "message-101.3", d2, "101.3", kind="message")
    return seed


# ---------------------------------------------------------------- static checks

def test_the_day_header_css_sits_under_its_own_names():
    css = (STATIC_DIR / "app.css").read_text()
    start, end = "/* ---------- the inbox, grouped by day", "/* ---------- the deck on an insight's card"
    sels = _selectors(css[css.index(start):css.index(end)])
    own = re.compile(r"^\.inbox-day")
    loose = [x for x in sels if not own.match(x)]
    assert len(sels) >= 6 and not loose, loose
    block = css[css.index(start):css.index(end)]
    assert re.search(r"\.inbox-day-h\s*\{[^}]*position:\s*sticky", block)  # sticky inside its day's group


def test_the_headings_nest_under_the_cards_h2():
    """"Waiting for you" is an h2: a day is an h3 (like the composer's and the team posts' headings), a draft's title
    an h4 inside it."""
    code = _code((STATIC_DIR / "inbox.js").read_text())
    assert 'el("h3", { class: "inbox-day-h"' in code and 'el("h4", { class: "icard-title" }' in code
    assert 'role: "group"' in code and "aria-labelledby" in code


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_day_header_text_colours_meet_aa_where_they_sit(theme):
    """The header is glass over the list: at rest over the card, stuck over a draft's sunken surface."""
    t, over, card, sunken = _surfaces(theme)
    for surface_name, under in (("the card", card), ("a draft", sunken)):
        glass = over("--glass", under)
        for name, token in (("the day", "--text2"), ("how many", "--muted")):
            assert _contrast(_rgb(t[token])[0], glass) >= 4.5, (theme, name, surface_name)


# ---------------------------------------------------------------- the grouped list

@needs_chrome
def test_the_list_is_grouped_by_day_newest_first_with_the_days_worked_out_in_kuwait(tmp_path, cdp):
    tz = _tz()
    today = the_day(tz)
    d1, d2 = today - timedelta(days=1), today - timedelta(days=2)
    with live_server(tmp_path, seed_three_days(today), stream_polls=200, clock=kuwait_clock(tz, today)
                     ) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 9")
        got = cdp.js(GROUPS)
        assert [g["day"] for g in got] == [today.isoformat(), d1.isoformat(), d2.isoformat()]  # newest day first
        assert [g["title"] for g in got] == [label(today, today), label(d1, today), label(d2, today)]
        assert got[0]["title"].startswith("Today · ") and got[1]["title"].startswith("Yesterday · ")
        assert not got[2]["title"].startswith(("Today", "Yesterday"))
        assert [g["count"] for g in got] == ["4 waiting", "3 waiting", "2 waiting"]  # under each header
        t = today.isoformat()
        assert got[0]["ids"] == [f"todo-{t}", f"insight-{t}-3", f"insight-{t}-2", f"insight-{t}-1"]  # newest first
        assert got[1]["ids"] == ["feedback-201.5", f"insight-{d1.isoformat()}-2", f"insight-{d1.isoformat()}-1"]
        assert got[2]["ids"] == ["message-101.3", f"insight-{d2.isoformat()}-1"]
        # the kinds stay mixed in that order, and each card keeps its kind label
        kinds = cdp.js("[...document.querySelectorAll('.inbox-day')].map(g => [...g.querySelectorAll('article.icard')]"
                       ".map(c => c.querySelector('.kind').textContent.trim()))")
        assert kinds == [["Next Todo", "Insight", "Insight", "Insight"], ["Feedback", "Insight", "Insight"],
                         ["Message", "Insight"]]
        # an h3 per day, named by the group, with the cards' own titles one level down
        assert cdp.js("[...document.querySelectorAll('.inbox-day')].every(g => { const h = g.querySelector('.inbox-day-h');"
                      " return h.tagName === 'H3' && h.id && g.getAttribute('role') === 'group' && "
                      "g.getAttribute('aria-labelledby') === h.id; })")
        assert cdp.js("[...document.querySelectorAll('article.icard .icard-title')].every(h => h.tagName === 'H4')")
        assert cdp.js("new Set([...document.querySelectorAll('.inbox-day-h')].map(h => h.id)).size") == 3
        # the badge still counts every draft
        assert cdp.js("document.querySelector('.count-badge').textContent") == "9"
        cdp.close()


@needs_chrome
def test_the_year_is_in_the_header_only_when_it_is_not_this_years(tmp_path, cdp):
    tz = _tz()
    new_year = date(datetime.now(tz).year - 1, 1, 1)  # the clock starts on 1 Jan, so yesterday is the year before's
    d1, d2 = new_year - timedelta(days=1), new_year - timedelta(days=2)

    def seed(store, settings):
        put(store, "insight-a", new_year, "30.1")
        put(store, "insight-b", d1, "20.1")
        put(store, "insight-c", d2, "10.1")
    with live_server(tmp_path, seed, stream_polls=200, clock=kuwait_clock(tz, new_year)) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 3")
        titles = [g["title"] for g in cdp.js(GROUPS)]
        assert titles == [label(new_year, new_year), label(d1, new_year), label(d2, new_year)]
        assert str(d1.year) in titles[1] and str(d2.year) in titles[2] and str(new_year.year) not in titles[0]
        cdp.close()


@needs_chrome
def test_a_live_insert_lands_under_the_right_header_and_an_empty_day_goes(tmp_path, cdp):
    tz = _tz()
    today = the_day(tz)
    d1, d2 = today - timedelta(days=1), today - timedelta(days=2)
    t, y, o = today.isoformat(), d1.isoformat(), d2.isoformat()

    def seed(store, settings):  # no draft of today yet
        put(store, f"insight-{y}-1", d1, "201.1")
        put(store, f"insight-{y}-2", d1, "201.2")
        put(store, f"insight-{o}-1", d2, "101.1")
        put(store, f"insight-{o}-2", d2, "101.2")
    with live_server(tmp_path, seed, stream_polls=400, clock=kuwait_clock(tz, today)) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=NO_REFRESH)  # only a stream event makes the page look again
        cdp.wait("document.querySelectorAll('article.icard').length === 4")
        assert [g["day"] for g in cdp.js(GROUPS)] == [y, o]
        cdp.js("document.querySelectorAll('article.icard, .inbox-day').forEach(n => n.__kept = 1)")
        time.sleep(1.0)  # the entrance has finished
        # a draft that is being read or edited keeps the focus whatever lands around it, and nothing that is already
        # there is taken out of the page and put back (which is what moving a node is)
        cdp.js(f"document.querySelector('article.icard[data-item=\"insight-{y}-1\"]').focus()")
        cdp.js("""window.__removed = [];
          new MutationObserver((recs) => recs.forEach((r) => r.removedNodes.forEach((n) => {
            if (n.nodeType === 1 && n.matches('article.icard, .inbox-day')) window.__removed.push(n.dataset.item || n.dataset.day);
          }))).observe(document.querySelector('.inbox-list'), {childList: true, subtree: true});""")

        def arrive(*items, **kw):
            write(settings, lambda st, s: [put(st, *i, **kw) for i in items])
            before = cdp.js("document.querySelectorAll('article.icard').length")
            deck_event(settings)
            return before

        # a new draft of yesterday, the newest of its day: first under "Yesterday", and the count follows
        n = arrive((f"insight-{y}-3", d1, "201.9"))
        cdp.wait(f"document.querySelectorAll('article.icard').length === {n + 1}", timeout=10)
        got = cdp.js(GROUPS)
        assert [g["day"] for g in got] == [y, o]
        assert got[0]["ids"] == [f"insight-{y}-3", f"insight-{y}-2", f"insight-{y}-1"] and got[0]["count"] == "3 waiting"
        assert got[1]["ids"] == [f"insight-{o}-2", f"insight-{o}-1"] and got[1]["count"] == "2 waiting"
        # an older one, delivered before the others of its day: last under that day, in the middle of the page
        n = arrive((f"insight-{o}-0", d2, "101.0"))
        cdp.wait(f"document.querySelectorAll('article.icard').length === {n + 1}", timeout=10)
        assert cdp.js(GROUPS)[1]["ids"] == [f"insight-{o}-2", f"insight-{o}-1", f"insight-{o}-0"]
        # the first draft of today: a new group, above the others
        n = arrive((f"insight-{t}-1", today, "301.1"), (f"todo-{t}", today, "301.4", "todo"))
        cdp.wait(f"document.querySelectorAll('article.icard').length === {n + 2}", timeout=10)
        got = cdp.js(GROUPS)
        assert [g["day"] for g in got] == [t, y, o]
        assert got[0]["title"] == label(today, today) and got[0]["count"] == "2 waiting"
        assert got[0]["ids"] == [f"todo-{t}", f"insight-{t}-1"] and got[1]["title"] == label(d1, today)
        # nothing that was already there was rebuilt or moved, so a draft being read or edited keeps its place
        assert cdp.js("[...document.querySelectorAll('article.icard')].filter(c => c.__kept).length") == 4
        assert cdp.js("[...document.querySelectorAll('.inbox-day')].filter(g => g.__kept).length") == 2
        assert cdp.js("window.__removed") == []
        assert cdp.js("document.activeElement.dataset.item") == f"insight-{y}-1"
        # one of yesterday's is posted from somewhere else: it fades out of the middle of its day, and the draft being
        # read, which sits below it, is not moved by that (it keeps the focus) or counted any more than the one that went
        write(settings, lambda st, s: st.mark(f"insight-{y}-2", "posted"))
        deck_event(settings)
        cdp.wait(f"!document.querySelector('article.icard[data-item=\"insight-{y}-2\"]')", timeout=10)
        got = cdp.js(GROUPS)
        assert got[1]["ids"] == [f"insight-{y}-3", f"insight-{y}-1"] and got[1]["count"] == "2 waiting"
        assert cdp.js("window.__removed") == [f"insight-{y}-2"]
        assert cdp.js("document.activeElement.dataset.item") == f"insight-{y}-1"
        # the two oldest drafts are posted from somewhere else: their day empties and its header goes
        write(settings, lambda st, s: [st.mark(f"insight-{o}-{k}", "posted") for k in ("0", "1", "2")])
        deck_event(settings)
        cdp.wait(f"document.querySelectorAll('.inbox-day').length === 2", timeout=10)
        got = cdp.js(GROUPS)
        assert [g["day"] for g in got] == [t, y] and got[1]["count"] == "2 waiting"
        assert cdp.js("document.querySelector('.count-badge').textContent") == "4"
        assert sorted(cdp.js("window.__removed")) == sorted(
            [f"insight-{y}-2", o, f"insight-{o}-0", f"insight-{o}-1", f"insight-{o}-2"])
        cdp.close()


@needs_chrome
def test_a_posted_draft_is_not_counted_as_waiting_while_it_shows_its_link(tmp_path, cdp):
    tz = _tz()
    today = the_day(tz)
    t = today.isoformat()
    link = "https://mustcompany.slack.com/archives/C0C62C128D8/p1760000000000100"

    def seed(store, settings):
        put(store, f"insight-{t}-1", today, "301.1")
        put(store, f"insight-{t}-2", today, "301.2")
    with live_server(tmp_path, seed, stream_polls=200, clock=kuwait_clock(tz, today)) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 2")
        assert cdp.js(GROUPS)[0]["count"] == "2 waiting"
        card = f"document.querySelector('article.icard[data-item=\"insight-{t}-2\"]')"
        btn = lambda label: f"[...{card}.querySelectorAll('.actions button')].find(b => b.textContent.trim() === '{label}')"  # noqa: E731
        cdp.js(f"{btn('Post')}.click()")
        cdp.wait(f"!!({btn('Confirm')})")
        cdp.js(f"{btn('Confirm')}.click()")
        cdp.wait(f"{card}.querySelector('.cstatus.s-queued')")
        from tests.test_dashboard_frontend import _commands
        (cmd,) = _commands(settings)
        write(settings, lambda st, s: (st.set_page_command(cmd.id, "done", f"✅ Posted · {link}"),
                                       st.mark(f"insight-{t}-2", "posted")))
        cdp.wait(f"{card}.querySelector('.cstatus.s-done')")
        got = cdp.js(GROUPS)  # the card is still there, with its link, for a moment
        assert got[0]["ids"] == [f"insight-{t}-2", f"insight-{t}-1"] and got[0]["count"] == "1 waiting"
        cdp.wait(f"!{card}", timeout=5)  # then it leaves, and the day keeps its other draft
        got = cdp.js(GROUPS)
        assert [g["ids"] for g in got] == [[f"insight-{t}-1"]] and got[0]["count"] == "1 waiting"
        cdp.close()


@needs_chrome
def test_a_night_passing_turns_today_into_yesterday_in_place(tmp_path, cdp):
    """The page is left open past midnight in Kuwait: at its next look, the headers say what the days are now, and a day
    that has gone out of the list's three-day window goes. The headers and cards that stay are the same elements."""
    tz = _tz()
    today = the_day(tz)
    d1, d2 = today - timedelta(days=1), today - timedelta(days=2)
    clock = kuwait_clock(tz, today, 23, 50)

    def seed(store, settings):
        put(store, f"insight-{today.isoformat()}-1", today, "301.1")
        put(store, f"insight-{d1.isoformat()}-1", d1, "201.1")
        put(store, f"insight-{d2.isoformat()}-1", d2, "101.1")
    with live_server(tmp_path, seed, stream_polls=400, clock=clock) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=NO_REFRESH)
        cdp.wait("document.querySelectorAll('article.icard').length === 3")
        assert [g["title"] for g in cdp.js(GROUPS)] == [label(today, today), label(d1, today), label(d2, today)]
        cdp.js("document.querySelectorAll('article.icard, .inbox-day').forEach(n => n.__kept = 1)")
        clock.jump(20 * 60)  # it is 00:10 on the next day in Kuwait
        tomorrow = today + timedelta(days=1)
        deck_event(settings)
        cdp.wait("document.querySelectorAll('.inbox-day').length === 2", timeout=10)
        got = cdp.js(GROUPS)
        assert [g["day"] for g in got] == [today.isoformat(), d1.isoformat()]
        assert [g["title"] for g in got] == [label(today, tomorrow), label(d1, tomorrow)]
        assert got[0]["title"].startswith("Yesterday · ") and not got[1]["title"].startswith(("Today", "Yesterday"))
        assert cdp.js("[...document.querySelectorAll('article.icard, .inbox-day')].every(n => n.__kept)")
        cdp.close()


@needs_chrome
def test_the_last_draft_leaving_shows_the_empty_state_and_a_new_one_starts_the_list_again(tmp_path, cdp):
    tz = _tz()
    today = the_day(tz)

    def seed(store, settings):
        put(store, "insight-only", today, "5.1")
    with live_server(tmp_path, seed, stream_polls=400, clock=kuwait_clock(tz, today)) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=NO_REFRESH)
        cdp.wait("document.querySelectorAll('.inbox-day').length === 1")
        write(settings, lambda st, s: st.mark("insight-only", "skipped"))
        deck_event(settings)
        assert "Nothing waiting for you" in cdp.wait("document.querySelector('.empty-t')?.textContent", timeout=10)
        assert cdp.js("document.querySelectorAll('.inbox-day, article.icard').length") == 0
        write(settings, lambda st, s: put(st, "insight-new", today, "6.1"))
        deck_event(settings)
        cdp.wait("document.querySelectorAll('.inbox-day article.icard').length === 1", timeout=10)
        assert [g["ids"] for g in cdp.js(GROUPS)] == [["insight-new"]] and not cdp.js("!!document.querySelector('.empty-t')")
        cdp.close()


# ---------------------------------------------------------------- the header: sticky, both themes, phone width

def seed_long(today: date):
    d1 = today - timedelta(days=1)

    def seed(store, settings):
        for n in range(1, 7):
            put(store, f"insight-{today.isoformat()}-{n}", today, f"30{n}.1", body="word " * 400)
        for n in range(1, 6):
            put(store, f"insight-{d1.isoformat()}-{n}", d1, f"20{n}.1", body="word " * 400)
    return seed


@needs_chrome
def test_a_day_header_sticks_to_the_top_while_its_day_scrolls_and_then_gives_way(tmp_path, cdp):
    tz = _tz()
    today = the_day(tz)
    with live_server(tmp_path, seed_long(today), stream_polls=200, clock=kuwait_clock(tz, today)) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 11")
        time.sleep(0.8)
        assert cdp.js("getComputedStyle(document.querySelector('.inbox-day-h')).position") == "sticky"
        rect = """(i) => { const h = document.querySelectorAll('.inbox-day-h')[i].getBoundingClientRect(); return Math.round(h.top); }"""
        # in the middle of the first day: its header is at the top of the window
        cdp.js("(() => { const g = document.querySelectorAll('.inbox-day')[0]; "
               "window.scrollTo(0, g.getBoundingClientRect().top + scrollY + 500); })()")
        time.sleep(0.2)
        assert cdp.js(f"({rect})(0)") == 0
        assert cdp.js(f"({rect})(1)") > 300  # the next day's header is still down the page
        # in the middle of the second day: now its header is stuck, and the first has gone up and out with its day
        cdp.js("(() => { const g = document.querySelectorAll('.inbox-day')[1]; "
               "window.scrollTo(0, g.getBoundingClientRect().top + scrollY + 500); })()")
        time.sleep(0.2)
        assert cdp.js(f"({rect})(1)") == 0 and cdp.js(f"({rect})(0)") < 0
        assert cdp.js("document.querySelectorAll('.inbox-day-h')[1].textContent.includes('Yesterday')")
        cdp.close()


@needs_chrome
def test_the_headers_fit_a_phone_and_read_in_one_line(tmp_path, cdp):
    tz = _tz()
    today = the_day(tz)
    with live_server(tmp_path, seed_three_days(today), stream_polls=200, clock=kuwait_clock(tz, today)
                     ) as (base, _s, login):
        cdp.open(login_url(base, login), width=380)
        cdp.wait("document.querySelectorAll('article.icard').length === 9")
        time.sleep(0.9)
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        assert cdp.js("[...document.querySelectorAll('.inbox-day, .inbox-day-h, .inbox-day-h *')]"
                      ".every(e => e.getBoundingClientRect().right <= 380.5)")
        # the label on the left, how many on the right, both on one line
        assert cdp.js("""[...document.querySelectorAll('.inbox-day-h')].every(h => {
          const t = h.querySelector('.inbox-day-t').getBoundingClientRect(), n = h.querySelector('.inbox-day-n').getBoundingClientRect();
          return Math.abs(t.top - n.top) < 8 && t.right < n.left && h.getBoundingClientRect().height < 44; })""")
        cdp.close()


@needs_chrome
@pytest.mark.parametrize("dark,theme", [(True, "midnight"), (False, "daylight")])
def test_the_headers_show_in_both_themes_with_the_token_colours(tmp_path, cdp, dark, theme):
    tz = _tz()
    today = the_day(tz)
    tokens = _surfaces(theme)[0]
    with live_server(tmp_path, seed_three_days(today), stream_polls=200, clock=kuwait_clock(tz, today)
                     ) as (base, _s, login):
        cdp.open(login_url(base, login), dark=dark)
        cdp.wait("document.querySelectorAll('article.icard').length === 9")
        got = cdp.js("""(() => { const h = document.querySelector('.inbox-day-h');
          const col = (e) => getComputedStyle(e).color;
          return {head: col(h), day: col(h.querySelector('.inbox-day-t')), n: col(h.querySelector('.inbox-day-n')),
                  sticky: getComputedStyle(h).position, bg: getComputedStyle(h).backgroundColor}; })()""")

        def rgb(v):
            m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", v)
            return tuple(int(x) for x in m.groups())
        assert rgb(got["day"]) == _rgb(tokens["--text2"])[0] and rgb(got["n"]) == _rgb(tokens["--muted"])[0]
        assert got["sticky"] == "sticky" and got["bg"] != "rgba(0, 0, 0, 0)"  # glass: the cards beneath do not show through
        cdp.close()


@needs_chrome
def test_the_headers_respect_reduced_motion(tmp_path, cdp):
    tz = _tz()
    today = the_day(tz)
    with live_server(tmp_path, seed_three_days(today), stream_polls=200, clock=kuwait_clock(tz, today)
                     ) as (base, _s, login):
        cdp.open(login_url(base, login), reduced=True)
        cdp.wait("document.querySelectorAll('article.icard').length === 9")
        names = cdp.js("[...document.querySelectorAll('.inbox-day, .inbox-day *')].map(e => getComputedStyle(e).animationName)")
        assert names and set(names) == {"none"}, set(names)
        cdp.close()
