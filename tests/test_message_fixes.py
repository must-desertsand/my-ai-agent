"""The composer's fix round (the independent review of 2026-10-20, `compose-review.md`): regression tests, one section
per finding. They build on the helpers of tests/test_message.py. No real call: FakeSlack, a scripted LLM, tmp stores."""
import logging
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from haseeb_agent import message_prompt as mp
from haseeb_agent import render
from haseeb_agent.cli import PAGE_JOB, Runtime, default_jobs, message_hold, run_tick
from haseeb_agent.commands import HELD_KEY, SAY_MAX_AGE_S, SAY_TOO_OLD, process_dm
from haseeb_agent.jobs.message import MessageHandlers, RevisionUnsure, draft_key, note_key, raw_key, say_item_id
from haseeb_agent.jobs.todo import item_id as todo_item_id
from haseeb_agent.jobs.todo import versions
from haseeb_agent.models import Item
from haseeb_agent.page_commands import COMPOSE_MAYBE_SENT, DRAFT_CHANGED, EXPIRED, body_hash, page_due, process_page
from haseeb_agent.slack_gateway import SlackError
from haseeb_agent.store import Store
from tests.fakes import FakeSlack, LandsThenTimesOut
from tests.test_feedback import FakeLLM
from tests.test_message import (CLICK, EPOCH, NOW, POLISHED, ROUGH, channel_sends, dm_sends, enqueue_compose, msg,  # noqa: F401
                                open_card, page_run, put_say, reply, reply_in_thread, settings, slack, store)


def quoted(text: str) -> str:
    """The draft a message card shows: its quoted lines, without the quote marks."""
    return "\n".join(ln[2:] if ln.startswith("> ") else ln[1:] for ln in text.splitlines() if ln.startswith(">"))


def card_of(slack, settings, item):
    return next(c for c in dm_sends(slack, settings) if c.ts == item.dm_ts)


# ---------------------------------------------------------------- C1: a card on screen is never polished again

def test_a_say_tried_again_after_a_timeout_that_landed_keeps_the_text_the_card_shows(store, settings):
    slack = LandsThenTimesOut()  # the first send reaches the DM, then times out
    ts = put_say(slack, settings, "say: " + ROUGH)
    llm = FakeLLM(writer=[reply("Version A."), reply("Version B, said differently.")])
    h = {"message": MessageHandlers(slack, llm, store, settings)}
    assert process_dm(slack, store, settings, h, NOW) == 0  # not settled: it is tried again
    assert store.get_item(say_item_id(ts)) is None and len(dm_sends(slack, settings)) == 1  # the card landed
    assert process_dm(slack, store, settings, h, NOW + timedelta(minutes=5)) == 1
    item = store.get_item(say_item_id(ts))
    (card,) = dm_sends(slack, settings)  # no second card
    assert item.dm_ts == card.ts and quoted(card.text) == item.body == "Version A."
    assert len(llm.calls) == 1  # polished once, never again
    assert store.get_cursor(draft_key(mp.normalize_raw(ROUGH))) == ""  # and the kept record is cleared


def test_two_says_in_one_run_never_adopt_each_others_cards(store, settings):
    slack = LandsThenTimesOut()
    x = put_say(slack, settings, "say: X: I will share the deck after the huddle", ago=120)
    y = put_say(slack, settings, "say: Y: Lunch is at 1 pm today", ago=100, micro=200)
    llm = FakeLLM(writer=[reply("X: I will share the deck after the huddle."), reply("Y: Lunch is at 1 pm today.")])
    h = {"message": MessageHandlers(slack, llm, store, settings)}
    process_dm(slack, store, settings, h, NOW)  # X lands and times out; Y is drafted in the same run
    assert store.get_item(say_item_id(x)) is None and store.get_item(say_item_id(y)) is not None
    process_dm(slack, store, settings, h, NOW + timedelta(minutes=5))  # X is tried again
    assert len(dm_sends(slack, settings)) == 2
    for ts, text in ((x, "X: I will share the deck after the huddle."), (y, "Y: Lunch is at 1 pm today.")):
        item = store.get_item(say_item_id(ts))
        assert quoted(card_of(slack, settings, item).text) == item.body == text


def test_a_polish_clicked_again_after_a_timeout_that_landed_adopts_that_card(store, settings):
    slack = LandsThenTimesOut()
    llm = FakeLLM(writer=[reply("Version A."), reply("Version B, said differently."), reply("A second message.")])
    h = MessageHandlers(slack, llm, store, settings)
    first = enqueue_compose(store, item_id="message-1")
    page_run(slack, store, settings, h=h)
    row = store.page_command(first)
    assert (row.state, row.result) == ("failed", COMPOSE_MAYBE_SENT)  # "may have reached your DM; check first"
    assert store.get_item("message-1") is None and len(dm_sends(slack, settings)) == 1
    other = enqueue_compose(store, text="a different message altogether", item_id="message-2")
    page_run(slack, store, settings, h=h)  # another message in between does not take that card
    assert store.page_command(other).state == "done" and len(dm_sends(slack, settings)) == 2
    again = enqueue_compose(store, item_id="message-3")  # the same words, clicked again
    page_run(slack, store, settings, h=h)
    assert store.page_command(again).state == "done"
    item = store.get_item("message-3")
    assert len(dm_sends(slack, settings)) == 2  # the landed card was adopted: no third card
    assert quoted(card_of(slack, settings, item).text) == item.body == "Version A."
    assert len(llm.calls) == 2  # the first words once, the other message once: no polish for the click again


def test_a_kept_polish_is_forgotten_after_an_hour(store, settings):
    clock = {"t": 1_000_000.0}
    slack = LandsThenTimesOut()
    llm = FakeLLM(writer=[reply("Version A."), reply("Version B.")])
    h = MessageHandlers(slack, llm, store, settings, clock=lambda: clock["t"])
    with pytest.raises(SlackError):
        h.compose(ROUGH, "message-1", NOW)
    assert store.get_cursor(draft_key(mp.normalize_raw(ROUGH)))  # kept, for a retry
    clock["t"] += 3601
    item = h.compose(ROUGH, "message-2", NOW)  # a new polish: the old record is gone
    assert len(llm.calls) == 2 and item.body == "Version B."
    assert store.get_cursor(draft_key(mp.normalize_raw(ROUGH))) == ""


