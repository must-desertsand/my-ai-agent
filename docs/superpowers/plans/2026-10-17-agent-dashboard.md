# Agent Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A premium local web dashboard for Haseeb's Agent. It shows the agent's live activity, the whole team history of the program channel, decks, logs and score. Haseeb can also act on drafts from the page as well as from the DM.

**Architecture:**
- The jobs write step events and every channel message they read into the existing SQLite store, which runs in WAL mode.
- A separate always-on Starlette process, bound to 127.0.0.1 and guarded by a key, cookie, CSRF and CSP, reads the store and streams updates over SSE.
- Page actions are queued in `page_commands`. The tick (kick-started by the dashboard) runs them through the same `_Runner` used for DM replies, so the tick stays the only Slack writer.

**Tech Stack:**
- Python 3 (existing `haseeb_agent` package)
- sqlite3 with FTS5
- Starlette + uvicorn (new)
- vanilla HTML/CSS/ES modules with no build step and vendored assets
- pytest + Starlette TestClient (httpx is already a dependency)

**Spec:** `docs/superpowers/specs/2026-10-17-agent-dashboard-design.md` (approved 2026-10-17). Agent background: `docs/architecture.md` and `.superpowers/sdd/2026-10-17-haseebs-agent-phase1/global-constraints.md` (rulings R1–R50b).

## Global Constraints

