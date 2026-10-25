"""The numbers behind the Usage tab (`GET /api/usage`), worked out from the `model_usage` rows. Read-only; a database with
no rows gives zeros and empty lists, never an error. No text is in a row, so none is in the answer.

Days are the settings timezone's (Kuwait), like every other page. "Today" is today; "7d" and "30d" are the last 7 and 30
days, today included. The chart always shows the last 14 days.

Tokens: `input_tokens` here is the whole prompt (fresh, cached and written to the cache), with `cache_read_tokens` and
`cache_write_tokens` the two parts of it that went through the cache (fresh input is the rest), so a Claude run that read
97,000 cached tokens is not shown as 6 tokens in. `tokens` is input plus output.

Runs: `calls` counts runs, not rows. A row is one model of one run, so a Claude run that used two models has two rows and is
one run (rows with the same provider and `ref` are one run). A LiteLLM request is a run of its own, and so is a Codex run.
A daily total of the proxy is no run at all: it adds cost and nothing else.

Cost: no single total mixes cost bases (review M5). A list price (Claude: paid from the subscription, never billed), a proxy
cost (real money) and Codex (a plan, tokens only) are shown apart: `by_provider` for the cards, `cost_by_basis` for a job or a
day. A call with no price adds no dollars and is counted in `unpriced_calls`; a provider with no known cost has none (null),
not $0.

Approximate: a row from a transcript with no final figures lives under `ref` `approx:<file>`; `approx_calls` counts the runs
that are, wherever they are counted, so the page can mark them "≈".
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, tzinfo

from .models import APPROX_REF_PREFIX, DAILY_TOTAL_STEP, UsageRow
from .store import Store
from .usage import LITELLM_CURSOR

RANGES: dict[str, int] = {"today": 1, "7d": 7, "30d": 30}
DAILY_DAYS = 14
CHART_MODELS = 6   # the chart colours this many models and groups the rest as "Other" (the page does the grouping)

NOTES: dict[tuple[str, str], str] = {
    ("claude", "list"): "List price, paid from your Claude subscription.",
    ("claude", "unknown"): "Some Claude runs have no cost on record (one that ended without a result, or an old transcript).",
    ("litellm", "proxy"): "Company proxy cost.",
    ("litellm", "unknown"): "Company proxy: cost not reported (tokens only).",
    ("codex", "subscription"): "ChatGPT Pro (tokens only).",
    ("codex", "unknown"): "Codex: no token counts in the stream.",
}
_ORDER = {"claude": 0, "litellm": 1, "codex": 2}
DAILY_NOTE = ("Days before the agent counted its own calls come from the proxy's daily totals, which are UTC days: they add "
              "cost, not runs or tokens.")
APPROX_NOTE = ("≈ marks figures summed from a transcript with no final totals (a run that was killed or hung): its output "
               "tokens are far under the real figure.")
RUNS_NOTE = ("A run is one Claude run, one Codex run or one proxy request; a Claude run that used two models counts once in a "
             "total and once in each model's row.")
ACCOUNTS_NOTE = "History includes runs on other Claude accounts used on this Mac ({span})"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _money(x: float | None) -> float | None:
    return None if x is None else round(x, 6)


def _prompt(r: UsageRow) -> int:
    return (r.input_tokens or 0) + (r.cache_read_tokens or 0) + (r.cache_write_tokens or 0)


def _span(first: date, last: date) -> str:
    """"21 Oct", "17–21 Oct", "28 Sep–3 Oct"."""
    if first == last:
        return f"{first.day} {_MONTHS[first.month - 1]}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}–{last.day} {_MONTHS[last.month - 1]}"
    return f"{first.day} {_MONTHS[first.month - 1]}–{last.day} {_MONTHS[last.month - 1]}"


class _Runs:
    """A count of runs: rows with the same provider and `ref` are one run, and a row with none is a run of its own."""

    def __init__(self) -> None:
        self.keys: set[tuple[str, str]] = set()
        self.bare = 0

    def add(self, r: UsageRow) -> None:
        if r.ref:
            self.keys.add((r.provider, r.ref))
        else:
            self.bare += 1

    def __len__(self) -> int:
        return len(self.keys) + self.bare


class _Agg:
    """Running totals of a group of rows."""

    def __init__(self) -> None:
        self.prompt = self.out = self.cached = self.written = 0
        self.runs, self.failed, self.priced, self.approx = _Runs(), _Runs(), _Runs(), _Runs()
        self.by_basis: dict[str, float] = defaultdict(float)  # the cost of the priced rows, apart for each basis
        self.bases: set[str] = set()         # the cost bases of the rows, whether they were priced or not
        self.priced_bases: set[str] = set()  # ... of the ones that have a cost
        self.latencies: list[int] = []

    def add(self, r: UsageRow) -> None:
        daily = r.step == DAILY_TOTAL_STEP  # a day's total from the proxy: its cost, but no run, no tokens, no time
        if not daily:
            self.runs.add(r)
            if not r.ok:
                self.failed.add(r)
            if r.ref and r.ref.startswith(APPROX_REF_PREFIX):
                self.approx.add(r)
            self.prompt += _prompt(r)
            self.out += r.output_tokens or 0
            self.cached += r.cache_read_tokens or 0
            self.written += r.cache_write_tokens or 0
            if r.latency_ms is not None:
                self.latencies.append(r.latency_ms)
        if r.cost_usd is not None:
            if not daily:
                self.priced.add(r)
            self.by_basis[r.cost_basis] += r.cost_usd
            self.priced_bases.add(r.cost_basis)
        self.bases.add(r.cost_basis)

    @property
    def unpriced(self) -> int:
        return len(self.runs) - len(self.priced)

    def basis(self) -> str:
        """What the cost rests on: the basis of the priced rows (a run that ended with no result has none to count), else
        of the rows; "mixed" when the priced ones differ."""
        bases = self.priced_bases or self.bases
        return next(iter(bases)) if len(bases) == 1 else "mixed"

    def counts(self) -> dict:
        return {"input_tokens": self.prompt, "output_tokens": self.out, "cache_read_tokens": self.cached,
                "cache_write_tokens": self.written, "calls": len(self.runs), "failures": len(self.failed),
                "unpriced_calls": self.unpriced, "approx_calls": len(self.approx)}

    def costs(self) -> dict:
        return {k: _money(v) for k, v in sorted(self.by_basis.items())}

    def cost_fields(self) -> dict:
        """The cost of a group that has one basis: `cost_usd` on `cost_basis`, and `cost_by_basis` for the page to use when it
        does not (a group that mixes bases has no single figure, and its `cost_usd` is None, never a sum across bases)."""
        only = next(iter(self.priced_bases)) if len(self.priced_bases) == 1 else None
        return {"cost_usd": _money(self.by_basis[only]) if only else None, "cost_basis": self.basis(),
                "cost_by_basis": self.costs()}


def _by_provider(rows: list[UsageRow]) -> list[dict]:
    """One entry for each provider that has rows: its cost on its own basis (never added to another provider's), its basis
    and its counts. Codex has no cost: tokens only."""
    aggs: dict[str, _Agg] = defaultdict(_Agg)
    for r in rows:
        aggs[r.provider].add(r)
    return [{"provider": p, **a.cost_fields(), **a.counts()}
            for p, a in sorted(aggs.items(), key=lambda kv: (_ORDER.get(kv[0], 9), kv[0]))]


def _range_totals(rows: list[UsageRow]) -> dict:
    """What a card shows: the counts of every row, the costs by basis, and each provider on its own. There is no single cost."""
    agg = _Agg()
    for r in rows:
        agg.add(r)
    return {**agg.counts(), "cost_by_basis": agg.costs(), "by_provider": _by_provider(rows)}


def _order_models(rows: list[dict]) -> list[dict]:
    """Most expensive first, then most tokens, then by name: a row with no cost sorts after every priced one."""
    return sorted(rows, key=lambda r: (-(r["cost_usd"] if r["cost_usd"] is not None else -1.0), -r["tokens"], r["model"]))


def _order_jobs(rows: list[dict]) -> list[dict]:
    """The most real money first (proxy cost), then the most list price, then the most tokens, then by name. Costs of different
    bases are never added up, not even to order by."""
    def key(r: dict):
        by = r["cost_by_basis"]
        return (-by.get("proxy", -1.0), -by.get("list", -1.0), -r["tokens"], r["job"])
    return sorted(rows, key=key)


def _in_range(rows: list[tuple[date, UsageRow]], today: date, days: int) -> list[UsageRow]:
    first = today - timedelta(days=days - 1)
    return [r for d, r in rows if first <= d <= today]


def _history(store: Store, tz: tzinfo, dated: list[tuple[date, UsageRow]]) -> dict:
    """What the page needs to say where its numbers begin: the first live record, what the company-proxy import did (state:
    imported, unavailable, skipped or not_run), and the days the Claude history covers (it was made on whatever Claude
    account the agent ran under then, which is not always the one the live runs use)."""
    def day(at: datetime | None) -> str | None:
        return at.astimezone(tz).date().isoformat() if at else None
    try:
        cur = json.loads(store.get_cursor(LITELLM_CURSOR) or "{}")
    except ValueError:
        cur = {}
    state = cur.get("state") if isinstance(cur, dict) and cur.get("state") in ("imported", "unavailable", "skipped") else "not_run"
    first = store.first_usage_at("live")
    past = [d for d, r in dated if r.provider == "claude" and r.source == "backfill"]
    return {"first_live": day(first),
            "litellm": {"state": state, "counted_from": day(store.first_usage_at("live", "litellm") or first)},
            "claude": {"from": min(past).isoformat() if past else None, "to": max(past).isoformat() if past else None}}


def build(store: Store, now: datetime, tz: tzinfo, range_name: str = "7d") -> dict:
    """The whole answer for one range. `range_name` must be a key of RANGES."""
    today = now.astimezone(tz).date()
    since = datetime.combine(today - timedelta(days=max(RANGES.values()) - 1), time.min, tz)
    dated = [(r.at.astimezone(tz).date(), r) for r in store.usage_since(since)]

    ranges = {name: _range_totals(_in_range(dated, today, days)) for name, days in RANGES.items()}

    chosen = _in_range(dated, today, RANGES[range_name])
    by_model: dict[tuple[str, str], _Agg] = defaultdict(_Agg)
    by_job: dict[str, _Agg] = defaultdict(_Agg)
    seen: set[tuple[str, str]] = set()
    for r in chosen:
        by_model[(r.provider, r.model)].add(r)
        by_job[r.job].add(r)
        seen.add((r.provider, r.cost_basis))

    models = _order_models([{
        "provider": p, "model": m, **a.counts(), "tokens": a.prompt + a.out, **a.cost_fields(),
        "latency_ms": int(statistics.median(a.latencies)) if a.latencies else None,
    } for (p, m), a in by_model.items()])
    jobs = _order_jobs([{
        "job": j, **a.counts(), "tokens": a.prompt + a.out, "cost_by_basis": a.costs(),
    } for j, a in by_job.items()])

    history = _history(store, tz, dated)
    notes = [{"provider": p, "basis": b, "text": NOTES.get((p, b), f"{p}: cost {b} (tokens only).")}
             for p, b in sorted(seen, key=lambda k: (_ORDER.get(k[0], 9), k[1]))]
    if any(r.step == DAILY_TOTAL_STEP for r in chosen):
        notes.append({"provider": "litellm", "basis": "daily", "text": DAILY_NOTE})
    if any(a.approx for a in by_model.values()):
        notes.append({"provider": "claude", "basis": "approx", "text": APPROX_NOTE})
    if history["claude"]["from"]:
        span = _span(date.fromisoformat(history["claude"]["from"]), date.fromisoformat(history["claude"]["to"]))
        notes.append({"provider": "claude", "basis": "history", "text": ACCOUNTS_NOTE.format(span=span)})
    if chosen:
        notes.append({"provider": "", "basis": "runs", "text": RUNS_NOTE})

    daily, chart = _daily(dated, today)
    return {"range": range_name, "days": RANGES[range_name], "since": (today - timedelta(days=RANGES[range_name] - 1)).isoformat(),
            "today": today.isoformat(), "has_data": store.usage_count() > 0,
            "totals": ranges[range_name], "ranges": ranges, "by_model": models, "by_job": jobs,
            "daily": daily, "chart_models": chart, "chart_max_models": CHART_MODELS, "notes": notes,
            "history": history}


def _daily(dated: list[tuple[date, UsageRow]], today: date) -> tuple[list[dict], list[dict]]:
    """The last 14 days, oldest first, each with its tokens and its cost by basis, in all and per model; and the models for
    the chart (most tokens in those days first: the page orders them again for the measure it shows)."""
    days = [today - timedelta(days=i) for i in range(DAILY_DAYS - 1, -1, -1)]
    per_day: dict[date, dict[tuple[str, str], _Agg]] = {d: defaultdict(_Agg) for d in days}
    whole: dict[tuple[str, str], _Agg] = defaultdict(_Agg)
    for d, r in dated:
        if d in per_day:
            per_day[d][(r.provider, r.model)].add(r)
            whole[(r.provider, r.model)].add(r)
    out = []
    for d in days:
        aggs = per_day[d]
        day_costs: dict[str, float] = defaultdict(float)
        for a in aggs.values():
            for basis, cost in a.by_basis.items():
                day_costs[basis] += cost
        out.append({
            "day": d.isoformat(), "tokens": sum(a.prompt + a.out for a in aggs.values()),
            "cost_by_basis": {k: _money(v) for k, v in sorted(day_costs.items())},
            "models": [{"key": f"{p}:{m}", "tokens": a.prompt + a.out, "cost_by_basis": a.costs()}
                       for (p, m), a in sorted(aggs.items())],
        })
    chart = [{"key": f"{p}:{m}", "provider": p, "model": m, "tokens": a.prompt + a.out, "cost_by_basis": a.costs()}
             for (p, m), a in sorted(whole.items(), key=lambda kv: (-(kv[1].prompt + kv[1].out), kv[0]))]
    return out, chart
