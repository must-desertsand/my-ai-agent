from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent.commands import Command, parse_command, process_dm, send_dm
from haseeb_agent.config import load_settings
from haseeb_agent.models import Item, Msg
from haseeb_agent.slack_gateway import SlackError, SlackPaused, SlackUnavailable
from haseeb_agent.store import Store
from tests.fakes import FakeSlack, LandsThenTimesOut, PausedSlack

ROOT = Path(__file__).resolve().parent.parent
HASEEB = "U08CZCGJW3E"
DM = "D08D9HFB8BB"
PARENT = "1800000100.000001"
LINK = "https://must.slack.com/archives/C0C62C128D8/p1800000300000001"
NOW = datetime.fromtimestamp(1_800_000_500, ZoneInfo("Asia/Kuwait"))


class Handlers:
    def __init__(self, fail_post=None, fail_revise=None):
        self.posted: list[str] = []
        self.revised: list[tuple[str, str]] = []
        self.fail_post, self.fail_revise = fail_post, fail_revise

    def post(self, item, reply_ts=None):
        if self.fail_post:
            raise self.fail_post
        self.posted.append(item.id)
        return LINK

    def revise(self, item, instruction):
        if self.fail_revise:
            raise self.fail_revise
        self.revised.append((item.id, instruction))
        return item


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


def make_item(store, id="i1", kind="insight", status="delivered", dm_ts=PARENT, title="Gold rush"):
    item = Item(
        id=id, kind=kind, day=NOW.date(), status=status, body="body", title=title,
        dm_ts=dm_ts, source_ts=None, score=90.0, models={}, attempts=0, edited=False,
        created_at=NOW, acted_at=None,
    )
    store.upsert_item(item)
    return item


def reply(ts, text, user_id=HASEEB, is_bot=False):
    return Msg(ts=ts, user_id=user_id, user_name="U", text=text, thread_ts=PARENT, reply_count=0,
               latest_reply=None, is_bot=is_bot)


def seed(slack, settings, replies, parent_ts=PARENT):
    latest = str(int(float(replies[-1].ts)))
    parent = slack.add_message(
        settings.self_dm_id, parent_ts, "item", user_id=HASEEB, reply_count=len(replies), latest_reply=latest
    )
    slack.add_thread(settings.self_dm_id, parent, replies)


def run(slack, store, settings, handlers=None):
    handlers = handlers or Handlers()
    return process_dm(slack, store, settings, {"insight": handlers, "todo": handlers, "feedback": handlers}, NOW)


def test_parse_command_variants():
    assert parse_command("POST ") == Command("post", None)
    assert parse_command("  post\n") == Command("post", None)
    assert parse_command("Shorter") == Command("shorter", None)
    assert parse_command(" SKIP ") == Command("skip", None)
    assert parse_command("edit: make it about Pakistan").arg == "make it about Pakistan"
    assert parse_command("Edit:   make it about Pakistan  ") == Command("edit", "make it about Pakistan")
    assert parse_command("hello") is None
    assert parse_command("post it now") is None
    assert parse_command("edit:") is None
    assert parse_command("") is None


def test_settings_have_dm_conversation_id(settings):
    assert settings.self_dm_channel == DM
    assert settings.self_dm_id == HASEEB


def test_post_reacts_eyes_then_check_and_posts_once(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    h = Handlers()
    assert run(slack, store, settings, h) == 1
    assert h.posted == ["i1"]
    assert [(r.channel_id, r.ts, r.emoji) for r in slack.reactions] == [
        (DM, "1800000200.000100", "eyes"),
        (DM, "1800000200.000100", "white_check_mark"),
    ]
    assert [(s.channel_id, s.thread_ts, s.text) for s in slack.sent] == [(HASEEB, PARENT, f"✅ Posted · {LINK}")]
    item = store.get_item("i1")
    assert item.status == "posted" and item.acted_at == NOW
    assert store.get_cursor("permalink:i1") == LINK


def test_same_reply_seen_twice_posts_once(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    h = Handlers()
    assert run(slack, store, settings, h) == 1
    assert run(slack, store, settings, h) == 0
    assert h.posted == ["i1"]
    assert len(slack.sent) == 1
    assert [r.emoji for r in slack.reactions] == ["eyes", "white_check_mark"]


def test_handled_cursor_is_set_before_the_slow_work(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    seen = []

    class Peek(Handlers):
        def post(self, item, reply_ts=None):
            seen.append(store.get_cursor(f"handled:{PARENT}"))
            return super().post(item)

    run(slack, store, settings, Peek())
    assert seen == ["1800000200.000100"]


def test_new_reply_after_first_tick_is_handled(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "hello")])
    h = Handlers()
    assert run(slack, store, settings, h) == 0
    seed(slack, settings, [reply("1800000200.000100", "hello"), reply("1800000210.000200", "skip")])
    slack.pages[settings.self_dm_id].pop(0)  # replace the stale parent
    assert run(slack, store, settings, h) == 1
    assert store.get_item("i1").status == "skipped"


def test_two_replies_in_one_second_both_run_in_order(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "shorter"), reply("1800000200.000200", "post")])
    h = Handlers()
    assert run(slack, store, settings, h) == 2
    assert h.revised and h.posted == ["i1"]


def test_post_on_posted_item_does_not_repost(slack, store, settings):
    make_item(store, status="posted")
    make_item(store, id="open", dm_ts="1800000050.000001")  # I5: the DM is read only while some item is open
    store.set_cursor("permalink:i1", LINK)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    h = Handlers()
    assert run(slack, store, settings, h) == 1
    assert h.posted == []
    assert [s.text for s in slack.sent] == [f"Already posted · {LINK}"]
    assert slack.reactions[-1].emoji == "white_check_mark"


