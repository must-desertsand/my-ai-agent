"""The site review of one post (haseeb_agent/site_review/service.py): targets, limits, budgets, storage, and every way a target can
fail, each ending as a short reason in the DM line. The renderers and the model are fakes: no Chrome, no network, no model."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent.config import load_settings
from haseeb_agent.models import FileRef
from haseeb_agent.offline import OfflineError
from haseeb_agent.site_review import service as svc
from haseeb_agent.site_review.chrome import RenderError
from haseeb_agent.site_review.service import AttachmentError, Renderers, review_post
from haseeb_agent.store import Store
from tests.test_site_review_review import GOOD, FakeLLM, rendered

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")
NOW = datetime(2026, 10, 21, 14, 0, tzinfo=KW)
LINK = "<https://reach-poc.vercel.app|reach-poc.vercel.app>"
HTML = FileRef("F1", "reach-io-poc-21-oct.html", "text/html")
IID = "feedback-1792574403.734689"


@pytest.fixture
def settings(tmp_path):
    from dataclasses import replace
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return replace(load_settings(ROOT / "tests" / "fixtures" / "config.toml", env), db_path=str(tmp_path / "data" / "agent.sqlite"))


@pytest.fixture
def store(tmp_path):
    (tmp_path / "data").mkdir()
    return Store(tmp_path / "data" / "agent.sqlite")


class Fakes:
    """Renderers that record what they were asked and answer from a script; a read_html that reads from a dict."""

    def __init__(self, link=None, html=None, files=None, clock=None, cost=0.0):
        self.link_answer, self.html_answer = link, html
        self.files = files if files is not None else {"F1": "<!doctype html><h1>Client view</h1>"}
        self.link_calls, self.html_calls, self.read_calls = [], [], []
        self.clock, self.cost = clock, cost

    def _answer(self, a):
        if self.clock is not None:
            self.clock[0] += self.cost
        if isinstance(a, Exception):
            raise a
        return a if a is not None else rendered()

    def render_link(self, url, **kw):
        self.link_calls.append((url, kw))
        return self._answer(self.link_answer)

    def render_html(self, text, **kw):
        self.html_calls.append((text, kw))
        return self._answer(self.html_answer if self.html_answer is not None else rendered(offline=True))

    def read_html(self, ref):
        self.read_calls.append(ref.id)
        got = self.files.get(ref.id)
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise AttachmentError("couldn't read the file")
        return got

    @property
    def renderers(self):
        return Renderers(link=self.render_link, html=self.render_html)


def run(fakes, llm, store, settings, text=LINK, files=(), *, now=NOW, iid=IID, clock=None):
    kw = {"clock": (lambda: fakes.clock[0])} if fakes.clock is not None else {}
    return review_post(text, list(files), read_html=fakes.read_html, llm=llm, store=store, settings=settings, now=now,
                       item_id=iid, renderers=fakes.renderers, **kw)


def good_llm(n=1):
    return FakeLLM(*[json.dumps(GOOD)] * n)


# ---------------------------------------------------------------- what it reviews

def test_a_post_with_nothing_to_look_at_is_not_reviewed_and_says_nothing(store, settings):
    f, llm = Fakes(), good_llm()
    r = run(f, llm, store, settings, text="just words, and a PDF", files=[FileRef("F9", "deck.pdf", "application/pdf")])
    assert (r.outcomes, r.line, r.notes) == ([], None, None)
    assert f.link_calls == f.html_calls == [] and llm.calls == [] and store.get_cursor(svc.cap_key(settings, NOW)) is None


def test_a_review_that_is_switched_off_in_the_config_is_skipped(store, settings):
    from dataclasses import replace
    f, llm = Fakes(), good_llm()
    r = run(f, llm, store, replace(settings, site_review_enabled=False))
    assert (r.outcomes, r.line, r.notes) == ([], None, None) and f.link_calls == [] and llm.calls == []


def test_the_config_has_the_step_the_switch_and_the_cap(settings):
    assert settings.models["site_review"] == ["litellm:gemini/gemini-3.1-pro-preview", "litellm:gemini/gemini-3.5-flash"]
    assert settings.site_review_enabled is True and settings.site_review_daily_cap == 10


def test_a_linked_site_is_rendered_reviewed_and_reported(store, settings, tmp_path):
    f, llm = Fakes(link=rendered(phone_scroll=807)), good_llm()
    r = run(f, llm, store, settings)
    assert r.reviewed == 1 and r.failed == 0
    assert r.line == "Reviewed: reach-poc.vercel.app (desktop + phone)"
    assert r.notes.startswith("Page: reach-poc.vercel.app (linked site; looked at on desktop and phone)")
    assert "Account executive view" in r.notes and "807px wide on a 390px screen" in r.notes
    (url, kw), = f.link_calls
    assert url == "https://reach-poc.vercel.app" and kw["chrome"] == settings.chrome_path and kw["budget_s"] <= 45
    assert len(llm.calls) == 1 and llm.calls[0]["images"]  # one vision call, with the pictures
    assert store.get_cursor(svc.cap_key(settings, NOW)) == "1"


def test_the_refused_networks_of_the_config_go_to_the_link_renderer_and_not_to_the_attachments(store, settings):
    from dataclasses import replace
    f, llm = Fakes(), good_llm(2)
    run(f, llm, store, replace(settings, site_review_refused_networks=("203.0.113.7/32", "198.51.100.0/24")),
        text=f"demo {LINK}", files=[HTML])
    (_url, kw), = f.link_calls
    assert kw["refused_networks"] == ("203.0.113.7/32", "198.51.100.0/24")
    (_text, hkw), = f.html_calls
    assert "refused_networks" not in hkw  # an attachment is rendered with no network at all
    f2 = Fakes()
    run(f2, good_llm(), store, settings, now=NOW + timedelta(days=1))
    assert f2.link_calls[0][1]["refused_networks"] == ()  # nothing is listed unless the config lists it


def test_a_link_and_an_attached_html_file_are_both_reviewed(store, settings):
    f, llm = Fakes(), good_llm(2)
    r = run(f, llm, store, settings, text=f"demo {LINK} and a copy", files=[HTML])
    assert r.reviewed == 2 and r.line == "Reviewed: reach-poc.vercel.app, reach-io-poc-21-oct.html (desktop + phone)"
    assert f.html_calls[0][0] == "<!doctype html><h1>Client view</h1>" and f.read_calls == ["F1"]
    assert r.notes.count("Page: ") == 2 and "attached HTML file" in r.notes and "network access was off" in r.notes
    assert store.get_cursor(svc.cap_key(settings, NOW)) == "2"


def test_at_most_two_links_and_one_attachment(store, settings):
    f, llm = Fakes(), good_llm(3)
    r = run(f, llm, store, settings, text="https://a.example.com https://b.example.com https://c.example.com",
            files=[HTML])
    assert [o.target.kind for o in r.outcomes] == ["link", "link", "html"] and len(f.link_calls) == 2


# ---------------------------------------------------------------- what the people see when it fails

@pytest.mark.parametrize("error,reason", [
    (RenderError("timed out", "cdp"), "timed out"), (RenderError("not a public address", "proxy refused"), "not a public address"),
    (RenderError("HTTP 404", "status"), "HTTP 404"), (RenderError("Chrome failed", "launch"), "Chrome failed"),
    (RenderError("the page navigated away", "frame"), "the page navigated away"),
])
def test_a_render_that_fails_is_a_short_reason_and_the_model_is_never_asked(store, settings, error, reason):
    f, llm = Fakes(link=error), good_llm()
    r = run(f, llm, store, settings)
    assert r.line == f"Couldn't review reach-poc.vercel.app: {reason}" and r.notes is None and r.reviewed == 0
    assert llm.calls == []
    assert store.get_cursor(svc.cap_key(settings, NOW)) == "1"  # the attempt used one of the day's reviews


def test_a_model_that_cannot_review_is_a_short_reason(store, settings):
    from haseeb_agent.litellm import LLMError
    llm = FakeLLM(LLMError("all models failed for step 'site_review'", infra=True))
    r = run(Fakes(), llm, store, settings)
    assert r.line == "Couldn't review reach-poc.vercel.app: the review model could not be reached" and r.notes is None


def test_an_answer_nobody_can_read_is_a_short_reason(store, settings):
    r = run(Fakes(), FakeLLM("nonsense", "more nonsense"), store, settings)
    assert r.line == "Couldn't review reach-poc.vercel.app: the review wasn't readable"


def test_one_target_failing_does_not_stop_the_other(store, settings):
    f = Fakes(link=RenderError("timed out"))
    r = run(f, good_llm(), store, settings, text=LINK, files=[HTML])
    assert r.reviewed == 1 and r.failed == 1
    assert r.line == "Reviewed: reach-io-poc-21-oct.html (desktop + phone) · Couldn't review reach-poc.vercel.app: timed out"
    assert r.notes.count("Page: ") == 1 and "attached HTML file" in r.notes


@pytest.mark.parametrize("files,reason", [
    ({"F1": AttachmentError("the file is too large")}, "the file is too large"),
    ({"F1": RuntimeError("slack went away")}, "couldn't read the file"),
    ({}, "couldn't read the file"),
    ({"F1": "x" * (5 * 1024 * 1024 + 1)}, "the file is too large"),
])
def test_an_attachment_that_cannot_be_read_or_is_too_big_is_a_short_reason_and_is_not_rendered(store, settings, files, reason):
    f = Fakes(files=files)
    r = run(f, good_llm(), store, settings, text="see the file", files=[HTML])
    assert r.line == f"Couldn't review reach-io-poc-21-oct.html: {reason}" and f.html_calls == []


def test_a_file_just_inside_the_size_limit_is_rendered(store, settings):
    f = Fakes(files={"F1": "x" * (5 * 1024 * 1024)})
    assert run(f, good_llm(), store, settings, text="see", files=[HTML]).reviewed == 1


def test_the_offline_guard_is_never_taken_for_a_failed_review(store, settings):
    with pytest.raises(OfflineError):
        run(Fakes(link=OfflineError("chrome blocked")), good_llm(), store, settings)
    with pytest.raises(OfflineError):
        run(Fakes(files={"F1": OfflineError("read blocked")}), good_llm(), store, settings, text="x", files=[HTML])


# ---------------------------------------------------------------- an exception nobody planned for (security review M1)

UNPLANNED = [KeyError("targetId"), RuntimeError("chrome said SECRET-PAGE-WORDS"), ValueError("int() of a forged value"),
             TypeError("x"), AttributeError("x"), IndexError("x"), AssertionError("x"), ZeroDivisionError("x"),
             UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")]


@pytest.mark.parametrize("boom", UNPLANNED, ids=lambda e: type(e).__name__)
def test_a_renderer_that_breaks_in_an_unplanned_way_is_a_short_reason_and_never_a_failed_feedback_run(store, settings, boom,
                                                                                                   caplog):
    caplog.set_level(logging.DEBUG)
    llm = good_llm()
    r = run(Fakes(link=boom), llm, store, settings)  # no exception reaches the caller
    assert r.line == "Couldn't review reach-poc.vercel.app: couldn't load the page" and r.notes is None and r.reviewed == 0
    assert llm.calls == []
    assert f"({type(boom).__name__})" in caplog.text and "SECRET-PAGE-WORDS" not in caplog.text  # the type, never the message
    (ev,) = [e for e in store.recent_events(10) if (e.job, e.step) == ("feedback", "site_review")]
    assert ev.ok is False and ev.kind == "RenderFailed"


@pytest.mark.parametrize("boom", UNPLANNED[:4], ids=lambda e: type(e).__name__)
def test_the_same_for_an_attachment_and_the_other_target_still_gets_its_review(store, settings, boom):
    f = Fakes(html=boom)
    r = run(f, good_llm(), store, settings, text=LINK, files=[HTML])
    assert r.reviewed == 1 and r.failed == 1
    assert r.line == ("Reviewed: reach-poc.vercel.app (desktop + phone) · "
                      "Couldn't review reach-io-poc-21-oct.html: couldn't load the page")


@pytest.mark.parametrize("boom", [KeyError("x"), RuntimeError("x"), ValueError("x")], ids=lambda e: type(e).__name__)
def test_a_model_call_that_breaks_in_an_unplanned_way_is_a_short_reason_too(store, settings, boom):
    r = run(Fakes(), FakeLLM(boom), store, settings)
    assert r.line == "Couldn't review reach-poc.vercel.app: the review wasn't readable" and r.reviewed == 0
    (ev,) = [e for e in store.recent_events(10) if (e.job, e.step) == ("feedback", "site_review")]
    assert ev.ok is False and ev.kind == "ReviewFailed"


def test_the_offline_guard_is_a_runtime_error_and_still_passes_through_the_catch_all(store, settings):
    assert issubclass(OfflineError, RuntimeError)  # the reason the order of the handlers matters
    with pytest.raises(OfflineError):
        run(Fakes(link=OfflineError("x")), good_llm(), store, settings)
    with pytest.raises(OfflineError):
        run(Fakes(), FakeLLM(OfflineError("x")), store, settings)


def test_a_failure_the_attachment_code_names_keeps_its_own_reason(store, settings):
    """The catch-all must not flatten the reasons `_render` gives itself."""
    r = run(Fakes(files={"F1": RuntimeError("slack went away")}), good_llm(), store, settings, text="x", files=[HTML])
    assert r.line == "Couldn't review reach-io-poc-21-oct.html: couldn't read the file"
    r = run(Fakes(files={"F1": "x" * (5 * 1024 * 1024 + 1)}), good_llm(), store, settings, text="x", files=[HTML])
    assert r.line == "Couldn't review reach-io-poc-21-oct.html: the file is too large"


# ---------------------------------------------------------------- an out-of-date Chrome (security review M11)

def stale(version="155.0.8059.85", age=62):
    from haseeb_agent.site_review.chrome import ChromeOutOfDate
    return ChromeOutOfDate(version, age)


def test_an_out_of_date_chrome_is_a_short_reason_in_the_line_and_the_review_falls_back_to_the_post_alone(store, settings):
    f, llm = Fakes(link=stale()), good_llm()
    r = run(f, llm, store, settings)
    assert r.line == "Couldn't review reach-poc.vercel.app: Chrome is out of date \u2014 update Chrome"
    assert r.notes is None and r.reviewed == 0 and llm.calls == []  # no notes: the draft is written from the post alone
    (ev,) = [e for e in store.recent_events(10) if (e.job, e.step) == ("feedback", "site_review")]
    assert ev.ok is False and ev.kind == "ChromeStale"


def test_the_same_for_an_attached_page(store, settings):
    r = run(Fakes(html=stale()), good_llm(), store, settings, text="see the file", files=[HTML])
    assert r.line == "Couldn't review reach-io-poc-21-oct.html: Chrome is out of date \u2014 update Chrome"


def test_it_is_logged_once_a_day_with_the_version_and_the_age_and_never_more(store, settings, caplog):
    caplog.set_level(logging.DEBUG)
    day2 = NOW + timedelta(days=1)
    for _ in range(3):
        run(Fakes(link=stale()), good_llm(), store, settings)
    notices = [r for r in caplog.records if "Update Chrome" in r.getMessage()]
    assert len(notices) == 1 and notices[0].levelno == logging.WARNING
    assert "Chrome 155.0.8059.85 is 62 days old (the limit is 45)" in notices[0].getMessage()
    run(Fakes(link=stale(age=63)), good_llm(), store, settings, now=day2)
    run(Fakes(link=stale(age=63)), good_llm(), store, settings, now=day2)
    notices = [r for r in caplog.records if "Update Chrome" in r.getMessage()]
    assert len(notices) == 2 and "63 days old" in notices[1].getMessage()  # the next day says it again, once
    assert store.get_cursor(svc.STALE_CHROME_KEY) == "2026-10-22"


def test_the_notice_is_once_a_day_even_for_a_post_with_two_sites(store, settings, caplog):
    caplog.set_level(logging.DEBUG)
    r = run(Fakes(link=stale(), html=stale()), good_llm(), store, settings, text=f"{LINK} https://b.example.com", files=[HTML])
    assert r.failed == 3
    assert len([x for x in caplog.records if "Update Chrome" in x.getMessage()]) == 1


def test_an_out_of_date_chrome_spends_nothing_of_the_days_reviews_so_the_reason_never_turns_into_a_daily_limit(store, settings):
    f = Fakes(link=stale())
    for i in range(settings.site_review_daily_cap + 5):
        r = run(f, good_llm(), store, settings, iid=f"feedback-stale-{i}")
        assert r.line.endswith("Chrome is out of date \u2014 update Chrome"), i
    assert store.get_cursor(svc.cap_key(settings, NOW)) == "0"
    ok = run(Fakes(), good_llm(), store, settings, text="https://reach-poc.vercel.app/after-the-update")
    assert ok.reviewed == 1 and store.get_cursor(svc.cap_key(settings, NOW)) == "1"  # once Chrome is updated, the full cap is there


def test_giving_back_a_slot_never_goes_below_zero_and_survives_a_bad_counter(store, settings):
    key = svc.cap_key(settings, NOW)
    svc.release_slot(store, key)
    assert store.get_cursor(key) == "0"
    store.set_cursor(key, "garbage")
    svc.release_slot(store, key)
    assert store.get_cursor(key) == "0"
    store.set_cursor(key, "4")
    svc.release_slot(store, key)
    assert store.get_cursor(key) == "3"


def test_other_render_failures_still_count_and_do_not_say_chrome_is_old(store, settings, caplog):
    caplog.set_level(logging.DEBUG)
    r = run(Fakes(link=RenderError("Chrome failed", "launch")), good_llm(), store, settings)
    assert r.line == "Couldn't review reach-poc.vercel.app: Chrome failed" and store.get_cursor(svc.cap_key(settings, NOW)) == "1"
    assert "Update Chrome" not in caplog.text and store.get_cursor(svc.STALE_CHROME_KEY) is None


# ---------------------------------------------------------------- limits

def test_over_the_daily_cap_a_target_is_reported_and_nothing_is_rendered(store, settings):
    store.set_cursor(svc.cap_key(settings, NOW), "10")
    f, llm = Fakes(), good_llm()
    r = run(f, llm, store, settings)
    assert r.line == "Couldn't review reach-poc.vercel.app: daily limit" and r.notes is None
    assert f.link_calls == [] and llm.calls == [] and store.get_cursor(svc.cap_key(settings, NOW)) == "10"


def test_the_cap_is_ten_a_day_counted_per_target_and_the_next_day_starts_again(store, settings):
    from dataclasses import replace
    s = replace(settings, site_review_daily_cap=3)
    f, llm = Fakes(), good_llm(9)
    r1 = run(f, llm, store, s, text="https://a.example.com https://b.example.com", files=[HTML])
    assert r1.reviewed == 3 and store.get_cursor(svc.cap_key(s, NOW)) == "3"
    r2 = run(f, llm, store, s, text=LINK)
    assert r2.line == "Couldn't review reach-poc.vercel.app: daily limit"
    tomorrow = NOW + timedelta(days=1)
    assert run(f, llm, store, s, text=LINK, now=tomorrow).reviewed == 1 and store.get_cursor(svc.cap_key(s, tomorrow)) == "1"
    assert svc.DAILY_CAP == 10


# ---- security review M10: the policy first, and one atomic operation for the count

REFUSED_LINKS = ["http://10.0.0.1/", "http://192.168.1.10/admin", "http://127.0.0.1:8765/api/inbox", "http://localhost/",
                 "http://169.254.169.254/latest/meta-data/", "https://example.com:8443/", "http://[::1]/x",
                 "https://user:secret@example.com/", "http://intranet/", "http://203.0.113.9/"]


@pytest.mark.parametrize("url", REFUSED_LINKS)
def test_a_link_the_policy_refuses_spends_nothing_of_the_days_reviews_and_never_reaches_a_renderer(store, settings, url):
    f, llm = Fakes(), good_llm()
    r = run(f, llm, store, settings, text=f"<{url}|here>")
    (o,) = r.outcomes
    assert o.reason in {"not a public address", "port not allowed", "IPv6 addresses are not used", "the link has a password in it",
                        "not a public web address"}, o.reason
    assert r.line == f"Couldn't review {o.target.label}: {o.reason}" and r.reviewed == 0
    assert f.link_calls == [] and llm.calls == []
    assert store.get_cursor(svc.cap_key(settings, NOW)) is None  # not counted


def test_five_posts_with_two_refused_links_each_leave_the_whole_cap_for_the_posts_that_can_be_reviewed(store, settings):
    """The reviewed scenario: five posts with two http://10.0.0.1/ links each used to spend the day's 10 reviews."""
    f, llm = Fakes(), good_llm(3)
    for i in range(5):
        r = run(f, llm, store, settings, text="http://10.0.0.1/ and http://10.0.0.2/", iid=f"feedback-bad-{i}")
        assert [o.reason for o in r.outcomes] == ["not a public address"] * 2
    assert store.get_cursor(svc.cap_key(settings, NOW)) is None and f.link_calls == []
    assert run(f, llm, store, settings, text=LINK).reviewed == 1  # and the next real post is reviewed, with the cap intact
    assert store.get_cursor(svc.cap_key(settings, NOW)) == "1"


