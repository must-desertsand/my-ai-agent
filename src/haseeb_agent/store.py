"""SQLite store: items, cursors, preferences, job runs, and the dashboard's events, history and page commands."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Collection
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from .models import (
    COMMAND_STATES,
    COMMAND_VERBS,
    COST_BASES,
    DAILY_TOTAL_STEP,
    MESSAGE_KINDS,
    PERSON_COLORS,
    STATUSES,
    USAGE_IDENT,
    USAGE_MAX_COST,
    USAGE_MAX_COUNT,
    USAGE_PROVIDERS,
    USAGE_SOURCES,
    Event,
    Item,
    PageCommand,
    Person,
    StoredMessage,
    UsageRow,
)

# Job names whose failed runs the weekly "Failed job runs" leaves out (see failed_runs_on): the composer (a message is his own text)
# and the channel-list refresh (cli.CHANNELS_JOB; it fails whenever the connector's page cannot be trusted, which anybody's Purpose
# can cause, and is tried again every 6 hours, so it would add up to four failed runs a day for nothing the agent got wrong).
UNCOUNTED_JOBS = ("compose", "refresh_channels")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    day        TEXT NOT NULL,
    status     TEXT NOT NULL,
    body       TEXT NOT NULL,
    title      TEXT NOT NULL,
    dm_ts      TEXT,
    source_ts  TEXT,
    score      REAL,
    models     TEXT NOT NULL DEFAULT '{}',
    attempts   INTEGER NOT NULL DEFAULT 0,
    edited     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    acted_at   TEXT
);
CREATE INDEX IF NOT EXISTS items_kind_day ON items(kind, day);
CREATE INDEX IF NOT EXISTS items_dm_ts ON items(dm_ts);
CREATE TABLE IF NOT EXISTS cursors (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prefs (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kind TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job    TEXT NOT NULL,
    ok     INTEGER NOT NULL,
    detail TEXT NOT NULL,
    at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job     TEXT NOT NULL,
    step    TEXT NOT NULL,
    model   TEXT,
    started TEXT NOT NULL,
    ended   TEXT,
    ok      INTEGER,
    kind    TEXT
);
-- M7: the live view asks for the newest open event on every poll; only open events are in this index.
CREATE INDEX IF NOT EXISTS events_open ON events(id) WHERE ended IS NULL;
-- Program-channel history. The integer id (not the implicit rowid) is the FTS content key, because VACUUM may
-- renumber implicit rowids and would silently detach the index.
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    channel   TEXT NOT NULL,
    ts        TEXT NOT NULL,
    thread_ts TEXT,
    user_id   TEXT NOT NULL,
    user_name TEXT NOT NULL,
    text      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    day       TEXT NOT NULL,
    topic     TEXT,
    UNIQUE (channel, ts)
);
CREATE INDEX IF NOT EXISTS messages_day ON messages(day);
CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, user_name, content='messages', content_rowid='id'
);
CREATE TABLE IF NOT EXISTS people (
    user_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    color   INTEGER NOT NULL
);
-- A local re-label. Kept apart from messages so a classifier re-run (an upsert) can never undo it.
CREATE TABLE IF NOT EXISTS labels (
    channel TEXT NOT NULL,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    PRIMARY KEY (channel, ts)
);
CREATE TABLE IF NOT EXISTS page_commands (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    verb       TEXT NOT NULL,
    arg        TEXT,
    shown_hash TEXT NOT NULL,
    state      TEXT NOT NULL,
    result     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS page_commands_state ON page_commands(state, id);
-- One row per model call, never any text (usage.py, docs/superpowers/specs/2026-10-21-usage-tab-design.md). Added by
-- this CREATE alone: a database made before it simply gets the empty table the first time a newer process opens it.
CREATE TABLE IF NOT EXISTS model_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    at                 TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    job                TEXT NOT NULL,
    step               TEXT NOT NULL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    cost_usd           REAL,
    cost_basis         TEXT NOT NULL,
    latency_ms         INTEGER,
    ok                 INTEGER NOT NULL,
    source             TEXT NOT NULL,
    ref                TEXT
);
-- The reads compare and order by julianday(at) (the times carry offsets, so the text is not the order), so the index is on that
-- expression; the first one, on the text, could never be used by them.
DROP INDEX IF EXISTS model_usage_at;
CREATE INDEX IF NOT EXISTS model_usage_time ON model_usage(julianday(at));
-- `ref` is for dedupe: a Claude run's session file, a proxy request id. A row that repeats one is not added twice.
CREATE UNIQUE INDEX IF NOT EXISTS model_usage_ref ON model_usage(provider, ref, model) WHERE ref IS NOT NULL;
"""