def test_post_failure_reacts_warning_is_final_and_never_retried(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    h = Handlers(fail_post=SlackError("slack_send_message: tool-call mismatch\nsecond line"))
    assert run(slack, store, settings, h) == 1
    assert [r.emoji for r in slack.reactions] == ["eyes", "warning"]
    assert len(slack.sent) == 1 and slack.sent[0].text.startswith("⚠️ ")
    assert "\n" not in slack.sent[0].text and slack.sent[0].thread_ts == PARENT
    item = store.get_item("i1")
    assert item.status == "failed" and item.attempts == 1
    h.fail_post = None
    assert run(slack, store, settings, h) == 0  # same reply: not retried automatically
    assert h.posted == []


def test_failed_item_can_be_reissued_by_a_new_post_reply(slack, store, settings):
    make_item(store, status="failed")
    seed(slack, settings, [reply("1800000200.000100", "post")])
    h = Handlers()
    run(slack, store, settings, h)
    assert h.posted == ["i1"] and store.get_item("i1").status == "posted"


def test_failure_reacts_warning_and_counts_attempts(slack, store, settings):
    make_item(store)
    h = Handlers(fail_revise=RuntimeError("model down"))
    replies = []
    for n in range(1, 4):
        replies.append(reply(f"18000002{n:02d}.000100", "shorter"))
        slack.pages.clear()
        seed(slack, settings, list(replies))
        assert run(slack, store, settings, h) == 1
        item = store.get_item("i1")
        assert item.attempts == n
        assert item.status == ("failed" if n == 3 else "delivered")
    assert [r.emoji for r in slack.reactions].count("warning") == 3
    assert slack.sent[0].text == "⚠️ model down"


def test_edit_saves_pref(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "edit: make it about Pakistan")])
    h = Handlers()
    assert run(slack, store, settings, h) == 1
    assert store.prefs() == ["make it about Pakistan"]
    assert h.revised == [("i1", "make it about Pakistan")]
    assert [s.text for s in slack.sent] == ["✏️ Revised below"]
    assert [r.emoji for r in slack.reactions] == ["eyes", "white_check_mark"]
    item = store.get_item("i1")
    assert item.edited is True and item.status == "delivered"


def test_shorter_saves_pref_and_uses_fixed_instruction(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "Shorter")])
    h = Handlers()
    run(slack, store, settings, h)
    assert store.prefs() == ["shorter"]
    assert h.revised == [("i1", "Make it shorter and tighter, keep the facts.")]
    assert store.get_item("i1").edited is True


def test_skip_saves_pref_and_marks_skipped(slack, store, settings):
    make_item(store, title="Gold rush")
    seed(slack, settings, [reply("1800000200.000100", "skip")])
    h = Handlers()
    run(slack, store, settings, h)
    assert store.prefs() == ["skipped: Gold rush"]
    item = store.get_item("i1")
    assert item.status == "skipped" and item.acted_at == NOW
    assert [s.text for s in slack.sent] == ["⏭️ Skipped"]
    assert h.posted == [] and h.revised == []


def test_handlers_dispatch_by_item_kind(slack, store, settings):
    make_item(store, kind="todo")
    seed(slack, settings, [reply("1800000200.000100", "post")])
    a, b = Handlers(), Handlers()
    process_dm(slack, store, settings, {"insight": a, "todo": b}, NOW)
    assert a.posted == [] and b.posted == ["i1"]


def test_missing_handler_for_kind_is_a_warning_not_a_crash(slack, store, settings):
    make_item(store, kind="feedback")
    seed(slack, settings, [reply("1800000200.000100", "post")])
    assert process_dm(slack, store, settings, {"insight": Handlers()}, NOW) == 1
    assert [r.emoji for r in slack.reactions] == ["eyes", "warning"]


def test_non_haseeb_reply_ignored(slack, store, settings):
    make_item(store)
    seed(slack, settings, [
        reply("1800000200.000100", "post", user_id="U03GRE909MJ"),
        reply("1800000201.000100", "post", user_id=HASEEB, is_bot=True),
    ])
    h = Handlers()
    assert run(slack, store, settings, h) == 0
    assert h.posted == [] and slack.reactions == [] and slack.sent == []
    assert store.get_item("i1").status == "delivered"


def test_non_command_text_is_ignored_quietly(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "thanks, looks good")])
    assert run(slack, store, settings) == 0
    assert slack.reactions == [] and slack.sent == []


def test_thread_for_unknown_item_is_not_read(slack, store, settings):
    seed(slack, settings, [reply("1800000200.000100", "post")])
    slack.threads.clear()  # a read would raise SlackError; it must not even be attempted
    assert run(slack, store, settings) == 0


def test_unreadable_thread_is_skipped_then_retried_next_tick(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    slack.fail_next("thread unreadable: bad marker", method="read_thread")
    h = Handlers()
    assert run(slack, store, settings, h) == 0
    assert h.posted == []
    assert run(slack, store, settings, h) == 1
    assert h.posted == ["i1"]


def test_unreadable_dm_page_is_not_a_crash(slack, store, settings):
    slack.fail_next("boom", method="read_channel")
    assert run(slack, store, settings) == 0


def test_failing_reaction_does_not_stop_the_command(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    slack.fail_next("no reaction for you", method="react")
    h = Handlers()
    assert run(slack, store, settings, h) == 1
    assert h.posted == ["i1"] and store.get_item("i1").status == "posted"
    assert slack.sent[0].text == f"✅ Posted · {LINK}"


def test_old_threads_outside_the_window_are_not_read(slack, store, settings):
    from datetime import timedelta
    item = make_item(store, dm_ts="1700000000.000001")
    store.mark(item.id, "delivered", day=NOW.date() - timedelta(days=8))
    seed(slack, settings, [reply("1700000100.000100", "post")], parent_ts="1700000000.000001")
    h = Handlers()
    assert run(slack, store, settings, h) == 0
    assert h.posted == []


def test_unreadable_thread_does_not_advance_seen_cursor(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    slack.fail_next("thread unreadable", method="read_thread")
    run(slack, store, settings)
    assert store.get_cursor(f"seen:{PARENT}") is None
    run(slack, store, settings)
    assert store.get_cursor(f"seen:{PARENT}") == "1800000200"


class CountingSlack(FakeSlack):
    def __init__(self):
        super().__init__()
        self.thread_reads = 0

    def read_thread(self, channel_id, ts):
        self.thread_reads += 1
        return super().read_thread(channel_id, ts)


def test_unchanged_latest_reply_reads_the_thread_once(store, settings):
    slack = CountingSlack()
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "hello")])
    run(slack, store, settings)
    run(slack, store, settings)
    assert slack.thread_reads == 1
    seed(slack, settings, [reply("1800000200.000100", "hello"), reply("1800000205.000100", "hi")])
    slack.pages[settings.self_dm_id].pop(0)
    run(slack, store, settings)
    assert slack.thread_reads == 2


def test_send_dm_records_own_ts(slack, store, settings):
    ts = send_dm(slack, store, settings, "hello", thread_ts=PARENT)
    assert slack.sent[0].channel_id == settings.self_dm_id and slack.sent[0].thread_ts == PARENT
    assert store.get_cursor(f"own:{ts}") is not None


