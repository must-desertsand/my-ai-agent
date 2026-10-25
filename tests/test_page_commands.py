"""Task 5: dashboard commands run in the tick, through the DM command path (commands._Runner)."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import cli
from haseeb_agent.commands import EARLIER_POST as EARLIER_POST_TEXT
from haseeb_agent.commands import MAY_HAVE_POSTED, process_dm
from haseeb_agent.config import load_settings
from haseeb_agent.jobs import decks
from haseeb_agent.jobs.todo import AUTOPOST_CANCELLED, TodoHandlers, run_todo
from haseeb_agent.models import Item, Msg
from haseeb_agent.page_commands import DASHBOARD_NOTE, body_hash, process_page
from haseeb_agent.slack_gateway import SlackError, SlackPaused, SlackTimeout
from haseeb_agent.store import Store
from tests.fakes import FakeSlack, PausedSlack
from tests import test_todo as T

ROOT = Path(__file__).resolve().parent.parent
KW = ZoneInfo("Asia/Kuwait")
HASEEB = "U08CZCGJW3E"
DM = "D08D9HFB8BB"
PARENT = "1800000100.000001"
LINK = "https://must.slack.com/archives/C0C62C128D8/p1800000300000001"
NOW = datetime.fromtimestamp(1_800_000_500, KW)
CLICK = datetime.fromtimestamp(1_800_000_200, KW)  # after the item's DM message, before NOW


class Handlers:
    def __init__(self, fail_post=None):
        self.posted: list[tuple[str, str | None]] = []
        self.attached: list[str] = []
        self.revised: list[tuple[str, str]] = []
        self.fail_post = fail_post

    def post(self, item, reply_ts=None):
        self.posted.append((item.id, reply_ts))
        if self.fail_post:
            raise self.fail_post
        return LINK

    def revise(self, item, instruction):
        self.revised.append((item.id, instruction))
        return item

    def attach(self, item):
        self.attached.append(item.id)
        return "📎 Deck attached to the post"


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


def make_item(store, id="i1", kind="insight", status="delivered", body="body", dm_ts=PARENT):
    item = Item(id=id, kind=kind, day=NOW.date(), status=status, body=body, title="Gold rush", dm_ts=dm_ts,
                source_ts=None, score=90.0, models={}, attempts=0, edited=False, created_at=NOW, acted_at=None)
    store.upsert_item(item)
    return item


def enqueue(store, verb="post", item_id="i1", arg=None, shown=None, at=CLICK):
    shown = shown if shown is not None else body_hash(store.get_item(item_id))
    return store.enqueue_page_command(item_id, verb, arg, shown, at)


def run(slack, store, settings, h, now=NOW):
    return process_page(slack, store, settings, {"insight": h, "todo": h, "feedback": h}, now)


def row(store, cid):
    return store.page_command(cid)


# -- the brief's tests ----------------------------------------------------------------------------------------------

def test_page_post_runs_once(slack, store, settings):
    make_item(store)
    first, second = enqueue(store), enqueue(store)  # a double click
    h = Handlers()
    assert run(slack, store, settings, h) == 2
    assert [p[0] for p in h.posted] == ["i1"]
    assert (row(store, first).state, row(store, first).result) == ("done", f"✅ Posted · {LINK}")
    assert row(store, second).state == "done" and row(store, second).result.startswith("Already posted")
    assert store.get_item("i1").status == "posted"
    assert run(slack, store, settings, h) == 0 and len(h.posted) == 1  # nothing queued: nothing runs again


def test_page_then_dm_post_race(slack, store, settings):
    make_item(store)
    # another open item keeps the DM read on (I5): with none, process_dm would not read the DM at all
    make_item(store, id="f1", kind="feedback", dm_ts="1800000050.000001")
    enqueue(store)
    reply_ts = "1800000250.000100"
    parent = slack.add_message(settings.self_dm_id, PARENT, "item", user_id=HASEEB, reply_count=1,
                               latest_reply="1800000250")
    slack.add_thread(settings.self_dm_id, parent, [Msg(ts=reply_ts, user_id=HASEEB, user_name="U", text="post",
                                                       thread_ts=PARENT, reply_count=0, latest_reply=None)])
    h = Handlers()
    handlers = {"insight": h, "feedback": h}
    process_page(slack, store, settings, handlers, NOW)  # the tick runs page commands first...
    process_dm(slack, store, settings, handlers, NOW)    # ...then the DM, in the same tick
    assert len(h.posted) == 1
    assert slack.sent[-1].thread_ts == PARENT and slack.sent[-1].text.startswith("Already posted")


def test_hash_mismatch_refuses(slack, store, settings):
    make_item(store, body="v1")
    cid = enqueue(store)                       # the page showed v1
    store.mark("i1", "delivered", body="v2")   # revised after the page loaded
    h = Handlers()
    assert run(slack, store, settings, h) == 1
    assert (row(store, cid).state, row(store, cid).result) == ("failed", "draft changed, reload")
    assert h.posted == [] and slack.sent == [] and slack.reactions == [] and slack.uploads == []
    assert store.get_item("i1").status == "delivered"


@pytest.mark.parametrize("verb,arg", [("edit", "make it about Pakistan"), ("shorter", None), ("skip", None)])
def test_hash_mismatch_refuses_every_draft_verb(slack, store, settings, verb, arg):
    make_item(store, body="v1")
    cid = enqueue(store, verb, arg=arg)
    store.mark("i1", "delivered", body="v2")
    h = Handlers()
    run(slack, store, settings, h)
    assert row(store, cid).result == "draft changed, reload" and h.revised == [] and slack.sent == []
    assert store.get_item("i1").status == "delivered"


def test_reactions_on_item_dm_ts(slack, store, settings):
    make_item(store)
    enqueue(store)
    run(slack, store, settings, Handlers())
    assert [(r.channel_id, r.ts, r.emoji) for r in slack.reactions] == [
        (DM, PARENT, "eyes"), (DM, PARENT, "white_check_mark")]


def test_cancel_on_a_non_todo_is_refused_without_slack(slack, store, settings):
    make_item(store, id="i2")
    cid = enqueue(store, "cancel", item_id="i2")
    run(slack, store, settings, Handlers())
    assert (row(store, cid).state, row(store, cid).result) == ("failed", "⚠️ cancel works on Next Todo items only")
    assert slack.reactions == [] and slack.sent == []


def test_dm_note_via_send_dm(slack, store, settings):
    make_item(store)
    enqueue(store)
    run(slack, store, settings, Handlers())
    (dm,) = slack.sent
    assert (dm.channel_id, dm.thread_ts) == (HASEEB, PARENT)
    assert dm.text == f"✅ Posted · {LINK}\n{DASHBOARD_NOTE}" and DASHBOARD_NOTE == "_Done from the dashboard_"
    assert store.get_cursor(f"own:{dm.ts}") == "1"  # R23: never read back as a command


def test_page_post_failure_never_retried(slack, store, settings):
    make_item(store)
    cid = enqueue(store)
    h = Handlers(fail_post=SlackTimeout("slack_send_message: tool-call mismatch: no call seen (timeout after 180s)"))
    run(slack, store, settings, h)
    got = row(store, cid)
    assert got.state == "failed" and got.result.startswith(MAY_HAVE_POSTED.split("({")[0])
    assert store.get_item("i1").status == "failed"           # R16: the first failure is final
    assert store.get_cursor("inflight:i1") == ""
    assert [r.emoji for r in slack.reactions] == ["eyes", "warning"]
    assert slack.sent[-1].text.startswith("⚠️ Post may have gone out") and DASHBOARD_NOTE in slack.sent[-1].text
    for minute in range(1, 5):
        run(slack, store, settings, h, NOW + timedelta(minutes=minute))
    assert len(h.posted) == 1                                 # never a second attempt


def test_running_post_row_from_crash_is_never_rerun(slack, store, settings):
    make_item(store)
    old = enqueue(store, at=NOW - timedelta(minutes=5))
    young = enqueue(store, at=NOW - timedelta(seconds=30))
    store.set_page_command(old, "running")   # a tick died mid-post
    store.set_page_command(young, "running")
    store.set_cursor("inflight:i1", (NOW - timedelta(minutes=5)).isoformat())  # _execute's M2 marker
    h = Handlers()
    run(slack, store, settings, h)
    assert h.posted == []
    got = row(store, old)
    assert got.state == "failed" and got.result == MAY_HAVE_POSTED.format(reason="the run was interrupted")
    assert row(store, young).state == "running"               # under 2 minutes: left alone, and not run
    run(slack, store, settings, h, NOW + timedelta(minutes=3))
    assert h.posted == [] and row(store, young).state == "failed"
    assert store.get_item("i1").status == "failed"            # recover_interrupted, as for a DM post


def test_attach_from_page(slack, store, settings):
    # R50: a page post's created time is the deck cutoff, as a DM reply's ts would be
    make_item(store)
    enqueue(store, at=CLICK)
    h = Handlers()
    run(slack, store, settings, h)
    cutoff = f"{CLICK.timestamp():.6f}"
    assert h.posted == [("i1", cutoff)] and store.get_cursor("post_cmd_ts:i1") == cutoff
    item = store.get_item("i1")
    store.set_cursor("deck:i1", json.dumps({"dm_at": f"{CLICK.timestamp() - 5:.6f}"}))
    assert decks._seen_before_post(store, item) is True       # the deck reached the DM before the click
    store.set_cursor("deck:i1", json.dumps({"dm_at": f"{CLICK.timestamp() + 5:.6f}"}))
    assert decks._seen_before_post(store, item) is False      # a later deck stays in the DM

    # `attach` on the posted insight: no hash check (the item is posted), through the handler's attach
    cid = enqueue(store, "attach", shown="not-the-hash")
    run(slack, store, settings, h)
    assert h.attached == ["i1"] and row(store, cid).result == "📎 Deck attached to the post"
    # `attach` on anything but an insight is refused by _execute
    make_item(store, id="f1", kind="feedback", status="posted")
    bad = enqueue(store, "attach", item_id="f1")
    run(slack, store, settings, h)
    assert row(store, bad).state == "failed" and "insights only" in row(store, bad).result


class NoSlack:
    def __getattr__(self, name):
        raise AssertionError(f"Slack touched: {name}")


def test_empty_queue_is_free(store, settings):
    assert process_page(NoSlack(), store, settings, {}, NOW) == 0
    assert store.events_since(0) == []


def test_tick_runs_page_commands_first_and_only_with_work(settings, store):
    calls = []

    def spy(name):
        return lambda now: calls.append(name) or "ok"

    jobs = {n: spy(n) for n in (cli.PAGE_JOB, *cli.JOBS)}
    at = datetime(2026, 10, 19, 11, 0, tzinfo=KW)
    assert cli.PAGE_JOB not in cli.run_tick(settings, store, jobs, clock=lambda: at)
    make_item(store)
    enqueue(store)
    calls.clear()
    ran = cli.run_tick(settings, store, jobs, clock=lambda: at + timedelta(seconds=30))
    assert ran == [cli.PAGE_JOB] and calls == [cli.PAGE_JOB]  # due on every tick with work, ahead of the rest


# -- the Next Todo from the page -------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pinned_wall(monkeypatch):
    """These tests run on fixed clocks; the real wall clock must not leak into failure times."""
    from haseeb_agent import commands
    monkeypatch.setattr(commands, "_wall", lambda: 0.0)


@pytest.fixture
def todo_slack():
    from haseeb_agent.slack_gateway import FileContent
    s = T.TodoSlack()
    s._next = int(T.at(15, 18).timestamp())  # message ts on the test clock, so clicks and sends compare as in life
    s.files[T.FILE] = FileContent(text=T.NOTES_MD, local_path=None, mimetype="application/vnd.slack-docs")
    T.seed_huddle(s, [T.notes_reply()])
    return s


def _armed(todo_slack, store, settings):
    T.tick(todo_slack, store, settings, T.at(15, 18, 0))  # the notes draft, auto-post at 15:20
    item = store.get_item(T.ITEM_ID)
    assert item.status == "delivered" and store.get_cursor(f"autopost:{T.ITEM_ID}")
    return item


def _after_dm(item, s=30):
    """A click time after the item's DM message (the fake's ts run on their own counter)."""
    return datetime.fromtimestamp(float(item.dm_ts) + s, KW)


def _todo_handlers(todo_slack, store, settings):
    h = TodoHandlers(todo_slack, T.FakeLLM(), store, settings)
    return {"todo": h}


@pytest.mark.parametrize("page_first", [True, False])
def test_cancel_blocks_autopost(todo_slack, store, settings, page_first):
    item = _armed(todo_slack, store, settings)
    cid = enqueue(store, "cancel", item_id=T.ITEM_ID, shown="", at=_after_dm(item))
    if page_first:   # the tick: page commands, then the todo job
        process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 19))
    for minute in range(19, 40):  # with page_first False, the auto-post decision itself finds the queued cancel
        T.tick(todo_slack, store, settings, T.at(15, minute))
    assert todo_slack.channel_sends() == []
    assert (row(store, cid).state, row(store, cid).result) == ("done", "Auto-post cancelled")
    dms = [s.text for s in todo_slack.dm_sends()]
    assert dms.count(f"Auto-post cancelled\n{DASHBOARD_NOTE}") == 1 and AUTOPOST_CANCELLED not in dms
    assert store.get_item(T.ITEM_ID).status == "delivered"
    # the item stays postable: "Post now" from the page posts it, once (clicked within M5's 15 minutes of the run)
    enqueue(store, "post", item_id=T.ITEM_ID, at=T.at(15, 40))
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 41))
    (post,) = todo_slack.channel_sends()
    assert post.thread_ts == T.HUDDLE_TS and post.text == T.GOOD
    assert store.get_item(T.ITEM_ID).status == "posted"


def test_cancel_older_than_the_announcing_draft_is_ignored_by_the_autopost(todo_slack, store, settings):
    """N1 for page rows: only a cancel created after the DM message that announced the auto-post counts."""
    item = _armed(todo_slack, store, settings)
    store.enqueue_page_command(T.ITEM_ID, "cancel", None, "", _after_dm(item, -30))
    T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    assert len(todo_slack.channel_sends()) == 1


def test_post_now_vs_autopost_race(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    cid = enqueue(store, "post", item_id=T.ITEM_ID, at=_after_dm(item))
    T.tick(todo_slack, store, settings, T.at(15, 20, 0))  # the auto-post fired first
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 20, 30))
    assert len(todo_slack.channel_sends()) == 1
    assert row(store, cid).state == "done" and row(store, cid).result.startswith("Already posted")


def test_cancel_after_the_autopost_answers_already_posted(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    cid = enqueue(store, "cancel", item_id=T.ITEM_ID, shown="", at=_after_dm(item))
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 21))
    assert row(store, cid).state == "failed" and row(store, cid).result.startswith("Already posted")
    assert len(todo_slack.channel_sends()) == 1


def test_page_post_now_checks_the_format_only_like_a_dm_post(todo_slack, store, settings):
    """D7: a click is attended, like a DM `post`. The auto-post guard (R25/R27/R28) is not applied; the format is."""
    item = _armed(todo_slack, store, settings)
    body = "Next Todo:\n1. Try Claude.ai for the deck.\n2. Ship it.\n3. Post daily."  # fails the R27 guard
    store.mark(T.ITEM_ID, "delivered", body=body)
    T.todo_mod.add_version(store, T.ITEM_ID, f"{float(item.dm_ts) + 10:.6f}", body)  # N2: the version shown
    cid = enqueue(store, "post", item_id=T.ITEM_ID, at=_after_dm(item))
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 19))
    assert row(store, cid).state == "done"
    (post,) = todo_slack.channel_sends()
    assert post.text == body

    bad = make_item(store, id="todo-x", kind="todo", body="oops")
    store.mark(bad.id, "delivered", source_ts=T.HUDDLE_TS)
    cid = enqueue(store, "post", item_id=bad.id)
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 19))
    assert row(store, cid).state == "failed" and len(todo_slack.channel_sends()) == 1  # the format check refuses


class Killed(BaseException):
    """The tick watchdog's kind of exit (cli.TickTimeout): nothing in the job catches it."""


