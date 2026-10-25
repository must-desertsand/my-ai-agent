"""The dashboard's JSON API (Task 7). Every route here sits behind Task 6's security middleware (session cookie plus
`X-Session-Token`) and is registered before the static catch-all.

Rules this module keeps:
  - JSON only, `cache-control: no-store`; the two deck routes return bytes, which the page fetches with its header
    and shows through blob: URLs (D11);
  - the dashboard never calls Slack, claude or LiteLLM: its only DB writes are `page_commands` and `labels` rows,
    the pause switch, the spawn records of the run controls (it starts allowlisted jobs as detached processes of
    their own, see run_job) and the request for a new list of his channels (two cursors, `channels.ask_refresh`: the
    tick reads it, the dashboard only asks);
  - errors are short fixed strings that never echo the caller's input;
  - ids are validated against the store BEFORE any file path is built, and every path is resolved and checked to
    sit inside the decks dir;
  - Slack text is returned as plain JSON strings; the frontend renders it with textContent only;
  - an empty database gives empty answers, never an error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import subprocess
import threading
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import channels, controls
from ..commands import CHANNEL_KINDS, LEADERSHIP_ONLY, channel_label, held_at, post_failed_key
from ..config import Settings
from ..metrics import week_stats
from ..models import CHANNEL_ID, MESSAGE_KINDS, Event, PageCommand, StoredMessage
from ..offline import guard
from ..page_commands import body_hash, expire_for_pause
from ..render import Idea, message_pings
from ..store import Store
from ..usage_report import RANGES as USAGE_RANGES
from ..usage_report import build as build_usage

log = logging.getLogger(__name__)

NO_STORE = {"cache-control": "no-store"}
LOGS_DIR = Path.home() / "Library" / "Logs" / "haseeb-agent"
RUNNING_MAX = timedelta(minutes=15)
AWAKE_WITHIN = timedelta(minutes=3)
INSIGHTS_AT = time(8, 15)
LOG_TAIL_LINES = 2000
LOG_TAIL_BYTES = 1_000_000
LOG_RETURN = 200
LOG_TEXT_MAX = 500
MAX_BODY = 20_000
MAX_Q = 200
EDIT_MAX = 2000
HEARTBEAT_S = 15
HOLD_FRESH = timedelta(minutes=2)  # a tick stamps the huddle hold every minute: a stamp this fresh means it holds now
THUMB_PX = 480
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

IDENTITY_FRESH = timedelta(hours=6)  # a Slack check older than this is "check again" (a Claude account may have changed)

TEAM_DAYS = 7          # "Team posts": the last 7 days, today included
TEAM_LIMIT = 100
TEAM_TEXT_MAX = 500    # the list shows a post's opening only

VERBS: dict[str, tuple[str, ...]] = {
    "insight": ("post", "edit", "shorter", "skip", "attach"),
    "feedback": ("post", "edit", "shorter", "skip"),
    "todo": ("post", "skip", "cancel", "edit", "shorter"),
    "message": ("post", "edit", "shorter", "skip"),  # Haseeb's own message, polished (the composer, `say:`)
}
NO_HASH = ("cancel", "attach")


def jr(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=NO_STORE)


def err(message: str, status: int = 400) -> JSONResponse:
    return jr({"error": message}, status)


def run_sips(argv: list[str]) -> int:
    """Run sips (the default thumbnail runner). Tests inject their own, so they never reach this."""
    guard("sips")
    return subprocess.run(argv, capture_output=True, timeout=30, check=False).returncode


def default_spawn(args: list[str]) -> None:
    """Start an allowlisted job (`controls.spawn_args`) as a detached process: cli.spawn_job, which checks the offline
    guard first. Tests inject their own, so they never reach this."""
    from .. import cli  # late: cli imports the dashboard app
    cli.spawn_job(args)


@dataclass
class ApiConfig:
    settings: Settings
    store_factory: Callable[[], Store]
    kick: Callable[[], int | None]  # launchctl's exit code (None: not reported, as a test's recorder); see run_job
    clock: Callable[[], float]
    logs_dir: Path = LOGS_DIR
    decks_dir: Path | None = None
    sips: Callable[[list[str]], int] = run_sips
    spawn: Callable[[list[str]], None] = default_spawn
    started_at: float | None = None  # when this dashboard process started (on `clock`); a Slack check older than it is stale
    run_lock: threading.Lock = field(default_factory=threading.Lock)  # one /api/run at a time in this process
    # The stream: poll interval, an optional cap on polls (tests read a finite stream) and the sleep it awaits.
    stream_poll_s: float = 1.0
    stream_max_polls: int | None = None
    stream_sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.settings.tz)

    def decks_root(self) -> Path:
        from ..jobs.decks import decks_root
        return Path(self.decks_dir or decks_root(self.settings))


# ---------------------------------------------------------------- shared helpers

def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(text: str, tz) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _event_json(e: Event) -> dict:
    return {"id": e.id, "job": e.job, "step": e.step, "model": e.model, "started": _iso(e.started),
            "ended": _iso(e.ended), "ok": e.ok, "kind": e.kind}


def _command_json(c: PageCommand) -> dict:
    return {"id": c.id, "item_id": c.item_id, "verb": c.verb, "state": c.state, "result": c.result}


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _next_at(now: datetime, t: time, settings: Settings, *, strictly_after: bool = False) -> datetime:
    """The next workday occurrence of local time `t` (at or after now)."""
    local = now.astimezone(settings.tz)
    d = local.date()
    for _ in range(8):
        if d.weekday() in settings.workdays:
            at = datetime.combine(d, t, settings.tz)
            if at > local or (not strictly_after and at == local):
                return at
        d += timedelta(days=1)
    return datetime.combine(d, t, settings.tz)


# ---------------------------------------------------------------- /api/live

def _icon(e: Event) -> str:
    if e.step in ("design", "render", "upload") or e.job == "decks":
        return "deck"
    return "model" if e.model else "job"


def live(cfg: ApiConfig) -> dict:
    s, now = cfg.settings, cfg.now()
    store = cfg.store_factory()
    try:
        last = _parse_dt(store.get_cursor("tick_seen") or "", s.tz)  # written by every tick, even a quiet one
        awake = last is not None and timedelta(0) <= now - last < AWAKE_WITHIN

        op = store.last_open_event()
        current = None
        if op is not None and op.started.astimezone(s.tz) <= now and now - op.started.astimezone(s.tz) < RUNNING_MAX:
            current = {"job": op.job, "step": op.step, "model": op.model,
                       "elapsed_s": int((now - op.started.astimezone(s.tz)).total_seconds())}

        today = []
        for e in store.recent_events(200):
            st = e.started.astimezone(s.tz)
            if st.date() != now.date():
                continue
            today.append({"at": _iso(st), "icon": _icon(e), "title": f"{e.job} {e.step}",
                          "detail": e.model or e.kind or "", "ok": e.ok})
            if len(today) >= 60:
                break

        upcoming = _upcoming(cfg, store, now)
    finally:
        store.close()
    try:
        free = round(shutil.disk_usage(Path(s.db_path).resolve().parent).free / 1e9, 1)
    except OSError:
        free = None
    awake = awake or current is not None  # a long tick holds the lock, so tick_seen goes stale while it runs
    return {"agent": {"running": current is not None, "last_tick": _iso(last), "mac_awake": awake},
            "disk_free_gb": free, "now": current, "upcoming": upcoming, "today": today}


def _upcoming(cfg: ApiConfig, store: Store, now: datetime) -> list[dict]:
    from .. import cli  # late: cli imports the dashboard app
    s = cfg.settings
    out = []
    try:
        state = cli.tick_state(store, s, now)
        last_fb = _parse_dt(store.get_cursor("tick_last:run_feedback") or "", s.tz)
        at = now if last_fb is None else max(now, last_fb + timedelta(seconds=cli.interval_s("run_feedback", last_fb, s, state)))
        if cli.huddle_open(at, s, state):  # no feedback run inside the huddle window: it waits for its end
            at = datetime.combine(at.astimezone(s.tz).date(), state.huddle_end, s.tz)
        out.append({"label": "Feedback scan", "at": at.isoformat()})
    except Exception:  # noqa: BLE001 - a countdown must never break the page
        pass
    out.append({"label": "Huddle", "at": _next_at(now, s.huddle_time, s).isoformat()})
    out.append({"label": "Insights", "at": _next_at(now, INSIGHTS_AT, s).isoformat()})
    out.sort(key=lambda u: datetime.fromisoformat(u["at"]))
    return out


# ---------------------------------------------------------------- /api/stream (SSE wire format, read by fetch)

def _sse(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def stream(cfg: ApiConfig, request: Request) -> Response:
    try:
        since = int(request.query_params["since"]) if "since" in request.query_params else None
        if since is not None and not 0 <= since < 2**63:
            return err("bad request")
    except ValueError:
        return err("bad request")

    def poll(last_id: int | None, seen: dict | None):
        """One poll on a thread of its own, with its own connection (opened and closed here)."""
        store = cfg.store_factory()
        try:
            if seen is None:  # the baseline
                newest = store.recent_events(1)
                base = since if since is not None else (newest[0].id if newest else 0)
                return base, {c.id: c.state for c in store.recent_page_commands(50)}, []
            out, new_seen = [], dict(seen)
            for e in store.events_since(last_id):
                last_id = e.id
                out.append(_sse("event", _event_json(e)))
            for c in store.recent_page_commands(50):
                if new_seen.get(c.id) != c.state:
                    new_seen[c.id] = c.state
                    out.append(_sse("command", _command_json(c)))
            return last_id, new_seen, out
        finally:
            store.close()

    async def gen():
        last_id, seen, _ = await run_in_threadpool(poll, None, None)
        last_sent = cfg.clock()
        yield b": connected\n\n"
        polls = 0
        while True:
            if await request.is_disconnected():
                return
            if cfg.stream_max_polls is not None and polls >= cfg.stream_max_polls:
                return
            await cfg.stream_sleep(cfg.stream_poll_s)
            if await request.is_disconnected():
                return
            polls += 1
            last_id, seen, chunks = await run_in_threadpool(poll, last_id, seen)
            for chunk in chunks:
                yield chunk
            now = cfg.clock()
            if chunks:
                last_sent = now
            elif now - last_sent >= HEARTBEAT_S:
                last_sent = now
                yield b": heartbeat\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={**NO_STORE, "x-accel-buffering": "no"})


# ---------------------------------------------------------------- /api/inbox

def _body_text(kind: str, body: str) -> str:
    """The draft as plain text: an insight's body is JSON; the page shows its readable fields. `draft_text` reads the
    body by its own schema version: a v1 insight shows its title, what happened, why it matters and Silicon Valley (as
    it always did), a v2 idea its header ("title · price"), the idea, why now, who pays, the build and the first step.
    A body that cannot be read, or has any other version, has no text."""
    if kind != "insight":
        return body
    from ..jobs.insights import draft_text  # late, like the other jobs this module reads
    try:
        return draft_text(json.loads(body))
    except ValueError:  # not JSON
        return ""


def _format(kind: str, body: str) -> str | None:
    """What an item's card calls itself: "idea" for an insight whose body is a v2 idea, "insight" for a v1 one (and for a
    body that cannot be read, the card's old name), and None for every other kind. The item's kind is "insight" either
    way, so its verbs, hash and deck are the same; only the label differs."""
    if kind != "insight":
        return None
    from ..jobs.insights import draft_from  # late, see _body_text
    try:
        return "idea" if isinstance(draft_from(json.loads(body)), Idea) else "insight"
    except (ValueError, KeyError, TypeError):  # not JSON, another version, or a wrong shape
        return "insight"


_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # date.weekday(); English whatever the locale
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_TAIL_NUMBER = re.compile(r"(\d+(?:\.\d+)?)$")


def _day_label(day: date, today: date) -> str:
    """The header of a day in the inbox: "Today · Wed 21 Oct", "Yesterday · Tue 20 Oct", then "Mon 19 Oct"; the year only
    when it is not `today`'s. Worked out from dates alone: `day` is the item's own day and `today` is the day in the
    settings timezone (Kuwait), so the browser's clock and zone never decide it."""
    plain = f"{_DAY_NAMES[day.weekday()]} {day.day} {_MONTH_NAMES[day.month - 1]}"
    if day.year != today.year:
        plain += f" {day.year}"
    if day == today:
        return f"Today · {plain}"
    if day == today - timedelta(days=1):
        return f"Yesterday · {plain}"
    return plain


