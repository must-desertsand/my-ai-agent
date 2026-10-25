"""Model usage: what each model call used and, where anyone knows it, what it cost.

Every call to a model leaves one row in `model_usage` (store.py): the provider, the model, the job and step it ran for,
the token counts, a cost with the basis it rests on, the wall time and whether it worked. A row never holds text (the M6
rule): no prompt, no answer, no page. The Usage tab reads these rows (dashboard/api.py `/api/usage`); `usage-backfill`
(usage_backfill.py) adds the history from before this existed. The design is in
docs/superpowers/specs/2026-10-21-usage-tab-design.md.

Where it is recorded: only at the lowest shared call paths, so a new caller is covered without knowing about this module:
`claude_cli.run_claude`, `LLM._post` (every LiteLLM request, embeddings too) and the two Codex runners in deck/.

Who the call was for comes from a context variable, never from an argument: the job entry points bind a session (the
database and the job), `events.step` binds the job and step of the block it wraps, and `LLM.complete` binds its model
step. Recording is best effort: a database error is logged by its type and swallowed, and nothing here can change what
the job does or how it ends. It is one INSERT, done after the call.

Token counts are normalised so the columns add up across providers (see models.UsageRow): `input_tokens` is the input
the model read fresh, the cached part is `cache_read_tokens`, the part written to the cache `cache_write_tokens`. Claude
reports it that way already; OpenAI-style usage (LiteLLM, Codex) counts the cached tokens inside the prompt, so they are
taken out of it here.

Costs are never invented. Claude's is the CLI's list-price figure (the runs are paid from the Max subscription's quota,
not per call), LiteLLM's is the proxy's own `x-litellm-response-cost` header when it sends one, and Codex runs on a
ChatGPT Pro plan with no per-call price. There is no price table.
"""
from __future__ import annotations

import functools
import json
import logging
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (  # noqa: F401 - DAILY_TOTAL_STEP is part of this module's surface
    DAILY_TOTAL_STEP, UNKNOWN_JOB, USAGE_IDENT, USAGE_MAX_COST, USAGE_MAX_COUNT, UsageRow)
from .offline import OfflineError

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

# The jobs a row can name. A job this module has never heard of keeps its own (well-formed) name instead of becoming
# "other", so a new job shows up under its own name without anyone touching this file.
JOBS: tuple[str, ...] = ("insights", "feedback", "todo", "decks", "slack", "site_review", "compose", "history", "weekly",
                         "deck_test", "other", UNKNOWN_JOB)  # `unknown`: a backfilled run whose job the transcript cannot say
# The names jobs run under elsewhere (events.step, the tick, the run controls) -> the job a row names. None: no job.
_JOB_ALIASES: dict[str, str | None] = {
    "history_import": "history", "history_refresh": "history", "history_enrich": "history", "enrich": "history",
    "process_dm": "slack", "process_page": "slack", "slack_check": "slack", "smoke": "slack",
    "deliver_pending": "insights", "run_todo": "todo", "run_feedback": "feedback",
    "tick": None,  # the tick is a round of other jobs: each of them binds its own
}
# A step that is a job of its own, whichever job's step it runs inside: the site review looks at a teammate's site inside the
# feedback job's step, and its vision calls are the `site_review` job (listed in JOBS).
_STEP_JOBS: dict[str, str] = {"site_review": "site_review"}
_IDENT = USAGE_IDENT   # the one rule for a model, job, step or ref (models.py); the store checks it again on insert
# A usage row may wait this long for a lock another connection holds, and not a millisecond more (review M7): after one
# that does not come, rows are dropped (and counted) for a while instead of asking again, so a stuck writer costs a job
# a fraction of a second once, not every call.
LOCK_MS = 200
LOCK_PAUSE_S = 30.0
_lock = {"until": 0.0, "dropped": 0}   # monotonic time recording resumes at, and rows dropped since it last worked
SLACK_TOOL_PREFIX = "mcp__claude_ai_Slack__"
# What `usage-backfill` leaves for the Usage tab (cursors): how each import ended.
CLAUDE_CURSOR = "usage_backfill:claude"
LITELLM_CURSOR = "usage_backfill:litellm"