def test_a_stale_kept_polish_is_cleared_by_the_next_compose(store, settings):
    clock = {"t": 1_000_000.0}
    slack = FakeSlack()
    h = MessageHandlers(slack, FakeLLM(writer=[reply("A.")]), store, settings, clock=lambda: clock["t"])
    slack.fail_next("dm down", method="send")
    with pytest.raises(SlackError):
        h.compose("first words", "message-1", NOW)
    key = draft_key("first words")
    assert store.get_cursor(key)
    clock["t"] += 7200
    h.compose("other words", "message-2", NOW)  # a different text: its own compose clears what nobody came back for
    assert store.get_cursor(key) == ""


def test_a_revision_whose_card_landed_but_timed_out_is_not_adopted_by_the_next_edit(store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two."), reply("Version three.")])
    slack = LandsThenTimesOut(timeouts=0)
    item, parent = open_card(slack, store, settings, llm)
    slack.timeouts = 1  # the revised card lands in the thread, then times out
    reply_in_thread(slack, settings, parent, "edit: warmer", n=1)
    h = {"message": MessageHandlers(slack, llm, store, settings)}
    process_dm(slack, store, settings, h, NOW + timedelta(minutes=5))
    kept = store.get_item(item.id)
    assert kept.body == POLISHED and kept.status == "delivered" and kept.edited is False  # nothing was saved
    notice = dm_sends(slack, settings)[-1]
    assert notice.text.startswith("⚠️ The revised draft may have reached your DM") and "`post` still sends" in notice.text
    reply_in_thread(slack, settings, parent, "edit: warmer please", n=2)
    process_dm(slack, store, settings, h, NOW + timedelta(minutes=10))
    item = store.get_item(item.id)
    assert item.body == "Version three."
    cards = [c for c in dm_sends(slack, settings) if c.text.startswith("**Message draft (revised)**")]
    assert [quoted(c.text) for c in cards] == ["Version two.", "Version three."]  # two cards, nobody adopted the first
    assert quoted(cards[-1].text) == item.body  # the latest card in the thread is the draft that stands


def test_a_revision_is_kept_per_command_so_the_same_command_sends_exactly_that_text(store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two."), reply("Version three.")])
    slack = LandsThenTimesOut(timeouts=0)
    item, _ = open_card(slack, store, settings, llm)
    h = MessageHandlers(slack, llm, store, settings)
    slack.timeouts = 1
    with pytest.raises(RevisionUnsure):
        h.revise_for(item, "make it warmer", "1800000100.000100")
    assert len(llm.calls) == 2
    done = h.revise_for(item, "make it warmer", "1800000100.000100")  # the same command again
    assert len(llm.calls) == 2  # no new polish
    thread = [c for c in dm_sends(slack, settings) if c.text.startswith("**Message draft (revised)**")]
    assert len(thread) == 1  # the landed card was adopted, not sent twice
    assert done.body == "Version two." == quoted(thread[0].text) and store.get_item(item.id).edited is True
    # another command is another revision
    h.revise_for(store.get_item(item.id), "make it warmer", "1800000200.000100")
    assert len(llm.calls) == 3 and store.get_item(item.id).body == "Version three."


def test_a_kept_revision_is_not_used_for_another_instruction_or_another_draft(store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two."), reply("Version three.")])
    slack = LandsThenTimesOut(timeouts=0)
    item, _ = open_card(slack, store, settings, llm)
    h = MessageHandlers(slack, llm, store, settings)
    slack.timeouts = 1
    with pytest.raises(RevisionUnsure):
        h.revise_for(item, "make it warmer", "1800000100.000100")
    h.revise_for(item, "something else", "1800000100.000100")  # same command ts, another instruction: polished again
    assert len(llm.calls) == 3


def test_a_revision_that_fails_cleanly_leaves_nothing_kept(store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two.")])
    slack = FakeSlack()
    item, _ = open_card(slack, store, settings, llm)
    h = MessageHandlers(slack, llm, store, settings)
    slack.fail_next("channel_not_found", method="send")
    with pytest.raises(SlackError):
        h.revise_for(item, "warmer", "1800000100.000100")
    assert not any(store.cursors("message_revision:").values())


# ---------------------------------------------------------------- M3: the cursors follow the send

def test_a_failed_polish_leaves_no_raw_or_note_cursors_behind(store, settings):
    slack = FakeSlack()
    h = MessageHandlers(slack, FakeLLM(writer=[reply()]), store, settings)
    slack.fail_next("dm down", method="send")
    with pytest.raises(SlackError):
        h.compose(ROUGH, "message-1", NOW)
    assert store.cursors("message_raw:") == {} and store.cursors("message_note:") == {}  # nothing of his text, nowhere
    h.compose(ROUGH, "message-1", NOW)  # the retry works, and writes them with the item
    assert store.get_cursor(raw_key("message-1")) == mp.normalize_raw(ROUGH)
    assert store.get_cursor(note_key("message-1")) == "Fixed grammar and made it clearer."


def test_a_failed_revise_does_not_change_the_note_the_page_shows(store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two.", "A new note.")])
    slack = FakeSlack()
    item, _ = open_card(slack, store, settings, llm)
    h = MessageHandlers(slack, llm, store, settings)
    slack.fail_next("channel_not_found", method="send")
    with pytest.raises(SlackError):
        h.revise(item, "warmer")
    assert store.get_cursor(note_key(item.id)) == "Fixed grammar and made it clearer."  # still the draft that stands


# ---------------------------------------------------------------- M11: a made draft never shows as a failed Polish

def test_bookkeeping_after_the_item_exists_is_best_effort(store, settings, monkeypatch):
    slack = FakeSlack()
    h = MessageHandlers(slack, FakeLLM(writer=[reply()]), store, settings)

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(Store, "record_run", boom)
    item = h.compose(ROUGH, "message-1", NOW)  # no exception: the card is in his DM and the item is saved
    assert store.get_item("message-1") == item and len(dm_sends(slack, settings)) == 1


def test_a_page_polish_whose_bookkeeping_failed_still_ends_done(store, settings, monkeypatch):
    slack = FakeSlack()
    h = MessageHandlers(slack, FakeLLM(writer=[reply()]), store, settings)
    monkeypatch.setattr(Store, "record_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    cid = enqueue_compose(store, item_id="message-1")
    page_run(slack, store, settings, h=h)
    assert store.page_command(cid).state == "done" and store.get_item("message-1") is not None


# ---------------------------------------------------------------- C2: `post` sends the draft that was showing (N2)

@pytest.fixture
def near():
    """A DM whose clock is just before NOW: the cards are older than a dashboard click made at CLICK, as in life."""
    return FakeSlack(start_ts=EPOCH - 3600)


def reply_after(slack, settings, parent, text, n=1):
    """A reply written right after the card and before anything the next run sends: every ts is in the same second."""
    ts = f"{int(float(parent.ts))}.{500 + n:06d}"
    replies = [*slack.threads.get((settings.self_dm_id, parent.ts), (parent, []))[1], msg(ts, text, thread_ts=parent.ts)]
    parent.reply_count, parent.latest_reply = len(replies), replies[-1].ts
    slack.add_thread(settings.self_dm_id, parent, replies)


def test_every_card_is_recorded_as_a_version_with_the_ts_of_its_dm_message(near, store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two.")])
    item, _ = open_card(near, store, settings, llm)
    revised = MessageHandlers(near, llm, store, settings).revise(item, "warmer")
    first, second = versions(store, item.id)
    assert (first["ts"], first["body"]) == (item.dm_ts, POLISHED)
    assert second["body"] == revised.body == "Version two." and Decimal(second["ts"]) > Decimal(first["ts"])
    assert second["ts"] == dm_sends(near, settings)[-1].ts  # the revised card's own ts


def test_an_edit_and_a_post_in_one_run_post_the_draft_he_saw_not_the_revision(near, store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two, with a sentence the model added.")])
    item, parent = open_card(near, store, settings, llm)
    reply_after(near, settings, parent, "edit: make it a bit warmer", n=1)
    reply_after(near, settings, parent, "post", n=2)  # written while the first draft was showing
    h = {"message": MessageHandlers(near, llm, store, settings)}
    process_dm(near, store, settings, h, NOW + timedelta(minutes=5))
    (posted,) = channel_sends(near)
    assert posted.text == POLISHED  # what he saw, never the revision that landed after his reply
    assert store.get_item(item.id).status == "posted"
    assert [v["body"] for v in versions(store, item.id)] == [POLISHED, "Version two, with a sentence the model added."]


def test_a_page_revise_and_a_dm_post_in_one_tick_post_the_draft_he_saw(near, store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two, with a sentence the model added.")])
    item, parent = open_card(near, store, settings, llm)
    edit = store.enqueue_page_command(item.id, "edit", "make it a bit warmer", body_hash(item), CLICK)
    reply_after(near, settings, parent, "post", n=1)
    h = MessageHandlers(near, llm, store, settings)
    page_run(near, store, settings, h=h)  # the page rows run first in a tick: the revision lands
    assert store.page_command(edit).state == "done" and store.get_item(item.id).body.startswith("Version two")
    process_dm(near, store, settings, {"message": h}, NOW)
    (posted,) = channel_sends(near)
    assert posted.text == POLISHED  # the `post` was written under the first draft


def test_a_post_written_after_a_revision_landed_posts_the_revision(near, store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two.")])
    item, parent = open_card(near, store, settings, llm)
    h = MessageHandlers(near, llm, store, settings)
    h.revise(item, "warmer")
    reply_in_thread(near, settings, parent, "post", n=1)  # ten seconds after the card: after the revised card too
    process_dm(near, store, settings, {"message": h}, NOW)
    assert [c.text for c in channel_sends(near)] == ["Version two."]


def test_a_page_post_checks_the_version_showing_at_the_click(near, store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two.")])
    item, _ = open_card(near, store, settings, llm)
    h = MessageHandlers(near, llm, store, settings)
    h.revise(item, "warmer")
    stale = store.enqueue_page_command(item.id, "post", None, body_hash(item), CLICK)  # the page still showed Version one
    page_run(near, store, settings, h=h)
    assert (store.page_command(stale).state, store.page_command(stale).result) == ("failed", DRAFT_CHANGED)
    assert channel_sends(near) == []
    current = store.enqueue_page_command(item.id, "post", None, body_hash(store.get_item(item.id)), CLICK)
    page_run(near, store, settings, h=h)  # the page showed Version two, the version showing at the click
    assert store.page_command(current).state == "done" and [c.text for c in channel_sends(near)] == ["Version two."]


def test_a_page_click_made_before_the_first_card_existed_is_refused(store, settings):
    far = FakeSlack()  # its cards are newer than the click
    llm = FakeLLM(writer=[reply()])
    item, _ = open_card(far, store, settings, llm)
    cid = store.enqueue_page_command(item.id, "post", None, body_hash(item), CLICK)
    page_run(far, store, settings, h=MessageHandlers(far, llm, store, settings))
    assert (store.page_command(cid).state, store.page_command(cid).result) == ("failed", DRAFT_CHANGED)
    assert channel_sends(far) == []


def test_a_post_older_than_every_draft_is_refused_and_one_without_versions_uses_the_body(near, store, settings):
    llm = FakeLLM(writer=[reply()])
    item, _ = open_card(near, store, settings, llm)
    h = MessageHandlers(near, llm, store, settings)
    with pytest.raises(LookupError, match="reply post under the latest draft"):
        h.post(item, reply_ts="1.000001")
    assert channel_sends(near) == []
    store.set_cursor(f"body_versions:{item.id}", "")  # an item with no versions recorded (made before they were)
    h.post(item, reply_ts=f"{EPOCH}.000001")
    assert [c.text for c in channel_sends(near)] == [POLISHED]


# ---------------------------------------------------------------- M7: the link guard's edges

def link_refused(polished, raw):
    try:
        mp.parse_reply(reply(polished), raw)
    except mp.LinkChanged:
        return True
    return False


def test_a_dotted_ipv4_host_is_a_link():
    assert mp.links_in("see 203.0.113.9:8080/login now") == {"203.0.113.9:8080/login"}
    assert mp.links_in("the box is 10.0.0.1.") == {"10.0.0.1"}
    assert mp.links_in("http://10.0.0.1:3000/a") == {"http://10.0.0.1:3000/a"}  # with a scheme it is a URL as before
    assert mp.links_in("version 2.1 and 3.5x and v1.2.3") == set()  # not four dotted numbers


def test_the_model_may_not_add_or_change_an_ip_address():
    assert link_refused("Log in at 203.0.113.9:8080/login", "log in somewhere")
    assert link_refused("Log in at 203.0.113.10:8080/login", "log in at 203.0.113.9:8080/login")
    assert link_refused("Log in at 203.0.113.9:9090/login", "log in at 203.0.113.9:8080/login")
    assert not link_refused("Log in at 203.0.113.9:8080/login.", "log in at 203.0.113.9:8080/login")


def test_a_look_alike_the_browser_folds_into_his_path_is_a_different_path():
    """Review M7: `https://good.com/<fullwidth a>rch` counted as equal to `.../arch` because only the folded text was
    compared. The host is the same; the path that gets requested is not."""
    fullwidth_a = "\uff41"
    assert link_refused(f"See https://good.com/{fullwidth_a}rch", "see https://good.com/arch")
    assert link_refused(f"See good.com/{fullwidth_a}rch", "see good.com/arch")
    assert not link_refused(f"See https://good.com/{fullwidth_a}rch", f"see https://good.com/{fullwidth_a}rch")  # his own
    assert mp.links_in(f"https://good.com/{fullwidth_a}rch") == {"https://good.com/arch"}  # found as the browser sees it
    assert mp.links_in(f"https://good.com/{fullwidth_a}rch", fold=False) == {f"https://good.com/{fullwidth_a}rch"}


def test_a_fullwidth_domain_is_still_found_and_compared_as_that_domain():
    assert link_refused("See \uff45\uff56\uff49\uff4c\uff0ecom", "see good.com")  # fullwidth evil.com is evil.com
    assert not link_refused("See \uff47\uff4f\uff4f\uff44\uff0ecom", "see good.com")  # fullwidth good.com is his


def test_link_spans_say_where_the_links_are_in_the_text_as_it_stands():
    text = "see https://good.com/a?x=1&y=2, and good.com/b. Then 10.0.0.1:80/c (www.good.org)"
    assert [text[a:b] for a, b in mp.link_spans(text)] == [
        "https://good.com/a?x=1&y=2", "good.com/b", "10.0.0.1:80/c", "www.good.org"]
    assert mp.link_spans("no links here") == []


# ---------------------------------------------------------------- I2: his own links reach the post exactly

URLS = ["https://www.youtube.com/watch?v=abc&t=10s", "https://good.com/?a=1&lang=en", "https://good.com/a[1]",
        "good.com/a?x=1&y=2", "http://10.0.0.1:8080/p?a=1&b=2", "https://good.com/a_(b)"]


@pytest.mark.parametrize("url", URLS)
def test_his_own_url_reaches_the_post_and_the_card_exactly(url):
    draft = f"Watch {url} now"
    assert render.render_message_post(draft, {}) == draft
    assert f"> {draft}" in render.render_message_dm(draft, None)
    if "&" in url or "[" in url:  # what the cleaner alone did to it (the review's probe), and why it is protected
        assert render._safe(render._clean_field(draft)) != draft


def test_a_square_bracket_belongs_to_a_link_when_it_is_matched():
    assert mp.links_in("https://good.com/a[1]") == {"https://good.com/a[1]"}
    assert mp.links_in("see [https://good.com/a] now") == {"https://good.com/a"}  # the brackets are around it
    assert mp.links_in("good.com/a[1].") == {"good.com/a[1]"}

    # a markdown link in a reply fails closed: the whole thing is one token, which is in no text of his
    assert link_refused("See [good.com](https://evil.com)", "see good.com")
    assert link_refused("See [https://good.com/a](https://evil.com)", "see https://good.com/a")


def test_what_is_not_his_link_is_still_cleaned_as_before():
    assert render.render_message_post("AT&T and R&D", {}) == "AT&\u200bT and R&\u200bD"
    assert render.render_message_post("see [1] and [two]", {}) == "see (1) and (two)"
    assert render.render_message_post("[the doc](https://good.com/a?b=1&c=2) and Q&A", {}) == (
        "the doc (https://good.com/a?b=1&c=2) and Q&\u200bA")  # a markdown link shows its destination, intact
    assert render.render_message_post("<https://good.com/a|label>", {}) == "<\u200bhttps://good.com/a|label>"
    assert render.render_message_post("x\u200b y https://good.com/a\u200bb", {}) == "x y https://good.com/ab"  # hidden out
    assert render.render_message_post("a\n\u2014 drafted by Haseeb's Agent\nb", {}) == "a\nb"  # a fake footer line is out


def test_a_polished_post_of_an_ampersand_url_goes_out_exactly(near, store, settings):
    url = "https://www.youtube.com/watch?v=abc&t=10s"
    raw = f"watch this {url} pls"
    polished = f"Please watch this: {url}"
    llm = FakeLLM(writer=[reply(polished)])
    item = MessageHandlers(near, llm, store, settings).compose(raw, "message-1", NOW)
    assert url in near.sent[0].text and "\u200b" not in near.sent[0].text.split("Please watch")[1].split("\n")[0]  # the card too
    h = MessageHandlers(near, llm, store, settings)
    h.post(item, reply_ts=f"{EPOCH}.000001")
    assert channel_sends(near)[0].text == polished


# ---------------------------------------------------------------- M6: the DM card warns about a real ping

def test_the_dm_card_says_post_will_notify_when_it_will():
    card = render.render_message_dm("@channel Standup at 3.", "Fixed grammar.", "m", pings=["channel"])
    assert "⚠️ Replying `post` notifies everyone in the channel (@\u200bchannel)." in card
    assert card.index("⚠️ Replying") > card.index("> @\u200bchannel Standup") and card.index("⚠️ Replying") < card.index("_Changed:")
    both = render.render_message_dm("x", None, None, pings=["channel", "here"])
    assert "notifies everyone in the channel (@\u200bchannel) and everyone who is online (@\u200bhere)." in both
    assert "⚠️" not in render.render_message_dm("@channel Standup at 3.", "Fixed grammar.", "m")
    assert "<!channel>" not in card  # a card is never a ping


def test_a_composed_card_carries_the_warning_only_for_a_mention_he_typed(near, store, settings):
    h = MessageHandlers(near, FakeLLM(writer=[reply("@channel Standup at 3."), reply("@channel Standup at 3.")]),
                        store, settings)
    h.compose("@channel standup at 3", "message-1", NOW)  # he typed it
    h.compose("standup at 3", "message-2", NOW)  # the model added it
    typed, added = (c.text for c in near.sent)
    assert "⚠️ Replying `post` notifies everyone in the channel" in typed
    assert "⚠️" not in added


def test_one_cap_for_the_note():
    assert mp.MAX_NOTE_CHARS is render.MAX_NOTE_CHARS or mp.MAX_NOTE_CHARS == render.MAX_NOTE_CHARS == 120
    long_note = "n" * 400
    assert len(mp.parse_reply(reply("Hi.", long_note), "hi")[1]) == 120
    assert len(render.render_message_dm("x", long_note).split("_Changed: ")[1].rstrip("_")) <= 120 + 1


# ---------------------------------------------------------------- I3: Slack markup in `say:` text, and real mentions of people

# the shapes of tests/fixtures/channel_page.txt: a mention with a name, a link with a label, an entity-escaped `&`
SAY_FIXTURE = ("to <@U0A3S7LUZ41|Iqbal Qazi> pls check arch doc "
               "<https://docs.google.com/document/d/1AbC/edit?usp=sharing&amp;ouid=123|arch doc> b4 huddle")
ARCH_URL = "https://docs.google.com/document/d/1AbC/edit?usp=sharing&ouid=123"


def test_slack_markup_becomes_plain_text_and_the_people_he_mentioned():
    text, people = mp.slack_to_text(SAY_FIXTURE)
    assert text == f"to @Iqbal Qazi pls check arch doc {ARCH_URL} b4 huddle"
    assert people == {"Iqbal Qazi": "U0A3S7LUZ41"}


def test_the_fixtures_own_names_with_commas_brackets_and_other_scripts_are_kept():
    text, people = mp.slack_to_text("<@U03GRE909MJ|Lion cho (조중현,Cho Chung Hyun)>, the report is attached. "
                                    "I met <@U06G6BSQY64|Lebo, Godfrey Emori (레보, 갓프리 에모리)> and <@U099LPY0E31|Piyush>")
    assert text == ("@Lion cho (조중현,Cho Chung Hyun), the report is attached. "
                    "I met @Lebo, Godfrey Emori (레보, 갓프리 에모리) and @Piyush")
    assert people == {"Lion cho (조중현,Cho Chung Hyun)": "U03GRE909MJ", "Lebo, Godfrey Emori (레보, 갓프리 에모리)": "U06G6BSQY64",
                      "Piyush": "U099LPY0E31"}


def test_channels_links_and_addresses_lose_their_markup():
    assert mp.slack_to_text("see <#C0C62C128D8|tf-leadership> and <http://reach.io|reach.io>, <https://example.com/a> "
                            "or <mailto:a@b.com|a@b.com>")[0] == (
        "see #tf-leadership and http://reach.io, https://example.com/a or mailto:a@b.com")
    assert mp.slack_to_text("<#C0C62C128D8>")[0] == "#C0C62C128D8"
    assert mp.slack_to_text("<@U0A3S7LUZ41>") == ("@U0A3S7LUZ41", {"U0A3S7LUZ41": "U0A3S7LUZ41"})  # no name: the id


def test_only_the_three_slack_entities_are_decoded_and_only_once():
    assert mp.slack_to_text("AT&amp;T &lt;b&gt; R&amp;D")[0] == "AT&T <b> R&D"
    assert mp.slack_to_text("&amp;lt; stays &lt;")[0] == "&lt; stays <"  # never decoded twice
    assert mp.slack_to_text("&quot;x&quot; &#39;y&#39; &nbsp;")[0] == "&quot;x&quot; &#39;y&#39; &nbsp;"  # nothing else


def test_a_typed_entity_for_a_mass_mention_stays_text_and_the_real_token_is_a_mention():
    text, _ = mp.slack_to_text("&lt;!channel&gt; standup")
    assert text == "<!channel> standup" and render.mass_mentions(text) == {"channel": 0, "here": 0}
    assert render.render_message_post(text, render.mass_mentions(text)) == "<\u200b!channel> standup"  # defused, text
    text, _ = mp.slack_to_text("<!channel> standup <!here|here>")
    assert text == "@channel standup @here" and render.mass_mentions(text) == {"channel": 1, "here": 1}


@pytest.mark.parametrize("label", ["channel", "Channel admin", "here team", "everyone", "evil.com", "see https://x.com",
                                   "a@b", "R&amp;D", "x" * 61, "&lt;b&gt;", "[x]"])
def test_a_display_name_that_is_not_plain_is_never_put_after_an_at_sign(label):
    """A teammate chooses their display name: one called "channel" must not turn Haseeb's mention of them into @channel."""
    text, people = mp.slack_to_text(f"to <@U0A3S7LUZ41|{label}> pls")
    assert text == "to @U0A3S7LUZ41 pls" and people == {"U0A3S7LUZ41": "U0A3S7LUZ41"}
    assert render.mass_mentions(text) == {"channel": 0, "here": 0}


def test_two_people_with_one_name_are_neither_a_mention():
    text, people = mp.slack_to_text("<@U0A3S7LUZ41|Sam> and <@U06G6BSQY64|Sam> and <@U099LPY0E31|Piyush>")
    assert text == "@Sam and @Sam and @Piyush" and people == {"Piyush": "U099LPY0E31"}


def say_post(near, store, settings, say_text, polished):
    ts = put_say(near, settings, "say: " + say_text)
    llm = FakeLLM(writer=[reply(polished)])
    h = MessageHandlers(near, llm, store, settings)
    process_dm(near, store, settings, {"message": h}, NOW)
    item = store.get_item(say_item_id(ts))
    h.post(item, reply_ts=f"{EPOCH}.000001")
    return item, llm, channel_sends(near)[0].text


def test_a_say_with_a_person_and_a_link_posts_a_real_mention_and_the_exact_link(near, store, settings):
    polished = f"@Iqbal Qazi, please check the arch doc before the huddle: {ARCH_URL}"
    item, llm, post = say_post(near, store, settings, SAY_FIXTURE, polished)
    user = llm.calls[0]["user"]  # the model saw plain text: no markup, no entity
    assert "@Iqbal Qazi" in user and "<@" not in user and "&amp;" not in user and f"{ARCH_URL}" in user
    assert item.body == polished
    card = card_of(near, settings, item)
    assert "<@U0A3S7LUZ41>" not in card.text and f"> @Iqbal Qazi, please check" in card.text  # a card is never a ping
    assert post == f"<@U0A3S7LUZ41>, please check the arch doc before the huddle: {ARCH_URL}"


def test_a_person_the_model_added_or_repeated_stays_plain_text(near, store, settings):
    polished = f"@Iqbal Qazi, @Iqbal Qazi and @Zakir Ullah: please check {ARCH_URL}"
    _, _, post = say_post(near, store, settings, SAY_FIXTURE, polished)
    assert post == f"<@U0A3S7LUZ41>, @Iqbal Qazi and @Zakir Ullah: please check {ARCH_URL}"  # once, as he typed it


def test_a_mention_inside_a_longer_name_or_word_is_not_a_mention(near, store, settings):
    _, _, post = say_post(near, store, settings, "ask <@U0A3S7LUZ41|Ann>", "Ask @Annabel, not Mr@Ann or (@Ann) or @Ann's")
    assert post == "Ask @Annabel, not Mr@Ann or (@Ann) or <@U0A3S7LUZ41>'s"  # a name's end must be a word end



def test_the_composers_at_names_are_plain_text(near, store, settings):
    """The page has no user ids: an @Name typed there is text, whatever the model keeps."""
    h = MessageHandlers(near, FakeLLM(writer=[reply("@Iqbal Qazi, please check.")]), store, settings)
    item = h.compose("@Iqbal Qazi pls check", "message-1", NOW)
    h.post(item, reply_ts=f"{EPOCH}.000001")
    assert channel_sends(near)[0].text == "@Iqbal Qazi, please check."


def test_a_say_whose_model_is_down_posts_his_own_words_with_the_mention_real(near, store, settings):
    ts = put_say(near, settings, "say: " + SAY_FIXTURE)
    h = MessageHandlers(near, FakeLLM(writer=[__import__("haseeb_agent.litellm", fromlist=["x"]).LLMError("down", infra=True)]),
                        store, settings)
    process_dm(near, store, settings, {"message": h}, NOW)
    item = store.get_item(say_item_id(ts))
    assert item.body == f"to @Iqbal Qazi pls check arch doc {ARCH_URL} b4 huddle"  # plain text, not Slack markup
    h.post(item, reply_ts=f"{EPOCH}.000001")
    assert channel_sends(near)[0].text == f"to <@U0A3S7LUZ41> pls check arch doc {ARCH_URL} b4 huddle"


def test_a_revision_keeps_the_people_of_the_item(near, store, settings):
    ts = put_say(near, settings, "say: " + SAY_FIXTURE)
    llm = FakeLLM(writer=[reply(f"@Iqbal Qazi, please check {ARCH_URL}"), reply(f"@Iqbal Qazi, check {ARCH_URL}")])
    h = MessageHandlers(near, llm, store, settings)
    process_dm(near, store, settings, {"message": h}, NOW)
    item = h.revise(store.get_item(say_item_id(ts)), "shorter")
    h.post(item, reply_ts=f"{EPOCH}.000001")
    assert channel_sends(near)[0].text == f"<@U0A3S7LUZ41>, check {ARCH_URL}"


def test_a_kept_polish_is_not_shared_between_two_say_texts_with_different_people(near, store, settings):
    assert mp.slack_to_text("hi <@U0A3S7LUZ41|Sam>")[0] == mp.slack_to_text("hi <@U06G6BSQY64|Sam>")[0]
    assert draft_key("hi @Sam", {"Sam": "U0A3S7LUZ41"}) != draft_key("hi @Sam", {"Sam": "U06G6BSQY64"})
    assert draft_key("hi @Sam") == draft_key("hi @Sam", {})


# ---------------------------------------------------------------- I1: R33's hold, M4 / M5: the `say:` scan's limits

class Hold:
    """The hold, by hand: `on` is the answer of MessageHandlers.holding."""

    def __init__(self, on=True):
        self.on = on

    def __call__(self, now):
        return self.on


def held_handlers(slack, store, settings, llm, hold):
    return MessageHandlers(slack, llm, store, settings, hold=hold)


def test_a_composer_row_waits_while_the_huddle_poll_must_not_be_blinded_and_runs_when_it_is_over(near, store, settings):
    hold, llm = Hold(True), FakeLLM(writer=[reply()])
    h = held_handlers(near, store, settings, llm, hold)
    cid = enqueue_compose(store, item_id="message-1")
    assert page_run(near, store, settings, h=h) == 0  # nothing was started
    row = store.page_command(cid)
    assert (row.state, row.result) == ("queued", None)  # not claimed, not failed: it waits its turn
    assert llm.calls == [] and near.sent == [] and store.get_item("message-1") is None
    hold.on = False  # today's Next Todo is out
    assert page_run(near, store, settings, h=h) == 1
    assert store.page_command(cid).state == "done" and store.get_item("message-1") is not None


def test_the_rows_behind_a_held_composer_row_still_run_even_inside_the_huddle_budget(near, store, settings):
    hold, llm = Hold(True), FakeLLM(writer=[reply(), reply("Version two.")])
    h = held_handlers(near, store, settings, llm, hold)
    item, _ = open_card(near, store, settings, llm)
    held = enqueue_compose(store, item_id="message-2")  # the oldest row
    skip = store.enqueue_page_command(item.id, "skip", None, body_hash(item), CLICK)
    # the huddle's rule (M3): one row per tick, and it must not be the held one
    assert process_page(near, store, settings, {"message": h}, NOW, limit=1) == 1
    assert store.page_command(skip).state == "done" and store.page_command(held).state == "queued"


def test_page_due_does_not_wake_the_tick_for_a_held_composer_row_alone():
    store = Store(":memory:")
    cid = store.enqueue_page_command("message-1", "compose", "x", "", NOW)
    assert page_due(store) is True and page_due(store, hold_compose=True) is False
    store.enqueue_page_command("item", "skip", None, "h", NOW)  # any other row is work
    assert page_due(store, hold_compose=True) is True
    store2 = Store(":memory:")
    cid2 = store2.enqueue_page_command("message-1", "compose", "x", "", NOW)
    store2.claim_page_command(cid2)  # a running row is to be recovered, held or not
    assert page_due(store2, hold_compose=True) is True


def test_a_row_that_waited_for_the_huddle_does_not_expire_but_one_that_waited_for_a_dead_agent_does(near, store, settings):
    old = NOW - timedelta(minutes=40)  # queued 40 minutes ago: past the usual 15
    fresh = enqueue_compose(store, item_id="message-1", at=old)
    store.set_cursor(HELD_KEY, (NOW - timedelta(minutes=1)).isoformat())  # a tick saw the hold a minute ago
    page_run(near, store, settings, FakeLLM(writer=[reply()]))
    assert store.page_command(fresh).state == "done"  # the 15 minutes count from the end of the hold
    dead = enqueue_compose(store, text="another message", item_id="message-2", at=old)
    store.set_cursor(HELD_KEY, (NOW - timedelta(hours=3)).isoformat())  # the Mac slept through it
    page_run(near, store, settings, FakeLLM(writer=[reply()]))
    assert (store.page_command(dead).state, store.page_command(dead).result) == ("failed", EXPIRED)
    assert store.get_item("message-2") is None


def test_only_a_composer_row_is_held_not_a_post_or_an_edit(near, store, settings):
    llm = FakeLLM(writer=[reply(), reply("Version two.")])
    hold = Hold(True)
    h = held_handlers(near, store, settings, llm, hold)
    item, _ = open_card(near, store, settings, llm)
    cid = store.enqueue_page_command(item.id, "post", None, body_hash(item), CLICK)
    page_run(near, store, settings, h=h)
    assert store.page_command(cid).state == "done" and len(channel_sends(near)) == 1


def test_a_say_waits_for_the_huddle_without_moving_the_scan_and_is_drafted_when_it_is_over(near, store, settings):
    hold, llm = Hold(True), FakeLLM(writer=[reply()])
    h = {"message": held_handlers(near, store, settings, llm, hold)}
    store.set_cursor("say_oldest", f"{EPOCH - 600}.000000")
    ts = put_say(near, settings, "say: " + ROUGH, ago=120)
    assert process_dm(near, store, settings, h, NOW) == 0
    assert store.get_item(say_item_id(ts)) is None and llm.calls == [] and store.get_cursor(f"say:{ts}") is None
    assert store.get_cursor("say_oldest") == f"{EPOCH - 600}.000000"  # never moved past the first unsettled `say:`
    hold.on = False
    assert process_dm(near, store, settings, h, NOW + timedelta(minutes=5)) == 1
    assert store.get_item(say_item_id(ts)).status == "delivered"


def test_a_command_in_a_thread_still_runs_during_the_hold(near, store, settings):
    hold, llm = Hold(True), FakeLLM(writer=[reply()])
    h = held_handlers(near, store, settings, llm, hold)
    item, parent = open_card(near, store, settings, llm)
    reply_in_thread(near, settings, parent, "post")
    process_dm(near, store, settings, {"message": h}, NOW + timedelta(minutes=5))
    assert len(channel_sends(near)) == 1 and store.get_item(item.id).status == "posted"


def settle_todays_todo(store, status="posted"):
    """Today's Next Todo is out: its item is terminal, which is what makes the hold end (`cli.tick_state`)."""
    store.upsert_item(Item(id=todo_item_id(NOW.date()), kind="todo", day=NOW.date(), status=status, body="Next Todo:",
                           title="Next Todo", dm_ts="1.1", source_ts=None, score=None, models={}, attempts=0,
                           edited=False, created_at=NOW, acted_at=None))


def test_the_real_predicate_holds_in_the_huddle_window_until_todays_todo_is_final(near, store, settings):
    hold = message_hold(store, settings)
    at = lambda h, m: NOW.replace(hour=h, minute=m)  # noqa: E731 - a Monday, Kuwait time
    assert hold(at(11, 0)) is False and hold(at(14, 24)) is False  # before the window
    assert hold(at(14, 25)) is True and hold(at(16, 4)) is True  # in it, today's todo not final
    assert hold(at(16, 5)) is False  # the window is over
    settle_todays_todo(store)
    assert hold(at(15, 0)) is False  # today's todo is out: nothing to protect


class Ticks:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


def test_over_the_huddle_both_wait_and_once_the_todo_is_out_both_run(near, store, settings, tmp_path):
    # the tick takes its locks (data/insights.lock) next to the database: that is this test's folder, not the repository's data/
    settings = replace(settings, db_path=str(tmp_path / "data" / "agent.sqlite"))
    llm = FakeLLM(writer=[reply("A composed message."), reply("A said message.")])
    h = MessageHandlers(near, llm, store, settings, hold=message_hold(store, settings))
    rt = Runtime(settings=settings, store=store, slack=near, llm=llm, handlers={"message": h})
    jobs = default_jobs(rt)
    start = NOW.replace(hour=15, minute=0)
    store.set_cursor("say_oldest", f"{int(start.timestamp()) - 600}.000000")
    compose = enqueue_compose(store, text="a composed message", item_id="message-1", at=start - timedelta(seconds=30))
    ts = f"{int(start.timestamp()) - 20}.000100"
    near.add_message(settings.self_dm_id, ts, "say: a said message", user_id=settings.haseeb_id)
    clock = Ticks(start)
    ran = run_tick(settings, store, jobs, clock=clock)
    assert PAGE_JOB not in ran  # the tick does not wake the page job for a row that waits
    assert store.page_command(compose).state == "queued" and store.get_item(say_item_id(ts)) is None
    assert llm.calls == [] and store.get_cursor(HELD_KEY) == start.isoformat()  # and a tick stamps the hold
    clock.now = start + timedelta(minutes=1)  # still the window, today's todo still not final
    run_tick(settings, store, jobs, clock=clock)
    assert store.page_command(compose).state == "queued" and llm.calls == []
    assert store.get_cursor(HELD_KEY) == clock.now.isoformat()
    settle_todays_todo(store)
    clock.now = start + timedelta(minutes=2)  # the Next Todo is out
    ran = run_tick(settings, store, jobs, clock=clock)
    assert PAGE_JOB in ran and "process_dm" in ran
    assert store.page_command(compose).state == "done" and store.get_item("message-1").body == "A composed message."
    assert store.get_item(say_item_id(ts)).body == "A said message."


def test_a_say_older_than_an_hour_is_answered_with_one_line_not_drafted(near, store, settings):
    llm = FakeLLM(writer=[reply()])
    h = {"message": MessageHandlers(near, llm, store, settings)}
    store.set_cursor("say_oldest", f"{EPOCH - 7200}.000000")  # the scan has not looked for two hours (the Mac was off)
    ts = put_say(near, settings, "say: " + ROUGH, ago=SAY_MAX_AGE_S + 60)
    assert process_dm(near, store, settings, h, NOW) == 1
    assert [c.text for c in dm_sends(near, settings)] == [SAY_TOO_OLD] == ["⚠️ too old to polish; send it again"]
    assert store.get_item(say_item_id(ts)) is None and llm.calls == [] and store.get_cursor(f"say:{ts}") == "1"
    process_dm(near, store, settings, h, NOW + timedelta(minutes=5))
    assert len(dm_sends(near, settings)) == 1  # said once


def test_a_say_just_under_an_hour_old_is_still_drafted(near, store, settings):
    h = {"message": MessageHandlers(near, FakeLLM(writer=[reply()]), store, settings)}
    store.set_cursor("say_oldest", f"{EPOCH - 7200}.000000")
    ts = put_say(near, settings, "say: " + ROUGH, ago=SAY_MAX_AGE_S - 60)
    assert process_dm(near, store, settings, h, NOW) == 1 and store.get_item(say_item_id(ts)) is not None


def test_the_age_of_a_say_counts_from_the_end_of_a_huddle_hold(near, store, settings):
    h = {"message": MessageHandlers(near, FakeLLM(writer=[reply()]), store, settings)}
    store.set_cursor("say_oldest", f"{EPOCH - 7200}.000000")
    store.set_cursor(HELD_KEY, (NOW - timedelta(minutes=2)).isoformat())  # it waited for the huddle until two minutes ago
    ts = put_say(near, settings, "say: " + ROUGH, ago=SAY_MAX_AGE_S + 600)
    assert process_dm(near, store, settings, h, NOW) == 1 and store.get_item(say_item_id(ts)) is not None
    assert dm_sends(near, settings)[0].text != SAY_TOO_OLD


def test_a_full_dm_page_that_may_hide_a_say_is_logged(near, store, settings, caplog):
    h = {"message": MessageHandlers(near, FakeLLM(writer=[reply()]), store, settings)}
    store.set_cursor("say_oldest", f"{EPOCH - 3000}.000000")
    for i in range(25):  # more chat than a page holds, all newer than the floor
        near.add_message(settings.self_dm_id, f"{EPOCH - 2000 + i}.000001", f"chat {i}", user_id=settings.haseeb_id)
    with caplog.at_level(logging.WARNING, logger="haseeb_agent.commands"):
        process_dm(near, store, settings, h, NOW)
    assert any("DM page is full; a say: message older than it may not have been seen" in r.message for r in caplog.records)


# ---------------------------------------------------------------- M2: only our own tags are refused

@pytest.mark.parametrize("message", [
    "Fixed the haseeb_agent dashboard.", "The haseeb_agent repo and haseeb_text are named so.",
    "See current_draft in the notes.", "Mail the HASEEB_ALERTS list.", "a < b and b > a, haseeb_x"])
def test_a_message_that_merely_names_haseeb_agent_is_not_refused(message):
    assert mp.parse_reply(reply(message), "haseeb_agent haseeb_text current_draft haseeb_alerts a b haseeb_x")[0] == message


@pytest.mark.parametrize("message", [
    "<haseeb_text>x</haseeb_text>", "text </haseeb_", "x < /haseeb_instruction >", "<current_draft>", "<HASEEB_PREFERENCES>",
    "ok <haseeb_anything_new>"])
def test_a_reply_that_repeats_one_of_our_tags_is_refused(message):
    with pytest.raises(ValueError, match="prompt's tags") as e:
        mp.parse_reply(reply(message), "words")
    assert not isinstance(e.value, mp.LinkChanged)


def test_a_message_that_names_haseeb_agent_is_polished_not_thrown_back(near, store, settings):
    polished = "I fixed the haseeb_agent dashboard."
    item = MessageHandlers(near, FakeLLM(writer=[reply(polished)]), store, settings).compose(
        "fixed the haseeb_agent dashboard", "message-1", NOW)
    assert item.body == polished and item.models  # polished by a model: no fallback, no "model unavailable"
    assert store.get_cursor(note_key("message-1")) == "Fixed grammar and made it clearer."


def test_an_answer_the_parser_refuses_everywhere_gets_its_own_note_not_model_unavailable(near, store, settings):
    llm = FakeLLM(writer=[reply("<haseeb_text>")])
    h = MessageHandlers(near, llm, store, settings)
    item = h.compose(ROUGH, "message-1", NOW)
    assert item.body == mp.normalize_raw(ROUGH) and len(llm.calls) == len(settings.models["feedback_writer"])
    assert store.get_cursor(note_key("message-1")) == "not polished (no usable answer)" != mp.NOT_POLISHED
