"""`haseeb-agent usage-backfill`: the usage history from before the Usage tab existed. One-time and safe to run again.

Claude. Every headless run left a Claude Code transcript under
`~/.claude/projects/-Users-cb--haseeb-agent-claude-home/<session>.jsonl`. For each session this adds one `model_usage` row
per model it used, `source` backfill and `ref` the session's file name, which is also the `ref` the live record gives the
same run (usage.session_ref), so a run is never counted twice. The counts and the cost come from the transcript's
`cost-state` line (the session's `modelUsage`: tokens and cost per model), or, for a transcript without one, from its
assistant messages (their `usage`, and a `costUSD` where they carry one). The job and step are inferred from the session's
first prompt, which the agent's own prompts open in known ways (`infer_job_step`); a prompt that matches none is
`unknown`: a Slack gateway run is `unknown` with its tool as the step, because the transcript cannot say which job asked, and
the Usage tab shows it as "Earlier runs (job unknown)", never as Slack; only a prompt that is the research step's or a chat
step's own is a confident inference. Only numbers and that inference come out of a transcript: no text of it is stored,
printed or logged. Sessions that started after the first live Claude record are left out (the live record has them), a
session with no model usage (a run that never got an answer) adds nothing, and a transcript with no `cost-state` line that
was written to a moment ago is a run still going: it is left for the live record, or for the next run of this command.
A transcript with no `cost-state` line that is idle (a run that was killed or hung) is summed from its messages, which is
approximate (output is far under the real figure: the CLI records a message's usage at its start): its rows are marked
(ref `approx:<file>`), recomputed on every run, and replaced by the final rows if a `cost-state` line ever appears.

LiteLLM. The company proxy may keep the agent's spend. `GET /spend/logs` is asked for the per-request logs of the agent's own
key (`api_key=` its sha256, which is how LiteLLM stores it), and each becomes a row, `ref` the request id. The answer is read
as it streams and refused past a byte cap. A log that names another key means the proxy did not filter, and nothing is
imported (an admin or team key sees everyone's spend); a summary, which cannot be filtered afterwards, is taken only if it is
tied to the key (no other key in its breakdown, and a total within the key's own lifetime spend from `/key/info`). A proxy that answers with its daily summary instead (or
one whose per-request answer is too big or too slow, which is then asked for the summary), gives each model's spend for
each day: a row per model and day, with a cost and no call (`DAILY_TOTAL_STEP`), and only for days that are over and that
the live record does not touch, so no day is counted twice. A day has one source: a summary never adds to a day that already
has per-request rows, and a run that brings a day's requests deletes that day's summary rows in the same transaction.
`GET /key/info` is asked next when neither works, but it only
has lifetime totals, so it can say the key exists and nothing more. When nothing usable comes back (a 401, 403, 404, a
timeout, anything else) the import is skipped with one line, and the Usage tab says "Company-proxy history not available;
counted from <first live date>". The outcome is kept in the cursor `usage_backfill:litellm` for the tab.
The proxy is a real outside call: `guard()` runs before it, so the test suite can never make it.

Codex has no history to import: the agent's runs are `--ephemeral`, they leave no session files.

The command prints counts only.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import httpx

from .claude_cli import AGENT_HOME
from .config import Settings
from .models import APPROX_REF_PREFIX, UNKNOWN_JOB, UsageRow
from .offline import guard
from .slack_gateway import SLACK_TOOLS
from .store import Store
from .usage import (CLAUDE_CURSOR, DAILY_TOTAL_STEP, LITELLM_CURSOR, SLACK_TOOL_PREFIX, count_of, model_of, money_of,
                    ref_of)

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
PROMPT_HEAD = 400            # a prompt is only ever looked at this far: enough to see how it opens
IDLE_S = 600                 # a transcript with no cost-state line is read once nothing has been written to it for this long
PROXY_TIMEOUT_S = 30         # one step of a request (connecting, a chunk coming in)
PROXY_DEADLINE_S = 120       # all of one answer, however slowly it trickles in
MAX_PROXY_BYTES = 64_000_000
KEY_INFO_BYTES = 1_000_000
DEFAULT_WINDOW_DAYS = 30     # how far back the proxy is asked for when there are no transcripts to say when the agent began
_MOCK_TRANSPORT = httpx.MockTransport  # a test's client on a mock transport makes no outside call, so it needs no guard


def transcripts_dir(home: Path = AGENT_HOME, root: Path = PROJECTS_ROOT) -> Path:
    """Where Claude Code keeps the transcripts of runs in `home`: the folder name is the path with / and . as -."""
    return root / str(home).replace("/", "-").replace(".", "-")


# --------------------------------------------------------------------------
# Which job a Claude session was for
# --------------------------------------------------------------------------

# The gateway's prompt (slack_gateway._PROMPT) is "Call the tool <tool> once with exactly these arguments..."; the early
# prompts of the first days, and the probes run by hand, said "Call <tool> ..." or left the rest out.
_SLACK_CALL = re.compile(r"Call (?:the tool )?" + re.escape(SLACK_TOOL_PREFIX) + r"(\w+)")
_RESEARCH = "You are the research step of Haseeb's Agent."
# How the system prompts of the chat steps open (litellm.LLM._claude_chat sends "SYSTEM:\n<system prompt>..."), and the
# job and step each belongs to. tests/test_usage_backfill.py checks every opening against the code that builds the prompt.
_SYSTEM_OPENINGS: tuple[tuple[str, str, str], ...] = (
    ("You write one daily insight for Haseeb Zafar", "insights", "insight_writer"),
    ("You are a strict quality judge for a daily insight", "insights", "judge"),
    ("You are a strict quality judge for feedback", "feedback", "judge"),
    ("You draft short feedback for Haseeb Zafar", "feedback", "feedback_writer"),
    ("You condense a teammate's post", "feedback", "summarize"),
    ('You write Haseeb Zafar\'s "Next Todo"', "todo", "todo_writer"),
    ("You polish a short message that Haseeb Zafar wrote roughly", "compose", "feedback_writer"),
    ("You label one message from a company leadership-program Slack channel", "history", "summarize"),
    ("You name a group of related messages from a company leadership-program Slack channel", "history", "summarize"),
)


def infer_job_step(prompt: str | None) -> tuple[str, str]:
    """(job, step) for a session from how its first prompt opens. The research prompt and the chat steps' system prompts open in
    known ways: those are confident. The Slack gateway's prompt names the tool but not the job that asked (the same read serves
    insights, the todo, feedback and the DM commands), so its job is `unknown` and its step the tool; anything else is
    ("unknown", "other"). The Usage tab shows `unknown` as "Earlier runs (job unknown)", never as Slack (review M3)."""
    text = (prompt or "").lstrip()[:PROMPT_HEAD]
    if m := _SLACK_CALL.match(text):
        # only a tool the connector really has: the word after the prefix is the one thing taken from a prompt, and a probe
        # run by hand ("Call mcp__claude_ai_Slack__<anything>") must not put anything of its own into a row (review M8)
        return (UNKNOWN_JOB, m.group(1)) if m.group(1) in SLACK_TOOLS else (UNKNOWN_JOB, "other")
    if text.startswith(_RESEARCH):
        return "insights", "research"
    if text.startswith("SYSTEM:"):
        system = text[len("SYSTEM:"):].lstrip()
        for opening, job, step in _SYSTEM_OPENINGS:
            if system.startswith(opening):
                return job, step
    return UNKNOWN_JOB, "other"


# --------------------------------------------------------------------------
# Reading a transcript
# --------------------------------------------------------------------------

@dataclass
class Session:
    name: str                     # the transcript's file name: the `ref` of its rows
    start: datetime
    job: str
    step: str
    models: list[dict] = field(default_factory=list)  # per model: tokens, cost; the model with the most work first
    wall_ms: int | None = None
    complete: bool = True   # it has its cost-state line: the run is over and these are its final figures


def _time(value: object, tz: tzinfo) -> datetime | None:
    """An ISO time (a naive one is UTC, as the transcripts' and the proxy's are) or epoch milliseconds, in `tz`."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(tz)
        if isinstance(value, str) and value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(tz)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _first_text(message: object) -> str | None:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content[:PROMPT_HEAD * 2]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                return block["text"][:PROMPT_HEAD * 2]
    return None