def canonical_job(name: object) -> str | None:
    """The job a row names for `name`, or None when `name` says no job (nothing, the tick, or a malformed name)."""
    if not isinstance(name, str) or not name.strip():
        return None
    key = name.strip().lower().replace("-", "_")
    if key in _JOB_ALIASES:
        return _JOB_ALIASES[key]
    return key if _IDENT.fullmatch(key) else None


# --------------------------------------------------------------------------
# Who a call is for
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Ctx:
    db_path: str | None = None   # the database a command works on: rows are written there on a connection of their own
    store: Store | None = None   # an open Store the code under `events.step` already holds; used before `db_path`
    job: str | None = None
    step: str | None = None


_CTX: ContextVar[_Ctx] = ContextVar("usage_ctx", default=_Ctx())


@contextmanager
def session(db_path: str | Path | None, job: str | None = None) -> Iterator[None]:
    """A job command's whole run (cli.run_recorded and the commands outside it): calls made in it are recorded in the
    database at `db_path`, for `job`, even when no step wraps them. The connection is opened per row and closed again."""
    token = _CTX.set(_Ctx(db_path=str(db_path) if db_path else None, job=job, step=None))
    try:
        yield
    finally:
        _CTX.reset(token)


@contextmanager
def scoped(job: str | None = None, step: str | None = None, *, store: Store | None = None) -> Iterator[None]:
    """Narrow who a call is for. Giving a `job` starts a new job (its step is `step`, else none); giving only a `step`
    keeps the job. A `store` is used for the rows of the block, ahead of the session's database."""
    cur = _CTX.get()
    new = _Ctx(db_path=cur.db_path, store=store if store is not None else cur.store,
               job=job or cur.job, step=step or (None if job else cur.step))
    token = _CTX.set(new)
    try:
        yield
    finally:
        _CTX.reset(token)