def _micros(text: str | None) -> int | None:
    """A Slack ts, or any number, as whole microseconds; None when it is not a usable, non-negative number."""
    try:
        d = Decimal(str(text))
    except (InvalidOperation, ValueError):
        return None
    return int(d * 1_000_000) if d.is_finite() and d >= 0 else None


def _inbox_order(it) -> str:
    """Where a draft sits among the inbox's drafts, as a string that sorts newest first when the keys are compared
    biggest first: the item's own day, then when its DM went out (the Slack ts) if that is a usable time, else the number
    its id ends with (insight-...-6 is newer than insight-...-5), then the id. The page puts a draft in its place by this
    key alone, so one that arrives live, or one that is still fading out, needs no rules of its own."""
    ts, tail = _micros(it.dm_ts), _TAIL_NUMBER.search(it.id)
    ts_part = 0 if ts is None else ts
    tail_part = (_micros(tail.group(1)) or 0) if tail else 0
    return f"{it.day.isoformat()}|{int(ts is not None)}|{ts_part:020d}|{tail_part:024d}|{it.id}"


def _post_may_have_gone_out(store: Store, item_id: str) -> bool:
    """An attempt to post the draft may have reached Slack: its failure time is on record (`post_failed_at:`, C1, written first
    whenever a post may have gone out), or an attempt is in flight whose outcome was never recorded (`inflight:`, CB2, which the next
    run turns into a failure time). A `post` row that merely ended `failed` is no evidence: a refusal of ours (a channel that is not
    his, a pinned retry, a draft that changed, an expired or paused click) sent nothing and leaves neither cursor, and a row that
    followed a real attempt has one of them."""
    return bool(store.get_cursor(post_failed_key(item_id))) or bool(store.get_cursor(f"inflight:{item_id}"))


