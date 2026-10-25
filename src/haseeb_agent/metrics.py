"""Weekly metrics: how much of what the agent drafted went out untouched.

Definitions (docs/quality-metrics.md has the long version):
  posted             items with status `posted` whose day falls in the week (insights, feedback, todos)
  edited_then_posted posted items that Haseeb changed first (`edit:` or `shorter`; the `edited` flag)
  auto_posted        todos the agent posted by itself after the 120 s wait; they count as posted unchanged
  skipped            items Haseeb skipped in the DM (a todo he posted himself before the agent did is not a skip)
  ignored            items still `delivered` after their day ended, shown as "Not used (alternates)" (M5): most are
                     the insights Haseeb did not pick on a day he posted another one
  missed_days        workdays up to today with no posted insight
  insight_days       workdays up to today with a posted insight, out of workdays_so_far
  intervention_rate  edited_then_posted / posted; None when nothing was posted
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from .models import KINDS
from .render import _day_label, _floor_score, _safe
from .store import Store

DEFAULT_WORKDAYS: frozenset[int] = frozenset({0, 1, 2, 3, 4})  # Mon..Fri


@dataclass(frozen=True)
class Stats:
    posted: int
    edited_then_posted: int
    skipped: int
    ignored: int
    auto_posted: int
    missed_days: int
    avg_score: float | None
    intervention_rate: float | None
    week_start: date | None = None
    self_posted: int = 0
    insight_days: int = 0
    workdays_so_far: int = 0
    failed_runs: int = 0  # job runs recorded as failed (a crash, or a Slack read that failed in the end)


def week_stats(store: Store, week_start: date, *, workdays: Collection[int] = DEFAULT_WORKDAYS,
               today: date | None = None) -> Stats:
    """Stats for the 7 days from `week_start`.

    `today` (the caller's date in settings.tz, ruling R4) marks which days have already ended or are running:
    an item is ignored only when its day is before `today`, and a workday is missed only when it is on or before
    `today`. Without it the week is treated as over.
    """
    days = [week_start + timedelta(days=i) for i in range(7)]
    today = today or days[-1] + timedelta(days=1)
    items = [it for day in days for kind in KINDS for it in store.items_for_day(kind, day)]

    posted = [it for it in items if it.status == "posted"]
    # A todo auto-post writes the cursor autopost_try:{id} just before it sends and never clears it; a manual
    # `post` or a post found already in the thread does not write it (see jobs/todo.py _autopost).
    auto = [it for it in posted if it.kind == "todo" and store.get_cursor(f"autopost_try:{it.id}") is not None]
    edited = [it for it in posted if it.edited]
    scores = [it.score for it in posted if it.score is not None]

    posted_insight_days = {it.day for it in posted if it.kind == "insight"}
    so_far = [d for d in days if d.weekday() in workdays and d <= today]
    missed = [d for d in so_far if d not in posted_insight_days]

    self_made = [it for it in items if it.status == "skipped" and store.get_cursor(f"manual_todo:{it.id}") is not None]
    self_ids = {it.id for it in self_made}

    return Stats(
        posted=len(posted),
        edited_then_posted=len(edited),
        skipped=sum(1 for it in items if it.status == "skipped" and it.dm_ts is not None and it.id not in self_ids),
        ignored=sum(1 for it in items if it.status == "delivered" and it.day < today),
        auto_posted=len(auto),
        missed_days=len(missed),
        avg_score=mean(scores) if scores else None,
        intervention_rate=len(edited) / len(posted) if posted else None,
        week_start=week_start,
        self_posted=len(self_made),
        insight_days=len(so_far) - len(missed),
        workdays_so_far=len(so_far),
        failed_runs=store.failed_runs_on(days),
    )


def render_weekly(stats: Stats) -> str:
    """The weekly DM text. Numbers only, but it still goes through render's sanitizer like every outgoing text."""
    head = "**Haseeb's Agent: weekly report" + (f", {_day_label(stats.week_start)}**" if stats.week_start else "**")
    detail = f"{stats.edited_then_posted} edited first, {stats.auto_posted} auto-posted"
    rate = (f"{round(stats.intervention_rate * 100)}% (edited then posted ÷ posted)"
            if stats.intervention_rate is not None else "n/a (nothing was posted)")
    score = f"{_floor_score(stats.avg_score)}/100" if _floor_score(stats.avg_score) is not None else "n/a"  # floored, like the model line
    lines = [
        head,
        "",
        f"Posted: {stats.posted} ({detail})",
        f"Skipped: {stats.skipped} · Not used (alternates): {stats.ignored}",
        f"Days with an insight posted: {stats.insight_days} of {stats.workdays_so_far}",
        f"Todo posted by Haseeb himself: {stats.self_posted}",
        f"Human intervention: {rate}",
        f"Average quality score: {score}",
        f"Missed days: {stats.missed_days} (workdays with no posted insight)",
        f"Failed job runs: {stats.failed_runs}",
    ]
    return _safe("\n".join(lines))