def _from_cost_state(state: dict) -> list[dict]:
    out = []
    per_model = state.get("modelUsage")
    for name, u in (per_model.items() if isinstance(per_model, dict) else ()):
        if not isinstance(u, dict) or not isinstance(name, str) or name.startswith("<"):
            continue
        cost = money_of(u.get("costUSD"))
        out.append({"model": model_of(name), "input_tokens": count_of(u.get("inputTokens")),
                    "output_tokens": count_of(u.get("outputTokens")),
                    "cache_read_tokens": count_of(u.get("cacheReadInputTokens")),
                    "cache_write_tokens": count_of(u.get("cacheCreationInputTokens")), "cost_usd": cost})
    return out


def _merge(cur: dict | None, new: dict) -> dict:
    """The same assistant message seen again (it is written once per content block): the largest figures."""
    if cur is None:
        return new
    out = {k: max(cur[k], new[k]) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}
    costs = [c for c in (cur["cost_usd"], new["cost_usd"]) if c is not None]
    return {"model": new["model"], **out, "cost_usd": max(costs) if costs else None}


def _from_messages(messages: dict[str, dict]) -> list[dict]:
    """The sum of each model's assistant messages. A message is written once per content block, each time with its usage
    so far, so one message counts once: the largest figure seen for its id."""
    sums: dict[str, dict] = {}
    for m in messages.values():
        agg = sums.setdefault(m["model"], {"model": m["model"], "input_tokens": 0, "output_tokens": 0,
                                           "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": None})
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            agg[key] += m[key]
        if m["cost_usd"] is not None:
            agg["cost_usd"] = (agg["cost_usd"] or 0.0) + m["cost_usd"]
    return list(sums.values())