def stepped(step: str | None = None) -> Callable:
    """A decorator for a method: it runs with this usage step. With no name, the step is the method's first argument after
    `self` (`LLM.complete`'s model step: insight_writer, judge, ...)."""
    def wrap(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def inner(self, *args, **kw):
            name = step if step is not None else (args[0] if args else kw.get("step"))
            with scoped(step=name if isinstance(name, str) else None):
                return fn(self, *args, **kw)
        return inner
    return wrap


# --------------------------------------------------------------------------
# Cleaning what goes into a row
# --------------------------------------------------------------------------

def count_of(x: object) -> int | None:
    """A token count or a wall time: a whole number of zero or more, else None, and never above USAGE_MAX_COUNT (a larger
    one is clamped: it could not be stored, and it is no real count). A bool is not a count; neither is a string that is not
    plain ASCII digits ("²" passes str.isdigit() and fails int()); nothing here raises."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        if isinstance(x, int):
            v = x
        elif isinstance(x, float):
            v = int(x) if math.isfinite(x) else -1
        elif isinstance(x, str) and x.strip().isascii() and x.strip().isdecimal():
            v = int(x.strip())
        else:
            return None
    except (ValueError, OverflowError):  # a digit string past Python's int limit, a float too big to be one
        return None
    return min(v, USAGE_MAX_COUNT) if v >= 0 else None


def money_of(x: object) -> float | None:
    """A cost in dollars: a finite number of zero or more (and not absurd: above USAGE_MAX_COST is none), else None."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) and 0 <= v <= USAGE_MAX_COST else None


def model_of(x: object, default: str = "unknown") -> str:
    """A model id for a row: the identifier rule, after a suffix such as Claude's "[1m]" (a context-size variant) or a "+" is
    turned into a hyphen, which the rule has room for ("opus[1m]" is "opus-1m"). Anything else that does not fit is `default`."""
    s = re.sub(r"[\[\]+]+", "-", x.strip()).strip("-") if isinstance(x, str) else ""
    return s if _IDENT.fullmatch(s) else default


def name_of(x: object) -> str | None:
    s = x.strip() if isinstance(x, str) else ""
    return s if _IDENT.fullmatch(s) else None


def ref_of(x: object) -> str | None:
    s = x.strip() if isinstance(x, str) else ""
    return s if _IDENT.fullmatch(s) else None


def _delta(x: int | None, *parts: int | None) -> int | None:
    """`x` less the parts of it that are counted apart (never below zero); None when `x` is not known."""
    return None if x is None else max(0, x - sum(p or 0 for p in parts))


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

def record(provider: str, model: object, *, input_tokens: int | None = None, output_tokens: int | None = None,
           cache_read_tokens: int | None = None, cache_write_tokens: int | None = None, cost_usd: float | None = None,
           cost_basis: str = "unknown", latency_ms: int | None = None, ok: bool = True, step: str | None = None,
           job: str | None = None, default_job: str = "other", ref: str | None = None) -> None:
    """Add one row for a call that has just ended. Never raises an Exception: any trouble (no database to write to, a
    locked file, a bad value) is logged by type and the call goes on as if nothing had happened. `step` and `job` given
    here win over the context's; without a job anywhere the row names `default_job`."""
    try:
        ctx = _CTX.get()
        if ctx.store is None and not ctx.db_path:
            return  # outside any job (a test, a one-off script): nothing to write to
        wall = count_of(latency_ms)  # a bad figure is no figure: it must not move `at` either
        began = datetime.now().astimezone() - timedelta(milliseconds=wall or 0)  # `at` is when the call began
        if time.monotonic() < _lock["until"]:  # the database was locked a moment ago: this row is dropped, and counted
            _lock["dropped"] += 1
            return
        cost = money_of(cost_usd)
        step_name = name_of(step) or name_of(ctx.step) or "other"
        row = UsageRow(
            at=began, provider=provider, model=model_of(model),
            job=_STEP_JOBS.get(step_name) or canonical_job(job) or canonical_job(ctx.job) or default_job,
            step=step_name,
            input_tokens=count_of(input_tokens), output_tokens=count_of(output_tokens),
            cache_read_tokens=count_of(cache_read_tokens), cache_write_tokens=count_of(cache_write_tokens),
            cost_usd=cost, cost_basis="unknown" if cost is None and cost_basis in ("list", "proxy") else cost_basis,
            latency_ms=wall, ok=bool(ok), source="live", ref=ref_of(ref),
        )
        if ctx.store is not None:
            ctx.store.add_usage(row, busy_ms=LOCK_MS)
        else:
            from .store import Store  # late: store imports models, never this module, but nothing here needs it at import
            store = Store(ctx.db_path, busy_ms=LOCK_MS)
            try:
                store.add_usage(row, busy_ms=LOCK_MS)
            finally:
                store.close()
        if _lock["dropped"]:  # recording works again: say what the lock cost, once
            log.warning("usage: %d row(s) were dropped while the database was locked", _lock["dropped"])
            _lock["dropped"] = 0
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            _lock["dropped"] += 1
            _lock["until"] = time.monotonic() + LOCK_PAUSE_S
            log.warning("usage: the database is locked, so a row was dropped (%d dropped so far); recording pauses for %d s",
                        _lock["dropped"], LOCK_PAUSE_S)
        else:
            log.warning("usage: cannot record a %s call (%s)", provider, type(e).__name__)
    except Exception as e:  # noqa: BLE001 - recording must never change a job's outcome
        log.warning("usage: cannot record a %s call (%s)", provider, type(e).__name__)


def _best_effort(fn: Callable) -> Callable:
    """A recording helper that can never raise an Exception, whatever it is handed: the parsing before the INSERT is as
    much a part of "recording is best effort" as the INSERT. (A BaseException, such as the tick watchdog's, passes.)"""
    @functools.wraps(fn)
    def inner(*args, **kw):
        try:
            return fn(*args, **kw)
        except Exception as e:  # noqa: BLE001 - recording must never change a job's outcome
            log.warning("usage: cannot record a call (%s in %s)", type(e).__name__, fn.__name__)
    return inner


# -- Claude -----------------------------------------------------------------

def claude_figures(final: dict | None, requested: str, init_model: str | None = None) -> list[dict]:
    """What a `claude -p` run used, one entry per model that ran (a run can use a second, small one). `final` is the
    stream's result event, None when the run left none (a timeout, a crash): then the entry has no counts and no cost.
    The result event's `modelUsage` names each model with its own counts and cost, and sums to the event's `usage` and
    `total_cost_usd`; without it the run is one entry from those two. The cost is the CLI's list-price figure."""
    unknown = {"model": model_of(init_model, model_of(requested)), "input_tokens": None, "output_tokens": None,
               "cache_read_tokens": None, "cache_write_tokens": None, "cost_usd": None, "cost_basis": "unknown"}
    if not isinstance(final, dict):
        return [unknown]
    per_model = final.get("modelUsage")
    out = []
    if isinstance(per_model, dict):
        for name, u in per_model.items():
            if not isinstance(u, dict) or not isinstance(name, str):
                continue
            cost = money_of(u.get("costUSD"))
            out.append({"model": model_of(name), "input_tokens": count_of(u.get("inputTokens")),
                        "output_tokens": count_of(u.get("outputTokens")),
                        "cache_read_tokens": count_of(u.get("cacheReadInputTokens")),
                        "cache_write_tokens": count_of(u.get("cacheCreationInputTokens")),
                        "cost_usd": cost, "cost_basis": "list" if cost is not None else "unknown"})
    if out:  # the model that did the most work first
        return sorted(out, key=lambda e: (e["cost_usd"] or 0.0, e["output_tokens"] or 0), reverse=True)
    block = final.get("usage") if isinstance(final.get("usage"), dict) else {}
    cost = money_of(final.get("total_cost_usd"))
    if not block and cost is None:
        return [unknown]
    return [{"model": unknown["model"], "input_tokens": count_of(block.get("input_tokens")),
             "output_tokens": count_of(block.get("output_tokens")),
             "cache_read_tokens": count_of(block.get("cache_read_input_tokens")),
             "cache_write_tokens": count_of(block.get("cache_creation_input_tokens")),
             "cost_usd": cost, "cost_basis": "list" if cost is not None else "unknown"}]


_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def session_ref(session_id: object) -> str | None:
    """The transcript file name of a Claude session (`<session id>.jsonl`): the `ref` of its rows, live and backfilled, so
    the backfill never adds a run the live record already holds."""
    return f"{session_id}.jsonl" if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id) else None


