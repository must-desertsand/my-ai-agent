"""The reactions on the teammate's post (2026-10-21): 👀 (`eyes`) when the agent starts drafting feedback for it, ✅
(`white_check_mark`) when Haseeb's reply is actually posted, both as Haseeb, both through `slack.react`.

They are cosmetic. A reaction that fails never blocks, delays or fails a draft or a post and is never a post failure; it gets
at most one retry on a later run; Slack's `already_reacted` counts as done; while the agent is paused the gateway refuses it
(`SlackPaused`) and it is added after Resume, if it still makes sense. Never on Haseeb's own posts, Lion's or a bot's. The
connector has no tool to take a reaction off, so a 👀 stays when the draft is skipped or given up. All offline: a fake Slack,
a fake model."""
from __future__ import annotations

import ast
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from haseeb_agent.commands import process_dm
from haseeb_agent.jobs import feedback as fb
from haseeb_agent.jobs.feedback import FeedbackHandlers, draft_one, retry_reactions, run_feedback
from haseeb_agent.models import Msg
from haseeb_agent.slack_gateway import FileContent, SlackError, SlackPaused, SlackTimeout
from tests.fakes import FakeSlack, PausedSlack
from tests.test_feedback import (CH, DRAFT, HASEEB, IQBAL, LION, LONG, NOW, FakeLLM, Store, delivered, dm_sends, drafted_by_hand,  # noqa: F401
                                 keep, m, post, settings, store, ts_ago)
from tests.test_feedback_site_review import HTML_REF, LINK, PAGE, Renders, SiteLLM, renders  # noqa: F401

ALREADY = "slack_add_reaction: already_reacted"


class ScriptedReact(FakeSlack):
    """A fake Slack whose reactions follow a script: each entry is an exception to raise, or None to go through. An empty
    script means every reaction goes through."""

    def __init__(self):
        super().__init__()
        self.react_script: list[Exception | None] = []
        self.react_calls = 0

    def react(self, channel_id, ts, emoji):
        self.react_calls += 1
        if self.react_script:
            what = self.react_script.pop(0)
            if what is not None:
                raise what
        return super().react(channel_id, ts, emoji)


class ReactPaused(ScriptedReact):
    """Only the reactions are refused by the pause; the rest of the run goes through, which isolates what a refused
    reaction does (a real pause refuses every write, see test_the_pause_*_end_to_end)."""
    paused = True

    def react(self, channel_id, ts, emoji):
        if self.paused:
            self.react_calls += 1
            raise SlackPaused("slack_add_reaction: the agent is paused since 2026-10-19T08:00:00+03:00, nothing was sent")
        return super().react(channel_id, ts, emoji)


@pytest.fixture
def slack():
    return ScriptedReact()


def run(slack, llm, store, settings, now=NOW):
    return run_feedback(slack, llm, store, settings, now)


def on_post(slack, ts):
    """The reactions on the channel post `ts`, as (channel, emoji) in order."""
    return [(r.channel_id, r.emoji) for r in slack.reactions if r.ts == ts]


def emojis(slack):
    return [r.emoji for r in slack.reactions if r.channel_id == CH]


def cursor(store, kind, ts):
    return store.get_cursor(fb.react_key(kind, ts))


def reply(slack, item, text, n=5):
    """Haseeb's reply in the item's DM thread, as the DM job reads it."""
    parent = next(msg for msg in slack.pages[HASEEB] if msg.ts == item.dm_ts)
    at = int(NOW.timestamp()) + n
    parent.reply_count, parent.latest_reply, parent.thread_ts = 1, str(at), parent.ts
    cmd = Msg(ts=f"{at}.000009", user_id=HASEEB, user_name="Haseeb", text=text, thread_ts=item.dm_ts, reply_count=0,
              latest_reply=None)
    slack.add_thread(HASEEB, parent, [cmd])


def handlers(slack, store, settings):
    return {"feedback": FeedbackHandlers(slack, FakeLLM(), store, settings)}


# ---------------------------------------------------------------- 👀 when the draft starts

def test_a_draft_starts_with_an_eyes_on_the_teammates_post(slack, store, settings):
    t = ts_ago(600)
    post(slack, t)
    assert run(slack, FakeLLM(), store, settings) == 1
    assert [(r.channel_id, r.ts, r.emoji) for r in slack.reactions] == [(CH, t, "eyes")]
    assert cursor(store, "eyes", t) == "done" and cursor(store, "check", t) is None
    assert store.get_item(f"feedback-{t}").status == "delivered"