def read_session(path: Path, tz: tzinfo) -> Session | None:
    """One transcript as a Session, or None when it holds no model usage or cannot be read. Only numbers, the first
    prompt's opening (to infer the job from) and the times are taken from it."""
    prompt: str | None = None
    start: datetime | None = None
    state: dict | None = None
    messages: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if line.startswith('{"type":"api-request'):  # the bulky request dumps: nothing in them is needed
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if start is None:
                    start = _time(ev.get("timestamp"), tz)
                kind = ev.get("type")
                if kind == "user" and prompt is None and not ev.get("isMeta"):
                    prompt = _first_text(ev.get("message"))
                elif kind == "cost-state":
                    state = ev
                elif kind == "assistant" and isinstance(ev.get("message"), dict):
                    msg = ev["message"]
                    u = msg.get("usage")
                    model = msg.get("model")
                    if not isinstance(u, dict) or not isinstance(model, str) or model.startswith("<"):
                        continue
                    key = str(msg.get("id") or f"line-{n}")
                    new = {"model": model_of(model), "input_tokens": count_of(u.get("input_tokens")) or 0,
                           "output_tokens": count_of(u.get("output_tokens")) or 0,
                           "cache_read_tokens": count_of(u.get("cache_read_input_tokens")) or 0,
                           "cache_write_tokens": count_of(u.get("cache_creation_input_tokens")) or 0,
                           "cost_usd": money_of(ev.get("costUSD"))}
                    messages[key] = _merge(messages.get(key), new)
    except OSError:
        return None
    wall = None
    if state is not None:
        models = _from_cost_state(state)
        start = _time(state.get("startTime"), tz) or start
        wall = count_of(state.get("totalDuration"))
    else:
        models = _from_messages(messages)
    models = [m for m in models if any(m[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                                       "cache_write_tokens", "cost_usd"))]
    if not models or start is None:
        return None
    models.sort(key=lambda m: (m["cost_usd"] or 0.0, m["output_tokens"] or 0), reverse=True)
    job, step = infer_job_step(prompt)
    return Session(name=path.name, start=start, job=job, step=step, models=models, wall_ms=wall, complete=state is not None)


def session_ref(name: str, complete: bool = True) -> str:
    """The `ref` of a session's rows: its file name (what the live record uses too), under `approx:` while the transcript has
    no final figures. A name that is not an identifier (the folder holds `<uuid>.jsonl` files, so none is) still gets a stable
    ref, so a rerun finds its rows."""
    ref = ref_of(name) or "h-" + hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:16]
    return ref if complete else APPROX_REF_PREFIX + ref


def session_rows(s: Session) -> list[UsageRow]:
    return [UsageRow(
        at=s.start, provider="claude", model=m["model"], job=s.job, step=s.step, input_tokens=m["input_tokens"],
        output_tokens=m["output_tokens"], cache_read_tokens=m["cache_read_tokens"],
        cache_write_tokens=m["cache_write_tokens"], cost_usd=m["cost_usd"],
        cost_basis="list" if m["cost_usd"] is not None else "unknown",
        latency_ms=s.wall_ms if i == 0 else None,  # the wall time belongs to the model that did the most work
        ok=True, source="backfill", ref=session_ref(s.name, s.complete),
    ) for i, m in enumerate(s.models)]


