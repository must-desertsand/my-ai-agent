"""R44: decks are built after the text is delivered, uploaded into the insight's DM thread, retried at most twice,
and attached to the channel post on `post` (never retried, R16)."""
import ast
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from haseeb_agent.config import load_settings

from haseeb_agent.deck.render import DeckError
from haseeb_agent.jobs import decks as decks_mod
from haseeb_agent.jobs.decks import (
    ATTACH_FAILED,
    ATTACH_NOT_SENT,
    ATTACH_NOT_SENT_TEXT,
    LATE_NOTE,
    DECK_COMMENT,
    MAX_TRIES,
    current_deck,
    decks_pending,
    deliver_decks,
)
from haseeb_agent.jobs.insights import InsightHandlers, deliver_pending
from haseeb_agent.slack_gateway import (
    SlackError,
    SlackNotCalled,
    SlackNotShared,
    SlackPaused,
    SlackTimeout,
    SlackUnavailable,
)
from tests.test_insights import (  # noqa: F401 - fixtures
    CH,
    HASEEB,
    MONDAY,
    FakeLLM,
    candidate_in,
    ROOT,
    slack,
    store,
    stored_v1,
)
from tests.test_insights import v1_insight_for as insight_for

DM = "D08D9HFB8BB"


@pytest.fixture
def settings(tmp_path):
    """The repo's settings with decks on, whatever config.toml says today."""
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return replace(load_settings(ROOT / "tests" / "fixtures" / "config.toml", env), decks_enabled=True, deck_designer=False)


@pytest.fixture(autouse=True)
def _wall(monkeypatch, slack):
    """R50: a DM upload finishes after every Slack ts handed out so far (FakeSlack counts its ts up)."""
    monkeypatch.setattr(decks_mod, "_wall", lambda: slack._next + 0.5)


class Builder:
    def __init__(self, fail=0, exc=DeckError("chrome exit 1")):
        self.calls, self.fail, self.exc = [], fail, exc

    def __call__(self, ins, day, out_dir, *, cover=None):
        self.calls.append((ins, day, Path(out_dir), cover))
        if self.fail:
            self.fail -= 1
            raise self.exc
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        pdf = Path(out_dir) / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4 " + ins.title.encode())
        return pdf


class Cover:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, topic, out_path):
        self.calls.append((topic, Path(out_path)))
        if not self.ok:
            return None
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"\x89PNG fake")
        return Path(out_path)


def delivered(slack, store, settings, n=1):
    """n delivered v1 insights, stored directly (the job writes ideas now): the items of before the switch.
    `delivered_ideas`, at the end of this file, does the same for ideas."""
    for i, name in enumerate(("alpha", "bravo", "charlie")[:n], 1):
        stored_v1(store, name, i, status="drafted")
    return deliver_pending(slack, store, settings, MONDAY)


@pytest.fixture(autouse=True)
def pinned_codex(monkeypatch):
    """The installed Codex is the pinned one unless a test says otherwise (deck/codexcheck.py)."""
    from haseeb_agent.deck import codexcheck
    monkeypatch.setattr(codexcheck, "version_ok", lambda *a, **k: True)


def decks(slack, store, settings, tmp_path, builder=None, cover=None, **kw):
    return deliver_decks(slack, store, settings, MONDAY, builder=builder or Builder(), cover_maker=cover or Cover(),
                         out_root=tmp_path / "decks", **kw)


# -- the DM deck -------------------------------------------------------------------------


