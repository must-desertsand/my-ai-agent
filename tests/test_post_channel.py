"""Task 2 of docs/superpowers/plans/2026-10-22-post-channel.md: an idea or a message of Haseeb's is posted to the channel he
chooses, and nothing else changes where anything is posted.

  - the DM: `post`, `post #name`, `post <#ID|name>` and `post <#ID>` (Slack's markup for a channel picked by autocomplete); a bare
    word after `post` (`post it`, `post now`, `post survive-ai`) is chat, not a command; the dashboard: the page command `post`
    carries a channel id;
  - the runner looks the channel up again just before the send: a name that is not in the list, a page that is out of date, a
    channel Haseeb has left, a DM or user id, and a feedback or Next Todo draft with a channel are each refused with one short line,
    nothing sent, nothing marked, the draft untouched and never locked (R16 locks only a post that may have gone out);
  - the channel is recorded BEFORE the send (cursor `post_channel:<item id>`), so a post that may have gone out names where
    ("check #survive-ai"), and a retry after a possible post is pinned to it;
  - one post per draft; the pause refuses; and every reader of a post (the permalink, the page's link, the deck, the thread watch)
    asks `channels.post_channel`, which says the leadership channel for a post made before the choice existed.

Names and ids are made up, except the leadership channel and #survive-ai, which are the real ones from the design.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import channels, commands
from haseeb_agent.channels import Channel
from haseeb_agent.commands import Command, mark_post_failed, parse_command, process_dm, recover_interrupted
from haseeb_agent.config import load_settings
from haseeb_agent.jobs.feedback import FeedbackHandlers
from haseeb_agent.jobs.insights import InsightHandlers
from haseeb_agent.jobs.message import MessageHandlers
from haseeb_agent.jobs.todo import TodoHandlers
from haseeb_agent.models import Item, Msg
from haseeb_agent.page_commands import body_hash, process_page
from haseeb_agent.render import FOOTER
from haseeb_agent.slack_gateway import SlackError
from haseeb_agent.store import Store
from tests.fakes import FakeSlack, PausedSlack
from tests.test_insights import good_idea_meta

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")
HASEEB = "U08CZCGJW3E"
LEAD = "C0C62C128D8"
SURVIVE_ID = "C093L3Y8UM6"
OPS_ID = "G01EEEEEEEE"
LEAD_CH = Channel(LEAD, "tf-leadership-q4-2026", False)
SURVIVE = Channel(SURVIVE_ID, "survive-ai", False)
OPS = Channel(OPS_ID, "ops_eu.alerts", True)
CHATTER = Channel("C01AAAAAAAA", "chat-random", False)
LIST = [LEAD_CH, SURVIVE, OPS, CHATTER]
NOW = datetime.fromtimestamp(1_800_000_500, KW)
DAY = NOW.date()
PARENT = "1800000100.000001"        # the item's card in the DM
FIRST = "1800000200.000100"         # Haseeb's first reply in its thread
LATER = "1800000600.000100"         # a reply after NOW: an informed one, after a failure recorded at NOW
CLICK = datetime.fromtimestamp(1_800_000_200, KW)
LISTED = f"list from {DAY.isoformat()}"   # the date of the list the tests cache
ZWSP = chr(0x200B)  # what the composer puts after an @ that he did not type, so it cannot ping
POLISHED = "Standup moved to 3 pm. Please update your notes before then: https://example.com/notes"
LEADERSHIP_ONLY = "this draft posts only to its thread in the leadership channel"
GONE = "⚠️ Post may have gone out — check {where} before re-posting ("
TODO_BODY = "Next Todo:\n1. Ship the auto-post.\n2. Share the prototype plan with Lion.\n3. Post an insight daily with my AI agent."


@pytest.fixture(autouse=True)
def pinned_wall(monkeypatch):
    """These tests run on fixed clocks (January 2027); the real wall clock must not leak into failure times."""
    monkeypatch.setattr(commands, "_wall", lambda: 0.0)


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agent.sqlite")


@pytest.fixture
def slack():
    return FakeSlack()


# ---------------------------------------------------------------- the world

def know(store, slack, *chans, at=NOW):
    """Cache the channel list the way the tick does: through the gateway's `list_channels` and `channels.refresh`."""
    slack.channels = list(chans)
    assert channels.refresh(slack, store, at, force=True)


def idea(store, *, id="insight-2027-01-15-1", status="delivered", dm_ts=PARENT) -> Item:
    meta = good_idea_meta(n=1, batch=id)
    item = Item(id=id, kind="insight", day=DAY, status=status, body=json.dumps(meta), title="Topic alpha", dm_ts=dm_ts,
                source_ts=None, score=90.0, models={}, attempts=0, edited=False, created_at=NOW, acted_at=None)
    store.upsert_item(item)
    return item


def message(store, *, id="message-1", status="delivered", body=POLISHED) -> Item:
    item = Item(id=id, kind="message", day=DAY, status=status, body=body, title="Message to the channel", dm_ts=PARENT,
                source_ts=None, score=None, models={}, attempts=0, edited=False, created_at=NOW, acted_at=None)
    store.upsert_item(item)
    return item


def feedback(store, *, id="feedback-1800000050.000001", dm_ts=PARENT) -> Item:
    item = Item(id=id, kind="feedback", day=DAY, status="delivered", body="Nice demo. One thing: the login is slow.",
                title="Feedback: Iqbal", dm_ts=dm_ts, source_ts="1800000050.000001", score=90.0, models={}, attempts=0,
                edited=False, created_at=NOW, acted_at=None)
    store.upsert_item(item)
    return item


def todo(store, *, id="todo-2027-01-15") -> Item:
    item = Item(id=id, kind="todo", day=DAY, status="delivered", body=TODO_BODY, title="Next Todo", dm_ts=PARENT,
                source_ts="1800000040.000001", score=None, models={}, attempts=0, edited=False, created_at=NOW, acted_at=None)
    store.upsert_item(item)
    return item