_COLUMNS = (
    "id", "kind", "day", "status", "body", "title", "dm_ts", "source_ts", "score",
    "models", "attempts", "edited", "created_at", "acted_at",
)
_MARKABLE = set(_COLUMNS) - {"id", "status"}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _encode(field: str, value):
    if field == "models":
        return json.dumps(value)
    if field == "edited":
        return int(bool(value))
    if field == "day":
        return value.isoformat()
    if field in ("created_at", "acted_at"):
        return _iso(value)
    return value


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        kind=row["kind"],
        day=date.fromisoformat(row["day"]),
        status=row["status"],
        body=row["body"],
        title=row["title"],
        dm_ts=row["dm_ts"],
        source_ts=row["source_ts"],
        score=row["score"],
        models=json.loads(row["models"]),
        attempts=row["attempts"],
        edited=bool(row["edited"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        acted_at=datetime.fromisoformat(row["acted_at"]) if row["acted_at"] else None,
    )


# Message columns with the kind a person may have overridden: the label wins over the classifier.
_MESSAGE_SELECT = (
    "SELECT m.id, m.channel, m.ts, m.thread_ts, m.user_id, m.user_name, m.text, "
    "COALESCE(l.kind, m.kind) AS kind, m.day, m.topic FROM messages m "
    "LEFT JOIN labels l ON l.channel = m.channel AND l.ts = m.ts"
)
_FILTER_TOKEN = re.compile(r'\b(person|kind):("[^"]*"|\S*)', re.IGNORECASE)


def _row_to_message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        channel=row["channel"],
        ts=row["ts"],
        thread_ts=row["thread_ts"],
        user_id=row["user_id"],
        user_name=row["user_name"],
        text=row["text"],
        kind=row["kind"],
        day=date.fromisoformat(row["day"]),
        topic=row["topic"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        job=row["job"],
        step=row["step"],
        model=row["model"],
        started=datetime.fromisoformat(row["started"]),
        ended=datetime.fromisoformat(row["ended"]) if row["ended"] else None,
        ok=None if row["ok"] is None else bool(row["ok"]),
        kind=row["kind"],
    )


def _row_to_command(row: sqlite3.Row) -> PageCommand:
    return PageCommand(
        id=row["id"],
        item_id=row["item_id"],
        verb=row["verb"],
        arg=row["arg"],
        shown_hash=row["shown_hash"],
        state=row["state"],
        result=row["result"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


_USAGE_COLUMNS = (
    "at", "provider", "model", "job", "step", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "cost_usd", "cost_basis", "latency_ms", "ok", "source", "ref",
)


class UsageImport(NamedTuple):
    """What `Store.import_usage_rows` did."""
    added: int      # rows that went in
    replaced: int   # rows deleted because the new ones replace them
    rejected: int   # rows left out because a value in them is not acceptable


def _usage_values(row: UsageRow) -> list:
    """The values of a row to insert, or ValueError. Every column is checked here, so the table holds no text however a
    row got to it (review M8): the words of a fixed vocabulary, identifiers of `USAGE_IDENT`, numbers in range and a time
    with its offset. The error names the column, never the value."""
    if row.provider not in USAGE_PROVIDERS:
        raise ValueError("unknown usage provider")
    if row.cost_basis not in COST_BASES:
        raise ValueError("unknown cost basis")
    if row.source not in USAGE_SOURCES:
        raise ValueError("unknown usage source")
    for name, value in (("model", row.model), ("job", row.job), ("step", row.step), ("ref", row.ref)):
        if not (value is None and name == "ref") and not (isinstance(value, str) and USAGE_IDENT.fullmatch(value)):
            raise ValueError(f"unacceptable usage {name}")
    for name, value in (("input_tokens", row.input_tokens), ("output_tokens", row.output_tokens),
                        ("cache_read_tokens", row.cache_read_tokens), ("cache_write_tokens", row.cache_write_tokens),
                        ("latency_ms", row.latency_ms)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= USAGE_MAX_COUNT):
            raise ValueError(f"unacceptable usage {name}")
    cost = row.cost_usd
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost)
                             or not 0 <= cost <= USAGE_MAX_COST):
        raise ValueError("unacceptable usage cost")
    if not isinstance(row.at, datetime) or row.at.tzinfo is None or row.at.utcoffset() is None:
        raise ValueError("unacceptable usage time")
    return [_iso(row.at), row.provider, row.model, row.job, row.step, row.input_tokens, row.output_tokens,
            row.cache_read_tokens, row.cache_write_tokens, row.cost_usd, row.cost_basis, row.latency_ms,
            int(bool(row.ok)), row.source, row.ref]


def _row_to_usage(row: sqlite3.Row) -> UsageRow:
    return UsageRow(
        id=row["id"], at=datetime.fromisoformat(row["at"]), provider=row["provider"], model=row["model"],
        job=row["job"], step=row["step"], input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"], cache_write_tokens=row["cache_write_tokens"],
        cost_usd=row["cost_usd"], cost_basis=row["cost_basis"], latency_ms=row["latency_ms"], ok=bool(row["ok"]),
        source=row["source"], ref=row["ref"],
    )


def _check_kind(kind: str) -> None:
    if kind not in MESSAGE_KINDS:
        raise ValueError(f"unknown message kind: {kind!r}")


def _fts_query(text: str, prefix_last: bool = True) -> str:
    """Quote every word so user input is only ever data: no operators, column filters or NEAR. The last word becomes
    a prefix (`"pric"*` finds "pricing"), so results follow the typing; the star goes after the closing quote, where
    FTS5 reads it as the prefix mark, and it is the only syntax that ever comes from here."""
    words = [f'"{w}"' for w in re.findall(r"\w+", text)]
    if prefix_last and words:
        words[-1] += "*"
    return " ".join(words)


def _like_prefix(text: str) -> str:
    """A LIKE pattern for "starts with `text`": its own %, _ and \\ are escaped (ESCAPE '\\'), so they match only
    themselves."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    def __init__(self, path: str | Path, *, busy_ms: int = 5000):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._busy_ms = int(busy_ms)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        # Before anything else: the dashboard reads while the tick writes, so a locked DB must wait, not fail.
        self._db.execute(f"PRAGMA busy_timeout={self._busy_ms}")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """A database made before a column existed gets it (CREATE TABLE IF NOT EXISTS leaves old tables alone)."""
        if "kind" not in {r["name"] for r in self._db.execute("PRAGMA table_info(prefs)")}:
            try:
                with self._db:
                    self._db.execute("ALTER TABLE prefs ADD COLUMN kind TEXT")
            except sqlite3.OperationalError:  # another process added it first
                if "kind" not in {r["name"] for r in self._db.execute("PRAGMA table_info(prefs)")}:
                    raise

    def close(self) -> None:
        self._db.close()

    def enable_wal(self, backup_dir: Path, now: datetime) -> Path | None:
        """Switch the DB to WAL so dashboard reads never block the tick's writes. A WAL switch is a one-way change
        to the file, so copy it first. Returns the backup path, or None when there was nothing to do."""
        if str(self._path) == ":memory:" or self._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            return None
        self._db.commit()
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Second resolution, and never overwrite an earlier backup: a collision gets a -1, -2 ... suffix.
        base = f"{self._path.name}.bak-{now:%Y%m%d%H%M%S}"
        backup, n = backup_dir / base, 0
        while True:
            try:
                backup.touch(exist_ok=False)  # claims the name exclusively
                break
            except FileExistsError:
                n += 1
                backup = backup_dir / f"{base}-{n}"
        try:
            copy = sqlite3.connect(backup)
            try:
                self._db.backup(copy)  # consistent even if another connection writes meanwhile
            finally:
                copy.close()
            mode = self._db.execute("PRAGMA journal_mode=wal").fetchone()[0]
            if mode != "wal":
                raise RuntimeError(f"could not switch to WAL (journal_mode is {mode!r})")
        except BaseException:
            backup.unlink(missing_ok=True)  # a failed switch is retried: don't pile up backups
            raise
        return backup

    # items
    def upsert_item(self, item: Item) -> None:
        values = [_encode(c, getattr(item, c)) for c in _COLUMNS]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "id")
        with self._db:
            self._db.execute(
                f"INSERT INTO items ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )

    def insert_item(self, item: Item) -> bool:
        """Create the item unless a row with its id exists, which is left exactly as it is (status, body, DM). True
        when it was inserted. For an item that two runs could both try to create: `upsert_item` would write the
        later run's draft over the earlier one's, even over a `posted` status."""
        values = [_encode(c, getattr(item, c)) for c in _COLUMNS]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._db:
            cur = self._db.execute(
                f"INSERT INTO items ({', '.join(_COLUMNS)}) VALUES ({placeholders}) ON CONFLICT(id) DO NOTHING",
                values,
            )
        return cur.rowcount == 1

    def get_item(self, id: str) -> Item | None:
        row = self._db.execute("SELECT * FROM items WHERE id=?", (id,)).fetchone()
        return _row_to_item(row) if row else None

    def item_by_dm_ts(self, ts: str) -> Item | None:
        row = self._db.execute("SELECT * FROM items WHERE dm_ts=?", (ts,)).fetchone()
        return _row_to_item(row) if row else None

    def items_for_day(self, kind: str, day: date) -> list[Item]:
        rows = self._db.execute(
            "SELECT * FROM items WHERE kind=? AND day=? ORDER BY created_at, rowid",
            (kind, day.isoformat()),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def recent_items(self, kind: str, days: int, today: date | None = None) -> list[Item]:
        """Items of `kind` from the last `days` days (inclusive of today), newest first."""
        today = today or date.today()
        since = (today - timedelta(days=days)).isoformat()
        rows = self._db.execute(
            "SELECT * FROM items WHERE kind=? AND day>=? ORDER BY day DESC, created_at DESC, rowid DESC",
            (kind, since),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def count_thread_items(self, id_prefix: str, thread_ts: str) -> int:
        """How many items have an id that starts with `id_prefix` and were drafted for the thread `thread_ts` (whatever
        their status: a skipped draft still counts). The caps of the thread watch (jobs/followback.py) count with it."""
        row = self._db.execute(
            "SELECT COUNT(*) FROM items WHERE kind='feedback' AND source_ts=? AND id LIKE ? ESCAPE '\\'",
            (thread_ts, _like_prefix(id_prefix))).fetchone()
        return int(row[0])

    def open_dm_items(self, since: date, statuses: tuple[str, ...] = ("delivered", "drafted", "failed")) -> list[Item]:
        """Items Haseeb may still command: a DM message (dm_ts), one of `statuses`, day >= since. Oldest dm_ts first."""
        marks = ", ".join("?" for _ in statuses)
        rows = self._db.execute(
            f"SELECT * FROM items WHERE dm_ts IS NOT NULL AND status IN ({marks}) AND day>=? "
            "ORDER BY CAST(dm_ts AS REAL), rowid",
            (*statuses, since.isoformat()),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def mark(self, id: str, status: str, **fields) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        bad = set(fields) - _MARKABLE
        if bad:
            raise ValueError(f"cannot set fields: {sorted(bad)}")
        cols = ["status", *fields]
        values = [status, *(_encode(k, v) for k, v in fields.items())]
        with self._db:
            self._db.execute(
                f"UPDATE items SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                [*values, id],
            )

    def set_model(self, id: str, key: str, value: str) -> None:
        """Set one key of an item's models map in a single UPDATE that never touches status. The detached deck
        process uses it: a read-then-mark could write a stale status back over a post made meanwhile."""
        with self._db:
            self._db.execute("UPDATE items SET models=json_set(models, '$.' || ?, ?) WHERE id=?", (key, value, id))

    # cursors
    def get_cursor(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_cursor(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO cursors (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def delete_cursor(self, key: str) -> None:
        """Remove a cursor, so it reads as never set (None). Most cursors are only ever cleared to "", which is right
        where a reader tests for a value; use this where one tells "absent" from "empty" (`is not None`)."""
        with self._db:
            self._db.execute("DELETE FROM cursors WHERE key=?", (key,))

    def claim_cursor(self, key: str, value: str, stale_before: datetime) -> bool:
        """Claim `key` for one caller, atomically (one statement, so two processes cannot both win). `value` is the
        time of the claim (ISO). The claim is won when the key is absent, or was released (""), or its claim is older
        than `stale_before` (a claimer that died is given up on) or not a time at all. Otherwise another caller holds
        it and this returns False. Give it back with `release_cursor` (which removes the key)."""
        with self._db:
            cur = self._db.execute(
                "INSERT INTO cursors (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                "WHERE cursors.value = '' OR julianday(cursors.value) IS NULL "
                "OR julianday(cursors.value) < julianday(?)",
                (key, value, _iso(stale_before)),
            )
        return cur.rowcount == 1

    def release_cursor(self, key: str, value: str) -> bool:
        """Give back a claim: the key is removed only while it still holds `value`, so a claimer whose claim was taken
        over (it ran past the stale time) cannot free the new holder's claim. True when it was released."""
        with self._db:
            cur = self._db.execute("DELETE FROM cursors WHERE key=? AND value=?", (key, value))
        return cur.rowcount == 1

    def update_cursor(self, key: str, update: Callable[[str | None], str]) -> str:
        """Set a cursor to `update(its value)` (None when there is none) in one step, and return the new value.
        BEGIN IMMEDIATE takes the write lock before the read, so a change that another process commits meanwhile
        (the tick's capture while the nightly refresh works the queue) is waited for, not written over. Nothing
        changes if `update` raises."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
            value = update(row["value"] if row else None)
            self._db.execute(
                "INSERT INTO cursors (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        return value

    def cursors(self, prefix: str) -> dict[str, str]:
        """Every cursor whose key starts with `prefix`."""
        rows = self._db.execute(
            "SELECT key, value FROM cursors WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    # preferences learned from edits and skips. A preference with a `kind` belongs to that kind's prompts only, and
    # the general ones (no kind) never include it: what Haseeb asks of his own messages must not steer insights.
    def add_pref(self, text: str, kind: str | None = None) -> None:
        with self._db:
            self._db.execute("INSERT INTO prefs (text, kind) VALUES (?, ?)", (text, kind))

    def prefs(self, limit: int = 50, kind: str | None = None) -> list[str]:
        """Newest first. `kind` None: the general preferences; a kind: that kind's own."""
        rows = self._db.execute(
            "SELECT text FROM prefs WHERE kind IS ? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
        return [r["text"] for r in rows]

    # job runs
    def record_run(self, job: str, ok: bool, detail: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO runs (job, ok, detail, at) VALUES (?, ?, ?, ?)",
                (job, int(ok), detail, datetime.now().astimezone().isoformat()),
            )

    def failed_runs_on(self, days) -> int:
        """How many failed runs were recorded on these local dates (the weekly metrics). Not the composer's: a message
        is Haseeb's own text, polished on request, so it is in none of the weekly numbers (docs/quality-metrics.md). Nor the
        channel-list refresh's (UNCOUNTED_JOBS)."""
        wanted = {d.isoformat() for d in days}
        rows = self._db.execute(
            f"SELECT at FROM runs WHERE ok = 0 AND job NOT IN ({', '.join('?' for _ in UNCOUNTED_JOBS)})",
            UNCOUNTED_JOBS).fetchall()
        return sum(1 for r in rows if str(r["at"])[:10] in wanted)

    def runs(self, limit: int = 50) -> list[tuple[str, bool, str, str]]:
        rows = self._db.execute(
            "SELECT job, ok, detail, at FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(r["job"], bool(r["ok"]), r["detail"], r["at"]) for r in rows]

    # dashboard: job/step events (text-free by design: job, step, model, timing, ok, kind)
    def add_event(
        self, job: str, step: str, *, model: str | None, started: datetime, ended: datetime | None,
        ok: bool | None, kind: str | None,
    ) -> int:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO events (job, step, model, started, ended, ok, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job, step, model, _iso(started), _iso(ended), None if ok is None else int(ok), kind),
            )
        return cur.lastrowid

    def finish_event(self, id: int, ended: datetime, ok: bool, kind: str | None, model: str | None = None) -> None:
        """Close an event. `model` is for a step that only learns its model while it runs; None keeps the old value."""
        with self._db:
            self._db.execute(
                "UPDATE events SET ended=?, ok=?, kind=?, model=COALESCE(?, model) WHERE id=?",
                (_iso(ended), int(ok), kind, model, id),
            )

    def prune_events(self, before: datetime) -> int:
        """M7: delete the events that started before `before` and return how many. The times carry their offsets, so
        they are compared as times (julianday), not as text."""
        with self._db:
            cur = self._db.execute("DELETE FROM events WHERE julianday(started) < julianday(?)", (_iso(before),))
        return cur.rowcount

    def events_since(self, id: int, limit: int = 200) -> list[Event]:
        """Events with an id above `id`, oldest first, so a poller can pass the last id it saw."""
        rows = self._db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (id, limit)).fetchall()
        return [_row_to_event(r) for r in rows]

    # model usage: one row per model call, text-free. Written by usage.py (best effort) and by `usage-backfill`.
    _USAGE_INSERT = (f"INSERT INTO model_usage ({', '.join(_USAGE_COLUMNS)}) "
                     f"VALUES ({', '.join('?' for _ in _USAGE_COLUMNS)}) ON CONFLICT DO NOTHING")

    def add_usage_rows(self, rows: list[UsageRow]) -> int:
        """Add these rows in one transaction and return how many went in. A row whose provider, ref and model repeat one
        already there is left out (that is how a backfill stays idempotent). A bad row raises before anything is added."""
        values = [_usage_values(r) for r in rows]
        if not values:
            return 0
        with self._db:
            before = self._db.total_changes
            self._db.executemany(self._USAGE_INSERT, values)
            return self._db.total_changes - before

    def import_usage_rows(self, rows: list[UsageRow], *, replace_daily: Collection[str] = (),
                          replace_refs: Collection[tuple[str, str]] = ()) -> UsageImport:
        """The backfill's way in: all or nothing as a transaction, but a row with a value that is not acceptable is left out
        and counted, never the reason for the rest to fail. `replace_daily` names UTC days (ISO dates) whose cost-only daily
        totals of the proxy (step daily_total, source backfill) are deleted in the same transaction: these rows are
        the proxy's per-request logs of those days, and a day has one source (a summary and the requests behind it must
        never both be counted). `replace_refs` are (provider, ref) pairs whose rows, of any model, are deleted in it too:
        what these rows replace (an approximate run that now has its final figures, or is recomputed)."""
        values, rejected = [], 0
        for r in rows:
            try:
                values.append(_usage_values(r))
            except (ValueError, TypeError, AttributeError, OverflowError):
                rejected += 1
        with self._db:
            replaced = 0
            for provider, ref in sorted(set(replace_refs)):
                replaced += self._db.execute("DELETE FROM model_usage WHERE provider=? AND ref=?", (provider, ref)).rowcount
            days = sorted(set(replace_daily))
            if days:
                cur = self._db.execute(
                    "DELETE FROM model_usage WHERE provider='litellm' AND source='backfill' AND step=? "
                    f"AND date(at) IN ({', '.join('?' for _ in days)})", (DAILY_TOTAL_STEP, *days))
                replaced += cur.rowcount
            before = self._db.total_changes
            if values:
                self._db.executemany(self._USAGE_INSERT, values)
            added = self._db.total_changes - before
        return UsageImport(added, replaced, rejected)

    def usage_utc_days(self, provider: str, source: str, *, step_not: str | None = None) -> set[str]:
        """The UTC days (ISO dates) that have rows of this provider and source, leaving out rows of the step `step_not`.
        (SQLite's date() reads a time with an offset as UTC, which is the day the proxy's logs and summaries go by.)"""
        sql, args = "SELECT DISTINCT date(at) AS d FROM model_usage WHERE provider=? AND source=?", [provider, source]
        if step_not is not None:
            sql, args = sql + " AND step != ?", [*args, step_not]
        return {r["d"] for r in self._db.execute(sql, args)}

    def add_usage(self, row: UsageRow, *, busy_ms: int | None = None) -> bool:
        """Add one row. False when it repeated a row with the same provider, ref and model. `busy_ms` is how long this
        insert may wait for a lock another connection holds, instead of the connection's own (5 s): the recording of a model
        call asks for a fraction of a second, because a job must never stall on a row of statistics (review M7). A row that
        is not acceptable raises ValueError before the database is touched."""
        values = _usage_values(row)
        if busy_ms is not None:
            self._db.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        try:
            with self._db:
                before = self._db.total_changes
                self._db.execute(self._USAGE_INSERT, values)
                return self._db.total_changes - before == 1
        finally:
            if busy_ms is not None:
                self._db.execute(f"PRAGMA busy_timeout={self._busy_ms}")

    def usage_since(self, since: datetime) -> list[UsageRow]:
        """The rows at or after `since`, oldest first. The times carry their offsets, so they are compared as times."""
        rows = self._db.execute(
            "SELECT * FROM model_usage WHERE julianday(at) >= julianday(?) ORDER BY julianday(at), id", (_iso(since),)
        ).fetchall()
        return [_row_to_usage(r) for r in rows]

    def first_usage_at(self, source: str | None = "live", provider: str | None = None) -> datetime | None:
        """The time of the earliest row of this source (None: any) and provider (None: any), or None when there is none."""
        sql, args = "SELECT at FROM model_usage", []
        clauses = []
        if source is not None:
            clauses.append("source=?")
            args.append(source)
        if provider is not None:
            clauses.append("provider=?")
            args.append(provider)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = self._db.execute(sql + " ORDER BY julianday(at), id LIMIT 1", args).fetchone()
        return datetime.fromisoformat(row["at"]) if row else None

    def usage_count(self, source: str | None = None) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM model_usage" + (" WHERE source=?" if source else ""), (source,) if source else ()
        ).fetchone()
        return row["n"]

    # dashboard: channel history
    def upsert_message(self, m: StoredMessage) -> bool:
        """Insert or update by (channel, ts). True if the message is new or its text changed. The FTS index is
        external-content, so it is not updated by SQLite: remove the old entry and add the new one here."""
        _check_kind(m.kind)
        with self._db:
            old = self._db.execute(
                "SELECT id, text, user_name FROM messages WHERE channel=? AND ts=?", (m.channel, m.ts)
            ).fetchone()
            if old is None:
                cur = self._db.execute(
                    "INSERT INTO messages (channel, ts, thread_ts, user_id, user_name, text, kind, day, topic) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (m.channel, m.ts, m.thread_ts, m.user_id, m.user_name, m.text, m.kind, m.day.isoformat(), m.topic),
                )
                self._fts_add(cur.lastrowid, m.text, m.user_name)
                return True
            self._fts_remove(old["id"], old["text"], old["user_name"])
            self._db.execute(
                "UPDATE messages SET thread_ts=?, user_id=?, user_name=?, text=?, kind=?, day=?, topic=? WHERE id=?",
                (m.thread_ts, m.user_id, m.user_name, m.text, m.kind, m.day.isoformat(), m.topic, old["id"]),
            )
            self._fts_add(old["id"], m.text, m.user_name)
            return old["text"] != m.text

    def get_message(self, channel: str, ts: str) -> StoredMessage | None:
        """One stored message as the classifier left it (a local label is not applied), or None."""
        row = self._db.execute(
            "SELECT id, channel, ts, thread_ts, user_id, user_name, text, kind, day, topic FROM messages "
            "WHERE channel=? AND ts=?", (channel, ts),
        ).fetchone()
        return _row_to_message(row) if row else None

    def get_message_effective(self, channel: str, ts: str) -> StoredMessage | None:
        """One stored message as the pages show it: its kind is the effective one, so a person's local label wins over
        the classifier (as in `messages`, `search` and `team_posts`), or None."""
        row = self._db.execute(f"{_MESSAGE_SELECT} WHERE m.channel=? AND m.ts=?", (channel, ts)).fetchone()
        return _row_to_message(row) if row else None

    def _fts_add(self, id: int, text: str, user_name: str) -> None:
        self._db.execute("INSERT INTO messages_fts (rowid, text, user_name) VALUES (?, ?, ?)", (id, text, user_name))

    def _fts_remove(self, id: int, text: str, user_name: str) -> None:
        # External-content delete: FTS5 needs the exact values that were indexed.
        self._db.execute(
            "INSERT INTO messages_fts (messages_fts, rowid, text, user_name) VALUES ('delete', ?, ?, ?)",
            (id, text, user_name),
        )

    def messages(
        self, day: date | None = None, user_id: str | None = None, kind: str | None = None,
        topic: str | None = None, limit: int = 500,
    ) -> list[StoredMessage]:
        """Newest first. `kind` is the effective kind, so a local label wins over the classifier."""
        where, args = self._message_filters(day, user_id, kind, topic)
        rows = self._db.execute(
            f"{_MESSAGE_SELECT} {where} ORDER BY m.day DESC, CAST(m.ts AS REAL) DESC, m.id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    @staticmethod
    def _message_filters(day, user_id, kind, topic, extra: list[str] | None = None) -> tuple[str, list]:
        clauses, args = list(extra or []), []
        if day is not None:
            clauses.append("m.day=?")
            args.append(day.isoformat())
        if user_id is not None:
            clauses.append("m.user_id=?")
            args.append(user_id)
        if kind is not None:
            clauses.append("COALESCE(l.kind, m.kind)=?")
            args.append(kind)
        if topic is not None:
            clauses.append("m.topic=?")
            args.append(topic)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), args

    def search(self, q: str, limit: int = 100, user_id: str | None = None) -> list[StoredMessage]:
        """Full-text search; the last word matches as a prefix. `person:<id or name>` and `kind:<kind>` tokens are
        filters: person is an exact id or the start of a name in any case (`person:Sara`, `person:"sara ali"`); an
        empty one filters nothing. `user_id` (the dashboard's people chip) is an exact id only. With no text left,
        filters alone select (newest first); with nothing at all, nothing is returned."""
        person = kind = None
        for key, value in _FILTER_TOKEN.findall(q):
            value = value.strip('"') or None
            if key.lower() == "person":
                person = value
            else:
                kind = value
        words = _fts_query(_FILTER_TOKEN.sub(" ", q))
        if not words and person is None and kind is None and user_id is None:
            return []
        clauses, args = [], []
        if user_id is not None:
            clauses.append("m.user_id=?")
            args.append(user_id)
        if person is not None:  # LIKE is case-insensitive for ASCII; the pattern is a bound, escaped parameter
            clauses.append("(m.user_id=? OR m.user_name LIKE ? ESCAPE '\\')")
            args += [person, _like_prefix(person)]
        if kind is not None:
            clauses.append("COALESCE(l.kind, m.kind)=?")
            args.append(kind)
        if words:
            clauses.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            args.append(words)
        # A kind that is no message kind just matches nothing; it is a bound parameter, never SQL.
        rows = self._db.execute(
            f"{_MESSAGE_SELECT} WHERE {' AND '.join(clauses)} "
            "ORDER BY m.day DESC, CAST(m.ts AS REAL) DESC, m.id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def set_label(self, channel: str, ts: str, kind: str) -> None:
        _check_kind(kind)
        with self._db:
            self._db.execute(
                "INSERT INTO labels (channel, ts, kind) VALUES (?, ?, ?) "
                "ON CONFLICT(channel, ts) DO UPDATE SET kind=excluded.kind",
                (channel, ts, kind),
            )

    def add_label_if_absent(self, channel: str, ts: str, kind: str) -> bool:
        """Insert a label unless one exists (a person's label made meanwhile wins). True when it was inserted."""
        _check_kind(kind)
        with self._db:
            cur = self._db.execute(
                "INSERT INTO labels (channel, ts, kind) VALUES (?, ?, ?) ON CONFLICT(channel, ts) DO NOTHING",
                (channel, ts, kind),
            )
        return cur.rowcount == 1

    def unlabelled_others(self, channel: str, limit: int) -> list[StoredMessage]:
        """Newest first: messages the rules left as "other" and that no label (a person's or the enrichment's) covers."""
        rows = self._db.execute(
            f"{_MESSAGE_SELECT} WHERE m.channel=? AND l.ts IS NULL AND m.kind='other' "
            "ORDER BY m.day DESC, CAST(m.ts AS REAL) DESC, m.id DESC LIMIT ?", (channel, limit),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def messages_of_kinds(self, channel: str, kinds: tuple[str, ...], since: date) -> list[StoredMessage]:
        """Messages whose effective kind is one of `kinds`, from day `since` on, oldest first."""
        marks = ",".join("?" for _ in kinds)
        rows = self._db.execute(
            f"{_MESSAGE_SELECT} WHERE m.channel=? AND m.day>=? AND COALESCE(l.kind, m.kind) IN ({marks}) "
            "ORDER BY m.day, CAST(m.ts AS REAL), m.id", (channel, since.isoformat(), *kinds),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def set_topics(self, channel: str, topics: dict[str, str]) -> None:
        """Replace the channel's topics in one transaction: every other message ends with no topic."""
        with self._db:
            self._db.execute("UPDATE messages SET topic=NULL WHERE channel=?", (channel,))
            self._db.executemany("UPDATE messages SET topic=? WHERE channel=? AND ts=?",
                                 [(topic, channel, ts) for ts, topic in topics.items()])

    # dashboard: read helpers for the API
    def recent_events(self, limit: int = 200) -> list[Event]:
        """The newest events, newest first."""
        rows = self._db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_event(r) for r in rows]

    def last_open_event(self) -> Event | None:
        """The newest event that has not ended."""
        row = self._db.execute("SELECT * FROM events WHERE ended IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return _row_to_event(row) if row else None

    def recent_page_commands(self, limit: int = 50) -> list[PageCommand]:
        """The newest commands in any state, oldest first."""
        rows = self._db.execute("SELECT * FROM page_commands ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_command(r) for r in reversed(rows)]

    def team_posts(self, channel: str, since: date, not_user: str, limit: int = 100) -> list[StoredMessage]:
        """The channel's top-level posts from day `since` on that are not `not_user`'s and not huddle notes (the
        effective kind, so a local label wins), newest first. Bots and Slackbot are the caller's to drop."""
        rows = self._db.execute(
            f"{_MESSAGE_SELECT} WHERE m.channel=? AND m.day>=? AND m.user_id != ? "
            "AND (m.thread_ts IS NULL OR m.thread_ts = '' OR m.thread_ts = m.ts) "
            "AND COALESCE(l.kind, m.kind) != 'huddle_notes' "
            "ORDER BY m.day DESC, CAST(m.ts AS REAL) DESC, m.id DESC LIMIT ?",
            (channel, since.isoformat(), not_user, limit),
        ).fetchall()
        return [_row_to_message(r) for r in rows]

    def item_statuses(self, ids: list[str]) -> dict[str, str]:
        """The status of each of these items that exists."""
        out: dict[str, str] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self._db.execute(f"SELECT id, status FROM items WHERE id IN ({marks})", chunk):
                out[r["id"]] = r["status"]
        return out

    def message_days(self) -> dict[date, int]:
        """How many stored messages each day has."""
        rows = self._db.execute("SELECT day, COUNT(*) AS n FROM messages GROUP BY day").fetchall()
        return {date.fromisoformat(r["day"]): r["n"] for r in rows}

    # dashboard: people
    def upsert_person(self, user_id: str, name: str) -> None:
        """The color is picked once, on first insert, from the number of people so far, and never changes: a
        person keeps their color on every page and across restarts."""
        with self._db:
            self._db.execute(
                "INSERT INTO people (user_id, name, color) VALUES (?, ?, (SELECT COUNT(*) FROM people) % ?) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                (user_id, name, PERSON_COLORS),
            )

    def people(self) -> list[Person]:
        rows = self._db.execute("SELECT user_id, name, color FROM people ORDER BY name, user_id").fetchall()
        return [Person(r["user_id"], r["name"], r["color"]) for r in rows]

    # dashboard: commands queued from the page, run by the tick
    def enqueue_page_command(self, item_id: str, verb: str, arg: str | None, shown_hash: str, now: datetime) -> int:
        if verb not in COMMAND_VERBS:
            raise ValueError(f"unknown verb: {verb!r}")
        with self._db:
            cur = self._db.execute(
                "INSERT INTO page_commands (item_id, verb, arg, shown_hash, state, created_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?)",
                (item_id, verb, arg, shown_hash, _iso(now)),
            )
        return cur.lastrowid

    def next_page_commands(self, limit: int = 10, *, skip_verbs: tuple[str, ...] = ()) -> list[PageCommand]:
        """Queued commands, oldest first, without those of `skip_verbs` (the ones that wait for the huddle to end)."""
        marks = ", ".join("?" for _ in skip_verbs)
        rows = self._db.execute(
            "SELECT * FROM page_commands WHERE state='queued'" + (f" AND verb NOT IN ({marks})" if skip_verbs else "")
            + " ORDER BY id LIMIT ?", (*skip_verbs, limit)
        ).fetchall()
        return [_row_to_command(r) for r in rows]

    def set_page_command(self, id: int, state: str, result: str | None = None) -> None:
        if state not in COMMAND_STATES:
            raise ValueError(f"unknown state: {state!r}")
        with self._db:
            self._db.execute("UPDATE page_commands SET state=?, result=? WHERE id=?", (state, result, id))

    def claim_page_command(self, id: int) -> bool:
        """Move a `queued` command to `running` (compare-and-set). False when it was not queued any more."""
        with self._db:
            cur = self._db.execute("UPDATE page_commands SET state='running' WHERE id=? AND state='queued'", (id,))
        return cur.rowcount == 1

    def fail_queued_page_commands(self, ids: list[int], result: str) -> int:
        """End these commands as `failed` with `result`, only the ones that are still `queued`: one UPDATE, so a row that
        a tick claimed meanwhile is left to it, and no row is ever left half-way (`running` with no outcome, as a claim
        followed by a second statement would if the process died between the two). Returns how many were ended."""
        ended = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            with self._db:
                cur = self._db.execute(
                    f"UPDATE page_commands SET state='failed', result=? WHERE state='queued' AND id IN ({marks})",
                    (result, *chunk))
            ended += cur.rowcount
        return ended

    def page_commands_in(self, states: tuple[str, ...], *, item_id: str | None = None, verb: str | None = None,
                         limit: int = 100) -> list[PageCommand]:
        """Commands in any of `states` (optionally for one item and one verb), oldest first."""
        bad = set(states) - set(COMMAND_STATES)
        if bad or not states:
            raise ValueError(f"unknown state(s): {sorted(bad)}")
        sql = f"SELECT * FROM page_commands WHERE state IN ({', '.join('?' for _ in states)})"
        args: list = list(states)
        if item_id is not None:
            sql, args = sql + " AND item_id=?", [*args, item_id]
        if verb is not None:
            sql, args = sql + " AND verb=?", [*args, verb]
        rows = self._db.execute(sql + " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
        return [_row_to_command(r) for r in rows]

    def last_page_command(self, item_id: str, states: tuple[str, ...]) -> PageCommand | None:
        """The newest command of one item in any of `states`."""
        bad = set(states) - set(COMMAND_STATES)
        if bad or not states:
            raise ValueError(f"unknown state(s): {sorted(bad)}")
        row = self._db.execute(
            f"SELECT * FROM page_commands WHERE item_id=? AND state IN ({', '.join('?' for _ in states)}) "
            "ORDER BY id DESC LIMIT 1", (item_id, *states),
        ).fetchone()
        return _row_to_command(row) if row else None

    def page_command(self, id: int) -> PageCommand | None:
        row = self._db.execute("SELECT * FROM page_commands WHERE id=?", (id,)).fetchone()
        return _row_to_command(row) if row else None