def inbox(cfg: ApiConfig) -> list[dict]:
    """The open drafts, grouped by day: the newest day first (the item's own day), and inside a day the newest first (see
    `_inbox_order`). Each row carries its `day`, the header text of that day (`day_label`, in the settings timezone) and
    its `order` key. Each also carries its command state, so the page's cards survive a reload: `pending` (a queued or
    running page command), `last_result` (the newest settled one) and `post_may_have_gone_out` (the item failed after a
    post that may have reached the channel, C1: posting again needs an explicit check; only an attempt that may have reached Slack
    counts, see `_post_may_have_gone_out`, never a click we refused ourselves) with `post_channel_id`, the channel an
    idea's or a message's post was recorded to before it was sent (a retry after a possible post is pinned to it), and
    `post_channel`, that channel said as a person would ("#survive-ai", "the leadership channel", "channel C093L3Y8UM6" for one
    he has left), so the card's warning says where to check. Both are null when no channel is recorded, and for every kind but
    an idea and a message. An insight also carries its `deck`
    (see `_deck_view`), so its card can show the deck that goes with it, and its `format` ("idea" or "insight", see
    `_format`), so its card can call itself by the right name (every other kind has `format` None); a feedback draft its
    `review`: the one line the site review left ("Reviewed: <host> (desktop + phone)" or "Couldn't review <host>:
    <reason>"), or None, and its `variant`: "followback" (an answer to a teammate's reply to his feedback),
    "insight_reply" (a reply on his insight), "own_post" (a post of their own that landed in his insight's thread, a
    normal feedback card) or None (a plain draft)."""
    from ..jobs.feedback import variant_of  # late: the job modules are big
    store = cfg.store_factory()
    try:
        out = []
        today = cfg.now().date()
        keyed = sorted(((_inbox_order(it), it) for it in store.open_dm_items(today - timedelta(days=2))),
                       key=lambda pair: pair[0], reverse=True)
        for order, it in keyed:
            due = store.get_cursor(f"autopost:{it.id}") if it.kind == "todo" else None
            # a message item (the composer) also shows what the polish changed and Haseeb's own words; never in the post
            note = (store.get_cursor(f"message_note:{it.id}") or None) if it.kind == "message" else None
            original = (store.get_cursor(f"message_raw:{it.id}") or None) if it.kind == "message" else None
            pings = message_pings(it.body, original) if it.kind == "message" else []
            review = (store.get_cursor(f"feedback_review:{it.id}") or None) if it.kind == "feedback" else None
            pend = store.last_page_command(it.id, ("queued", "running"))
            if pend is not None and pend.verb == "compose":  # the composer's own row ends as the item appears
                pend = None
            last = store.last_page_command(it.id, ("done", "failed"))
            recorded = channels.recorded_post_channel(store, it.id) if it.kind in CHANNEL_KINDS else None
            unsure = it.status == "failed" and _post_may_have_gone_out(store, it.id)
            out.append({"item_id": it.id, "kind": it.kind, "title": it.title, "body": _body_text(it.kind, it.body),
                        "format": _format(it.kind, it.body),
                        "day": it.day.isoformat(), "day_label": _day_label(it.day, today), "order": order,
                        "shown_hash": body_hash(it), "status": it.status, "autopost_at": due or None,
                        "note": note, "original": original, "pings": pings, "review": review,
                        "variant": variant_of(it.id) if it.kind == "feedback" else None,
                        "deck": _deck_view(cfg, store, it) if it.kind == "insight" else None,
                        "pending": None if pend is None else {"command_id": pend.id, "verb": pend.verb,
                                                              "state": pend.state},
                        "last_result": None if last is None else {"command_id": last.id, "verb": last.verb,
                                                                  "state": last.state, "result": last.result},
                        "post_may_have_gone_out": unsure,
                        "post_channel_id": recorded,
                        "post_channel": None if recorded is None else channel_label(store, cfg.settings, recorded)})
        return out
    finally:
        store.close()


# ---------------------------------------------------------------- history

def permalink(settings: Settings, m: StoredMessage) -> str | None:
    """Built from the channel id and ts only, never from message text."""
    if not SAFE_ID.match(m.channel) or not SAFE_ID.match(m.ts):
        return None
    url = f"{settings.workspace_url}/archives/{m.channel}/p{m.ts.replace('.', '')}"
    if m.thread_ts and m.thread_ts != m.ts:
        if not SAFE_ID.match(m.thread_ts):
            return None
        url += f"?thread_ts={quote(m.thread_ts, safe='')}&cid={quote(m.channel, safe='')}"
    return url


def _msg_json(settings: Settings, colors: dict[str, int], m: StoredMessage, fb: dict[str, str] | None = None) -> dict:
    """`feedback` is the draft-feedback state of a teammate's top-level post (none, drafting, or the status of its
    feedback item) and null for any other message: the History page offers "Draft feedback" where it says "none"."""
    return {"ts": m.ts, "channel": m.channel, "thread_ts": m.thread_ts, "user_id": m.user_id,
            "user_name": m.user_name, "color": colors.get(m.user_id, 0), "text": m.text, "kind": m.kind,
            "topic": m.topic, "day": m.day.isoformat(), "permalink": permalink(settings, m),
            "mine": m.user_id == settings.haseeb_id, "feedback": (fb or {}).get(m.ts)}


def _feedback_states(cfg: ApiConfig, store: Store, msgs: list[StoredMessage]) -> dict[str, str]:
    """ts -> feedback state, for the teammates' top-level posts among `msgs` (one query for all of them)."""
    from ..jobs.feedback import feedback_states, manual_refusal
    posts = [m.ts for m in msgs if manual_refusal(cfg.settings, m) is None]
    if not posts:
        return {}
    now = cfg.now()
    drafting = controls.job_state(store, cfg.settings, controls.BY_NAME["feedback"], now).arg
    return feedback_states(store, posts, drafting, now)


def _ts_key(m: StoredMessage) -> float:
    try:
        return float(m.ts)
    except ValueError:
        return 0.0


def history(cfg: ApiConfig, params) -> JSONResponse:
    s = cfg.settings
    view = params.get("view", "day")
    if view not in ("day", "person", "topic"):
        return err("bad view")
    kind = params.get("kind") or None
    if kind is not None and kind not in MESSAGE_KINDS:
        return err("bad kind")
    q = params.get("q", "")
    if len(q) > MAX_Q:
        return err("query too long")
    person = params.get("person") or None
    if person is not None and not ID_RE.match(person):
        return err("bad person")
    week = None
    explicit_week = bool(params.get("week"))
    if params.get("week"):
        try:
            week = _monday(date.fromisoformat(params["week"]))
        except ValueError:
            return err("bad week")
    searching = bool(q.strip())
    if view == "day" and week is None and not searching:
        week = _monday(cfg.now().date())

    store = cfg.store_factory()
    try:
        colors = {p.user_id: p.color for p in store.people()}
        if searching:  # the filters go into the store's query, so the limit applies after them. A people chip
            # sends an exact user id (only a typed person: also matches the start of a name)
            msgs = store.search(q.strip() + (f" kind:{kind}" if kind else ""), limit=500, user_id=person)
        elif week is not None:
            msgs = [m for i in range(7) for m in store.messages(day=week + timedelta(days=i), user_id=person,
                                                                kind=kind, limit=500)]
        else:
            msgs = store.messages(user_id=person, kind=kind, limit=500)
        if week is not None and (explicit_week or not searching):
            msgs = [m for m in msgs if week <= m.day < week + timedelta(days=7)]
        fb = _feedback_states(cfg, store, msgs)
    finally:
        store.close()
    j = lambda m: _msg_json(s, colors, m, fb)  # noqa: E731

    if view == "day":
        return jr({"view": "day", "week": week.isoformat() if week else None, "groups": _day_groups(msgs, j)})
    if view == "person":
        by: dict[str, list[StoredMessage]] = defaultdict(list)
        for m in msgs:
            by[m.user_id].append(m)
        groups = [{"person": {"user_id": uid, "name": ms[0].user_name, "color": colors.get(uid, 0)},
                   "count": len(ms), "messages": [j(m) for m in ms]} for uid, ms in by.items()]
    else:
        bt: dict[str | None, list[StoredMessage]] = defaultdict(list)
        for m in msgs:
            bt[m.topic].append(m)
        groups = [{"topic": t, "count": len(ms), "messages": [j(m) for m in ms]} for t, ms in bt.items()]
    groups.sort(key=lambda g: -g["count"])
    return jr({"view": view, "week": week.isoformat() if week else None, "groups": groups})


