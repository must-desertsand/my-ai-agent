"""Each job records its named steps as text-free events, and the jobs behave as before."""
import logging
from datetime import datetime
from pathlib import Path
import sqlite3
from dataclasses import replace

import pytest

from haseeb_agent import cli
from haseeb_agent.jobs.decks import deliver_decks
from haseeb_agent.jobs.feedback import run_feedback
from haseeb_agent.store import Store
from tests.test_decks_job import Builder, Cover, delivered, settings  # noqa: F401 - decks on, no real Codex
from tests.test_feedback import FakeLLM as FeedbackLLM
from tests.test_feedback import NOW, post, ts_ago
from tests.test_insights import FakeLLM as InsightLLM
from tests.test_insights import FakeResearch, MONDAY, cand, research_ok, run, slack, store  # noqa: F401


def steps(store, job=None):
    return [(e.job, e.step, e.ok) for e in store.events_since(0) if job in (None, e.job)]


def test_feedback_steps(slack, store, settings):
    post(slack, ts_ago(600))
    assert run_feedback(slack, FeedbackLLM(), store, settings, NOW) == 1
    assert steps(store) == [("feedback", "read_channel", True), ("feedback", "write_draft", True),
                            ("feedback", "deliver", True)]
    assert store.events_since(0)[1].model == "writer-m"


def test_feedback_read_failure_is_an_event_and_still_returns_zero(slack, store, settings):
    slack.fail_next("boom secret", method="read_channel")
    assert run_feedback(slack, FeedbackLLM(), store, settings, NOW) == 0
    (e,) = store.events_since(0)
    assert (e.step, e.ok, e.kind) == ("read_channel", False, "SlackError")


def test_insights_steps(slack, store, settings):
    research = FakeResearch(research_ok([cand("alpha")]))
    items = run(slack, InsightLLM(), store, settings, research)
    assert len(items) == 1
    got = [s for _, s, _ in steps(store, "insights")]
    assert got[0] == "research" and {"write", "judge", "deliver"} <= set(got)
    assert all(ok for _, _, ok in steps(store, "insights"))
    by = {e.step: e for e in store.events_since(0)}
    assert by["research"].model == "sonnet" and by["write"].model == "writer-m" and by["judge"].model == "judge-m"


def test_insights_idle_tick_leaves_no_deliver_event(slack, store, settings):
    research = FakeResearch(research_ok([cand("alpha")]))
    run(slack, InsightLLM(), store, settings, research)
    before = len(store.events_since(0))
    run(slack, InsightLLM(), store, settings, research)  # already ran today, nothing left to send
    assert len(store.events_since(0)) == before


def test_decks_steps(slack, store, settings, tmp_path):
    delivered(slack, store, settings)
    assert deliver_decks(slack, store, settings, MONDAY, builder=Builder(), cover_maker=Cover(),
                         designer=None, out_root=tmp_path / "decks") == 1
    assert [s for _, s, _ in steps(store, "decks")] == ["design", "render", "upload"]


def test_decks_designer_step_carries_the_model(slack, store, settings, tmp_path):
    from types import SimpleNamespace

    def designer(ins, day, out_pdf, *, deck_id, keep=False):
        Path(out_pdf).write_bytes(b"%PDF-1.4")
        return SimpleNamespace(pdf=out_pdf, model="gpt-x", seconds=1.0, pages=3, images=1, repaired=False, reason="")

    delivered(slack, store, settings)
    assert deliver_decks(slack, store, settings, MONDAY, builder=Builder(), cover_maker=Cover(), designer=designer,
                         out_root=tmp_path / "decks") == 1
    evs = [(e.step, e.model) for e in store.events_since(0) if e.job == "decks"]
    assert evs == [("design", "gpt-x"), ("upload", None)]


def test_a_failed_deck_step_is_an_event_without_text(slack, store, settings, tmp_path):
    delivered(slack, store, settings)
    deliver_decks(slack, store, settings, MONDAY, builder=Builder(fail=5), cover_maker=Cover(), designer=None,
                  out_root=tmp_path / "decks")
    bad = [e for e in store.events_since(0) if e.ok is False]
    assert [(e.step, e.kind) for e in bad] == [("render", "DeckError")]


def test_process_dm_steps(slack, store, settings):
    from haseeb_agent.commands import process_dm
    from haseeb_agent.models import Item
    t = ts_ago(300)
    slack.add_message(settings.self_dm_id, t, "draft", user_id=settings.haseeb_id, reply_count=1,
                      latest_reply=ts_ago(100))
    store.upsert_item(Item(
        id="feedback-x", kind="feedback", day=NOW.date(), status="delivered", body="b", title="t", dm_ts=t,
        source_ts=None, score=None, models={}, attempts=0, edited=False, created_at=NOW, acted_at=None))
    slack.add_thread(settings.self_dm_id, slack.pages[settings.self_dm_id][0], [])
    process_dm(slack, store, settings, {}, NOW)
    assert [s for _, s, _ in steps(store, "process_dm")] == ["read_dm", "command"]


class TestBuildRuntimeWal:
    @pytest.fixture
    def cfg(self, settings, tmp_path):
        return replace(settings, db_path=str(tmp_path / "data" / "agent.sqlite"))

    def test_switches_to_wal_once_with_a_backup(self, cfg, tmp_path):
        rt = cli.build_runtime(cfg)
        assert rt.store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        backups = list((tmp_path / "data").glob("agent.sqlite.bak-*"))
        assert len(backups) == 1
        cli.build_runtime(cfg)  # the next tick: already WAL, no second backup
        assert len(list((tmp_path / "data").glob("agent.sqlite.bak-*"))) == 1

    @pytest.mark.parametrize("exc", [sqlite3.DatabaseError("x"), OSError("disk"), RuntimeError("y")])
    def test_any_switch_error_is_swallowed(self, cfg, monkeypatch, exc):
        monkeypatch.setattr(Store, "enable_wal", lambda self, d, n: (_ for _ in ()).throw(exc))
        assert cli.build_runtime(cfg).store is not None

    def test_a_failed_switch_removes_its_backup(self, tmp_path, monkeypatch):
        s = Store(tmp_path / "a.sqlite")
        real = s._db

        class Proxy:
            def __getattr__(self, n):
                return getattr(real, n)

            def execute(self, sql, *a):
                if "journal_mode=wal" in sql:
                    raise sqlite3.OperationalError("locked")
                return real.execute(sql, *a)

        s._db = Proxy()
        for _ in range(3):
            with pytest.raises(sqlite3.OperationalError):
                s.enable_wal(tmp_path / "bak", datetime(2026, 10, 19, 1, 2, 3))
        assert list((tmp_path / "bak").iterdir()) == []

    def test_a_failed_switch_never_fails_the_tick(self, cfg, monkeypatch, caplog):
        def locked(self, backup_dir, now):
            raise sqlite3.OperationalError("database is locked: secret draft text")

        monkeypatch.setattr(Store, "enable_wal", locked)
        with caplog.at_level(logging.WARNING, logger="haseeb_agent.cli"):
            rt = cli.build_runtime(cfg)
        assert rt.store is not None
        assert "secret draft text" not in caplog.text and "OperationalError" in caplog.text
