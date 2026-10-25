"""Run controls (2026-10-20): the pause switch, the job allowlist, the "is it running" probe and the records a finished
job leaves. Tmp stores and tmp lock files only."""
import fcntl
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import cli, controls
from haseeb_agent.config import load_settings
from haseeb_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=KW)


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return replace(load_settings(ROOT / "tests" / "fixtures" / "config.toml", env), db_path=str(tmp_path / "data" / "agent.sqlite"))


@pytest.fixture
def store(settings):
    s = Store(settings.db_path)
    yield s
    s.close()


def hold(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")  # noqa: SIM115 - held open: the caller closes it to release the lock
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


# ---------------------------------------------------------------- the pause switch

def test_pause_set_keeps_the_first_time_and_resume_clears(store):
    assert controls.paused_since(store) is None
    assert controls.set_paused(store, True, NOW) == NOW.isoformat()
    assert store.get_cursor("agent_paused") == NOW.isoformat()  # the cursor is the ISO time it was paused
    assert controls.set_paused(store, True, NOW + timedelta(hours=1)) == NOW.isoformat()  # not moved
    assert controls.paused_since(store) == NOW.isoformat()
    assert controls.set_paused(store, False, NOW) is None
    assert controls.paused_since(store) is None
    assert store.get_cursor("agent_paused") == ""  # cleared, not deleted: empty means resumed
    assert controls.set_paused(store, False, NOW) is None  # resuming twice is fine


def test_any_text_in_the_cursor_counts_as_paused(store):
    """Fail closed: an unreadable value is still a pause."""
    store.set_cursor("agent_paused", "not a time")
    assert controls.paused_since(store) == "not a time"


def test_paused_since_db_opens_its_own_connection(settings, store):
    assert controls.paused_since_db(settings) is None
    controls.set_paused(store, True, NOW)
    assert controls.paused_since_db(settings) == NOW.isoformat()


# ---------------------------------------------------------------- the allowlist

def test_the_job_list_is_the_decided_one():
    assert [j.name for j in controls.JOBS] == ["tick", "insights", "decks", "weekly", "history_import", "enrich",
                                               "slack_check", "feedback"]
    assert {j.name: j.argv for j in controls.JOBS} == {
        "tick": None, "insights": ("insights", "--force"), "decks": ("decks", "--retry"), "weekly": ("weekly",),
        "history_import": ("history-import",), "enrich": ("history-enrich",), "slack_check": ("slack-check",),
        "feedback": ("feedback",)}
    assert [j.name for j in controls.JOBS if j.takes_ts] == ["feedback"]  # the one client-supplied value


def test_spawn_args_are_fixed_and_only_feedback_takes_a_ts():
    spawnable = [j for j in controls.JOBS if j.argv is not None]
    for job in spawnable:
        if job.takes_ts:
            continue
        assert controls.spawn_args(job) == list(job.argv)
        with pytest.raises(ValueError):
            controls.spawn_args(job, "1760000000.000100")  # no other job takes a value
    fb = controls.BY_NAME["feedback"]
    assert controls.spawn_args(fb, "1760000000.000100") == ["feedback", "--ts", "1760000000.000100"]
    with pytest.raises(ValueError):
        controls.spawn_args(fb)  # a feedback run needs its ts
    with pytest.raises(ValueError):
        controls.spawn_args(controls.BY_NAME["tick"])  # the tick is kicked, never spawned


@pytest.mark.parametrize("ts,ok", [
    ("1760000000.000100", True), ("1760000000.000000", True),
    ("1760000000.00010", False), ("176000000.000100", False), ("17600000000.000100", False),
    ("1760000000.0001000", False), ("1760000000000100", False), ("1760000000.000100\n", False),
    (" 1760000000.000100", False), ("1760000000.00010x", False), ("", False), (None, False), (1760000000.0001, False),
    ("１７６０００００００.000100", False),  # fullwidth digits are not \d{10} in a Slack ts
])
def test_ts_must_match_exactly(ts, ok):
    assert controls.valid_ts(ts) is ok
    if not ok and isinstance(ts, str):
        with pytest.raises(ValueError):
            controls.spawn_args(controls.BY_NAME["feedback"], ts)


def test_every_lock_is_the_file_the_command_takes(settings):
    """The dashboard probes the same file the job's process holds."""
    paths = {j.name: controls.lock_path(settings, j) for j in controls.JOBS}
    assert paths["tick"] == cli.tick_lock_path(settings)
    assert paths["insights"] == cli.insights_lock_path(settings)
    assert paths["decks"] == cli.decks_lock_path(settings)
    assert paths["enrich"] == cli.enrich_lock_path(settings)
    assert paths["weekly"] == cli.weekly_lock_path(settings)
    assert paths["history_import"] == cli.history_import_lock_path(settings)
    assert paths["slack_check"] == cli.slack_check_lock_path(settings)
    assert paths["feedback"] == cli.feedback_lock_path(settings)
    assert len(set(paths.values())) == len(paths)  # one lock each


def test_every_command_of_a_job_is_in_the_cli_parser():
    parser = cli._parser()
    for job in controls.JOBS:
        if job.argv is None:
            continue
        args = [*job.argv, *(["--ts", "1760000000.000100"] if job.takes_ts else [])]
        assert parser.parse_args(args).command == job.argv[0]


def test_each_command_maps_to_its_job():
    """COMMAND_JOB is derived from JOBS (the first word of a job's argv; the tick, which is kicked, is `tick`)."""
    assert controls.COMMAND_JOB == {
        "tick": "tick", "insights": "insights", "decks": "decks", "weekly": "weekly", "history-import": "history_import",
        "history-enrich": "enrich", "slack-check": "slack_check", "feedback": "feedback"}
    assert set(controls.COMMAND_JOB.values()) == set(controls.BY_NAME)


# ---------------------------------------------------------------- the lock probe

def test_lock_held_probe(tmp_path):
    path = tmp_path / "x.lock"
    assert controls.lock_held(path) is False
    assert not path.exists()  # a probe never creates a lock file
    assert controls.lock_held(None) is False
    path.touch()
    assert controls.lock_held(path) is False
    f = hold(path)
    try:
        assert controls.lock_held(path) is True
    finally:
        f.close()
    assert controls.lock_held(path) is False
    with cli.try_lock(path) as got:  # and the probe left it free
        assert got is True


# ---------------------------------------------------------------- is it running?

def state(store, settings, name, now=NOW):
    return controls.job_state(store, settings, controls.BY_NAME[name], now)


def test_idle_job_is_not_running(store, settings):
    st = state(store, settings, "insights")
    assert (st.running, st.started_at, st.last) == (False, None, None)


def test_a_held_lock_means_running(store, settings):
    f = hold(controls.lock_path(settings, controls.BY_NAME["insights"]))
    try:
        assert state(store, settings, "insights").running is True
        assert state(store, settings, "decks").running is False  # another job's lock
    finally:
        f.close()
    assert state(store, settings, "insights").running is False


def test_a_fresh_spawn_counts_as_running_until_the_process_has_its_lock(store, settings):
    controls.mark_spawned(store, "weekly", NOW - timedelta(seconds=30))
    st = state(store, settings, "weekly")
    assert st.running is True and st.started_at == (NOW - timedelta(seconds=30)).isoformat()


def test_a_spawn_older_than_two_minutes_is_not_running(store, settings):
    controls.mark_spawned(store, "weekly", NOW - timedelta(minutes=2, seconds=1))
    assert state(store, settings, "weekly").running is False
    controls.mark_spawned(store, "weekly", NOW - timedelta(minutes=1, seconds=59))
    assert state(store, settings, "weekly").running is True


def test_a_spawn_the_job_has_ended_since_is_not_running(store, settings):
    """A quick job (a Slack check takes seconds) must not look busy for the rest of the window."""
    controls.mark_spawned(store, "slack_check", NOW - timedelta(seconds=40))
    controls.record_last(store, "slack_check", NOW - timedelta(seconds=20), True, "posting as you")
    st = state(store, settings, "slack_check")
    assert st.running is False and st.started_at is None
    assert st.last == {"at": (NOW - timedelta(seconds=20)).isoformat(), "ok": True, "summary": "posting as you"}


def test_a_last_run_from_before_the_spawn_does_not_end_it(store, settings):
    controls.record_last(store, "weekly", NOW - timedelta(days=1), True, "done")
    controls.mark_spawned(store, "weekly", NOW - timedelta(seconds=10))
    st = state(store, settings, "weekly")
    assert st.running is True and st.last["summary"] == "done"


def test_a_spawn_from_the_future_is_ignored(store, settings):
    controls.mark_spawned(store, "weekly", NOW + timedelta(minutes=5))  # a clock that went backwards
    assert state(store, settings, "weekly").running is False


def test_the_running_feedback_job_names_its_post(store, settings):
    controls.mark_spawned(store, "feedback", NOW - timedelta(seconds=5), "1760000000.000100")
    assert state(store, settings, "feedback").arg == "1760000000.000100"
    controls.record_last(store, "feedback", NOW, True, "done")
    assert state(store, settings, "feedback").arg is None  # it ended


def test_a_naive_stored_time_is_read_in_the_settings_zone(store, settings):
    store.set_cursor("job_spawned:weekly", json.dumps({"at": "2026-10-20T09:59:30", "arg": None}))
    assert state(store, settings, "weekly").running is True


@pytest.mark.parametrize("raw", ["", "nope", "[]", "{}", '{"at": 5}', '{"at": "garbage"}'])
def test_unreadable_spawn_records_are_ignored(store, settings, raw):
    store.set_cursor("job_spawned:weekly", raw)
    assert state(store, settings, "weekly").running is False


# ---------------------------------------------------------------- the records

def test_record_last_round_trip_and_clean_summary(store):
    controls.record_last(store, "decks", NOW, True, "3 deck(s)\n  uploaded " + "x" * 400)
    last = controls.read_last(store, "decks")
    assert last["at"] == NOW.isoformat() and last["ok"] is True
    assert "\n" not in last["summary"] and len(last["summary"]) == controls.SUMMARY_MAX and last["summary"].endswith("…")
    assert controls.read_last(store, "weekly") is None


@pytest.mark.parametrize("raw", ["", "x", "[]", '{"at": "t"}', '{"ok": true}', '{"at": 5, "ok": true}',
                                 '{"at": "t", "ok": "yes"}'])
def test_unreadable_last_run_reads_as_none(store, raw):
    store.set_cursor("job_last:weekly", raw)
    assert controls.read_last(store, "weekly") is None


def test_summary_of_the_wrong_type_reads_as_empty(store):
    store.set_cursor("job_last:weekly", json.dumps({"at": NOW.isoformat(), "ok": False, "summary": 7}))
    assert controls.read_last(store, "weekly") == {"at": NOW.isoformat(), "ok": False, "summary": ""}


def test_identity_round_trip(store):
    assert controls.read_identity(store) is None
    controls.record_identity(store, "U08CZCGJW3E", True, NOW, None)
    assert controls.read_identity(store) == {"user_id": "U08CZCGJW3E", "ok": True, "at": NOW.isoformat(),
                                             "error": None}
    controls.record_identity(store, None, False, NOW, "the profile has no user id")
    assert controls.read_identity(store) == {"user_id": None, "ok": False, "at": NOW.isoformat(),
                                             "error": "the profile has no user id"}
    store.set_cursor("slack_identity", "garbage")
    assert controls.read_identity(store) is None
    store.set_cursor("slack_identity", json.dumps({"user_id": 5, "ok": "yes"}))
    assert controls.read_identity(store) == {"user_id": None, "ok": False, "at": None, "error": None}


# ---------------------------------------------------------------- what a pause does to work already in flight

def test_a_pause_holds_an_insight_back_without_using_up_its_resend_cap(tmp_path, settings):
    """The gateway refuses the DM while paused (SlackPaused: nothing was sent). The insight stays drafted however many
    times it is tried, and goes out after Resume: a pause must never cost the day's insight."""
    from haseeb_agent.commands import MAX_DM_SENDS
    from haseeb_agent.jobs.insights import deliver_pending
    from haseeb_agent.slack_gateway import SlackPaused
    from tests.test_insights import DmDown, FakeLLM, FakeResearch, MONDAY, cand, research_ok, run
    store = Store(tmp_path / "ins.sqlite")
    paused = SlackPaused("slack_send_message: the agent is paused since X, nothing was sent")
    slack = DmDown("☀️", paused)  # the index, the first DM, is refused every time
    run(slack, FakeLLM(), store, settings, FakeResearch(research_ok([cand("alpha")])))
    for m in range(5, 5 * (MAX_DM_SENDS + 4), 5):  # many more tries than the cap allows
        assert deliver_pending(slack, store, settings, MONDAY + timedelta(minutes=m)) == []
    (item,) = store.items_for_day("insight", MONDAY.date())
    assert item.status == "drafted" and slack.tries > MAX_DM_SENDS  # tried again and again, never given up on
    assert slack.sent == []  # and nothing was sent
    slack.errors = [None]  # Resume
    out = deliver_pending(slack, store, settings, MONDAY + timedelta(hours=1))
    assert [i.status for i in out] == ["delivered"] and slack.sent


def test_an_item_whose_dm_was_refused_by_a_pause_is_not_failed_or_flagged(tmp_path, settings):
    from haseeb_agent.alert import request_alert
    from haseeb_agent.commands import dm_send_failed
    from haseeb_agent.slack_gateway import SlackPaused
    store = Store(tmp_path / "x.sqlite")
    controls.set_paused(store, True, NOW)
    for _ in range(10):
        assert dm_send_failed(store, "k", SlackPaused("nothing was sent")) is False
    request_alert(store, "an insight could not be delivered")
    assert not store.get_cursor("alert_request")