def _day_groups(msgs: list[StoredMessage], j) -> list[dict]:
    days: dict[date, list[StoredMessage]] = defaultdict(list)
    for m in msgs:
        days[m.day].append(m)
    groups = []
    for day in sorted(days, reverse=True):
        ms = sorted(days[day], key=_ts_key)
        insights = [m for m in ms if m.kind == "insight"]
        by_ts = {m.ts: {"msg": j(m), "feedback": []} for m in insights}
        other = []
        for m in ms:
            if m.kind == "feedback" and m.thread_ts in by_ts:
                by_ts[m.thread_ts]["feedback"].append(j(m))
            elif m.kind not in ("huddle_notes", "todo", "insight"):
                other.append(j(m))
        notes = [m for m in ms if m.kind == "huddle_notes"]
        groups.append({
            "day": day.isoformat(),
            # each notes message and each todo comes whole (ts, permalink, mine, ...): the page links, labels and
            # outlines them one by one
            "huddle": {"notes": "\n\n".join(m.text for m in notes), "messages": [j(m) for m in notes],
                       "todos": [{**j(m), "user": m.user_name} for m in ms if m.kind == "todo"]},
            "insights": [by_ts[m.ts] for m in insights],
            "other": other,
        })
    return groups


def people(cfg: ApiConfig) -> list[dict]:
    store = cfg.store_factory()
    try:
        return [asdict(p) for p in store.people()]
    finally:
        store.close()


def weeks(cfg: ApiConfig) -> list[dict]:
    store = cfg.store_factory()
    try:
        counts = store.message_days()
    finally:
        store.close()
    if not counts:
        return []
    first, last = _monday(max(min(counts), cfg.now().date() - timedelta(days=120))), _monday(max(max(counts), cfg.now().date()))
    out, w = [], first
    while w <= last:
        days = [counts.get(w + timedelta(days=i), 0) for i in range(7)]
        out.append({"week": w.isoformat(), "days": days, "total": sum(days)})
        w += timedelta(days=7)
    return out


# ---------------------------------------------------------------- decks

def _deck_state(store: Store, item_id: str) -> dict:
    from ..jobs.decks import _state
    return _state(store, item_id)


def _safe_pdf(cfg: ApiConfig, raw) -> Path | None:
    """The PDF a deck record names: resolved, and it must sit inside the decks dir and be a plain .pdf file."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        base = cfg.decks_root().resolve()
        pdf = Path(raw).resolve()
        if not pdf.is_relative_to(base) or not pdf.is_file() or pdf.suffix != ".pdf":
            return None
    except (OSError, ValueError):
        return None
    return pdf


def _deck_pdf(cfg: ApiConfig, store: Store, item_id: str) -> Path | None:
    """The item's deck PDF. The id is checked against the store first; the stored path is resolved and must sit
    inside the decks dir and be a plain file. While the deck for a revised text is made, it is the deck of the text
    before (the one in his DM) that the deck job still remembers and whose file is still there."""
    if not ID_RE.match(item_id):
        return None
    item = store.get_item(item_id)
    if item is None or item.kind != "insight":
        return None
    from ..jobs.decks import _previous
    st = _deck_state(store, item.id)
    if st.get("state") == "done":
        return _safe_pdf(cfg, st.get("pdf"))
    prev = _previous(st)
    return _safe_pdf(cfg, prev.get("pdf")) if prev else None


_PAGES: dict[tuple[str, float], int | None] = {}


def _page_count(pdf: Path) -> int | None:
    try:
        key = (str(pdf), pdf.stat().st_mtime)
        if key in _PAGES:
            return _PAGES[key]
        with open(pdf, "rb") as f:
            data = f.read(20_000_000)  # at most 20 MB
    except OSError:
        return None
    n = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data)) or None
    if len(_PAGES) > 256:
        _PAGES.clear()
    _PAGES[key] = n
    return n


def _attachable(cfg: ApiConfig, store: Store, item) -> bool:
    """R50: exactly what `attach` (jobs.decks.attach_now) accepts: a posted insight whose post's ts is known and whose
    deck is for the posted text, with no attach tried yet or one the deck job held back as late (the deck finished
    after the post). A started, failed or done attach is never offered again (R16)."""
    from ..jobs.decks import current_deck
    return (item.status == "posted" and bool(store.get_cursor(f"post_ts:{item.id}"))
            and store.get_cursor(f"attach:{item.id}") in (None, "late")
            and current_deck(store, cfg.settings, item, cfg.decks_root()) is not None)


def _factcheck(model: str | None) -> str | None:
    """What the deck job's model label says about the fact check: the designer's deck passed it ("codex <model>",
    repaired or not) or it refused it ("template (fact-check)"). The template, or the template with a Codex cover (a
    bare "codex"), had none: None."""
    if model is None:
        return None
    if "fact-check" in model:
        return "refused"
    return "passed" if model.startswith("codex ") else None


def _deck_at(cfg: ApiConfig, st: dict) -> str | None:
    """When the deck reached Haseeb's DM (the deck job's `dm_at`, R50: epoch seconds), or None."""
    try:
        return datetime.fromtimestamp(float(st["dm_at"]), cfg.settings.tz).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def _deck_facts(cfg: ApiConfig, store: Store, item_id: str, st: dict) -> dict:
    """What a finished deck is, from its stored state and its file: the one place the Decks page and an insight's card
    both read it (the model label, the page count, what the fact check said, whether the PDF is still there, and when
    the deck reached Haseeb's DM)."""
    pdf = _deck_pdf(cfg, store, item_id)
    model = st.get("model") if isinstance(st.get("model"), str) else None
    return {"model": model, "pages": _page_count(pdf) if pdf else None, "factcheck": _factcheck(model),
            "available": pdf is not None, "at": _deck_at(cfg, st)}


def _deck_view(cfg: ApiConfig, store: Store, item) -> dict:
    """The deck of an insight as its card in "Waiting for you" shows it, for the insight's CURRENT text (the text hash
    the deck job and R50's attach use), never for whatever deck the item last had. `state` is where the deck job is:
      - `done`: built for the current text, uploaded to his DM, and its PDF is still on disk (`available`);
      - `pending`: the deck job still has an attempt to make or is making it (today's insight, delivered or still
        being delivered), the last allowed attempt included: the card says "Designing…";
      - `gave_up`: no deck will come (the attempts are used up, the insight is not one the deck job works on any more,
        or the PDF is gone);
      - `off`: decks are turned off in the config and none was built.
    `stale` is a `pending` deck that is made because the text was EDITED after a deck was made for it: the card says
    "New deck being made for your edit…", and `model`, `pages`, `factcheck` and `available` describe the deck of the
    previous text (still in his DM, and still on disk unless the new build has replaced its file), which the card shows
    as that. `dm_at` is when the deck for the current text reached his DM (R50: only a deck uploaded before a post is
    attached to it); the previous text's deck never has one here."""
    from ..jobs.decks import _dm_work, _insight_meta, content_hash
    st = _deck_state(store, item.id)
    meta = _insight_meta(item)
    # a revised text needs a new deck; an unreadable text has none that R50 would attach (current_deck agrees)
    current = meta is not None and st.get("sha") == content_hash(meta["insight"])
    facts = _deck_facts(cfg, store, item.id, st) if st.get("state") == "done" else None
    if facts is not None and facts["available"] and current:
        return {"state": "done", "stale": False, "model": facts["model"], "pages": facts["pages"],
                "factcheck": facts["factcheck"], "available": True, "dm_at": facts["at"]}
    nothing = {"stale": False, "model": None, "pages": None, "factcheck": None, "available": False, "dm_at": None}
    if not cfg.settings.decks_enabled:
        return {"state": "off", **nothing}
    # a draft whose later DM parts are still going out is the deck job's as soon as it is delivered
    job_item = replace(item, status="delivered") if item.status == "drafted" else item
    work = _dm_work(store, job_item) if item.day == cfg.now().astimezone(cfg.settings.tz).date() else None
    if work is None:  # the job marks `gave_up` itself, at the top of its next pass: the card never guesses
        return {"state": "gave_up", **nothing}
    prev = work[2].get("prev")  # the job's own working state carries the deck of the text before an edit
    if not isinstance(prev, dict):
        return {"state": "pending", **nothing}
    old = _deck_facts(cfg, store, item.id, prev)
    if not old["available"]:  # the new build has replaced its file: nothing of it to show, only that an edit is waiting
        return {"state": "pending", **{**nothing, "stale": True}}
    return {"state": "pending", "stale": True, "model": old["model"], "pages": old["pages"],
            "factcheck": old["factcheck"], "available": True, "dm_at": None}


def decks(cfg: ApiConfig) -> list[dict]:
    """The finished decks of the last 14 days' insights, newest first. `pending` is an attach (or any page
    command) still queued or running for the item, so the page keeps its button disabled across a reload."""
    store = cfg.store_factory()
    try:
        out = []
        for it in store.recent_items("insight", 14, today=cfg.now().date()):
            st = _deck_state(store, it.id)
            if st.get("state") != "done":
                continue
            facts = _deck_facts(cfg, store, it.id, st)
            pend = store.last_page_command(it.id, ("queued", "running"))
            out.append({
                "item_id": it.id, "title": it.title, "model": facts["model"], "seconds": None,
                "pages": facts["pages"], "images": None,
                "factcheck": facts["factcheck"],
                "attachable": facts["available"] and _attachable(cfg, store, it),
                "available": facts["available"],
                "day": it.day.isoformat(), "at": facts["at"],
                "pending": None if pend is None else {"command_id": pend.id, "verb": pend.verb, "state": pend.state},
            })
        return out
    finally:
        store.close()


def deck_pdf(cfg: ApiConfig, item_id: str) -> Response:
    store = cfg.store_factory()
    try:
        pdf = _deck_pdf(cfg, store, item_id)
    finally:
        store.close()
    if pdf is None:
        return err("not found", 404)
    try:
        data = pdf.read_bytes()
    except OSError:
        return err("not found", 404)
    return Response(data, media_type="application/pdf", headers={**NO_STORE, "content-disposition": "inline"})


def deck_thumb(cfg: ApiConfig, item_id: str) -> Response:
    store = cfg.store_factory()
    try:
        pdf = _deck_pdf(cfg, store, item_id)
    finally:
        store.close()
    if pdf is None:
        return err("not found", 404)
    thumb = pdf.with_name(pdf.stem + ".thumb.png")
    try:
        fresh = thumb.is_file() and not thumb.is_symlink() and thumb.stat().st_mtime >= pdf.stat().st_mtime
        if not fresh:
            thumb.unlink(missing_ok=True)
            code = cfg.sips(["sips", "-s", "format", "png", "--resampleHeightWidthMax", str(THUMB_PX),
                             str(pdf), "--out", str(thumb)])
            if code != 0:
                thumb.unlink(missing_ok=True)
                return err("not found", 404)
        if not thumb.is_file() or thumb.is_symlink() or not thumb.resolve().is_relative_to(cfg.decks_root().resolve()):
            return err("not found", 404)
        data = thumb.read_bytes()
    except Exception:  # noqa: BLE001 - sips missing, timed out, offline guard: no thumbnail
        return err("not found", 404)
    return Response(data, media_type="image/png", headers=NO_STORE)


# ---------------------------------------------------------------- logs

_TICK_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+(?:[+-]\d\d:\d\d|Z)?)\s+(\S+)\s*(.*)$")
_ERR_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)?\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+?):\s?(.*)$")
SLACK_JOBS = ("process_dm", "process_page", "deliver_pending", "run_todo", "run_feedback")
MODEL_SOURCES = ("litellm", "judge", "claude_cli")


