"""The Usage tab (2026-10-21): the frontend. Static checks read the files under dashboard/static. The render checks start a
real uvicorn on 127.0.0.1 against a tmp store of INVENTED usage rows, log in as `dashboard open` does, and drive headless
Chrome over CDP, as the other dashboard tests do (skipped when Chrome is missing). Offline guard on; tmp data only.

The server's clock is a moving one that starts at noon in Kuwait three days back (always in the real past, so the login
nonce made on the real clock is still valid for it): every seeded row is placed relative to that day."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent.dashboard.app import STATIC_DIR
from haseeb_agent.models import UsageRow
from haseeb_agent.store import Store
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    FAST_REFRESH, HOSTILE_IMG, RLO, _code, _contrast, _rgb, _selectors, _surfaces, cdp, live_server, login_url,
    needs_chrome, open_page,
)

KW = ZoneInfo("Asia/Kuwait")
PAGE = "document.querySelector('#view > .usage')"
MODELS = "section[aria-label=\"By model\"]"
JOBS = "section[aria-label=\"By job\"]"


class noon_three_days_ago:  # noqa: N801 - used like a function: it is the server's clock
    def __init__(self):
        self.day: date = (datetime.now(KW) - timedelta(days=3)).date()
        self.start = datetime(self.day.year, self.day.month, self.day.day, 12, 0, tzinfo=KW).timestamp()
        self.t0 = time.time()

    def __call__(self) -> float:
        return self.start + (time.time() - self.t0)


def at(day: date, days_ago: int, hour: int) -> datetime:
    d = day - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=KW)


def row(day, ago, hour, provider, model, job, step, *, i=None, o=None, cr=None, cw=None, cost=None, basis="unknown",
        ms=None, ok=True, source="live", ref=None) -> UsageRow:
    return UsageRow(at=at(day, ago, hour), provider=provider, model=model, job=job, step=step, input_tokens=i,
                    output_tokens=o, cache_read_tokens=cr, cache_write_tokens=cw, cost_usd=cost, cost_basis=basis,
                    latency_ms=ms, ok=ok, source=source, ref=ref)


def seed_usage(day: date, store: Store) -> None:
    """Today: nine runs over five models. Three days ago: three more. Twenty days ago: five. Backfilled ones from before."""
    for k, ms in enumerate((2000, 4000, 6000)):
        store.add_usage(row(day, 0, 8 + k, "claude", "claude-haiku-5-5", "slack", "slack_read_channel",
                            i=100, o=40, cr=900, cw=50, cost=0.002, basis="list", ms=ms))
    store.add_usage(row(day, 0, 11, "claude", "claude-haiku-5-5", "slack", "slack_read_channel", ok=False))  # no result
    store.add_usage(row(day, 0, 9, "claude", "claude-sonnet-5-5", "insights", "research", i=10, o=500, cr=10000,
                        cw=2000, cost=0.3, basis="list", ms=30000))
    store.add_usage(row(day, 0, 9, "litellm", "deepseek-v4-pro", "feedback", "feedback_writer", i=1000, o=200,
                        cost=0.004, basis="proxy", ms=1500))
    store.add_usage(row(day, 0, 10, "litellm", "deepseek-v4-pro", "feedback", "feedback_writer", i=1000, o=200,
                        basis="unknown", ms=1500))  # the proxy sent no price
    store.add_usage(row(day, 0, 10, "codex", "gpt-6.1-sol", "decks", "designer", i=8338, o=197, cr=14208,
                        basis="subscription", ms=60000))
    store.add_usage(row(day, 0, 10, "litellm", "qwen3-embedding", "insights", "embed", i=50, basis="unknown", ms=300))
    store.add_usage(row(day, 3, 9, "claude", "claude-sonnet-5-5", "insights", "research", i=5000, o=0, cost=0.5,
                        basis="list", ms=20000, source="backfill", ref="a.jsonl"))
    for k in range(2):
        store.add_usage(row(day, 3, 10 + k, "claude", "claude-haiku-5-5", "slack", "slack_read_channel", i=10, o=1,
                            cost=0.002, basis="list", ms=3000, source="backfill", ref=f"b{k}.jsonl"))
    for k in range(5):
        store.add_usage(row(day, 20, 9 + k, "claude", "claude-haiku-5-5", "slack", "slack_read_channel", i=10, o=1,
                            cost=0.01, basis="list", source="backfill", ref=f"c{k}.jsonl"))


def insert_unchecked(store: Store, r: UsageRow) -> None:
    """Straight into the table, round the checks `Store.add_usage` makes (names that are identifiers, and no more): what the
    page does if the database ever holds anything else, a file edited by hand, say."""
    values = [r.at.isoformat(), r.provider, r.model, r.job, r.step, r.input_tokens, r.output_tokens, r.cache_read_tokens,
              r.cache_write_tokens, r.cost_usd, r.cost_basis, r.latency_ms, int(r.ok), r.source, r.ref]
    with store._db:
        store._db.execute(Store._USAGE_INSERT, values)


def server(tmp_path, clock, seed=seed_usage, **kw):
    def seeder(store, settings):
        seed(clock.day, store)
    return live_server(tmp_path, seeder, stream_polls=200, clock=clock, **kw)


def open_usage(cdp, base, login, **kw) -> None:
    open_page(cdp, base, login, "usage", **kw)


def texts(selector: str) -> str:
    return f"[...document.querySelectorAll('{selector}')].map(e => e.textContent.replace(/\\s+/g, ' ').trim())"


def providers(card: str) -> str:
    """Each provider line of a totals card: [badge, value, what the figure is, [the tokens line, the runs line]]."""
    return f"""[...document.querySelectorAll('{card} .u-p')].map(p => [p.querySelector('.u-prov').textContent,
      p.querySelector('.u-pv').textContent.replace(/\\s+/g, ' '), p.querySelector('.u-pb').textContent,
      [...p.querySelectorAll('.u-ps')].map(e => e.textContent.replace(/\\s+/g, ' ').trim())])"""


def days_text(first: date, last: date) -> str:
    """"17–21 Oct" or "28 Sep–3 Oct", the way the page writes the days of the Claude history."""
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{last.day} {last.strftime('%b')}"
    return f"{first.day} {first.strftime('%b')}–{last.day} {last.strftime('%b')}"


def table(section: str) -> str:
    """Each body row of a table as {cells: [text of each cell...]}: the cost cell as 'amount|basis'."""
    return f"""[...document.querySelectorAll('{section} tbody tr')].map(tr => [...tr.children].map(td => {{
      const small = td.querySelector('small');
      const t = td.cloneNode(true); t.querySelectorAll('small').forEach(s => s.remove());
      return t.textContent.replace(/\\s+/g, ' ').trim() + (small ? '|' + small.textContent : ''); }}))"""


# ---------------------------------------------------------------- static checks

def test_the_page_is_wired_into_the_shell():
    assert (STATIC_DIR / "usage.js").is_file()
    html = (STATIC_DIR / "index.html").read_text()
    nav = re.findall(r'<a href="#/(\w+)" data-route="\1">', html)
    assert nav[-2:] == ["score", "usage"]  # the Usage tab comes after Score
    app = _code((STATIC_DIR / "app.js").read_text())
    assert 'import { mountUsage } from "./usage.js"' in app and 'usage: { title: "Usage", mount: mountUsage }' in app


def test_the_page_reaches_the_api_only_through_the_client_and_asks_for_one_thing():
    code = _code((STATIC_DIR / "usage.js").read_text())
    assert not re.search(r"\bfetch\(", code) and "XMLHttpRequest" not in code and "localStorage" not in code
    assert re.findall(r'api\(`?"?(/api/[^`"?]*)', code) == ["/api/usage"]
    assert 'from "./client.js"' in code and 'from "./dom.js"' in code
    assert "setInterval(load" in code and "REFRESH_MS = 60000" in code  # the page keeps itself up to date


def test_the_usage_css_sits_under_its_own_name():
    """Every rule of the page's block is under `.usage` (or the Daylight colours of it), so none of it leaks into another
    page, and the block's only other rule is its own icon."""
    css = (STATIC_DIR / "app.css").read_text()
    start, end = "/* ---------- usage (2026-10-21)", "/* ---------- reduced motion"
    block = css[css.index(start):css.index(end)]
    sels = _selectors(block)
    own = re.compile(r'^(?:\.usage(?![\w-])|:root\[data-theme="daylight"\] \.usage(?![\w-])|\.i-coin$)')
    loose = [x for x in sels if not own.match(x)]
    assert len(sels) > 60 and not loose, loose
    before = _selectors(css[:css.index(start)])
    assert not [x for x in before if re.search(r"\.(usage|u-[a-z]+|ucard)(?![\w-])", x)]  # nothing of it above