@pytest.mark.parametrize("text", ["skip", "edit: x", "post"])
def test_own_reply_that_reads_like_a_command_does_nothing(slack, store, settings, text):
    make_item(store)
    ts = send_dm(slack, store, settings, text, thread_ts=PARENT)  # a revised draft sent as Haseeb
    seed(slack, settings, [reply(ts, text)])
    h = Handlers()
    assert run(slack, store, settings, h) == 0
    assert h.posted == [] and h.revised == [] and slack.reactions == []
    assert store.get_item("i1").status == "delivered"


def test_status_replies_are_recorded_as_own(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "skip")])
    run(slack, store, settings)
    assert store.get_cursor(f"own:{slack.sent[0].ts}") is not None


@pytest.mark.parametrize("text", ["skip", "shorter", "edit: x"])
def test_skipped_item_rejects_other_commands(slack, store, settings, text):
    make_item(store, status="skipped")
    make_item(store, id="open", dm_ts="1800000050.000001")  # I5: the DM is read only while some item is open
    seed(slack, settings, [reply("1800000200.000100", text)])
    h = Handlers()
    run(slack, store, settings, h)
    assert [s.text for s in slack.sent] == ["⏭️ Already skipped"]
    assert h.revised == [] and store.prefs() == [] and store.get_item("i1").status == "skipped"


def test_post_overrides_skipped(slack, store, settings):
    make_item(store, status="skipped")
    make_item(store, id="open", dm_ts="1800000050.000001")  # I5: the DM is read only while some item is open
    seed(slack, settings, [reply("1800000200.000100", "post")])
    h = Handlers()
    run(slack, store, settings, h)
    assert h.posted == ["i1"] and store.get_item("i1").status == "posted"


@pytest.mark.parametrize("text", ["skip", "shorter", "edit: x"])
def test_posted_item_rejects_other_commands(slack, store, settings, text):
    make_item(store, status="posted")
    make_item(store, id="open", dm_ts="1800000050.000001")  # I5: the DM is read only while some item is open
    store.set_cursor("permalink:i1", LINK)
    seed(slack, settings, [reply("1800000200.000100", text)])
    h = Handlers()
    run(slack, store, settings, h)
    assert [s.text for s in slack.sent] == [f"Already posted · {LINK}"]
    assert h.revised == [] and store.prefs() == [] and store.get_item("i1").status == "posted"


def test_revise_does_not_write_back_a_stale_status(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "shorter")])

    class Racy(Handlers):
        def revise(self, item, instruction):
            store.mark(item.id, "posted", acted_at=NOW)  # e.g. the todo auto-post fired meanwhile
            return item

    run(slack, store, settings, Racy())
    item = store.get_item("i1")
    assert item.status == "posted" and item.edited is True


def test_failed_revise_does_not_unpost_or_fail_a_posted_item(slack, store, settings):
    make_item(store)
    store.mark("i1", "delivered", attempts=2)
    seed(slack, settings, [reply("1800000200.000100", "shorter")])

    class Racy(Handlers):
        def revise(self, item, instruction):
            store.mark(item.id, "posted", acted_at=NOW)
            raise RuntimeError("boom")

    run(slack, store, settings, Racy())
    assert store.get_item("i1").status == "posted"


def test_attempts_reset_after_success(slack, store, settings):
    make_item(store)
    store.mark("i1", "delivered", attempts=2)
    seed(slack, settings, [reply("1800000200.000100", "shorter")])
    run(slack, store, settings)
    assert store.get_item("i1").attempts == 0
    store.mark("i1", "delivered", attempts=2)
    seed(slack, settings, [reply("1800000200.000100", "shorter"), reply("1800000300.000100", "post")])
    slack.pages[settings.self_dm_id].pop(0)
    run(slack, store, settings)
    assert store.get_item("i1").attempts == 0 and store.get_item("i1").status == "posted"


def test_edit_pref_is_cleaned_capped_and_saved_only_on_success(slack, store, settings):
    make_item(store)
    long = "make it\nabout " + "x" * 600
    seed(slack, settings, [reply("1800000200.000100", f"edit: {long}")])
    run(slack, store, settings, Handlers(fail_revise=RuntimeError("down")))
    assert store.prefs() == []
    seed(slack, settings, [reply("1800000200.000100", f"edit: {long}"), reply("1800000300.000100", f"edit: {long}")])
    slack.pages[settings.self_dm_id].pop(0)
    h = Handlers()
    run(slack, store, settings, h)
    (pref,) = store.prefs()
    assert "\n" not in pref and len(pref) == 500 and pref.startswith("make it about x")
    assert h.revised[0][1] == pref


def test_mark_failing_in_failure_path_does_not_abort_the_tick(slack, store, settings, monkeypatch):
    make_item(store)
    make_item(store, id="i2", dm_ts="1800000101.000001")
    seed(slack, settings, [reply("1800000200.000100", "post")])
    seed(slack, settings, [reply("1800000201.000100", "post")], parent_ts="1800000101.000001")
    real = store.mark

    def flaky(id, status, **fields):
        if status == "failed":
            raise RuntimeError("db locked")
        return real(id, status, **fields)

    monkeypatch.setattr(store, "mark", flaky)
    h = Handlers(fail_post=SlackError("x"))
    assert run(slack, store, settings, h) == 2
    assert [r.emoji for r in slack.reactions].count("warning") == 2


def test_failing_post_does_not_overwrite_a_concurrent_posted(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])

    class Racy(Handlers):
        def post(self, item, reply_ts=None):
            store.mark(item.id, "posted", acted_at=NOW)  # the auto-post won the race
            raise SlackError("send failed")

    assert run(slack, store, settings, Racy()) == 1
    assert store.get_item("i1").status == "posted"
    assert [r.emoji for r in slack.reactions] == ["eyes", "warning"]


def test_status_reply_store_error_does_not_abort_the_tick(slack, store, settings, monkeypatch):
    make_item(store)
    make_item(store, id="i2", dm_ts="1800000101.000001")
    seed(slack, settings, [reply("1800000200.000100", "skip")])
    seed(slack, settings, [reply("1800000201.000100", "skip")], parent_ts="1800000101.000001")
    real = store.set_cursor

    def flaky(key, value):
        if key.startswith("own:"):
            raise RuntimeError("db locked")
        return real(key, value)

    monkeypatch.setattr(store, "set_cursor", flaky)
    assert run(slack, store, settings) == 2
    assert store.get_item("i2").status == "skipped"