@dataclass
class ClaudeCounts:
    sessions: int = 0       # transcripts looked at
    added_rows: int = 0
    existing_rows: int = 0  # rows that were there already (a run of this command before, or the live record)
    newer: int = 0          # sessions left out because they started after the first live Claude record
    empty: int = 0          # sessions with no model usage (a run that never got an answer), or unreadable
    busy: int = 0           # sessions with no cost-state line that were written to a moment ago: still running
    approx: int = 0         # idle sessions with no cost-state line, summed from their messages: approximate, recomputed each run
    unreadable: int = 0     # transcripts whose reading failed (left out; the others go on)
    rejected: int = 0       # rows the store would not take (a value it does not accept), left out and counted
    first_day: date | None = None  # the day of the oldest session read
    missing: bool = False   # no transcripts folder


def backfill_claude(store: Store, folder: Path, tz: tzinfo, now: float | None = None) -> ClaudeCounts:
    """`now` (epoch seconds, default the clock) is what a transcript's last write is measured against."""
    res = ClaudeCounts()
    if not folder.is_dir():
        res.missing = True
        return res
    now = time.time() if now is None else now
    cutoff = store.first_usage_at("live", "claude")
    rows: list[UsageRow] = []         # final rows: a run with its cost-state line
    approx_rows: list[UsageRow] = []  # approximate rows: an idle run with none
    gone: list[tuple[str, str]] = []  # the approximate rows of runs that now have final ones
    for path in sorted(folder.glob("*.jsonl")):
        res.sessions += 1
        try:
            s = read_session(path, tz)
        except Exception:  # noqa: BLE001 - one odd file must not stop the others (review M9)
            res.unreadable += 1
            continue
        if s is None:
            res.empty += 1
            continue
        if not s.complete:
            try:
                idle = now - path.stat().st_mtime
            except OSError:
                idle = 0.0
            if idle < IDLE_S:  # still being written: its final figures are not in yet
                res.busy += 1
                continue
        if cutoff is not None and s.start >= cutoff:
            res.newer += 1
            continue
        res.first_day = min(res.first_day, s.start.date()) if res.first_day else s.start.date()
        if s.complete:
            rows += session_rows(s)
            gone.append(("claude", session_ref(path.name, complete=False)))  # an approximate row of this run is replaced
        else:
            approx_rows += session_rows(s)
            res.approx += 1
    done = store.import_usage_rows(rows, replace_refs=gone)
    res.added_rows, res.rejected = done.added, done.rejected
    res.existing_rows = len(rows) - done.added - done.rejected
    if approx_rows:  # recomputed every run: what an earlier run made of these runs goes, and what it is now goes in
        again = store.import_usage_rows(approx_rows, replace_refs=[("claude", r.ref) for r in approx_rows if r.ref])
        res.rejected += again.rejected
    return res


# --------------------------------------------------------------------------
# The company proxy
# --------------------------------------------------------------------------

@dataclass
class LiteLLMCounts:
    state: str = "skipped"   # imported | unavailable | skipped (--skip-proxy)
    reason: str = ""         # a status code or an exception class: never text from the proxy
    added_rows: int = 0
    existing_rows: int = 0
    newer: int = 0           # left out: after the first live record (per request), or a day not over / the live record touches
    daily: bool = False      # the rows are the proxy's daily totals, not its per-request logs
    replaced: int = 0        # daily totals deleted because this run brought that day's per-request rows
    covered: int = 0         # daily totals left out because that day already has per-request rows
    rejected: int = 0        # entries or rows left out and counted: one with a value the store will not take


def _answer_list(payload: object) -> list | None:
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("logs", payload.get("results")))
    return payload if isinstance(payload, list) else None


def _logs_of(payload: object) -> list[dict] | None:
    """The per-request spend logs in a /spend/logs answer, or None when it is not a list of them (a summary by day has
    `models` spend per day, with no request in it)."""
    found = _answer_list(payload)
    if found is None:
        return None
    logs = [e for e in found if isinstance(e, dict)]
    # a per-request log has a request id or a start time and no per-model map; a summary by day has the map
    if logs and not any(("request_id" in e or "startTime" in e) and not isinstance(e.get("models"), dict) for e in logs):
        return None
    return logs