def test_a_tick_killed_mid_post_never_posts_again(slack, store, settings):
    make_item(store)
    cid = enqueue(store)
    seen = []

    class Dies(Handlers):
        def post(self, item, reply_ts=None):
            seen.append(store.page_command(cid).state)  # claimed before the channel write
            super().post(item, reply_ts)
            raise Killed()

    h = Dies()
    with pytest.raises(Killed):
        run(slack, store, settings, h)
    assert seen == ["running"]
    for minute in range(1, 5):
        run(slack, store, settings, h, NOW + timedelta(minutes=minute))
    assert len(h.posted) == 1 and row(store, cid).state == "failed"
    assert row(store, cid).result.startswith("⚠️ Post may have gone out")


# -- review fixes (Task 5 review) ------------------------------------------------------------------------------------

from haseeb_agent import page_commands as pc  # noqa: E402
from haseeb_agent.jobs.todo import add_version, versions  # noqa: E402

EARLIER = "⚠️ An earlier post may have gone out — check the channel before posting again"


def test_c1_double_click_then_timeout_makes_one_write(slack, store, settings):
    make_item(store)
    a, b = enqueue(store), enqueue(store)
    h = Handlers(fail_post=SlackTimeout("slack_send_message: tool-call mismatch: no call seen (timeout after 180s)"))
    run(slack, store, settings, h)
    assert len(h.posted) == 1
    assert row(store, a).result.startswith("⚠️ Post may have gone out")
    assert (row(store, b).state, row(store, b).result) == ("failed", EARLIER)


def test_c1_crash_after_the_write_then_a_tick_makes_one_write(slack, store, settings):
    make_item(store)
    enqueue(store), enqueue(store)

    class Dies(Handlers):
        def post(self, item, reply_ts=None):
            super().post(item, reply_ts)
            raise Killed()

    h = Dies()
    with pytest.raises(Killed):
        run(slack, store, settings, h)
    h2 = Handlers()
    run(slack, store, settings, h2, NOW + timedelta(minutes=1))
    assert len(h.posted) + len(h2.posted) == 1


