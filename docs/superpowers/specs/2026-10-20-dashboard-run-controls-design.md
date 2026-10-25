# Dashboard run controls: design (2026-10-20)

Requested by Haseeb on 2026-10-20 ("add all these manual buttons on the dashboard … so i can directly force start
from there"). He approved this list in chat and asked for it to be built.

## Why

On 2026-10-20 every manual run was started by Claude from the CLI because the 08:15 insights had not run: a forced
insights run, one tick, the deck build and a Slack identity check. Haseeb wants to start these himself from the
dashboard. He also needs a fast kill switch. On 2026-10-19 the agent posted from the wrong Slack account and was
stopped by unloading launchd by hand.

## Goals

### 1. Run controls (Live page)

| Control | What it runs | Notes |
|---|---|---|
| Check round | `kick_tick` (already in app.py) | The tick runs now: DM replies, deliveries, Next Todo, feedback, page clicks. |
| Force insights | `haseeb-agent insights --force` | New research plus up to 3 more insights in the DM. An inline confirm says so. Refused while `insights.lock` is held. |
| Build decks | today's missing or given-up decks get another try, then `haseeb-agent decks` | Refused while `decks.lock` is held. |
| Weekly report | this week's report to the DM, now | Must not stop Friday's scheduled report (if `weekly` marks the week as sent, a dashboard run must not set that marker). |
| Import team history | `haseeb-agent history-import` | It holds the tick lock and refuses inside the huddle window (plus its 180 s clearance). The control says the regular checks wait while it runs, and shows the CLI's refusal reason. |
| Label topics now | `haseeb-agent history-enrich` | `enrich.lock`. |
| Check Slack account | new read-only CLI `haseeb-agent slack-check` | Calls `slack_read_user_profile` through `ClaudeConnectorSlack._call` (no write, unlike `smoke`). It records cursor `slack_identity` = `{user_id, ok: user_id == settings.haseeb_id, at, error}` and never the raw profile (it holds the email). The Live page shows "Slack: posting as you ✓ (checked hh:mm)", or a red warning naming the other id. |

### 2. Draft feedback on a chosen post

A new CLI command, `haseeb-agent feedback --ts <ts>`, drafts feedback for one channel message. It uses the same
drafting, quality gate and DM delivery (an approvable feedback item) as `run_feedback`. It refuses Haseeb's own posts
and any message that already has a feedback item. The button appears in two places:

- on teammates' top-level posts in History;
- in a "Team posts (last 7 days)" list on the Inbox page. That list comes from the `messages` table and shows each
  post's feedback state.

### 3. Pause / Resume (header, every page)

The pause is the cursor `agent_paused`, set to the ISO time it was paused.

**While paused:**
- the tick skips every job but still records its heartbeat;
- every job command refuses at start: the tick jobs, `insights` (the 08:15 launchd run included), `decks`, `weekly`,
  `history-*` and `feedback`;
- the Slack gateway refuses every write (send, react, upload) with a "paused" error that means *nothing was sent*, so
  an already-running job stops posting at once. Reads stay allowed.

**The dashboard while paused:**
- `/api/action` and `/api/run` answer 409 `paused`, except Resume and Check Slack;
- the header pill shows Paused;
- the Live page shows a banner with a Resume button.

## Non-goals

- No control installs or uninstalls launchd, or rotates the dashboard key: those stay CLI-only.
- No cancel or kill for a running job. Pause stops its posting.
- The dashboard process makes no Slack or LLM calls of its own.

## Constraints (binding)

**Spawning jobs**
- The dashboard only spawns allowlisted jobs: `[sys.executable, "-m", "haseeb_agent.cli", <fixed args>]`, detached
  through cli's existing `_spawn_detached` (double fork, offline guard).
- The only client-supplied value is the feedback ts. It must match `^\d{10}\.\d{6}$` and be a top-level message in
  the configured channel, by a teammate (not Haseeb), in the `messages` table.

**One run per job**
- A job counts as running while its lock is held (a non-blocking flock probe), or for 2 minutes after it was spawned
  if it hasn't taken its lock yet. That second rule is the same as `_start_decks`' `decks_spawned_at`.
- A second request gets 409 with the job's state.

**Security**
- New routes sit behind the existing SecurityMiddleware (session token, Host, Origin, Sec-Fetch).
- Actions are POST only.
- CSP is unchanged: no inline script or style.
- No `window.confirm`: use an inline confirm, like the deck Attach.

**Feedback to the user**
- Progress shows in the Live feed through the existing `events` module.
- Each job records `job_last:<name>` = `{at, ok, summary}` when it ends, so its control shows "Last run hh:mm ·
  ok/failed".

**Tests, design and docs**
- Tests never make real calls (`HASEEB_AGENT_OFFLINE`, tests/conftest.py).
- Add API, CLI, gateway (pause) and frontend render tests in the existing styles.
- Use both themes, work at phone width, and use the existing design tokens and `dom.el()` builders.
- In docs/architecture.md, document the controls, the pause and the new CLI commands.

## Left to the builder

- the layout of the Run card and the wording of statuses;
- how `weekly` avoids marking the week;
- how `decks` gives given-up decks another try;
- polling `/api/jobs` versus using the stream.