def _days_of(payload: object) -> list[dict] | None:
    """The days of a /spend/logs summary: each has a day and `models`, the spend of each model that day. None when the
    answer has none."""
    found = _answer_list(payload)
    days = [e for e in found or [] if isinstance(e, dict) and isinstance(e.get("models"), dict)]
    return days or None


def litellm_row(entry: dict, tz: tzinfo) -> UsageRow | None:
    """One per-request spend log as a row, or None when it has no usable time or model."""
    began = _time(entry.get("startTime"), tz)
    model = model_of(entry.get("model_group") or entry.get("model"), "")
    if began is None or not model:
        return None
    ended = _time(entry.get("endTime"), tz)
    prompt, completion = count_of(entry.get("prompt_tokens")), count_of(entry.get("completion_tokens"))
    cost = money_of(entry.get("spend"))
    kind = str(entry.get("call_type") or "")
    request_id = ref_of(entry.get("request_id")) or "h-" + hashlib.sha256(
        f"{entry.get('startTime')}|{model}|{prompt}|{completion}|{entry.get('spend')}".encode()).hexdigest()[:16]
    failed = str(entry.get("status") or "").lower() in ("failure", "failed", "error")
    return UsageRow(
        at=began, provider="litellm", model=model, job="other", step="embed" if "embed" in kind else "other",
        input_tokens=prompt, output_tokens=None if "embed" in kind else completion, cost_usd=cost,
        cost_basis="proxy" if cost is not None else "unknown",
        latency_ms=int((ended - began).total_seconds() * 1000) if ended is not None and ended >= began else None,
        ok=not failed, source="backfill", ref=request_id,
    )


def litellm_day_rows(days: list[dict], tz: tzinfo, limit: datetime, covered: Collection[str] = ()
                     ) -> tuple[list[UsageRow], int, int]:
    """A cost-only row for each model of each day of the proxy's summary (`DAILY_TOTAL_STEP`), at the start of the day
    (the proxy's days are UTC days) in `tz`; how many were left out because the day is not over or the live record touches
    it; and how many because the day is in `covered` (UTC dates that already have per-request rows). A day counts only
    when it is over by `limit` (the first live LiteLLM record or now, whichever is first): a day the live record touches,
    or that is still going, would be counted twice or half, and a day total cannot be cut. And a day has one source: the
    proxy's summary of a day whose requests are already here would count its spend twice."""
    rows, left_out, skipped = [], 0, 0
    for entry in days:
        start = _time(entry.get("startTime") or entry.get("date") or entry.get("day"), timezone.utc)
        if start is None:
            continue
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        for model, spend in entry["models"].items():
            name = model_of(model, "")
            cost = money_of(spend.get("spend") if isinstance(spend, dict) else spend)
            if not name or not cost:  # no name, no cost, or a day with nothing spent: nothing to count
                continue
            if start.date().isoformat() in covered:
                skipped += 1
                continue
            if start + timedelta(days=1) > limit:
                left_out += 1
                continue
            rows.append(UsageRow(
                at=start.astimezone(tz), provider="litellm", model=name, job="other", step=DAILY_TOTAL_STEP, cost_usd=cost,
                cost_basis="proxy", ok=True, source="backfill", ref=f"day:{start.date().isoformat()}:{name}"))
    return rows, left_out, skipped


def key_filter(settings: Settings) -> str:
    """How the proxy knows the agent's key: LiteLLM stores a virtual key hashed (sha256, hex), and `/spend/logs?api_key=` takes
    that hash. It is what the import asks for, and what it expects to find in the logs it gets back."""
    return hashlib.sha256(settings.litellm_api_key.encode()).hexdigest()