def test_c1_crash_with_a_young_running_row_still_blocks_the_queued_one(slack, store, settings):
    """The `inflight:` marker alone (recover_interrupted) records the failure, before the 2-minute row recovery."""
    make_item(store)
    enqueue(store, at=NOW - timedelta(seconds=20)), enqueue(store, at=NOW - timedelta(seconds=10))

    class Dies(Handlers):
        def post(self, item, reply_ts=None):
            super().post(item, reply_ts)
            raise Killed()

    with pytest.raises(Killed):
        run(slack, store, settings, Dies())
    h2 = Handlers()
    run(slack, store, settings, h2, NOW + timedelta(seconds=30))
    assert h2.posted == []


def test_c1_a_click_after_the_failure_still_posts(slack, store, settings):
    make_item(store)
    enqueue(store)
    run(slack, store, settings, Handlers(fail_post=SlackTimeout("timeout after 180s")))
    assert store.get_item("i1").status == "failed"
    cid = enqueue(store, at=NOW + timedelta(minutes=1))  # an informed override
    h = Handlers()
    run(slack, store, settings, h, NOW + timedelta(minutes=2))
    assert len(h.posted) == 1 and row(store, cid).result == f"✅ Posted · {LINK}"


def test_c1_a_refusal_before_the_send_does_not_block_later_rows(slack, store, settings):
    make_item(store)
    a, b = enqueue(store), enqueue(store)
    h = Handlers(fail_post=LookupError("the huddle thread isn't known yet"))
    run(slack, store, settings, h)
    assert store.get_cursor("post_failed_at:i1") is None  # nothing can have gone out
    assert row(store, b).result != EARLIER


def test_i1_cancel_on_a_replaced_draft_leaves_the_autopost_armed(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    shown = versions(store, T.ITEM_ID)[-1]["ts"]
    add_version(store, T.ITEM_ID, f"{float(shown) + 60:.6f}", T.GOOD)  # a newer draft replaced the one he saw
    cid = enqueue(store, "cancel", item_id=T.ITEM_ID, shown="", at=_after_dm(item, 30))
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 19))
    assert (row(store, cid).state, row(store, cid).result) == (
        "done", "No auto-post pending (a newer draft replaced the one you saw)")
    assert store.get_cursor(f"autopost:{T.ITEM_ID}")  # still armed


def test_second_cancel_answers_already_cancelled(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    a = enqueue(store, "cancel", item_id=T.ITEM_ID, shown="", at=_after_dm(item, 30))
    b = enqueue(store, "cancel", item_id=T.ITEM_ID, shown="", at=_after_dm(item, 31))
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 19))
    assert row(store, a).result == "Auto-post cancelled" and row(store, b).result == "Already cancelled"


def test_i2_todo_hash_checks_the_version_that_would_post(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    v1 = versions(store, T.ITEM_ID)[-1]
    v2 = "Next Todo:\n1. A newer line.\n2. Ship it.\n3. Post daily."
    add_version(store, T.ITEM_ID, f"{float(v1['ts']) + 60:.6f}", v2)
    store.mark(T.ITEM_ID, "delivered", body=v2)
    # the page showed v2, but the click is older than v2's DM message: TodoHandlers.post would post v1 (N2)
    cid = enqueue(store, "post", item_id=T.ITEM_ID, at=_after_dm(item, 30))
    sent = len(todo_slack.sent)
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 19))
    assert (row(store, cid).state, row(store, cid).result) == ("failed", "draft changed, reload")
    assert len(todo_slack.sent) == sent


def test_m1_only_a_cancel_that_cancelled_counts_once_done(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    cid = enqueue(store, "cancel", item_id=T.ITEM_ID, shown="", at=_after_dm(item, 30))
    store.set_page_command(cid, "done", "No auto-post pending")
    T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    assert len(todo_slack.channel_sends()) == 1


def test_m2_claim_is_compare_and_set(slack, store, settings):
    make_item(store)
    cid = enqueue(store)
    assert store.claim_page_command(cid) is True
    assert store.claim_page_command(cid) is False
    store.set_page_command(cid, "queued")
    stale = store.next_page_commands()            # a snapshot that is out of date by the time it runs
    store.set_page_command(cid, "done", "x")
    make_item(store, id="i2", dm_ts="1800000110.000001")
    enqueue(store, item_id="i2")                  # keeps the queue non-empty, so the job runs
    store.next_page_commands = lambda limit=10: stale
    h = Handlers()
    run(slack, store, settings, h)
    assert h.posted == [] and row(store, cid).result == "x"


def test_m3_huddle_budget_caps_rows_and_time(slack, store, settings):
    for i in range(5):
        make_item(store, id=f"i{i}", dm_ts=f"18000001{i:02d}.000001")
        enqueue(store, item_id=f"i{i}")
    h = Handlers()
    assert process_page(slack, store, settings, {"insight": h}, NOW, limit=3) == 3
    assert len(h.posted) == 3
    t = [0.0]

    def clock():
        t[0] += 70.0
        return t[0]
    # two rows left: the first always starts; the second would start 70 s in, past the 60 s budget
    assert process_page(slack, store, settings, {"insight": h}, NOW, budget_s=60, clock=clock) == 1
    assert len(h.posted) == 4 and len(store.next_page_commands()) == 1  # the last one waits for the next tick


def test_m3_tick_passes_the_huddle_budget(settings, store, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "process_page", lambda *a, **kw: seen.update(kw) or 0)
    rt = cli.Runtime(settings=settings, store=store, slack=FakeSlack(), llm=None, handlers={})
    page = cli.default_jobs(rt)[cli.PAGE_JOB]
    page(datetime(2026, 10, 19, 14, 40, tzinfo=KW))  # Monday, in the huddle, today's todo not final
    assert seen == {"limit": 3, "budget_s": 60}
    seen.clear()
    page(datetime(2026, 10, 19, 11, 0, tzinfo=KW))
    assert seen == {}


def test_m4_record_is_retried_once(slack, store, settings, monkeypatch):
    make_item(store)
    cid = enqueue(store)
    real, fails = store.set_page_command, [1]

    def flaky(id, state, result=None):
        if state == "done" and fails[0]:
            fails[0] -= 1
            raise RuntimeError("database is locked")
        return real(id, state, result)

    monkeypatch.setattr(store, "set_page_command", flaky)
    run(slack, store, settings, Handlers())
    assert row(store, cid).state == "done"


def test_m4_an_unrecorded_post_says_posted_not_may_have(slack, store, settings, monkeypatch):
    make_item(store)
    cid = enqueue(store)
    real = store.set_page_command

    def broken(id, state, result=None):
        if state == "done":
            raise RuntimeError("database is locked")
        return real(id, state, result)

    monkeypatch.setattr(store, "set_page_command", broken)
    h = Handlers()
    run(slack, store, settings, h)
    monkeypatch.setattr(store, "set_page_command", real)
    assert row(store, cid).state == "running"
    run(slack, store, settings, h, NOW + timedelta(minutes=5))
    assert row(store, cid).result == pc.POSTED_UNRECORDED
    assert len(h.posted) == 1


# -- re-review: CB1, CB2 and the send-started marker -----------------------------------------------------------------

def _autopost_send(todo_slack, settings, exc, *, lands=True, during=None):
    """The first channel send (the auto-post at 15:20) lands (or not), runs `during` (a click mid-hang), then
    raises `exc`. Its ts is on the test clock, after the attempt began, as in life."""
    real, state = todo_slack.send, {"n": 0}

    def send(channel_id, text, thread_ts=None):
        if channel_id == settings.channel_id and state["n"] == 0:
            state["n"] += 1
            todo_slack._next = max(todo_slack._next, int(T.at(15, 20, 5).timestamp()))
            if lands:
                real(channel_id, text, thread_ts)
            if during:
                during()
            raise exc
        return real(channel_id, text, thread_ts)
    todo_slack.send = send


def test_cb1_post_now_clicked_before_a_timed_out_autopost(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    cid = enqueue(store, "post", item_id=T.ITEM_ID, at=_after_dm(item))  # clicked just before the auto-post
    _autopost_send(todo_slack, settings, SlackTimeout("timeout after 180s"))
    T.tick(todo_slack, store, settings, T.at(15, 20, 0))  # the auto-post: landed, but timed out
    assert store.get_item(T.ITEM_ID).status == "failed" and store.get_cursor(f"post_failed_at:{T.ITEM_ID}")
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 21))
    assert len(todo_slack.channel_sends()) == 1
    assert row(store, cid).state == "failed"


def test_cb1_post_now_after_a_killed_autopost(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    cid = enqueue(store, "post", item_id=T.ITEM_ID, at=_after_dm(item))
    _autopost_send(todo_slack, settings, Killed())
    with pytest.raises(Killed):
        T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    assert store.get_item(T.ITEM_ID).status == "delivered"  # nothing recorded the outcome
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 21))
    assert len(todo_slack.channel_sends()) == 1
    got = row(store, cid)
    assert got.state == "failed" and got.result == f"⚠️ {T.todo_mod.AUTOPOST_MAYBE}"
    assert store.get_cursor(f"post_failed_at:{T.ITEM_ID}") is None  # a pre-send refusal records no failure