def _tail(path: Path) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    return raw.decode("utf-8", "replace").splitlines()[-LOG_TAIL_LINES:]


def _parse_log(path: Path, tz) -> list[dict]:
    err_file = path.name.endswith(".err.log")
    out = []
    for line in _tail(path):
        if err_file:
            m = _ERR_LINE.match(line)
            if not m:
                continue  # a traceback or other continuation: skipped
            at = _parse_dt(m.group(1).replace(" ", "T"), tz)
            level, source, text = m.group(2), m.group(3).rsplit(".", 1)[-1], m.group(4)
        else:
            m = _TICK_LINE.match(line)
            if not m:
                continue
            at = _parse_dt(m.group(1), tz)
            source, text = m.group(2), m.group(3)
            words = f" {text.lower()} "
            level = "ERROR" if (text.lower().startswith(("fail", "error")) or " fail " in words
                                or " failed " in words or " error " in words) else "INFO"
            if path.name.startswith("tick") and text.startswith("fail"):
                level = "ERROR"
        if at is None:
            continue
        out.append({"at": at.isoformat(), "level": level, "source": source, "text": text[:LOG_TEXT_MAX], "_t": at})
    return out


def _matches(filt: str, row: dict) -> bool:
    src, text = row["source"].lower(), row["text"].lower()
    if filt == "all":
        return True
    if filt == "slack":
        return "slack" in src or "slack" in text or row["source"] in SLACK_JOBS
    if filt == "models":
        return src in MODEL_SOURCES or any(k in text for k in ("model", "litellm", "claude"))
    if filt == "decks":
        return "deck" in src or "deck" in text
    return row["level"] in ("WARNING", "ERROR", "CRITICAL") or " fail " in f" {text} "  # problems


def logs(cfg: ApiConfig, params) -> JSONResponse:
    filt = params.get("filter", "all")
    if filt not in ("all", "slack", "models", "decks", "problems"):
        return err("bad filter")
    q = params.get("q", "")
    if len(q) > 100:
        return err("query too long")
    before = None
    if params.get("before"):
        before = _parse_dt(params["before"], cfg.settings.tz)
        if before is None:
            return err("bad before")
    rows = []
    # dashboard.log and dashboard.err.log too: a job the run controls start is detached from the dashboard, so its lines
    # land there. The server's own warnings have no timestamp in these formats and are skipped by the parsers.
    for name in ("tick.log", "tick.err.log", "insights.log", "insights.err.log", "dashboard.log", "dashboard.err.log"):
        rows += _parse_log(cfg.logs_dir / name, cfg.settings.tz)
    rows.sort(key=lambda r: r["_t"])
    ql = q.lower()
    rows = [r for r in rows if _matches(filt, r) and (not ql or ql in r["text"].lower() or ql in r["source"].lower())
            and (before is None or r["_t"] < before)]
    return jr([{k: v for k, v in r.items() if k != "_t"} for r in rows[-LOG_RETURN:]])


# ---------------------------------------------------------------- score, actions, labels, commands

