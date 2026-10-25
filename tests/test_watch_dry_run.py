"""`haseeb-agent watch --dry-run` (2026-10-21, review M6): the thread watch's scan run for real against Slack, with no draft, no
DM, no reaction, no writing or sorting model and no write to the store by the watch (its Slack reads are real: headless Haiku
runs on the shared quota, and the gateway may write `new_tools_alert:` as on any read), printing only the replies the watch would draft for or sort (thread
ts, reply ts, kind, author id) and never a word of any message. The coordinator runs it once live after deploy, to see that the
two real threads (Iqbal's and Zakir's, both answered by Haseeb by hand) have nothing eligible.

Same fakes as test_followback.py: a fake Slack with threads. The model is None here: a call to it would fail the test."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from haseeb_agent import controls
from haseeb_agent.cli import main
from haseeb_agent.jobs import followback as fbk
from haseeb_agent.models import Item
from haseeb_agent.slack_gateway import SlackError
from haseeb_agent.store import Store
from tests.test_cli import Clock, fake_runtime, repo, repo_store  # noqa: F401
from tests.test_feedback import CH, HASEEB, IQBAL, store  # noqa: F401
from tests.test_feedback import settings as feedback_settings  # noqa: F401
from tests.test_followback import LoggingSlack, iqbal_thread
from tests.test_insight_threads import reply_of, zakir_thread
from tests.thread_support import (HASEEB_ANSWER, HASEEB_FOLLOWBACK, IQBAL_POST, IQBAL_REPLY, MASTERCARD, NOW, ZAKIR, ZAKIR_REPLY,
                                  ZAKIR_TEXT, make_thread, msg, posted_feedback)


@pytest.fixture
def settings(feedback_settings, tmp_path):
    """The feedback tests' settings with the database inside this test's own folder, so that the screenshots of a site review (kept
    next to the database) are not written under the repository's data/ folder."""
    return replace(feedback_settings, db_path=str(tmp_path / "agent.sqlite"))


@pytest.fixture
def slack():
    return LoggingSlack()


def snapshot(store: Store) -> dict:
    """Every row of every table: two snapshots are equal only if nothing was written in between."""
    db = store._db
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: sorted(repr(tuple(r)) for r in db.execute(f"SELECT * FROM {t}")) for t in tables}


def look(slack, store, settings, now=NOW):
    return fbk.preview(slack, store, settings, now)


def both_threads(slack, store):
    """Iqbal's thread with his reply, and Zakir's insight thread with his update, both unanswered."""
    iqbal_thread(slack, store)
    zakir_thread(slack, store, [reply_of(ZAKIR_REPLY, ZAKIR_TEXT)])


# ---------------------------------------------------------------- the preview

def test_the_preview_lists_the_replies_the_watch_would_draft_for_or_sort_and_writes_nothing(slack, store, settings):
    both_threads(slack, store)
    before = snapshot(store)
    out = look(slack, store, settings)
    assert sorted((c.thread_ts, c.reply_ts, c.kind, c.author_id) for c in out.candidates) == sorted([
        (IQBAL_POST, IQBAL_REPLY, "followback", IQBAL), (MASTERCARD, ZAKIR_REPLY, "insight", ZAKIR)])
    assert (out.watched, out.scanned, out.with_news, len(out.read), out.not_read, out.page_failed) == (2, 2, 2, 2, 0, False)
    assert snapshot(store) == before  # not a cursor, not an event, not a message of the history
    assert slack.sent == [] and slack.reactions == [] and slack.uploads == []
    assert [k for k, _ in slack.read_log].count("channel") == 1 and [k for k, _ in slack.read_log].count("thread") == 2


def test_a_follow_back_thread_lists_the_one_reply_that_would_be_answered_not_its_context(slack, store, settings):
    iqbal_thread(slack, store, extra=[msg("1792581300.000100", IQBAL, "Iqbal Qazi", "One more thing: the export button is gone.", IQBAL_POST)])
    out = look(slack, store, settings)
    assert [(c.reply_ts, c.kind) for c in out.candidates] == [("1792581300.000100", "followback")]


def test_an_insight_thread_lists_every_reply_the_sorter_would_look_at(slack, store, settings):
    zakir_thread(slack, store, [reply_of(ZAKIR_REPLY, ZAKIR_TEXT), reply_of("1792581100.000100", "A question about the 12 leads?", user="U06FH6HRG1Z")])
    out = look(slack, store, settings)
    assert [(c.reply_ts, c.author_id) for c in out.candidates] == [(ZAKIR_REPLY, ZAKIR), ("1792581100.000100", "U06FH6HRG1Z")]