def test_cb1_a_click_after_the_recorded_failure_still_posts(todo_slack, store, settings):
    _armed(todo_slack, store, settings)
    _autopost_send(todo_slack, settings, SlackTimeout("timeout after 180s"))
    T.tick(todo_slack, store, settings, T.at(15, 20, 0))  # the timeout is recorded at once: failed, told
    assert store.get_item(T.ITEM_ID).status == "failed"
    cid = enqueue(store, "post", item_id=T.ITEM_ID, at=T.at(15, 21))  # informed: after the outcome
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 22))
    assert row(store, cid).state == "done" and len(todo_slack.channel_sends()) == 2
    assert store.get_item(T.ITEM_ID).status == "posted"


# -- re-review 2: CB3 (ruling D8), nothing posts while an attempt's outcome is unknown ------------------------------

def test_cb3_post_now_clicked_during_a_killed_autopost_send(todo_slack, store, settings):
    _armed(todo_slack, store, settings)
    clicked = {}
    _autopost_send(todo_slack, settings, Killed(), during=lambda: clicked.setdefault(
        "cid", enqueue(store, "post", item_id=T.ITEM_ID, at=T.at(15, 20, 20))))  # clicked mid-hang
    with pytest.raises(Killed):
        T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 21))
    assert len(todo_slack.channel_sends()) == 1
    assert row(store, clicked["cid"]).result == f"⚠️ {T.todo_mod.AUTOPOST_MAYBE}"
    assert store.get_item(T.ITEM_ID).status == "delivered"  # the refusal leaves the outcome to the todo job
    T.tick(todo_slack, store, settings, T.at(15, 21, 30))  # the todo job finds its own post in the thread
    assert store.get_item(T.ITEM_ID).status == "posted"
    enqueue(store, "post", item_id=T.ITEM_ID, at=T.at(15, 22))
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 22, 30))
    assert len(todo_slack.channel_sends()) == 1


def test_cb3_dm_post_given_during_a_timed_out_autopost(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    clock = [T.at(15, 20, 0)]

    def hang():
        clock[0] = T.at(15, 23, 0)  # the send hangs for its whole budget

    _autopost_send(todo_slack, settings, SlackTimeout("timeout after 180s"), during=hang)
    run_todo(todo_slack, T.FakeLLM(), store, settings, T.at(15, 20, 0), clock=lambda: clock[0])
    assert store.get_item(T.ITEM_ID).status == "failed"
    parent = next(m for m in todo_slack.pages[HASEEB] if m.ts == item.dm_ts)
    cmd = T.msg(T.ts_at(15, 20, 20), "post", HASEEB, item.dm_ts)  # sent while the auto-post hung
    parent.reply_count, parent.latest_reply = 1, cmd.ts.split(".")[0]
    todo_slack.add_thread(HASEEB, parent, [cmd])
    h = TodoHandlers(todo_slack, T.FakeLLM(), store, settings)
    process_dm(todo_slack, store, settings, {"todo": h}, T.at(15, 24))
    assert len(todo_slack.channel_sends()) == 1
    # refused by the shared rule in `_execute` (R1): the reply is older than the recorded failure
    assert todo_slack.dm_sends()[-1].text == f"⚠️ {EARLIER_POST_TEXT}"


def test_cb3_after_the_autopost_failed_notice_a_new_click_posts(todo_slack, store, settings):
    _armed(todo_slack, store, settings)
    _autopost_send(todo_slack, settings, Killed(), lands=False)
    with pytest.raises(Killed):
        T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    early = enqueue(store, "post", item_id=T.ITEM_ID, at=T.at(15, 20, 30))  # outcome still unknown
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 21))
    assert row(store, early).state == "failed" and todo_slack.channel_sends() == []
    T.tick(todo_slack, store, settings, T.at(15, 21, 0))  # the next todo run records the outcome and says so
    assert store.get_item(T.ITEM_ID).status == "failed"
    assert T.todo_mod.AUTOPOST_FAILED in [s.text for s in todo_slack.dm_sends()]
    cid = enqueue(store, "post", item_id=T.ITEM_ID, at=T.at(15, 22))  # informed, after the notice
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 22, 30))
    assert row(store, cid).state == "done" and len(todo_slack.channel_sends()) == 1


def test_cb1_the_dm_path_is_refused_the_same_way(todo_slack, store, settings):
    item = _armed(todo_slack, store, settings)
    _autopost_send(todo_slack, settings, Killed())
    with pytest.raises(Killed):
        T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    h = TodoHandlers(todo_slack, T.FakeLLM(), store, settings)
    with pytest.raises(ValueError, match="auto-post may have gone out"):
        h.post(store.get_item(T.ITEM_ID), reply_ts=f"{T.at(15, 19, 50).timestamp():.6f}")
    # through process_dm: a `post` reply from before the attempt is answered, not posted
    parent = next(m for m in todo_slack.pages[HASEEB] if m.ts == item.dm_ts)
    cmd = T.msg(T.ts_at(15, 19, 50), "post", HASEEB, item.dm_ts)
    parent.reply_count, parent.latest_reply = 1, cmd.ts.split(".")[0]
    todo_slack.add_thread(HASEEB, parent, [cmd])
    process_dm(todo_slack, store, settings, {"todo": h}, T.at(15, 21))
    assert len(todo_slack.channel_sends()) == 1
    assert todo_slack.dm_sends()[-1].text == f"⚠️ {T.todo_mod.AUTOPOST_MAYBE}"


def test_cb1_close_overdue_records_the_failure_time(todo_slack, store, settings):
    _armed(todo_slack, store, settings)
    store.set_cursor(f"autopost_try:{T.ITEM_ID}", T.at(15, 20).isoformat())  # a crash mid-send
    T.tick(todo_slack, store, settings, T.at(16, 30))  # past the window: stage 0 closes it
    assert store.get_item(T.ITEM_ID).status == "failed"
    assert store.get_cursor(f"post_failed_at:{T.ITEM_ID}") == f"{T.at(16, 30).timestamp():.6f}"


def test_cb2_an_unrecorded_failure_time_keeps_inflight(slack, store, settings, monkeypatch):
    make_item(store)
    a, b = enqueue(store), enqueue(store)
    real = store.set_cursor

    def broken(key, value):
        if key.startswith("post_failed_at:"):
            raise RuntimeError("database is locked")
        return real(key, value)

    monkeypatch.setattr(store, "set_cursor", broken)
    h = Handlers(fail_post=SlackTimeout("timeout after 180s"))
    run(slack, store, settings, h)
    assert store.get_cursor("inflight:i1")                # left for recover_interrupted
    assert row(store, b).result == EARLIER                # and the second click is refused meanwhile
    assert len(h.posted) == 1
    monkeypatch.setattr(store, "set_cursor", real)
    run(slack, store, settings, h, NOW + timedelta(minutes=1))   # nothing queued: page_due is False...
    from haseeb_agent.commands import recover_interrupted
    recover_interrupted(slack, store, settings, NOW + timedelta(minutes=1))
    assert store.get_cursor("post_failed_at:i1") and store.get_cursor("inflight:i1") == ""


def test_send_started_marker_records_any_failure_after_the_send(slack, store, settings):
    """Robustness: a handler that set `send_started:` before its send has its failure recorded as may-have-posted,
    even when the exception is a ValueError (which, without the marker, counts as a pre-send refusal)."""
    from haseeb_agent.commands import mark_send_started
    make_item(store)
    a, b = enqueue(store), enqueue(store)

    class AfterSend(Handlers):
        def post(self, item, reply_ts=None):
            mark_send_started(store, item.id)
            self.posted.append((item.id, reply_ts))
            raise ValueError("something odd after the send")

    h = AfterSend()
    run(slack, store, settings, h)
    assert len(h.posted) == 1 and row(store, b).result == EARLIER
    assert store.get_cursor("send_started:i1") == ""


@pytest.mark.parametrize("kind", ["todo", "insight", "feedback"])
def test_real_handlers_mark_and_clear_send_started(kind, todo_slack, store, settings, monkeypatch):
    from haseeb_agent.jobs.feedback import FeedbackHandlers
    from haseeb_agent.jobs.insights import InsightHandlers
    seen = []
    real = todo_slack.send

    def send(channel_id, text, thread_ts=None):
        if channel_id == settings.channel_id:
            seen.append(store.get_cursor(f"send_started:{iid}"))
        return real(channel_id, text, thread_ts)

    todo_slack.send = send
    if kind == "todo":
        item = _armed(todo_slack, store, settings)
        iid = item.id
        TodoHandlers(todo_slack, T.FakeLLM(), store, settings).post(item, reply_ts=None)
    elif kind == "feedback":
        iid = "fb1"
        make_item(store, id=iid, kind="feedback")
        store.mark(iid, "delivered", source_ts=T.HUDDLE_TS)
        FeedbackHandlers(todo_slack, T.FakeLLM(), store, settings).post(store.get_item(iid))
    else:
        iid = "in1"
        make_item(store, id=iid, kind="insight")
        from haseeb_agent.jobs import insights as ins
        monkeypatch.setattr(ins, "_load", lambda st, it: {"v": 1, "insight": {}})  # a v1 body: the reader dispatches on v
        monkeypatch.setattr(ins, "_insight_from", lambda d: None)
        monkeypatch.setattr(ins, "render_insight_post", lambda x: ["part 1", "part 2"])
        InsightHandlers(todo_slack, T.FakeLLM(), store, settings).post(store.get_item(iid))
    assert seen and all(v for v in seen)                   # set during every channel send
    assert store.get_cursor(f"send_started:{iid}") == ""   # cleared with the outcome