def test_the_eyes_go_on_before_the_files_the_site_review_and_the_models(slack, store, settings, renders, tmp_path):
    """Teammates see it while the agent works: it is the first thing the draft does, before the post's files are read, the
    site is rendered and any model is asked."""
    # the review keeps its screenshots next to the database: that is this test's folder, never the repository's data/
    settings = replace(settings, site_review_enabled=True, db_path=str(tmp_path / "data" / "agent.sqlite"))
    slack.files["F7"] = FileContent(text=PAGE, local_path=None, mimetype="text/html")
    journal: list[tuple[str, str]] = []
    real_react, real_read = slack.react, slack.read_file
    slack.react = lambda c, t, e: (journal.append(("react", e)), real_react(c, t, e))[1]
    slack.read_file = lambda fid: (journal.append(("read_file", fid)), real_read(fid))[1]
    link, html = renders.link, renders.html
    renders.link = lambda url, **kw: (journal.append(("render", url)), link(url, **kw))[1]
    renders.html = lambda text, **kw: (journal.append(("render", "html")), html(text, **kw))[1]
    llm = SiteLLM()
    real_complete = llm.complete
    llm.complete = lambda step, *a, **kw: (journal.append(("llm", step)), real_complete(step, *a, **kw))[1]
    post(slack, ts_ago(600), f"The demo is attached and live: {LINK}. " + LONG, files=[HTML_REF])
    assert run(slack, llm, store, settings) == 1
    assert journal[0] == ("react", "eyes")
    kinds = {k for k, _ in journal[1:]}
    assert {"read_file", "render", "llm"} <= kinds and ("react", "eyes") not in journal[1:]
    assert (tmp_path / "data" / "site-reviews" / f"feedback-{ts_ago(600)}" / "link1-desktop.jpg").is_file()  # kept in tmp


def test_the_eyes_are_added_once_per_post(slack, store, settings):
    t = ts_ago(600)
    post(slack, t)
    run(slack, FakeLLM(), store, settings)
    run(slack, FakeLLM(), store, settings)  # the post is handled: nothing more
    assert emojis(slack) == ["eyes"]
    keep(store, settings, post(slack, ts_ago(300)))
    other = ts_ago(300)
    drafted_by_hand(slack, store, settings, other)
    assert on_post(slack, other) == [(CH, "eyes")] and emojis(slack).count("eyes") == 2  # one per post


def test_a_draft_that_is_tried_again_keeps_its_one_eyes(slack, store, settings):
    t = ts_ago(600)
    post(slack, t)
    assert run(slack, FakeLLM(writer=[RuntimeError("model blew up")]), store, settings) == 0  # attempt 1 fails: pending
    assert emojis(slack) == ["eyes"] and store.get_item(f"feedback-{t}") is None
    assert run(slack, FakeLLM(), store, settings) == 1  # attempt 2 delivers
    assert emojis(slack) == ["eyes"] and cursor(store, "eyes", t) == "done"


def test_the_manual_draft_gets_the_eyes_too(slack, store, settings):
    t = ts_ago(600)
    keep(store, settings, post(slack, t))
    drafted_by_hand(slack, store, settings, t)  # `feedback --ts`, the dashboard's Draft feedback
    assert on_post(slack, t) == [(CH, "eyes")]


def test_a_post_that_already_has_a_draft_gets_nothing_from_a_second_try(slack, store, settings):
    t = ts_ago(600)
    keep(store, settings, post(slack, t))
    drafted_by_hand(slack, store, settings, t)
    with pytest.raises(fb.FeedbackRefused):
        drafted_by_hand(slack, store, settings, t)
    assert slack.react_calls == 1


def test_a_post_whose_draft_is_given_up_keeps_its_eyes_and_nothing_follows(slack, store, settings):
    t = ts_ago(600)
    post(slack, t)
    for _ in range(fb.MAX_FAILS):
        run(slack, FakeLLM(writer=[RuntimeError("down")]), store, settings)
    assert store.get_cursor(f"feedback_gaveup:{t}") == "1"
    assert emojis(slack) == ["eyes"] and slack.react_calls == 1  # no remove-reaction tool: it stays, and nothing is added