def test_a_refused_link_is_a_failed_step_on_the_live_page_but_not_a_review_and_a_good_link_next_to_it_is_reviewed(store, settings):
    f = Fakes()
    r = run(f, good_llm(), store, settings, text="http://10.0.0.1/ https://reach-poc.vercel.app")
    assert [o.ok for o in r.outcomes] == [False, True] and store.get_cursor(svc.cap_key(settings, NOW)) == "1"
    evs = [e for e in store.recent_events(10) if (e.job, e.step) == ("feedback", "site_review")]
    assert sorted((e.ok, e.kind) for e in evs) == [(False, "RenderFailed"), (True, None)]


def test_a_link_in_a_network_of_the_setting_is_refused_before_it_is_counted_too(store, settings):
    from dataclasses import replace
    s = replace(settings, site_review_refused_networks=("93.184.216.0/24",))
    f = Fakes()
    r = run(f, good_llm(), store, s, text="http://93.184.216.34/ https://reach-poc.vercel.app")
    assert [o.reason for o in r.outcomes] == ["not a public address", None] and f.link_calls[0][0].startswith("https://reach-poc")
    assert store.get_cursor(svc.cap_key(s, NOW)) == "1"


def test_a_link_that_passes_the_check_but_fails_in_the_render_still_counts(store, settings):
    """The count is for work started: only what could never be opened is free."""
    run(Fakes(link=RenderError("timed out")), good_llm(), store, settings)
    assert store.get_cursor(svc.cap_key(settings, NOW)) == "1"