@_best_effort
def record_claude(final: dict | None, requested: str, *, init_model: str | None, ok: bool, latency_ms: int | None,
                  allowed_tools: list[str] | None = None) -> None:
    """One row for each model a `claude -p` run used (see claude_figures). A run that allows a Slack connector tool is a
    gateway call: its step is that tool's name, and with no job around it the job is `slack`. The run's wall time goes on
    the row of the model that did most of the work, so a side model's row does not carry it."""
    tools = [t for t in (allowed_tools or []) if isinstance(t, str)]
    slack = next((t for t in tools if t.startswith(SLACK_TOOL_PREFIX)), None)
    step = slack[len(SLACK_TOOL_PREFIX):] if slack else None
    ref = session_ref(final.get("session_id")) if isinstance(final, dict) else None
    for i, fig in enumerate(claude_figures(final, requested, init_model)):
        record("claude", fig["model"], input_tokens=fig["input_tokens"], output_tokens=fig["output_tokens"],
               cache_read_tokens=fig["cache_read_tokens"], cache_write_tokens=fig["cache_write_tokens"],
               cost_usd=fig["cost_usd"], cost_basis=fig["cost_basis"], latency_ms=latency_ms if i == 0 else None,
               ok=ok, step=step, default_job="slack" if slack else "other", ref=ref)


# -- LiteLLM ----------------------------------------------------------------

COST_HEADER = "x-litellm-response-cost"
CALL_ID_HEADER = "x-litellm-call-id"