- **Worktree only.** Work in `.worktrees/dashboard` on branch `feat/dashboard`, created from `feat/phase1`. Never edit `/Users/cb/Projects/Must/my-ai-agent` itself: launchd runs that tree live. Merge into `feat/phase1` only after the final review, never from 14:25 to 16:05 Mon–Fri (Asia/Kuwait).
- **Local only.** Bind `127.0.0.1` only, with a default port of `8765` (config `dashboard_port`).
- **Login (ruling D10, amended 2026-10-19, replaces the `?key=` login and D1's `{{csrf}}`).**
  - `dashboard open` mints a single-use login nonce: 32 random bytes, 60 s TTL. Only its sha256 is stored, in `~/.haseeb-agent/dashboard/login`, written atomically in a 0700 directory. A newer nonce replaces it. The URL carries only `?login=<nonce>`.
  - The login path accepts only a user-started top-level navigation (Sec-Fetch-Site `none` or absent, Sec-Fetch-Mode `navigate` or absent). Anything else gets 403 and leaves the nonce untouched.
  - The server compares the guess with the stored hash in constant time first, and consumes it by atomic rename only on a match. It re-checks after the rename and puts a newer nonce back. Wrong, missing, expired or used nonces get 401 and never spend the outstanding nonce.
  - A valid nonce ALWAYS creates a fresh random 32-byte session in server memory (at most 16; 12 h idle, 7 d absolute; expired ones pruned), even when a cookie is already live. It sets cookie `ha_session` (HttpOnly, SameSite=Strict, Path=/, Max-Age 7 d) and answers 303 to `/#c=<one-time code>`. The code is single-use and good for 60 s.
  - Page JS POSTs the code (with Origin checks) to `/auth/exchange`, gets the per-session API token, keeps it in `sessionStorage` and strips the fragment. The token itself never appears in a URL or in history. Every `/api/*` request, GETs included, must send header `X-Session-Token` equal to that session's token, compared in constant time. That header also serves as the CSRF token, so `{{csrf}}` is dropped and index.html is served verbatim.
  - The permanent key in `~/.haseeb-agent/dashboard.key` (0600) is never in a URL, argv or the terminal output. It signs nothing that leaves the server. `dashboard rotate-key` replaces it and restarts the dashboard via `launchctl kickstart -k gui/<uid>/com.haseeb.agent.dashboard`, guarded offline, which clears every session.
  - A server restart logs everyone out; run `dashboard open` again.
  - Residual, documented: cookies aren't port-isolated, so another 127.0.0.1 port can see `ha_session`. Without the sessionStorage token it can't call the API.
- **Host and Origin.** Host must be `127.0.0.1:<port>` or `localhost:<port>`, otherwise 421. Every method except GET, HEAD and OPTIONS needs an `Origin` that is one of those two (otherwise 403) and a valid `X-Session-Token`. Reject `Sec-Fetch-Site: cross-site` on those methods too.
- **CSP.** Every response sends `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin` and `Cross-Origin-Opener-Policy: same-origin`. There are no CORS headers and no third-party origins. All fonts and icons are vendored under `src/haseeb_agent/dashboard/static/`.
- **Untrusted text.** Every Slack text is untrusted. The frontend renders it only through `textContent` or `document.createTextNode`, never `innerHTML` with data. The server returns JSON only, never HTML containing data.
- **The dashboard never calls Slack, claude or LiteLLM.** Its only DB writes are `page_commands` rows and `labels` rows.
- **Text-free logs and events.** No message text goes to logs or `events`, only job, step, model, timing, ok and kind (the existing M6 rule).
- **Off-hours only.** The history import, enrichment and topic recompute refuse to run from 14:25 to 16:05 Mon–Fri, and are paced with at least 2 s between claude calls. The import cap is 150 claude calls per run.
- **Channel only.** Messages stored in `messages` come only from the program channel `settings.channel_id`, never the DM.
- **WAL once.** The live DB is switched to WAL once, after copying it to `data/agent.sqlite.bak-<YYYYmmddHHMM>`. Every connection sets `PRAGMA busy_timeout=5000`.
- **Themes.** `midnight` and `daylight`. The default follows `prefers-color-scheme`, and a toggle is remembered in `localStorage`, wrapped in try/catch. Animations are off under `prefers-reduced-motion`. The visual reference is `.superpowers/brainstorm/14515-1792262301/content/dashboard-look.html` and `history-team.html`.
- **Commits.** TDD. Every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Run `uv run pytest -q` before each commit.

## Review Focus

1. **Hostile Slack text.** A teammate's post containing `<img src=x onerror=…>`, `</script>`, a `javascript:` link or an RTL override must show as literal text on every page, with no script run. Pinned in Tasks 8 and 9.
2. **A double click or a page/DM race on `post`.** A second post from either place must answer "Already posted" and make exactly one channel write. Pinned in Task 5.
3. **The DB is busy when the tick writes.** Dashboard reads during a tick write must neither error nor block the tick for more than 5 s. Pinned in Task 1.
4. **The dashboard opened before any history or events exist.** Every page renders an empty state, not an error. Pinned in Task 9.
5. **The import is interrupted by a crash, a quota failure or the huddle window.** Rerunning must resume from its cursor without duplicating messages. Pinned in Task 3.

---

### Task 1: Store — WAL, busy timeout, new tables

**Files:**
- Modify: `src/haseeb_agent/store.py`
- Test: `tests/test_store_dashboard.py`

**Interfaces:**
- Produces, all on `Store`:
  - `enable_wal(backup_dir: Path, now: datetime) -> Path | None`: returns the backup path, or None if WAL was already on.
  - `add_event(job: str, step: str, *, model: str | None, started: datetime, ended: datetime | None, ok: bool | None, kind: str | None) -> int`
  - `finish_event(id: int, ended: datetime, ok: bool, kind: str | None) -> None`
  - `events_since(id: int, limit: int = 200) -> list[Event]`
  - `upsert_message(m: StoredMessage) -> bool`: True if new or the text changed. Keyed on (channel, ts).
  - `messages(day: date | None = None, user_id: str | None = None, kind: str | None = None, topic: str | None = None, limit: int = 500) -> list[StoredMessage]`
  - `search(q: str, limit: int = 100) -> list[StoredMessage]`: FTS5. `person:` and `kind:` tokens are parsed out as filters.
  - `upsert_person(user_id: str, name: str) -> None`: the color is assigned once, a stable index into a 12-color palette.
  - `people() -> list[Person]`
  - `set_label(channel: str, ts: str, kind: str) -> None`: a local re-label, which wins over the classifier.
  - `enqueue_page_command(item_id: str, verb: str, arg: str | None, shown_hash: str, now: datetime) -> int`
  - `next_page_commands(limit: int = 10) -> list[PageCommand]`
  - `set_page_command(id: int, state: str, result: str | None = None) -> None`
  - `page_command(id: int) -> PageCommand | None`
- New dataclasses, in `models.py`:
  - `Event(id, job, step, model, started, ended, ok, kind)`
  - `StoredMessage(channel, ts, thread_ts, user_id, user_name, text, kind, day, topic)`
  - `Person(user_id, name, color)`
  - `PageCommand(id, item_id, verb, arg, shown_hash, state, result, created_at)`
- Values:
  - `kind` is one of `huddle_notes|todo|insight|feedback|other`.
  - `state` is one of `queued|running|done|failed`.
  - `verb` is one of `post|edit|shorter|skip|attach|cancel`.

- [ ] **Step 1: Write failing tests.**
  - `test_wal_backup_once`: the first call returns a path that exists and `PRAGMA journal_mode` is `wal`; the second call returns None.
  - `test_busy_timeout_set`: `PRAGMA busy_timeout` is 5000 on a fresh Store.
  - `test_upsert_message_idempotent`: the same (channel, ts) twice gives one row; a changed text returns True and updates the row.
  - `test_search_person_and_kind_filters`: `search("pricing person:U1 kind:insight")` returns only U1's insights that contain "pricing".
  - `test_concurrent_reader_during_write`: a second Store reads while the first holds a write transaction, without raising within 5 s.
  - `test_person_color_stable`: re-upserting keeps the color.
  - `test_page_command_queue_order`: commands come back FIFO, and only those in state `queued`.
  - `test_label_overrides_kind`: after `set_label`, `messages(kind=…)` reflects the label.
- [ ] **Step 2:** Run `uv run pytest tests/test_store_dashboard.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Add the tables with `CREATE TABLE IF NOT EXISTS`: `events`, `messages`, `messages_fts` (FTS5, external content), `people`, `labels` and `page_commands`. Set `busy_timeout` in `__init__`. `enable_wal` copies the file with `sqlite3.Connection.backup` before switching.
- [ ] **Step 4:** Run the full suite (`uv run pytest -q`). Expected: PASS.
- [ ] **Step 5:** Commit: `feat(store): WAL, busy timeout, tables for the dashboard`.

### Task 2: Step events and message capture in the jobs

**Files:**
- Create: `src/haseeb_agent/events.py`, `src/haseeb_agent/history.py`
- Modify: `src/haseeb_agent/jobs/feedback.py` (around the `read_channel` at :388), `src/haseeb_agent/jobs/todo.py` (the channel reads at :577, :589 and :932), `src/haseeb_agent/commands.py` (`process_dm`), `src/haseeb_agent/jobs/insights.py` (research, write, judge, deliver), `src/haseeb_agent/jobs/decks.py` (design, render, upload), `src/haseeb_agent/cli.py` (`build_runtime` calls `store.enable_wal` once)
- Test: `tests/test_events.py`, `tests/test_history_capture.py`

**Interfaces:**
- Produces:
  - `events.step(store: Store, job: str, step: str, model: str | None = None) -> ContextManager[None]`: records start, end, ok and the exception kind (`_kind`-style: the class name or the error prefix, ≤80 chars, no text). It never raises its own errors into the job; a DB failure is logged and swallowed.
  - `history.capture(store: Store, settings: Settings, msgs: Iterable[Msg], *, parent: Msg | None = None) -> int`: stores channel messages and returns the number new. Skips anything if `settings.channel_id` isn't the source; callers pass only channel reads.
  - `history.classify(msg: Msg, parent: Msg | None, settings: Settings) -> str`: rules only, as listed below.
- Rules for `classify`, first match wins:
  1. The parent is Slackbot (`is_bot`) and its text contains "AI huddle notes are ready", and the message is in that thread from Slackbot → `huddle_notes`.
  2. The text starts (after whitespace and emoji) with "Next todo" or "Next Todo", case-insensitive → `todo`.
  3. A top-level message with `files` holding a PDF, or a first line that matches `^(daily insight|insight)\b`, case-insensitive → `insight`.
  4. A reply whose parent was classified `insight` → `feedback`.
  5. Otherwise → `other`. Task 4 may relabel these.

- [ ] **Step 1: Write failing tests.**
  - `test_step_records_ok_and_fail`: an ok step and a raising step give two events (ok=True and ok=False, kind="SlackUnavailable"), and the exception propagates.
  - `test_step_survives_db_error`: the store raises in add_event, and the job body still runs.
  - `test_event_has_no_text`: an exception message containing "secret draft text" doesn't appear in any event column.
  - `test_classify_rules`: one case per rule, built with the real-shaped fixtures from `tests/fixtures/` (reuse the existing huddle and Slackbot fixtures).
  - `test_capture_from_feedback_read`: running the feedback job with a FakeSlack page of 3 channel messages stores 3 messages.
  - `test_capture_never_stores_dm`: `process_dm` with a DM page stores 0 messages.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Wrap each named job step in `events.step`, and call `history.capture` right after each channel read listed above. Jobs must behave identically; existing tests must stay green unchanged.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: step events and channel-message capture for the dashboard`.

### Task 3: History import command

**Files:**
- Create: `src/haseeb_agent/jobs/history_import.py`
- Modify: `src/haseeb_agent/cli.py` (subcommand `history-import [--max-calls N]`)
- Test: `tests/test_history_import.py`

**Interfaces:**
- Consumes: `history.capture`, `Store` cursors, `SlackGateway.read_channel` / `read_thread`.
- Produces: `run_import(slack, store, settings, now_fn, sleep_fn, max_calls: int = 150) -> ImportResult(calls, messages_new, threads_done, finished: bool, stopped_reason: str | None)`.

- [ ] **Step 1: Write failing tests.**
  - `test_import_pages_backwards_and_resumes`: a FakeSlack with 3 pages; a stop after 2 calls, then a rerun, gives every message exactly once, with the cursor `history_import:oldest` advancing.
  - `test_import_reads_threads_of_huddle_and_insight_posts_only`: only parents with `reply_count>0` that classify as huddle-bot or insight get `read_thread`.
  - `test_import_refuses_in_huddle_window`: at 14:40 on a Monday, it returns `stopped_reason="huddle window"` with 0 calls.
  - `test_import_stops_entering_huddle_window`: the clock crosses 14:25 mid-run and it stops before the next call.
  - `test_import_paces_calls`: `sleep_fn` is called with ≥2 s between calls.
  - `test_import_cap`: `max_calls=5` gives 5 calls, `finished=False`.
  - `test_import_slack_failure_is_resumable`: a SlackUnavailable on call 3 returns `stopped_reason` and a resume continues.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Page `read_channel` with limit 50, newest to oldest, until an empty page (the oldest reachable message). Keep a thread queue in cursor `history_import:threads` as JSON. The CLI prints counts only and takes the existing tick lock in non-blocking mode, so it never overlaps a tick.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: resumable, paced history import`.

### Task 4: Enrichment — leftover labels and topics

**Files:**
- Create: `src/haseeb_agent/jobs/enrich.py`
- Modify: `src/haseeb_agent/cli.py` (subcommand `history-enrich`, plus a nightly spawn from the tick between 01:00 and 05:00 local, once per day, through `_spawn_detached` like `spawn_insights`)
- Test: `tests/test_enrich.py`

**Interfaces:**
- Consumes: the existing LiteLLM client and router for the steps `summarize` (labels and topic names) and `embed` (`qwen3-embedding`).
- Produces: `run_enrich(store, settings, llm, now_fn, max_items: int = 300) -> EnrichResult(labelled, topics)`. Sets `messages.topic`, at most 12 topics. Topic names are 1–3 words written by the summarize model, chosen from cluster centroid texts.

- [ ] **Step 1: Write failing tests.**
  - `test_enrich_labels_other_only`: messages already labelled by the rules or by `labels` are never sent to the model.
  - `test_enrich_invalid_model_output_keeps_other`: garbage JSON keeps `other` and doesn't raise.
  - `test_topics_assigned_and_capped`: with fake embeddings in 3 clusters, every message gets one of 3 topics.
  - `test_enrich_refuses_in_huddle_window`.
  - `test_nightly_spawn_once_per_day`: the tick at 01:10 spawns it, and at 01:20 it doesn't.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Cluster with a simple k-means (k = min(12, n//8), at least 1) in pure Python over normalized vectors, with no numpy dependency. Use a fixed seed for determinism. Prompts fence message text as untrusted data (the R30 pattern) and ask for JSON `{"kind": ...}` from the fixed set.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: nightly labels and topics for the history`.

### Task 5: Page commands in the tick

**Files:**
- Create: `src/haseeb_agent/page_commands.py`
- Modify: `src/haseeb_agent/commands.py` (`_Runner.handle_page(item, cmd: Command, created_ts: str, cmd_id: int)`), `src/haseeb_agent/cli.py` (job `process_page` first in `JOBS`, due every tick when the queue isn't empty), `src/haseeb_agent/jobs/todo.py` (the auto-post checks for a queued or done `cancel` page command right where it checks DM replies, at :923)
- Test: `tests/test_page_commands.py`

**Interfaces:**
- Consumes: `Store.next_page_commands`, `set_page_command`, `_Runner._execute`, `send_dm`.
- Produces:
  - `body_hash(item: Item) -> str`: the sha256 hex of `item.body`, the hash the page sends.
  - `process_page(slack, store, settings, handlers, now) -> int`
  - `DASHBOARD_NOTE = "_Done from the dashboard_"`.
- Behavior:
  - Each queued row goes `running`, then `done` or `failed`, with `result` holding the status text.
  - `created_ts` is `f"{created_at.timestamp():.6f}"` and is passed as `reply_ts`, which covers R50 and N2.
  - If `body_hash(fresh item)` doesn't equal `shown_hash`, the result is failed with "draft changed, reload", and nothing runs.
  - Reactions 👀 / ✅ / ⚠️ go on `item.dm_ts`.
  - After success, the status text plus `DASHBOARD_NOTE` goes through `send_dm` into the item's DM thread.
  - `cancel` applies to todo items only, and settles the auto-post in the same tick.

- [ ] **Step 1: Write failing tests.**
  - `test_page_post_runs_once`: two queued posts for one item give exactly one `handler.post` call; the second result starts "Already posted".
  - `test_page_then_dm_post_race`: a page post done, then a DM `post` reply in the same tick, gives one channel write and a DM answer of "Already posted".
  - `test_hash_mismatch_refuses`: the item was revised after the page loaded, so the result is failed "draft changed, reload" with 0 Slack writes.
  - `test_reactions_on_item_dm_ts`: the reactions hit `item.dm_ts`.
  - `test_dm_note_via_send_dm`: the own-ts is recorded.
  - `test_cancel_blocks_autopost`: a queued cancel before the auto-post deadline means no auto-post, and the item stays postable.
  - `test_post_now_vs_autopost_race`: an auto-post fired first, so the page post answers "Already posted".
  - `test_page_post_failure_never_retried`: R16. A SlackTimeout gives failed with the `MAY_HAVE_POSTED` wording and no second attempt.
  - `test_attach_from_page`: R50 paths.
  - `test_empty_queue_is_free`: `process_page` makes no Slack call.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `handle_page`. Mirror `handle`, minus the reply message. Reuse `_execute`, `_fail` and `_clear_inflight` unchanged.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: page commands run through the DM command path in the tick`.

### Task 6: Dashboard server, security, launchd

**Files:**
- Create: `src/haseeb_agent/dashboard/__init__.py`, `src/haseeb_agent/dashboard/app.py`, `src/haseeb_agent/dashboard/security.py`
- Modify: `pyproject.toml` (add `starlette`, `uvicorn`), `src/haseeb_agent/config.py` and `config.toml` (`dashboard_port = 8765`), `src/haseeb_agent/cli.py` (subcommands `dashboard serve` and `dashboard open`), `scripts/install-launchd.sh` and `scripts/uninstall-launchd.sh` (label `com.haseeb.agent.dashboard`: KeepAlive true, RunAtLoad true, logs to `~/Library/Logs/haseeb-agent/dashboard*.log`)
- Test: `tests/test_dashboard_security.py`

**Interfaces:**
- Produces:
  - `create_app(settings: Settings, store_factory: Callable[[], Store], key: str, kick: Callable[[], None]) -> Starlette`
  - `load_or_create_key(path: Path) -> str`
  - `kick_tick() -> None`: runs `launchctl kickstart gui/<uid>/com.haseeb.agent.tick`, never raises.
  - `dashboard open` prints and opens `http://127.0.0.1:<port>/?key=<key>`.

- [ ] **Step 1: Write failing tests** with TestClient:
  - `test_no_cookie_401`
  - `test_key_sets_cookie_and_redirects_without_query`
  - `test_wrong_key_401`
  - `test_bad_host_421` (Host `evil.com:8765`)
  - `test_post_bad_origin_403`
  - `test_post_missing_csrf_403`
  - `test_csp_and_headers_on_every_response`
  - `test_no_cors_headers` (an OPTIONS preflight gives no `Access-Control-*`)
  - `test_key_file_0600`
  - `test_binds_localhost_only`: the uvicorn config host is `127.0.0.1`.
  - `test_kick_failure_swallowed`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the middleware in `security.py`. Compare the key and cookie with `hmac.compare_digest`. Serve the static files from the package dir.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(dashboard): local server with key, cookie, CSRF, CSP and launchd`.

### Task 7: Dashboard API

**Files:**
- Create: `src/haseeb_agent/dashboard/api.py`
- Test: `tests/test_dashboard_api.py`

**Interfaces:**
- Consumes: `Store` (Task 1), `metrics.week_stats` / `Stats`, the log files in `~/Library/Logs/haseeb-agent/`, deck PDFs in the existing deck data dir, and `cli.interval_s`, `due` and `tick_state` for the countdowns.
- Produces (JSON unless noted):
  - `GET /api/live`: `{agent:{running, last_tick, mac_awake}, disk_free_gb, now:{job, step, model, elapsed_s}|null, upcoming:[{label, at}], today:[{at, icon, title, detail, ok}]}`
  - `GET /api/stream`: SSE. Events `event` (a new Event row) and `command` (a PageCommand state change), polled from the DB every 1 s, with a heartbeat every 15 s.
  - `GET /api/inbox`: `[{item_id, kind, title, body, shown_hash, status, autopost_at|null}]` for open DM items.
  - `GET /api/history?view=day|person|topic&week=YYYY-MM-DD&person=&kind=&q=`: grouped JSON. Day groups hold `{day, huddle:{notes, todos:[{user, text}]}, insights:[{msg, feedback:[msg]}], other:[msg]}`. Each msg has `{ts, user_id, user_name, color, text, kind, permalink, mine}`.
  - `GET /api/people`, `GET /api/weeks` (week starts since the first message, with counts per day for the heat grid).
  - `GET /api/decks`: `[{item_id, title, model, seconds, pages, images, factcheck, attachable}]`.
  - `GET /api/decks/{item_id}.pdf` and `/thumb.png` (the first page via `sips -s format png`, cached).
  - `GET /api/logs?filter=all|slack|models|decks|problems&q=&before=`: the last 200 matching lines, parsed into `{at, level, source, text}`. Logs are text-free by rule.
  - `GET /api/score?weeks=8`: a list of `Stats` as dicts.
  - `POST /api/action {item_id, verb, arg, shown_hash}`: enqueues, kicks the tick, returns `{command_id}`. Verbs are validated against the item kind; `edit` arg is ≤ 2000 chars.
  - `POST /api/label {channel, ts, kind}`.
  - `GET /api/command/{id}`.
- `mine` = `user_id == settings.haseeb_id`.
- `permalink` = `f"{settings.workspace_url}/archives/{channel}/p{ts.replace('.', '')}"`, built from IDs only.

- [ ] **Step 1: Write failing tests:**
  - `test_live_empty_db`: every key is present, `now` is null, the lists are empty.
  - `test_history_day_grouping`: a seeded huddle, 2 todos, an insight with 2 feedback replies, all in the right slots.
  - `test_history_person_view` and `test_history_topic_view`.
  - `test_search_endpoint_filters`.
  - `test_action_enqueues_and_kicks`: the kick is called once.
  - `test_action_rejects_bad_verb`: `cancel` on an insight gives 400.
  - `test_action_edit_too_long_400`.
  - `test_stream_emits_new_event`: insert an event and get one SSE message.
  - `test_logs_filter_problems`.
  - `test_deck_pdf_path_traversal_404`: an `item_id` of `../../.env` gives 404.
  - `test_permalink_from_ids_only`.
  - `test_score_weeks`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Validate `item_id` against the store before any filesystem path is built.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(dashboard): JSON API, SSE stream and actions`.

### Task 8: Frontend shell, themes, Live and Inbox

**Files:**
- Create: `src/haseeb_agent/dashboard/static/index.html`, `app.css`, `app.js` (router, SSE client, theme), `dom.js` (safe element builders: `el(tag, {class, text, attrs})` that set text only through `textContent`), `live.js`, and vendored `fonts/Inter-var.woff2` with its license file
- Test: `tests/test_dashboard_frontend.py` (static checks), plus a Chrome headless render check reusing `chrome_path`, like the deck tests: skipped if Chrome is missing

**Interfaces:**
- Consumes: Task 7 endpoints. Per D10, the per-session token arrives in the URL fragment `#t=`. `app.js` moves it to `sessionStorage`, strips the fragment, and sends it as `X-Session-Token` on every `/api/*` request. A missing token shows "Session expired — run `haseeb-agent dashboard open`".
- Produces: the hash routes `#/live`, `#/history`, `#/decks`, `#/logs` and `#/score`, and the `dom.el` helper used by Tasks 9 and 10.
- Look:
  - Port the mockup `dashboard-look.html`: tokens for `midnight` and `daylight`, the glowing step pipeline with an animated link, pulsing status dot, countdowns, sliding log rows, the "Waiting for you" inbox cards with Post / Edit / Shorter / Skip, and Cancel auto-post / Post now for todos.
  - Edit opens an inline textarea filled with the draft.
  - The card state moves queued → 👀 → ✅/⚠️ from the `command` SSE events.

- [ ] **Step 1: Write failing tests:**
  - `test_no_innerhtml_with_data`: grep the static JS; `innerHTML` may appear only as `= ''`.
  - `test_no_external_urls`: no `http(s)://` in static files except the license.
  - `test_reduced_motion_rule_present`.
  - `test_theme_tokens_both`.
  - `test_render_hostile_text` (Chrome): seed a message `<img src=x onerror=alert(1)>` and a todo body with `</script>`, render `#/live`, and the DOM text contains them literally with 0 `img` elements.
  - `test_empty_state_renders` (Chrome): an empty DB shows "Nothing waiting for you" and "Quiet right now".
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(dashboard): shell, themes, live view and inbox`.

### Task 9: History page

**Files:**
- Create: `src/haseeb_agent/dashboard/static/history.js`, with styles added to `app.css`
- Test: extend `tests/test_dashboard_frontend.py`

**Interfaces:**
- Consumes: `/api/history`, `/api/people`, `/api/weeks`, `/api/label`, `dom.el`.
- Look:
  - Port `history-team.html`: people chips with stable colors (click to filter), the view switch By day / By person / By topic, kind tabs, and search. The search input is debounced at 250 ms and passes `person:` and `kind:` through.
  - Week list plus activity heat grid.
  - Each day section shows the huddle (notes + a todo row per person), then insights with nested feedback.
  - Haseeb's items are outlined. Each item has "Open in Slack" (the API permalink, `rel=noopener noreferrer`, `target=_blank`) and a re-label menu.
  - Load per week, lazily.

- [ ] **Step 1: Write failing tests** (Chrome):
  - `test_history_hostile_text_literal`: same as Task 8, on the history page.
  - `test_history_empty_state`: "No history yet — run `haseeb-agent history-import`".
  - `test_history_day_order`: huddle before insights, and feedback nested under its insight.
  - `test_person_filter`.
  - `test_relabel_sends_session_token`: the request carries the `X-Session-Token` header.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(dashboard): team history page`.

### Task 10: Decks, Logs, Score pages

**Files:**
- Create: `src/haseeb_agent/dashboard/static/decks.js`, `logs.js`, `score.js`
- Test: extend `tests/test_dashboard_frontend.py`

**Interfaces:**
- Consumes: `/api/decks`, the deck PDF and thumb endpoints, `/api/logs`, `/api/score`, `/api/action` (verb `attach`).
- Look:
  - **Decks:** a thumbnail grid. Clicking one opens the PDF in a new tab; the PDF comes from the same origin and CSP allows `'self'`. Each tile shows the fact-check badge, model and time, and an Attach button when `attachable`.
  - **Logs:** filter chips, search, and newest-first rows that slide in from SSE, with problems in amber.
  - **Score:** four stat tiles (the score, % human intervention, Next Todo on time, failed job runs) plus an inline SVG sparkline per stat over the last 8 weeks, built with `createElementNS`.

- [ ] **Step 1: Write failing tests** (Chrome):
  - `test_decks_empty_state`
  - `test_attach_button_only_when_attachable`
  - `test_logs_problem_filter`
  - `test_score_sparkline_points`: 8 weeks give 8 points.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(dashboard): decks, logs and score pages`.

### Task 11: Docs, install, first import (controller-run)

**Files:**
- Modify: `docs/architecture.md` (the dashboard section: process model, security, page commands, the history pipeline, what the dashboard can and cannot do), `README` if present
- Test: none new. Run the full suite.

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2:** Run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 3:** Commit: `docs: dashboard architecture and operations`.
- [ ] **Step 4 (controller, after the final review and outside 14:25–16:05):**
  1. Merge `feat/dashboard` into `feat/phase1` in the live tree.
  2. Run `uv sync`.
  3. Haseeb runs `scripts/install-launchd.sh` to add the dashboard label.
  4. `uv run haseeb-agent dashboard open`.
  5. Run the first `history-import` on a weekend or evening.
  6. Push to GitHub.
