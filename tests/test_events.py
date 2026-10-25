"""events.step: text-free step events that never change what the job does."""
import sqlite3

import pytest

from haseeb_agent import events
from haseeb_agent.slack_gateway import SlackUnavailable
from haseeb_agent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agent.sqlite")
    yield s
    s.close()


def test_step_records_ok_and_fail(store):
    with events.step(store, "feedback", "read_channel", "m1"):
        pass
    with pytest.raises(SlackUnavailable):
        with events.step(store, "feedback", "deliver"):
            raise SlackUnavailable("send: Slack unavailable")
    ok, bad = store.events_since(0)
    assert (ok.job, ok.step, ok.model, ok.ok, ok.kind) == ("feedback", "read_channel", "m1", True, None)
    assert ok.ended is not None and ok.ended >= ok.started
    assert (bad.step, bad.ok, bad.kind) == ("deliver", False, "SlackUnavailable")
    assert bad.ended is not None


def test_step_exception_propagates_unchanged(store):
    err = ValueError("boom")
    with pytest.raises(ValueError) as caught:
        with events.step(store, "j", "s"):
            raise err
    assert caught.value is err


def test_step_model_can_be_set_while_running(store):
    with events.step(store, "j", "write") as ev:
        ev.model = "writer-m"
    assert store.events_since(0)[0].model == "writer-m"
    with pytest.raises(RuntimeError):
        with events.step(store, "j", "write", "first") as ev:
            ev.model = "second"
            raise RuntimeError
    assert store.events_since(0)[1].model == "second"


def test_step_survives_db_error(store, caplog):
    class Broken:
        def add_event(self, *a, **k):
            raise sqlite3.OperationalError("database is locked: secret draft text")

        def finish_event(self, *a, **k):
            raise AssertionError("no event was opened, so none can be closed")

    ran = []
    with events.step(Broken(), "j", "s"):
        ran.append(1)
    assert ran == [1]
    assert "secret draft text" not in caplog.text and "OperationalError" in caplog.text


def test_step_survives_db_error_when_closing(store, caplog):
    class BrokenEnd:
        def add_event(self, *a, **k):
            return 1

        def finish_event(self, *a, **k):
            raise sqlite3.OperationalError("disk full: secret draft text")

    with pytest.raises(KeyError):  # the job's own error still wins
        with events.step(BrokenEnd(), "j", "s"):
            raise KeyError("x")
    assert "secret draft text" not in caplog.text


def test_step_without_a_store_just_runs():
    with events.step(None, "j", "s") as ev:
        ev.model = "m"


def test_event_has_no_text(store):
    with pytest.raises(RuntimeError):
        with events.step(store, "feedback", "write_draft"):
            raise RuntimeError("secret draft text: the whole post")
    rows = store._db.execute("SELECT * FROM events").fetchall()
    assert len(rows) == 1
    assert "secret draft text" not in " ".join(str(v) for v in tuple(rows[0]))
    assert rows[0]["kind"] == "RuntimeError"


def test_kind_is_the_class_name_capped_at_80():
    class E(Exception):
        pass

    E.__name__ = "X" * 200
    assert events.error_kind(E("a: b")) == "X" * 80
    assert events.error_kind(ValueError("x: y")) == "ValueError"