def _ask(http: httpx.Client, settings: Settings, path: str, params: dict | None = None, cap: int | None = None
         ) -> tuple[object | None, str]:
    """(the JSON answer, "") for a 200, else (None, why): the status code or the exception's class, never the body. The
    answer is read as it streams in and refused ("too large") the moment it passes `cap` (default MAX_PROXY_BYTES), a
    declared length over it before a byte is read, and one that takes longer than PROXY_DEADLINE_S is a "timeout": a huge
    answer is never all in memory first (review M2)."""
    cap = MAX_PROXY_BYTES if cap is None else cap
    deadline = time.monotonic() + PROXY_DEADLINE_S
    chunks: list[bytes] = []
    size = 0
    try:
        with http.stream("GET", f"{settings.litellm_base_url.rstrip('/')}{path}", params=params,
                         headers={"Authorization": f"Bearer {settings.litellm_api_key}"}, timeout=PROXY_TIMEOUT_S) as resp:
            if resp.status_code != 200:
                return None, str(resp.status_code)
            declared = resp.headers.get("content-length", "")
            if declared.isascii() and declared.isdigit() and int(declared) > cap:
                return None, "too large"
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > cap:
                    return None, "too large"
                if time.monotonic() > deadline:
                    return None, "timeout"
                chunks.append(chunk)
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.HTTPError as e:
        return None, type(e).__name__
    try:
        answer = json.loads(b"".join(chunks))
    except ValueError:  # (UnicodeDecodeError is one)
        return None, "not JSON"
    return (answer, "") if answer is not None else (None, "empty")


def _other_keys_logs(logs: list[dict], settings: Settings) -> bool:
    """True when a per-request log names a key that is not the agent's: the proxy did not filter by key, and what came back
    cannot be taken as Haseeb's (an admin or team key sees everyone's). A log with no key field is not held against it."""
    mine = {key_filter(settings), settings.litellm_api_key}
    return any(isinstance(e.get("api_key"), str) and e["api_key"] and e["api_key"] not in mine for e in logs)


def _summary_is_this_keys(http: httpx.Client, settings: Settings, days: list[dict]) -> tuple[bool, str]:
    """Whether a daily summary can be taken as the agent's own spend. A summary cannot be filtered after the fact, so it has to
    be tied to the key: a per-key breakdown in it must name only this key; and its total must not be more than the key's
    own lifetime spend (`/key/info`), which, with a key whose spend is not known, means it cannot be told and is not taken."""
    mine = {key_filter(settings), settings.litellm_api_key}
    for entry in days:
        by_key = entry.get("api_keys")
        if isinstance(by_key, dict) and any(k not in mine for k in by_key):
            return False, "the daily totals name other keys (the proxy did not filter by key)"
    info, why = _ask(http, settings, "/key/info", cap=KEY_INFO_BYTES)
    detail = info.get("info", info) if isinstance(info, dict) else None
    spend = money_of(detail.get("spend")) if isinstance(detail, dict) else None
    if spend is None:
        return False, f"the daily totals cannot be tied to this key (/key/info {why or 'has no spend'})"
    total = sum(money_of(v.get("spend") if isinstance(v, dict) else v) or 0.0 for e in days for v in e["models"].values())
    if total > spend * 1.02 + 0.01:
        return False, "the daily totals are more than this key's own spend (the proxy did not filter by key)"
    return True, ""