def litellm_figures(body: object, headers: object = None) -> dict:
    """The counts of an OpenAI-style response body's `usage` block and, from the response headers, the cost the proxy
    reports (`x-litellm-response-cost`) and the call's id. Cached tokens sit inside `prompt_tokens`, so they come out of
    the fresh input. An embeddings answer has a prompt count only."""
    u = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
    details = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    prompt = count_of(u.get("prompt_tokens"))
    cached = count_of(u.get("cache_read_input_tokens"))
    if cached is None:
        cached = count_of(details.get("cached_tokens"))
    written = count_of(u.get("cache_creation_input_tokens"))
    if written is None:
        written = count_of(details.get("cache_creation_tokens"))
    if prompt is not None:
        cached = None if cached is None else min(cached, prompt)
        written = None if written is None else min(written, max(0, prompt - (cached or 0)))
    get = getattr(headers, "get", None)
    cost = money_of(get(COST_HEADER)) if callable(get) else None
    return {"input_tokens": _delta(prompt, cached, written), "output_tokens": count_of(u.get("completion_tokens")),
            "cache_read_tokens": cached, "cache_write_tokens": written, "cost_usd": cost,
            "cost_basis": "proxy" if cost is not None else "unknown",
            "ref": ref_of(get(CALL_ID_HEADER)) if callable(get) else None}


@_best_effort
def record_litellm(model: object, response: object | None, *, latency_ms: int | None) -> None:
    """One row for a LiteLLM request that has just ended. `response` is None when no answer came (a timeout, a connection
    error). Any status but 200 is a failed call, and it keeps whatever the answer still says about its counts."""
    if response is None:
        record("litellm", model, latency_ms=latency_ms, ok=False)
        return
    ok = getattr(response, "status_code", None) == 200
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an error page is not JSON; the row simply has no counts
        body = None
    record("litellm", model, latency_ms=latency_ms, ok=ok, **litellm_figures(body, getattr(response, "headers", None)))


def recorded_post(fn: Callable) -> Callable:
    """A decorator for `LLM._post(self, path, payload, ...)`: the usage row of the request, whatever else the method takes
    or does (so litellm.py carries one line, and a variant of the call that goes through `_post`, an image call say, is
    recorded too). A request that ends in an exception (a timeout, a connection error) is a failed row. `OfflineError` is
    the guard saying no request was made: it leaves no row."""
    @functools.wraps(fn)
    def inner(self, path, payload, *args, **kw):
        model = payload.get("model") if isinstance(payload, dict) else None
        began = time.monotonic()
        try:
            resp = fn(self, path, payload, *args, **kw)
        except OfflineError:
            raise
        except Exception:
            record_litellm(model, None, latency_ms=int((time.monotonic() - began) * 1000))
            raise
        record_litellm(model, resp, latency_ms=int((time.monotonic() - began) * 1000))
        return resp
    return inner


# -- Codex ------------------------------------------------------------------

def codex_figures(stdout: object) -> dict | None:
    """The token counts in the last `turn.completed` event of a `codex exec --json` stream, or None when the stream has
    none. Codex counts the cached tokens inside `input_tokens`, so they come out of the fresh input. Nothing else of the
    stream is read, and none of it is kept."""
    if not isinstance(stdout, str):
        return None
    found = None
    for line in stdout.splitlines():
        if "turn.completed" not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            found = ev["usage"]
    if found is None:
        return None
    prompt, cached, written = (count_of(found.get("input_tokens")), count_of(found.get("cached_input_tokens")),
                               count_of(found.get("cache_write_input_tokens")))
    if prompt is not None:
        cached = None if cached is None else min(cached, prompt)
        written = None if written is None else min(written, max(0, prompt - (cached or 0)))
    return {"input_tokens": _delta(prompt, cached, written), "output_tokens": count_of(found.get("output_tokens")),
            "cache_read_tokens": cached, "cache_write_tokens": written}


def codex_model(argv: list[str]) -> str:
    """The model a `codex exec` command line asks for (`-m <model>`). The cover run asks for none, so Codex picks its own
    default: that is what the row says."""
    for i, a in enumerate(argv[:-1]):
        if a in ("-m", "--model"):
            return model_of(argv[i + 1], "codex-default")
    return "codex-default"


@_best_effort
def record_codex(argv: list[str], stdout: object, *, returncode: int | None, latency_ms: int | None, step: str) -> None:
    """One row for a `codex exec` run that has just ended. ChatGPT Pro has no per-call price, so there is no cost; a
    stream with no token counts leaves them empty."""
    figures = codex_figures(stdout) or {}
    record("codex", codex_model(argv), cost_basis="subscription", latency_ms=latency_ms, ok=returncode == 0, step=step,
           **figures)