def _todo_deadline(cfg: ApiConfig, store: Store, item) -> tuple[datetime, bool]:
    """(the deadline, whether it is the 16:05 fallback). D16: 5 minutes after the huddle notes (the Slackbot notes
    message's ts, `notes:{todo id}`, from the todo job's _deliver_notes); without it, the todo window's end (16:05,
    WINDOW_END)."""
    from ..jobs.todo import WINDOW_END
    raw = store.get_cursor(f"notes:{item.id}")
    try:
        return datetime.fromtimestamp(float(raw), cfg.settings.tz) + timedelta(minutes=5), False
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.combine(item.day, WINDOW_END, cfg.settings.tz), True


def _todo_on_time(cfg: ApiConfig, store: Store, week_start: date, now: datetime) -> tuple[int, int, int]:
    """(on time, due, judged by 16:05) for the week's Next Todos. Only a workday with a todo item counts (no item: no
    huddle, nothing was due). It is due once its deadline (_todo_deadline) has passed, or once it is out; it is on
    time when it reached the huddle thread by then, by the agent (its auto-post, or Haseeb's `post`) or as his own
    post (`manual_todo:`). Days after today and non-workdays don't count. A read of the store only: metrics.py is
    unchanged."""
    tz = cfg.settings.tz
    on = due = by_1605 = 0
    for i in range(7):
        day = week_start + timedelta(days=i)
        if day.weekday() not in cfg.settings.workdays or day > now.date():
            continue
        items = store.items_for_day("todo", day)
        if not items:
            continue
        ok = passed = fallback = False
        for it in items:
            end, fell_back = _todo_deadline(cfg, store, it)
            fallback = fallback or fell_back
            out = it.status == "posted" or store.get_cursor(f"manual_todo:{it.id}") is not None
            at = it.acted_at if it.acted_at is None or it.acted_at.tzinfo else it.acted_at.replace(tzinfo=tz)
            ok = ok or (out and at is not None and at <= end)
            passed = passed or now >= end
        if ok or passed:
            due += 1
            on += ok
            by_1605 += fallback
    return on, due, by_1605


def score(cfg: ApiConfig, params) -> JSONResponse:
    """`Stats` per week, oldest first, plus `todo_on_time`, `todo_due` and `todo_by_1605` (see _todo_on_time), and
    `pending_today` (M4): true, for this week only, while today is a workday whose insight isn't posted yet. Stats
    count such a today as missed; the page counts it as neither posted nor missed until the day ends."""
    try:
        n = int(params.get("weeks", "8"))
    except ValueError:
        return err("bad weeks")
    if not 1 <= n <= 52:
        return err("bad weeks")
    now = cfg.now()
    today = now.date()
    start = _monday(today)
    store = cfg.store_factory()
    try:
        pending = today.weekday() in cfg.settings.workdays and not any(
            it.status == "posted" for it in store.items_for_day("insight", today))
        out = []
        for i in range(n - 1, -1, -1):
            st = week_stats(store, start - timedelta(weeks=i), workdays=cfg.settings.workdays, today=today)
            d = asdict(st)
            d["week_start"] = st.week_start.isoformat() if st.week_start else None
            d["todo_on_time"], d["todo_due"], d["todo_by_1605"] = _todo_on_time(cfg, store, start - timedelta(weeks=i),
                                                                                 now)
            d["pending_today"] = i == 0 and pending
            out.append(d)
        return jr(out)
    finally:
        store.close()


def usage(cfg: ApiConfig, params) -> JSONResponse:
    """What each model costs and uses (`model_usage`, see usage_report.py): totals, the table by model and by job, the last
    14 days for the chart, and what each cost means. `range` is today, 7d (the default) or 30d. Read-only."""
    name = params.get("range", "7d")
    if name not in USAGE_RANGES:
        return err("bad range")
    store = cfg.store_factory()
    try:
        return jr(build_usage(store, cfg.now(), cfg.settings.tz, name))
    finally:
        store.close()


async def _capped_body(request: Request) -> bytes | None:
    """The body, or None when it is over MAX_BODY: refused from Content-Length before reading, and the stream is cut
    off at the cap otherwise."""
    try:
        if int(request.headers.get("content-length", "0")) > MAX_BODY:
            return None
    except ValueError:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            return None
    return bytes(buf)


def _json_body(raw: bytes) -> dict | None:
    if len(raw) > MAX_BODY:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def do_action(cfg: ApiConfig, raw: bytes) -> JSONResponse:
    body = _json_body(raw)
    if body is None:
        return err("bad request")
    item_id, verb, arg, shown = (body.get(k) for k in ("item_id", "verb", "arg", "shown_hash"))
    if not isinstance(item_id, str) or not ID_RE.match(item_id) or not isinstance(verb, str):
        return err("bad request")
    if arg is not None and not isinstance(arg, str):
        return err("bad request")
    if shown is not None and not isinstance(shown, str):
        return err("bad request")
    store = cfg.store_factory()
    try:
        if (since := controls.paused_since(store)) is not None:  # the agent is paused: no click is queued
            return jr({"error": "paused", "paused_at": since}, 409)
        item = store.get_item(item_id)
        if item is None:
            return err("unknown item", 404)
        if verb not in VERBS.get(item.kind, ()):
            return err("bad verb")
        if verb == "edit":
            if arg is None or not 1 <= len(arg) <= EDIT_MAX or not arg.strip():
                return err("bad edit")
        elif verb == "post":
            # The channel he chose for an idea or a message: a channel id, or none for the leadership channel. Only the shape is
            # checked here; whether it is one of his channels is the runner's check, made again just before the send
            # (commands.post_target), so a stale page is refused there with a reason and nothing is sent.
            if arg is not None:
                if item.kind not in CHANNEL_KINDS:  # feedback and the Next Todo post only to their thread in the leadership channel
                    return err(LEADERSHIP_ONLY)
                if CHANNEL_ID.fullmatch(arg) is None:
                    return err("bad channel")
        else:
            arg = None
        if verb in NO_HASH:
            shown = ""  # nothing to protect: whatever was sent is ignored
        elif shown is None or not SHA_RE.match(shown):
            return err("shown_hash required")
        pend = store.last_page_command(item.id, ("queued", "running"))
        if pend is not None:  # one command per item at a time: a second tab can't stack a second row
            return jr({"error": "already pending", "command_id": pend.id, "verb": pend.verb, "state": pend.state}, 409)
        cid = store.enqueue_page_command(item.id, verb, arg, shown, cfg.now())
    finally:
        store.close()
    cfg.kick()
    return jr({"command_id": cid})


def do_compose(cfg: ApiConfig, raw: bytes) -> JSONResponse:
    """The composer's Polish: queue a `compose` page command that carries Haseeb's rough text. The dashboard makes no
    model call; the tick polishes the text into a `message` item and sends the draft to the DM, which then waits for
    Post, Edit, Shorter or Skip like any draft. The text is cleaned (control characters out), must be 1 to 2,000
    characters (400 otherwise), and is refused with 409 while the agent is paused. A second click with the same text
    while the first is queued or running gets 409 and that command's id, never a second item."""
    from ..jobs.message import new_item_id
    from ..message_prompt import MAX_RAW_CHARS, normalize_raw
    body = _json_body(raw)
    if body is None or set(body) != {"text"} or not isinstance(body["text"], str):
        return err("bad request")
    text = normalize_raw(body["text"])
    if not text:
        return err("empty message")
    if len(text) > MAX_RAW_CHARS:
        return err("too long")
    store = cfg.store_factory()
    try:
        if (since := controls.paused_since(store)) is not None:
            return jr({"error": "paused", "paused_at": since}, 409)
        for c in store.page_commands_in(("queued", "running"), verb="compose"):
            if c.arg == text:
                return jr({"error": "already pending", "command_id": c.id, "verb": "compose", "state": c.state}, 409)
        taken = {c.item_id for c in store.page_commands_in(("queued", "running"), verb="compose")}
        at = cfg.now()
        item_id = new_item_id(at)
        while item_id in taken or store.get_item(item_id) is not None:  # two clicks in one microsecond are two messages
            at += timedelta(microseconds=1)
            item_id = new_item_id(at)
        cid = store.enqueue_page_command(item_id, "compose", text, "", cfg.now())
    finally:
        store.close()
    cfg.kick()
    return jr({"command_id": cid, "item_id": item_id})