def real_handlers(slack, store, settings):
    return {"insight": InsightHandlers(slack, None, store, settings), "message": MessageHandlers(slack, None, store, settings),
            "feedback": FeedbackHandlers(slack, None, store, settings), "todo": TodoHandlers(slack, None, store, settings)}


def reply_in_thread(slack, settings, text, ts=FIRST):
    """Haseeb's reply in the item's DM thread (the card is made on the first call)."""
    key = (settings.self_dm_id, PARENT)
    if key not in slack.threads:
        parent = slack.add_message(settings.self_dm_id, PARENT, "card", user_id=HASEEB)
        slack.add_thread(settings.self_dm_id, parent, [])
    parent, replies = slack.threads[key]
    parent.thread_ts, parent.reply_count, parent.latest_reply = PARENT, parent.reply_count + 1, str(int(float(ts)))
    slack.threads[key] = (parent, [*replies, Msg(ts=ts, user_id=HASEEB, user_name="Haseeb", text=text, thread_ts=PARENT,
                                                 reply_count=0, latest_reply=None)])


def dm(slack, store, settings, text, *, ts=FIRST, now=NOW, handlers=None):
    """Haseeb replies `text` under the item's card; the tick's DM pass runs it."""
    reply_in_thread(slack, settings, text, ts)
    return process_dm(slack, store, settings, handlers or real_handlers(slack, store, settings), now)


def click(slack, store, settings, item_id, arg=None, *, handlers=None, at=CLICK, now=NOW, verb="post"):
    """The dashboard's click, queued as a page command and run by the tick. Returns the row."""
    item = store.get_item(item_id)
    cid = store.enqueue_page_command(item_id, verb, arg, body_hash(item), at)
    process_page(slack, store, settings, handlers or real_handlers(slack, store, settings), now)
    return store.page_command(cid)


def dm_texts(slack, settings):
    return [s.text for s in slack.sent if s.channel_id == settings.self_dm_id]


def channel_sends(slack, settings):
    return [s for s in slack.sent if s.channel_id != settings.self_dm_id]


def link_in(channel_id, ts):
    return f"https://mustcompany.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def untouched(store, item_id, status="delivered"):
    """A refusal before any send leaves the item as it was: its status, no attempt, no failure time, no marker, no target."""
    item = store.get_item(item_id)
    assert (item.status, item.attempts) == (status, 0)
    for key in (f"post_failed_at:{item_id}", f"inflight:{item_id}", f"send_started:{item_id}", f"post_channel:{item_id}",
                f"permalink:{item_id}", f"post_ts:{item_id}"):
        assert not store.get_cursor(key), key


# ---------------------------------------------------------------- the DM command

@pytest.mark.parametrize("text,arg", [
    ("post", None), (" POST\n", None),
    ("post #survive-ai", "#survive-ai"), ("Post   #Survive-AI  ", "#Survive-AI"), ("post #x", "#x"),
    ("post <#C093L3Y8UM6|survive-ai>", "<#C093L3Y8UM6|survive-ai>"), ("post <#C093L3Y8UM6>", "<#C093L3Y8UM6>"),
])
def test_post_takes_a_channel_written_with_a_hash_or_as_slacks_markup(text, arg):
    assert parse_command(text) == Command("post", arg)


@pytest.mark.parametrize("text", [
    "post it", "post now", "post it now", "post survive-ai", "post C093L3Y8UM6", "post #", "post <#>",
    "post to #survive-ai", "posting", "post: hi", "posted", "post #a #b", "post\n#a\n#b", "please post",
    "post <https://example.com/a|b>", "post <@U08CZCGJW3E>",
])
def test_anything_else_after_post_is_ordinary_chat(text):
    """Without a `#` or Slack's channel markup, a word after `post` is chat, exactly as before the choice existed: a reply like
    "post it" must not become a command (it would cancel the Next Todo's auto-post without the notice, and a channel called #it
    would be posted to with no confirm)."""
    assert parse_command(text) is None


@pytest.mark.parametrize("text", ["post survive-ai", "post it", "post now", "post C093L3Y8UM6", "post tf-leadership-q4-2026"])
def test_a_bare_word_after_post_in_the_dm_is_chat_the_agent_does_not_answer(slack, store, settings, text):
    know(store, slack, *LIST)
    item = idea(store)
    assert dm(slack, store, settings, text) == 0
    assert slack.sent == [] and slack.reactions == []  # not a command: no reply, not even a reaction
    untouched(store, item.id)
    dm(slack, store, settings, "post #survive-ai", ts="1800000300.000100", now=NOW + timedelta(minutes=1))
    assert [s.channel_id for s in channel_sends(slack, settings)] == [SURVIVE_ID]  # and the draft posts when he asks properly


def test_the_post_handler_protocol_takes_a_channel():
    assert list(inspect.signature(commands.Handlers.post).parameters) == ["self", "item", "reply_ts", "channel_id"]
    for h in (InsightHandlers, MessageHandlers, FeedbackHandlers, TodoHandlers):
        params = inspect.signature(h.post).parameters
        assert list(params) == ["self", "item", "reply_ts", "channel_id"], h
        assert params["reply_ts"].default is None and params["channel_id"].default is None