def test_the_slot_is_taken_in_one_store_operation(store, settings, monkeypatch):
    calls = []
    real = store.update_cursor
    monkeypatch.setattr(store, "update_cursor", lambda key, fn: (calls.append(key), real(key, fn))[1])
    monkeypatch.setattr(store, "set_cursor", lambda *a: pytest.fail("a plain write of the counter is not atomic"))
    key = svc.cap_key(settings, NOW)
    assert svc.take_slot(store, key, 3) is True
    assert calls == [key] and store.get_cursor(key) == "1"


def test_the_last_slot_goes_to_one_taker_and_the_cap_is_never_passed(store, settings):
    key = svc.cap_key(settings, NOW)
    assert [svc.take_slot(store, key, 3) for _ in range(5)] == [True, True, True, False, False]
    assert store.get_cursor(key) == "3"
    store.set_cursor(key, "not a number")
    assert svc.take_slot(store, key, 3) is True and store.get_cursor(key) == "1"  # a counter nobody can read starts again
    assert svc.take_slot(store, "site_review_day:x", 0) is False and store.get_cursor("site_review_day:x") == "0"


def test_separate_processes_cannot_both_take_the_last_slot(tmp_path):
    """Four connections to the same database, as the tick and a dashboard draft are: 40 attempts for a cap of 10 take 10."""
    import threading
    path = tmp_path / "shared" / "agent.sqlite"
    Store(path).close()
    taken, errors = [], []

    def worker():
        try:
            st = Store(path)
            for _ in range(10):
                if svc.take_slot(st, "site_review_day:2026-10-21", 10):
                    taken.append(1)
            st.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == [] and len(taken) == 10
    st = Store(path)
    assert st.get_cursor("site_review_day:2026-10-21") == "10"