# ---------------------------------------------------------------- never on these posts

@pytest.mark.parametrize("who,reacts", [(IQBAL, True), (HASEEB, False), (LION, False), ("USLACKBOT", False)])
def test_who_gets_a_reaction(settings, who, reacts):
    assert fb._reacts_to(settings, m(ts_ago(60), LONG, user_id=who)) is reacts
    assert fb._reacts_to(settings, m(ts_ago(60), LONG, user_id=IQBAL, is_bot=True)) is False


def test_a_lions_post_drafted_on_request_gets_no_reaction_at_either_end(slack, store, settings):
    t = ts_ago(600)
    keep(store, settings, post(slack, t, user_id=LION, name="Lion"))
    drafted_by_hand(slack, store, settings, t)  # Haseeb asked for it; Lion's posts are never left out of a manual draft
    item = store.get_item(f"feedback-{t}")
    assert item is not None and slack.react_calls == 0
    assert cursor(store, "eyes", t) == "n/a" and cursor(store, "check", t) == "n/a"
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    assert slack.react_calls == 0 and emojis(slack) == []
    store.mark(item.id, "posted", acted_at=NOW)
    assert retry_reactions(slack, store, settings, NOW) == 0 and run(slack, FakeLLM(), store, settings) == 0
    assert slack.react_calls == 0 and cursor(store, "check", t) == "n/a"  # and nothing is pending either


def test_a_lions_post_the_tick_drafts_when_lion_is_included_gets_none_either(slack, store, settings):
    settings = replace(settings, feedback_include_lion=True)
    t = ts_ago(600)
    post(slack, t, user_id=LION, name="Lion")
    assert run(slack, FakeLLM(), store, settings) == 1
    assert slack.react_calls == 0 and cursor(store, "eyes", t) == "n/a"


# ---------------------------------------------------------------- reactions never fail a draft

def test_a_failed_eyes_does_not_fail_or_delay_the_draft(slack, store, settings, caplog):
    caplog.set_level(logging.DEBUG)
    t = ts_ago(600)
    post(slack, t)
    slack.react_script = [SlackError("slack_add_reaction: boom")]
    assert run(slack, FakeLLM(), store, settings) == 1  # delivered in the same run
    assert store.get_item(f"feedback-{t}").status == "delivered" and len(dm_sends(slack)) == 1
    assert store.get_cursor(f"feedback_fail:{t}") is None and store.get_cursor(f"feedback_gaveup:{t}") is None
    assert cursor(store, "eyes", t) == "failed:1"
    assert "reaction on" in caplog.text and "boom" not in caplog.text  # the type only, never the message


@pytest.mark.parametrize("what", [SlackError("boom"), SlackTimeout("slack_add_reaction: tool-call mismatch: timeout"),
                                  RuntimeError("anything at all"), ValueError("x")])
def test_whatever_a_reaction_raises_the_draft_still_comes(slack, store, settings, what):
    post(slack, ts_ago(600))
    slack.react_script = [what]
    assert run(slack, FakeLLM(), store, settings) == 1


def test_the_offline_guard_is_never_swallowed_by_a_reaction(slack, store, settings):
    from haseeb_agent.offline import OfflineError
    post(slack, ts_ago(600))
    slack.react_script = [OfflineError("a real step was reached")]
    with pytest.raises(OfflineError):
        fb._react(slack, store, settings, "eyes", ts_ago(600))


def test_a_store_that_cannot_record_a_reaction_does_not_fail_it_either(slack, store, settings, monkeypatch):
    post(slack, ts_ago(600))
    monkeypatch.setattr(store, "set_cursor", lambda *a: (_ for _ in ()).throw(RuntimeError("disk full")))
    fb._react(slack, store, settings, "eyes", ts_ago(600))  # no exception


def test_already_reacted_counts_as_done(slack, store, settings):
    t = ts_ago(600)
    post(slack, t)
    slack.react_script = [SlackError(ALREADY)]
    assert run(slack, FakeLLM(), store, settings) == 1
    assert cursor(store, "eyes", t) == "done" and slack.reactions == []
    assert retry_reactions(slack, store, settings, NOW) == 0 and slack.react_calls == 1  # nothing to try again


@pytest.mark.parametrize("text", ["slack_add_reaction: already_reacted", "Already reacted to that message",
                                  "slack_add_reaction: ALREADY_REACTED (the user has already added the emoji)"])
