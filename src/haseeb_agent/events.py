"""Step events for the dashboard: when a job step started, ended, whether it worked, and the kind of its failure.

Text-free by design (the M6 rule): an event holds job, step, model, timing, ok and kind only. The kind is the
exception's class name, never its message, because a message can carry a draft or a teammate's post.

Recording is best effort. A database error here is logged (type only) and swallowed, and the job's own exception
is re-raised unchanged, so a step behaves exactly as it would without the wrapper.

A step also says who the model calls inside it are for (usage.py): their rows in `model_usage` carry this job and step.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from . import usage
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class StepHandle:
    """Yielded by `step`. A step that only learns its model while it runs sets `.model` before it ends."""
    model: str | None = None


def _now() -> datetime:
    return datetime.now().astimezone()


def error_kind(e: BaseException) -> str:
    """The exception's class name (at most 80 chars, nothing after a ':'), never its message."""
    return type(e).__name__.split(":", 1)[0].strip()[:80]


@contextmanager
def step(store: Store | None, job: str, step: str, model: str | None = None) -> Iterator[StepHandle]:
    """Record one job step as an event. Without a store the step just runs."""
    handle = StepHandle(model)
    event_id: int | None = None
    if store is not None:
        try:
            event_id = store.add_event(job, step, model=model, started=_now(), ended=None, ok=None, kind=None)
        except Exception as e:  # noqa: BLE001 - the job must run whatever the database does
            log.warning("events: cannot record the start of %s/%s (%s)", job, step, type(e).__name__)
    with usage.scoped(job, step, store=store):
        try:
            yield handle
        except BaseException as e:
            _finish(store, event_id, job, step, False, error_kind(e), handle.model)
            raise
        _finish(store, event_id, job, step, True, None, handle.model)


def _finish(store: Store | None, event_id: int | None, job: str, step: str, ok: bool, kind: str | None,
            model: str | None) -> None:
    if store is None or event_id is None:
        return
    try:
        store.finish_event(event_id, _now(), ok, kind, model)
    except Exception as e:  # noqa: BLE001 - see step()
        log.warning("events: cannot record the end of %s/%s (%s)", job, step, type(e).__name__)