def test_the_page_uses_no_class_name_another_page_styles_on_its_own():
    """The first draft called a label "now" and picked up the Live page's `.now { min-height: 252px }`. Any lower-case word
    the page's script holds that is also the whole selector of a rule above the page's own block must be a class it means
    to share."""
    code = re.sub(r'\b(?:el|svg)\("[a-z0-9]+"', "", _code((STATIC_DIR / "usage.js").read_text()))  # a tag name is no class
    css = (STATIC_DIR / "app.css").read_text()
    start = css.index("/* ---------- usage (2026-10-21)")
    shared = {"card", "card-h", "card-meta", "rise", "num", "ico", "skeleton", "load-error", "pg-head", "pg-title",
              "pg-orb", "empty-t", "empty-s", "sr-only"}
    words = set()
    for lit in re.findall(r'"([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*)"', code):
        words |= set(lit.split())
    alone = {s[1:] for s in _selectors(css[:start]) if re.fullmatch(r"\.[a-z0-9-]+", s)}  # a rule for one class, anywhere
    assert not sorted((words & alone) - shared), sorted((words & alone) - shared)


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_usage_text_colours_meet_aa_where_they_sit(theme):
    t, over, card, sunken = _surfaces(theme)
    panel2 = over("--panel2", card)
    for name in ("--text", "--text2", "--muted", "--violet-text", "--a2-text", "--pink-text", "--warn", "--bad"):
        fg = _rgb(t[name])[0]
        for where, bg in (("card", card), ("panel2 (a row under the pointer, the pressed button)", panel2)):
            assert _contrast(fg, bg) >= 4.5, (theme, name, where)
    # the provider badges: their text on their own tint over the card
    pink = _rgb(t["--pink"])[0]
    pink_tint = tuple(round(0.12 * a + 0.88 * b) for a, b in zip(pink, card))
    for name, token, bg in (("Claude", "--violet-text", over("--a1-soft", card)),
                            ("LiteLLM", "--a2-text", over("--a2-soft", card)),
                            ("Codex", "--pink-text", pink_tint),
                            ("other", "--text2", panel2)):
        assert _contrast(_rgb(t[token])[0], bg) >= 4.5, (theme, name)
    # the selected total's card keeps its text readable over the soft ring
    assert _contrast(_rgb(t["--violet-text"])[0], over("--a1-soft", card)) >= 4.5


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_the_chart_colours_stand_out_from_the_card_in_both_themes(theme):
    """A bar is not text, but it has to be seen: each model colour reaches 3:1 (WCAG 1.4.11) against the card."""
    css = (STATIC_DIR / "app.css").read_text()
    t, over, card, sunken = _surfaces(theme)
    pattern = r"\.usage \{[^}]*?(--u0:.*?)display" if theme == "midnight" else r':root\[data-theme="daylight"\] \.usage \{(.*?)\}'
    block = re.search(pattern, css, re.S).group(1)
    colours = dict(re.findall(r"(--u\d):\s*(#[0-9a-fA-F]{6})", block))
    assert sorted(colours) == [f"--u{i}" for i in range(7)]
    for name, value in colours.items():
        assert _contrast(_rgb(value)[0], card) >= 3.0, (theme, name)
    assert len({v.lower() for v in colours.values()}) == 7  # seven different colours


# ---------------------------------------------------------------- the page