def test_the_two_real_threads_show_nothing_eligible_and_say_that_they_were_read(slack, store, settings):
    """Iqbal's thread has Haseeb's follow-back after his reply, Zakir's has Haseeb's answer after the update."""
    iqbal_thread(slack, store, extra=[msg(HASEEB_FOLLOWBACK, HASEEB, "Haseeb Zafar", "Fixed, thanks. Yes to the timer.", IQBAL_POST)])
    zakir_thread(slack, store, [reply_of(ZAKIR_REPLY, ZAKIR_TEXT),
                                msg(HASEEB_ANSWER, HASEEB, "Haseeb Zafar", "Good update. Repost it at the top level.", MASTERCARD)])
    out = look(slack, store, settings)
    assert out.candidates == [] and sorted(out.read) == sorted([IQBAL_POST, MASTERCARD])


def test_the_preview_ignores_the_rest_and_the_seen_marks_that_the_live_watch_keeps(slack, store, settings):
    both_threads(slack, store)
    store.set_cursor(fbk.SCAN_AT, NOW.isoformat())
    store.set_cursor(f"followback_seen:{IQBAL_POST}", str(int(float(IQBAL_REPLY))))
    store.set_cursor(f"followback_seen:{MASTERCARD}", str(int(float(ZAKIR_REPLY))))
    assert len(look(slack, store, settings).candidates) == 2  # (the next run would read neither thread)


def test_a_reply_that_has_a_draft_or_was_finished_is_not_listed(slack, store, settings):
    both_threads(slack, store)
    store.set_cursor(f"{fbk.DONE_PREFIX}{IQBAL_REPLY}", "other")
    store.set_cursor(f"{fbk.DONE_PREFIX}{ZAKIR_REPLY}", "capped")
    assert look(slack, store, settings).candidates == []


@pytest.mark.parametrize("text", ["👍", "ok", "Thanks a lot Haseeb, I will fix these today."])
def test_a_reply_with_nothing_to_answer_is_not_listed_in_a_follow_back_thread(slack, store, settings, text):
    iqbal_thread(slack, store, reply_text=text)
    assert look(slack, store, settings).candidates == []


def test_a_full_thread_is_not_watched_so_not_listed(slack, store, settings):
    iqbal_thread(slack, store)
    for n in (1, 2):
        store.upsert_item(Item(id=f"followback-17925800{n}0.000100", kind="feedback", day=NOW.date(), status="skipped", body="x",
                               title="t", dm_ts=None, source_ts=IQBAL_POST, score=None, models={}, attempts=0, edited=False,
                               created_at=NOW, acted_at=None))
    out = look(slack, store, settings)
    assert out.watched == 0 and out.candidates == [] and slack.read_log == []


def test_nothing_is_read_when_nothing_is_watched(slack, store, settings):
    out = look(slack, store, settings)
    assert (out.watched, out.candidates, out.page_failed) == (0, [], False) and slack.read_log == []


def test_a_channel_that_cannot_be_read_is_reported_not_raised(slack, store, settings):
    iqbal_thread(slack, store)
    slack.fail_next("down", method="read_channel")
    assert look(slack, store, settings).page_failed is True


def test_a_thread_that_cannot_be_read_is_counted_as_not_read(slack, store, settings):
    both_threads(slack, store)
    slack.fail_next("boom", method="read_thread")
    out = look(slack, store, settings)
    assert out.not_read == 1 and len(out.read) == 1 and len(out.candidates) == 1


def test_at_most_fifteen_threads_are_read(slack, store, settings):
    for n in range(17):
        t = f"17925{70000 + n}.000100"
        posted_feedback(store, t, name=f"T{n}")
        make_thread(slack, t, [msg(f"17925{75000 + n}.000100", HASEEB, "Haseeb", "✅ x", t),
                               msg(f"17925{76000 + n}.000100", f"U0MATE00{n:02d}", f"T{n}", "I fixed the layout and the badges, look again.", t)],
                    author=f"U0MATE00{n:02d}", name=f"T{n}")
    out = look(slack, store, settings)
    assert len(out.read) == fbk.PREVIEW_MAX_READS == 15 and out.not_read == 2 and len(out.candidates) == 15


def test_a_reply_older_than_two_days_is_not_looked_at(slack, store, settings):
    iqbal_thread(slack, store)
    out = look(slack, store, settings, NOW + timedelta(days=3))
    assert out.with_news == 0 and out.candidates == [] and [k for k, _ in slack.read_log] == ["channel"]


# ---------------------------------------------------------------- the command

