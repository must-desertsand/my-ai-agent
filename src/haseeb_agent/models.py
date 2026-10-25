"""Dataclasses shared by all modules."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Kind = Literal["insight", "feedback", "todo", "message"]
Status = Literal["drafted", "delivered", "posted", "skipped", "failed", "expired"]

# The three things the agent drafts on its own: the weekly metrics count these. A `message` item (the dashboard's
# composer, or `say:` in the DM) is Haseeb's own message, polished: it is never counted there.
KINDS: tuple[str, ...] = ("insight", "feedback", "todo")
STATUSES: tuple[str, ...] = ("drafted", "delivered", "posted", "skipped", "failed", "expired")


@dataclass(frozen=True)
class FileRef:
    id: str
    name: str
    mimetype: str


# A channel Haseeb is a member of (docs/superpowers/specs/2026-10-22-post-channel-design.md). It lives here, with the other shared
# values, so the page parser can build one without importing the gateway; `channels` re-exports it. The id and the name come from
# Slack, so they are checked wherever they come in (the page parser, the cached list) by these two rules and nothing looser:
#   an id is a public or private channel's: a C or a G, then 8 to 12 capitals and digits (never a D... DM or a U... user);
#   a name is a Slack channel name, ASCII only: lower-case letters, digits, `-`, `_` and `.`, at most 80 characters.
# Both are used with `fullmatch`.
CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{8,12}")
CHANNEL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    private: bool


@dataclass
class Msg:
    ts: str
    user_id: str
    user_name: str
    text: str
    thread_ts: str | None
    reply_count: int
    latest_reply: str | None
    files: list[FileRef] = field(default_factory=list)
    is_bot: bool = False


@dataclass
class Item:
    id: str
    kind: Kind
    day: date
    status: Status
    body: str
    title: str
    dm_ts: str | None
    source_ts: str | None
    score: float | None
    models: dict[str, str]
    attempts: int
    edited: bool
    created_at: datetime
    acted_at: datetime | None


# Dashboard records. Slack text is untrusted and private, so the fields that hold it are kept out of repr():
# a stray log line or traceback must not carry message text (the M6 rule).
MESSAGE_KINDS: tuple[str, ...] = ("huddle_notes", "todo", "insight", "feedback", "other")
COMMAND_STATES: tuple[str, ...] = ("queued", "running", "done", "failed")
COMMAND_VERBS: tuple[str, ...] = ("post", "edit", "shorter", "skip", "attach", "cancel", "compose")
PERSON_COLORS = 12  # size of the dashboard's person palette


@dataclass
class Event:
    id: int
    job: str
    step: str
    model: str | None
    started: datetime
    ended: datetime | None
    ok: bool | None
    kind: str | None


@dataclass
class StoredMessage:
    channel: str
    ts: str
    thread_ts: str | None
    user_id: str
    user_name: str
    text: str = field(repr=False)
    kind: str
    day: date
    topic: str | None


@dataclass
class Person:
    user_id: str
    name: str
    color: int


@dataclass
class PageCommand:
    id: int
    item_id: str
    verb: str
    arg: str | None = field(repr=False)
    shown_hash: str
    state: str
    result: str | None = field(repr=False)
    created_at: datetime


# Model usage (docs/superpowers/specs/2026-10-21-usage-tab-design.md): one row per model call, never any text.
USAGE_PROVIDERS: tuple[str, ...] = ("claude", "litellm", "codex")
COST_BASES: tuple[str, ...] = ("list", "proxy", "subscription", "unknown")
USAGE_SOURCES: tuple[str, ...] = ("live", "backfill")
# What a row may hold (review M8: "no text" is the table's own rule, not only its writers'): the identifier columns (model,
# job, step, ref) take these characters and at most 120 of them, so no sentence fits; every other column is a number, a time
# or a word of a fixed vocabulary. Checked when a row is inserted (store._usage_values); a row that fails is rejected.
USAGE_IDENT = re.compile(r"[A-Za-z0-9._:/@-]{1,120}")
USAGE_MAX_COUNT = 2**62          # a token count or a wall time above this is clamped by the writers and rejected by the store
USAGE_MAX_COST = 1e9             # dollars
# A backfilled row from a transcript with no final figures (no cost-state line) lives under this prefix of its ref, apart from
# the final rows of the same run: it is recomputed on every run of the backfill and deleted once the final rows are in, and the
# Usage tab marks what it counts with "≈" (review M1).
APPROX_REF_PREFIX = "approx:"
# The job of a backfilled run that is not confidently inferred (a Slack gateway run: the transcript cannot say which job asked).
# The tab shows it as "Earlier runs (job unknown)", never as Slack (review M3).
UNKNOWN_JOB = "unknown"
# The step of a backfilled row that is a whole day's total for a model, from the proxy's daily summary: it adds cost to the
# Usage tab, and is not a call (the summary says how much was spent, not on how many calls, nor with how many tokens).
DAILY_TOTAL_STEP = "daily_total"


@dataclass
class UsageRow:
    """One model call. Token counts are normalised so the columns add up across providers: `input_tokens` is the input
    the model read fresh (not from the cache), the cached part is in `cache_read_tokens` and the part written to the cache
    in `cache_write_tokens`; the whole prompt is the sum of the three. A count the provider did not give is None, and so
    is a cost nobody reported (`cost_basis` then says why: unknown, or subscription for a plan with no per-call price)."""
    at: datetime
    provider: str
    model: str
    job: str = "other"
    step: str = "other"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    cost_basis: str = "unknown"
    latency_ms: int | None = None
    ok: bool = True
    source: str = "live"
    ref: str | None = None
    id: int | None = None
