"""Final-review fixes for the Next Todo job: I1 (a promised auto-post never silently fails), I2 (the veto counts
from DM delivery), I3 (R36: the pre-draft goes out at 15:00; unverified notes still get a DM draft)."""
import json
from dataclasses import replace
from datetime import timedelta

from haseeb_agent.jobs.todo import run_todo
from haseeb_agent.slack_gateway import SlackError
from tests.test_todo import (  # noqa: F401 - pytest fixtures
    CH, FILE, HASEEB, HUDDLE_TS, ITEM_ID, FakeLLM, add_reply, at, msg, notes_file, notes_reply, seed_huddle,
    settings, slack, store, tick, ts_at,
)

NOT_SENT = "⚠️ Auto-post did not go out — reply post to post it"
FAILED = "⚠️ Auto-post may have failed — check the huddle thread, then reply post if it's missing"


class Clock:
    """A clock that returns the given times in order, then keeps returning the last one."""

    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0) if len(self.times) > 1 else self.times[0]


# -- I1 (a): the auto-post stage runs until 16:05 --------------------------------------------------------------

def test_i1a_due_1559_posts_on_the_tick_after_1600(slack, store, settings):
    seed_huddle(slack, [notes_reply(ts=ts_at(15, 57, 0))])
    tick(slack, store, settings, at(15, 57, 30))
    assert "Auto-posts at 15:59" in slack.dm_sends()[0].text
    tick(slack, store, settings, at(16, 0, 30))  # the reviewer's probe: due 15:59:30, next tick after 16:00
    (post,) = slack.channel_sends()
    assert post.thread_ts == HUDDLE_TS and store.get_item(ITEM_ID).status == "posted"


def test_i1a_job_is_idle_from_1605(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(16, 5))
    assert slack.calls == [] and slack.sent == []


def test_i1a_no_autopost_is_armed_when_it_would_be_due_in_the_last_minute(slack, store, settings):
    """The last minute is 17:59 since the overtime (2026-10-24); before, it was 16:04."""
    seed_huddle(slack, [notes_reply(ts=ts_at(17, 56, 0))])
    slack.clock = at(15, 0)
    tick(slack, store, settings, at(15, 0))  # the pre-draft: the huddle runs long, so the job keeps looking
    slack.clock = None
    tick(slack, store, settings, at(17, 57, 30))  # due 17:59:30: no 60 s tick could be sure to post it
    dm = slack.dm_sends()[-1]
    assert "Auto-posts" not in dm.text and "18:00" in dm.text
    assert not store.get_cursor(f"autopost:{ITEM_ID}")


# -- I1 (b, c): the closing stage --------------------------------------------------------------------------------

def test_i1b_overdue_autopost_is_cancelled_and_haseeb_told_once(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))                    # due 15:20
    item = store.get_item(ITEM_ID)
    slack.fail_next("thread unreadable", method="read_thread")  # 15:20:30: the huddle thread can't be read
    tick(slack, store, settings, at(15, 20, 30))
    for minute in (21, 22):                                      # still within due + 3 min: the DM thread fails
        slack.fail_next("thread unreadable", method="read_thread")
        tick(slack, store, settings, at(15, minute, 30))
    assert slack.channel_sends() == []
    tick(slack, store, settings, at(15, 23, 1))                  # past due + 3 min: closed, never posts late
    assert slack.channel_sends() == []
    notices = [s for s in slack.dm_sends() if s.text == NOT_SENT]
    assert len(notices) == 1 and notices[0].thread_ts == item.dm_ts
    assert store.get_cursor(f"own:{notices[0].ts}") == "1"
    assert not store.get_cursor(f"autopost:{ITEM_ID}")
    for minute in range(24, 60):
        tick(slack, store, settings, at(15, minute))
    assert slack.channel_sends() == [] and len([s for s in slack.dm_sends() if s.text == NOT_SENT]) == 1
    assert store.get_item(ITEM_ID).status == "delivered"  # Haseeb can still reply `post`