def test_attempts_reset_after_successful_skip(slack, store, settings):
    make_item(store)
    store.mark("i1", "delivered", attempts=2)
    seed(slack, settings, [reply("1800000200.000100", "skip")])
    run(slack, store, settings)
    item = store.get_item("i1")
    assert item.status == "skipped" and item.attempts == 0


# -- I5 (R37): the DM read is skipped when nothing is open, and starts at the oldest open item --------------------

class ReadCountingSlack(FakeSlack):
    def __init__(self):
        super().__init__()
        self.reads: list[tuple[str | None, int]] = []

    def read_channel(self, channel_id, oldest, limit=50):
        self.reads.append((oldest, limit))
        return super().read_channel(channel_id, oldest, limit)


def test_i5_no_items_no_read(store, settings):
    slack = ReadCountingSlack()
    assert process_dm(slack, store, settings, {}, NOW) == 0
    assert slack.reads == []


@pytest.mark.parametrize("status", ["posted", "skipped", "expired"])
def test_i5_only_closed_items_no_read(store, settings, status):
    slack = ReadCountingSlack()
    make_item(store, status=status)
    process_dm(slack, store, settings, {}, NOW)
    assert slack.reads == []


def test_i5_open_item_older_than_7_days_no_read(store, settings):
    from datetime import timedelta
    slack = ReadCountingSlack()
    item = make_item(store)
    store.mark(item.id, "delivered", day=NOW.date() - timedelta(days=8))
    process_dm(slack, store, settings, {}, NOW)
    assert slack.reads == []


def test_i5_drafted_item_without_dm_ts_no_read(store, settings):
    slack = ReadCountingSlack()
    make_item(store, status="drafted", dm_ts=None)
    process_dm(slack, store, settings, {}, NOW)
    assert slack.reads == []


@pytest.mark.parametrize("status", ["delivered", "drafted", "failed"])
def test_i5_open_item_reads_from_its_dm_ts_with_limit_20(store, settings, status):
    # "failed" is read too: AUTOPOST_FAILED and a failed post both tell Haseeb to reply `post` on that item
    slack = ReadCountingSlack()
    make_item(store, id="old", status=status, dm_ts="1800000100.000001")
    make_item(store, id="new", status="delivered", dm_ts="1800000300.000001")
    process_dm(slack, store, settings, {}, NOW)
    assert slack.reads == [("1800000099.000001", 20)]
    assert store.get_cursor("dm_oldest") == "1800000099.000001"


def test_i5_oldest_follows_the_open_items(store, settings):
    slack = ReadCountingSlack()
    make_item(store, id="old", dm_ts="1800000100.000001")
    make_item(store, id="new", dm_ts="1800000300.000001")
    process_dm(slack, store, settings, {}, NOW)
    store.mark("old", "posted")
    process_dm(slack, store, settings, {}, NOW)
    assert slack.reads[-1] == ("1800000299.000001", 20)