def test_already_reacted_is_recognised_in_the_connectors_wordings(slack, store, settings, text):
    slack.react_script = [SlackError(text)]
    fb._react(slack, store, settings, "eyes", "1.1")
    assert cursor(store, "eyes", "1.1") == "done"


# ---------------------------------------------------------------- one retry, never more

def test_a_failed_reaction_is_tried_once_more_and_then_given_up(slack, store, settings):
    t = ts_ago(600)
    fb._react(slack, store, settings, "eyes", t)  # goes through
    slack.reactions.clear()
    store.set_cursor(fb.react_key("eyes", t), "done")
    t2 = ts_ago(500)
    slack.react_script = [SlackError("boom 1"), SlackError("boom 2")]
    fb._react(slack, store, settings, "eyes", t2)
    assert cursor(store, "eyes", t2) == "failed:1"
    fb._react(slack, store, settings, "eyes", t2)  # the one retry
    assert cursor(store, "eyes", t2) == "failed:2"
    fb._react(slack, store, settings, "eyes", t2)  # never again
    assert slack.react_calls == 3 and slack.reactions == [] and cursor(store, "eyes", t2) == "failed:2"


def test_a_post_marked_as_getting_no_reactions_gets_none(slack, store, settings):
    for kind in ("eyes", "check"):
        store.set_cursor(fb.react_key(kind, "1.1"), "n/a")
        fb._react(slack, store, settings, kind, "1.1")
    assert slack.react_calls == 0 and cursor(store, "eyes", "1.1") == cursor(store, "check", "1.1") == "n/a"


def test_a_reaction_is_added_once_whatever_calls_for_it(slack, store, settings):
    for _ in range(3):
        fb._react(slack, store, settings, "check", "1.1")
        fb._react(slack, store, settings, "eyes", "1.1")
    assert [(r.channel_id, r.emoji) for r in slack.reactions] == [(CH, "white_check_mark"), (CH, "eyes")]


# ---------------------------------------------------------------- the pause

def test_a_reaction_the_pause_refused_is_added_after_resume_by_the_next_draft_attempt(store, settings):
    slack = ReactPaused()
    t = ts_ago(600)
    post(slack, t)
    assert run(slack, FakeLLM(), store, settings) == 1  # the pause refused the reaction; the draft itself went through
    assert slack.reactions == [] and cursor(store, "eyes", t) == "paused"  # refused is not failed: it is not counted
    slack.paused = False
    assert retry_reactions(slack, store, settings, NOW) == 1  # the item waits for Haseeb: the 👀 still makes sense
    assert on_post(slack, t) == [(CH, "eyes")] and cursor(store, "eyes", t) == "done"


def test_the_pause_end_to_end_a_real_pause_refuses_the_draft_too_and_resume_does_both(store, settings):
    slack = PausedSlack()
    t = ts_ago(600)
    post(slack, t)
    assert run(slack, FakeLLM(), store, settings) == 0  # the 👀 and the DM are both refused
    assert slack.reactions == [] and slack.sent == [] and store.get_item(f"feedback-{t}") is None
    assert cursor(store, "eyes", t) == "paused" and store.get_cursor(f"feedback_fail:{t}") is None
    slack.paused = False
    assert run(slack, FakeLLM(), store, settings) == 1  # after Resume the same post is drafted, with its 👀
    assert on_post(slack, t) == [(CH, "eyes")] and cursor(store, "eyes", t) == "done"


def test_a_paused_eyes_that_no_longer_makes_sense_is_dropped(store, settings):
    slack = ReactPaused()
    t = ts_ago(600)
    post(slack, t)
    run(slack, FakeLLM(), store, settings)
    slack.paused = False
    store.mark(f"feedback-{t}", "skipped")  # Haseeb skipped the draft while the agent was paused
    assert retry_reactions(slack, store, settings, NOW) == 0
    assert slack.reactions == [] and cursor(store, "eyes", t) == "n/a"