def test_a_run_that_loses_the_last_slot_to_another_between_the_look_and_the_take_reports_the_limit(store, settings, monkeypatch):
    key = svc.cap_key(settings, NOW)
    store.set_cursor(key, "9")
    real = store.update_cursor
    # the dashboard's draft takes the last slot after this run has looked at the counter and before it takes its own
    monkeypatch.setattr(store, "update_cursor", lambda k, fn: (store.set_cursor(k, "10"), real(k, fn))[1])
    f, llm = Fakes(), good_llm()
    r = run(f, llm, store, settings)
    assert r.line == "Couldn't review reach-poc.vercel.app: daily limit" and f.link_calls == [] and llm.calls == []
    assert store.get_cursor(key) == "10"


def test_the_cap_day_is_the_settings_days_not_utc(store, settings):
    just_after_midnight_kuwait = datetime(2026, 10, 21, 0, 20, tzinfo=KW)  # still the 20th in UTC
    assert svc.cap_key(settings, just_after_midnight_kuwait) == "site_review_day:2026-10-21"


def test_a_target_gets_at_most_45_seconds_and_a_post_at_most_90_of_rendering(store, settings):
    tick = [0.0]
    f = Fakes(clock=tick, cost=45.0)
    r = run(f, good_llm(3), store, settings, text="https://a.example.com https://b.example.com", files=[HTML])
    budgets = [kw["budget_s"] for _u, kw in f.link_calls] + [kw["budget_s"] for _t, kw in f.html_calls]
    assert budgets == [45.0, 45.0]  # each of the two that got a turn had its 45 s
    assert r.outcomes[2].reason == "ran out of time" and len(f.html_calls) == 0  # 90 s spent: none left for a third
    # a target is given what is left of the post's 90 s when that is less than 45
    tick2 = [0.0]
    g = Fakes(clock=tick2, cost=70.0)
    run(g, good_llm(3), store, settings, text="https://a.example.com https://b.example.com")
    assert [kw["budget_s"] for _u, kw in g.link_calls] == [45.0, 20.0]