def test_i5_commands_still_work_on_the_open_item(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000001", "skip")])
    assert process_dm(slack, store, settings, {"insight": Handlers()}, NOW) == 1
    assert store.get_item("i1").status == "skipped"


# -- I10 (R38): only insight skips teach a preference --------------------------------------------------------

@pytest.mark.parametrize("kind, expect", [("insight", ["skipped: Gold rush"]), ("feedback", []), ("todo", [])])
def test_i10_skip_pref_only_for_insights(slack, store, settings, kind, expect):
    make_item(store, kind=kind)
    seed(slack, settings, [reply("1800000200.000001", "skip")])
    process_dm(slack, store, settings, {kind: Handlers()}, NOW)
    assert store.get_item("i1").status == "skipped"
    assert store.prefs() == expect


def test_i10_edits_still_teach_for_every_kind(slack, store, settings):
    make_item(store, kind="feedback")
    seed(slack, settings, [reply("1800000200.000001", "edit: warmer")])
    process_dm(slack, store, settings, {"feedback": Handlers()}, NOW)
    assert store.prefs() == ["warmer"]


# -- M2: an interrupted post is caught on the next tick; M3: a failed post says it may have gone out ---------

INTERRUPTED = "⚠️ Post was interrupted — check the channel before re-posting"


def test_m2_inflight_is_set_before_post_and_cleared_after(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000001", "post")])
    seen = {}

    class Spy(Handlers):
        def post(self, item, reply_ts=None):
            seen["inflight"] = store.get_cursor(f"inflight:{item.id}")
            return super().post(item)

    process_dm(slack, store, settings, {"insight": Spy()}, NOW)
    assert seen["inflight"]  # set before the channel write
    assert not store.get_cursor("inflight:i1")
    assert store.get_item("i1").status == "posted"


def test_m2_inflight_cleared_after_a_failed_post_too(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000001", "post")])
    process_dm(slack, store, settings, {"insight": Handlers(fail_post=SlackError("boom"))}, NOW)
    assert not store.get_cursor("inflight:i1") and store.get_item("i1").status == "failed"


@pytest.mark.parametrize("status", ["delivered", "skipped", "failed"])
def test_m2_a_marker_left_behind_means_the_post_was_interrupted(slack, store, settings, status):
    make_item(store, status=status)
    store.set_cursor("inflight:i1", NOW.isoformat())  # the process died during handler.post
    process_dm(slack, store, settings, {"insight": Handlers()}, NOW)
    assert store.get_item("i1").status == "failed"
    (dm,) = [s for s in slack.sent if s.text == INTERRUPTED]
    assert dm.thread_ts == PARENT and store.get_cursor(f"own:{dm.ts}")
    assert not store.get_cursor("inflight:i1")
    process_dm(slack, store, settings, {"insight": Handlers()}, NOW)
    assert [s.text for s in slack.sent].count(INTERRUPTED) == 1


def test_m2_a_posted_item_with_a_marker_is_just_cleaned_up(slack, store, settings):
    make_item(store, status="posted")
    store.set_cursor("inflight:i1", NOW.isoformat())
    process_dm(slack, store, settings, {"insight": Handlers()}, NOW)
    assert store.get_item("i1").status == "posted" and slack.sent == []
    assert not store.get_cursor("inflight:i1")


def test_m2_interrupted_notice_is_retried_if_it_cannot_be_sent(slack, store, settings):
    make_item(store)
    store.set_cursor("inflight:i1", NOW.isoformat())
    slack.fail_next("dm down", method="send")
    process_dm(slack, store, settings, {"insight": Handlers()}, NOW)
    assert store.get_item("i1").status == "failed" and slack.sent == []
    process_dm(slack, store, settings, {"insight": Handlers()}, NOW)
    assert [s.text for s in slack.sent] == [INTERRUPTED] and not store.get_cursor("inflight:i1")


def test_m3_failed_post_warns_it_may_have_gone_out(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000001", "post")])
    process_dm(slack, store, settings, {"insight": Handlers(fail_post=SlackError("slack_send_message: timeout"))}, NOW)
    (dm,) = slack.sent
    assert dm.text == ("⚠️ Post may have gone out — check the channel before re-posting "
                       "(slack_send_message: timeout)")


def test_m3_a_post_refused_before_any_send_keeps_its_plain_reason(slack, store, settings):
    make_item(store)
    seed(slack, settings, [reply("1800000200.000001", "post")])
    h = Handlers(fail_post=LookupError("the huddle thread isn't known yet, so post it in the huddle thread yourself"))
    process_dm(slack, store, settings, {"insight": h}, NOW)
    assert slack.sent[0].text == "⚠️ the huddle thread isn't known yet, so post it in the huddle thread yourself"


# -- N3: outages are not "may have landed" and do not count toward the DM resend cap --------------------------

def test_n3_outage_never_reaches_the_cap(tmp_path):
    """The probe: a run that failed before any tool_use used to count as a tool-call mismatch, so the second
    failure already gave up."""
    from haseeb_agent.commands import dm_send_failed
    st = Store(tmp_path / "db.sqlite")
    down = SlackUnavailable("slack_send_message: Slack unavailable (claude exit 1)")
    assert [dm_send_failed(st, "x:0", down) for _ in range(10)] == [False] * 10


def test_n3_mismatch_then_outage_then_failure(tmp_path):
    from haseeb_agent.commands import dm_send_failed
    st = Store(tmp_path / "db.sqlite")
    down = SlackUnavailable("slack_send_message: Slack unavailable (claude exit 1)")
    assert dm_send_failed(st, "x:0", SlackError("slack_send_message: tool-call mismatch: 2 results")) is False
    assert dm_send_failed(st, "x:0", down) is False      # the outage resent nothing: the one resend is still left
    assert dm_send_failed(st, "x:0", SlackError("slack_send_message: channel_not_found")) is True


def test_a_send_whose_connector_was_not_ready_never_reaches_the_cap(tmp_path):
    """2026-10-23: the HackerRank card gave up after 3 runs that started before the Slack connector was connected.
    Nothing was sent by any of them: like an outage, they do not count."""
    from haseeb_agent.commands import dm_send_failed
    from haseeb_agent.slack_gateway import SlackNotReady
    st = Store(tmp_path / "db.sqlite")
    late = SlackNotReady("slack_send_message: Slack unavailable (the connector was not connected yet)")
    assert [dm_send_failed(st, "x:0", late) for _ in range(5)] == [False] * 5


def test_n3_plain_failures_still_cap_at_three(tmp_path):
    from haseeb_agent.commands import dm_send_failed
    st = Store(tmp_path / "db.sqlite")
    err = SlackError("slack_send_message: the model did not call the tool")
    assert [dm_send_failed(st, "x:0", err) for _ in range(3)] == [False, False, True]


# -- R43: a timeout counts toward the resend cap at most once per 10 minutes, and a resend looks for the message ----

import json  # noqa: E402
from datetime import UTC, timedelta  # noqa: E402

from haseeb_agent.commands import dm_send_failed  # noqa: E402
from haseeb_agent.slack_gateway import SlackTimeout  # noqa: E402
from tests.fakes import TIMEOUT  # noqa: E402

def test_r43_a_timeout_counts_once_per_ten_minutes(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    t0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    err = SlackTimeout(TIMEOUT)
    assert dm_send_failed(st, "x:0", err, now=t0) is False                          # counted: n=1
    assert dm_send_failed(st, "x:0", err, now=t0 + timedelta(minutes=5)) is False   # inside 10 min: not counted
    assert dm_send_failed(st, "x:0", err, now=t0 + timedelta(minutes=9, seconds=59)) is False
    state = json.loads(st.get_cursor("dm_fail:x:0"))
    assert state["n"] == 1 and state["after_mismatch"] == 1
    # 10 min after the counted one it counts again; two may-have-landed failures end the resends
    assert dm_send_failed(st, "x:0", err, now=t0 + timedelta(minutes=10)) is True


def test_r43_ten_minutes_run_from_the_last_counted_timeout(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    t0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    err = SlackTimeout(TIMEOUT)
    dm_send_failed(st, "k", err, now=t0)
    dm_send_failed(st, "k", err, now=t0 + timedelta(minutes=8))    # ignored, so it does not move the window
    assert json.loads(st.get_cursor("dm_fail:k"))["n"] == 1
    dm_send_failed(st, "k", err, now=t0 + timedelta(minutes=11))   # counted: 11 min after the counted one
    assert json.loads(st.get_cursor("dm_fail:k"))["n"] == 2


def test_r43_other_failures_still_count_every_time(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    t0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    err = SlackError("slack_send_message: the model did not call the tool")
    assert [dm_send_failed(st, "y", err, now=t0) for _ in range(3)] == [False, False, True]


def test_r43_a_plain_failure_inside_the_timeout_window_still_counts(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    t0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    dm_send_failed(st, "z", SlackTimeout(TIMEOUT), now=t0)
    dm_send_failed(st, "z", SlackError("channel_not_found"), now=t0 + timedelta(minutes=1))
    assert json.loads(st.get_cursor("dm_fail:z"))["n"] == 2


def test_r43_a_timeout_that_landed_is_adopted_not_resent(store, settings):
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "**1. Gold**\n\nbody text")
    (landed,) = slack.sent
    ts = send_dm(slack, store, settings, "**1. Gold**\n\nbody text")
    assert ts == landed.ts and len(slack.sent) == 1          # nothing was sent again
    assert slack.reads == [("channel", None)]                 # one DM read, on the resend only
    assert store.get_cursor(f"own:{ts}") == "1"               # recorded as own (R23)
    send_dm(slack, store, settings, "**1. Gold**\n\nbody text")  # the marker is gone: a later identical send is new
    assert len(slack.sent) == 2 and len(slack.reads) == 1


def test_r43_a_timeout_that_did_not_land_is_sent_after_one_read(store, settings):
    slack = LandsThenTimesOut(lands=False)
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "hello there")
    ts = send_dm(slack, store, settings, "hello there")
    assert [s.text for s in slack.sent] == ["hello there"] and slack.sent[0].ts == ts
    assert len(slack.reads) == 1


def test_r43_matching_is_on_the_whole_text_and_ignores_markdown_the_connector_rewrites(store, settings):
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "**1. Gold rush: Ghana’s plan**\n\nlong body one")
    # what the page shows for the landed message is rendered differently
    slack.pages[HASEEB][0] = replace(slack.pages[HASEEB][0], text="*1. Gold rush: Ghana’s plan*\n\nlong body one")
    ts = send_dm(slack, store, settings, "**1. Gold rush: Ghana’s plan**\n\nlong body one")  # the same message
    assert ts == slack.sent[0].ts and len(slack.sent) == 1


def test_r43_the_same_first_line_with_another_body_is_another_message(store, settings):
    """Changed in the composer fix round (review C1): this test used to adopt the landed card for a resend with a
    different body ("long body two") because only the first line was compared. Every message draft starts with the same
    header, so that put one message's card under another's text. Only the same text is adopted now; a missed adoption
    is a duplicate card, never a wrong one."""
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "**1. Gold rush: Ghana’s plan**\n\nlong body one")
    ts = send_dm(slack, store, settings, "**1. Gold rush: Ghana’s plan**\n\nlong body two")
    assert ts == slack.sent[1].ts and ts != slack.sent[0].ts and len(slack.sent) == 2  # sent for real
    assert slack.sent[1].text.endswith("long body two")


def test_r43_a_marker_belongs_to_one_text_so_another_send_does_not_clear_it(store, settings):
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "**Header**\n\nfirst body")  # lands, times out
    other = send_dm(slack, store, settings, "**Header**\n\nsecond body")  # another message, same first line
    assert other == slack.sent[1].ts and len(slack.sent) == 2
    again = send_dm(slack, store, settings, "**Header**\n\nfirst body")  # the first one's retry finds its own card
    assert again == slack.sent[0].ts and len(slack.sent) == 2


def test_r43_the_connectors_sent_using_line_is_allowed_for(store, settings):
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "status line\n\nsome detail")
    slack.pages[HASEEB][0] = replace(slack.pages[HASEEB][0], text="status line\n\nsome detail\n\n_Sent using_ @Claude")
    assert send_dm(slack, store, settings, "status line\n\nsome detail") == slack.sent[0].ts and len(slack.sent) == 1


def test_r43_nothing_else_after_the_text_is_allowed_for(store, settings):
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "status line")
    slack.pages[HASEEB][0] = replace(slack.pages[HASEEB][0], text="status line and a lot more that is a different message")
    ts = send_dm(slack, store, settings, "status line")
    assert ts == slack.sent[1].ts and len(slack.sent) == 2


def test_r43_a_link_matches_whether_it_is_markdown_or_slack_markup(store, settings):
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "1. [Alpha post (alpha.com)](https://alpha.com/p?a=1&b=2) is the source")
    slack.pages[HASEEB][0] = replace(slack.pages[HASEEB][0],
                                     text="1. <https://alpha.com/p?a=1&b=2|Alpha post (alpha.com)> is the source")
    assert send_dm(slack, store, settings, "1. [Alpha post (alpha.com)](https://alpha.com/p?a=1&b=2) is the source"
                   ) == slack.sent[0].ts and len(slack.sent) == 1