def test_a_check_the_pause_refused_is_added_after_resume_once_the_item_is_posted(store, settings):
    slack = ReactPaused()
    slack.paused = False
    t = ts_ago(600)
    post(slack, t)
    run(slack, FakeLLM(), store, settings)
    item = store.get_item(f"feedback-{t}")
    link = FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    assert link and cursor(store, "check", t) == "pending" and emojis(slack) == ["eyes"]
    store.mark(item.id, "posted", acted_at=NOW)  # what the command runner records
    slack.paused = True  # the pause starts just after the reply went out
    assert retry_reactions(slack, store, settings, NOW) == 1  # tried, and refused by the pause
    assert cursor(store, "check", t) == "paused" and emojis(slack) == ["eyes"]
    slack.paused = False
    assert retry_reactions(slack, store, settings, NOW) == 1
    assert emojis(slack) == ["eyes", "white_check_mark"] and cursor(store, "check", t) == "done"


# ---------------------------------------------------------------- ✅ when the reply is posted

def test_the_check_is_only_marked_pending_when_the_reply_goes_out_and_goes_on_once_it_is_recorded_as_posted(slack, store,
                                                                                                           settings):
    """M7: no Slack call between the channel send and the runner's record. `post` sends the reply and marks the ✅ as pending;
    the ✅ itself is put on by the next feedback run, after the item is recorded `posted`."""
    item = delivered(slack, store, settings)
    t = item.source_ts
    journal = []
    real_send, real_react = slack.send, slack.react
    slack.send = lambda *a, **kw: (journal.append("send"), real_send(*a, **kw))[1]
    slack.react = lambda *a, **kw: (journal.append("react"), real_react(*a, **kw))[1]
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    assert journal == ["send"]  # nothing after the send: no reaction call to hang or to be cut short by a crash
    assert on_post(slack, t) == [(CH, "eyes")] and cursor(store, "check", t) == "pending"
    assert retry_reactions(slack, store, settings, NOW) == 0  # the item is not recorded as posted yet: it waits
    assert journal == ["send"] and cursor(store, "check", t) == "pending"
    store.mark(item.id, "posted", acted_at=NOW)
    assert retry_reactions(slack, store, settings, NOW) == 1
    assert journal == ["send", "react"]  # only once the reply has gone out AND is recorded
    assert on_post(slack, t) == [(CH, "eyes"), (CH, "white_check_mark")] and cursor(store, "check", t) == "done"


def test_no_check_when_the_post_does_not_go_out(slack, store, settings):
    item = delivered(slack, store, settings)
    slack.fail_next("channel_not_found", method="send")
    with pytest.raises(SlackError):
        FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    assert emojis(slack) == ["eyes"] and cursor(store, "check", item.source_ts) is None


def test_no_check_on_a_skip(slack, store, settings):
    item = delivered(slack, store, settings)
    reply(slack, item, "skip")
    assert process_dm(slack, store, settings, handlers(slack, store, settings), NOW) == 1
    assert store.get_item(item.id).status == "skipped"
    assert emojis(slack) == ["eyes"] and cursor(store, "check", item.source_ts) is None  # the 👀 stays: nothing more


def test_a_post_from_the_dm_puts_the_check_on_the_post_and_not_on_the_dm(slack, store, settings):
    item = delivered(slack, store, settings)
    reply(slack, item, "post")
    journal = []
    real_mark, real_react, real_send = store.mark, slack.react, slack.send
    store.mark = lambda iid, status, **kw: (journal.append(f"mark:{status}"), real_mark(iid, status, **kw))[1]
    slack.react = lambda c, t, e: (journal.append(f"react:{c}:{t}"), real_react(c, t, e))[1]
    slack.send = lambda c, *a, **kw: (journal.append(f"send:{c}"), real_send(c, *a, **kw))[1]
    assert process_dm(slack, store, settings, handlers(slack, store, settings), NOW) == 1
    assert store.get_item(item.id).status == "posted"
    assert on_post(slack, item.source_ts) == [(CH, "eyes")] and cursor(store, "check", item.source_ts) == "pending"
    sent, posted_at = journal.index(f"send:{CH}"), journal.index("mark:posted")
    # between the channel send and the record there is no call to Slack at all, and no reaction on the teammate's post after it
    assert [j for j in journal[sent + 1:posted_at]] == []
    assert not [j for j in journal if j.startswith("react:") and j.endswith(item.source_ts)]
    assert run(slack, FakeLLM(), store, settings) == 0  # the tick's feedback run
    assert on_post(slack, item.source_ts) == [(CH, "eyes"), (CH, "white_check_mark")]
    dm = [(r.ts, r.emoji) for r in slack.reactions if r.channel_id == settings.self_dm_channel]
    assert dm and all(ts != item.source_ts for ts, _ in dm)  # the command's own marks are on the DM, as before