# -- re-review 3: F1, F2 (generalised D8) -----------------------------------------------------------------------------

def _killed_autopost(todo_slack, store, settings, lands):
    _armed(todo_slack, store, settings)
    _autopost_send(todo_slack, settings, Killed(), lands=lands)
    with pytest.raises(Killed):
        T.tick(todo_slack, store, settings, T.at(15, 20, 0))
    assert T.todo_mod.unsettled(store, store.get_item(T.ITEM_ID))


def _page(todo_slack, store, settings, verb, at, arg=None):
    cid = enqueue(store, verb, item_id=T.ITEM_ID, arg=arg, at=at)
    process_page(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), at + timedelta(seconds=10))
    return row(store, cid)


@pytest.mark.parametrize("lands", [True, False])
def test_f1_skip_while_unsettled_then_post_makes_one_write(todo_slack, store, settings, lands):
    _killed_autopost(todo_slack, store, settings, lands)
    got = _page(todo_slack, store, settings, "skip", T.at(15, 20, 30))
    assert got.state == "failed" and got.result == f"⚠️ {T.todo_mod.AUTOPOST_PENDING}"
    assert store.get_item(T.ITEM_ID).status == "delivered"  # not skipped: the todo job can still settle it
    T.tick(todo_slack, store, settings, T.at(15, 21, 0))     # settles: posted (landed) or failed + notice
    _page(todo_slack, store, settings, "post", T.at(15, 22, 0))
    assert len(todo_slack.channel_sends()) == 1


@pytest.mark.parametrize("lands", [True, False])
@pytest.mark.parametrize("verb", ["shorter", "skip"])
def test_f2_revise_or_skip_never_leaves_it_stuck(todo_slack, store, settings, verb, lands):
    """The reviewer's repro, with assertions: the attempt is settled, with AUTOPOST_FAILED when it did not land,
    and a click after the notice posts."""
    _killed_autopost(todo_slack, store, settings, lands)
    assert _page(todo_slack, store, settings, verb, T.at(15, 20, 30)).state == "failed"
    for m in range(21, 60):
        T.tick(todo_slack, store, settings, T.at(15, m, 0))
    for h in (16, 17):
        T.tick(todo_slack, store, settings, T.at(h, 30, 0))
    it = store.get_item(T.ITEM_ID)
    dms = [s.text for s in todo_slack.dm_sends()]
    assert not T.todo_mod.unsettled(store, it)
    if lands:
        assert it.status == "posted" and T.todo_mod.AUTOPOST_FAILED not in dms
    else:
        assert it.status == "failed" and dms.count(T.todo_mod.AUTOPOST_FAILED) == 1
    got = _page(todo_slack, store, settings, "post", T.at(17, 31))
    assert len(todo_slack.channel_sends()) == 1
    assert got.result.startswith("Already posted" if lands else "✅ Posted")


def test_f2_settled_outside_the_window_and_after_an_edit(todo_slack, store, settings):
    """Settlement depends on neither `autopost:` nor `edited`: an item revised before the fix (or by any path)
    with an attempt on record is settled by the next todo run, even past 16:05."""
    _killed_autopost(todo_slack, store, settings, lands=False)
    store.set_cursor(f"autopost:{T.ITEM_ID}", "")
    store.mark(T.ITEM_ID, "delivered", edited=True)
    T.tick(todo_slack, store, settings, T.at(16, 30))
    assert store.get_item(T.ITEM_ID).status == "failed"
    assert T.todo_mod.AUTOPOST_FAILED in [s.text for s in todo_slack.dm_sends()]


@pytest.mark.parametrize("verb,arg", [("skip", None), ("edit", "make 2 shorter"), ("shorter", None)])
def test_f2_draft_verbs_refused_while_unsettled_and_allowed_after(todo_slack, store, settings, verb, arg):
    _killed_autopost(todo_slack, store, settings, lands=False)
    got = _page(todo_slack, store, settings, verb, T.at(15, 20, 30), arg)
    assert got.state == "failed" and got.result == f"⚠️ {T.todo_mod.AUTOPOST_PENDING}"
    assert not store.get_item(T.ITEM_ID).edited and todo_slack.channel_sends() == []
    T.tick(todo_slack, store, settings, T.at(15, 21, 0))  # settled: failed, told
    assert store.get_item(T.ITEM_ID).status == "failed"
    got = _page(todo_slack, store, settings, verb, T.at(15, 22, 0), arg)
    assert got.state == "done", got.result


def test_f2_dm_commands_refused_while_unsettled(todo_slack, store, settings):
    _killed_autopost(todo_slack, store, settings, lands=False)
    item = store.get_item(T.ITEM_ID)
    parent = next(m for m in todo_slack.pages[HASEEB] if m.ts == item.dm_ts)
    cmd = T.msg(T.ts_at(15, 20, 30), "skip", HASEEB, item.dm_ts)
    parent.reply_count, parent.latest_reply = 1, cmd.ts.split(".")[0]
    todo_slack.add_thread(HASEEB, parent, [cmd])
    process_dm(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 20, 40))
    assert store.get_item(T.ITEM_ID).status == "delivered"
    assert todo_slack.dm_sends()[-1].text == f"⚠️ {T.todo_mod.AUTOPOST_PENDING}"


def test_f2_cancel_while_unsettled_still_answers(todo_slack, store, settings):
    _killed_autopost(todo_slack, store, settings, lands=False)
    got = _page(todo_slack, store, settings, "cancel", T.at(15, 20, 30))
    assert got.result == "⚠️ An auto-post attempt already started — check the huddle thread"  # not Unsettled
    T.tick(todo_slack, store, settings, T.at(15, 21, 0))
    assert store.get_item(T.ITEM_ID).status == "failed"  # settlement is unaffected


def test_a_failed_item_from_before_d8_is_settled_silently(todo_slack, store, settings):
    """An attempt recorded and failed by the older code (no failure time) was told when it failed: no second notice."""
    _killed_autopost(todo_slack, store, settings, lands=False)
    store.mark(T.ITEM_ID, "failed")
    sent = len(todo_slack.dm_sends())
    T.tick(todo_slack, store, settings, T.at(15, 21, 0))
    assert not T.todo_mod.unsettled(store, store.get_item(T.ITEM_ID)) and len(todo_slack.dm_sends()) == sent


@pytest.mark.parametrize("status", ["delivered", "skipped", "failed"])
def test_handler_post_refuses_while_unsettled_whatever_the_status(todo_slack, store, settings, status):
    """`TodoHandlers.post` itself (any caller, not only `_execute`'s guard) refuses while unsettled."""
    from haseeb_agent.commands import Unsettled
    _killed_autopost(todo_slack, store, settings, lands=False)
    store.mark(T.ITEM_ID, status)
    h = TodoHandlers(todo_slack, T.FakeLLM(), store, settings)
    with pytest.raises(Unsettled):
        h.post(store.get_item(T.ITEM_ID), reply_ts=f"{T.at(15, 30).timestamp():.6f}")  # after the attempt
    assert todo_slack.channel_sends() == []


# -- re-review 4: R1 (one earlier-post rule for the DM and the page), R2 (bounded settle retries) -------------------

def _two_dm_posts(slack, settings, kind_ts):
    parent = slack.add_message(settings.self_dm_id, kind_ts, "item", user_id=HASEEB, reply_count=2,
                               latest_reply=str(int(float(kind_ts)) + 160))
    replies = [Msg(ts=f"{int(float(kind_ts)) + d}.000100", user_id=HASEEB, user_name="U", text="post",
                   thread_ts=kind_ts, reply_count=0, latest_reply=None) for d in (150, 160)]
    slack.add_thread(settings.self_dm_id, parent, replies)


@pytest.mark.parametrize("kind", ["insight", "feedback"])
def test_r1_two_dm_post_replies_then_a_timeout_make_one_write(slack, store, settings, kind):
    """The second `post` reply was sent during the first one's hang: refused, never an override."""
    make_item(store, kind=kind)
    make_item(store, id="other", kind="feedback" if kind == "insight" else "insight", dm_ts="1800000050.000001")
    _two_dm_posts(slack, settings, PARENT)
    h = Handlers(fail_post=SlackTimeout("timeout after 180s"))
    process_dm(slack, store, settings, {"insight": h, "feedback": h}, NOW)
    assert len(h.posted) == 1
    assert slack.sent[-1].text == f"⚠️ {EARLIER_POST_TEXT}"
    assert store.get_item("i1").status == "failed"


