"""Run controls (2026-10-20): the frontend. Static checks read the files under dashboard/static. The render checks start
a real uvicorn on 127.0.0.1 against a tmp store, with recorders for what the controls start (`spawn`, `kick`: nothing
ever runs), log in as `dashboard open` does, and drive headless Chrome over CDP, as the Task 8 tests do. They are
skipped when Chrome is missing. Offline guard on; tmp data only."""
from __future__ import annotations

import fcntl
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from haseeb_agent import controls
from haseeb_agent.dashboard.app import STATIC_DIR
from haseeb_agent.models import Item, StoredMessage
from haseeb_agent.store import Store
from tests.test_dashboard_frontend import (  # noqa: F401 - `cdp` is a fixture
    CARD, CH9, HIST_READY, JS, _code, _contrast, _last_week, _rgb, _selectors, _surfaces, _ts_at, cdp, live_server,
    login_url, needs_chrome, seed_team, seed_two,
)

IQBAL, BEA, SARA = ("U0A3S7LUZ41", "Iqbal Qazi"), ("U0B2C3D4E5F", "Bea Lopez"), ("U0C3D4E5F6G", "Sara Ali")
HOSTILE = "<img src=x onerror=window.__xss=1> " + "\u202egnp.exe\u2066 javascript:alert(3)"
JOB_TITLES = ["Check round", "Force insights", "Build decks", "Weekly report", "Import team history",
              "Label topics now", "Check Slack account"]


# ---------------------------------------------------------------- helpers