def test_a_failed_check_does_not_fail_the_post(slack, store, settings):
    item = delivered(slack, store, settings)
    link = FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)  # returns the reply's permalink as usual
    assert f"thread_ts={item.source_ts}" in link
    store.mark(item.id, "posted", acted_at=NOW)
    slack.react_script = [SlackError("slack_add_reaction: boom")]
    assert retry_reactions(slack, store, settings, NOW) == 1  # the ✅ fails in the next run, long after the post
    assert cursor(store, "check", item.source_ts) == "failed:1"
    assert store.get_item(item.id).status == "posted"
    assert store.get_cursor(f"post_failed:{item.id}") is None and store.get_cursor(f"send_started:{item.id}") == ""


def test_a_failed_check_command_end_to_end_the_post_is_posted_and_the_row_is_done(slack, store, settings):
    item = delivered(slack, store, settings)
    reply(slack, item, "post")
    assert process_dm(slack, store, settings, handlers(slack, store, settings), NOW) == 1
    assert store.get_item(item.id).status == "posted"  # the post itself made no reaction call that could fail it
    slack.react_script = [SlackError("slack_add_reaction: boom")]  # the ✅, in the next feedback run, fails
    run(slack, FakeLLM(), store, settings)
    assert store.get_item(item.id).status == "posted"  # never marked failed because of a reaction
    assert emojis(slack).count("white_check_mark") == 0 and cursor(store, "check", item.source_ts) == "failed:1"
    assert store.get_cursor(f"post_failed:{item.id}") is None


def test_the_failed_check_gets_its_one_retry_on_the_next_feedback_run(slack, store, settings):
    item = delivered(slack, store, settings)
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    store.mark(item.id, "posted", acted_at=NOW)  # what the command runner records
    slack.react_script = [SlackError("boom")]
    assert run(slack, FakeLLM(), store, settings) == 0  # the tick's feedback run: nothing new to draft; the ✅ fails
    assert emojis(slack) == ["eyes"] and cursor(store, "check", item.source_ts) == "failed:1"
    assert run(slack, FakeLLM(), store, settings) == 0  # the one more try
    assert emojis(slack) == ["eyes", "white_check_mark"] and cursor(store, "check", item.source_ts) == "done"
    slack.react_calls = 0
    run(slack, FakeLLM(), store, settings)
    assert slack.react_calls == 0  # never again


def test_the_retry_also_happens_when_the_channel_cannot_be_read(slack, store, settings):
    item = delivered(slack, store, settings)
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    store.mark(item.id, "posted", acted_at=NOW)
    slack.fail_next("down", method="read_channel")
    assert run(slack, FakeLLM(), store, settings) == 0
    assert cursor(store, "check", item.source_ts) == "done"


def test_a_check_that_fails_twice_is_given_up(slack, store, settings):
    item = delivered(slack, store, settings)
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    store.mark(item.id, "posted", acted_at=NOW)
    slack.react_script = [SlackError("boom 1"), SlackError("boom 2")]
    run(slack, FakeLLM(), store, settings)
    run(slack, FakeLLM(), store, settings)
    assert cursor(store, "check", item.source_ts) == "failed:2"
    slack.react_calls = 0
    run(slack, FakeLLM(), store, settings)
    assert slack.react_calls == 0 and "white_check_mark" not in emojis(slack)


# ---------------------------------------------------------------- the author rule at the end too (security review M6)

def test_a_draft_from_before_reactions_existed_gets_no_check_when_it_is_posted_whoever_wrote_the_post(slack, store, settings):
    """An item drafted before this feature has no `fb_react:` cursor. Posting it used to put a ✅ on its source post whoever
    wrote it (a Lion post drafted by hand included): now `post` marks nothing for a post whose 👀 cursor is not there."""
    item = delivered(slack, store, settings)
    t = item.source_ts
    for kind in ("eyes", "check"):
        store._db.execute("DELETE FROM cursors WHERE key=?", (fb.react_key(kind, t),))
    store._db.commit()
    slack.reactions.clear()
    slack.react_calls = 0
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    store.mark(item.id, "posted", acted_at=NOW)
    assert cursor(store, "check", t) is None and cursor(store, "eyes", t) is None  # nothing pending
    assert retry_reactions(slack, store, settings, NOW) == 0 and run(slack, FakeLLM(), store, settings) == 0
    assert slack.react_calls == 0 and slack.reactions == []