def test_deck_is_uploaded_in_the_items_dm_thread_after_the_text(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    texts_before = list(slack.sent)
    b, c = Builder(), Cover()
    assert decks(slack, store, settings, tmp_path, b, c) == 1
    assert slack.sent == texts_before  # the text is untouched
    (up,) = slack.uploads
    assert (up.channel_id, up.thread_ts, up.comment, up.title) == (DM, item.dm_ts, DECK_COMMENT, item.title)
    assert up.path.name == f"{item.id}.pdf" and up.path.is_file()
    assert c.calls[0][0] == "AI: Topic alpha"  # title + category only
    assert b.calls[0][3] == c.calls[0][1]  # the codex image is the cover
    assert store.get_item(item.id).models["deck"] == "codex"
    assert store.get_item(item.id).status == "delivered"


def test_no_codex_image_means_the_template(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    b = Builder()
    decks(slack, store, settings, tmp_path, b, Cover(ok=False))
    assert b.calls[0][3] is None
    assert store.get_item(item.id).models["deck"] == "template"


def test_only_delivered_items_get_decks_and_only_once(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    store.mark(item.id, "drafted")
    assert decks(slack, store, settings, tmp_path) == 0
    store.mark(item.id, "delivered")
    assert decks(slack, store, settings, tmp_path) == 1
    b = Builder()
    assert decks(slack, store, settings, tmp_path, b) == 0
    assert b.calls == [] and len(slack.uploads) == 1


def test_decks_disabled_does_nothing(slack, store, settings, tmp_path):
    delivered(slack, store, settings)
    b = Builder()
    assert decks(slack, store, replace(settings, decks_enabled=False), tmp_path, b) == 0
    assert b.calls == [] and slack.uploads == []


def test_limit_builds_one_deck_per_call(slack, store, settings, tmp_path):
    items = delivered(slack, store, settings, n=3)
    assert len(items) == 3
    assert decks(slack, store, settings, tmp_path, limit=1) == 1
    assert decks(slack, store, settings, tmp_path, limit=1) == 1
    assert len(slack.uploads) == 2


def test_failed_upload_retries_next_time_without_rebuilding(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    b, c = Builder(), Cover()
    slack.fail_next("upload broke", method="upload_pdf")
    assert decks(slack, store, settings, tmp_path, b, c) == 0  # no raise
    assert store.get_item(item.id).status == "delivered" and slack.uploads == []
    assert decks(slack, store, settings, tmp_path, b, c) == 1
    assert len(b.calls) == 1 and len(c.calls) == 1  # Chrome and Codex ran once
    assert len(slack.uploads) == 1


def test_gives_up_after_two_retries_with_one_log_line(slack, store, settings, tmp_path, caplog):
    (item,) = delivered(slack, store, settings)
    b = Builder(fail=99)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            decks(slack, store, settings, tmp_path, b)
    assert len(b.calls) == MAX_TRIES == 3
    assert sum("gave up" in r.getMessage() for r in caplog.records) == 1
    assert slack.uploads == [] and store.get_item(item.id).status == "delivered"
    assert "deck" not in store.get_item(item.id).models


# -- "Build decks" on the dashboard: today's given-up decks get one more try (`decks --retry`) ----------------------


def test_retry_gives_a_given_up_deck_one_more_try(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    b = Builder(fail=99)
    for _ in range(5):
        decks(slack, store, settings, tmp_path, b)
    assert len(b.calls) == MAX_TRIES and decks_pending(store, settings, MONDAY) is False  # gave up
    assert decks_mod.retry_given_up(store, settings, MONDAY) == 1
    assert decks_mod._state(store, item.id)["tries"] == MAX_TRIES - 1 and decks_pending(store, settings, MONDAY) is True
    b.fail = 0
    assert decks(slack, store, settings, tmp_path, b) == 1  # the same work as any other run
    assert len(slack.uploads) == 1 and store.get_item(item.id).models["deck"] == "codex"
    assert decks_mod.retry_given_up(store, settings, MONDAY) == 0  # a finished deck is left alone


def test_retry_is_exactly_one_attempt_per_click(slack, store, settings, tmp_path):
    """M6: the tick runs a deck run every 2 minutes while a deck is pending, so a reset to a full set of tries gave a
    failing deck three build-and-upload attempts (with Codex, up to about 12 minutes) per click. Now: one attempt, then
    it has given up again and nothing tries until the next click."""
    (item,) = delivered(slack, store, settings)
    b = Builder(fail=99)
    for _ in range(5):
        decks(slack, store, settings, tmp_path, b)
    before = len(b.calls)
    assert decks_mod.retry_given_up(store, settings, MONDAY) == 1
    for _ in range(4):  # the click's own run, then the tick's runs that follow
        decks(slack, store, settings, tmp_path, b)
    assert len(b.calls) - before == 1  # one attempt
    assert decks_mod._state(store, item.id)["state"] == "gave_up" and decks_pending(store, settings, MONDAY) is False
    assert decks_mod.retry_given_up(store, settings, MONDAY) == 1  # and the next click is one more
    decks(slack, store, settings, tmp_path, b)
    assert len(b.calls) - before == 2


def test_retry_keeps_a_built_pdf_so_a_failed_upload_only_uploads_again(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    b = Builder()
    for _ in range(MAX_TRIES):
        slack.fail_next("upload down", method="upload_pdf")
        decks(slack, store, settings, tmp_path, b)
    assert decks_mod._state(store, item.id)["state"] == "gave_up" and len(b.calls) == 1  # built once, never uploaded
    assert decks_mod.retry_given_up(store, settings, MONDAY) == 1
    assert decks(slack, store, settings, tmp_path, b) == 1
    assert len(b.calls) == 1 and len(slack.uploads) == 1  # the kept build was uploaded


def test_retry_also_resets_a_deck_whose_attempts_died_without_a_result(slack, store, settings, tmp_path):
    """A crash or the watchdog leaves tries at the cap with the state still pending: the next run would only give up."""
    (item,) = delivered(slack, store, settings)
    st = decks_mod._state(store, item.id) or {"sha": "x"}
    st.update(tries=MAX_TRIES, state="pending")
    store.set_cursor(decks_mod._key(item.id), json.dumps(st))
    assert decks_mod.retry_given_up(store, settings, MONDAY) == 1
    assert decks_mod._state(store, item.id)["tries"] == MAX_TRIES - 1


def test_retry_leaves_decks_that_have_not_gone_wrong_and_other_days_alone(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    assert decks_mod.retry_given_up(store, settings, MONDAY) == 0  # missing, not given up: deliver_decks builds it
    assert decks_mod._state(store, item.id) == {}
    b = Builder(fail=99)
    for _ in range(4):
        decks(slack, store, settings, tmp_path, b)
    tomorrow = MONDAY.replace(day=MONDAY.day + 1)
    assert decks_mod.retry_given_up(store, settings, tomorrow) == 0  # today's decks only
    assert decks_mod._state(store, item.id)["state"] == "gave_up"


def test_a_crash_mid_attempt_still_counts(slack, store, settings, tmp_path):
    class Killed(BaseException):
        pass

    delivered(slack, store, settings)
    b = Builder(fail=99, exc=Killed())
    for _ in range(MAX_TRIES + 2):
        with pytest.raises(Killed):
            decks(slack, store, settings, tmp_path, b)
        if len(b.calls) == MAX_TRIES:
            break
    assert decks(slack, store, settings, tmp_path, b) == 0
    assert len(b.calls) == MAX_TRIES  # a watchdog kill can't make it rebuild forever


def test_revised_text_gets_a_fresh_deck_and_keeps_the_cover(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    b, c = Builder(), Cover()
    decks(slack, store, settings, tmp_path, b, c)
    llm = FakeLLM(writer=lambda user, after: insight_for(candidate_in(user), title="Topic alpha, revised"))
    InsightHandlers(slack, llm, store, settings).revise(store.get_item(item.id), "change it")
    assert current_deck(store, settings, store.get_item(item.id), tmp_path / "decks") is None  # stale now
    assert decks(slack, store, settings, tmp_path, b, c) == 1
    assert len(b.calls) == 2 and len(c.calls) == 1  # the Codex cover is reused
    assert b.calls[1][0].title == "Topic alpha, revised"
    assert len(slack.uploads) == 2


def test_model_line_shows_the_deck_once_it_exists(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    assert any("Deck: —" in s.text for s in slack.sent)
    decks(slack, store, settings, tmp_path)
    InsightHandlers(slack, FakeLLM(), store, settings).revise(store.get_item(item.id), "tighter")
    assert "Deck: codex" in [s for s in slack.sent if s.thread_ts == item.dm_ts][-1].text


def test_delivery_never_waits_for_a_deck(slack, store, settings, monkeypatch):
    monkeypatch.setattr(decks_mod, "deliver_decks", lambda *a, **k: pytest.fail("no deck during text delivery"))
    delivered(slack, store, settings)
    deliver_pending(slack, store, settings, MONDAY)
    assert slack.uploads == []


def test_build_leftovers_are_removed_and_the_cover_kept(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    folder = tmp_path / "decks" / item.id
    assert sorted(p.name for p in folder.iterdir()) == ["codex-cover.png", f"{item.id}.pdf"]  # no build/ copy


def test_deck_folders_older_than_30_days_are_pruned(slack, store, settings, tmp_path):
    import os
    root = tmp_path / "decks"
    old, fresh = root / "insight-2026-09-01-1", root / "insight-2026-10-18-1"
    for d in (old, fresh):
        d.mkdir(parents=True)
        (d / "x.pdf").write_bytes(b"%PDF")
    stamp = (MONDAY.timestamp() - 31 * 86400)
    os.utime(old, (stamp, stamp))
    decks(slack, store, settings, tmp_path)
    assert not old.exists() and fresh.exists()


# -- the channel post (attached by the deck job afterwards, never inside post) ----------------------------------


def post(slack, store, settings, item):
    return InsightHandlers(slack, FakeLLM(), store, settings).post(store.get_item(item.id))


def channel_uploads(slack):
    return [u for u in slack.uploads if u.channel_id == CH]


def top_post(slack):
    return next(s for s in slack.sent if s.channel_id == CH and s.thread_ts is None)


def test_post_returns_as_soon_as_the_text_is_out(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    link = post(slack, store, settings, item)
    assert channel_uploads(slack) == []  # no upload inside post
    assert store.get_cursor(f"post_ts:{item.id}") == top_post(slack).ts
    assert top_post(slack).ts.replace(".", "") in link


def test_deck_job_attaches_the_deck_in_the_posts_thread_once(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    store.mark(item.id, "posted")
    post(slack, store, settings, item)
    decks(slack, store, settings, tmp_path)
    (up,) = channel_uploads(slack)
    assert (up.thread_ts, up.title, up.comment) == (top_post(slack).ts, item.title, None)
    assert store.get_cursor(f"attach:{item.id}") == "done"
    decks(slack, store, settings, tmp_path)
    assert len(channel_uploads(slack)) == 1


def test_a_deck_finished_after_the_post_goes_to_the_dm_only_with_a_note(slack, store, settings, tmp_path):
    """R50: Haseeb never saw that deck before he posted, so it is not attached on its own."""
    (item,) = delivered(slack, store, settings)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    decks(slack, store, settings, tmp_path)
    dm = [u for u in slack.uploads if u.channel_id == DM]
    assert len(dm) == 1 and channel_uploads(slack) == []
    note = slack.sent[-1]
    assert (note.channel_id, note.thread_ts, note.text) == (HASEEB, item.dm_ts, LATE_NOTE)
    assert LATE_NOTE == "📎 Deck finished after you posted — not attached; reply `attach` to add it"
    assert store.get_cursor(f"attach:{item.id}") == "late"
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == [] and slack.sent[-1] == note  # the note goes once


def test_an_unposted_item_is_never_attached(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    post(slack, store, settings, item)  # the text went out but the command never recorded "posted"
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == []


def test_a_revised_then_posted_insight_gets_the_fresh_deck(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    llm = FakeLLM(writer=lambda user, after: insight_for(candidate_in(user), title="Topic alpha, revised"))
    InsightHandlers(slack, llm, store, settings).revise(store.get_item(item.id), "change it")
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == []  # R50: the fresh deck reached the DM after the post: not on its own
    from haseeb_agent.jobs.decks import attach_now
    attach_now(slack, store, settings, store.get_item(item.id), tmp_path / "decks")
    (up,) = channel_uploads(slack)
    assert b"Topic alpha, revised" in up.path.read_bytes()


def test_attach_failure_is_not_retried_and_says_so_in_the_dm(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    slack.fail_next("upload broke", method="upload_pdf")
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == []
    note = slack.sent[-1]
    assert (note.channel_id, note.thread_ts, note.text) == (HASEEB, item.dm_ts, ATTACH_FAILED)
    assert store.get_cursor(f"own:{note.ts}") == "1"
    assert store.get_cursor(f"attach:{item.id}") == "failed"
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == [] and slack.sent[-1] == note  # R16: never again


def test_a_failing_note_never_raises(slack, store, settings, tmp_path, monkeypatch):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    slack.fail_next("upload broke", method="upload_pdf")
    monkeypatch.setattr(decks_mod, "send_dm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk I/O error")))
    decks(slack, store, settings, tmp_path)  # no raise
    assert store.get_item(item.id).status == "posted"


def test_attach_killed_mid_way_leaves_the_post_recorded(slack, store, settings, tmp_path):
    """Fix round 1: a crash or the watchdog during the attach can no longer touch the post's own outcome."""
    from haseeb_agent.commands import process_dm
    from haseeb_agent.models import Msg

    class Killed(BaseException):
        pass

    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    reply_ts = f"{slack._next + 1}.000001"  # R50: after the deck reached the DM
    parent = slack.add_message(HASEEB, item.dm_ts, "card", user_id=HASEEB, reply_count=1, latest_reply=reply_ts)
    slack.add_thread(HASEEB, parent, [Msg(ts=reply_ts, user_id=HASEEB, user_name="Haseeb", text="post",
                                          thread_ts=item.dm_ts, reply_count=0, latest_reply=None)])
    handlers = {"insight": InsightHandlers(slack, FakeLLM(), store, settings)}
    assert process_dm(slack, store, settings, handlers, MONDAY) == 1
    link = store.get_cursor(f"permalink:{item.id}")
    assert store.get_item(item.id).status == "posted" and link.startswith("https://")

    def die(*a, **k):
        raise Killed()
    monkeypatch_upload = slack.upload_pdf
    slack.upload_pdf = die
    with pytest.raises(Killed):
        decks(slack, store, settings, tmp_path)
    slack.upload_pdf = monkeypatch_upload
    assert store.get_item(item.id).status == "posted"
    assert store.get_cursor(f"permalink:{item.id}") == link
    assert not store.get_cursor(f"inflight:{item.id}")
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == []  # recorded before the attempt: never retried (R16)


def test_decks_pending_tracks_builds_retries_and_attaches(slack, store, settings, tmp_path):
    root = tmp_path / "decks"
    assert not decks_pending(store, settings, MONDAY, root)
    (item,) = delivered(slack, store, settings)
    assert decks_pending(store, settings, MONDAY, root)
    decks(slack, store, settings, tmp_path)
    assert not decks_pending(store, settings, MONDAY, root)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    assert decks_pending(store, settings, MONDAY, root)  # the attach is due
    decks(slack, store, settings, tmp_path)
    assert not decks_pending(store, settings, MONDAY, root)
    from dataclasses import replace as _r
    assert not decks_pending(store, _r(settings, decks_enabled=False), MONDAY, root)


def test_decks_pending_is_false_once_given_up(slack, store, settings, tmp_path):
    delivered(slack, store, settings)
    for _ in range(MAX_TRIES):
        decks(slack, store, settings, tmp_path, Builder(fail=1))
    assert not decks_pending(store, settings, MONDAY, tmp_path / "decks")


# -- safety scans ------------------------------------------------------------------------


def _calls(path: Path, names: tuple[str, ...]) -> list[tuple[str, str]]:
    found = []

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.ClassDef)) else owner
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in names:
                found.append((child.func.attr, ast.unparse(child.args[0]) if child.args else ""))
            visit(child, name)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def test_the_deck_job_uploads_only_to_the_self_dm_and_the_posts_thread():
    found = _calls(Path(decks_mod.__file__), ("send", "upload_pdf", "react"))
    assert sorted(found) == [("upload_pdf", "channel"), ("upload_pdf", "settings.self_dm_channel"),
                             ("upload_pdf", "settings.self_dm_channel")]  # R49: deck_test, the self-DM only
    # the post's thread is in the channel the post went to, which is read from the post's own record and from nothing else
    assert "channel = channels.post_channel(store, settings, item.id)" in Path(decks_mod.__file__).read_text(encoding="utf-8")


def test_deck_model_is_stored_as_a_plain_label(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    state = json.loads(store.get_cursor(f"deck:{item.id}"))
    assert state["state"] == "done" and state["model"] in ("codex", "template")


def test_recording_the_deck_model_never_reverts_a_concurrent_post(slack, store, settings, tmp_path, monkeypatch):
    """Parked from the Task 14 re-review: the detached deck process read the item, `post` then marked it posted in
    another process, and the deck's `mark(id, fresh.status, models=...)` wrote the stale `delivered` back."""
    (item,) = delivered(slack, store, settings)
    real_get, real_upload = store.get_item, slack.upload_pdf

    def upload_then_post(*a, **k):
        stale = real_get(item.id)  # what a read in the deck process sees just before the post lands
        out = real_upload(*a, **k)
        store.mark(item.id, "posted")  # the post, recorded by the tick's process
        monkeypatch.setattr(store, "get_item", lambda id: stale if id == item.id else real_get(id))
        return out

    monkeypatch.setattr(slack, "upload_pdf", upload_then_post)
    decks(slack, store, settings, tmp_path)
    got = real_get(item.id)
    assert got.status == "posted"  # the post survives
    assert got.models["deck"] == "codex" and got.models["wrote"]


# -- designer decks ----------------------------------------------------------------------------------------------


class Designer:
    """Stands in for deck.designer.design_deck."""

    def __init__(self, reason="ok", model="gpt-6.1-sol", repaired=False):
        self.reason, self.model, self.repaired, self.calls = reason, model, repaired, []

    def __call__(self, ins, day, out_pdf, *, deck_id):
        from haseeb_agent.deck.designer import DesignResult
        self.calls.append((ins, day, Path(out_pdf), deck_id))
        if self.reason != "ok":
            return DesignResult(None, None, self.reason)
        Path(out_pdf).parent.mkdir(parents=True, exist_ok=True)
        Path(out_pdf).write_bytes(b"%PDF-1.7 designer " + ins.title.encode())
        return DesignResult(Path(out_pdf), self.model, "ok", pages=10, images=4, repaired=self.repaired)


def test_a_designer_deck_is_uploaded_and_labelled_with_its_model(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    b, c, d = Builder(), Cover(), Designer()
    assert decks(slack, store, settings, tmp_path, b, c, designer=d) == 1
    assert b.calls == [] and c.calls == []  # neither the template nor a separate Codex cover
    (up,) = slack.uploads
    assert b"designer" in up.path.read_bytes() and up.path.name == f"{item.id}.pdf"
    assert d.calls[0][3] == item.id
    assert store.get_item(item.id).models["deck"] == "codex gpt-6.1-sol"
    assert json.loads(store.get_cursor(f"deck:{item.id}"))["model"] == "codex gpt-6.1-sol"


def test_the_astra_fallback_shows_in_the_model_line(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path, designer=Designer(model="gpt-6-astra"))
    assert store.get_item(item.id).models["deck"] == "codex gpt-6-astra"


def test_a_failed_fact_check_falls_back_to_the_template(slack, store, settings, tmp_path, caplog):
    (item,) = delivered(slack, store, settings)
    b, c = Builder(), Cover()
    with caplog.at_level(logging.INFO):
        assert decks(slack, store, settings, tmp_path, b, c, designer=Designer("fact-check")) == 1
    assert len(b.calls) == 1 and b.calls[0][3] is None and c.calls == []  # plain template: no second Codex run
    assert store.get_item(item.id).models["deck"] == "template (fact-check)"
    assert len(slack.uploads) == 1


@pytest.mark.parametrize("reason", ["timeout", "lockdown", "pages", "render", "no-deck", "error"])
def test_any_other_designer_failure_falls_back_to_the_plain_template(slack, store, settings, tmp_path, reason):
    (item,) = delivered(slack, store, settings)
    b = Builder()
    assert decks(slack, store, settings, tmp_path, b, Cover(), designer=Designer(reason)) == 1
    assert store.get_item(item.id).models["deck"] == "template"


def test_the_template_fallback_reuses_an_existing_codex_cover(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    folder = tmp_path / "decks" / item.id
    folder.mkdir(parents=True)
    (folder / "codex-cover.png").write_bytes(b"\x89PNG old cover")
    b = Builder()
    decks(slack, store, settings, tmp_path, b, Cover(), designer=Designer("timeout"))
    assert b.calls[0][3] == folder / "codex-cover.png"
    assert store.get_item(item.id).models["deck"] == "template"


def test_the_designer_pdf_and_leftovers_are_tidied(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path, designer=Designer())
    folder = tmp_path / "decks" / item.id
    assert sorted(p.name for p in folder.iterdir()) == [f"{item.id}.pdf"]


def test_the_default_designer_is_used_only_when_enabled(settings, monkeypatch):
    from haseeb_agent.jobs.decks import default_designer
    assert default_designer(replace(settings, deck_designer=False)) is None
    monkeypatch.setattr(decks_mod, "resolve_codex", lambda path: None)
    assert default_designer(replace(settings, deck_designer=True)) is None  # no codex binary: the template path
    monkeypatch.setattr(decks_mod, "resolve_codex", lambda path: "/opt/codex")
    seen = {}
    monkeypatch.setattr(decks_mod, "design_deck", lambda *a, **k: seen.update(k) or "r")
    d = default_designer(replace(settings, deck_designer=True, deck_models=("m1", "m2"), deck_effort="medium"))
    assert d("ins", "day", Path("x.pdf"), deck_id="i1") == "r"
    assert (seen["codex"], seen["models"], seen["effort"], seen["deck_id"]) == ("/opt/codex", ("m1", "m2"), "medium", "i1")
    assert seen["chrome"] == settings.chrome_path


def test_a_repaired_designer_deck_says_so_in_the_model_line(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path, designer=Designer(repaired=True))
    assert store.get_item(item.id).models["deck"] == "codex gpt-6.1-sol (repaired)"


# -- R49: haseeb-agent deck-test <item-id> -----------------------------------------------------------------------


def test_deck_test_builds_a_designer_deck_and_uploads_it_to_the_items_dm_thread(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import TEST_COMMENT, deck_test
    (item,) = delivered(slack, store, settings)
    before = store.get_item(item.id)
    d = Designer(repaired=True)
    res, uploaded = deck_test(slack, store, settings, item.id, designer=d, out_root=tmp_path / "decks")
    assert uploaded and res.repaired and res.model == "gpt-6.1-sol"
    (up,) = slack.uploads
    assert (up.channel_id, up.thread_ts, up.comment, up.title) == (DM, item.dm_ts, TEST_COMMENT, item.title)
    assert TEST_COMMENT == "📎 Deck (designer v2)"
    assert d.calls[0][3] == f"deck-test-{item.id}"
    assert not up.path.exists() and not up.path.parent.exists()  # M8: deleted after the upload
    after = store.get_item(item.id)
    assert (after.status, after.models) == (before.status, before.models)  # never changes the item
    assert store.get_cursor(f"deck:{item.id}") is None


def test_deck_test_never_touches_the_channel(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import deck_test
    (item,) = delivered(slack, store, settings)
    deck_test(slack, store, settings, item.id, designer=Designer(), out_root=tmp_path / "decks")
    assert all(u.channel_id == DM for u in slack.uploads)
    assert settings.channel_id == "C0C62C128D8" and all(u.channel_id != settings.channel_id for u in slack.uploads)
    with pytest.raises(ValueError):
        deck_test(slack, store, replace(settings, self_dm_channel=settings.channel_id), item.id, designer=Designer(),
                  out_root=tmp_path / "decks")


def test_deck_test_works_while_designer_decks_are_off(slack, store, settings, tmp_path, monkeypatch):
    from haseeb_agent.jobs.decks import deck_test
    (item,) = delivered(slack, store, settings)
    assert settings.deck_designer is False
    seen = {}
    monkeypatch.setattr(decks_mod, "resolve_codex", lambda path: "/opt/codex")
    monkeypatch.setattr(decks_mod, "design_deck", lambda *a, **k: seen.update(k) or Designer()(*a, deck_id=k["deck_id"]))
    res, uploaded = deck_test(slack, store, settings, item.id, out_root=tmp_path / "decks")
    assert uploaded and seen["models"] == settings.deck_models and seen["codex"] == "/opt/codex"


def test_deck_test_uploads_nothing_when_the_designer_fails(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import deck_test
    (item,) = delivered(slack, store, settings)
    res, uploaded = deck_test(slack, store, settings, item.id, designer=Designer("fact-check"),
                              out_root=tmp_path / "decks")
    assert not uploaded and res.reason == "fact-check" and slack.uploads == []


@pytest.mark.parametrize("item_id", ["insight-1999-01-01-1", "nope"])
def test_deck_test_needs_a_delivered_insight(slack, store, settings, tmp_path, item_id):
    from haseeb_agent.jobs.decks import deck_test
    with pytest.raises(ValueError):
        deck_test(slack, store, settings, item_id, designer=Designer(), out_root=tmp_path / "decks")


# -- R50: attach only a deck Haseeb could have seen; `attach` on request ------------------------------------------


def post_command(slack, store, settings, item, *, at):
    """Haseeb's `post` reply at Slack ts `at`, run through the command runner."""
    from haseeb_agent.commands import Command, _Runner
    from haseeb_agent.models import Msg
    handlers = {"insight": InsightHandlers(slack, FakeLLM(), store, settings)}
    reply = Msg(ts=at, user_id=HASEEB, user_name="Haseeb", text="post", thread_ts=item.dm_ts, reply_count=0,
                latest_reply=None)
    _Runner(slack, store, settings, handlers, MONDAY).handle(store.get_item(item.id), reply, Command("post"))
    return handlers


def test_the_cutoff_is_the_post_commands_ts(slack, store, settings, tmp_path, monkeypatch):
    (item,) = delivered(slack, store, settings)
    monkeypatch.setattr(decks_mod, "_wall", lambda: 2000.0)
    decks(slack, store, settings, tmp_path)  # the DM upload finished at 2000
    post_command(slack, store, settings, item, at="1999.000001")  # Haseeb replied `post` before it arrived
    assert store.get_cursor(f"post_cmd_ts:{item.id}") == "1999.000001"
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == [] and store.get_cursor(f"attach:{item.id}") == "late"


def test_a_deck_seen_before_the_post_command_is_attached(slack, store, settings, tmp_path, monkeypatch):
    (item,) = delivered(slack, store, settings)
    monkeypatch.setattr(decks_mod, "_wall", lambda: 2000.0)
    decks(slack, store, settings, tmp_path)
    post_command(slack, store, settings, item, at="2001.000001")
    decks(slack, store, settings, tmp_path)
    (up,) = channel_uploads(slack)
    assert up.thread_ts == store.get_cursor(f"post_ts:{item.id}") and store.get_cursor(f"attach:{item.id}") == "done"


def test_a_deck_without_an_upload_time_is_never_attached_on_its_own(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    st = json.loads(store.get_cursor(f"deck:{item.id}"))
    st.pop("dm_at")
    store.set_cursor(f"deck:{item.id}", json.dumps(st))  # a deck uploaded before R50
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    decks(slack, store, settings, tmp_path)
    assert channel_uploads(slack) == [] and store.get_cursor(f"attach:{item.id}") == "late"


def late(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    decks(slack, store, settings, tmp_path)
    assert store.get_cursor(f"attach:{item.id}") == "late"
    return store.get_item(item.id)


def test_attach_uploads_the_dm_deck_into_the_posts_thread_once(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import attach_now
    item = late(slack, store, settings, tmp_path)
    assert attach_now(slack, store, settings, item, tmp_path / "decks") == "📎 Deck attached to the post"
    (up,) = channel_uploads(slack)
    dm_deck = next(u for u in slack.uploads if u.channel_id == DM)
    assert up.thread_ts == store.get_cursor(f"post_ts:{item.id}") and up.path == dm_deck.path
    assert attach_now(slack, store, settings, item, tmp_path / "decks") == "📎 Deck already attached"
    assert len(channel_uploads(slack)) == 1
    assert store.get_item(item.id).status == "posted"


def test_a_failed_attach_is_not_retried(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import attach_now
    item = late(slack, store, settings, tmp_path)
    slack.fail_next("upload broke", method="upload_pdf")
    with pytest.raises(ValueError):
        attach_now(slack, store, settings, item, tmp_path / "decks")
    assert store.get_cursor(f"attach:{item.id}") == "failed"
    with pytest.raises(ValueError, match="not retried"):
        attach_now(slack, store, settings, item, tmp_path / "decks")
    assert channel_uploads(slack) == []


def test_attach_needs_a_posted_insight_with_a_dm_deck(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import attach_now
    (item,) = delivered(slack, store, settings)
    with pytest.raises(ValueError):
        attach_now(slack, store, settings, store.get_item(item.id), tmp_path / "decks")  # not posted
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    with pytest.raises(ValueError, match="no deck"):
        attach_now(slack, store, settings, store.get_item(item.id), tmp_path / "decks")  # no DM deck yet


def test_the_attach_reply_in_the_dm_thread_runs_once(slack, store, settings, tmp_path):
    """R50 end to end: the late note, then Haseeb's `attach` reply in the item's DM thread."""
    from haseeb_agent.commands import process_dm
    from haseeb_agent.models import Msg
    item = late(slack, store, settings, tmp_path)
    reply_ts = f"{slack._next + 1}.000001"
    parent = slack.add_message(HASEEB, item.dm_ts, "card", user_id=HASEEB, reply_count=1, latest_reply=reply_ts)
    slack.add_thread(HASEEB, parent, [Msg(ts=reply_ts, user_id=HASEEB, user_name="Haseeb", text="attach",
                                          thread_ts=item.dm_ts, reply_count=0, latest_reply=None)])
    handlers = {"insight": InsightHandlers(slack, FakeLLM(), store, settings)}
    import haseeb_agent.jobs.decks as dm
    dm_root = dm.decks_root
    dm.decks_root = lambda s: tmp_path / "decks"
    try:
        assert process_dm(slack, store, settings, handlers, MONDAY) == 1
        assert process_dm(slack, store, settings, handlers, MONDAY) == 0
    finally:
        dm.decks_root = dm_root
    assert len(channel_uploads(slack)) == 1 and store.get_cursor(f"attach:{item.id}") == "done"
    assert slack.sent[-1].text == "📎 Deck attached to the post" and store.get_item(item.id).status == "posted"


def test_a_changed_codex_version_alerts_once_a_day_and_the_template_is_used(slack, store, settings, tmp_path,
                                                                            monkeypatch):
    """The review of 466e622: another Codex version runs no designer deck or cover (codexcheck refuses it, tested
    in test_deck_codexcheck.py), and Haseeb is told once a day."""
    from haseeb_agent.deck import codexcheck
    from haseeb_agent.jobs import decks as dj
    shown = []
    monkeypatch.setattr(codexcheck, "version_ok", lambda *a, **k: False)
    monkeypatch.setattr(dj, "resolve_codex", lambda p: "/opt/codex")
    monkeypatch.setattr(dj, "notify", lambda s, kind, text, **k: shown.append((kind, text)) or True)
    delivered(slack, store, settings, n=2)
    assert decks(slack, store, settings, tmp_path) == 2  # template decks
    dj._codex_version_alert(store, settings, MONDAY.date())  # another tick, the same day
    assert shown == [("alert", "⚠️ Codex version changed — designer decks paused, run smoke")]
    dj._codex_version_alert(store, settings, MONDAY.date().replace(day=MONDAY.day + 1))  # the next day
    assert len(shown) == 2


def test_deck_test_keep_passes_keep_and_keeps_the_pdf(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import deck_test
    (item,) = delivered(slack, store, settings)
    seen = {}

    def designer(ins, day, out_pdf, *, deck_id, keep=False):
        seen["keep"] = keep
        return Designer()(ins, day, out_pdf, deck_id=deck_id)

    res, uploaded = deck_test(slack, store, settings, item.id, designer=designer, out_root=tmp_path / "decks", keep=True)
    assert uploaded and seen["keep"] is True and slack.uploads[0].path.exists()


# -- M1: a write the pause refused is nothing sent ---------------------------------------------------------------

def pause_uploads(slack, *, only_channel=None):
    """The gateway's pause for uploads: `upload_pdf` raises SlackPaused before it does anything (to the channel only,
    with `only_channel`). Returns the function that lifts it (the Resume)."""
    from haseeb_agent.slack_gateway import SlackPaused
    real = slack.upload_pdf

    def upload(channel_id, path, title, thread_ts, comment):
        if only_channel is None or channel_id == only_channel:
            raise SlackPaused("slack_get_file_upload_url: the agent is paused, nothing was sent")
        return real(channel_id, path, title, thread_ts, comment)
    slack.upload_pdf = upload

    def resume():
        slack.upload_pdf = real
    return resume


def test_an_attach_the_pause_refused_is_put_back_and_attaches_after_resume(slack, store, settings, tmp_path):
    """Before: `attach:{id}` was left `failed` for good, the deck never attached, and `attach` said "already tried"."""
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    resume = pause_uploads(slack, only_channel=CH)
    notes_before = len(slack.sent)
    decks(slack, store, settings, tmp_path)  # the attach is refused before the file is shared
    assert channel_uploads(slack) == [] and store.get_cursor(f"attach:{item.id}") is None  # as if never tried
    assert len(slack.sent) == notes_before  # no "Deck didn't attach" note: it did not fail
    decks(slack, store, settings, tmp_path)  # still paused: refused again, still nothing lost
    assert store.get_cursor(f"attach:{item.id}") is None
    resume()
    decks(slack, store, settings, tmp_path)
    (up,) = channel_uploads(slack)
    assert up.thread_ts == store.get_cursor(f"post_ts:{item.id}") and store.get_cursor(f"attach:{item.id}") == "done"


def test_an_attach_command_the_pause_refused_keeps_the_late_marker(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import attach_now
    from haseeb_agent.slack_gateway import SlackPaused
    item = late(slack, store, settings, tmp_path)  # attach:{id} is "late": reply `attach` adds it
    resume = pause_uploads(slack, only_channel=CH)
    with pytest.raises(SlackPaused):  # the command runner answers "paused: nothing was sent"
        attach_now(slack, store, settings, item, tmp_path / "decks")
    assert store.get_cursor(f"attach:{item.id}") == "late" and channel_uploads(slack) == []
    resume()
    assert attach_now(slack, store, settings, item, tmp_path / "decks") == "📎 Deck attached to the post"
    assert len(channel_uploads(slack)) == 1


def test_a_deck_upload_the_pause_refused_does_not_use_up_a_try(slack, store, settings, tmp_path):
    """The DM upload is refused before any call: not one of the three tries, however often the pause refuses it. The
    built PDF is kept, so after Resume it is only uploaded."""
    (item,) = delivered(slack, store, settings)
    b = Builder()
    resume = pause_uploads(slack)
    for _ in range(MAX_TRIES + 2):  # more refusals than there are tries
        assert decks(slack, store, settings, tmp_path, b) == 0
    state = decks_mod._state(store, item.id)
    assert (state["tries"], state["state"]) == (0, "pending") and decks_pending(store, settings, MONDAY)
    assert len(b.calls) == 1 and slack.uploads == []  # built once, never uploaded
    resume()
    assert decks(slack, store, settings, tmp_path, b) == 1
    assert len(b.calls) == 1 and len(slack.uploads) == 1 and decks_mod._state(store, item.id)["state"] == "done"


# -- an attach that sent nothing is not a failed attempt (R16 counts only what may have shared the file) --------------

def refuse_uploads(slack, exc, *, only_channel=CH):
    """`upload_pdf` raises `exc` (to the channel only, with `only_channel`). Returns (the list of refused attempts, the
    function that lifts it)."""
    real = slack.upload_pdf
    attempts = []

    def upload(channel_id, path, title, thread_ts, comment):
        if only_channel is None or channel_id == only_channel:
            attempts.append(channel_id)
            raise exc
        return real(channel_id, path, title, thread_ts, comment)
    slack.upload_pdf = upload

    def lift():
        slack.upload_pdf = real
    return attempts, lift


NOTHING_SENT = [
    pytest.param(SlackNotCalled("slack_get_file_upload_url: the model did not call the tool"), id="the model made no call"),
    pytest.param(SlackUnavailable("slack_get_file_upload_url: Slack unavailable (claude exit 1)"), id="the run failed first"),
    pytest.param(SlackNotShared("upload of x.pdf failed: HTTPStatusError"), id="the bytes were not accepted"),
    pytest.param(SlackNotShared("upload URL result has 0 file id line(s) and 0 upload URL line(s)"), id="no usable URL"),
]
MAY_HAVE_SHARED = [
    pytest.param(SlackTimeout("slack_complete_file_upload: tool-call mismatch: no call seen (timeout after 180s)"),
                 id="a timeout"),
    pytest.param(SlackError("slack_complete_file_upload: tool-call mismatch: arguments differ in ['title']"),
                 id="a tool-call mismatch"),
    pytest.param(SlackError("slack_complete_file_upload: unexpected tool call: ['mcp__x__slack_send_message']"),
                 id="another tool was called"),
    pytest.param(SlackError("slack_complete_file_upload: channel_not_found"), id="a tool error"),
    pytest.param(RuntimeError("boom"), id="anything else"),
]


@pytest.mark.parametrize("exc", NOTHING_SENT)
def test_an_attach_that_sent_nothing_goes_to_late_and_says_how_to_try_again(slack, store, settings, tmp_path, exc,
                                                                            caplog):
    """The live incident: `attach` failed with SlackNotCalled (a complete stream, no call), the marker went to `failed`
    for good, and both `attach` and the Attach button refused ("already tried"). Now it goes to `late`."""
    from haseeb_agent.jobs.decks import attach_now
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    attempts, lift = refuse_uploads(slack, exc)
    caplog.set_level(logging.INFO, logger=decks_mod.log.name)
    decks(slack, store, settings, tmp_path)  # the deck job's own attach
    assert attempts == [CH] and channel_uploads(slack) == []
    assert "to channel posts" not in caplog.text  # nothing was attached, and the run does not say so
    assert store.get_cursor(f"attach:{item.id}") == "late"  # not `failed`, and not unset (the next tick would retry)
    note = slack.sent[-1]
    assert (note.channel_id, note.thread_ts, note.text) == (HASEEB, item.dm_ts, ATTACH_NOT_SENT)
    assert ATTACH_NOT_SENT == "⚠️ Deck didn't attach (nothing was sent). Use Attach, or reply attach, to try again"
    assert store.get_cursor(f"own:{note.ts}") == "1"
    decks(slack, store, settings, tmp_path)  # the next ticks do not try by themselves
    assert attempts == [CH] and slack.sent[-1] == note
    lift()
    assert attach_now(slack, store, settings, store.get_item(item.id), tmp_path / "decks") == "📎 Deck attached to the post"
    (up,) = channel_uploads(slack)
    assert up.thread_ts == store.get_cursor(f"post_ts:{item.id}") and store.get_cursor(f"attach:{item.id}") == "done"
    decks(slack, store, settings, tmp_path)
    assert len(channel_uploads(slack)) == 1  # exactly one deck in the thread


@pytest.mark.parametrize("exc", MAY_HAVE_SHARED)
def test_an_attach_that_may_have_shared_the_file_stays_failed(slack, store, settings, tmp_path, exc):
    """R16: a timeout, a tool-call mismatch, a tool error: the file may be in the thread, so it is never sent again."""
    from haseeb_agent.jobs.decks import attach_now
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    attempts, lift = refuse_uploads(slack, exc)
    decks(slack, store, settings, tmp_path)
    assert attempts == [CH] and store.get_cursor(f"attach:{item.id}") == "failed"
    note = slack.sent[-1]
    assert (note.channel_id, note.thread_ts, note.text) == (HASEEB, item.dm_ts, ATTACH_FAILED)
    lift()
    decks(slack, store, settings, tmp_path)
    with pytest.raises(ValueError, match="not retried"):
        attach_now(slack, store, settings, store.get_item(item.id), tmp_path / "decks")
    assert channel_uploads(slack) == [] and attempts == [CH]


@pytest.mark.parametrize("exc", NOTHING_SENT)
def test_an_attach_command_that_sent_nothing_keeps_the_late_marker_and_says_how_to_try_again(
        slack, store, settings, tmp_path, exc):
    from haseeb_agent.jobs.decks import attach_now
    item = late(slack, store, settings, tmp_path)
    attempts, lift = refuse_uploads(slack, exc)
    with pytest.raises(ValueError) as e:
        attach_now(slack, store, settings, item, tmp_path / "decks")
    assert str(e.value) == ATTACH_NOT_SENT_TEXT == "Deck didn't attach (nothing was sent). Use Attach, or reply attach, to try again"
    assert store.get_cursor(f"attach:{item.id}") == "late" and channel_uploads(slack) == []
    with pytest.raises(ValueError, match="nothing was sent"):  # and again: it never became "already tried"
        attach_now(slack, store, settings, item, tmp_path / "decks")
    assert attempts == [CH, CH]
    lift()
    assert attach_now(slack, store, settings, item, tmp_path / "decks") == "📎 Deck attached to the post"
    assert len(channel_uploads(slack)) == 1


@pytest.mark.parametrize("exc", MAY_HAVE_SHARED)
def test_an_attach_command_that_may_have_shared_the_file_is_not_retried(slack, store, settings, tmp_path, exc):
    from haseeb_agent.jobs.decks import attach_now
    item = late(slack, store, settings, tmp_path)
    refuse_uploads(slack, exc)
    with pytest.raises(ValueError, match="didn't attach"):
        attach_now(slack, store, settings, item, tmp_path / "decks")
    assert store.get_cursor(f"attach:{item.id}") == "failed"
    with pytest.raises(ValueError, match="not retried"):
        attach_now(slack, store, settings, item, tmp_path / "decks")


def test_a_pause_is_still_put_back_and_never_a_late_marker_or_a_note(slack, store, settings, tmp_path):
    """SlackPaused is a SlackUnavailable, but it keeps its own, older handling: the marker is as it was (unset, so the
    deck attaches by itself after Resume), and there is no note while every DM write is refused."""
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    post(slack, store, settings, item)
    store.mark(item.id, "posted")
    sent = len(slack.sent)
    attempts, lift = refuse_uploads(slack, SlackPaused("slack_get_file_upload_url: the agent is paused, nothing was sent"))
    decks(slack, store, settings, tmp_path)
    assert store.get_cursor(f"attach:{item.id}") is None and len(slack.sent) == sent
    lift()
    decks(slack, store, settings, tmp_path)
    assert len(channel_uploads(slack)) == 1 and store.get_cursor(f"attach:{item.id}") == "done"


def test_the_attach_reply_that_sent_nothing_is_answered_and_can_be_sent_again(slack, store, settings, tmp_path):
    """End to end through the DM: the reply says nothing was sent and how to try again, and his next `attach` works."""
    from haseeb_agent.commands import process_dm
    from haseeb_agent.models import Msg
    item = late(slack, store, settings, tmp_path)
    attempts, lift = refuse_uploads(slack, SlackNotCalled("slack_get_file_upload_url: the model did not call the tool"))
    first = f"{slack._next + 1}.000001"
    parent = slack.add_message(HASEEB, item.dm_ts, "card", user_id=HASEEB, reply_count=1, latest_reply=first)
    slack.add_thread(HASEEB, parent, [Msg(ts=first, user_id=HASEEB, user_name="Haseeb", text="attach",
                                          thread_ts=item.dm_ts, reply_count=0, latest_reply=None)])
    handlers = {"insight": InsightHandlers(slack, FakeLLM(), store, settings)}
    import haseeb_agent.jobs.decks as dm
    dm_root = dm.decks_root
    dm.decks_root = lambda s: tmp_path / "decks"
    try:
        assert process_dm(slack, store, settings, handlers, MONDAY) == 1
        assert slack.sent[-1].text == ATTACH_NOT_SENT and store.get_cursor(f"attach:{item.id}") == "late"
        assert store.get_item(item.id).status == "posted"
        lift()
        second = f"{slack._next + 1}.000001"
        parent.latest_reply = second
        slack.threads[(HASEEB, item.dm_ts)][1].append(Msg(ts=second, user_id=HASEEB, user_name="Haseeb", text="attach",
                                                          thread_ts=item.dm_ts, reply_count=0, latest_reply=None))
        assert process_dm(slack, store, settings, handlers, MONDAY) == 1
    finally:
        dm.decks_root = dm_root
    assert slack.sent[-1].text == "📎 Deck attached to the post"
    assert len(channel_uploads(slack)) == 1 and store.get_cursor(f"attach:{item.id}") == "done"


# -- the real gateway's failures, as the attach reads them (every step of the upload) ---------------------------------

def real_gateway(settings, store, *results, http=None):
    from haseeb_agent.slack_gateway import ClaudeConnectorSlack
    from tests.test_slack_gateway import FakeHttp, FakeRun
    run = FakeRun(*results)
    return ClaudeConnectorSlack(settings, run=run, http_post=http or FakeHttp(), store=store), run


@pytest.mark.parametrize("script,marker", [
    pytest.param(("NO_CALL",), "late", id="URL step: the model made no call"),
    pytest.param(("LOST",), "late", id="URL step: timed out (the URL tool shares nothing)"),
    pytest.param(("MISMATCH",), "late", id="URL step: a tool-call mismatch (it shares nothing)"),
    pytest.param(("GOT_URL", "HTTP500"), "late", id="the bytes were refused"),
    pytest.param(("GOT_URL", "NO_CALL"), "late", id="sharing step: the model made no call"),
    pytest.param(("GOT_URL", "OUTAGE"), "late", id="sharing step: the run failed before any call"),
    pytest.param(("GOT_URL", "LOST"), "failed", id="sharing step: timed out"),
    pytest.param(("GOT_URL", "MISMATCH_SHARE"), "failed", id="sharing step: a tool-call mismatch"),
    pytest.param(("GOT_URL", "TOOL_ERROR"), "failed", id="sharing step: a tool error"),
])
def test_an_attach_through_the_real_gateway_is_late_only_when_nothing_could_have_been_shared(
        slack, store, settings, tmp_path, script, marker):
    from haseeb_agent.jobs.decks import attach_now
    from tests import test_slack_gateway as g
    results = {"NO_CALL": g.NO_CALL, "LOST": g.LOST, "OUTAGE": g.OUTAGE, "GOT_URL": g.GOT_URL, "MISMATCH": g.MISMATCH,
               "MISMATCH_SHARE": g.BAD_SHARE, "HTTP500": None,
               "TOOL_ERROR": g.tool("slack_complete_file_upload", "channel_not_found", is_error=True)}
    item = late(slack, store, settings, tmp_path)

    def failing():
        http = g.FakeHttp(status=500) if "HTTP500" in script else g.FakeHttp()
        return real_gateway(settings, store, *[results[k] for k in script if results[k] is not None], http=http)
    gateway, run = failing()
    with pytest.raises(ValueError) as e:
        attach_now(gateway, store, settings, item, tmp_path / "decks")
    assert store.get_cursor(f"attach:{item.id}") == marker
    share = g.PREFIX + "slack_complete_file_upload"
    if marker == "late":
        assert str(e.value) == ATTACH_NOT_SENT_TEXT
        if script[-1] not in ("NO_CALL", "OUTAGE") or script[0] != "GOT_URL":  # it never got as far as the sharing step
            assert not any(share in c[1]["allowed_tools"] for c in run.calls)
        again, _ = failing()  # asked again: it is tried again, and told the same
        with pytest.raises(ValueError) as e2:
            attach_now(again, store, settings, item, tmp_path / "decks")
        assert str(e2.value) == ATTACH_NOT_SENT_TEXT and store.get_cursor(f"attach:{item.id}") == "late"
        assert channel_uploads(slack) == []
        assert attach_now(slack, store, settings, item, tmp_path / "decks") == "📎 Deck attached to the post"
        assert len(channel_uploads(slack)) == 1 and store.get_cursor(f"attach:{item.id}") == "done"
    else:
        assert any(share in c[1]["allowed_tools"] for c in run.calls)  # the sharing call was the one that failed
        assert "didn't attach" in str(e.value) and "Slack hiccup" not in str(e.value)
        with pytest.raises(ValueError, match="not retried"):
            attach_now(slack, store, settings, item, tmp_path / "decks")
        assert channel_uploads(slack) == []


# -- the deck of an older text is remembered while the new one is made (the dashboard shows it, labelled) -------------

def test_a_revised_text_starts_a_fresh_state_that_remembers_the_deck_of_the_older_text(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    old = json.loads(store.get_cursor(f"deck:{item.id}"))
    assert old["state"] == "done" and "prev" not in old
    llm = FakeLLM(writer=lambda user, after: insight_for(candidate_in(user), title="Topic alpha, revised"))
    InsightHandlers(slack, llm, store, settings).revise(store.get_item(item.id), "change it")
    meta, sha, st = decks_mod._dm_work(store, store.get_item(item.id))
    assert (st["state"], st["tries"], st["sha"]) == ("pending", 0, sha) and sha != old["sha"]
    assert st["prev"] == {"sha": old["sha"], "pdf": old["pdf"], "model": old["model"]}
    assert "dm_at" not in st and "pdf" not in st  # R50: the old deck's upload time does not count for the new text


def test_the_old_deck_is_carried_until_a_build_replaces_its_file_and_dropped_when_the_new_deck_is_done(
        slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    old = json.loads(store.get_cursor(f"deck:{item.id}"))
    writer = lambda title: FakeLLM(writer=lambda user, after: insight_for(candidate_in(user), title=title))  # noqa: E731
    handler = lambda llm: InsightHandlers(slack, llm, store, settings)  # noqa: E731
    handler(writer("Topic alpha, revised")).revise(store.get_item(item.id), "change it")
    b = Builder(fail=1)  # the first attempt at the new text fails: its state is written, and has no build yet
    decks(slack, store, settings, tmp_path, b)
    st = json.loads(store.get_cursor(f"deck:{item.id}"))
    assert st["state"] == "pending" and st["tries"] == 1 and st["prev"]["pdf"] == old["pdf"]
    handler(writer("Topic alpha, revised again")).revise(store.get_item(item.id), "again")  # a second edit meanwhile
    _, _, st2 = decks_mod._dm_work(store, store.get_item(item.id))
    assert st2["prev"] == st["prev"] and st2["tries"] == 0  # still the deck that is in his DM
    # a state whose build has replaced the file no longer names an old deck to carry
    assert decks_mod._previous({"state": "pending", "built": "x", "prev": st["prev"]}) is None
    assert decks_mod._previous({"state": "gave_up", "prev": st["prev"]}) == st["prev"]
    assert decks_mod._previous({"state": "done"}) is None  # a record without a PDF has nothing to show
    assert decks(slack, store, settings, tmp_path) == 1
    done = json.loads(store.get_cursor(f"deck:{item.id}"))
    assert done["state"] == "done" and "prev" not in done and done["dm_at"]


# -- ideas (schema v2): the deck job reads each item's draft in its own version ---------------------------------

from haseeb_agent.jobs.insights import draft_from  # noqa: E402
from haseeb_agent.render import MARKET_LABELS, MARKETS, Idea, Insight  # noqa: E402
from tests.test_insights import good_idea_meta, idea_for, stored  # noqa: E402


def delivered_ideas(slack, store, settings, n=1):
    """n delivered v2 ideas ("Topic alpha", ...), stored the way the job writes them now."""
    for i, name in enumerate(("alpha", "bravo", "charlie")[:n], 1):
        stored(store, good_idea_meta(i, name=name), "drafted", i, title=f"Topic {name}")
    return deliver_pending(slack, store, settings, MONDAY)


def test_decks_job_designs_an_idea_with_draft_from(slack, store, settings, tmp_path):
    (item,) = delivered_ideas(slack, store, settings)
    d = Designer()
    assert decks(slack, store, settings, tmp_path, designer=d) == 1
    ((draft, day, out_pdf, deck_id),) = d.calls
    assert isinstance(draft, Idea) and draft == draft_from(good_idea_meta())  # the designer receives the Idea
    assert (draft.title, draft.market, draft.price) == ("Topic alpha", "PK", "Rs 15k/month")
    assert (day, deck_id) == (item.day, item.id)
    (up,) = slack.uploads
    assert (up.channel_id, up.thread_ts, up.comment, up.title) == (DM, item.dm_ts, DECK_COMMENT, item.title)
    assert up.path.name == f"{item.id}.pdf" and b"designer" in up.path.read_bytes()
    assert store.get_item(item.id).models["deck"] == "codex gpt-6.1-sol"


def test_the_template_deck_of_an_idea_is_built_from_the_idea_with_its_market_on_the_cover(
        slack, store, settings, tmp_path):
    (item,) = delivered_ideas(slack, store, settings)
    b, c = Builder(), Cover()
    assert decks(slack, store, settings, tmp_path, b, c) == 1
    assert isinstance(b.calls[0][0], Idea) and b.calls[0][0].title == "Topic alpha"
    assert c.calls[0][0] == "Pakistan: Topic alpha"  # the market label and the title only
    assert b.calls[0][3] == c.calls[0][1]  # the codex image is the cover
    assert store.get_item(item.id).models["deck"] == "codex"


@pytest.mark.parametrize("market", MARKETS)
def test_the_cover_gets_the_ideas_market_label(slack, store, settings, tmp_path, market):
    meta = good_idea_meta()
    meta["insight"]["market"] = meta["candidate"]["market"] = market
    stored(store, meta, "drafted", 1, title="Topic alpha")
    deliver_pending(slack, store, settings, MONDAY)
    c = Cover()
    assert decks(slack, store, settings, tmp_path, cover=c) == 1
    assert c.calls[0][0] == f"{MARKET_LABELS[market]}: Topic alpha"


def test_an_idea_falls_back_to_the_template_deck_when_the_designer_fails(slack, store, settings, tmp_path):
    (item,) = delivered_ideas(slack, store, settings)
    b, c = Builder(), Cover()
    assert decks(slack, store, settings, tmp_path, b, c, designer=Designer("fact-check")) == 1
    assert isinstance(b.calls[0][0], Idea) and b.calls[0][3] is None and c.calls == []  # no second Codex run
    assert store.get_item(item.id).models["deck"] == "template (fact-check)"


def test_a_mixed_day_builds_each_item_from_its_own_version(slack, store, settings, tmp_path):
    stored_v1(store, "alpha", 1, status="drafted", batch="insight-2026-10-19-1")
    stored(store, good_idea_meta(2, batch="insight-2026-10-19-2", name="bravo"), "drafted", 2, title="Topic bravo")
    assert len(deliver_pending(slack, store, settings, MONDAY)) == 2
    b, c = Builder(), Cover()
    assert decks(slack, store, settings, tmp_path, b, c) == 2
    assert [type(call[0]) for call in b.calls] == [Insight, Idea]
    assert [topic for topic, _ in c.calls] == ["AI: Topic alpha", "Pakistan: Topic bravo"]


def test_deck_test_designs_an_idea_too(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import deck_test
    (item,) = delivered_ideas(slack, store, settings)
    d = Designer()
    res, uploaded = deck_test(slack, store, settings, item.id, designer=d, out_root=tmp_path / "decks")
    assert uploaded and isinstance(d.calls[0][0], Idea) and d.calls[0][3] == f"deck-test-{item.id}"
    (up,) = slack.uploads
    assert (up.channel_id, up.thread_ts) == (DM, item.dm_ts)


def test_a_revised_idea_gets_a_fresh_deck_and_keeps_the_cover(slack, store, settings, tmp_path):
    (item,) = delivered_ideas(slack, store, settings)
    b, c = Builder(), Cover()
    decks(slack, store, settings, tmp_path, b, c)
    llm = FakeLLM(writer=lambda user, after: idea_for(candidate_in(user), title="Topic alpha, revised"))
    InsightHandlers(slack, llm, store, settings).revise(store.get_item(item.id), "change it")
    assert current_deck(store, settings, store.get_item(item.id), tmp_path / "decks") is None  # stale now
    assert decks(slack, store, settings, tmp_path, b, c) == 1
    assert len(b.calls) == 2 and len(c.calls) == 1  # the Codex cover is reused
    assert isinstance(b.calls[1][0], Idea) and b.calls[1][0].title == "Topic alpha, revised"
    assert len(slack.uploads) == 2


def test_a_posted_ideas_deck_is_attached_in_the_posts_thread(slack, store, settings, tmp_path):
    (item,) = delivered_ideas(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    store.mark(item.id, "posted")
    post(slack, store, settings, item)
    decks(slack, store, settings, tmp_path)
    (up,) = channel_uploads(slack)
    assert (up.thread_ts, up.title, up.comment) == (top_post(slack).ts, item.title, None)
    assert store.get_cursor(f"attach:{item.id}") == "done"


# -- a post in another channel: its deck goes into that channel's thread (post-channel design, Task 2) -------------------

ELSEWHERE = "C093L3Y8UM6"  # #survive-ai


def post_to(slack, store, settings, item, channel_id=None):
    return InsightHandlers(slack, FakeLLM(), store, settings).post(store.get_item(item.id), channel_id=channel_id)


def uploads_to(slack, channel_id):
    return [u for u in slack.uploads if u.channel_id == channel_id]


def top_post_in(slack, channel_id):
    return next(s for s in slack.sent if s.channel_id == channel_id and s.thread_ts is None)


def test_the_deck_job_attaches_the_deck_in_the_channel_the_post_went_to(slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    store.mark(item.id, "posted")
    link = post_to(slack, store, settings, item, ELSEWHERE)
    assert f"/archives/{ELSEWHERE}/" in link
    decks(slack, store, settings, tmp_path)
    (up,) = uploads_to(slack, ELSEWHERE)
    assert (up.thread_ts, up.title, up.comment) == (top_post_in(slack, ELSEWHERE).ts, item.title, None)
    assert channel_uploads(slack) == []  # nothing into the leadership channel, which has no post of this item
    assert store.get_cursor(f"attach:{item.id}") == "done"
    decks(slack, store, settings, tmp_path)
    assert len(uploads_to(slack, ELSEWHERE)) == 1  # once


def test_attach_puts_the_dm_deck_into_the_channel_the_post_went_to(slack, store, settings, tmp_path):
    from haseeb_agent.jobs.decks import attach_now
    (item,) = delivered(slack, store, settings)
    post_to(slack, store, settings, item, ELSEWHERE)  # posted before the deck had reached the DM
    store.mark(item.id, "posted")
    decks(slack, store, settings, tmp_path)
    assert store.get_cursor(f"attach:{item.id}") == "late"
    assert attach_now(slack, store, settings, store.get_item(item.id), tmp_path / "decks") == "📎 Deck attached to the post"
    (up,) = uploads_to(slack, ELSEWHERE)
    assert up.thread_ts == top_post_in(slack, ELSEWHERE).ts and channel_uploads(slack) == []
    assert store.get_cursor(f"attach:{item.id}") == "done"


def test_a_post_made_before_the_choice_existed_still_gets_its_deck_in_the_leadership_channel(slack, store, settings, tmp_path):
    """Review focus 3: an old post has no `post_channel:` record, and the leadership channel is where it went."""
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    store.mark(item.id, "posted")
    post(slack, store, settings, item)
    store.delete_cursor(f"post_channel:{item.id}")
    decks(slack, store, settings, tmp_path)
    (up,) = channel_uploads(slack)
    assert up.thread_ts == top_post(slack).ts and store.get_cursor(f"attach:{item.id}") == "done"


def test_a_deck_attached_to_a_post_elsewhere_that_fails_is_not_retried_and_nothing_goes_to_the_leadership_channel(
        slack, store, settings, tmp_path):
    (item,) = delivered(slack, store, settings)
    decks(slack, store, settings, tmp_path)
    store.mark(item.id, "posted")
    post_to(slack, store, settings, item, ELSEWHERE)
    slack.fail_next("upload broke", method="upload_pdf")
    decks(slack, store, settings, tmp_path)
    decks(slack, store, settings, tmp_path)
    assert store.get_cursor(f"attach:{item.id}") == "failed"  # R16: never retried
    assert uploads_to(slack, ELSEWHERE) == [] and channel_uploads(slack) == []  # and never redirected to the leadership channel