def test_after_the_whole_review_budget_nothing_new_is_started(store, settings):
    tick = [0.0]
    f = Fakes(clock=tick, cost=60.0)
    r = run(f, good_llm(3), store, settings, text="https://a.example.com https://b.example.com", files=[HTML])
    assert [o.ok for o in r.outcomes][:1] == [True] and any(o.reason == "ran out of time" for o in r.outcomes)


def test_a_review_call_is_given_what_is_left_of_the_budget(store, settings):
    tick = [0.0]
    f, llm = Fakes(clock=tick, cost=148.0), good_llm()
    r = run(f, llm, store, settings)
    assert r.outcomes[0].reason == "ran out of time" and llm.calls == []  # 148 s of rendering left no time to ask
    tick2 = [0.0]
    g, llm2 = Fakes(clock=tick2, cost=100.0), good_llm()
    run(g, llm2, store, settings)
    assert 0 < llm2.calls[0]["timeout_s"] <= 40  # 50 s were left: a call is given the least of that and its own cap


# ---------------------------------------------------------------- storage

def test_the_screenshots_are_kept_per_item_and_named_by_target_and_size(store, settings, tmp_path):
    run(Fakes(), good_llm(2), store, settings, text=LINK, files=[HTML])
    folder = tmp_path / "data" / "site-reviews" / IID
    assert sorted(p.name for p in folder.iterdir()) == ["html2-desktop.jpg", "html2-phone.jpg", "link1-desktop.jpg",
                                                         "link1-phone.jpg"]
    assert (folder / "link1-desktop.jpg").read_bytes().startswith(b"\xff\xd8")