def test_a_lions_post_drafted_before_reactions_existed_gets_no_check_either(slack, store, settings):
    t = ts_ago(600)
    keep(store, settings, post(slack, t, user_id=LION, name="Lion"))
    drafted_by_hand(slack, store, settings, t)
    item = store.get_item(f"feedback-{t}")
    store._db.execute("DELETE FROM cursors WHERE key LIKE 'fb_react:%'")  # a draft made by the code before this branch
    store._db.commit()
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    store.mark(item.id, "posted", acted_at=NOW)
    run(slack, FakeLLM(), store, settings)
    assert slack.react_calls == 0 and cursor(store, "check", t) is None


def test_the_rule_is_the_eyes_cursor_itself_a_post_marked_na_gets_no_pending_check_even_if_the_check_cursor_is_unset(
        slack, store, settings):
    item = delivered(slack, store, settings)
    store.set_cursor(fb.react_key("eyes", item.source_ts), "n/a")
    store._db.execute("DELETE FROM cursors WHERE key=?", (fb.react_key("check", item.source_ts),))
    store._db.commit()
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    assert cursor(store, "check", item.source_ts) is None


@pytest.mark.parametrize("eyes", ["done", "paused", "failed:1", "failed:2"])
def test_the_check_is_pending_for_any_post_that_was_marked_for_a_reaction(slack, store, settings, eyes):
    item = delivered(slack, store, settings)
    store.set_cursor(fb.react_key("eyes", item.source_ts), eyes)
    store.set_cursor(fb.react_key("check", item.source_ts), "")  # (an empty cursor reads as not set)
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    assert cursor(store, "check", item.source_ts) == "pending"


@pytest.mark.parametrize("state", ["done", "n/a", "failed:1", "failed:2", "paused"])
def test_marking_the_check_pending_leaves_a_state_that_is_already_set_alone(slack, store, settings, state):
    item = delivered(slack, store, settings)
    store.set_cursor(fb.react_key("check", item.source_ts), state)
    fb._expect_check(store, item.source_ts)
    assert cursor(store, "check", item.source_ts) == state


def test_a_store_that_cannot_record_the_pending_check_does_not_fail_a_post_that_has_gone_out(slack, store, settings, monkeypatch,
                                                                                         caplog):
    item = delivered(slack, store, settings)
    real = store.get_cursor
    monkeypatch.setattr(store, "get_cursor", lambda key: (_ for _ in ()).throw(RuntimeError("disk full"))
                        if key.startswith(fb.REACT_PREFIX) else real(key))
    link = FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)  # no exception: the reply is out
    assert f"thread_ts={item.source_ts}" in link and "pending" in caplog.text and "disk full" not in caplog.text


# ---------------------------------------------------------------- the pending check waits for the record (security review M7)

def test_a_pending_check_waits_while_the_item_is_not_recorded_as_posted_and_is_dropped_when_it_never_will_be(slack, store,
                                                                                                           settings):
    item = delivered(slack, store, settings)
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    for _ in range(3):  # (the runner has not recorded the post yet: this run reads the item as delivered)
        assert retry_reactions(slack, store, settings, NOW) == 0
    assert cursor(store, "check", item.source_ts) == "pending" and slack.react_calls == 1  # only the 👀
    store.mark(item.id, "failed")  # the post was cut short after the send (the watchdog, a crash): the runner calls it failed
    assert retry_reactions(slack, store, settings, NOW) == 0
    assert cursor(store, "check", item.source_ts) == "n/a" and slack.react_calls == 1


def test_a_pending_check_for_a_reply_nobody_recorded_for_days_is_dropped(slack, store, settings):
    item = delivered(slack, store, settings)
    FeedbackHandlers(slack, FakeLLM(), store, settings).post(item)
    store.mark(item.id, "posted", acted_at=NOW)
    assert retry_reactions(slack, store, settings, NOW + timedelta(days=4)) == 0  # older than the retry window
    assert cursor(store, "check", item.source_ts) == "n/a" and slack.react_calls == 1


