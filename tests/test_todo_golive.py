"""Go-live fixes for the Next Todo job: N1 (only replies newer than the announcing draft cancel its auto-post, and
Haseeb is always told), N2 (`post` posts the version he replied to; a pending `post` holds the refine), N7 (the
late margin uses the real due time)."""
from dataclasses import replace
from decimal import Decimal

import pytest

from haseeb_agent.commands import process_dm
from haseeb_agent.jobs.todo import TodoHandlers, run_todo
from haseeb_agent.models import Msg
from tests.test_todo import (  # noqa: F401 - pytest fixtures
    CH, GOOD, HASEEB, HUDDLE_TS, ITEM_ID, FakeLLM, at, msg, notes_file, notes_reply, seed_huddle, settings, slack,
    store, tick, ts_at,
)
from tests.test_todo_review import Clock

CANCELLED = "Auto-post cancelled — reply post to post it"
LATE_CANCELLED = "Auto-post cancelled — too close to 18:00 · reply post to post it"
PRE = "Next Todo:\n1. Draft the prototype plan.\n2. Review the agent logs.\n3. Post an insight daily with my AI agent."


def _after(ts: str, seconds: float) -> str:
    """A ts `seconds` after `ts` (the fake's send ts run on their own counter, not on the test clock)."""
    return f"{Decimal(ts) + Decimal(str(seconds)):.6f}"


def _dm_reply(slack, parent_ts, ts, text, latest=True):
    """Add Haseeb's reply to a DM thread, keeping the page's reply_count/latest_reply in step (for process_dm)."""
    parent = next(m for m in slack.pages[HASEEB] if m.ts == parent_ts)
    _, replies = slack.threads.get((HASEEB, parent_ts), (parent, []))
    reply = Msg(ts=ts, user_id=HASEEB, user_name="Haseeb", text=text, thread_ts=parent_ts, reply_count=0,
                latest_reply=None)
    replies = sorted([*replies, reply], key=lambda m: Decimal(m.ts))
    slack.threads[(HASEEB, parent_ts)] = (parent, replies)
    if latest:
        parent.reply_count, parent.latest_reply = len(replies), replies[-1].ts.split(".")[0]
    return reply


def _notes_land(slack, at_ts):
    p, _ = slack.threads[(CH, HUDDLE_TS)]
    slack.threads[(CH, HUDDLE_TS)] = (replace(p, files=[notes_file()]), [notes_reply(ts=at_ts)])


def _predraft(slack, store, settings):
    seed_huddle(slack, [], parent_files=())
    tick(slack, store, settings, at(15, 0), FakeLLM(PRE))
    pre = store.get_item(ITEM_ID)
    assert pre.status == "delivered" and pre.dm_ts and pre.body == PRE
    return pre


def _notices(slack, item):
    return [s.text for s in slack.dm_sends() if s.thread_ts == item.dm_ts and "Auto-post cancelled" in s.text]


# -- N1: an older chat reply no longer cancels; a newer one cancels and Haseeb is told once ---------------------

def test_n1_chat_in_the_predraft_thread_before_the_notes_does_not_cancel_the_refined_autopost(slack, store, settings):
    """The reviewer's probe: Haseeb chats in the pre-draft's thread, then the notes land and the refined draft
    promises an auto-post. That promise must be kept."""
    pre = _predraft(slack, store, settings)
    _dm_reply(slack, pre.dm_ts, _after(pre.dm_ts, 0.5), "ok, will check after the huddle")
    _notes_land(slack, ts_at(15, 16))
    tick(slack, store, settings, at(15, 17))
    refined = slack.dm_sends()[-1]
    assert refined.thread_ts == pre.dm_ts and "Auto-posts at 15:19" in refined.text
    for m in range(19, 25):
        tick(slack, store, settings, at(15, m, 30))
    (post,) = slack.channel_sends()
    assert post.thread_ts == HUDDLE_TS and post.text == GOOD
    assert store.get_item(ITEM_ID).status == "posted"
    assert _notices(slack, pre) == []