def test_screenshots_are_kept_even_when_the_review_after_them_fails(store, settings, tmp_path):
    run(Fakes(), FakeLLM("nope", "nope"), store, settings)
    assert (tmp_path / "data" / "site-reviews" / IID / "link1-phone.jpg").is_file()


def test_an_unsafe_item_id_keeps_no_screenshots_but_the_review_goes_on(store, settings, tmp_path):
    r = run(Fakes(), good_llm(), store, settings, iid="../../escape")
    assert r.reviewed == 1 and not (tmp_path / "data" / "site-reviews").exists() and not (tmp_path / "escape").exists()


def test_folders_older_than_seven_days_are_pruned_and_recent_ones_stay(store, settings, tmp_path):
    root = tmp_path / "data" / "site-reviews"
    for name, age in (("feedback-old", 8), ("feedback-edge", 6.9), ("feedback-new", 0)):
        d = root / name
        d.mkdir(parents=True)
        (d / "x.jpg").write_bytes(b"x")
        t = NOW.timestamp() - age * 86400
        os.utime(d, (t, t))
    outside = tmp_path / "outside"
    outside.mkdir()
    t = NOW.timestamp() - 30 * 86400
    os.utime(outside, (t, t))
    (root / "linked").symlink_to(outside)
    run(Fakes(), good_llm(), store, settings)
    assert not (root / "feedback-old").exists() and (root / "feedback-edge").is_dir() and (root / "feedback-new").is_dir()
    assert outside.is_dir()  # a link out of the folder is not followed
    assert svc.prune(tmp_path / "nowhere", NOW) == 0


