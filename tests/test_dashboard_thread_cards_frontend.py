"""The cards of the thread watch on the dashboard (2026-10-21): a follow-back says "Follow-back", a reply on Haseeb's insight
says "Reply", a post of their own that landed in his insight's thread is a plain feedback card; and reading a thread is an
optional step of the Live pipeline. Static checks read the files under dashboard/static; the render checks use the dashboard's
existing Chrome harness (a local uvicorn on 127.0.0.1 against a tmp store, headless Chrome over CDP), skipped without Chrome."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from haseeb_agent.dashboard.app import STATIC_DIR
from tests.test_dashboard_frontend import (_code, _done, _item, cdp, live_server, login_url, needs_chrome,  # noqa: F401
                                           render_dom, section)

FB_ID = "followback-1792581205.027989"
OWN_ID = "threadfb-1792581035.472729"
REPLY_ID = "insreply-1792581100.000100"
PLAIN_ID = "feedback-1792574403.734689"
HOSTILE = '<img src=x onerror="window.__pwned=1"> Zakir'


def seed(store, settings) -> None:
    today = datetime.now(settings.tz).date()
    rows = [(FB_ID, "Follow-back · Iqbal Qazi's thread"), (OWN_ID, "Feedback: Zakir Ullah"),
            (REPLY_ID, f"Reply on your insight · {HOSTILE}"), (PLAIN_ID, "Feedback: Iqbal Qazi")]
    for n, (iid, title) in enumerate(rows, 1):
        store.upsert_item(_item(iid, "feedback", title, f"✅ A strength {n}.\n❓ A question?", today, f"{50 + n}.1"))
    store.set_cursor(f"feedback_review:{FB_ID}", "Reviewed: reach-3c5e08e.html (desktop + phone)")


def card(item: str) -> str:
    return f"document.querySelector('article.icard[data-item=\"{item}\"]')"


# ---------------------------------------------------------------- static

def test_the_inbox_names_the_two_answer_kinds_and_leaves_the_rest_as_they_were():
    js = (STATIC_DIR / "inbox.js").read_text()
    code = _code(js)
    assert 'followback: { label: "Follow-back", icon: "chat" }' in code and 'insight_reply: { label: "Reply", icon: "chat" }' in code
    assert "own_post" not in code  # a post of their own is a plain feedback card
    assert "VARIANT_KIND[it.variant] || (idea ? IDEA : KIND[it.kind])" in code  # a variant first, then an idea (feat/ideas)


def test_the_live_pipeline_knows_the_thread_read_as_an_optional_step():
    code = _code((STATIC_DIR / "live.js").read_text())
    assert 'new Set(["site_review", "read_thread"])' in code
    assert '"read_channel", "read_thread", "site_review", "write_draft", "deliver"' in code
    assert 'read_thread: ["Read thread", "chat"]' in code


# ---------------------------------------------------------------- the cards

@needs_chrome
def test_each_kind_of_answer_has_its_own_label_and_the_rest_keep_theirs(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 4")
        got = cdp.js("""Object.fromEntries([...document.querySelectorAll('article.icard')].map(c => [c.dataset.item, {
            label: c.querySelector('.kind span:not(.ico)').textContent, aria: c.getAttribute('aria-label'),
            title: c.querySelector('.icard-title').textContent, classes: c.className}]))""")
        assert got[FB_ID]["label"] == "Follow-back" and got[FB_ID]["aria"] == "Follow-back: Follow-back · Iqbal Qazi's thread"
        assert got[REPLY_ID]["label"] == "Reply" and got[REPLY_ID]["title"] == f"Reply on your insight · {HOSTILE}"
        assert got[OWN_ID]["label"] == "Feedback" and got[PLAIN_ID]["label"] == "Feedback"
        assert all("k-feedback" in v["classes"] for v in got.values())  # the same colours as any feedback card
        cdp.close()


@needs_chrome
def test_a_follow_back_card_shows_the_review_line_like_any_feedback_card(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 4")
        assert cdp.js(f"{card(FB_ID)}.querySelector('.review-line span:not(.ico)').textContent") == \
            "Reviewed: reach-3c5e08e.html (desktop + phone)"
        assert cdp.js(f"getComputedStyle({card(REPLY_ID)}.querySelector('.review-line')).display") == "none"  # nothing to say
        cdp.close()


@needs_chrome
def test_a_hostile_title_is_text_and_nothing_else(tmp_path, cdp):
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('article.icard').length === 4")
        assert cdp.js(f"{card(REPLY_ID)}.querySelectorAll('img').length") == 0 and cdp.js("window.__pwned") is None
        cdp.close()


# ---------------------------------------------------------------- the pipeline

def _run(store, settings, *, step: str, read: bool) -> None:
    now = datetime.now(settings.tz)
    store.set_cursor("tick_seen", (now - timedelta(seconds=20)).isoformat())
    _done(store, "feedback", "read_channel", now - timedelta(seconds=90), 5)
    if read:
        _done(store, "feedback", "read_thread", now - timedelta(seconds=60), 9)
    store.add_event("feedback", step, model=None, started=now - timedelta(seconds=20), ended=None, ok=None, kind=None)


def _labels(tmp_path, seed_fn) -> tuple[str, list[str]]:
    with live_server(tmp_path, seed_fn) as (base, _s, login):
        now = section(render_dom(tmp_path, base, login), "Right now")
    return now, re.findall(r'class="step-l">([^<]*)<', now)


@needs_chrome
def test_a_run_that_reads_a_thread_shows_the_step_in_its_place(tmp_path):
    now, labels = _labels(tmp_path, lambda st, se: _run(st, se, step="write_draft", read=True))
    assert labels == ["Read channel", "Read thread", "Write draft", "Your DM"] and "step 3 of 4" in now
    assert re.search(r'class="step done".*?Read thread', now, re.S)


@needs_chrome
def test_a_run_that_is_reading_a_thread_now_has_it_live(tmp_path):
    now, labels = _labels(tmp_path, lambda st, se: _run(st, se, step="read_thread", read=False))
    assert labels == ["Read channel", "Read thread", "Write draft", "Your DM"]
    assert re.search(r'class="step live"[^>]*aria-current="step".*?Read thread', now, re.S) and "step 2 of 4" in now


@needs_chrome
def test_a_run_that_read_no_thread_does_not_show_the_step(tmp_path):
    now, labels = _labels(tmp_path, lambda st, se: _run(st, se, step="write_draft", read=False))
    assert labels == ["Read channel", "Write draft", "Your DM"] and "Read thread" not in now