@needs_chrome
def test_the_usage_tab_follows_score_and_opens_the_page(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("!!document.querySelector('.page-live')")
        assert cdp.js(texts(".nav a"))[-2:] == ["Score", "Usage"]
        cdp.js("document.querySelector('.nav a[data-route=usage]').click()")
        cdp.wait("document.querySelector('#view > .usage')?.dataset.ready === '1'")
        assert cdp.js("location.hash") == "#/usage" and cdp.js("document.title") == "Usage · Haseeb's Agent"
        assert cdp.js("document.querySelector('.nav a[data-route=usage]').getAttribute('aria-current')") == "page"
        assert cdp.js("document.querySelector('#view h1').textContent") == "Usage"
        cdp.close()


@needs_chrome
def test_the_three_cards_show_each_provider_on_its_own_basis_and_never_one_total(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        cards = cdp.js("[...document.querySelectorAll('.ucard')].map(c => ({label: c.getAttribute('aria-label'), "
                       "on: c.classList.contains('on')}))")
        assert [c["label"] for c in cards] == ["Today", "7 days", "30 days"]
        assert [c["on"] for c in cards] == [False, True, False]  # 7 days: the range the tables show
        # no single figure for the whole: every figure is a provider's own
        assert cdp.js("document.querySelectorAll('.ucard .u-v').length") == 0
        assert cdp.js("[...document.querySelectorAll('.ucard')].map(c => c.querySelectorAll('.u-pv').length)") == [3, 3, 3]
        today = cdp.js(providers(".ucard[aria-label=Today]"))
        assert today == [
            ["Claude", "≥ $0.306", "list price, paid from your subscription",
             ["15.2k in (12.7k cached, 2.15k cache write) · 620 out", "5 runs · 1 failed · 1 unpriced"]],
            ["LiteLLM", "≥ $0.0040", "company proxy, real cost", ["2.05k in · 400 out", "3 runs · 2 unpriced"]],
            ["Codex", "—", "ChatGPT Pro, tokens only", ["22.5k in (14.2k cached) · 197 out", "1 run"]],  # no price: tokens only
        ]
        # 7 days adds three days ago (Claude: 0.5 + 2 x 0.002 more); 30 days adds the five runs of 20 days ago (0.05)
        week = cdp.js(providers(".ucard[aria-label=\"7 days\"]"))
        assert week[0][1:] == ["≥ $0.810", "list price, paid from your subscription",
                               ["20.2k in (12.7k cached, 2.15k cache write) · 622 out", "8 runs · 1 failed · 1 unpriced"]]
        assert week[1:] == today[1:]  # nothing of the proxy's or Codex's in those days
        month = cdp.js(providers(".ucard[aria-label=\"30 days\"]"))
        assert month[0][1] == "≥ $0.860" and month[0][3][1] == "13 runs · 1 failed · 1 unpriced"
        cdp.close()


@needs_chrome
def test_the_model_table_lists_the_dearest_first_with_what_each_row_cost_and_used(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        cdp.js(f"document.querySelector('.u-seg[aria-label=Range] button:first-child').click()")  # Today
        cdp.wait(f"document.querySelector('{MODELS} caption').textContent.endsWith('today')")
        assert cdp.js(f"[...document.querySelectorAll('{MODELS} thead th')].map(th => th.textContent.replace(/[▲▼]/, ''))") == [
            "Model", "Runs", "Input", "Output", "Cached", "Cache write", "Cost", "Median time", "Failed"]
        rows = cdp.js(table(MODELS))
        assert rows == [
            ["Claudeclaude-sonnet-5-5", "1", "12k", "500", "10k", "2k", "$0.300|list price", "30.0 s", "0"],
            ["Claudeclaude-haiku-5-5", "4", "3.15k", "120", "2.7k", "150", "≥ $0.0060|list price", "4.0 s", "1"],
            ["LiteLLMdeepseek-v4-pro", "2", "2k", "400", "0", "0", "≥ $0.0040|company proxy", "1.5 s", "0"],
            ["Codexgpt-6.1-sol", "1", "22.5k", "197", "14.2k", "0", "—", "1 m 00 s", "0"],
            ["LiteLLMqwen3-embedding", "1", "50", "0", "0", "0", "—", "300 ms", "0"],
        ]
        # the badges are the provider's name, the failures are marked, a missing cost is a dash with a reason
        assert cdp.js(texts(f"{MODELS} .u-prov")) == ["Claude", "Claude", "LiteLLM", "Codex", "LiteLLM"]
        assert cdp.js(f"document.querySelector('{MODELS} tbody tr:nth-child(2) td:last-child').classList.contains('bad')")
        assert cdp.js(f"document.querySelector('{MODELS} tbody tr:nth-child(4) .cost').title") == "Paid from a plan: no price per run"
        assert cdp.js(f"document.querySelector('{MODELS} tbody tr:nth-child(5) .cost').title") == "No cost was reported"
        assert cdp.js(f"document.querySelector('{MODELS} tbody tr:nth-child(2) .cost').title") == "1 of 4 runs had no price"
        cdp.close()


@needs_chrome
def test_the_job_table_adds_each_job_up(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        cdp.js("document.querySelector('.u-seg[aria-label=Range] button:first-child').click()")
        cdp.wait(f"document.querySelector('{JOBS} caption').textContent.endsWith('today')")
        assert cdp.js(f"[...document.querySelectorAll('{JOBS} thead th')].map(th => th.textContent.replace(/[▲▼]/, ''))") == [
            "Job", "Runs", "Input", "Output", "List price", "Proxy cost", "Failed"]
        # a job's list price and its proxy cost are two columns, never one figure; the real money is listed first
        assert cdp.js(table(JOBS)) == [
            ["Feedback", "2", "2k", "400", "—", "≥ $0.0040", "0"],
            ["Decks", "1", "22.5k", "197", "—", "—", "0"],
            ["Insights", "2", "12.1k", "500", "≥ $0.300", "—", "0"],
            ["Slack (DMs and clicks)", "4", "3.15k", "120", "≥ $0.0060", "—", "1"],
        ]
        cdp.close()


@needs_chrome
def test_a_column_header_sorts_and_keeps_the_focus(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        cdp.js("document.querySelector('.u-seg[aria-label=Range] button:first-child').click()")
        cdp.wait(f"document.querySelector('{MODELS} caption').textContent.endsWith('today')")
        order = f"[...document.querySelectorAll('{MODELS} tbody tr')].map(tr => tr.dataset.model)"
        sort = lambda col: f"document.querySelector('{MODELS} .th-sort[data-fk=\"model:{col}\"]')"  # noqa: E731
        assert cdp.js(f"{sort('cost')}.closest('th').getAttribute('aria-sort')") == "descending"  # dearest first
        cdp.js(f"{sort('runs')}.focus(); {sort('runs')}.click()")
        assert cdp.js(order) == ["claude-haiku-5-5", "deepseek-v4-pro", "gpt-6.1-sol", "claude-sonnet-5-5",
                                 "qwen3-embedding"]  # most calls first; ties by tokens, most first
        assert cdp.js(f"{sort('runs')}.closest('th').getAttribute('aria-sort')") == "descending"
        assert cdp.js(f"{sort('cost')}.closest('th').getAttribute('aria-sort')") == "none"
        assert cdp.js("document.activeElement.dataset.fk") == "model:runs"  # the repaint gave the focus back
        cdp.js(f"{sort('runs')}.click()")
        assert cdp.js(order) == ["gpt-6.1-sol", "claude-sonnet-5-5", "qwen3-embedding", "deepseek-v4-pro", "claude-haiku-5-5"]
        assert cdp.js(f"{sort('runs')}.closest('th').getAttribute('aria-sort')") == "ascending"
        cdp.js(f"{sort('model')}.click()")  # a name sorts A to Z first
        assert cdp.js(order) == ["claude-haiku-5-5", "claude-sonnet-5-5", "deepseek-v4-pro", "gpt-6.1-sol", "qwen3-embedding"]
        cdp.js(f"{sort('cost')}.click(); {sort('cost')}.click()")  # ascending: a row with no cost is still last
        assert cdp.js(order) == ["deepseek-v4-pro", "claude-haiku-5-5", "claude-sonnet-5-5", "gpt-6.1-sol", "qwen3-embedding"]
        cdp.close()


@needs_chrome
def test_the_range_buttons_ask_the_server_and_move_the_marker(tmp_path, cdp):
    clock = noon_three_days_ago()
    record = """(() => { const f = window.fetch; window.__usage = [];
      window.fetch = function (u) { if (String(u).startsWith('/api/usage')) window.__usage.push(String(u)); return f.apply(this, arguments); };
    })();"""
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login, init_js=record)
        assert cdp.js("window.__usage") == ["/api/usage?range=7d"]
        pressed = "[...document.querySelectorAll('.u-seg[aria-label=Range] button')].map(b => b.getAttribute('aria-pressed'))"
        assert cdp.js(pressed) == ["false", "true", "false"]
        cdp.js("document.querySelectorAll('.u-seg[aria-label=Range] button')[2].click()")
        cdp.wait(f"document.querySelectorAll('{MODELS} tbody tr').length === 5 && "
                 f"document.querySelector('{MODELS} caption').textContent.endsWith('the last 30 days')")
        assert cdp.js("window.__usage")[-1] == "/api/usage?range=30d"
        assert cdp.js(pressed) == ["false", "false", "true"]
        assert cdp.js("document.querySelector('.ucard.on').getAttribute('aria-label')") == "30 days"
        calls = cdp.js(f"document.querySelector('{MODELS} tbody tr[data-model=\"claude-haiku-5-5\"] td:nth-child(2)').textContent")
        assert calls == "11"  # 4 today, 2 three days ago and 5 twenty days ago
        cdp.js("document.querySelectorAll('.u-seg[aria-label=Range] button')[0].click()")
        cdp.wait(f"document.querySelectorAll('{MODELS} tbody tr').length === 5 && "
                 f"document.querySelector('{MODELS} caption').textContent.endsWith('today')")
        assert cdp.js("window.__usage")[-1] == "/api/usage?range=today"
        cdp.close()


SEGMENTS = "[...document.querySelectorAll('.u-day')].map(g => [...g.querySelectorAll('.u-seg-r')].map(r => Number(r.getAttribute('height'))))"
METRIC = "document.querySelector('.u-seg[aria-label=\"Chart shows\"] button[data-fk=\"metric:%s\"]')"


@needs_chrome
def test_the_chart_stacks_the_last_14_days_by_model(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        days = cdp.js("[...document.querySelectorAll('.u-day')].map(g => g.dataset.day)")
        assert len(days) == 14 and days[-1] == clock.day.isoformat() and days == sorted(days)
        # one measure at a time, the list price first (the Claude cost is the one most runs have): today has two models with
        # one, three days ago two, no other day any
        assert cdp.js(texts(".u-seg[aria-label=\"Chart shows\"] button")) == ["List price", "Proxy cost", "Tokens"]
        per_day = cdp.js(SEGMENTS)
        assert [len(d) for d in per_day] == [0] * 10 + [2, 0, 0, 2]
        label = cdp.js("document.querySelector('.u-day:last-child').getAttribute('aria-label')")
        name = f"{clock.day.strftime('%a')} {clock.day.day} {clock.day.strftime('%b')}"
        assert label.startswith(f"{name}: list price $0.306, company proxy $0.0040, 41k tokens. By model, list price: ")
        # the y axis names the dearest day (three days ago: 0.5 + 2 x 0.002); the x axis names the first, the middle, today
        assert cdp.js(texts(".u-y span")) == ["$0.504", "$0.252", "0"]
        xs = cdp.js(texts(".u-x span"))
        assert len(xs) == 14 and [x for x in xs if x] == [
            f"{(clock.day - timedelta(days=n)).day} {(clock.day - timedelta(days=n)).strftime('%b')}" for n in (13, 6, 0)]
        # each model drawn has a colour, named in the legend, the one with most of the measure first; a model with none of
        # it (Codex has no price) is not drawn and takes no colour
        assert cdp.js(texts(".u-legend li")) == ["Claudeclaude-sonnet-5-5", "Claudeclaude-haiku-5-5"]
        assert cdp.js("document.querySelector('.u-legend .u-sw').className") == "u-sw m0"
        # the proxy cost is its own chart, with the proxy's models only
        cdp.js(f"{METRIC % 'proxy'}.click()")
        assert cdp.js(texts(".u-legend li")) == ["LiteLLMdeepseek-v4-pro"]
        assert [len(d) for d in cdp.js(SEGMENTS)] == [0] * 13 + [1]
        assert cdp.js(texts(".u-y span")) == ["$0.0040", "$0.0020", "0"]
        assert cdp.js("document.querySelector('.u-cap').textContent") == "Last 14 days · $0.0040 proxy cost"
        # tokens: every model, the one with most tokens in those days first
        cdp.js(f"{METRIC % 'tokens'}.click()")
        assert cdp.js(texts(".u-legend li")) == ["Codexgpt-6.1-sol", "Claudeclaude-sonnet-5-5", "Claudeclaude-haiku-5-5",
                                                 "LiteLLMdeepseek-v4-pro", "LiteLLMqwen3-embedding"]
        cdp.close()


@needs_chrome
def test_the_bars_are_in_proportion_and_the_toggle_switches_to_tokens(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        per_day = cdp.js(SEGMENTS)
        # list price: three days ago ($0.504) is the tallest day and fills the plot; today ($0.306) is in proportion to it
        assert sum(per_day[-4]) == pytest.approx(154, abs=1.5)  # (a sliver is never thinner than 2 px)
        assert sum(per_day[-1]) / sum(per_day[-4]) == pytest.approx(0.306 / 0.504, rel=0.02)
        assert cdp.js(f"{METRIC % 'list'}.getAttribute('aria-pressed')") == "true"
        cdp.js(f"{METRIC % 'tokens'}.focus(); {METRIC % 'tokens'}.click()")
        assert cdp.js("document.activeElement.dataset.fk") == "metric:tokens"  # the repaint gave the focus back
        assert cdp.js(f"{METRIC % 'tokens'}.getAttribute('aria-pressed')") == "true"
        per_day = cdp.js(SEGMENTS)
        assert len(per_day[-1]) == 5  # tokens count the runs with no price too
        assert cdp.js(texts(".u-y span"))[0] == "41k"
        assert cdp.js("document.querySelector('.u-cap').textContent") == "Last 14 days · 46k tokens"
        assert sum(per_day[-1]) == pytest.approx(154, abs=3)  # today is the biggest day in tokens (40,973 against 5,022)
        assert cdp.js("document.querySelector('.u-hint')") is None  # nothing is left out of a token chart
        cdp.js(f"{METRIC % 'list'}.click()")
        cdp.wait("!!document.querySelector('.u-hint')")
        assert cdp.js("document.querySelector('.u-hint').textContent").startswith("Only models with a list price are drawn.")
        cdp.close()


@needs_chrome
def test_a_day_says_what_it_held_on_hover_focus_and_to_a_screen_reader(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        cap = "document.querySelector('.u-cap')"
        assert cdp.js(f"{cap}.textContent") == "Last 14 days · $0.810 list price"
        today = f"document.querySelector('.u-day[data-day=\"{clock.day.isoformat()}\"]')"
        cdp.js(f"{today}.dispatchEvent(new PointerEvent('pointerenter', {{pointerType: 'mouse'}}))")
        name = f"{clock.day.strftime('%a')} {clock.day.day} {clock.day.strftime('%b')}"
        assert cdp.js(f"{cap}.textContent") == f"{name} · $0.306 list price · 41k tokens" and cdp.js(f"{cap}.classList.contains('on')")
        cdp.js(f"{today}.dispatchEvent(new PointerEvent('pointerleave', {{pointerType: 'mouse'}}))")
        assert cdp.js(f"{cap}.textContent") == "Last 14 days · $0.810 list price"
        cdp.js(f"{today}.focus()")  # a keyboard user gets the same
        assert cdp.js(f"{cap}.textContent") == f"{name} · $0.306 list price · 41k tokens"
        cdp.js(f"{today}.blur()")
        assert cdp.js(f"{cap}.textContent") == "Last 14 days · $0.810 list price"
        # every day is focusable and reads itself out; there is also a plain table of the same numbers
        assert cdp.js("[...document.querySelectorAll('.u-day')].every(g => g.tabIndex === 0 && g.getAttribute('role') === 'img')")
        assert cdp.js("document.querySelectorAll('.u-chart .sr-only tbody tr').length") == 14
        assert cdp.js(texts(".u-chart .sr-only thead th")) == ["Day", "List price", "Proxy cost", "Tokens"]
        assert cdp.js(texts(".u-chart .sr-only tbody tr:last-child td")) == [name, "$0.306", "$0.0040", "41k"]
        assert cdp.js("getComputedStyle(document.querySelector('.u-chart .sr-only')).position") == "absolute"
        assert cdp.js("document.querySelector('.u-chart .sr-only').getBoundingClientRect().width") <= 1
        cdp.close()


@needs_chrome
def test_the_notes_say_what_each_cost_means_and_where_the_numbers_begin(tmp_path, cdp):
    clock = noon_three_days_ago()

    def seed(day, store):
        seed_usage(day, store)
        store.add_usage(row(day, 5, 9, "litellm", "glm-5.3", "feedback", "feedback_writer", i=5, cost=0.001, basis="proxy",
                            source="live"))
        store.set_cursor("usage_backfill:litellm", '{"state": "unavailable", "reason": "/spend/logs 404"}')
    with server(tmp_path, clock, seed) as (base, settings, login):
        open_usage(cdp, base, login)
        notes = cdp.js(texts(".u-notes-l li"))
        assert notes[:5] == ["ClaudeList price, paid from your Claude subscription.",
                             "ClaudeSome Claude runs have no cost on record (one that ended without a result, or an old transcript).",
                             "LiteLLMCompany proxy cost.", "LiteLLMCompany proxy: cost not reported (tokens only).",
                             "CodexChatGPT Pro (tokens only)."]
        # the Claude history was made on whatever account the agent ran under then: it says so, with the days it covers
        span = days_text(clock.day - timedelta(days=20), clock.day - timedelta(days=3))
        assert notes[5] == f"ClaudeHistory includes runs on other Claude accounts used on this Mac ({span})"
        assert notes[6].startswith("A run is one Claude run, one Codex run or one proxy request;")  # for no provider: no badge
        assert cdp.js("document.querySelectorAll('.u-notes-l li')[6].querySelector('.u-prov')") is None
        first_live = clock.day - timedelta(days=5)
        assert notes[7] == f"Company-proxy history not available; counted from {first_live.day} {first_live.strftime('%b')}"
        assert notes[8].startswith("Input is the whole prompt, cached tokens included.")
        assert "Cache write" in notes[8]
        assert notes[9] == "≥ means some runs in that row had no price, so its cost is at least that much."
        assert len(notes) == 10
        cdp.close()


@needs_chrome
def test_a_run_with_approximate_figures_is_marked_with_a_tilde_and_explained(tmp_path, cdp):
    clock = noon_three_days_ago()

    def seed(day, store):
        seed_usage(day, store)
        # a transcript with no final totals (a run that was killed): its output tokens are far too few, and so is its cost
        store.add_usage(row(day, 1, 9, "claude", "claude-opus-5", "insights", "research", i=1000, o=5, cr=2000, cw=100,
                            cost=0.05, basis="list", ms=9000, source="backfill", ref="approx:killed.jsonl"))
    with server(tmp_path, clock, seed) as (base, settings, login):
        open_usage(cdp, base, login)
        week = cdp.js(providers(".ucard[aria-label=\"7 days\"]"))
        assert week[0] == ["Claude", "≈ $0.860", "list price, paid from your subscription",
                           ["23.3k in (14.7k cached, 2.25k cache write) · ≈ 627 out",
                            "9 runs · 1 failed · 1 approximate · 1 unpriced"]]
        assert week[1][1] == "≥ $0.0040"  # the proxy's figures are exact: no tilde
        today = cdp.js(providers(".ucard[aria-label=Today]"))
        assert not today[0][1].startswith("≈")  # the approximate run was yesterday
        rows = {r[0]: r for r in cdp.js(table(MODELS))}
        opus = rows["Claudeclaude-opus-5"]
        assert opus[3] == "≈ 5" and opus[6] == "≈ $0.050|list price"  # its output and its cost; the prompt is exact
        assert opus[2] == "3.1k" and opus[4] == "2k" and opus[5] == "100"
        assert cdp.js(f"document.querySelector('{MODELS} tbody tr[data-model=\"claude-opus-5\"] .cost').title") == "1 run with approximate figures"
        jobs = {r[0]: r for r in cdp.js(table(JOBS))}
        assert jobs["Insights"][3].startswith("≈ ") and jobs["Insights"][4].startswith("≈ ")  # output and list price
        assert not jobs["Feedback"][5].startswith("≈")                                       # not the proxy's cost
        assert any(n.startswith("Claude≈ marks figures summed from a transcript with no final totals")
                   for n in cdp.js(texts(".u-notes-l li")))
        cdp.close()


@needs_chrome
def test_a_run_whose_job_was_not_worked_out_is_listed_as_earlier_runs_and_not_as_slack(tmp_path, cdp):
    clock = noon_three_days_ago()

    def seed(day, store):
        seed_usage(day, store)
        store.add_usage(row(day, 2, 9, "claude", "claude-haiku-5-5", "unknown", "other", i=10, o=1, cost=0.002, basis="list",
                            source="backfill", ref="old.jsonl"))
    with server(tmp_path, clock, seed) as (base, settings, login):
        open_usage(cdp, base, login)
        jobs = {r[0]: r for r in cdp.js(table(JOBS))}
        assert "Earlier runs (job unknown)" in jobs and jobs["Earlier runs (job unknown)"][1] == "1"
        assert jobs["Slack (DMs and clicks)"][1] == "6"  # the slack runs are the ones that were seeded as slack
        cdp.close()


@needs_chrome
def test_a_days_total_from_the_proxy_adds_cost_and_no_call(tmp_path, cdp):
    clock = noon_three_days_ago()

    def seed(day, store):
        seed_usage(day, store)
        store.add_usage(row(day, 5, 0, "litellm", "glm-5.3", "other", "daily_total", cost=1.25, basis="proxy",
                            source="backfill", ref="day:x:glm-5.3"))
    with server(tmp_path, clock, seed) as (base, settings, login):
        open_usage(cdp, base, login)
        # the 7 days hold the day total: a row with no calls, its cost, and a note that says what it is
        rows = cdp.js(table(MODELS))
        glm = next(r for r in rows if r[0].endswith("glm-5.3"))
        assert glm[:7] == ["LiteLLMglm-5.3", "0", "0", "0", "0", "0", "$1.25|company proxy"] and glm[7:] == ["—", "0"]
        # the proxy's line of the card has the day's cost (0.004 + 1.25) and its runs are only the runs there were
        lite = cdp.js(providers(".ucard[aria-label=\"7 days\"]"))[1]
        assert lite == ["LiteLLM", "≥ $1.25", "company proxy, real cost", ["2.05k in · 400 out", "3 runs · 2 unpriced"]]
        notes = cdp.js(texts(".u-notes-l li"))
        [daily] = [n for n in notes if n.endswith("they add cost, not runs or tokens.")]
        assert "UTC days" in daily  # the proxy's days are not Kuwait's
        # the model has a colour in the proxy chart (it is the biggest there), and none in the token chart (it has no tokens)
        cdp.js(f"{METRIC % 'proxy'}.click()")
        assert cdp.js(texts(".u-legend li")) == ["LiteLLMglm-5.3", "LiteLLMdeepseek-v4-pro"]
        cdp.js(f"{METRIC % 'tokens'}.click()")
        assert "LiteLLMglm-5.3" not in cdp.js(texts(".u-legend li"))
        cdp.close()


@needs_chrome
@pytest.mark.parametrize("cursor,expect", [
    (None, "Company-proxy history not imported yet; counted from"),
    ('{"state": "skipped"}', "Company-proxy history not imported yet; counted from"),
    ('{"state": "unavailable"}', "Company-proxy history not available; counted from"),
    ('{"state": "imported"}', None),
])
def test_the_proxy_history_note_follows_what_the_import_did(tmp_path, cdp, cursor, expect):
    clock = noon_three_days_ago()

    def seed(day, store):
        seed_usage(day, store)
        if cursor:
            store.set_cursor("usage_backfill:litellm", cursor)
    with server(tmp_path, clock, seed) as (base, settings, login):
        open_usage(cdp, base, login)
        found = cdp.js("document.querySelector('.u-hist')?.textContent")
        if expect is None:
            assert found is None
        else:
            assert found.startswith(expect)
        cdp.close()


@needs_chrome
def test_an_empty_database_says_so_and_how_to_fill_it(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock, lambda day, store: None) as (base, settings, login):
        open_usage(cdp, base, login)
        assert cdp.js("document.querySelector('.u-empty .empty-t').textContent") == "No usage recorded yet"
        assert cdp.js("document.querySelector('.u-empty code').textContent") == "uv run haseeb-agent usage-backfill"
        assert cdp.js("document.querySelector('.u-empty .empty-s').textContent").endswith("in Terminal, from the agent's folder.")
        assert cdp.js("document.querySelectorAll('.u-table, .ucard, .u-chart').length") == 0
        cdp.close()


@needs_chrome
def test_hostile_names_are_shown_as_text_and_nothing_runs(tmp_path, cdp):
    clock = noon_three_days_ago()
    evil_model = HOSTILE_IMG + " " + RLO + "gnp.exe"

    def seed(day, store):  # straight into the table, round the checks the store makes on the way in
        insert_unchecked(store, row(day, 0, 9, "claude", evil_model, "<svg onload=window.__pwned=1>", "<x>", i=5, o=5,
                                    cost=0.01, basis="list", ms=100))
        insert_unchecked(store, row(day, 0, 9, "litellm", "javascript:alert(1)", "<img src=x onerror=window.__pwned=1>", "s",
                                    i=5, cost=0.01, basis="proxy", ms=100))
        store.set_cursor("usage_backfill:litellm", '{"state": "<img src=x onerror=window.__pwned=1>"}')
    with server(tmp_path, clock, seed) as (base, settings, login):
        open_usage(cdp, base, login)
        time.sleep(0.5)
        names = cdp.js(texts(f"{MODELS} .u-mn"))
        assert evil_model in names and "javascript:alert(1)" in names  # literally
        assert HOSTILE_IMG[:12] in cdp.js(f"document.querySelector('{MODELS}').textContent")
        assert cdp.js("window.__pwned") is None
        assert cdp.js("document.querySelectorAll('#view img, #view svg[onload], #view [onerror]').length") == 0
        assert cdp.js("document.querySelectorAll('#view script, #view iframe').length") == 0
        assert cdp.js(f"document.querySelectorAll('{MODELS} .u-mn').length") == cdp.js(f"document.querySelectorAll('{MODELS} bdi').length")
        jobs = cdp.js(texts(f"{JOBS} tbody td.l"))
        # a job the page has no name for is shown with its underscores as spaces, and otherwise as it is
        assert set(jobs) == {"<svg onload=window. pwned=1>", "<img src=x onerror=window. pwned=1>"}
        assert cdp.js(texts(".u-hist"))[0].startswith("Company-proxy history not imported yet")  # a state it does not know
        cdp.close()


@needs_chrome
def test_the_page_refreshes_itself_and_keeps_the_focus(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login, init_js=FAST_REFRESH)
        today_calls = "document.querySelector('.ucard[aria-label=Today] .u-p[data-provider=claude] .u-ps:last-child').textContent"
        assert cdp.js(today_calls) == "5 runs · 1 failed · 1 unpriced"
        cdp.js("document.querySelector('.th-sort[data-fk=\"job:runs\"]').focus()")
        s = Store(settings.db_path)
        try:
            s.add_usage(row(clock.day, 0, 11, "claude", "claude-haiku-5-5", "slack", "slack_read_channel", i=1, o=1,
                            cost=0.002, basis="list", ms=1000))
        finally:
            s.close()
        cdp.wait(f"{today_calls} === '6 runs · 1 failed · 1 unpriced'")
        assert cdp.js("document.activeElement.dataset.fk") == "job:runs"  # the repaint did not take the focus away
        cdp.close()


@needs_chrome
def test_a_page_that_cannot_load_says_so_and_tries_again(tmp_path, cdp):
    clock = noon_three_days_ago()
    gate = {"/api/usage": 500}
    quick_retry = """(() => { const st = window.setTimeout;
      window.setTimeout = function (f, ms) { return st.call(this, f, ms === 5000 ? 200 : ms); }; })();"""
    with server(tmp_path, clock, gate=gate) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=quick_retry)
        cdp.wait("!!document.querySelector('.page-live')")
        cdp.js("location.hash = '#/usage'")
        cdp.wait("!!document.querySelector('.usage .load-error')")
        assert cdp.js("document.querySelector('.usage .load-error').textContent") == "Couldn't load the usage. Trying again…"
        assert cdp.js("document.querySelectorAll('.u-table').length") == 0
        gate.clear()
        cdp.wait("document.querySelectorAll('.ucard').length === 3")
        assert cdp.js("document.querySelector('.usage .load-error')") is None
        cdp.close()


@needs_chrome
def test_the_chosen_sort_and_range_survive_a_visit_to_another_page(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login)
        cdp.js("document.querySelectorAll('.u-seg[aria-label=Range] button')[0].click()")
        cdp.wait(f"document.querySelector('{MODELS} caption').textContent.endsWith('today')")
        cdp.js(f"document.querySelector('{MODELS} .th-sort[data-fk=\"model:runs\"]').click()")
        cdp.js("location.hash = '#/score'")
        cdp.wait("!!document.querySelector('#view > .score')")
        cdp.js("location.hash = '#/usage'")
        cdp.wait("document.querySelector('#view > .usage')?.dataset.ready === '1'")
        assert cdp.js(f"document.querySelector('{MODELS} caption').textContent").endswith("today")
        assert cdp.js(f"document.querySelector('{MODELS} th[aria-sort=descending] .th-sort').dataset.fk") == "model:runs"
        cdp.close()


@needs_chrome
def test_the_model_colours_follow_the_theme(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login, dark=True)
        bar = "getComputedStyle(document.querySelector('.u-seg-r.m1')).fill"  # the second model drawn: Claude Haiku
        swatch = "getComputedStyle(document.querySelector('.u-sw.m1')).backgroundColor"
        assert cdp.js(bar) == "rgb(34, 211, 238)" and cdp.js(swatch) == "rgb(34, 211, 238)"  # the legend's is the bar's
        cdp.js("document.querySelector('[data-theme-pick=daylight]').click()")
        cdp.wait(f"{bar} === 'rgb(11, 122, 150)' && {swatch} === 'rgb(11, 122, 150)'")  # the theme eases in over half a second
        cdp.close()


@needs_chrome
def test_phone_width_has_no_horizontal_scroll_and_a_row_is_a_card(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login, width=380)
        time.sleep(0.8)  # the entrance animations settle
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        assert cdp.js("[...document.querySelectorAll('.usage .card')].every(c => c.getBoundingClientRect().right <= 380)")
        # the chart and the tables keep inside their cards: nothing scrolls sideways, not even a table
        assert cdp.js("[...document.querySelectorAll('.usage .tbl')].every(t => t.scrollWidth <= t.clientWidth + 1)")
        # a model's row: the name and the cost share the first line, the small figures come below, each saying what it is
        geo = cdp.js(f"""(() => {{ const tr = document.querySelector('{MODELS} tbody tr');
          const q = (s) => tr.querySelector(s).getBoundingClientRect();
          return {{name: q('td.l').top, cost: q('td.cost').top, runs: q('td[data-label=Runs]').top,
                  label: getComputedStyle(tr.querySelector('td[data-label=Runs]'), '::before').content}}; }})()""")
        assert abs(geo["name"] - geo["cost"]) < 8 and geo["runs"] > geo["name"] + 10
        assert geo["label"] == '"Runs"'
        assert cdp.js("getComputedStyle(document.querySelector('.u-table thead tr'), '::before').content") == '"Sort by"'
        # the controls are still reachable by touch-sized buttons and the chart still reads
        assert cdp.js("document.querySelectorAll('.u-day').length") == 14
        assert cdp.js("document.querySelector('.u-bars').getBoundingClientRect().width") > 200
        cdp.close()


NAV = """(() => {
  const nav = document.querySelector('.nav'), nb = nav.getBoundingClientRect();
  const lo = nb.left + nav.clientLeft, hi = lo + nav.clientWidth;      // what of the nav can be seen: inside its border
  const links = [...nav.querySelectorAll('a')].map(a => {
    const b = a.getBoundingClientRect(), r = document.createRange(); r.selectNodeContents(a);
    return {route: a.dataset.route, on: a.classList.contains('on'), whole: b.left >= lo - 0.5 && b.right <= hi + 0.5,
            text: r.getBoundingClientRect().width <= a.clientWidth + 0.5 && a.scrollWidth <= a.clientWidth + 0.5,
            one: b.height < 3.4 * parseFloat(getComputedStyle(a).fontSize),   // a wrapped label would be two lines tall
            left: b.left - lo, right: b.right - lo};
  });
  return {links, scrolls: nav.scrollWidth > nav.clientWidth + 1, visible: nav.clientWidth, scrollLeft: nav.scrollLeft,
          page: document.documentElement.scrollWidth, max: nav.scrollWidth - nav.clientWidth};
})()"""
ROUTES = ["live", "history", "decks", "logs", "score", "usage"]


@needs_chrome
@pytest.mark.parametrize("width", [280, 320, 360, 375])
def test_all_six_tabs_are_reachable_on_a_narrow_phone_and_none_has_cut_off_text(tmp_path, cdp, width):
    """The sixth tab (Usage) went past the edge of the nav on a phone: its name was cut off ("Usa") with nothing to say the
    nav scrolls (review I2). Now every label is whole and the six tabs sit side by side from 320 px up; on a screen narrower
    than that (280 here) the nav scrolls and the tab of the page being shown is always scrolled fully into view."""
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login, width=width)
        time.sleep(0.5)
        for route in ROUTES:
            cdp.js(f"location.hash = '#/{route}'")
            cdp.wait(f"document.querySelector('.nav a.on')?.dataset.route === '{route}'")
            nav = cdp.js(NAV)
            assert [x["route"] for x in nav["links"]] == ROUTES
            assert all(x["text"] and x["one"] for x in nav["links"]), (width, route, nav)  # each label whole, on one line
            [here] = [x for x in nav["links"] if x["on"]]
            assert here["route"] == route and here["whole"], (width, route, nav)  # the tab of this page is fully in view
            if width >= 320:
                assert not nav["scrolls"] and all(x["whole"] for x in nav["links"]), (width, route, nav)  # all six at once
            assert nav["page"] <= width, (width, route, nav)  # the page itself does not scroll sideways
        # whatever the width, the nav can be scrolled to either end and every tab is then whole in view
        for end, tab in ((0, "live"), (10**6, "usage")):
            cdp.js(f"document.querySelector('.nav').scrollLeft = {end}")
            nav = cdp.js(NAV)
            assert next(x for x in nav["links"] if x["route"] == tab)["whole"], (width, tab, nav)
        if width < 320:
            assert cdp.js(NAV)["scrolls"]  # six tabs do not fit: the nav is what scrolls, not the page
            # a phone turned from landscape to portrait: the tab of the page being shown is brought back into view
            cdp.js("location.hash = '#/usage'")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 700, "height": 900, "deviceScaleFactor": 1, "mobile": True})
            cdp.wait("document.querySelector('.nav').scrollWidth <= document.querySelector('.nav').clientWidth")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": True})
            cdp.wait("document.querySelector('.nav').scrollWidth > document.querySelector('.nav').clientWidth")
            # (the resize event comes at the next frame, a moment after the new size is in force)
            cdp.wait(f"{NAV}.links.find(x => x.route === 'usage').whole")
        cdp.close()


@needs_chrome
def test_reduced_motion_turns_the_pages_animation_off(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login, reduced=True)
        names = cdp.js("[...document.querySelectorAll('.usage .card, .usage .rise, .usage .u-seg-b')].map(e => getComputedStyle(e).animationName)")
        assert names and set(names) == {"none"}, names
        cdp.close()


@needs_chrome
def test_the_page_does_not_replay_its_entrance_when_it_refreshes(tmp_path, cdp):
    clock = noon_three_days_ago()
    with server(tmp_path, clock) as (base, settings, login):
        open_usage(cdp, base, login, init_js=FAST_REFRESH)
        time.sleep(1.0)
        s = Store(settings.db_path)
        try:
            s.add_usage(row(clock.day, 0, 11, "claude", "claude-haiku-5-5", "slack", "slack_read_channel", i=1, o=1,
                            cost=0.002, basis="list", ms=1000))
        finally:
            s.close()
        cdp.wait("document.querySelector('.ucard[aria-label=Today] .u-p[data-provider=claude] .u-ps:last-child').textContent === "
                 "'6 runs · 1 failed · 1 unpriced'")
        assert cdp.js("document.querySelectorAll('.usage .rise').length") == 0  # only the first paint animates
        cdp.close()