# ---------------------------------------------------------------- what is logged and recorded

def test_the_logs_name_hosts_steps_and_timings_and_never_page_text(store, settings, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "SECRET-PAGE-WORDS-4242"
    f = Fakes(link=rendered(text=f"hello {secret}", title=f"{secret} title"))
    run(f, good_llm(), store, settings)
    run(Fakes(link=RenderError("timed out", "cdp")), good_llm(), store, settings)
    assert secret not in caplog.text
    assert "site review: link reach-poc.vercel.app: reviewed in" in caplog.text
    assert "site review: link reach-poc.vercel.app: timed out (cdp)" in caplog.text


def test_each_target_is_a_step_event_with_the_model_that_reviewed_it(store, settings):
    run(Fakes(link=RenderError("timed out")), good_llm(), store, settings, text=LINK, files=[HTML])
    evs = [e for e in store.recent_events(10) if (e.job, e.step) == ("feedback", "site_review")]
    assert len(evs) == 2
    ok = [e for e in evs if e.ok]
    bad = [e for e in evs if e.ok is False]
    assert len(ok) == 1 and ok[0].model == "model-a" and len(bad) == 1 and bad[0].kind == "RenderFailed"


def test_a_page_that_rendered_but_could_not_be_reviewed_records_a_review_failure(store, settings):
    r = run(Fakes(), FakeLLM("not json at all"), store, settings)
    assert r.reviewed == 0 and "Couldn't review reach-poc.vercel.app" in r.line
    (ev,) = [e for e in store.recent_events(10) if (e.job, e.step) == ("feedback", "site_review")]
    assert ev.ok is False and ev.kind == "ReviewFailed"


def test_the_line_is_one_line_and_capped(store, settings):
    f = Fakes(link=RenderError("x" * 400))
    r = run(f, good_llm(), store, settings)
    assert "\n" not in r.line and len(r.line) <= 240