def do_label(cfg: ApiConfig, raw: bytes) -> JSONResponse:
    body = _json_body(raw)
    if body is None:
        return err("bad request")
    channel, ts, kind = (body.get(k) for k in ("channel", "ts", "kind"))
    if not all(isinstance(v, str) for v in (channel, ts, kind)) or kind not in MESSAGE_KINDS:
        return err("bad request")
    store = cfg.store_factory()
    try:
        if store.get_message(channel, ts) is None:
            return err("unknown message", 404)
        store.set_label(channel, ts, kind)
    finally:
        store.close()
    return jr({"ok": True})


def command(cfg: ApiConfig, cid: int) -> JSONResponse:
    store = cfg.store_factory()
    try:
        c = store.page_command(cid)
    finally:
        store.close()
    return err("unknown command", 404) if c is None else jr(_command_json(c))


# ---------------------------------------------------------------- the channels a post may go to

def channel_list(cfg: ApiConfig) -> dict:
    """GET /api/channels: where an idea or a message may be posted, for the card's "Post to" choice. `channels` are
    `channels.targets`, the leadership channel first (always, even when the list lacks it or there is none) and then his other
    channels A-Z, each `{id, name, private, leadership}`. The leadership channel's `name` is null when the list does not hold it
    (its name is not known: the page says "the program channel"; the id is never passed off as a name). The list is the cache
    read back through the shape rules of the page it came from, so a channel with markup or a long name in it, or a DM id, is not
    here. `fetched_at` and `age_s` (the server's clock, never negative) say how old the list is, both null when that is unknown;
    `refresh_pending` and `requested_at` say a refresh was asked for and the tick has not taken it. Read only: it works while the
    agent is paused."""
    store = cfg.store_factory()
    try:
        now = cfg.now()
        fetched, _ = channels.cached(store)
        pending = channels.requested(store)
        asked = _parse_dt(store.get_cursor(channels.REFRESH_KEY) or "", cfg.settings.tz) if pending else None
        rows = [{"id": c.id, "name": None if c.name == c.id else c.name, "private": c.private,
                 "leadership": c.id == cfg.settings.channel_id} for c in channels.targets(store, cfg.settings)]
    finally:
        store.close()
    return {"channels": rows, "fetched_at": _iso(fetched),
            "age_s": None if fetched is None else max(0, int((now - fetched).total_seconds())),
            "refresh_pending": pending, "requested_at": _iso(asked)}


def do_channels_refresh(cfg: ApiConfig, raw: bytes) -> JSONResponse:
    """POST /api/channels/refresh: ask the tick to read the list of his channels again. The dashboard never calls Slack: it sets the
    request (`channels.ask_refresh`) and kicks the tick, whose refresh job takes the request and makes one try at its next unpaused
    tick (never in the huddle hold). At most one request in 10 minutes: a second one is a 429 with `requested_at` (the request that
    was made) and `retry_after_s`, and changes nothing. Refused with 409 while the agent is paused, like every action, and that
    refusal uses up none of the window. No body, or `{}`; anything else is a 400."""
    if raw.strip():
        body = _json_body(raw)
        if body is None or body:
            return err("bad request")
    store = cfg.store_factory()
    try:
        if (since := controls.paused_since(store)) is not None:
            return jr({"error": "paused", "paused_at": since}, 409)
        now = cfg.now()
        made = channels.ask_refresh(store, now)
    finally:
        store.close()
    if made is not None:
        wait = max(1, math.ceil((made + channels.ASK_EVERY - now).total_seconds()))
        return JSONResponse({"error": "asked a moment ago", "requested_at": _iso(made), "retry_after_s": wait},
                            status_code=429, headers={**NO_STORE, "retry-after": str(wait)})
    cfg.kick()
    return jr({"requested_at": now.isoformat(), "refresh_pending": True})


# ---------------------------------------------------------------- run controls: jobs, run, pause, team posts

def jobs_state(cfg: ApiConfig) -> dict:
    """The run controls' state: whether the agent is paused, which Slack account the last `slack-check` saw (with `stale`:
    the check is too old to trust, see _identity_stale), and for each allowlisted job whether it is running (its lock is
    held, or it was spawned under 2 minutes ago and has not ended since) and the record of its last finished run."""
    store = cfg.store_factory()
    try:
        now = cfg.now()
        since = controls.paused_since(store)
        jobs = []
        for job in controls.JOBS:
            st = controls.job_state(store, cfg.settings, job, now)
            jobs.append({"name": job.name, "running": st.running, "started_at": st.started_at, "last": st.last})
        identity = controls.read_identity(store)
        if identity is not None:
            identity["stale"] = _identity_stale(cfg, identity, now)
        held = held_at(store)  # the huddle hold (R33): the composer's rows wait while a tick keeps stamping it
        return {"paused": since is not None, "paused_at": since, "slack_identity": identity,
                "compose_hold": held is not None and timedelta(0) <= now - held < HOLD_FRESH, "jobs": jobs}
    finally:
        store.close()


def _identity_stale(cfg: ApiConfig, identity: dict, now: datetime) -> bool:
    """Whether the last Slack check can no longer be trusted: it is older than 6 hours, older than this dashboard process
    (a restart is how new code or a changed setup is loaded; the 10-19 incident was a Claude account switch that a
    green line from before it would have hidden), or has no readable time. The page then shows "check again" instead
    of a green tick."""
    at = _parse_dt(identity.get("at") or "", cfg.settings.tz)
    if at is None or now - at > IDENTITY_FRESH:
        return True
    return cfg.started_at is not None and at.timestamp() < cfg.started_at


def _feedback_refusal(cfg: ApiConfig, store: Store, ts: str) -> JSONResponse | None:
    """Why the post `ts` may not get a manual feedback draft (a fixed answer), or None. The ts is the one client value
    the run controls take: it must be a top-level post by a teammate in the stored channel history."""
    from ..jobs.feedback import claimed_posts, item_id, manual_refusal
    m = store.get_message_effective(cfg.settings.channel_id, ts)  # the kind the lists showed, label included
    if m is None:
        return err("unknown post", 404)
    if (why := manual_refusal(cfg.settings, m)) is not None:
        return err(why)
    status = store.item_statuses([item_id(ts)]).get(item_id(ts))
    if status is not None:
        return jr({"error": "it already has a feedback draft", "state": status}, 409)
    if ts in claimed_posts(store, cfg.now()):  # the tick, or an earlier click, is drafting it right now
        return jr({"error": "a draft is already being written", "state": "drafting"}, 409)
    return None