def test_r1_reviewer_repro(slack, store, settings):
    make_item(store)
    make_item(store, id="f1", kind="feedback", dm_ts="1800000050.000001")
    parent = slack.add_message(settings.self_dm_id, PARENT, "item", user_id=HASEEB, reply_count=2,
                               latest_reply="1800000260")
    slack.add_thread(settings.self_dm_id, parent, [
        Msg(ts="1800000250.000100", user_id=HASEEB, user_name="U", text="post", thread_ts=PARENT, reply_count=0,
            latest_reply=None),
        Msg(ts="1800000260.000100", user_id=HASEEB, user_name="U", text="post", thread_ts=PARENT, reply_count=0,
            latest_reply=None)])
    h = Handlers(fail_post=SlackTimeout("timeout after 180s"))
    process_dm(slack, store, settings, {"insight": h, "feedback": h}, NOW)
    assert len(h.posted) == 1


def test_r1_an_informed_dm_post_after_the_failure_posts(slack, store, settings):
    make_item(store)
    make_item(store, id="f1", kind="feedback", dm_ts="1800000050.000001")
    _two_dm_posts(slack, settings, PARENT)
    process_dm(slack, store, settings, {"insight": Handlers(fail_post=SlackTimeout("timeout")),
                                        "feedback": Handlers()}, NOW)
    assert slack.sent[-2].text.startswith("⚠️ Post may have gone out")  # the failure notice
    parent, replies = slack.threads[(settings.self_dm_id, PARENT)]
    later = Msg(ts=f"{NOW.timestamp() + 60:.6f}", user_id=HASEEB, user_name="U", text="post", thread_ts=PARENT,
                reply_count=0, latest_reply=None)
    slack.threads[(settings.self_dm_id, PARENT)] = (parent, [*replies, later])
    page_parent = next(m for m in slack.pages[settings.self_dm_id] if m.ts == PARENT)
    page_parent.reply_count, page_parent.latest_reply = 3, str(int(float(later.ts)))
    h = Handlers()
    process_dm(slack, store, settings, {"insight": h, "feedback": h}, NOW + timedelta(minutes=2))
    assert len(h.posted) == 1 and store.get_item("i1").status == "posted"


def test_r1_the_rule_holds_for_a_manual_todo_post_too(todo_slack, store, settings):
    _armed(todo_slack, store, settings)
    store.set_cursor(f"post_failed_at:{T.ITEM_ID}", f"{T.at(15, 19).timestamp():.6f}")  # a DM post timed out
    store.mark(T.ITEM_ID, "failed")
    got = _page(todo_slack, store, settings, "post", T.at(15, 18, 50))
    assert got.result == f"⚠️ {EARLIER_POST_TEXT}" and todo_slack.channel_sends() == []
    from haseeb_agent.commands import _Runner
    runner = _Runner(todo_slack, store, settings, _todo_handlers(todo_slack, store, settings), T.at(15, 20))
    from haseeb_agent.commands import Command, Unsettled
    with pytest.raises(Unsettled):
        runner._execute(store.get_item(T.ITEM_ID), Command("post"), f"{T.at(15, 18, 55).timestamp():.6f}")
    assert runner._execute(store.get_item(T.ITEM_ID), Command("post"),
                           f"{T.at(15, 19, 30).timestamp():.6f}").startswith("✅ Posted")


class UnreadableHuddle(T.TodoSlack):
    def read_thread(self, channel_id, ts):
        if channel_id == T.CH:
            raise SlackError("thread unreadable")
        return super().read_thread(channel_id, ts)


@pytest.mark.parametrize("when", ["three_runs", "window_closed"])
def test_r2_an_unreadable_huddle_thread_is_settled_in_the_end(todo_slack, store, settings, when):
    _killed_autopost(todo_slack, store, settings, lands=False)
    todo_slack.__class__ = UnreadableHuddle
    if when == "three_runs":
        T.tick(todo_slack, store, settings, T.at(15, 21))
        T.tick(todo_slack, store, settings, T.at(15, 22))
        assert T.todo_mod.unsettled(store, store.get_item(T.ITEM_ID))   # still waiting after two
        T.tick(todo_slack, store, settings, T.at(15, 23))
    else:
        store.set_cursor(f"autopost:{T.ITEM_ID}", "")  # (close_overdue would settle an armed one by itself)
        T.tick(todo_slack, store, settings, T.at(16, 6))                 # the window has closed: at once
    it = store.get_item(T.ITEM_ID)
    assert it.status == "failed" and not T.todo_mod.unsettled(store, it)
    dms = [s.text for s in todo_slack.dm_sends()]
    assert dms.count(T.todo_mod.AUTOPOST_UNCHECKED) == 1
    assert "may have gone out — check the huddle thread" in T.todo_mod.AUTOPOST_UNCHECKED


def test_r1_a_post_while_inflight_holds_a_time_is_refused_on_any_path(slack, store, settings):
    """CB2's state (a failed post whose failure time could not be recorded) blocks the DM path too."""
    from haseeb_agent.commands import Command, Unsettled, _Runner
    make_item(store)
    store.set_cursor("inflight:i1", NOW.isoformat())
    h = Handlers()
    runner = _Runner(slack, store, settings, {"insight": h}, NOW)
    with pytest.raises(Unsettled):
        runner._execute(store.get_item("i1"), Command("post"), f"{NOW.timestamp() + 60:.6f}")
    store.set_cursor("inflight:i1", "notify")  # recovered: only the notice is pending
    assert runner._execute(store.get_item("i1"), Command("post"), f"{NOW.timestamp() + 60:.6f}").startswith("✅")
    assert len(h.posted) == 1


# -- the final review: M1 (an Unsettled refusal keeps an earlier inflight:), M2 (pre-D8 attempts settle quietly),
# M3 (the settle's failure time is taken after its thread read), M5 (a queued click expires after 15 minutes) ------

def _break_failure_time(store, monkeypatch):
    real = store.set_cursor

    def broken(key, value):
        if key.startswith("post_failed_at:"):
            raise RuntimeError("database is locked")
        return real(key, value)

    monkeypatch.setattr(store, "set_cursor", broken)
    return real


@pytest.mark.parametrize("second_reply", [True, False])
def test_m1_an_unsettled_refusal_never_clears_an_earlier_inflight(slack, store, settings, monkeypatch, second_reply):
    """CB2's state (the first post timed out and its failure time could not be recorded, so `inflight:` stays), then
    a second `post` reply in the same run: it is refused, and must leave that marker for recover_interrupted."""
    make_item(store)
    make_item(store, id="f1", kind="feedback", dm_ts="1800000050.000001")
    if second_reply:
        _two_dm_posts(slack, settings, PARENT)
    else:
        parent = slack.add_message(settings.self_dm_id, PARENT, "item", user_id=HASEEB, reply_count=1,
                                   latest_reply=str(int(float(PARENT)) + 150))
        slack.add_thread(settings.self_dm_id, parent, [Msg(ts=f"{int(float(PARENT)) + 150}.000100", user_id=HASEEB,
                                                           user_name="U", text="post", thread_ts=PARENT,
                                                           reply_count=0, latest_reply=None)])
    real = _break_failure_time(store, monkeypatch)
    h = Handlers(fail_post=SlackTimeout("timeout after 180s"))
    process_dm(slack, store, settings, {"insight": h, "feedback": h}, NOW)
    monkeypatch.setattr(store, "set_cursor", real)
    assert store.get_cursor("inflight:i1")  # kept for recover_interrupted
    cid = enqueue(store, at=CLICK)          # a click made during the first post's hang
    h2 = Handlers()
    process_page(slack, store, settings, {"insight": h2, "feedback": h2}, NOW + timedelta(minutes=1))
    assert len(h.posted) + len(h2.posted) == 1
    assert row(store, cid).result == f"⚠️ {EARLIER_POST_TEXT}"
    assert store.get_cursor("post_failed_at:i1")  # recorded by recover_interrupted on the page run


def _pre_d8_item(store, status, acted_at=None):
    """An item the older code left: an attempt (autopost_try only), no autopost_send, no failure time."""
    store.upsert_item(Item(id=T.ITEM_ID, kind="todo", day=T.MONDAY, status=status, body=T.GOOD, title="t",
                           dm_ts="1792400000.000100", source_ts=T.HUDDLE_TS, score=None, models={}, attempts=1,
                           edited=False, created_at=T.at(15, 18), acted_at=acted_at))
    store.set_cursor(f"autopost_try:{T.ITEM_ID}", T.at(15, 20).isoformat())


TWO_DAYS_LATER = T.at(15, 0, day=T.MONDAY + timedelta(days=2))