def seeded(repo, seed):  # noqa: F811
    st = Store(repo / "data" / "agent.sqlite")
    slack = LoggingSlack()
    seed(slack, st)
    st.close()
    return slack


def test_the_command_prints_one_line_for_each_eligible_reply_and_never_a_word_of_a_message(repo, capsys):  # noqa: F811
    slack = seeded(repo, both_threads)
    assert main(["watch", "--dry-run"], runtime_factory=fake_runtime(slack), clock=Clock(NOW)) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "nothing is drafted, sent or reacted to, and no text is printed" in lines[0]
    assert "it reads Slack for real (up to 16 headless Haiku runs, on the shared quota)" in lines[0]  # not "no model call"
    assert f"eligible thread={IQBAL_POST} reply={IQBAL_REPLY} kind=followback author={IQBAL}" in lines
    assert f"eligible thread={MASTERCARD} reply={ZAKIR_REPLY} kind=insight author={ZAKIR}" in lines
    assert "2 thread(s) watched, 2 in the scan, 2 with a reply of the last 2 days, 2 read (" in lines[-1] and lines[-1].endswith("2 eligible")
    for word in ("Fixed all four", "Iqbal", "Zakir", "Mastercard", "lead-sourcing", "<@"):
        assert word not in out, word  # not a word of any message, not a name
    assert slack.sent == [] and slack.reactions == []


def test_the_command_changes_nothing_in_the_store(repo, capsys):  # noqa: F811
    slack = seeded(repo, both_threads)
    st = repo_store(repo)
    before = snapshot(st)
    st.close()
    assert main(["watch", "--dry-run"], runtime_factory=fake_runtime(slack), clock=Clock(NOW)) == 0
    st = repo_store(repo)
    assert snapshot(st) == before
    st.close()


def test_the_command_runs_while_paused_and_says_so_nowhere_else(repo, capsys):  # noqa: F811
    slack = seeded(repo, both_threads)
    st = repo_store(repo)
    controls.set_paused(st, True, NOW - timedelta(hours=1))
    st.close()
    assert main(["watch", "--dry-run"], runtime_factory=fake_runtime(slack), clock=Clock(NOW)) == 0
    assert "2 eligible" in capsys.readouterr().out


def test_the_command_with_nothing_eligible_says_so(repo, capsys):  # noqa: F811
    slack = seeded(repo, lambda s, st: iqbal_thread(s, st, extra=[msg(HASEEB_FOLLOWBACK, HASEEB, "Haseeb Zafar", "Fixed.", IQBAL_POST)]))
    assert main(["watch", "--dry-run"], runtime_factory=fake_runtime(slack), clock=Clock(NOW)) == 0
    out = capsys.readouterr().out
    assert "eligible thread=" not in out and out.splitlines()[-1].endswith("0 eligible")


def test_the_command_fails_when_the_channel_cannot_be_read(repo, capsys):  # noqa: F811
    slack = seeded(repo, both_threads)
    slack.fail_next("down", method="read_channel")
    assert main(["watch", "--dry-run"], runtime_factory=fake_runtime(slack), clock=Clock(NOW)) == 1
    assert "the channel could not be read" in capsys.readouterr().err


def test_the_command_fails_when_the_runtime_cannot_be_built(repo, capsys):  # noqa: F811
    def broken(settings):
        raise SlackError("no connector")
    assert main(["watch", "--dry-run"], runtime_factory=broken, clock=Clock(NOW)) == 1
    assert "watch dry-run failed" in capsys.readouterr().err


def test_the_help_and_the_first_line_say_what_the_slack_reads_are_not_that_no_model_runs(repo, capsys):  # noqa: F811
    """Fix round 1 re-review: the reads are headless Haiku runs on the shared quota (the channel and up to 15 threads), so it is not
    true that "no model" runs. The writing and sorting models are not asked, and nothing is drafted."""
    with pytest.raises(SystemExit) as e:
        main(["watch", "--help"], runtime_factory=fake_runtime(LoggingSlack()), clock=Clock(NOW))
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps at $COLUMNS
    assert e.value.code == 0 and "headless Haiku runs" in out and "shared quota" in out and "model call" not in out
    assert fbk.PREVIEW_MAX_READS + 1 == 16 and "16 headless Haiku runs" in out


def test_dry_run_is_the_only_mode(repo, capsys):  # noqa: F811
    with pytest.raises(SystemExit) as e:
        main(["watch"], runtime_factory=fake_runtime(LoggingSlack()), clock=Clock(NOW))
    assert e.value.code == 2 and "--dry-run" in capsys.readouterr().err