def run_job(cfg: ApiConfig, raw: bytes) -> JSONResponse:
    """Start one allowlisted job. The job comes from a fixed list (`controls.JOBS`), its command line is fixed too
    (`controls.spawn_args`), and the only client value is a feedback run's post ts, checked against the store. The
    tick is kicked as before; every other job is spawned detached. Refused with 409 while the agent is paused (Check
    Slack account, which only reads, still runs) and while the job runs. The spawn is recorded first so a second
    request in the next 2 minutes sees it running before the new process has taken its lock. The tick's kick reports
    launchctl's exit code: when it is not zero launchd did not start the tick (it may not be loaded, or launchctl failed),
    the spawn record is taken back and the answer is 409 "launchd did not start the tick", so the control never shows
    "Running" for a tick that is not running."""
    body = _json_body(raw)
    if body is None or set(body) - {"job", "ts"}:
        return err("bad request")
    name = body.get("job")
    job = controls.BY_NAME.get(name) if isinstance(name, str) else None
    if job is None:
        return err("bad job")
    ts = body.get("ts")
    if job.takes_ts != (ts is not None) or (ts is not None and not controls.valid_ts(ts)):
        return err("bad ts")
    with cfg.run_lock:  # two requests in this process cannot both pass the checks below
        store = cfg.store_factory()
        try:
            since = controls.paused_since(store)
            if since is not None and job.name != "slack_check":
                return jr({"error": "paused", "paused_at": since}, 409)
            if job.name == "decks" and not cfg.settings.decks_enabled:
                return jr({"error": "decks are turned off in config.toml"}, 409)
            if ts is not None and (refusal := _feedback_refusal(cfg, store, ts)) is not None:
                return refusal
            now = cfg.now()
            state = controls.job_state(store, cfg.settings, job, now)
            if state.running:
                return jr({"error": "already running", "job": job.name, "running": True,
                           "started_at": state.started_at, "last": state.last}, 409)
            controls.mark_spawned(store, job.name, now, ts)
            try:
                if job.argv is None:
                    if cfg.kick():  # launchctl's exit code: non-zero, nothing ran. A page click ignores it; this does not
                        controls.clear_spawned(store, job.name)
                        return jr({"error": "launchd did not start the tick"}, 409)
                else:
                    cfg.spawn(controls.spawn_args(job, ts))
            except Exception:  # noqa: BLE001
                controls.clear_spawned(store, job.name)
                return err("could not start", 500)
        finally:
            store.close()
    return jr({"ok": True, "job": job.name, "started_at": now.isoformat()})


def set_pause(cfg: ApiConfig, raw: bytes) -> JSONResponse:
    """Pause or resume. Either way the clicks queued before the pause are expired right here (`expire_for_pause`, the
    function the paused tick runs: database only, it touches `page_commands` rows and nothing else), so the kill switch
    kills them even when no paused tick ran in between: a short pause, or a tick that was busy with a long job. On Pause
    it runs once the cursor is set (the pause time it compares with); on Resume, before the cursor is cleared. The Next
    Todo auto-post is not touched: the dashboard writes no todo state, the paused tick cancels it."""
    body = _json_body(raw)
    if body is None or set(body) != {"paused"} or not isinstance(body["paused"], bool):
        return err("bad request")
    store = cfg.store_factory()
    try:
        now = cfg.now()
        if body["paused"]:
            since = controls.set_paused(store, True, now)
            try:  # the cursor is set first: the kill switch is on whatever happens next
                expire_for_pause(store, since, now)
            except Exception as e:  # noqa: BLE001 - the paused tick expires them within a minute
                log.warning("pause: could not expire the queued clicks (%s); the paused tick will", type(e).__name__)
        else:
            current = controls.paused_since(store)
            if current is not None:
                try:  # before the cursor is cleared: a pause that something else set (the CLI) is covered too
                    expire_for_pause(store, current, now)
                except Exception as e:  # noqa: BLE001 - armed clicks must not outlive the pause: stay paused
                    log.warning("resume: could not expire the queued clicks (%s); still paused", type(e).__name__)
                    return err("could not resume", 500)
            since = controls.set_paused(store, False, now)
    finally:
        store.close()
    return jr({"paused": since is not None, "paused_at": since})


def team_posts(cfg: ApiConfig) -> list[dict]:
    """Teammates' top-level posts of the last 7 days (today included), newest first, each with its feedback state: the
    list under "Waiting for you" on the Live page, where a post without a draft gets a "Draft feedback" button."""
    from ..jobs.feedback import feedback_states, manual_refusal
    s, now = cfg.settings, cfg.now()
    store = cfg.store_factory()
    try:
        colors = {p.user_id: p.color for p in store.people()}
        found = store.team_posts(s.channel_id, now.date() - timedelta(days=TEAM_DAYS - 1), s.haseeb_id, TEAM_LIMIT * 2)
        posts = [m for m in found if manual_refusal(s, m) is None][:TEAM_LIMIT]
        drafting = controls.job_state(store, s, controls.BY_NAME["feedback"], now).arg
        states = feedback_states(store, [m.ts for m in posts], drafting, now)
    finally:
        store.close()
    return [{"ts": m.ts, "channel": m.channel, "user_id": m.user_id, "user_name": m.user_name,
             "color": colors.get(m.user_id, 0), "text": m.text[:TEAM_TEXT_MAX], "day": m.day.isoformat(),
             "permalink": permalink(s, m), "feedback": states[m.ts]} for m in posts]


# ---------------------------------------------------------------- routes

def api_routes(cfg: ApiConfig) -> list[Route]:
    """All /api routes, in the order that must precede the static catch-all."""

    def get(path: str, fn, *, with_request: str | None = None) -> Route:
        async def endpoint(request: Request):
            if with_request == "params":
                return await run_in_threadpool(fn, cfg, request.query_params)
            if with_request == "path":
                return await run_in_threadpool(fn, cfg, request.path_params["item_id"])
            return await run_in_threadpool(fn, cfg)
        return Route(path, endpoint, methods=["GET"])

    def jsonify(fn):
        def wrapped(*a):
            r = fn(*a)
            return r if isinstance(r, Response) else jr(r)
        return wrapped

    async def stream_ep(request: Request):
        return await stream(cfg, request)

    def post(path: str, fn) -> Route:
        async def endpoint(request: Request):
            raw = await _capped_body(request)
            if raw is None:
                return err("bad request")
            return await run_in_threadpool(fn, cfg, raw)
        return Route(path, endpoint, methods=["POST"])

    async def command_ep(request: Request):
        raw = request.path_params["id"]
        if not raw.isdigit() or len(raw) > 12:
            return err("unknown command", 404)
        return await run_in_threadpool(command, cfg, int(raw))

    return [
        get("/api/live", jsonify(live)),
        Route("/api/stream", stream_ep, methods=["GET"]),
        get("/api/inbox", jsonify(inbox)),
        get("/api/history", history, with_request="params"),
        get("/api/people", jsonify(people)),
        get("/api/weeks", jsonify(weeks)),
        get("/api/decks", jsonify(decks)),
        get("/api/decks/{item_id}.pdf", deck_pdf, with_request="path"),
        get("/api/decks/{item_id}/thumb.png", deck_thumb, with_request="path"),
        get("/api/logs", logs, with_request="params"),
        get("/api/score", score, with_request="params"),
        get("/api/usage", usage, with_request="params"),
        get("/api/jobs", jsonify(jobs_state)),
        get("/api/teamposts", jsonify(team_posts)),
        get("/api/channels", jsonify(channel_list)),
        post("/api/action", do_action),
        post("/api/compose", do_compose),
        post("/api/label", do_label),
        post("/api/run", run_job),
        post("/api/pause", set_pause),
        post("/api/channels/refresh", do_channels_refresh),
        Route("/api/command/{id}", command_ep, methods=["GET"]),
        Route("/api", _not_found),
        Route("/api/{rest:path}", _not_found),  # an unknown /api path never falls through to the static files
    ]


async def _not_found(request: Request):
    return err("not found", 404)
