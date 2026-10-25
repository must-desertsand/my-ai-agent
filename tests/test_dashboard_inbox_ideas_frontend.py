"""An idea on a card in "Waiting for you" (2026-10-22). A schema v2 insight is a daily AI business idea: its card says
"Idea", its heading is the idea's header ("title · price", as in the DM), and its text is the idea, why now, who pays,
the build and the first step. A schema v1 insight, stored before ideas, keeps "Insight" and its title and text exactly
as before. The render checks start a real uvicorn on 127.0.0.1 against a tmp store, log in as `dashboard open` does and
drive headless Chrome over CDP, as the other dashboard tests do (skipped when Chrome is missing). Offline guard on; tmp
data only."""
from __future__ import annotations

import json
from datetime import datetime

from haseeb_agent.dashboard.app import STATIC_DIR
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    HOSTILE_IMG, HOSTILE_SCRIPT, _code, _item, cdp, live_server, login_url, needs_chrome,
)
from tests.test_insights import good_idea_meta, good_meta

IDEA, OLD, HOSTILE = "insight-t4-1", "insight-t4-2", "insight-t4-3"


def card(iid: str) -> str:
    return f"document.querySelector('article.icard[data-item=\"{iid}\"]')"


def part(iid: str, css: str) -> str:
    """The text of one part of a card (css without quotes)."""
    return f"{card(iid)}.querySelector('{css}').textContent.trim()"


def put(store, settings, iid: str, meta: dict, title: str, dm_ts: str) -> None:
    today = datetime.now(settings.tz).date()
    store.upsert_item(_item(iid, "insight", title, json.dumps(meta), today, dm_ts))


def seed(store, settings) -> None:
    put(store, settings, IDEA, good_idea_meta(batch=IDEA), "Topic alpha", "301.1")  # an idea (schema v2)
    put(store, settings, OLD, good_meta(batch=OLD, name="beta"), "Topic beta", "301.2")  # a v1 insight, as before


def seed_hostile(store, settings) -> None:
    meta = good_idea_meta(batch=HOSTILE)
    meta["insight"].update(title=HOSTILE_IMG, idea=HOSTILE_SCRIPT, why_now="<svg onload=\"window.__pwned=1\">")
    put(store, settings, HOSTILE, meta, "Hostile idea", "301.3")


# ---------------------------------------------------------------- static checks

def test_the_idea_label_is_set_by_the_items_format_and_built_as_text():
    code = _code((STATIC_DIR / "inbox.js").read_text())
    assert 'label: "Idea"' in code and 'label: "Insight"' in code  # both stay: v1 items keep theirs
    assert 'it.format === "idea"' in code and 'it.kind === "insight"' in code
    assert "innerHTML" not in code and "insertAdjacentHTML" not in code


# ---------------------------------------------------------------- the render

@needs_chrome
def test_an_idea_card_says_idea_and_an_old_insight_card_keeps_insight(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"{card(IDEA)} && {card(OLD)}")
        # the badge, the card's name for screen readers, and the kind's own class (its colour) for both
        assert cdp.js(part(IDEA, ".kind")) == "Idea" and cdp.js(part(OLD, ".kind")) == "Insight"
        assert cdp.js(f"{card(IDEA)}.getAttribute('aria-label')") == "Idea: Topic alpha · Rs 15k/month"
        assert cdp.js(f"{card(OLD)}.getAttribute('aria-label')") == "Insight: Topic beta"
        assert cdp.js(f"{card(IDEA)}.classList.contains('k-insight') && {card(OLD)}.classList.contains('k-insight')")
        # the idea's heading is its header, as in the DM; the text starts at the idea and is not headed twice
        assert cdp.js(part(IDEA, ".icard-title")) == "Topic alpha · Rs 15k/month"
        text = cdp.js(f"{card(IDEA)}.querySelector('.draft').textContent")
        assert text.startswith("The pain behind Topic alpha. We build the fix.")
        for piece in ("A new model made this cheap to build this week.", "Our estimate is Rs 15k/month.",
                      "Two people ship a bot on a hosted model in two weeks.", "Call 10 buyers by Friday."):
            assert piece in text, piece
        for left_out in ("Topic alpha ·", "A rival may copy it fast.", "The price is untested with buyers."):
            assert left_out not in text, left_out
        # the v1 card is what it was: the title as the heading, the text after it
        assert cdp.js(part(OLD, ".icard-title")) == "Topic beta"
        old = cdp.js(f"{card(OLD)}.querySelector('.draft').textContent")
        assert old.startswith("What happened with Topic beta.") and "Topic beta\n" not in old
        # both are still insight cards: the same buttons, and the deck strip's place
        for iid in (IDEA, OLD):
            labels = cdp.js(f"[...{card(iid)}.querySelectorAll('.actions button')].map(b => b.textContent.trim())")
            assert labels == ["Post", "Edit", "Shorter", "Skip"], iid
        assert cdp.js("document.querySelector('.count-badge').textContent") == "2"
        cdp.close()


@needs_chrome
def test_an_ideas_hostile_text_is_shown_as_text(tmp_path, cdp):
    with live_server(tmp_path, seed_hostile, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait(card(HOSTILE))
        assert cdp.js(part(HOSTILE, ".kind")) == "Idea"
        assert cdp.js(part(HOSTILE, ".icard-title")) == f"{HOSTILE_IMG} · Rs 15k/month"
        text = cdp.js(f"{card(HOSTILE)}.querySelector('.draft').textContent")
        assert text.startswith(HOSTILE_SCRIPT) and '<svg onload="window.__pwned=1">' in text
        # all of it is text: the heading and the text each hold their one <bdi> and nothing else, and nothing ran
        for css in (".icard-title", ".draft"):
            assert cdp.js(f"{card(HOSTILE)}.querySelector('{css}').querySelectorAll('*').length") == 1, css
        assert cdp.js("document.querySelectorAll('img[src=\"x\"], svg[onload]').length") == 0
        assert cdp.js("window.__pwned === undefined")
        cdp.close()