@pytest.mark.parametrize("status", ["skipped", "expired", "delivered", "failed"])
def test_m2_an_old_attempt_settles_quietly_and_keeps_its_status(store, settings, status):
    from haseeb_agent.jobs.todo import settle_attempts, unsettled
    s = T.TodoSlack()
    T.seed_huddle(s, [T.notes_reply()])  # the thread holds no post of ours
    _pre_d8_item(store, status)
    settle_attempts(s, store, settings, TWO_DAYS_LATER)
    it = store.get_item(T.ITEM_ID)
    assert it.status == status and not unsettled(store, it)  # the status is kept; the attempt is settled
    assert s.dm_sends() == [] and s.channel_sends() == []     # no DM for an attempt from another day
    assert store.get_cursor(f"post_failed_at:{T.ITEM_ID}")


@pytest.mark.parametrize("acted_at", [None, T.at(15, 30)])
def test_m2_an_old_attempt_found_posted_keeps_its_acted_at(store, settings, acted_at):
    from haseeb_agent.jobs.todo import settle_attempts
    s = T.TodoSlack()
    T.seed_huddle(s, [T.notes_reply()])
    T.add_reply(s, T.msg(T.ts_at(15, 20, 6), T.GOOD, HASEEB, T.HUDDLE_TS))  # our auto-post did land
    _pre_d8_item(store, "delivered", acted_at)
    settle_attempts(s, store, settings, TWO_DAYS_LATER)
    it = store.get_item(T.ITEM_ID)
    assert it.status == "posted" and s.dm_sends() == []
    assert it.acted_at == (acted_at or T.at(15, 20))  # kept; without one, the attempt's time, never the settle's


def test_m2_todays_unsettled_attempt_is_still_announced(todo_slack, store, settings):
    _killed_autopost(todo_slack, store, settings, lands=False)
    T.tick(todo_slack, store, settings, T.at(15, 21, 0))
    assert store.get_item(T.ITEM_ID).status == "failed"
    assert [m.text for m in todo_slack.dm_sends()].count(T.todo_mod.AUTOPOST_FAILED) == 1


def test_m3_the_settle_failure_time_is_taken_after_its_thread_read(todo_slack, store, settings):
    _killed_autopost(todo_slack, store, settings, lands=False)  # the attempt began 15:20:00
    clock = [T.at(15, 21, 0)]
    real = todo_slack.read_thread

    def read(channel_id, ts):
        if channel_id == T.CH:
            clock[0] = T.at(15, 21, 40)  # the settle's thread read takes 40 s
        return real(channel_id, ts)

    todo_slack.read_thread = read
    run_todo(todo_slack, T.FakeLLM(), store, settings, T.at(15, 21, 0), clock=lambda: clock[0])
    assert store.get_item(T.ITEM_ID).status == "failed"
    assert store.get_cursor(f"post_failed_at:{T.ITEM_ID}") == f"{T.at(15, 21, 40).timestamp():.6f}"
    got = _page(todo_slack, store, settings, "post", T.at(15, 21, 5))  # clicked during the read: not informed
    assert got.result == f"⚠️ {EARLIER_POST_TEXT}" and todo_slack.channel_sends() == []
    got = _page(todo_slack, store, settings, "post", T.at(15, 22, 0))  # after the notice: an informed override
    assert got.state == "done" and len(todo_slack.channel_sends()) == 1


EXPIRED = "Expired — this click waited too long (the agent wasn't running). Click again if you still want it."


def test_m5_a_queued_click_older_than_15_minutes_expires_and_never_runs(slack, store, settings):
    make_item(store)
    old = enqueue(store, at=NOW - timedelta(minutes=15, seconds=1))
    h = Handlers()
    run(slack, store, settings, h)
    assert (row(store, old).state, row(store, old).result) == ("failed", EXPIRED)
    assert h.posted == [] and slack.sent == [] and slack.reactions == []
    young = enqueue(store, at=NOW - timedelta(minutes=14))
    run(slack, store, settings, h)
    assert row(store, young).state == "done" and len(h.posted) == 1


# -- M1: a write the pause refused is nothing sent ------------------------------------------------------------------

def test_a_page_post_the_pause_refused_is_a_failed_row_that_says_nothing_was_sent(store, settings):
    """The page's row ends `failed` with the words, the item is left as it was (no failure time, no attempt, no send
    marker), and there is no reaction or DM line (every DM write is refused while paused). After Resume the same click,
    made again, posts."""
    from haseeb_agent.commands import PAUSED_NOTHING_SENT, post_failed_key
    from haseeb_agent.jobs.feedback import FeedbackHandlers
    slack = PausedSlack()
    make_item(store, id="fb1", kind="feedback")
    store.mark("fb1", "delivered", source_ts=T.HUDDLE_TS)
    h = FeedbackHandlers(slack, T.FakeLLM(), store, settings)
    cid = enqueue(store, item_id="fb1")
    assert run(slack, store, settings, h) == 1
    got = row(store, cid)
    assert (got.state, got.result) == ("failed", "paused: nothing was sent") and got.result == PAUSED_NOTHING_SENT
    item = store.get_item("fb1")
    assert (item.status, item.attempts) == ("delivered", 0)
    assert not store.get_cursor(post_failed_key("fb1")) and not store.get_cursor("send_started:fb1")
    assert not store.get_cursor("inflight:fb1")
    assert slack.sent == [] and slack.reactions == [] and slack.refused.count("send") == 1  # only the channel write
    assert store.cursors("paused_command_notice:") == {}  # the row says it; only a DM command needs a notice (N2)
    slack.paused = False
    again = enqueue(store, item_id="fb1")
    assert run(slack, store, settings, h) == 1
    assert row(store, again).state == "done" and store.get_item("fb1").status == "posted"


class PausedHandlers(Handlers):
    """Handlers whose every write the gateway's pause refuses."""

    def post(self, item, reply_ts=None):
        raise SlackPaused("slack_send_message: the agent is paused, nothing was sent")

    def revise(self, item, instruction):
        raise SlackPaused("slack_send_message: the agent is paused, nothing was sent")

    def attach(self, item):
        raise SlackPaused("slack_get_file_upload_url: the agent is paused, nothing was sent")


@pytest.mark.parametrize("verb,arg", [("post", None), ("edit", "make it about Pakistan"), ("shorter", None),
                                      ("attach", None)])
def test_every_verb_the_pause_refused_says_the_same_words(store, settings, verb, arg):
    """N7: an edit, a shorter and an attach are not "posts", so the row says "nothing was sent" for all of them, and none
    of them counts as an attempt or fails the item."""
    slack = PausedSlack()
    make_item(store)
    cid = enqueue(store, verb, arg=arg)
    assert process_page(slack, store, settings, {"insight": PausedHandlers()}, NOW) == 1
    got = row(store, cid)
    assert (got.state, got.result) == ("failed", "paused: nothing was sent")
    item = store.get_item("i1")
    assert (item.status, item.attempts) == ("delivered", 0) and not store.get_cursor("post_failed_at:i1")


def test_a_pause_between_the_parts_of_a_post_is_still_may_have_gone_out(store, settings, monkeypatch):
    """Part 1 is in the channel, so that is not "nothing was sent": the insight handler reports a failure after part 1
    went out (a SlackError, not the SlackPaused that caused it), which stays a real may-have-posted failure."""
    from haseeb_agent.jobs import insights as ins
    from haseeb_agent.jobs.insights import InsightHandlers
    slack = PausedSlack()
    slack.paused = False
    real = slack.send

    def send(channel_id, text, thread_ts=None):
        ts = real(channel_id, text, thread_ts)
        slack.paused = True  # the pause lands right after part 1 went out
        return ts
    slack.send = send
    make_item(store, id="in1", kind="insight")
    monkeypatch.setattr(ins, "_load", lambda st, it: {"v": 1, "insight": {}})  # a v1 body: the reader dispatches on v
    monkeypatch.setattr(ins, "_insight_from", lambda d: None)
    monkeypatch.setattr(ins, "render_insight_post", lambda x: ["part 1", "part 2"])
    cid = enqueue(store, item_id="in1")
    assert run(slack, store, settings, InsightHandlers(slack, T.FakeLLM(), store, settings)) == 1
    got = row(store, cid)
    assert got.state == "failed" and got.result.startswith("⚠️ Post may have gone out")
    assert store.get_item("in1").status == "failed" and store.get_cursor("post_failed_at:in1")
    assert len([x for x in slack.sent if x.channel_id == settings.channel_id]) == 1