def backfill_litellm(store: Store, settings: Settings, start: date, now: datetime, http: httpx.Client | None = None
                     ) -> LiteLLMCounts:
    """Import the proxy's spend from `start` on: its per-request logs (older than the first live LiteLLM record), else its
    daily totals. The offline guard runs first: the one place this command makes a real outside call (a test's client on
    a mock transport makes none)."""
    if http is None or not isinstance(getattr(http, "_transport", None), _MOCK_TRANSPORT):
        guard("litellm")
    http = http or httpx.Client(timeout=PROXY_TIMEOUT_S)
    res = LiteLLMCounts(state="unavailable")
    window = {"start_date": start.isoformat(), "end_date": (now.astimezone(settings.tz).date() + timedelta(days=1)).isoformat(),
              "api_key": key_filter(settings)}  # only this key's spend: other people's is not Haseeb's (review M2)
    payload, why = _ask(http, settings, "/spend/logs", {**window, "summarize": "false"})
    logs, days = _logs_of(payload), _days_of(payload)
    if payload is not None and logs is None and days is None:
        why = "no usable logs"
    if logs is None and days is None and why in ("too large", "timeout"):
        # more than can be read, or too slow to come: the daily summary is small
        payload, why = _ask(http, settings, "/spend/logs", {**window, "summarize": "true"})
        days = _days_of(payload)
        if payload is not None and days is None:
            why = "no usable logs"
    if logs is None and days is None:
        # /key/info only holds lifetime totals per model: there is no day in it to put a total on
        info, why_info = _ask(http, settings, "/key/info")
        res.reason = f"/spend/logs {why}; /key/info " + ("lifetime totals only" if info is not None else why_info)
        return res
    if logs is not None and _other_keys_logs(logs, settings):
        res.reason = "/spend/logs returned another key's logs (the proxy did not filter by key)"
        return res
    if logs is None:
        mine, why_not = _summary_is_this_keys(http, settings, days)
        if not mine:
            res.reason = f"/spend/logs: {why_not}"
            return res
    cutoff = store.first_usage_at("live", "litellm")
    if logs is not None:
        rows = []
        for entry in logs:
            try:
                row = litellm_row(entry, settings.tz)
            except Exception:  # noqa: BLE001 - one odd log must not stop the others (review M9)
                res.rejected += 1
                continue
            if row is None:
                continue
            if cutoff is not None and row.at >= cutoff:
                res.newer += 1
                continue
            rows.append(row)
        # one source a day: the days these requests are on lose any daily total an earlier run took from the summary
        replace = {r.at.astimezone(timezone.utc).date().isoformat() for r in rows}
    else:
        res.daily = True
        replace = set()
        rows, res.newer, res.covered = litellm_day_rows(
            days, settings.tz, min(t for t in (cutoff, now) if t is not None),
            covered=store.usage_utc_days("litellm", "backfill", step_not=DAILY_TOTAL_STEP))
    res.state = "imported"
    done = store.import_usage_rows(rows, replace_daily=replace)
    res.added_rows, res.replaced = done.added, done.replaced
    res.rejected += done.rejected
    res.existing_rows = len(rows) - done.added - done.rejected
    return res


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

def run(store: Store, settings: Settings, now: datetime, *, folder: Path | None = None, skip_proxy: bool = False,
        http: httpx.Client | None = None, emit=print) -> int:
    """Both imports, then the notes the Usage tab reads. Prints counts only, one line per source. Exit code 0: a source
    that is not there (no folder, a proxy that does not answer) is a line, not a failure."""
    claude = backfill_claude(store, folder or transcripts_dir(), settings.tz)
    if claude.missing:
        emit("usage-backfill: claude: no transcripts folder; nothing to import")
    else:
        emit(f"usage-backfill: claude: {claude.sessions} session(s) read, {claude.added_rows} row(s) added, "
             f"{claude.existing_rows} already there, {claude.newer} newer than the live record, "
             f"{claude.empty} without usage" + (f", {claude.busy} still being written" if claude.busy else "")
             + (f", {claude.approx} approximate" if claude.approx else "")
             + (f", {claude.unreadable} unreadable" if claude.unreadable else "")
             + (f", {claude.rejected} row(s) rejected" if claude.rejected else ""))
    store.set_cursor(CLAUDE_CURSOR, json.dumps({
        "at": now.isoformat(), "rows": claude.added_rows + claude.existing_rows,
        "from": claude.first_day.isoformat() if claude.first_day else None}))
    if skip_proxy:
        emit("usage-backfill: litellm: skipped (--skip-proxy)")
        return 0
    today = now.astimezone(settings.tz).date()
    start = claude.first_day or today - timedelta(days=DEFAULT_WINDOW_DAYS)
    lite = backfill_litellm(store, settings, start, now, http)
    if lite.state == "imported":
        if lite.daily:
            emit(f"usage-backfill: litellm: {lite.added_rows} daily total(s) added, {lite.existing_rows} already there, "
                 f"{lite.newer + lite.covered} not counted (a day not over, or one the live record or the request logs "
                 f"already cover)" + (f", {lite.rejected} rejected" if lite.rejected else ""))
        else:
            emit(f"usage-backfill: litellm: {lite.added_rows} row(s) added, {lite.existing_rows} already there, "
                 f"{lite.newer} newer than the live record"
                 + (f", {lite.replaced} daily total(s) replaced by the request logs" if lite.replaced else "")
                 + (f", {lite.rejected} rejected" if lite.rejected else ""))
    else:
        emit(f"usage-backfill: litellm: not available ({lite.reason}); company-proxy history is not counted")
    store.set_cursor(LITELLM_CURSOR, json.dumps({
        "state": lite.state, "reason": lite.reason, "at": now.isoformat(), "rows": lite.added_rows + lite.existing_rows}))
    return 0