def test_a_plain_post_goes_to_the_leadership_channel_and_says_so(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    assert dm(slack, store, settings, "post") == 1
    (sent,) = channel_sends(slack, settings)
    assert (sent.channel_id, sent.thread_ts) == (LEAD, None) and sent.text.endswith(FOOTER)
    assert store.get_cursor(f"post_channel:{item.id}") == LEAD  # recorded for every post, the default too
    assert store.get_cursor(f"post_ts:{item.id}") == sent.ts
    assert store.get_cursor(f"permalink:{item.id}") == link_in(LEAD, sent.ts)
    assert dm_texts(slack, settings) == [f"✅ Posted · {link_in(LEAD, sent.ts)}"]
    assert store.get_item(item.id).status == "posted"


@pytest.mark.parametrize("text", [
    "post #survive-ai", "POST #Survive-AI", "post <#C093L3Y8UM6|survive-ai>", "post <#C093L3Y8UM6>",
    "post <#C093L3Y8UM6|an-old-name>", "post <#c093l3y8um6>",
])
def test_post_to_a_named_channel_posts_the_idea_there_and_only_there(slack, store, settings, text):
    know(store, slack, *LIST)
    item = idea(store)
    assert dm(slack, store, settings, text) == 1
    (sent,) = channel_sends(slack, settings)
    assert (sent.channel_id, sent.thread_ts) == (SURVIVE_ID, None)  # a new top-level message of its own
    assert sent.text.endswith(FOOTER)  # the same text as in the leadership channel, footer included
    assert store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID and store.get_cursor(f"post_ts:{item.id}") == sent.ts
    link = link_in(SURVIVE_ID, sent.ts)  # the link is in the channel it went to, with no thread query
    assert store.get_cursor(f"permalink:{item.id}") == link
    assert dm_texts(slack, settings) == [f"✅ Posted to #survive-ai · {link}"]  # a channel that is not the usual one is named
    assert store.get_item(item.id).status == "posted"
    assert [s.channel_id for s in slack.sent].count(LEAD) == 0  # nothing went to the leadership channel


def test_a_private_channel_is_a_target_like_any_other(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    dm(slack, store, settings, "post <#G01EEEEEEEE|ops_eu.alerts>")
    (sent,) = channel_sends(slack, settings)
    assert sent.channel_id == OPS_ID and store.get_cursor(f"post_channel:{item.id}") == OPS_ID
    assert dm_texts(slack, settings) == [f"✅ Posted to #ops_eu.alerts · {link_in(OPS_ID, sent.ts)}"]


def test_the_leadership_channel_by_name_is_a_valid_choice(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    dm(slack, store, settings, "post #tf-leadership-q4-2026")
    (sent,) = channel_sends(slack, settings)
    assert sent.channel_id == LEAD and store.get_cursor(f"post_channel:{item.id}") == LEAD
    assert dm_texts(slack, settings) == [f"✅ Posted · {link_in(LEAD, sent.ts)}"]  # the leadership channel's line is as it always was


def test_the_leadership_channel_is_a_target_even_when_there_is_no_list(slack, store, settings):
    item = idea(store)  # nothing cached: only the leadership channel is a target, by its id
    dm(slack, store, settings, f"post <#{LEAD}>")
    assert [s.channel_id for s in channel_sends(slack, settings)] == [LEAD] and store.get_item(item.id).status == "posted"


def test_a_message_of_his_own_goes_to_the_chosen_channel_as_it_would_go_to_the_leadership_channel(slack, store, settings):
    know(store, slack, *LIST)
    item = message(store)
    dm(slack, store, settings, "post #survive-ai")
    (sent,) = channel_sends(slack, settings)
    assert (sent.channel_id, sent.thread_ts, sent.text) == (SURVIVE_ID, None, POLISHED)  # no footer, no note, no thread
    assert store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID
    assert dm_texts(slack, settings) == [f"✅ Posted to #survive-ai · {link_in(SURVIVE_ID, sent.ts)}"]


def test_the_composers_rules_hold_in_any_channel(slack, store, settings):
    """The link guard and "@channel only when he typed it" are unchanged: the text is built before the channel is looked at."""
    know(store, slack, *LIST)
    item = message(store, body="@channel Standup moved to 3 pm. @here be on time")
    store.set_cursor(f"message_raw:{item.id}", "@channel standup is at 3pm")
    dm(slack, store, settings, "post #survive-ai")
    (sent,) = channel_sends(slack, settings)
    assert sent.channel_id == SURVIVE_ID and sent.text == f"<!channel> Standup moved to 3 pm. @{ZWSP}here be on time"


# ---------------------------------------------------------------- refusals: nothing sent, nothing marked, never locked

@pytest.mark.parametrize("text,shown", [
    ("post #x", "#x"), ("post #Nope-AI", "#Nope-AI"),
    ("post <#C0UNKNOWN99|nope>", "#nope"), ("post <#C0UNKNOWN99>", "C0UNKNOWN99"),
    ("post <#D08D9HFB8BB>", "D08D9HFB8BB"), ("post <#U08CZCGJW3E|someone>", "#someone"),  # a DM and a user: never a target
    ("post #" + "a" * 81, "that"), ("post #" + "<b>", "that"), ("post #<script>alert(1)</script>", "that"),
    ("post #survıve-ai", "that"),  # a look-alike (a dotless i) is neither matched nor repeated
])
def test_an_unknown_name_gets_one_short_reply_and_nothing_is_posted(slack, store, settings, text, shown):
    know(store, slack, *LIST)
    item = idea(store)
    assert dm(slack, store, settings, text) == 1
    assert channel_sends(slack, settings) == []
    assert dm_texts(slack, settings) == [f"⚠️ {shown} is not one of your channels ({LISTED}) · nothing posted"]  # one reply
    untouched(store, item.id)
    assert [r.emoji for r in slack.reactions] == ["eyes", "warning"]


def test_a_refusal_never_locks_the_draft(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    dm(slack, store, settings, "post #x", ts=FIRST)
    assert dm(slack, store, settings, "post #x", ts="1800000300.000100", now=NOW + timedelta(minutes=1)) == 1  # again: refused again
    assert len(channel_sends(slack, settings)) == 0
    dm(slack, store, settings, "post #survive-ai", ts="1800000400.000100", now=NOW + timedelta(minutes=2))
    assert [s.channel_id for s in channel_sends(slack, settings)] == [SURVIVE_ID]  # and a good one posts, as the first would
    assert store.get_item(item.id).status == "posted"


def test_the_refused_command_is_handled_once_and_not_run_again(slack, store, settings):
    know(store, slack, *LIST)
    idea(store)
    dm(slack, store, settings, "post #x")
    assert process_dm(slack, store, settings, real_handlers(slack, store, settings), NOW + timedelta(minutes=1)) == 0
    assert len(dm_texts(slack, settings)) == 1


def test_with_no_list_yet_the_reply_says_so(slack, store, settings):
    item = idea(store)
    dm(slack, store, settings, "post #survive-ai")
    assert channel_sends(slack, settings) == []
    assert dm_texts(slack, settings) == ["⚠️ #survive-ai is not one of your channels (no list yet) · nothing posted"]
    untouched(store, item.id)


def test_a_name_in_two_channels_or_that_is_also_an_id_is_not_guessed(slack, store, settings):
    know(store, slack, LEAD_CH, Channel("C01AAAAAAAA", "chat-random", False), Channel("C01BBBBBBBB", "chat-random", False))
    item = idea(store)
    dm(slack, store, settings, "post #chat-random")
    assert channel_sends(slack, settings) == [] and dm_texts(slack, settings)[0].startswith("⚠️ #chat-random is not one of your")
    untouched(store, item.id)


@pytest.mark.parametrize("kind", ["feedback", "todo"])
@pytest.mark.parametrize("text", ["post #x", "post #survive-ai", "post <#C093L3Y8UM6|survive-ai>", "post #tf-leadership-q4-2026"])
def test_a_feedback_draft_and_the_todo_post_only_to_their_thread(slack, store, settings, kind, text):
    know(store, slack, *LIST)
    item = feedback(store) if kind == "feedback" else todo(store)
    assert dm(slack, store, settings, text) == 1
    assert slack.sent and channel_sends(slack, settings) == []  # nothing in any channel
    assert dm_texts(slack, settings) == [f"⚠️ {LEADERSHIP_ONLY}"]
    untouched(store, item.id)


def test_a_feedback_draft_still_posts_to_its_thread_after_a_refused_channel(slack, store, settings):
    know(store, slack, *LIST)
    item = feedback(store)
    dm(slack, store, settings, "post #survive-ai")
    dm(slack, store, settings, "post", ts="1800000300.000100", now=NOW + timedelta(minutes=1))
    (sent,) = channel_sends(slack, settings)
    assert (sent.channel_id, sent.thread_ts) == (LEAD, item.source_ts)  # a reply in the teammate's thread, as ever
    assert store.get_item(item.id).status == "posted" and not store.get_cursor(f"post_channel:{item.id}")


def test_the_handlers_of_feedback_and_the_todo_refuse_a_channel_themselves_with_a_plain_value_error(slack, store, settings):
    for item, handler in ((feedback(store), FeedbackHandlers(slack, None, store, settings)),
                          (todo(store), TodoHandlers(slack, None, store, settings))):
        with pytest.raises(ValueError, match=LEADERSHIP_ONLY) as refused:
            handler.post(item, channel_id=SURVIVE_ID)
        assert not isinstance(refused.value, commands.Unsettled)  # see the next test: an Unsettled would leave `inflight:` behind
        assert slack.sent == [] and not store.get_cursor(f"send_started:{item.id}")


@pytest.mark.parametrize("kind", ["feedback", "todo"])
def test_a_channel_that_got_past_the_runner_to_a_feedback_or_todo_handler_leaves_no_marker_and_no_lock(
        slack, store, settings, monkeypatch, kind):
    """The handlers' own refusal is a backstop: `post_target` refuses first, so it is not reached today. If it ever were, it runs
    after `inflight:` is set, so it must be a plain ValueError, which the runner clears with the command, and not an `Unsettled`,
    which leaves the marker for `recover_interrupted` to turn into a failed draft with a failure time (a lock)."""
    know(store, slack, *LIST)
    item = feedback(store) if kind == "feedback" else todo(store)
    real = commands.post_target
    monkeypatch.setattr(commands, "post_target", lambda store, settings, item, arg: SURVIVE_ID)  # the runner's check, bypassed
    dm(slack, store, settings, "post #survive-ai")
    assert dm_texts(slack, settings) == [f"⚠️ {LEADERSHIP_ONLY}"] and channel_sends(slack, settings) == []
    for key in (f"inflight:{item.id}", f"post_failed_at:{item.id}", f"send_started:{item.id}", f"post_channel:{item.id}"):
        assert not store.get_cursor(key), key
    recover_interrupted(slack, store, settings, NOW + timedelta(minutes=1))  # there is nothing left for it to find
    assert len(dm_texts(slack, settings)) == 1 and not store.get_cursor(f"post_failed_at:{item.id}")
    monkeypatch.setattr(commands, "post_target", real)
    dm(slack, store, settings, "post", ts="1800000300.000100", now=NOW + timedelta(minutes=2))  # not locked: a plain post posts
    (sent,) = channel_sends(slack, settings)
    assert sent.channel_id == LEAD and store.get_item(item.id).status == "posted"


def test_the_handlers_check_the_shape_of_the_channel_before_any_mark_or_send(slack, store, settings):
    for item, handler in ((idea(store), InsightHandlers(slack, None, store, settings)),
                          (message(store), MessageHandlers(slack, None, store, settings))):
        for bad in ("D08D9HFB8BB", "U08CZCGJW3E", "survive-ai", "", "C0<img>"):
            with pytest.raises(ValueError):
                handler.post(item, channel_id=bad)
        assert slack.sent == []
        for key in (f"send_started:{item.id}", f"post_channel:{item.id}", f"post_ts:{item.id}"):
            assert not store.get_cursor(key)


class Recording:
    """A handler that remembers how it was called."""

    def __init__(self):
        self.calls: list[tuple] = []

    def post(self, item, reply_ts=None, channel_id=None):
        self.calls.append((item.id, channel_id))
        return link_in(channel_id or LEAD, "1800000300.000001")

    def revise(self, item, instruction):
        return item


def test_the_runner_names_the_channel_to_the_handler_only_when_it_was_asked_for_one(slack, store, settings):
    know(store, slack, *LIST)
    h = Recording()
    for n, (iid, text) in enumerate((("a", "post"), ("b", "post #survive-ai"), ("c", f"post <#{LEAD}>"))):
        parent = f"18000001{n}0.000001"
        idea(store, id=iid, dm_ts=parent)
        card = slack.add_message(settings.self_dm_id, parent, "card", user_id=HASEEB, reply_count=1, latest_reply="1800000200")
        slack.add_thread(settings.self_dm_id, card, [Msg(ts=f"180000020{n}.000100", user_id=HASEEB, user_name="H", text=text,
                                                         thread_ts=parent, reply_count=0, latest_reply=None)])
    assert process_dm(slack, store, settings, {"insight": h}, NOW) == 3
    assert sorted(h.calls) == [("a", None), ("b", SURVIVE_ID), ("c", LEAD)]  # none asked for: no channel is passed at all


# ---------------------------------------------------------------- the dashboard's click

def test_a_dashboard_post_with_a_channel_id_posts_there(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    got = click(slack, store, settings, item.id, SURVIVE_ID)
    (sent,) = channel_sends(slack, settings)
    assert (sent.channel_id, sent.thread_ts) == (SURVIVE_ID, None)
    assert (got.state, got.result) == ("done", f"✅ Posted to #survive-ai · {link_in(SURVIVE_ID, sent.ts)}")  # the page's link
    assert store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID and store.get_item(item.id).status == "posted"


@pytest.mark.parametrize("arg", [None, LEAD])
def test_a_dashboard_post_to_the_leadership_channel_keeps_its_success_line(slack, store, settings, arg):
    """With no channel, or with the leadership channel's own id (what the picker will send): the line is as it always was."""
    know(store, slack, *LIST)
    item = idea(store)
    got = click(slack, store, settings, item.id, arg)
    (sent,) = channel_sends(slack, settings)
    assert sent.channel_id == LEAD and got.result == f"✅ Posted · {link_in(LEAD, sent.ts)}"
    assert store.get_cursor(f"post_channel:{item.id}") == LEAD


def test_a_message_posted_from_the_dashboard_goes_to_the_chosen_channel(slack, store, settings):
    know(store, slack, *LIST)
    item = message(store)
    got = click(slack, store, settings, item.id, OPS_ID)
    (sent,) = channel_sends(slack, settings)
    assert (sent.channel_id, sent.text, got.state) == (OPS_ID, POLISHED, "done")
    assert got.result == f"✅ Posted to #ops_eu.alerts · {link_in(OPS_ID, sent.ts)}"


@pytest.mark.parametrize("arg", ["C0NOTINLIST1", "G09NOTINLIST", "D08D9HFB8BB", "U08CZCGJW3E", "x", "#nope", "<#C0NOTINLIST1>"])
def test_the_page_is_refused_a_channel_that_is_not_in_the_list_with_no_slack_call_at_all(slack, store, settings, arg):
    know(store, slack, *LIST)
    item = idea(store)
    got = click(slack, store, settings, item.id, arg)
    assert got.state == "failed" and got.result.startswith("⚠️ ")
    assert got.result.endswith(f"is not one of your channels ({LISTED}) · nothing posted")
    assert slack.sent == [] and slack.reactions == []  # not a reaction, not a DM line: the row says it
    untouched(store, item.id)


def test_a_stale_page_a_channel_he_left_since_the_list_was_drawn_is_refused(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    slack.channels = [LEAD_CH, OPS, CHATTER]  # he left #survive-ai; the tick refreshes the list; the page still offers it
    assert channels.refresh(slack, store, NOW + timedelta(minutes=5), force=True)
    got = click(slack, store, settings, item.id, SURVIVE_ID, now=NOW + timedelta(minutes=6))
    assert got.state == "failed" and "is not one of your channels" in got.result and slack.sent == []
    untouched(store, item.id)


@pytest.mark.parametrize("kind", ["feedback", "todo"])
def test_a_dashboard_post_of_a_feedback_draft_or_the_todo_with_a_channel_is_refused(slack, store, settings, kind):
    know(store, slack, *LIST)
    item = feedback(store) if kind == "feedback" else todo(store)
    got = click(slack, store, settings, item.id, SURVIVE_ID)
    assert (got.state, got.result) == ("failed", f"⚠️ {LEADERSHIP_ONLY}")
    assert slack.sent == [] and slack.reactions == []
    untouched(store, item.id)


def test_a_refused_click_does_not_lock_the_draft(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    click(slack, store, settings, item.id, "C0NOTINLIST1")
    got = click(slack, store, settings, item.id, SURVIVE_ID, at=CLICK + timedelta(seconds=30))
    assert got.state == "done" and [s.channel_id for s in channel_sends(slack, settings)] == [SURVIVE_ID]


# ---------------------------------------------------------------- one post per draft

def test_a_draft_posted_to_one_channel_is_posted_and_never_goes_to_another(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    feedback(store, id="feedback-other", dm_ts="1800000050.000001")  # another open item keeps the DM read on (I5)
    dm(slack, store, settings, "post #survive-ai")
    (sent,) = channel_sends(slack, settings)
    link = link_in(SURVIVE_ID, sent.ts)
    dm(slack, store, settings, "post #ops_eu.alerts", ts="1800000300.000100", now=NOW + timedelta(minutes=1))
    dm(slack, store, settings, "post", ts="1800000310.000100", now=NOW + timedelta(minutes=2))
    got = click(slack, store, settings, item.id, OPS_ID, now=NOW + timedelta(minutes=3))
    assert len(channel_sends(slack, settings)) == 1  # still the one post
    assert dm_texts(slack, settings)[1:3] == [f"Already posted · {link}"] * 2  # and the answer shows where it is
    assert (got.state, got.result) == ("done", f"Already posted · {link}")
    assert store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID  # the record does not move


def test_two_clicks_at_once_post_once_and_the_second_does_not_name_another_channel(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    first = store.enqueue_page_command(item.id, "post", SURVIVE_ID, body_hash(item), CLICK)
    second = store.enqueue_page_command(item.id, "post", OPS_ID, body_hash(item), CLICK)
    process_page(slack, store, settings, real_handlers(slack, store, settings), NOW)
    assert [s.channel_id for s in channel_sends(slack, settings)] == [SURVIVE_ID]
    assert store.page_command(first).state == "done"
    assert store.page_command(second).result.startswith("Already posted")


# ---------------------------------------------------------------- recorded before the send

def test_the_channel_is_recorded_before_the_send_so_a_failed_attempt_keeps_its_target(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    seen = {}
    real = slack.send

    def send(channel_id, text, thread_ts=None):
        if channel_id == SURVIVE_ID:
            seen["target"] = store.get_cursor(f"post_channel:{item.id}")
            seen["marker"] = store.get_cursor(f"send_started:{item.id}")
        return real(channel_id, text, thread_ts)

    slack.send = send
    dm(slack, store, settings, "post #survive-ai")
    assert seen["target"] == SURVIVE_ID and seen["marker"]  # both set when the send was made
    slack.send = real
    other = idea(store, id="insight-2027-01-15-2", dm_ts="1800000110.000001")
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    p = slack.add_message(settings.self_dm_id, other.dm_ts, "card", user_id=HASEEB, reply_count=1, latest_reply="1800000250")
    slack.add_thread(settings.self_dm_id, p, [Msg(ts="1800000250.000100", user_id=HASEEB, user_name="H", text="post #ops_eu.alerts",
                                                  thread_ts=other.dm_ts, reply_count=0, latest_reply=None)])
    process_dm(slack, store, settings, real_handlers(slack, store, settings), NOW + timedelta(minutes=1))
    assert store.get_item(other.id).status == "failed" and store.get_cursor(f"post_failed_at:{other.id}")
    assert store.get_cursor(f"post_channel:{other.id}") == OPS_ID  # the failed attempt kept its target
    assert not store.get_cursor(f"send_started:{other.id}")


def test_a_post_that_may_have_gone_out_names_the_channel_in_the_dm(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    dm(slack, store, settings, "post #survive-ai")
    assert dm_texts(slack, settings)[-1].startswith(GONE.format(where="#survive-ai"))
    assert store.get_item(item.id).status == "failed" and store.get_cursor(f"post_failed_at:{item.id}")


def test_the_leadership_channel_is_named_too_when_that_is_where_it_may_have_gone(slack, store, settings):
    know(store, slack, *LIST)
    idea(store)
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    dm(slack, store, settings, "post")
    assert dm_texts(slack, settings)[-1].startswith(GONE.format(where="#tf-leadership-q4-2026"))


def test_without_a_name_for_the_leadership_channel_the_line_says_so(slack, store, settings):
    idea(store)  # no list cached
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    dm(slack, store, settings, "post")
    assert dm_texts(slack, settings)[-1].startswith(GONE.format(where="the leadership channel"))


def test_a_channel_that_is_no_longer_in_the_list_is_named_by_its_id(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    dm(slack, store, settings, "post #survive-ai")
    slack.channels = [LEAD_CH]
    assert channels.refresh(slack, store, NOW + timedelta(days=2), force=True)  # he left it since
    line = commands.name_the_channel(commands.EARLIER_POST, store, settings, item.id)
    assert line == f"An earlier post may have gone out — check channel {SURVIVE_ID} before posting again"


def test_every_may_have_gone_out_line_has_the_words_the_channel_replaces():
    from haseeb_agent import page_commands
    for text in (commands.INTERRUPTED, commands.MAY_HAVE_POSTED, commands.EARLIER_POST, page_commands.EARLIER_POST,
                 page_commands.POSTED_UNRECORDED):
        assert commands.CHECK_THE_CHANNEL in text, text


def test_a_hostile_name_in_the_cached_list_is_never_put_in_a_line(slack, store, settings):
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": NOW.isoformat(), "channels": [
        {"id": SURVIVE_ID, "name": "<img src=x onerror=alert(1)>", "private": False},
        {"id": OPS_ID, "name": "x" * 200, "private": True}]}))
    item = idea(store)
    for cid in (SURVIVE_ID, OPS_ID):
        channels.record_post_channel(store, item.id, cid)
        assert commands.name_the_channel(commands.EARLIER_POST, store, settings, item.id) == (
            f"An earlier post may have gone out — check channel {cid} before posting again")  # dropped by the cache: the id


def test_the_failed_page_post_names_the_channel_in_the_row_and_the_dm(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    got = click(slack, store, settings, item.id, OPS_ID)
    assert got.state == "failed" and got.result.startswith(GONE.format(where="#ops_eu.alerts"))
    assert dm_texts(slack, settings)[-1].startswith(GONE.format(where="#ops_eu.alerts"))


def test_the_interrupted_notice_names_the_channel(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    store.set_cursor(f"post_channel:{item.id}", SURVIVE_ID)
    store.set_cursor(f"inflight:{item.id}", NOW.isoformat())  # the process died between the send and its record
    recover_interrupted(slack, store, settings, NOW)
    assert dm_texts(slack, settings) == ["⚠️ Post was interrupted — check #survive-ai before re-posting"]
    assert store.get_item(item.id).status == "failed"


def test_an_earlier_command_is_refused_with_the_channel_named(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    dm(slack, store, settings, "post #survive-ai", ts="1800000200.000100")
    # a second reply sent while the first hung: older than the recorded failure (NOW = 1800000500)
    dm(slack, store, settings, "post #survive-ai", ts="1800000300.000100", now=NOW + timedelta(minutes=1))
    assert dm_texts(slack, settings)[-1] == "⚠️ An earlier post may have gone out — check #survive-ai before posting again"
    assert channel_sends(slack, settings) == []
    got = click(slack, store, settings, item.id, SURVIVE_ID, at=datetime.fromtimestamp(1_800_000_400, KW), now=NOW + timedelta(minutes=2))
    assert got.result == "⚠️ An earlier post may have gone out — check #survive-ai before posting again"


def test_a_row_left_running_is_failed_naming_the_channel(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    store.set_cursor(f"post_channel:{item.id}", SURVIVE_ID)
    cid = store.enqueue_page_command(item.id, "post", SURVIVE_ID, body_hash(item), CLICK)
    assert store.claim_page_command(cid)  # a tick that died after claiming it
    process_page(slack, store, settings, real_handlers(slack, store, settings), NOW + timedelta(minutes=3))
    assert store.page_command(cid).result == GONE.format(where="#survive-ai") + "the run was interrupted)"


def test_a_post_whose_result_could_not_be_recorded_names_the_channel(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store)
    store.set_cursor(f"post_channel:{item.id}", SURVIVE_ID)
    store.set_cursor(f"permalink:{item.id}", link_in(SURVIVE_ID, "1800000300.000001"))
    cid = store.enqueue_page_command(item.id, "post", SURVIVE_ID, body_hash(item), CLICK)
    assert store.claim_page_command(cid)
    process_page(slack, store, settings, real_handlers(slack, store, settings), NOW + timedelta(minutes=3))
    assert store.page_command(cid).result == "Posted, but the result could not be recorded — check #survive-ai"


def test_feedback_and_todo_lines_still_say_the_channel(slack, store, settings):
    """They post only in the leadership channel's thread and record no target: their lines are unchanged."""
    know(store, slack, *LIST)
    item = feedback(store)
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    dm(slack, store, settings, "post")
    assert dm_texts(slack, settings)[-1].startswith("⚠️ Post may have gone out — check the channel before re-posting (")
    assert not store.get_cursor(f"post_channel:{item.id}")


# ---------------------------------------------------------------- a retry after a possible post is pinned to where it went

def fail_post_to(slack, store, settings, target):
    """A post to `target` (a DM word) that fails the way a timeout does: it may have gone out. Returns the item."""
    item = idea(store)
    slack.fail_next("tool-call mismatch: no call seen", method="send")
    dm(slack, store, settings, f"post {target}".strip())
    assert store.get_item(item.id).status == "failed" and store.get_cursor(f"post_failed_at:{item.id}")
    return item


def test_a_retry_that_names_another_channel_is_refused_and_leaves_the_draft_as_it_is(slack, store, settings):
    know(store, slack, *LIST)
    item = fail_post_to(slack, store, settings, "#survive-ai")
    before = (store.get_item(item.id), store.get_cursor(f"post_failed_at:{item.id}"))
    for n, text in enumerate(("post #ops_eu.alerts", "post #tf-leadership-q4-2026", "post")):
        dm(slack, store, settings, text, ts=f"18000006{n}0.000100", now=NOW + timedelta(minutes=2 + n))
        assert dm_texts(slack, settings)[-1] == "⚠️ this draft may already be in #survive-ai; if it isn't there, post it by hand"
    assert channel_sends(slack, settings) == []
    assert (store.get_item(item.id), store.get_cursor(f"post_failed_at:{item.id}")) == before  # no new attempt, no new time
    assert store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID and not store.get_cursor(f"inflight:{item.id}")


def test_a_retry_to_the_channel_it_may_already_be_in_is_the_informed_override(slack, store, settings):
    know(store, slack, *LIST)
    item = fail_post_to(slack, store, settings, "#survive-ai")
    dm(slack, store, settings, "post #survive-ai", ts=LATER, now=NOW + timedelta(minutes=2))
    (sent,) = channel_sends(slack, settings)
    assert sent.channel_id == SURVIVE_ID and store.get_item(item.id).status == "posted"
    assert dm_texts(slack, settings)[-1] == f"✅ Posted to #survive-ai · {link_in(SURVIVE_ID, sent.ts)}"


def test_a_plain_retry_is_pinned_to_the_leadership_channel_when_that_is_where_it_may_have_gone(slack, store, settings):
    know(store, slack, *LIST)
    item = fail_post_to(slack, store, settings, "")
    dm(slack, store, settings, "post #survive-ai", ts=LATER, now=NOW + timedelta(minutes=2))
    assert channel_sends(slack, settings) == []
    assert dm_texts(slack, settings)[-1] == "⚠️ this draft may already be in #tf-leadership-q4-2026; if it isn't there, post it by hand"
    dm(slack, store, settings, "post", ts="1800000700.000100", now=NOW + timedelta(minutes=3))
    assert [s.channel_id for s in channel_sends(slack, settings)] == [LEAD] and store.get_item(item.id).status == "posted"


def test_the_pin_holds_for_a_click_too_and_names_the_channel_with_no_slack_call(slack, store, settings):
    know(store, slack, *LIST)
    item = fail_post_to(slack, store, settings, "#survive-ai")
    sent_before, reactions_before = len(slack.sent), len(slack.reactions)
    got = click(slack, store, settings, item.id, OPS_ID, at=datetime.fromtimestamp(1_800_000_650, KW),
                now=NOW + timedelta(minutes=3))
    assert (got.state, got.result) == ("failed", "⚠️ this draft may already be in #survive-ai; if it isn't there, post it by hand")
    assert (len(slack.sent), len(slack.reactions)) == (sent_before, reactions_before)
    got = click(slack, store, settings, item.id, None, at=datetime.fromtimestamp(1_800_000_660, KW), now=NOW + timedelta(minutes=4))
    assert got.result == "⚠️ this draft may already be in #survive-ai; if it isn't there, post it by hand"
    got = click(slack, store, settings, item.id, SURVIVE_ID, at=datetime.fromtimestamp(1_800_000_670, KW), now=NOW + timedelta(minutes=5))
    assert got.state == "done" and channel_sends(slack, settings)[-1].channel_id == SURVIVE_ID


def test_a_failure_from_before_the_choice_existed_is_pinned_to_the_leadership_channel(slack, store, settings):
    """No record, but a possible post: it can only have gone to the leadership channel, the only place there was."""
    know(store, slack, *LIST)
    item = idea(store, status="failed")
    mark_post_failed(store, item.id, NOW)
    dm(slack, store, settings, "post #survive-ai", ts=LATER, now=NOW + timedelta(minutes=2))
    assert channel_sends(slack, settings) == []
    assert dm_texts(slack, settings)[-1] == "⚠️ this draft may already be in #tf-leadership-q4-2026; if it isn't there, post it by hand"
    dm(slack, store, settings, "post", ts="1800000700.000100", now=NOW + timedelta(minutes=3))
    assert [s.channel_id for s in channel_sends(slack, settings)] == [LEAD]


def test_an_old_skipped_or_failed_draft_without_a_possible_post_may_go_anywhere(slack, store, settings):
    know(store, slack, *LIST)
    item = idea(store, status="failed")  # e.g. three failed revisions: nothing may have gone out
    dm(slack, store, settings, "post #survive-ai")
    assert [s.channel_id for s in channel_sends(slack, settings)] == [SURVIVE_ID]
    assert store.get_item(item.id).status == "posted"


# ---------------------------------------------------------------- the pause refuses

def test_the_pause_refuses_a_post_to_a_chosen_channel_and_it_leaves_no_trace(store, settings):
    slack = PausedSlack()
    know(store, slack, *LIST)
    item = idea(store)
    got = click(slack, store, settings, item.id, SURVIVE_ID)
    assert (got.state, got.result) == ("failed", "paused: nothing was sent")
    assert slack.sent == [] and slack.refused.count("send") == 1
    untouched(store, item.id)  # no failure time, no attempt, no marker, and the target went back with the attempt
    slack.paused = False  # Resume: a plain first post, not an override, and to a channel of his choice
    got = click(slack, store, settings, item.id, OPS_ID, at=CLICK + timedelta(seconds=30))
    assert got.state == "done" and [s.channel_id for s in channel_sends(slack, settings)] == [OPS_ID]


def test_the_pause_refuses_a_dm_post_to_a_chosen_channel_too(store, settings):
    slack = PausedSlack()
    know(store, slack, *LIST)
    item = idea(store)
    dm(slack, store, settings, "post #survive-ai")
    assert slack.sent == [] and store.get_item(item.id).status == "delivered"
    untouched(store, item.id)
    assert store.cursors("paused_command_notice:") != {}  # he is told after Resume, as for any command the pause refused


def test_a_pause_after_a_failed_attempt_keeps_the_earlier_target(store, settings):
    """The retry is pinned to the channel of the earlier attempt, so a pause that refuses it must not forget that channel."""
    slack = PausedSlack()
    slack.paused = False
    know(store, slack, *LIST)
    item = fail_post_to(slack, store, settings, "#survive-ai")
    slack.paused = True
    click(slack, store, settings, item.id, SURVIVE_ID, at=datetime.fromtimestamp(1_800_000_650, KW), now=NOW + timedelta(minutes=3))
    assert store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID and store.get_cursor(f"post_failed_at:{item.id}")


# ---------------------------------------------------------------- the readers of a post

def test_a_post_without_the_cursor_is_in_the_leadership_channel(store, settings):
    assert channels.post_channel(store, settings, "insight-old") == LEAD  # made before the choice existed
    channels.record_post_channel(store, "insight-new", SURVIVE_ID)
    assert channels.post_channel(store, settings, "insight-new") == SURVIVE_ID


def test_an_idea_posted_to_another_channel_counts_as_posted_in_the_weekly_numbers(slack, store, settings):
    from haseeb_agent.metrics import week_stats
    know(store, slack, *LIST)
    item = idea(store)
    dm(slack, store, settings, "post #survive-ai")
    stats = week_stats(store, DAY - timedelta(days=DAY.weekday()), workdays=settings.workdays, today=DAY)
    assert (stats.posted, stats.edited_then_posted, stats.intervention_rate) == (1, 0, 0.0)
    assert store.get_item(item.id).status == "posted"


# ---------------------------------------------------------------- the idea handler, called directly

LONG = ("A long paragraph about alpha that keeps going. " * 40 + "\n\n") * 6


def long_idea(store) -> Item:
    item = idea(store)
    meta = json.loads(item.body)
    meta["insight"]["idea"] = LONG
    store.mark(item.id, "delivered", body=json.dumps(meta))
    return store.get_item(item.id)


def test_a_long_idea_goes_to_the_chosen_channel_in_every_part_and_the_link_is_there(slack, store, settings):
    item = long_idea(store)
    link = InsightHandlers(slack, None, store, settings).post(item, channel_id=SURVIVE_ID)
    posts = channel_sends(slack, settings)
    assert len(posts) >= 2 and {s.channel_id for s in posts} == {SURVIVE_ID}
    assert posts[0].thread_ts is None and all(p.thread_ts == posts[0].ts for p in posts[1:])
    assert link == link_in(SURVIVE_ID, posts[0].ts) and store.get_cursor(f"post_ts:{item.id}") == posts[0].ts
    assert [store.get_cursor(f"insight_part:{p.ts}") for p in posts] == [item.id] * len(posts)
    assert store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID and not store.get_cursor(f"send_started:{item.id}")


def test_a_second_part_that_fails_reports_the_link_in_the_chosen_channel(store, settings):
    item = long_idea(store)

    class Second(FakeSlack):
        def send(self, channel_id, text, thread_ts=None):
            if thread_ts is not None:
                raise SlackError("part failed")
            return super().send(channel_id, text, thread_ts)

    s2 = Second()
    with pytest.raises(SlackError) as e:
        InsightHandlers(s2, None, store, settings).post(item, channel_id=SURVIVE_ID)
    (top,) = s2.sent
    link = link_in(SURVIVE_ID, top.ts)
    assert top.channel_id == SURVIVE_ID and link in str(e.value)
    assert store.get_cursor(f"permalink:{item.id}") == link and store.get_cursor(f"post_channel:{item.id}") == SURVIVE_ID


def test_without_a_channel_the_handlers_post_to_the_leadership_channel_as_ever(slack, store, settings):
    item, note = idea(store), message(store)
    InsightHandlers(slack, None, store, settings).post(item)
    MessageHandlers(slack, None, store, settings).post(note)
    assert [s.channel_id for s in channel_sends(slack, settings)] == [LEAD, LEAD]
    assert store.get_cursor(f"post_channel:{item.id}") == LEAD and store.get_cursor(f"post_channel:{note.id}") == LEAD