def test_r43_a_threaded_part_is_looked_for_in_its_thread(store, settings):
    slack = LandsThenTimesOut(timeouts=0)
    parent = slack.send(HASEEB, "parent line")
    slack.timeouts = 1
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "second part\nmore", thread_ts=parent)
    landed = slack.sent[-1]
    ts = send_dm(slack, store, settings, "second part\nmore", thread_ts=parent)
    assert ts == landed.ts and len(slack.sent) == 2
    assert slack.reads == [("thread", parent)]            # replies are not on the channel page: read the thread


def test_r43_only_haseebs_own_unrecorded_messages_are_adopted(store, settings):
    slack = LandsThenTimesOut(lands=False)
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "status line")
    slack.add_message(HASEEB, "1800000050.000001", "status line", user_id="U0A3S7LUZ41")       # someone else
    slack.add_message(HASEEB, "1800000051.000001", "status line", user_id=HASEEB, is_bot=True)  # a bot
    old = slack.add_message(HASEEB, "1800000052.000001", "status line", user_id=HASEEB)         # an earlier own send
    store.set_cursor(f"own:{old.ts}", "1")
    slack.add_message(HASEEB, "1800000053.000001", "other line", user_id=HASEEB)                # another first line
    ts = send_dm(slack, store, settings, "status line")
    assert len(slack.sent) == 1 and slack.sent[0].ts == ts          # none of them was adopted: sent for real


def test_r43_an_unreadable_dm_falls_back_to_sending(store, settings):
    slack = LandsThenTimesOut(lands=False)
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "hello")
    slack.fail_next("down", method="read_channel")
    assert send_dm(slack, store, settings, "hello") == slack.sent[0].ts


def test_r43_no_read_when_the_earlier_failure_was_not_a_timeout(store, settings):
    slack = LandsThenTimesOut(timeouts=0)
    slack.fail_next("slack_send_message: tool-call mismatch: 2 results", method="send")
    with pytest.raises(SlackError):
        send_dm(slack, store, settings, "hello")
    send_dm(slack, store, settings, "hello")
    assert slack.reads == []


def test_r43_a_normal_send_reads_nothing(store, settings):
    slack = LandsThenTimesOut(timeouts=0)
    send_dm(slack, store, settings, "hello")
    assert slack.reads == []


# -- R43 fix round: the marker carries the attempt time, expires after an hour, and bounds what can be adopted -----

import time as _time  # noqa: E402

T0 = 1_800_000_000.0  # just before FakeSlack's first ts (1800000001)


def at_t(seconds):
    return lambda: T0 + seconds


def _landed_timeout(store, settings, text="status line", clock=None, thread_ts=None):
    slack = LandsThenTimesOut()
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, text, thread_ts=thread_ts, clock=clock or at_t(0))
    return slack


def test_r43_the_marker_holds_the_attempt_time(store, settings):
    _landed_timeout(store, settings)
    (key, value), = store.cursors("dm_timeout:").items()
    assert float(value) == T0


def test_r43_a_day_one_status_line_does_not_swallow_day_twos(store, settings):
    slack = _landed_timeout(store, settings)           # day 1: landed, then timed out; never retried
    day1 = slack.sent[0].ts
    ts = send_dm(slack, store, settings, "status line", clock=at_t(86_400))   # day 2: the same first line
    assert ts != day1 and len(slack.sent) == 2         # sent for real
    assert slack.reads == []                            # the expired marker costs no read
    assert store.get_cursor(f"own:{ts}") == "1"
    assert not any(v for v in store.cursors("dm_timeout:").values())  # and is cleared