def test_a_pending_check_is_added_by_the_tick_that_runs_after_the_post_even_when_nothing_else_happens(slack, store, settings):
    item = delivered(slack, store, settings)
    reply(slack, item, "post")
    process_dm(slack, store, settings, handlers(slack, store, settings), NOW)
    slack.fail_next("down", method="read_channel")  # the channel cannot even be read
    assert run(slack, FakeLLM(), store, settings) == 0
    assert on_post(slack, item.source_ts) == [(CH, "eyes"), (CH, "white_check_mark")]


# ---------------------------------------------------------------- the retry's limits

def test_a_draft_from_before_reactions_existed_is_never_touched(slack, store, settings):
    item = delivered(slack, store, settings)
    for kind in ("eyes", "check"):
        store._db.execute("DELETE FROM cursors WHERE key=?", (fb.react_key(kind, item.source_ts),))
    store._db.commit()
    store.mark(item.id, "posted", acted_at=NOW)
    slack.reactions.clear()
    assert retry_reactions(slack, store, settings, NOW) == 0 and slack.reactions == []


def test_the_retry_does_at_most_two_a_run_and_only_for_young_drafts(slack, store, settings):
    tss = []
    for i in range(4):
        t = ts_ago(900 - i * 10)
        tss.append(t)
        post(slack, t, name=f"Teammate {i}", user_id=f"U0AB{i}CDEF")
    run(slack, FakeLLM(), store, replace(settings, feedback_include_lion=False))
    for t in tss:
        item = store.get_item(f"feedback-{t}")
        if item is None:
            continue
        store.mark(item.id, "posted", acted_at=NOW)
        store.set_cursor(fb.react_key("check", t), "failed:1")
    marked = [t for t in tss if store.get_item(f"feedback-{t}") is not None]
    assert len(marked) >= 3
    slack.react_calls = 0
    assert retry_reactions(slack, store, settings, NOW) == fb.MAX_REACT_RETRIES_PER_RUN == 2
    assert slack.react_calls == 2
    old = NOW + timedelta(days=5)
    stale = [t for t in marked if cursor(store, "check", t) == "failed:1"]
    assert retry_reactions(slack, store, settings, old) == 0  # a draft that old: no reaction makes sense any more
    assert all(cursor(store, "check", t) == "n/a" for t in stale)


def test_a_reaction_still_being_decided_for_a_post_without_an_item_is_left_alone(slack, store, settings, caplog):
    caplog.set_level(logging.DEBUG)
    store.set_cursor(fb.react_key("eyes", "1.1"), "paused")  # the draft is still to be made: its own attempt will do it
    assert retry_reactions(slack, store, settings, NOW) == 0 and slack.react_calls == 0
    assert cursor(store, "eyes", "1.1") == "paused" and "retry failed" not in caplog.text  # skipped, not an error


def test_a_check_waits_while_its_item_is_not_yet_posted(slack, store, settings):
    item = delivered(slack, store, settings)
    store.set_cursor(fb.react_key("check", item.source_ts), "paused")
    assert retry_reactions(slack, store, settings, NOW) == 0
    assert cursor(store, "check", item.source_ts) == "paused"  # waits for the reply to be posted
    store.mark(item.id, "skipped")
    assert retry_reactions(slack, store, settings, NOW) == 0
    assert cursor(store, "check", item.source_ts) == "n/a"  # it never will be


def test_a_marker_that_is_not_ours_is_ignored(slack, store, settings):
    store.set_cursor("fb_react:smile:1.1", "paused")
    store.set_cursor("fb_react:eyes", "paused")
    assert retry_reactions(slack, store, settings, NOW) == 0 and slack.react_calls == 0


# ---------------------------------------------------------------- where reactions are made

def test_reactions_are_made_only_where_they_are_designed_to_be():
    """`_react` is called from the start of a draft and from the retry, and nowhere else (not from `post`, which only marks the
    ✅ as pending: M7); and it is the one place in the module that calls `slack.react`."""
    tree = ast.parse(Path(fb.__file__).read_text(encoding="utf-8"))
    callers: list[str] = []

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.ClassDef)) else owner
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "_react":
                callers.append(owner)
            visit(child, name)
    visit(tree, "<module>")
    assert sorted(callers) == ["_draft_and_send", "retry_reactions"]
    react_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "react"]
    assert len(react_calls) == 1  # `slack.react(...)`, inside `_react` only