def hold(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")  # noqa: SIM115 - held open: the caller closes it to release the lock
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def until(cond, timeout: float = 8.0, what: str = "the condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def tile(job: str, rest: str = "") -> str:
    return f"document.querySelector('.run-tile[data-job=\"{job}\"]'){rest}"


def go(job: str) -> str:
    return tile(job, ".querySelector('.run-actions button')")


def status(job: str) -> str:
    return tile(job, ".querySelector('.run-status')?.textContent")


def row(ts: str, rest: str = "") -> str:
    return f"document.querySelector('.team-row[data-ts=\"{ts}\"]'){rest}"


def put(store, settings, hours_ago: float, text: str, who, *, thread=None, kind="other", micro=100) -> str:
    """A channel message of the last week, in the configured channel, its author in people too. Returns its ts."""
    t = int(datetime.now(settings.tz).timestamp()) - int(hours_ago * 3600)
    ts = f"{t}.{micro:06d}"
    store.upsert_person(*who)
    store.upsert_message(StoredMessage(channel=settings.channel_id, ts=ts, thread_ts=thread, user_id=who[0],
                                       user_name=who[1], text=text, kind=kind,
                                       day=datetime.fromtimestamp(t, settings.tz).date(), topic=None))
    return ts


def feedback_item(store, settings, ts: str, status: str) -> None:
    now = datetime.now(settings.tz)
    store.upsert_item(Item(id=f"feedback-{ts}", kind="feedback", day=now.date(), status=status, body="draft", title="t",
                           dm_ts="1.1", source_ts=ts, score=None, models={}, attempts=0, edited=False, created_at=now,
                           acted_at=None))


def seed_runs(store, settings) -> None:
    """How each control last ended, and whose Slack the last check saw."""
    now = datetime.now(settings.tz)
    controls.record_last(store, "tick", now - timedelta(minutes=1), True, "4 job(s) ran")
    controls.record_last(store, "decks", now - timedelta(minutes=30), False, "DeckError")
    controls.record_last(store, "history_import", now - timedelta(minutes=20), True,
                         "0 new message(s), stopped: huddle window")
    controls.record_identity(store, settings.haseeb_id, True, now - timedelta(minutes=5), None)


def seed_posts(box: dict):
    """Eight teammate posts in the configured channel, one in each feedback state, and what must stay out."""
    def seed(store, settings) -> None:
        box["none"] = put(store, settings, 1, "Learned a lot this week. " * 6, IQBAL)
        box["dm"] = put(store, settings, 2, "Second post, its draft is in the DM", BEA)
        box["posted"] = put(store, settings, 3, "Third post, its feedback was posted", SARA)
        box["skipped"] = put(store, settings, 4, "Fourth post, its draft was skipped", IQBAL)
        box["failed"] = put(store, settings, 5, "Fifth post, its draft failed", BEA)
        box["hostile"] = put(store, settings, 6, HOSTILE + " " + "x" * 120, ("U0D4E5F6G7H", HOSTILE))
        box["seven"] = put(store, settings, 7, "Seventh post, behind Show all", SARA)
        box["eight"] = put(store, settings, 8, "Eighth post, behind Show all", IQBAL)
        put(store, settings, 1.5, "Haseeb's own post", (settings.haseeb_id, "Haseeb"))
        put(store, settings, 1.6, "a reply", BEA, thread=box["none"])
        put(store, settings, 1.7, "huddle notes", ("USLACKBOT", "Slackbot"), kind="huddle_notes")
        put(store, settings, 24 * 9, "too old", SARA)
        for state, ts in (("delivered", box["dm"]), ("posted", box["posted"]), ("skipped", box["skipped"]),
                          ("failed", box["failed"])):
            feedback_item(store, settings, ts, state)
    return seed


def both(*seeds):
    def seed(store, settings) -> None:
        for s in seeds:
            s(store, settings)
    return seed


# ---------------------------------------------------------------- static checks

def test_no_window_confirm_alert_or_prompt_anywhere():
    """A confirm is inline (the Decks page's Attach is the pattern), never a browser dialog."""
    for p in JS:
        assert not re.search(r"(?<![\w$.])(?:window\.)?(?:confirm|alert|prompt)\s*\(", _code(p.read_text())), p.name


def test_the_new_modules_reach_the_api_only_through_the_client():
    for name in ("jobs.js", "runs.js", "teamposts.js", "feedbackbtn.js"):
        code = _code((STATIC_DIR / name).read_text())
        assert not re.search(r"\bfetch\(", code) and "XMLHttpRequest" not in code, name
    runs = _code((STATIC_DIR / "jobs.js").read_text())
    assert '"/api/run"' in runs and '"/api/pause"' in runs and '"/api/jobs"' in runs  # the three calls, nothing else


def test_run_controls_css_is_scoped_to_its_own_names():
    """The new rules sit under names of their own, and redefine nothing the other pages share."""
    css = (STATIC_DIR / "app.css").read_text()
    start, end = "/* ---------- run controls (Task 15)", "/* ---------- history (Task 9) */"
    sels = _selectors(css[css.index(start):css.index(end)])
    own = re.compile(r"^(?:\.pause-btn|\.is-expired \.pause-btn|\.pill\.paused|\.paused-banner|\.pb-|\.grid > \.runs|"
                     r"\.run-|\.inbox-main|\.team|\.fbk)")
    loose = [x for x in sels if not own.match(x)]
    assert len(sels) > 60 and not loose, loose


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_run_controls_text_colours_meet_aa_where_they_sit(theme):
    """The colours the new controls use for text, over the surfaces they really sit on: on a card, a sunken tile, a
    tinted strip, the header's glass, and the translucent chip on a team post's row."""
    t, over, card, sunken = _surfaces(theme)
    bg = _rgb(t["--bg"])[0]
    glass = over("--panel", bg)  # the header: a pill or the Pause button over the page
    chip = over("--panel2", sunken)  # a state chip on a team post's row
    pairs = [
        ("status lines on a tile", ("--ok", "--bad", "--warn", "--text2", "--muted"), (sunken,)),
        ("the identity strip", ("--ok", "--bad", "--warn"), None),
        ("a draft button", ("--a2-text",), None),
        ("a busy draft button", ("--text2",), None),
        ("a state chip", ("--text2",), (chip,)),
        ("the header's Pause button", ("--text2", "--warn"), (glass,)),
        ("the Paused pill", ("--warn",), None),
        ("a team post", ("--text", "--text2", "--muted", "--a2-text"), (sunken,)),
    ]
    tints = {"the identity strip": {"--ok": "--ok-soft", "--bad": "--bad-soft", "--warn": "--warn-soft"},
             "a draft button": {"--a2-text": "--a2-soft"}, "a busy draft button": {"--text2": "--a1-soft"},
             "the Paused pill": {"--warn": "--warn-soft"}}
    for name, tokens, surfaces in pairs:
        for token in tokens:
            fg = _rgb(t[token])[0]
            if surfaces is not None:
                on = surfaces
            else:
                tint = tints[name][token]
                on = (over(tint, card), over(tint, sunken), over(tint, glass if "pill" in name else card))
            for surface in on:
                assert _contrast(fg, surface) >= 4.5, (theme, name, token)
    # the Pause button when it is on: warn on its own tint over the glass
    assert _contrast(_rgb(t["--warn"])[0], over("--warn-soft", glass)) >= 4.5


# ---------------------------------------------------------------- the Run card

@needs_chrome
def test_the_run_card_lists_the_seven_controls_and_how_each_last_ended(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('.run-tile').length === 7")
        assert cdp.js("[...document.querySelectorAll('.run-tile .run-t')].map(e => e.textContent)") == JOB_TITLES
        cdp.wait(tile("tick", ".querySelector('.run-status')?.textContent.startsWith('Last run')"))
        when = r"(?:[A-Z][a-z]{2} )?\d\d:\d\d"
        assert re.fullmatch(rf"Last run {when} · ok · 4 job\(s\) ran", cdp.js(status("tick")))
        assert re.fullmatch(rf"Last run {when} · failed · DeckError", cdp.js(status("decks")))
        assert re.fullmatch(rf"Last run {when} · ok · 0 new message\(s\), stopped: huddle window",
                            cdp.js(status("history_import")))
        for job in ("insights", "weekly", "enrich", "slack_check"):
            assert cdp.js(status(job)) == "Not run yet."
        # each tile has one button, enabled, named by its tile; the card says what the last Slack check saw
        assert cdp.js("[...document.querySelectorAll('.run-tile')].every(t => t.querySelectorAll('button').length === 1"
                      " && !t.querySelector('button').disabled)")
        assert cdp.js("document.querySelector('.run-tile .run-actions button').getAttribute('aria-labelledby')"
                      " === 'run-b-tick run-t-tick'")
        assert re.fullmatch(r"Slack: posting as you ✓ \(checked \d\d:\d\d\)", cdp.js(
            "document.querySelector('.run-ident').textContent"))
        assert cdp.js("document.querySelector('.run-ident').classList.contains('is-ok')")
        # not paused: the banner is hidden, the header offers Pause
        assert cdp.js("document.querySelector('.paused-banner').hidden") is True
        cdp.wait("!document.getElementById('pause-btn').hidden")
        assert cdp.js("document.querySelector('#pause-btn .pause-lbl').textContent") == "Pause"
        assert cdp.js("document.getElementById('pause-btn').hasAttribute('aria-pressed')") is False  # the label says it
        assert cdp.js("!!document.querySelector('.pill.paused')") is False
        cdp.close()


@needs_chrome
def test_check_round_kicks_the_tick_once_and_shows_it_running(tmp_path, cdp):
    kicks, spawned = [], []
    with live_server(tmp_path, seed_runs, stream_polls=200, kick=lambda: kicks.append(1),
                     spawn=spawned.append) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(tile("tick", ".querySelector('.run-status')?.textContent.startsWith('Last run')"))
        cdp.js(f"{go('tick')}.click(); {go('tick')}.click()")  # a double click
        cdp.wait(tile("tick", ".querySelector('.run-status')?.textContent.startsWith('Running')"))
        assert cdp.js(f"{go('tick')}.disabled") is True
        assert cdp.js(tile("tick", ".getAttribute('aria-busy')")) == "true"
        time.sleep(0.5)
        assert kicks == [1] and spawned == []  # one kick, and the tick is kicked, never spawned
        cdp.close()


@needs_chrome
def test_check_round_says_so_when_launchd_did_not_start_the_tick_and_does_not_show_running(tmp_path, cdp):
    """M3: launchctl exits non-zero when it did not start the tick (it is not loaded in launchd, say). The tile says so
    and never reads "Running since…"."""
    with live_server(tmp_path, seed_runs, stream_polls=200, kick=lambda: 113) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(tile("tick", ".querySelector('.run-status')?.textContent.startsWith('Last run')"))
        cdp.js(f"{go('tick')}.click()")
        cdp.wait(tile("tick", ".querySelector('.run-status')?.textContent === \"Launchd did not start the tick.\""))
        time.sleep(2.5)  # the jobs state is looked at again: it is still not running
        assert not cdp.js(status("tick")).startswith("Running")
        assert cdp.js(f"{go('tick')}.disabled") is False
        cdp.close()


@needs_chrome
def test_force_insights_asks_first_inline_and_starts_one_forced_run(tmp_path, cdp):
    spawned = []
    with live_server(tmp_path, seed_runs, stream_polls=200, spawn=spawned.append) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(tile("insights", ".querySelector('.run-status')?.textContent === 'Not run yet.'"))
        cdp.js(f"{go('insights')}.click()")
        assert cdp.wait(tile("insights", ".querySelector('.confirm-q')?.textContent")) == (
            "Research new topics and send up to 3 more insights to your DM?")
        assert cdp.wait("document.activeElement.textContent.trim() === 'Cancel'")  # Enter lands on Cancel
        assert spawned == []
        # Escape puts it back, with the focus on the control
        cdp.js("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        cdp.wait(tile("insights", ".querySelector('.confirm-q') === null"))
        assert cdp.js(f"document.activeElement === {tile('insights')}")
        # Cancel does nothing either
        cdp.js(f"{go('insights')}.click()")
        cdp.wait(tile("insights", ".querySelector('.confirm-q')"))
        cdp.js(f"[...{tile('insights')}.querySelectorAll('button')].find(b => b.textContent.trim() === 'Cancel').click()")
        cdp.wait(tile("insights", ".querySelector('.confirm-q') === null"))
        assert spawned == []
        # Confirm starts exactly one run, however often it is clicked
        cdp.js(f"{go('insights')}.click()")
        cdp.wait(tile("insights", ".querySelector('.confirm-q')"))
        cdp.js(f"{{ const b = [...{tile('insights')}.querySelectorAll('button')].find(b => b.textContent.trim() === 'Confirm');"
               " b.click(); b.click(); }")
        until(lambda: spawned, what="the insights spawn")
        cdp.wait(tile("insights", ".querySelector('.run-status')?.textContent.startsWith('Running')"))
        time.sleep(0.4)
        assert spawned == [["insights", "--force"]]
        cdp.close()


@needs_chrome
def test_every_control_starts_its_own_fixed_command(tmp_path, cdp):
    kicks, spawned = [], []
    with live_server(tmp_path, seed_runs, stream_polls=200, kick=lambda: kicks.append(1),
                     spawn=spawned.append) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('.run-tile').length === 7")
        want = []
        for job, args in (("weekly", ["weekly"]), ("decks", ["decks", "--retry"]),
                          ("history_import", ["history-import"]), ("enrich", ["history-enrich"]),
                          ("slack_check", ["slack-check"])):
            cdp.js(f"{go(job)}.click()")
            want.append(args)
            until(lambda: spawned == want, what=f"the {job} spawn")
        cdp.js(f"{go('tick')}.click()")
        until(lambda: kicks == [1], what="the kick")
        assert spawned == want
        # all of them show as running (a recorded spawn is a running job until it ends)
        cdp.wait("[...document.querySelectorAll('.run-tile')].filter(t => t.classList.contains('is-running')).length === 6")
        cdp.close()


@needs_chrome
def test_a_running_job_comes_from_its_lock_and_ends_when_the_lock_is_released(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200) as (base, settings, login):
        lock = hold(Path(settings.db_path).parent / "decks.lock")
        try:
            cdp.open(login_url(base, login))
            cdp.wait(tile("decks", ".querySelector('.run-status')?.textContent.startsWith('Running')"))
            assert cdp.js(f"{go('decks')}.disabled") is True
            assert cdp.js(status("weekly")) == "Not run yet." and cdp.js(f"{go('weekly')}.disabled") is False
        finally:
            lock.close()
        # the page looks again every 2 s while something runs
        cdp.wait(tile("decks", ".querySelector('.run-status')?.textContent.startsWith('Last run')"), timeout=15)
        assert cdp.js(f"{go('decks')}.disabled") is False
        cdp.close()


@needs_chrome
def test_a_running_import_says_the_regular_checks_wait(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200) as (base, settings, login):
        data = Path(settings.db_path).parent
        locks = [hold(data / "history_import.lock"), hold(data / "tick.lock")]  # the import holds both
        try:
            cdp.open(login_url(base, login))
            cdp.wait(tile("history_import", ".querySelector('.run-status')?.textContent.startsWith('Running')"))
            assert "The regular checks wait." in cdp.js(status("history_import"))
            assert cdp.js(status("tick")) == "The history import holds the tick lock; the regular checks wait."
            assert cdp.js(f"{go('tick')}.disabled") is True
        finally:
            for f in locks:
                f.close()
        cdp.close()


@needs_chrome
def test_a_refused_start_says_why_in_the_tile(tmp_path, cdp):
    """Another tab started it first: the server answers 409 and the tile says so, then settles."""
    spawned = []
    with live_server(tmp_path, seed_runs, stream_polls=200, spawn=spawned.append) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(tile("weekly", ".querySelector('.run-status')?.textContent === 'Not run yet.'"))
        st = Store(settings.db_path)
        controls.mark_spawned(st, "weekly", datetime.now(settings.tz))  # started elsewhere, before this page looked
        st.close()
        cdp.js(f"{go('weekly')}.click()")
        cdp.wait(tile("weekly", ".querySelector('.run-status')?.textContent === 'Already running.'"))
        assert spawned == []
        cdp.close()


# ---------------------------------------------------------------- Pause

@needs_chrome
def test_pause_from_the_header_stops_the_controls_on_every_page_until_resume(tmp_path, cdp):
    spawned = []
    with live_server(tmp_path, both(seed_runs, seed_two), stream_polls=200, spawn=spawned.append
                     ) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("!document.getElementById('pause-btn').hidden && document.querySelector('.icard')")
        cdp.js("document.getElementById('pause-btn').click()")
        cdp.wait("document.querySelector('#pause-btn .pause-lbl').textContent === 'Resume'")
        st = Store(settings.db_path)
        since = controls.paused_since(st)
        st.close()
        assert since  # the server holds it: the cursor agent_paused is the time it was paused
        # the pill, the banner, the controls
        assert re.fullmatch(r"Paused since \d\d:\d\d", cdp.js("document.querySelector('.pill.paused')?.textContent"))
        assert cdp.js("document.querySelector('.pill.paused') === document.querySelector('.pills').firstElementChild")
        cdp.wait("!document.querySelector('.paused-banner').hidden")
        assert re.fullmatch(r"Paused since \d\d:\d\d\. Nothing is sent to Slack and no job runs\.",
                            cdp.js("document.querySelector('.paused-banner .pb-text').textContent"))
        assert cdp.js("document.getElementById('pause-btn').classList.contains('on')")
        off = dict(cdp.js("[...document.querySelectorAll('.run-tile')].map(t => [t.dataset.job, "
                          "t.querySelector('.run-actions button').disabled])"))
        assert off == {"tick": True, "insights": True, "decks": True, "weekly": True, "history_import": True,
                       "enrich": True, "slack_check": False}  # only the read-only Slack check still runs
        assert cdp.js(status("weekly")) == "Paused: resume to run this."
        assert cdp.js("[...document.querySelectorAll('.icard .actions button')].every(b => b.disabled)")
        # a click on a disabled control starts nothing
        cdp.js(f"{go('weekly')}.click(); {go('insights')}.click()")
        time.sleep(0.4)
        assert spawned == []
        # the Slack check still starts
        cdp.js(f"{go('slack_check')}.click()")
        until(lambda: spawned == [["slack-check"]], what="the Slack check")
        # a reload keeps it (the server holds it), and so does every other page
        cdp.reload()
        cdp.wait("document.querySelector('#pause-btn .pause-lbl')?.textContent === 'Resume'")
        cdp.js("location.hash = '#/decks'")
        cdp.wait("!!document.querySelector('.page-decks')")
        assert cdp.js("document.querySelector('#pause-btn .pause-lbl').textContent") == "Resume"
        assert cdp.js("!!document.querySelector('.pill.paused')") is True
        # Resume from the banner on the Live page
        cdp.js("location.hash = '#/live'")
        cdp.wait("!document.querySelector('.paused-banner')?.hidden")
        cdp.js("document.querySelector('.paused-banner button').click()")
        cdp.wait("document.querySelector('.paused-banner').hidden && "
                 "document.querySelector('#pause-btn .pause-lbl').textContent === 'Pause'")
        st = Store(settings.db_path)
        assert controls.paused_since(st) is None
        st.close()
        assert cdp.js("!!document.querySelector('.pill.paused')") is False
        # every control is back, but the Slack check this test started: nothing ever ends that "run" but its record
        cdp.wait("[...document.querySelectorAll('.run-tile')].filter(t => t.dataset.job !== 'slack_check')"
                 ".every(t => !t.querySelector('.run-actions button').disabled)")
        assert cdp.js("[...document.querySelectorAll('.icard .actions button')].every(b => !b.disabled)")
        cdp.close()


@needs_chrome
def test_a_paused_agent_shows_paused_in_place_of_the_autopost_countdown(tmp_path, cdp):
    """M2: the paused tick cancels the auto-post and nothing posts while paused, so a ticking "Auto-posts in 9:58" would
    be a promise nobody keeps. The chip says Paused, and the countdown is back after Resume (while the cursor stands)."""
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        chip = f"{CARD.format(kind='Next Todo')}.querySelector('.auto-chip')"
        cdp.wait(f"{chip}?.textContent.startsWith('Auto-posts in ')")
        cdp.wait("!document.getElementById('pause-btn').hidden")
        cdp.js("document.getElementById('pause-btn').click()")
        cdp.wait(f"{chip}.textContent === 'Paused'")
        assert cdp.js(f"{chip}.title") == "The agent is paused, so the auto-post is cancelled."
        time.sleep(1.2)  # the per-second tick does not bring the countdown back
        assert cdp.js(f"{chip}.textContent") == "Paused"
        cdp.js("document.getElementById('pause-btn').click()")  # Resume
        cdp.wait(f"{chip}.textContent.startsWith('Auto-posts in ')")
        assert cdp.js(f"{chip}.hasAttribute('title')") is False
        cdp.close()


@needs_chrome
def test_a_paused_click_on_a_draft_says_so_in_words(tmp_path, cdp):
    """A card that was open in another tab when the agent was paused: its click is refused with 409 and the card says
    what to do, not the server's one word."""
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelector('.icard .actions button')")
        # pause behind the page's back and click before the page has looked
        st = Store(settings.db_path)
        controls.set_paused(st, True, datetime.now(settings.tz))
        st.close()
        cdp.js("[...document.querySelectorAll('.icard')].find(c => c.textContent.includes('Feedback for Sara'))"
               ".querySelector('.actions button:nth-child(3)').click()")  # Shorter
        cdp.wait("[...document.querySelectorAll('.cstatus.s-failed')].some(s => s.textContent.includes("
                 "'Paused: resume the agent first'))")
        cdp.close()


@needs_chrome
def test_a_pause_made_elsewhere_shows_up_on_its_own(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("!document.getElementById('pause-btn').hidden")
        st = Store(settings.db_path)
        controls.set_paused(st, True, datetime.now(settings.tz))  # another tab, or the CLI
        st.close()
        cdp.wait("document.querySelector('#pause-btn .pause-lbl').textContent === 'Resume' "
                 "&& !document.querySelector('.paused-banner').hidden", timeout=16)
        cdp.close()


@needs_chrome
def test_the_pause_button_is_gone_when_the_session_has_expired(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200, wrong_token=True) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.body.classList.contains('is-expired')")
        assert cdp.js("document.getElementById('pause-btn').hidden") is True
        assert cdp.js("getComputedStyle(document.getElementById('pause-btn')).display") == "none"
        cdp.close()


# ---------------------------------------------------------------- which Slack account

@needs_chrome
def test_the_slack_account_line_says_whose_it_is(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelector('.run-ident.is-ok')")
        now = datetime.now(settings.tz)

        def say(user_id, ok, error, want):
            st = Store(settings.db_path)
            controls.record_identity(st, user_id, ok, now - timedelta(minutes=2), error)
            st.close()
            cdp.reload()
            cdp.wait(f"document.querySelector('.run-ident.{want}')")  # the page paints twice: before and after /api/jobs
            return cdp.js("[document.querySelector('.run-ident').className, document.querySelector('.run-ident').textContent]")

        cls, text = say("U0OTHER12345", False, None, "is-bad")
        assert cls == "run-ident is-bad"
        assert re.fullmatch(r"Slack: posting as another account \(U0OTHER12345\) ✗ \(checked \d\d:\d\d\)\. "
                            r"Pause the agent and fix the connector\.", text)
        cls, text = say(None, False, "the profile has no user id", "is-warn")
        assert cls == "run-ident is-warn"
        assert re.fullmatch(r"Slack: couldn't check — the profile has no user id \(checked \d\d:\d\d\)\.", text)
        st = Store(settings.db_path)
        st.set_cursor("slack_identity", "")
        st.close()
        cdp.reload()
        cdp.wait("document.querySelector('.run-ident.is-none')")
        cdp.wait("document.querySelector('.run-status')?.textContent !== ''")  # the jobs answer is in
        assert cdp.js("document.querySelector('.run-ident').className") == "run-ident is-none"
        assert cdp.js("document.querySelector('.run-ident').textContent").startswith("Slack: not checked yet.")
        cdp.close()


@needs_chrome
def test_a_green_check_that_is_too_old_to_trust_goes_amber_and_says_check_again(tmp_path, cdp):
    """M4: a green from before a Claude-account switch must not stay green. Older than 6 hours, or older than the
    dashboard process, the strip is amber with "check again". A red one stays red."""
    with live_server(tmp_path, seed_runs, stream_polls=200, started_at=time.time() - 86400) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelector('.run-ident.is-ok')")  # 5 minutes old: fresh
        now = datetime.now(settings.tz)

        def say(user_id, ok, at, want):
            st = Store(settings.db_path)
            controls.record_identity(st, user_id, ok, at, None)
            st.close()
            cdp.reload()
            cdp.wait(f"document.querySelector('.run-ident.{want}')")
            return cdp.js("document.querySelector('.run-ident').textContent")

        text = say(settings.haseeb_id, True, now - timedelta(hours=7), "is-warn")
        assert re.fullmatch(r"Slack: the last check is old \(checked (?:[A-Z][a-z]{2} )?\d\d:\d\d\) — check again\.", text)
        assert "✓" not in text
        assert re.fullmatch(r"Slack: posting as you ✓ \(checked \d\d:\d\d\)",
                            say(settings.haseeb_id, True, now - timedelta(hours=5), "is-ok"))
        text = say("U0OTHER12345", False, now - timedelta(hours=9), "is-bad")  # old, but a warning stays a warning
        assert text.startswith("Slack: posting as another account (U0OTHER12345) ✗")
        cdp.close()


@needs_chrome
def test_a_check_from_before_the_dashboard_started_is_old_too(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200, started_at=time.time()) as (base, settings, login):
        cdp.open(login_url(base, login))  # seed_runs checked 5 minutes ago, before this server started
        cdp.wait("document.querySelector('.run-ident.is-warn')")
        assert "check again" in cdp.js("document.querySelector('.run-ident').textContent")
        cdp.close()


@needs_chrome
def test_an_identity_from_the_server_is_text_never_markup(tmp_path, cdp):
    def seed(store, settings):
        controls.record_identity(store, None, False, datetime.now(settings.tz), HOSTILE)
        controls.record_last(store, "weekly", datetime.now(settings.tz), False, HOSTILE)
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelector('.run-ident.is-warn')")
        cdp.wait(tile("weekly", ".querySelector('.run-status')?.textContent.startsWith('Last run')"))
        assert cdp.js("document.querySelectorAll('.runs img, .runs svg, .runs script').length") == 0
        assert cdp.js("window.__xss === undefined")
        assert HOSTILE.split()[0] in cdp.js("document.querySelector('.run-ident').textContent")  # shown, as text
        assert cdp.js("[...document.querySelectorAll('.runs *')].every(e => [...e.attributes].every(a => !/[<>]/.test(a.value)))")
        assert cdp.js("[...document.querySelectorAll('.runs .run-status bdi, .runs .run-ident bdi')].length >= 2")
        cdp.close()


# ---------------------------------------------------------------- Team posts

@needs_chrome
def test_team_posts_are_the_teammates_top_level_posts_each_with_its_state(tmp_path, cdp):
    box: dict = {}
    with live_server(tmp_path, seed_posts(box), stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('.team-row').length > 0")
        assert cdp.js("document.querySelector('.team-h').textContent") == "Team posts (last 7 days)"
        assert cdp.js("document.querySelector('.team').parentElement === document.querySelector('.inbox')"
                      ".querySelector('.card-b')")  # under the drafts of "Waiting for you"
        # the newest six, then "Show all 8"; own posts, replies, Slackbot and week-old posts are not there
        assert cdp.js("[...document.querySelectorAll('.team-row')].map(r => r.dataset.ts)") == [
            box["none"], box["dm"], box["posted"], box["skipped"], box["failed"], box["hostile"]]
        assert cdp.js("document.querySelector('.team-count').textContent") == "8 · 4 without a draft"
        more = "document.querySelector('.team-more')"
        assert cdp.js(f"{more}.textContent") == "Show all 8" and cdp.js(f"{more}.getAttribute('aria-expanded')") == "false"
        # each row: who, when, the opening of the post, a checked Slack link, and its state
        assert cdp.js(row(box["none"], ".querySelector('.who').textContent")) == "Iqbal Qazi"
        assert cdp.js(row(box["none"], ".querySelector('.team-av').textContent")) == "IQ"
        assert cdp.js(row(box["none"], ".querySelector('.team-text').textContent")).startswith("Learned a lot this week.")
        link = row(box["none"], ".querySelector('a.slack-link')")
        assert cdp.js(f"{link}.getAttribute('href')") == (
            f"{settings.workspace_url}/archives/{settings.channel_id}/p{box['none'].replace('.', '')}")
        assert cdp.js(f"{link}.rel") == "noopener noreferrer" and cdp.js(f"{link}.target") == "_blank"
        btn = lambda k: row(box[k], ".querySelector('.fbk-btn')")  # noqa: E731
        chip = lambda k: row(box[k], ".querySelector('.fbk-chip')")  # noqa: E731
        assert cdp.js(f"{btn('none')}.hidden") is False and cdp.js(f"{btn('none')}.textContent") == "Draft feedback"
        assert cdp.js(f"{btn('none')}.disabled") is False
        for key, words in (("dm", "Draft in your DM"), ("posted", "Feedback posted"), ("skipped", "Draft skipped"),
                           ("failed", "Draft failed")):
            assert cdp.js(f"{btn(key)}.hidden") is True and cdp.js(f"{chip(key)}.hidden") is False, key
            assert cdp.js(f"{chip(key)}.textContent") == words, key
        # "Show all 8" shows the other two, "Show fewer" takes them away
        cdp.js(f"{more}.click()")
        cdp.wait("document.querySelectorAll('.team-row').length === 8")
        assert cdp.js(f"{more}.textContent") == "Show fewer" and cdp.js(f"{more}.getAttribute('aria-expanded')") == "true"
        cdp.js(f"{more}.click()")
        cdp.wait("document.querySelectorAll('.team-row').length === 6")
        cdp.close()


@needs_chrome
def test_team_post_text_is_text_and_a_hostile_name_is_inert(tmp_path, cdp):
    box: dict = {}
    with live_server(tmp_path, seed_posts(box), stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(row(box["hostile"]))
        assert cdp.js("window.__xss === undefined")
        assert cdp.js("document.querySelectorAll('.team img, .team script, .team svg').length") == 0
        assert cdp.js(row(box["hostile"], ".querySelector('.who bdi').textContent")) == HOSTILE  # the name, whole, as text
        assert cdp.js(row(box["hostile"], ".querySelector('.team-text bdi').textContent")).startswith(
            "<img src=x onerror=window.__xss=1>")
        assert cdp.js("[...document.querySelectorAll('.team *')].every(e => [...e.attributes].every(a => !/[<>\\u202e\\u2066]/.test(a.value)))")
        assert cdp.js("[...document.querySelectorAll('.team .who, .team .team-text')].every(e => e.querySelector('bdi'))")
        cdp.close()


@needs_chrome
def test_draft_feedback_from_the_list_starts_one_run_and_follows_it_to_the_end(tmp_path, cdp):
    box: dict = {}
    spawned = []
    with live_server(tmp_path, seed_posts(box), stream_polls=200, spawn=spawned.append) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(row(box["none"], ".querySelector('.fbk-btn')"))
        cdp.js(row(box["none"], ".querySelector('.fbk-btn')") + ".click()")
        until(lambda: spawned, what="the feedback spawn")
        assert spawned == [["feedback", "--ts", box["none"]]]  # the post's ts, and nothing else of the page's
        # that post shows it is drafting; no other draft can start meanwhile
        cdp.wait(row(box["none"], ".querySelector('.fbk-btn').getAttribute('aria-busy') === 'true'"))
        assert cdp.js(row(box["none"], ".querySelector('.fbk-btn').textContent")) == "Drafting…"
        cdp.wait("[...document.querySelectorAll('.team-row .fbk-btn')].filter(b => !b.hidden && b.getAttribute('aria-busy') !== 'true')"
                 ".every(b => b.disabled && b.title === 'Another draft is running')")
        # the job ends: its item is in the DM and the run is recorded; the list looks again by itself
        st = Store(settings.db_path)
        feedback_item(st, settings, box["none"], "delivered")
        controls.record_last(st, "feedback", datetime.now(settings.tz) + timedelta(seconds=1), True,
                             "draft sent to your DM")
        st.close()
        cdp.wait(row(box["none"], ".querySelector('.fbk-chip:not([hidden])')?.textContent === 'Draft in your DM'"),
                 timeout=15)
        assert cdp.js(row(box["none"], ".querySelector('.fbk-btn').hidden")) is True
        assert cdp.js("document.querySelector('.team-count').textContent") == "8 · 3 without a draft"
        cdp.close()


@needs_chrome
def test_a_post_the_tick_is_drafting_shows_drafting_and_offers_no_second_draft(tmp_path, cdp):
    """C1: the tick's own scan holds a claim on the post it drafts. The list shows that post as being drafted (the
    control is the disabled "Drafting…", not "Draft feedback"), a click on it starts nothing, and once the draft is in
    the DM and the claim is given back, the post shows the draft's state."""
    box: dict = {}
    spawned = []

    def claimed(store, settings) -> None:
        store.set_cursor(f"feedback_claim:{box['none']}", datetime.now(settings.tz).isoformat())

    with live_server(tmp_path, both(seed_posts(box), claimed), stream_polls=200, spawn=spawned.append
                     ) as (base, settings, login):
        cdp.open(login_url(base, login))
        btn = row(box["none"], ".querySelector('.fbk-btn')")
        cdp.wait(f"{btn} && {btn}.getAttribute('aria-busy') === 'true'")
        assert cdp.js(f"{btn}.textContent") == "Drafting…" and cdp.js(f"{btn}.disabled") is True
        assert "Draft feedback" not in cdp.js(row(box["none"], ".textContent"))
        assert cdp.js("document.querySelector('.team-count').textContent") == "8 · 3 without a draft"  # it is not "none"
        cdp.js(f"{btn}.click()")
        time.sleep(0.4)
        assert spawned == []
        st = Store(settings.db_path)  # the tick's draft reaches the DM and the claim is given back
        feedback_item(st, settings, box["none"], "delivered")
        st.set_cursor(f"feedback_claim:{box['none']}", "")
        st.close()
        cdp.reload()
        cdp.wait(row(box["none"], ".querySelector('.fbk-chip:not([hidden])')?.textContent === 'Draft in your DM'"))
        assert cdp.js(row(box["none"], ".querySelector('.fbk-btn').hidden")) is True
        cdp.close()


@needs_chrome
def test_a_post_that_got_its_draft_elsewhere_shows_its_state_after_the_click(tmp_path, cdp):
    box: dict = {}
    with live_server(tmp_path, seed_posts(box), stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(row(box["none"], ".querySelector('.fbk-btn')"))
        st = Store(settings.db_path)  # the post gets its draft elsewhere, after this page last looked
        feedback_item(st, settings, box["none"], "delivered")
        st.close()
        cdp.js(row(box["none"], ".querySelector('.fbk-btn')") + ".click()")
        # the server says it already has one (409): the control takes the state it names, and starts nothing
        cdp.wait(row(box["none"], ".querySelector('.fbk-chip:not([hidden])')?.textContent === 'Draft in your DM'"))
        assert cdp.js(row(box["none"], ".querySelector('.fbk-btn').hidden")) is True
        cdp.close()


@needs_chrome
def test_nothing_in_the_list_when_there_are_no_posts_yet(tmp_path, cdp):
    with live_server(tmp_path, seed_runs, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelector('.team-empty')")
        assert "No teammate posts in the last 7 days yet." in cdp.js("document.querySelector('.team-empty').textContent")
        assert cdp.js("document.querySelector('.team-count').textContent") == ""
        cdp.close()


# ---------------------------------------------------------------- History

@needs_chrome
def test_history_offers_the_draft_on_teammates_top_level_posts_only(tmp_path, cdp):
    spawned = []
    with live_server(tmp_path, seed_team, stream_polls=200, patch={"channel_id": CH9},
                     spawn=spawned.append) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("!!document.querySelector('.page-live')")
        cdp.js("location.hash = '#/history'")
        cdp.wait(HIST_READY)
        cdp.wait("!!document.querySelector('.fbk-btn')")
        # Ann's insight, Bob's lunch note, Ann's Next Todo, Bob's and Ann's Monday posts: five posts, five buttons
        assert cdp.js("document.querySelectorAll('.fbk-btn').length") == 5
        # none on Haseeb's own items, on replies (the feedback under an insight) or on the huddle notes
        assert cdp.js("document.querySelectorAll('.item.mine .fbk-btn, .fb-row .fbk-btn, .note-item .fbk-btn').length") == 0
        # the button is icon-only here, named for screen readers and in its tooltip
        assert cdp.js("document.querySelector('.ins .fbk-btn').textContent") == "Draft feedback"
        assert cdp.js("document.querySelector('.ins .fbk-btn .sr-only').textContent") == "Draft feedback"
        assert cdp.js("document.querySelector('.ins .fbk-btn').title") == "Draft feedback"
        wed = _last_week(settings) + timedelta(days=2)
        ins_ann = _ts_at(settings, wed, (9, 0))
        cdp.js("document.querySelector('.ins:not(.mine) .fbk-btn').click()")
        until(lambda: spawned, what="the feedback spawn")
        assert spawned == [["feedback", "--ts", ins_ann]]
        cdp.wait("document.querySelector('.ins:not(.mine) .fbk-btn').getAttribute('aria-busy') === 'true'")
        cdp.close()


@needs_chrome
def test_history_shows_a_post_that_has_a_draft_without_the_button(tmp_path, cdp):
    def seed(store, settings):
        seed_team(store, settings)
        wed = _last_week(settings) + timedelta(days=2)
        feedback_item(store, settings, _ts_at(settings, wed, (9, 0)), "posted")
    with live_server(tmp_path, seed, stream_polls=200, patch={"channel_id": CH9}) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("!!document.querySelector('.page-live')")
        cdp.js("location.hash = '#/history'")
        cdp.wait(HIST_READY)
        cdp.wait("!!document.querySelector('.fbk-chip:not([hidden])')")
        assert cdp.js("document.querySelectorAll('.fbk-btn:not([hidden])').length") == 4
        assert cdp.js("document.querySelector('.fbk-chip:not([hidden])').title") == "Feedback posted"
        cdp.close()


# ---------------------------------------------------------------- both themes, phone width, reduced motion

CONTRAST_JS = r"""((selectors) => {
  const parse = (s) => {
    let m = /^rgba?\(([^)]+)\)$/.exec(s);
    if (m) { const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    m = /^color\(srgb ([^)]+)\)$/.exec(s);
    if (m) { const p = m[1].split(/[\s\/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1]; }
    throw new Error("unparsed colour " + s);
  };
  const over = (t, u) => [0, 1, 2].map((i) => t[3] * t[i] + (1 - t[3]) * u[i]);
  const lum = (c) => { const f = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
                       return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const behind = (el) => { const chain = []; for (let n = el; n; n = n.parentElement) chain.push(getComputedStyle(n).backgroundColor);
    let base = [255, 255, 255]; for (const c of chain.reverse()) { const p = parse(c); if (p[3] > 0) base = over(p, base); } return base; };
  const out = {};
  for (const sel of selectors) {
    const els = [...document.querySelectorAll(sel)].filter((e) => !e.disabled && e.offsetParent !== null && e.textContent.trim());
    if (!els.length) { out[sel] = null; continue; }
    out[sel] = Math.min(...els.map((e) => { const bg = behind(e); return ratio(over(parse(getComputedStyle(e).color), bg), bg); }));
  }
  return out; })"""

CONTRAST_SELECTORS = [".paused-banner .pb-text", ".run-ident", ".run-status.s-ok", ".run-status.s-bad", ".run-status.s-none",
                      ".run-status.s-paused", ".run-status.s-run", ".run-about", ".run-t", ".run-actions .btn",
                      ".team-text", ".team-top .who", ".team .slack-link", ".fbk-btn", ".fbk-chip", ".team-count",
                      ".team-h", "#pause-btn", ".pill.paused", ".pill.paused b"]


@needs_chrome
@pytest.mark.parametrize("dark", [True, False], ids=["midnight", "daylight"])
def test_run_controls_are_readable_in_both_themes_as_rendered(tmp_path, cdp, dark):
    """The real computed colours of every piece of text the controls draw, composited over the backgrounds under them,
    reach 4.5:1 (WCAG AA): paused, a running job, a wrong Slack account and a failed run, then the plain state."""
    box: dict = {}

    def seed(store, settings):
        seed_runs(store, settings)
        seed_posts(box)(store, settings)
        controls.record_identity(store, "U0OTHER12345", False, datetime.now(settings.tz), None)
        controls.set_paused(store, True, datetime.now(settings.tz) - timedelta(minutes=3))
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        lock = hold(Path(settings.db_path).parent / "weekly.lock")  # a running job: the "running" status line
        try:
            cdp.open(login_url(base, login), dark=dark)
            cdp.wait("!document.querySelector('.paused-banner')?.hidden && document.querySelector('.team-row')")
            cdp.wait(tile("weekly", ".querySelector('.run-status.s-run')"))
            time.sleep(0.8)  # the entrance animations settle
            seen = cdp.js(f"({CONTRAST_JS})({CONTRAST_SELECTORS!r})")
            low = {k: v for k, v in seen.items() if v is not None and v < 4.5}
            assert not low, (low, seen)
            for k in (".paused-banner .pb-text", ".run-ident", ".run-status.s-paused", ".run-status.s-run",
                      ".run-status.s-none", ".team-text", ".fbk-chip", "#pause-btn", ".pill.paused"):
                assert seen[k] is not None, k  # the check really looked at it
        finally:
            lock.close()
        # not paused, right account: the green strip, the ok status line and a usable draft button
        st = Store(settings.db_path)
        controls.set_paused(st, False, datetime.now(settings.tz))
        controls.record_identity(st, settings.haseeb_id, True, datetime.now(settings.tz), None)
        st.close()
        cdp.reload()
        cdp.wait("document.querySelector('.run-ident.is-ok') && document.querySelector('.fbk-btn:not(:disabled)')")
        time.sleep(0.8)
        seen = cdp.js(f"({CONTRAST_JS})({CONTRAST_SELECTORS!r})")
        low = {k: v for k, v in seen.items() if v is not None and v < 4.5}
        assert not low, (low, seen)
        for k in (".run-ident", ".run-status.s-ok", ".run-status.s-bad", ".fbk-btn"):
            assert seen[k] is not None, k
        cdp.close()


@needs_chrome
def test_run_controls_have_no_horizontal_scroll_at_phone_width(tmp_path, cdp):
    box: dict = {}

    def seed(store, settings):
        both(seed_runs, seed_two, seed_posts(box))(store, settings)
        controls.record_identity(store, "U0OTHER12345", False, datetime.now(settings.tz), None)
        controls.set_paused(store, True, datetime.now(settings.tz))
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login), width=380)
        cdp.wait("!document.querySelector('.paused-banner')?.hidden && document.querySelector('.team-row')")
        cdp.wait(row(box["hostile"]))
        time.sleep(0.8)
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        assert cdp.js("[...document.querySelectorAll('.card, .run-tile, .team-row, .paused-banner, .run-ident')]"
                      ".every(c => c.getBoundingClientRect().right <= 380.5)")
        assert cdp.js("getComputedStyle(document.querySelector('.run-grid')).gridTemplateColumns.split(' ').length") == 1
        # the header: brand, Pause/Resume and the theme switch fit, and so does a confirm row
        assert cdp.js("document.getElementById('pause-btn').getBoundingClientRect().right <= 380")
        cdp.close()


@needs_chrome
def test_a_running_tile_and_a_busy_draft_stop_moving_under_reduced_motion(tmp_path, cdp):
    box: dict = {}
    with live_server(tmp_path, both(seed_runs, seed_posts(box)), stream_polls=200) as (base, settings, login):
        lock = hold(Path(settings.db_path).parent / "weekly.lock")
        try:
            cdp.open(login_url(base, login), reduced=True)
            cdp.wait(tile("weekly", ".querySelector('.run-status .spinner')"))
            names = cdp.js(f"""[{tile('weekly', '.querySelector(".spinner")')},
                getComputedStyle({tile('weekly')}, '::after'), getComputedStyle(document.querySelector('.paused-banner'))]
                .map(e => e instanceof CSSStyleDeclaration ? e.animationName : getComputedStyle(e).animationName)""")
            assert set(names) == {"none"}, names
        finally:
            lock.close()
        cdp.close()