def test_i1c_mac_wakes_after_the_window_the_notice_still_goes_out(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    tick(slack, store, settings, at(17, 40))  # the Mac slept through 15:20..16:05
    assert slack.channel_sends() == []
    assert [s.text for s in slack.dm_sends()][-1] == NOT_SENT
    assert not store.get_cursor(f"autopost:{ITEM_ID}")


def test_i1c_mac_wakes_next_day_the_notice_still_goes_out(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    tick(slack, store, settings, at(9, 0) + timedelta(days=1))
    assert [s.text for s in slack.dm_sends()][-1] == NOT_SENT
    assert slack.channel_sends() == []


def test_i1c_notice_is_retried_until_it_is_sent(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    slack.fail_next("dm down", method="send")
    tick(slack, store, settings, at(15, 30))
    assert NOT_SENT not in [s.text for s in slack.dm_sends()]
    assert not store.get_cursor(f"autopost:{ITEM_ID}")  # cleared at once: it can never fire late
    tick(slack, store, settings, at(15, 31))
    assert [s.text for s in slack.dm_sends()].count(NOT_SENT) == 1
    tick(slack, store, settings, at(15, 32))
    assert [s.text for s in slack.dm_sends()].count(NOT_SENT) == 1


def test_i1c_interrupted_send_found_by_the_closing_stage_is_a_failure_not_a_no_show(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    store.set_cursor(f"autopost_try:{ITEM_ID}", at(15, 20).isoformat())  # crash mid-send, then asleep
    tick(slack, store, settings, at(17, 0))
    assert store.get_item(ITEM_ID).status == "failed"
    assert [s.text for s in slack.dm_sends()][-1] == FAILED
    assert slack.channel_sends() == []


# -- I1 (d): failure notices are retried ------------------------------------------------------------------------

def test_i1d_autopost_failed_notice_is_retried(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    real_send = slack.send
    state = {"n": 0}

    def send(channel_id, text, thread_ts=None):
        state["n"] += 1
        if channel_id == CH:
            raise SlackError("slack_send_message: tool-call mismatch")
        if text == FAILED and state.get("dm_fails", 0) < 1:
            state["dm_fails"] = 1
            raise SlackError("dm down")
        return real_send(channel_id, text, thread_ts)

    slack.send = send
    tick(slack, store, settings, at(15, 20, 30))
    assert store.get_item(ITEM_ID).status == "failed"
    assert FAILED not in [s.text for s in slack.dm_sends()]
    assert json.loads(store.get_cursor(f"todo_notice:{ITEM_ID}")) == [FAILED]
    tick(slack, store, settings, at(15, 21, 30))
    assert [s.text for s in slack.dm_sends()].count(FAILED) == 1
    assert json.loads(store.get_cursor(f"todo_notice:{ITEM_ID}")) == []


# -- I2: the 120 s veto counts from DM delivery ---------------------------------------------------------------

def test_i2_due_counts_from_delivery_not_from_job_start(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    # The tick starts 15:18:00, drafting takes until 15:18:50, the DM lands at 15:19:00.
    run_todo(slack, FakeLLM(), store, settings, at(15, 18), clock=Clock(at(15, 18, 50), at(15, 19, 0)))
    (dm,) = slack.sent
    assert "Auto-posts at 15:20" in dm.text  # shown from the clock right before the send: never later than real
    assert store.get_cursor(f"delivered:{ITEM_ID}") == at(15, 19, 0).isoformat()
    assert store.get_cursor(f"autopost:{ITEM_ID}") == at(15, 21, 0).isoformat()
    tick(slack, store, settings, at(15, 20, 55))  # 120 s after job start, but only 115 s after delivery
    assert slack.channel_sends() == []
    tick(slack, store, settings, at(15, 21, 5))
    assert len(slack.channel_sends()) == 1


def test_i2_autopost_requires_120s_since_delivery_even_if_the_due_cursor_is_early(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    store.set_cursor(f"delivered:{ITEM_ID}", at(15, 19).isoformat())  # e.g. a refined draft re-delivered
    tick(slack, store, settings, at(15, 20, 30))
    assert slack.channel_sends() == []
    tick(slack, store, settings, at(15, 21, 0))
    assert len(slack.channel_sends()) == 1


# -- I3 (R36): the pre-draft goes out at 15:00 -----------------------------------------------------------------

def test_i3_predraft_is_delivered_at_1500_without_autopost(slack, store, settings):
    seed_huddle(slack, [], parent_files=())
    tick(slack, store, settings, at(14, 59))
    assert slack.sent == []
    tick(slack, store, settings, at(15, 0))
    (dm,) = slack.sent
    assert dm.channel_id == HASEEB and dm.thread_ts is None
    assert "Pre-draft · reply post when the huddle ends" in dm.text and "Auto-posts" not in dm.text
    item = store.get_item(ITEM_ID)
    assert item.status == "delivered" and item.dm_ts == dm.ts and item.source_ts == HUDDLE_TS
    assert not store.get_cursor(f"autopost:{ITEM_ID}")
    for minute in range(1, 40):
        tick(slack, store, settings, at(15, minute))
    assert len(slack.sent) == 1 and slack.channel_sends() == []  # nothing more without notes


def test_i3_predraft_send_failure_retries_next_tick(slack, store, settings):
    seed_huddle(slack, [], parent_files=())
    slack.fail_next("dm down", method="send")
    tick(slack, store, settings, at(15, 0))
    assert slack.sent == [] and store.get_item(ITEM_ID).status == "drafted"
    tick(slack, store, settings, at(15, 1))
    assert len(slack.dm_sends()) == 1 and store.get_item(ITEM_ID).status == "delivered"


def test_i3_notes_refine_the_predraft_and_may_arm_the_autopost(slack, store, settings):
    seed_huddle(slack, [], parent_files=())
    tick(slack, store, settings, at(15, 0))
    pre = store.get_item(ITEM_ID)
    parent, _ = slack.threads[(CH, HUDDLE_TS)]
    slack.threads[(CH, HUDDLE_TS)] = (replace(parent, files=[notes_file()]), [notes_reply(ts=ts_at(15, 16))])
    tick(slack, store, settings, at(15, 17))
    refined = slack.dm_sends()[-1]
    assert refined.thread_ts == pre.dm_ts and "Auto-posts at 15:19" in refined.text
    tick(slack, store, settings, at(15, 19, 30))
    assert len(slack.channel_sends()) == 1


def test_i3_predraft_already_posted_by_haseeb_is_never_refined_or_autoposted(slack, store, settings):
    seed_huddle(slack, [], parent_files=())
    tick(slack, store, settings, at(15, 0))
    store.mark(ITEM_ID, "posted")  # Haseeb replied `post` on the pre-draft right after the huddle
    parent, _ = slack.threads[(CH, HUDDLE_TS)]
    slack.threads[(CH, HUDDLE_TS)] = (replace(parent, files=[notes_file()]), [notes_reply(ts=ts_at(15, 16))])
    n = len(slack.sent)
    for minute in range(17, 30):
        tick(slack, store, settings, at(15, minute))
    assert len(slack.sent) == n and slack.channel_sends() == []


def test_i3_unverified_slackbot_notes_get_a_dm_draft_without_autopost(slack, store, settings):
    # 15 Oct: Slackbot's notes reply exists, but the huddle parent never got a Files line (R10 fails)
    seed_huddle(slack, [notes_reply()], parent_files=())
    tick(slack, store, settings, at(15, 18))
    (dm,) = slack.sent
    assert "Auto-post off: notes not verified" in dm.text and "Auto-posts" not in dm.text
    assert dm.text.index("Next Todo:") > dm.text.index("notes not verified")
    assert store.get_item(ITEM_ID).body.startswith("Next Todo:\n1. Ship the auto-post.")
    assert not store.get_cursor(f"autopost:{ITEM_ID}")
    for minute in range(19, 60):
        tick(slack, store, settings, at(15, minute))
    assert slack.channel_sends() == [] and len(slack.sent) == 1


def test_i3_unverified_draft_is_upgraded_once_the_notes_verify(slack, store, settings):
    seed_huddle(slack, [notes_reply()], parent_files=())
    tick(slack, store, settings, at(15, 18))
    first = slack.sent[0]
    parent, replies = slack.threads[(CH, HUDDLE_TS)]
    slack.threads[(CH, HUDDLE_TS)] = (replace(parent, files=[notes_file()]), replies)
    tick(slack, store, settings, at(15, 19))
    second = slack.dm_sends()[-1]
    assert second.thread_ts == first.ts and "Auto-posts at 15:21" in second.text
    tick(slack, store, settings, at(15, 21, 30))
    assert len(slack.channel_sends()) == 1


def test_i3_teammate_notes_reply_is_still_ignored(slack, store, settings):
    from tests.test_todo import TEAMMATE
    seed_huddle(slack, [notes_reply(user_id=TEAMMATE)], parent_files=())
    tick(slack, store, settings, at(15, 18))
    (dm,) = slack.sent  # only the 15:00 pre-draft: no notes-based draft from a teammate's reply
    assert "Pre-draft" in dm.text and "read_file" not in slack.calls


def test_i3_unverified_notes_on_a_huddle_haseeb_already_posted_in_do_nothing(slack, store, settings):
    mine = msg(ts_at(15, 14), "Next Todo:\n1. Mine.", HASEEB, HUDDLE_TS)
    seed_huddle(slack, [mine, notes_reply()], parent_files=())
    tick(slack, store, settings, at(15, 18))
    assert slack.sent == [] and store.get_item(ITEM_ID).status == "skipped"


# -- I5 (R37): huddle threads are re-read only when the page shows a change -----------------------------------

def _thread_reads(slack):
    return slack.calls.count("read_thread")


def test_i5_huddle_thread_is_read_once_while_nothing_changes(slack, store, settings):
    seed_huddle(slack, [], parent_files=())
    tick(slack, store, settings, at(14, 40))
    tick(slack, store, settings, at(14, 41))
    tick(slack, store, settings, at(14, 42))
    assert slack.calls.count("read_channel") == 3 and _thread_reads(slack) == 1


def test_i5_a_new_reply_triggers_a_re_read(slack, store, settings):
    seed_huddle(slack, [], parent_files=())
    tick(slack, store, settings, at(14, 40))
    parent, _ = slack.threads[(CH, HUDDLE_TS)]
    slack.threads[(CH, HUDDLE_TS)] = (replace(parent, files=[notes_file()]), [notes_reply(ts=ts_at(14, 40, 30))])
    tick(slack, store, settings, at(14, 41))
    assert _thread_reads(slack) == 2
    assert "Auto-posts" in slack.dm_sends()[-1].text


def test_i5_a_parent_file_appearing_triggers_a_re_read(slack, store, settings):
    seed_huddle(slack, [notes_reply()], parent_files=())
    tick(slack, store, settings, at(15, 18))
    n = _thread_reads(slack)
    tick(slack, store, settings, at(15, 18, 30))
    assert _thread_reads(slack) == n  # cached
    parent, replies = slack.threads[(CH, HUDDLE_TS)]
    slack.threads[(CH, HUDDLE_TS)] = (replace(parent, files=[notes_file()]), replies)
    tick(slack, store, settings, at(15, 19))
    # the huddle thread re-read, plus (N2) one read of the DM thread for a pending `post` before the refine
    assert _thread_reads(slack) == n + 2


def test_i5_due_autopost_forces_a_fresh_read_and_keeps_the_pre_send_read(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    n = _thread_reads(slack)
    tick(slack, store, settings, at(15, 19))
    assert _thread_reads(slack) == n  # not due yet: cached
    tick(slack, store, settings, at(15, 20, 30))
    # the scan's fresh read, the DM thread read and the pre-send huddle re-read
    assert _thread_reads(slack) == n + 3 and len(slack.channel_sends()) == 1


# -- M4: the guard runs on the text that would actually be sent --------------------------------------------------

def test_m4_body_that_passes_but_renders_differently_never_autoposts(slack, store, settings):
    from haseeb_agent.jobs.todo import passes_guard
    from haseeb_agent.render import render_todo_post
    body = "Next Todo:\n1. Ship the R&D plan.\n2. Share the prototype plan with Lion.\n3. Post an insight daily."
    assert passes_guard(body) and not passes_guard(render_todo_post(body))  # "&D" gets a ZWSP when rendered
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18), FakeLLM(body))
    assert store.get_cursor(f"autopost:{ITEM_ID}")  # armed from the draft...
    tick(slack, store, settings, at(15, 20, 30))
    assert slack.channel_sends() == []  # ...but the rendered text fails, so nothing goes out
    assert not store.get_cursor(f"autopost:{ITEM_ID}")
    assert "Auto-post off: draft needs a look" in slack.dm_sends()[-1].text