def test_n1_a_chat_reply_after_the_refined_draft_cancels_and_tells_haseeb_once(slack, store, settings):
    pre = _predraft(slack, store, settings)
    _notes_land(slack, ts_at(15, 16))
    tick(slack, store, settings, at(15, 17))
    refined = slack.dm_sends()[-1]
    _dm_reply(slack, pre.dm_ts, _after(refined.ts, 0.5), "wait, line 2 is wrong")
    for m in range(19, 40):
        tick(slack, store, settings, at(15, m, 30))
    assert slack.channel_sends() == []
    assert _notices(slack, pre) == [CANCELLED]
    assert store.get_item(ITEM_ID).status == "delivered" and not store.get_cursor(f"autopost:{ITEM_ID}")


def test_n1_a_chat_reply_on_a_top_level_draft_cancels_and_tells_haseeb(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    item = store.get_item(ITEM_ID)
    _dm_reply(slack, item.dm_ts, _after(item.dm_ts, 30), "hmm")
    for m in range(20, 30):
        tick(slack, store, settings, at(15, m))
    assert slack.channel_sends() == []
    assert _notices(slack, item) == [CANCELLED]


def test_n1_the_cancel_notice_is_retried_until_sent_and_never_sent_twice(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    item = store.get_item(ITEM_ID)
    _dm_reply(slack, item.dm_ts, _after(item.dm_ts, 30), "hmm")
    slack.fail_next("dm down", method="send")
    tick(slack, store, settings, at(15, 20))          # cancelled; the notice send fails
    assert _notices(slack, item) == [] and not store.get_cursor(f"autopost:{ITEM_ID}")
    for m in range(21, 30):
        tick(slack, store, settings, at(15, m))
    assert _notices(slack, item) == [CANCELLED]
    assert slack.channel_sends() == []


def test_n1_a_command_reply_cancels_without_the_notice(slack, store, settings):
    """A `post` or `skip` that process_dm has not handled yet: the command's own status reply tells Haseeb, so the
    "reply post" notice would only contradict it."""
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    item = store.get_item(ITEM_ID)
    _dm_reply(slack, item.dm_ts, _after(item.dm_ts, 30), "skip")
    tick(slack, store, settings, at(15, 20, 30))
    assert slack.channel_sends() == [] and _notices(slack, item) == []
    assert not store.get_cursor(f"autopost:{ITEM_ID}")


@pytest.mark.parametrize("text", ["post it", "post now", "post survive-ai", "post C093L3Y8UM6"])
def test_n1_a_word_after_post_is_chat_so_it_cancels_with_the_notice(slack, store, settings, text):
    """Post-channel design: a channel is written `post #name` (or as Slack's markup). A bare word after `post` is ordinary chat, as it
    was before the choice existed, so the cancel is not quiet: Haseeb is told the auto-post is cancelled and how to post."""
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    item = store.get_item(ITEM_ID)
    _dm_reply(slack, item.dm_ts, _after(item.dm_ts, 30), text)
    for m in range(20, 30):
        tick(slack, store, settings, at(15, m))
    assert slack.channel_sends() == []
    assert _notices(slack, item) == [CANCELLED]
    assert not store.get_cursor(f"autopost:{ITEM_ID}")


@pytest.mark.parametrize("text", ["post #survive-ai", "post <#C093L3Y8UM6|survive-ai>", "post #x"])
def test_n1_a_post_with_a_channel_on_the_todo_cancels_and_still_tells_haseeb(slack, store, settings, text):
    """It is a command, so process_dm answers it: the Next Todo posts only to its thread in the leadership channel. That answer does
    not say the auto-post is cancelled, so the cancel is not quiet either: the notice goes out as for chat, and nothing is posted."""
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    item = store.get_item(ITEM_ID)
    _dm_reply(slack, item.dm_ts, _after(item.dm_ts, 30), text)
    for m in range(20, 30):
        tick(slack, store, settings, at(15, m))
    assert slack.channel_sends() == [] and _notices(slack, item) == [CANCELLED]
    assert not store.get_cursor(f"autopost:{ITEM_ID}")
    handlers = {"todo": TodoHandlers(slack, FakeLLM(), store, settings)}
    assert process_dm(slack, store, settings, handlers, at(15, 31)) == 1
    assert slack.dm_sends()[-1].text == "⚠️ this draft posts only to its thread in the leadership channel"
    assert slack.channel_sends() == [] and store.get_item(ITEM_ID).status == "delivered"
    assert _notices(slack, item) == [CANCELLED]  # told once, not twice


# -- N2: `post` posts the text Haseeb saw ---------------------------------------------------------------------

def test_n2_a_pending_post_on_the_predraft_holds_the_refine_and_posts_the_predraft(slack, store, settings):
    pre = _predraft(slack, store, settings)
    cmd = _dm_reply(slack, pre.dm_ts, _after(pre.dm_ts, 60), "post")
    _notes_land(slack, ts_at(15, 16))
    llm = FakeLLM()
    n = len(slack.dm_sends())
    tick(slack, store, settings, at(15, 17), llm)       # process_dm has not seen the reply yet
    assert len(slack.dm_sends()) == n and llm.calls == []    # no refine, no drafting
    assert store.get_item(ITEM_ID).body == PRE and store.get_cursor(f"notes:{ITEM_ID}") is None
    handlers = {"todo": TodoHandlers(slack, FakeLLM(), store, settings)}
    assert process_dm(slack, store, settings, handlers, at(15, 18)) == 1
    (post,) = slack.channel_sends()
    assert post.text == PRE and post.thread_ts == HUDDLE_TS
    assert store.get_item(ITEM_ID).status == "posted"
    tick(slack, store, settings, at(15, 19))
    assert len(slack.channel_sends()) == 1
    assert cmd.ts  # the command, not the refine, decided


def test_n2_a_post_command_processed_after_the_refine_posts_the_predraft_it_replied_to(slack, store, settings):
    """The race: Haseeb replies `post` to the pre-draft; before process_dm handles it, the notes refine replaces
    item.body. The command must post the pre-draft (what he saw), not the refined text."""
    pre = _predraft(slack, store, settings)
    _notes_land(slack, ts_at(15, 16))
    tick(slack, store, settings, at(15, 17))            # refined: body is now GOOD
    refined = slack.dm_sends()[-1]
    assert store.get_item(ITEM_ID).body == GOOD
    # His `post` went out before the refined draft landed (its ts is older), but is handled only now.
    cmd = _dm_reply(slack, pre.dm_ts, _after(pre.dm_ts, 0.5), "post")
    assert Decimal(pre.dm_ts) < Decimal(cmd.ts) < Decimal(refined.ts)
    handlers = {"todo": TodoHandlers(slack, FakeLLM(), store, settings)}
    assert process_dm(slack, store, settings, handlers, at(15, 18)) == 1
    (post,) = slack.channel_sends()
    assert post.text == PRE


def test_n2_post_after_the_refined_draft_posts_the_refined_text(slack, store, settings):
    pre = _predraft(slack, store, settings)
    _notes_land(slack, ts_at(15, 16))
    tick(slack, store, settings, at(15, 17))
    refined = slack.dm_sends()[-1]
    _dm_reply(slack, pre.dm_ts, _after(refined.ts, 1), "post")
    handlers = {"todo": TodoHandlers(slack, FakeLLM(), store, settings)}
    assert process_dm(slack, store, settings, handlers, at(15, 18)) == 1
    (post,) = slack.channel_sends()
    assert post.text == GOOD


def test_n2_post_after_a_revise_posts_the_revised_text(slack, store, settings):
    revised = "Next Todo:\n1. Ship it.\n2. Share the plan.\n3. Post daily."
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    item = store.get_item(ITEM_ID)
    h = TodoHandlers(slack, FakeLLM(revised), store, settings)
    h.revise(item, "shorter")
    rev = slack.dm_sends()[-1]
    store.mark(ITEM_ID, "delivered", edited=True)
    assert h.post(store.get_item(ITEM_ID), reply_ts=_after(rev.ts, 1)) and slack.channel_sends()[-1].text == revised
    # ...and a `post` that replied before the revision posts the draft it replied to
    store.mark(ITEM_ID, "delivered")
    h.post(store.get_item(ITEM_ID), reply_ts=_after(item.dm_ts, 0.5))
    assert slack.channel_sends()[-1].text == GOOD


def test_n2_post_with_a_reply_older_than_every_version_is_refused(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    item = store.get_item(ITEM_ID)
    import pytest
    with pytest.raises(LookupError):
        TodoHandlers(slack, FakeLLM(), store, settings).post(item, reply_ts=_after(item.dm_ts, -10))
    assert slack.channel_sends() == []


def test_n2_post_without_a_reply_ts_posts_the_current_body(slack, store, settings):
    seed_huddle(slack, [notes_reply()])
    tick(slack, store, settings, at(15, 18))
    TodoHandlers(slack, FakeLLM(), store, settings).post(store.get_item(ITEM_ID))
    assert slack.channel_sends()[-1].text == GOOD


def test_n2_an_unreadable_dm_thread_holds_the_refine_for_this_tick(slack, store, settings):
    pre = _predraft(slack, store, settings)
    _notes_land(slack, ts_at(15, 16))
    real = slack.read_thread

    def flaky(channel_id, ts):
        if channel_id == HASEEB:
            from haseeb_agent.slack_gateway import SlackError
            raise SlackError("thread unreadable")
        return real(channel_id, ts)
    slack.read_thread = flaky
    n = len(slack.dm_sends())
    tick(slack, store, settings, at(15, 17))
    assert len(slack.dm_sends()) == n and store.get_item(ITEM_ID).body == PRE
    slack.read_thread = real
    tick(slack, store, settings, at(15, 18))
    assert slack.dm_sends()[-1].thread_ts == pre.dm_ts and store.get_item(ITEM_ID).body == GOOD


# -- N7: the late margin uses the real due time -----------------------------------------------------------------

def test_n7_a_due_time_past_the_last_tick_after_the_send_cancels_and_says_so(slack, store, settings):
    """At the end of the overtime (18:00 since 2026-10-24; before, at 16:05)."""
    seed_huddle(slack, [notes_reply(ts=ts_at(17, 56, 0))])
    slack.clock = at(15, 0)
    tick(slack, store, settings, at(15, 0))  # the pre-draft: the huddle runs long, so the job keeps looking
    slack.clock = None
    pre = len(slack.dm_sends())
    # Shown 17:56:50 + 120 s = 17:58:50 (in time), but the DM lands at 17:57:30: due 17:59:30, after 17:59.
    run_todo(slack, FakeLLM(), store, settings, at(17, 56, 40), clock=Clock(at(17, 56, 50), at(17, 57, 30)))
    dm = slack.dm_sends()[pre]
    item = store.get_item(ITEM_ID)
    assert "Auto-posts at 17:58" in dm.text
    assert not store.get_cursor(f"autopost:{ITEM_ID}")
    assert [s.text for s in slack.dm_sends()[pre + 1:]] == [LATE_CANCELLED]
    assert slack.dm_sends()[pre + 1].thread_ts == item.dm_ts
    for m in (58, 59):
        tick(slack, store, settings, at(17, m, 45))
    assert slack.channel_sends() == [] and len(slack.dm_sends()) == pre + 2


def test_n7_a_due_time_at_the_margin_still_autoposts(slack, store, settings):
    seed_huddle(slack, [notes_reply(ts=ts_at(16, 1, 0))])
    run_todo(slack, FakeLLM(), store, settings, at(16, 1, 40), clock=Clock(at(16, 1, 50), at(16, 2, 0)))
    assert store.get_cursor(f"autopost:{ITEM_ID}") == at(16, 4, 0).isoformat()
    tick(slack, store, settings, at(16, 4, 30))
    assert len(slack.channel_sends()) == 1