def test_r43_the_marker_expires_after_one_hour(store, settings):
    slack = _landed_timeout(store, settings)
    assert send_dm(slack, store, settings, "status line", clock=at_t(3_599)) == slack.sent[0].ts
    slack = _landed_timeout(store, settings, text="other line")
    ts = send_dm(slack, store, settings, "other line", clock=at_t(3_601))
    assert len(slack.sent) == 2 and ts == slack.sent[1].ts


def test_r43_only_messages_from_the_attempt_on_can_be_adopted(store, settings):
    slack = LandsThenTimesOut(lands=False)
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "status line", clock=at_t(0))
    slack.add_message(HASEEB, f"{int(T0) - 31}.000001", "status line", user_id=HASEEB)    # 31 s before: too old
    slack.add_message(HASEEB, f"{int(T0) - 86_400}.000001", "status line", user_id=HASEEB)  # yesterday's
    ts = send_dm(slack, store, settings, "status line", clock=at_t(5))
    assert len(slack.sent) == 1 and slack.sent[0].ts == ts


def test_r43_a_message_within_30_seconds_before_the_attempt_counts(store, settings):
    slack = LandsThenTimesOut(lands=False)
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "status line", clock=at_t(0))
    near = slack.add_message(HASEEB, f"{int(T0) - 29}.000001", "status line", user_id=HASEEB)
    assert send_dm(slack, store, settings, "status line", clock=at_t(5)) == near.ts and slack.sent == []


def test_r43_the_newest_qualifying_match_is_adopted(store, settings):
    slack = LandsThenTimesOut(lands=False)
    with pytest.raises(SlackTimeout):
        send_dm(slack, store, settings, "status line", clock=at_t(0))
    slack.add_message(HASEEB, f"{int(T0) + 10}.000001", "status line", user_id=HASEEB)
    newest = slack.add_message(HASEEB, f"{int(T0) + 20}.000001", "status line", user_id=HASEEB)
    assert send_dm(slack, store, settings, "status line", clock=at_t(30)) == newest.ts


def test_r43_a_second_timeout_keeps_the_first_attempt_time(store, settings):
    slack = LandsThenTimesOut(timeouts=2, lands=False)
    for t in (0, 60):
        with pytest.raises(SlackTimeout):
            send_dm(slack, store, settings, "status line", clock=at_t(t))
    assert [float(v) for v in store.cursors("dm_timeout:").values()] == [T0]