def test_an_autopost_the_pause_refused_is_cancelled_and_not_failed(todo_slack, store, settings):
    """The pause landed during the tick: the gateway refused the auto-post before any call. The attempt is taken back
    (otherwise the item would look unsettled and be marked failed), the auto-post is cancelled by the same path as a
    reply of Haseeb's, and he is told after Resume. No failed run is recorded."""
    item = _armed(todo_slack, store, settings)
    real_send = todo_slack.send

    def refuse(channel_id, text, thread_ts=None):
        raise SlackPaused("slack_send_message: the agent is paused, nothing was sent")
    todo_slack.send = refuse
    sent_before = len(todo_slack.sent)
    T.tick(todo_slack, store, settings, T.at(15, 20, 0))  # the auto-post falls due
    got = store.get_item(T.ITEM_ID)
    assert got.status == "delivered" and len(todo_slack.sent) == sent_before
    assert not store.get_cursor(f"autopost:{T.ITEM_ID}")  # cancelled, so it cannot fire late either
    assert store.get_cursor(f"autopost_try:{T.ITEM_ID}") is None and store.get_cursor(f"autopost_send:{T.ITEM_ID}") is None
    assert not store.get_cursor(f"post_failed_at:{T.ITEM_ID}") and not T.todo_mod.unsettled(store, got)
    assert json.loads(store.get_cursor(f"todo_notice:{T.ITEM_ID}")) == [AUTOPOST_CANCELLED]  # queued for the Resume
    assert all(ok for _, ok, _, _ in store.runs())  # nothing is recorded as a failed run
    # Resume: the notice reaches the DM thread once, nothing posts by itself, and his `post` is a plain first post
    todo_slack.send = real_send
    for minute in range(21, 40):
        T.tick(todo_slack, store, settings, T.at(15, minute, 0))
    assert [x.text for x in todo_slack.dm_sends() if x.thread_ts == item.dm_ts] == [AUTOPOST_CANCELLED]
    assert todo_slack.channel_sends() == []
    done = _page(todo_slack, store, settings, "post", T.at(15, 41, 0))
    assert done.state == "done" and done.result.startswith("✅ Posted") and len(todo_slack.channel_sends()) == 1


# -- M2: the paused tick cancels a pending auto-post (database only) --------------------------------------------------

def test_a_paused_tick_cancels_a_pending_autopost_and_the_notice_waits_for_resume(todo_slack, store, settings):
    """The pause comes while an auto-post is armed. /api/action refuses a cancel while paused, so the few minutes of
    veto would run out during the pause and the post would fire on the first tick after Resume. The paused tick
    cancels it with no Slack call, and "reply post to post it" is queued for the Resume."""
    from haseeb_agent import controls
    item = _armed(todo_slack, store, settings)  # auto-post due 15:20
    controls.set_paused(store, True, T.at(15, 19, 0))
    sent, reads = len(todo_slack.sent), len(todo_slack.calls)
    since = controls.paused_since(store)
    assert cli.pause_housekeeping(settings, store, since, T.at(15, 19, 30)) == "1 auto-post(s) cancelled"
    assert not store.get_cursor(f"autopost:{T.ITEM_ID}")
    assert json.loads(store.get_cursor(f"todo_notice:{T.ITEM_ID}")) == [AUTOPOST_CANCELLED]
    assert (len(todo_slack.sent), len(todo_slack.calls)) == (sent, reads)  # no Slack call of any kind
    assert store.get_item(T.ITEM_ID).status == "delivered"  # still his to post
    # tick after tick of the pause: nothing more to do, and the notice is not queued twice
    assert cli.pause_housekeeping(settings, store, since, T.at(15, 20, 30)) == ""
    assert json.loads(store.get_cursor(f"todo_notice:{T.ITEM_ID}")) == [AUTOPOST_CANCELLED]
    # Resume 10 minutes later, inside the 30-minute notes window: without the cancel it would post now
    controls.set_paused(store, False, T.at(15, 29, 0))
    T.tick(todo_slack, store, settings, T.at(15, 30, 0))
    assert todo_slack.channel_sends() == []
    assert [x.text for x in todo_slack.dm_sends() if x.thread_ts == item.dm_ts] == [AUTOPOST_CANCELLED]
    T.tick(todo_slack, store, settings, T.at(15, 31, 0))
    assert [x.text for x in todo_slack.dm_sends() if x.thread_ts == item.dm_ts] == [AUTOPOST_CANCELLED]  # once
    assert _page(todo_slack, store, settings, "post", T.at(15, 32, 0)).state == "done"  # and his `post` posts it
    assert len(todo_slack.channel_sends()) == 1


def test_the_pause_leaves_alone_what_has_no_pending_autopost(todo_slack, store, settings):
    from haseeb_agent.jobs.todo import cancel_autoposts_for_pause
    _armed(todo_slack, store, settings)
    # an auto-post whose attempt began is settle_attempts' to find out, not the pause's
    store.set_cursor(f"autopost_try:{T.ITEM_ID}", T.at(15, 20, 0).isoformat())
    assert cancel_autoposts_for_pause(store, settings, T.at(15, 21, 0)) == 0
    assert store.get_cursor(f"autopost:{T.ITEM_ID}") and not store.get_cursor(f"todo_notice:{T.ITEM_ID}")
    store.delete_cursor(f"autopost_try:{T.ITEM_ID}")
    store.mark(T.ITEM_ID, "posted")  # only a delivered item has one to cancel
    assert cancel_autoposts_for_pause(store, settings, T.at(15, 21, 0)) == 0
    store.mark(T.ITEM_ID, "delivered", edited=True)
    store.set_cursor(f"autopost:{T.ITEM_ID}", "")  # none pending
    assert cancel_autoposts_for_pause(store, settings, T.at(15, 21, 0)) == 0


def test_expire_for_pause_reads_the_pause_time_as_a_time(store):
    """The clicks and the pause are compared as times, whatever offset either was written with; a pause time that
    cannot be read expires every queued click (it cannot tell which came first)."""
    from haseeb_agent.page_commands import EXPIRED_BY_PAUSE, expire_for_pause
    kuwait_noon = datetime(2026, 10, 20, 12, 0, tzinfo=KW)  # 09:00 UTC
    older = enqueue(store, item_id="a", shown="0" * 64, at=datetime(2026, 10, 20, 8, 59, tzinfo=ZoneInfo("UTC")))
    newer = enqueue(store, item_id="b", shown="0" * 64, at=datetime(2026, 10, 20, 9, 1, tzinfo=ZoneInfo("UTC")))
    naive = enqueue(store, item_id="c", shown="0" * 64, at=datetime(2026, 10, 20, 11, 59))  # taken as Kuwait time
    assert expire_for_pause(store, kuwait_noon.isoformat(), kuwait_noon) == 2
    assert [row(store, c).state for c in (older, newer, naive)] == ["failed", "queued", "failed"]
    assert row(store, older).result == EXPIRED_BY_PAUSE == "expired: queued before the pause"
    assert expire_for_pause(store, "not a time", kuwait_noon) == 1 and row(store, newer).state == "failed"
    assert expire_for_pause(store, kuwait_noon.isoformat(), kuwait_noon) == 0  # nothing left


def test_a_naive_pause_time_is_read_in_the_ticks_zone_not_as_utc(store):
    """A pause time with no offset (hand-written data; the old manual one-liner wrote one) is read in the zone of `now`,
    the zone the dashboard writes its own times in. Read as UTC, noon would be 15:00 in Kuwait and both clicks expire."""
    from haseeb_agent.page_commands import expire_for_pause
    noon = datetime(2026, 10, 20, 12, 0, tzinfo=KW)
    before = enqueue(store, item_id="a", shown="0" * 64, at=datetime(2026, 10, 20, 11, 59, tzinfo=KW))
    after = enqueue(store, item_id="b", shown="0" * 64, at=datetime(2026, 10, 20, 12, 1, tzinfo=KW))
    assert expire_for_pause(store, "2026-10-20 12:00:00", noon) == 1
    assert [row(store, c).state for c in (before, after)] == ["failed", "queued"]


def test_the_expiry_is_one_update_that_only_touches_queued_rows(store):
    """N4: one `UPDATE ... WHERE state='queued'`, so a row that a tick claims at the same moment is left to the tick and
    no row is ever left `running` with no outcome (a claim followed by a second statement could be, if the process died
    between the two)."""
    from haseeb_agent.page_commands import EXPIRED_BY_PAUSE, expire_for_pause
    noon = datetime(2026, 10, 20, 12, 0, tzinfo=KW)
    early = datetime(2026, 10, 20, 11, 0, tzinfo=KW)
    ids = [enqueue(store, item_id=f"i{n}", shown="0" * 64, at=early) for n in range(3)]
    seen = []
    store._db.set_trace_callback(seen.append)
    assert expire_for_pause(store, noon.isoformat(), noon) == 3
    store._db.set_trace_callback(None)
    updates = [q for q in seen if q.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1 and "state='queued'" in updates[0]
    assert [(row(store, c).state, row(store, c).result) for c in ids] == [("failed", EXPIRED_BY_PAUSE)] * 3


def test_a_row_a_tick_claimed_between_the_listing_and_the_update_is_left_to_the_tick(store):
    from haseeb_agent.page_commands import expire_for_pause
    noon = datetime(2026, 10, 20, 12, 0, tzinfo=KW)
    first = enqueue(store, item_id="a", shown="0" * 64, at=datetime(2026, 10, 20, 11, 0, tzinfo=KW))
    second = enqueue(store, item_id="b", shown="0" * 64, at=datetime(2026, 10, 20, 11, 1, tzinfo=KW))
    real = store.page_commands_in

    def listing(*args, **kwargs):
        rows = real(*args, **kwargs)
        store.claim_page_command(first)  # the tick takes the first click after it was listed
        return rows
    store.page_commands_in = listing
    assert expire_for_pause(store, noon.isoformat(), noon) == 1
    assert [row(store, c).state for c in (first, second)] == ["running", "failed"]