def test_r43_a_naive_now_never_raises_in_dm_send_failed(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    err = SlackTimeout(TIMEOUT)
    assert dm_send_failed(st, "n", err, now=datetime(2026, 10, 19, 9, 0)) is False
    assert dm_send_failed(st, "n", err, now=datetime(2026, 10, 19, 9, 5)) is False      # still inside the 10 minutes
    assert json.loads(st.get_cursor("dm_fail:n"))["n"] == 1
    # an aware `now` after a naive one is fine too
    assert dm_send_failed(st, "n", err, now=datetime(2026, 10, 19, 9, 11, tzinfo=UTC)) is True


def test_r43_a_corrupt_timeout_stamp_does_not_raise(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    st.set_cursor("dm_fail:c", json.dumps({"n": 1, "after_mismatch": 1, "t_timeout": "not a date"}))
    assert dm_send_failed(st, "c", SlackTimeout(TIMEOUT), now=datetime(2026, 10, 19, 9, 0)) is True


def test_attach_is_a_command():
    """R50."""
    assert parse_command(" Attach\n") == Command("attach", None)
    assert parse_command("attach it") is None


def test_attach_runs_on_a_posted_insight_and_only_on_insights(tmp_path):
    from dataclasses import replace as _replace
    from datetime import datetime, timezone
    from haseeb_agent.commands import _Runner
    from haseeb_agent.models import Item

    class H:
        def __init__(self):
            self.calls = []

        def attach(self, item):
            self.calls.append(item.id)
            return "📎 Deck attached to the post"

    h = H()
    runner = _Runner(None, None, None, {"insight": h, "todo": object()}, datetime.now(timezone.utc))
    item = Item(id="insight-1", kind="insight", day=datetime.now().date(), title="t", status="posted", body="",
                dm_ts="1.0", source_ts=None, score=None, models={}, attempts=0, edited=False, created_at=None,
                acted_at=None)
    assert runner._execute(item, Command("attach")) == "📎 Deck attached to the post" and h.calls == ["insight-1"]
    with pytest.raises(ValueError):
        runner._execute(_replace(item, id="todo-1", kind="todo"), Command("attach"))


# ---------------------------------------------------------------- M1: a write the pause refused is nothing sent

def feedback_item(store, id="feedback-1800000050.000001"):
    store.upsert_item(Item(id=id, kind="feedback", day=NOW.date(), status="delivered", body="draft",
                           title="Feedback: X", dm_ts=PARENT, source_ts="1800000050.000001", score=90.0, models={},
                           attempts=0, edited=False, created_at=NOW, acted_at=None))
    return id


def test_a_post_the_pause_refused_leaves_no_trace_and_posts_after_resume(store, settings):
    """The gateway refuses a write with SlackPaused before any call: the channel never saw the post. So the item is
    not failed, no failure time is recorded (a `post` after Resume is a first post, not an informed override), the
    send marker the handler set is taken back, and there is no reaction or DM reply (every DM write is refused too)."""
    from haseeb_agent.commands import post_failed_key, send_key
    from haseeb_agent.jobs.feedback import FeedbackHandlers
    slack = PausedSlack()
    iid = feedback_item(store)
    handlers = {"feedback": FeedbackHandlers(slack, None, store, settings)}
    seed(slack, settings, [reply("1800000200.000100", "post")])
    assert process_dm(slack, store, settings, handlers, NOW) == 1
    item = store.get_item(iid)
    assert (item.status, item.attempts) == ("delivered", 0)
    assert not store.get_cursor(post_failed_key(iid)) and not store.get_cursor(send_key(iid))
    assert not store.get_cursor(f"inflight:{iid}")  # nothing started: recover_interrupted must not fail it later
    assert slack.sent == [] and slack.reactions == []
    assert slack.refused.count("send") == 1  # the channel write; no status reply was even tried
    # Resume: Haseeb says `post` again and it simply posts
    slack.paused = False
    parent = slack.pages[settings.self_dm_id][0]
    slack.pages[settings.self_dm_id][0] = replace(parent, latest_reply="1800000400", reply_count=2)
    _, replies = slack.threads[(settings.self_dm_id, PARENT)]
    slack.threads[(settings.self_dm_id, PARENT)] = (parent, [*replies, reply("1800000400.000100", "post")])
    assert process_dm(slack, store, settings, handlers, NOW) == 1
    assert store.get_item(iid).status == "posted"
    assert [(x.channel_id, x.thread_ts) for x in slack.sent if x.channel_id == settings.channel_id] == [
        (settings.channel_id, "1800000050.000001")]


def test_a_revision_the_pause_refused_does_not_count_as_an_attempt(store, settings):
    """Only `post` has a send marker; `shorter` and `edit` count attempts toward the item failing. A refusal by the
    pause is not an attempt."""
    slack = PausedSlack()
    make_item(store)
    handlers = Handlers(fail_revise=SlackPaused("slack_send_message: the agent is paused, nothing was sent"))
    seed(slack, settings, [reply("1800000200.000100", "shorter")])
    assert run(slack, store, settings, handlers) == 1
    item = store.get_item("i1")
    assert (item.status, item.attempts) == ("delivered", 0)
    assert slack.sent == [] and slack.reactions == []


# ---------------------------------------------------------------- N2: a command the pause refused is told after Resume

def refused_by_the_pause(store, settings, replies):
    """A feedback item whose DM thread holds these replies, handled while the gateway is paused. Returns (slack, handlers,
    item id): the paused fake, the real feedback handlers on it, and the id."""
    from haseeb_agent.jobs.feedback import FeedbackHandlers
    slack = PausedSlack()
    iid = feedback_item(store)
    handlers = {"feedback": FeedbackHandlers(slack, None, store, settings)}
    seed(slack, settings, replies)
    assert process_dm(slack, store, settings, handlers, NOW) == len(replies)
    assert slack.sent == []  # during the pause nothing is said: every DM write is refused
    return slack, handlers, iid


def notices_sent(slack):
    from haseeb_agent.commands import PAUSED_COMMAND_NOTICE
    return [x for x in slack.sent if x.text == PAUSED_COMMAND_NOTICE]


def test_a_command_the_pause_refused_is_told_once_after_resume(store, settings):
    """The reply's `handled:` cursor had moved, so the command will not run again, and the pause left no reaction or
    reply: without a word Haseeb would never know. One notice per item, however many commands were refused."""
    from haseeb_agent.commands import PAUSED_COMMAND_NOTICE, PAUSED_NOTICE_PREFIX
    slack, handlers, iid = refused_by_the_pause(
        store, settings, [reply("1800000200.000100", "post"), reply("1800000210.000100", "shorter")])
    assert store.cursors(PAUSED_NOTICE_PREFIX) == {PAUSED_NOTICE_PREFIX + iid: NOW.isoformat()}  # queued, one
    slack.paused = False  # Resume
    process_dm(slack, store, settings, handlers, NOW)  # the first run after it
    sent = notices_sent(slack)
    assert PAUSED_COMMAND_NOTICE == "A command you sent during the pause was not run — send it again"
    assert [(x.channel_id, x.thread_ts) for x in sent] == [(HASEEB, PARENT)]  # in the item's DM thread, once
    assert store.get_cursor(f"own:{sent[0].ts}") == "1"  # recorded as the agent's own message (R23)
    assert store.cursors(PAUSED_NOTICE_PREFIX) == {}
    process_dm(slack, store, settings, handlers, NOW)
    assert len(notices_sent(slack)) == 1  # not again
    assert store.get_item(iid).status == "delivered" and not [x for x in slack.sent if x.channel_id != HASEEB]


def test_the_paused_command_notice_is_retried_until_it_is_sent(store, settings):
    from haseeb_agent.commands import PAUSED_NOTICE_PREFIX
    slack, handlers, iid = refused_by_the_pause(store, settings, [reply("1800000200.000100", "post")])
    slack.paused = False
    slack.fail_next("dm down", method="send")
    process_dm(slack, store, settings, handlers, NOW)
    assert notices_sent(slack) == [] and PAUSED_NOTICE_PREFIX + iid in store.cursors(PAUSED_NOTICE_PREFIX)  # kept
    process_dm(slack, store, settings, handlers, NOW)
    assert len(notices_sent(slack)) == 1 and store.cursors(PAUSED_NOTICE_PREFIX) == {}


def test_the_paused_command_notice_gives_up_after_the_dm_send_cap(store, settings):
    from haseeb_agent.commands import MAX_DM_SENDS, PAUSED_NOTICE_PREFIX
    slack, handlers, iid = refused_by_the_pause(store, settings, [reply("1800000200.000100", "post")])
    slack.paused = False
    for n in range(MAX_DM_SENDS):
        assert store.cursors(PAUSED_NOTICE_PREFIX), n  # still queued until the last counted failure
        slack.fail_next("dm down", method="send")
        process_dm(slack, store, settings, handlers, NOW)
    assert store.cursors(PAUSED_NOTICE_PREFIX) == {} and notices_sent(slack) == []
    process_dm(slack, store, settings, handlers, NOW)
    assert notices_sent(slack) == []  # and it does not come back


def test_a_paused_gateway_keeps_the_notice_queued(store, settings):
    """Resume and Pause again before the notice goes out: the refusal is an outage-like one, never counted."""
    from haseeb_agent.commands import MAX_DM_SENDS, PAUSED_NOTICE_PREFIX
    slack, handlers, iid = refused_by_the_pause(store, settings, [reply("1800000200.000100", "post")])
    for _ in range(MAX_DM_SENDS + 2):  # more runs than the cap allows failures
        process_dm(slack, store, settings, handlers, NOW)  # still paused: refused again
    assert store.cursors(PAUSED_NOTICE_PREFIX) and notices_sent(slack) == []
    slack.paused = False
    process_dm(slack, store, settings, handlers, NOW)
    assert len(notices_sent(slack)) == 1


def test_a_failed_command_that_is_not_the_pause_queues_no_notice(store, settings):
    from haseeb_agent.commands import PAUSED_NOTICE_PREFIX
    slack = FakeSlack()
    make_item(store)
    seed(slack, settings, [reply("1800000200.000100", "post")])
    assert run(slack, store, settings, Handlers(fail_post=SlackError("boom"))) == 1
    assert store.cursors(PAUSED_NOTICE_PREFIX) == {}

