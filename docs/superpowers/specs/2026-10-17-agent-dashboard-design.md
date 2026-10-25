# Haseeb's Agent: Dashboard design

Date: 2026-10-17 · Status: draft for Haseeb's review · Builds on the Phase 1 agent (`docs/superpowers/specs/2026-10-17-haseebs-agent-design.md`, `docs/architecture.md`)

## Intent

Today the agent runs in the background, and Haseeb can't see what it is doing. He wants a page on his Mac that shows, live and with animation, what the agent is doing now and what it did. The page also holds the whole history of the leadership channel, organized: huddle notes, everyone's Next Todos, everyone's insights, and the feedback on them. He can act on drafts from the page as well as from his DM. It should look and feel premium.

Success: he opens the page during a workday and immediately sees what the agent is doing and what is waiting for him. He can find any past huddle, todo, insight or feedback in seconds. He can approve a draft with one click, and the DM stays consistent with the page.

## Goals

1. **Live view.** Shows the agent's current activity, step by step, with the model being used and the elapsed time. Updates within ~2 s, without reloading.
2. **History.** Holds everyone's posts in the program channel since it began, organized by day. Each day is laid out in a fixed order: the huddle (notes, plus everyone's Next Todos), then insights, each with its feedback thread underneath. There are views by day, by person and by topic, plus full-text search.
3. **Actions from both places.** Every DM command (`post`, `edit:`, `shorter`, `skip`, `attach`, and approving or cancelling the Next Todo auto-post) can also be given from the page. Both run through the same code path.
4. **Premium look.** Two themes, Midnight (dark, glowing) and Daylight (light, clean), with a toggle. By default the page follows the macOS appearance. The visual reference is the two brainstorm mockups (`.superpowers/brainstorm/14515-1792262301/content/dashboard-look.html`, `history-team.html`): the glowing pipeline, sliding log rows, countdowns, people chips with colors, and day sections.

## Non-goals

- Hosting anywhere other than Haseeb's Mac. Nothing is uploaded and no one else sees the page.
- Replacing the DM flow. The DM stays a full, equal way to act.
- Editing teammates' posts, or any Slack write beyond what the DM commands already do.
- The Slack dashboard canvas for the team (Task 15). That is separate.
- Mobile layout polish beyond "readable on a narrow window".

## Anti-goals (would make it a failure even if it works)

- The dashboard or the history import slows or breaks the live agent. The worst cases are the 14:30 huddle and the 5-minute Next Todo.
- A click on the page posts something twice, or posts something Haseeb didn't approve.
- Another website, or another device on the network, can read the page or press its buttons.
- Teammates' text can run script in the page. Their posts are untrusted input.
- It looks like a generic admin template.

## Constraints

- **Local only.** The page is bound to `127.0.0.1`. Every request needs a random secret: it arrives first as a `?key=` link, then lives in an HttpOnly, SameSite=Strict cookie. The server checks Host and Origin against `127.0.0.1:<port>`/`localhost:<port>`, needs a CSRF token on every POST, sends no CORS headers, and serves a strict CSP with no third-party origins. All assets are vendored, so the page works offline.
- **Untrusted text.** Every piece of Slack text is rendered as text, never as HTML: `textContent` or escaping on the server. Links in Slack text appear as plain text, apart from our own "open in Slack" links built from IDs.
- **One action path.** A page action becomes the same command object a DM reply produces, and runs through the same handler in `commands.py`. That means the same R-rulings: post once, R16 no auto-retry of channel writes, R23 reactions, the R25/R27/R28 content guards, R50 attach rules. Each draft is idempotent: a second `post` from either place answers "already posted". After a page action, the DM thread gets the same ✅/⚠️ reaction plus a short note, "done from the dashboard", so the DM never looks pending.
- **No extra load on the live agent.**
  - New channel messages are stored by the reads the agent already makes (feedback and the huddle and todo reads). The dashboard itself never calls Slack.
  - The one-time history import runs as its own command. It can be resumed, it's paced, and it refuses to run from 14:25 to 16:05 on workdays.
  - The page reads SQLite in WAL mode and never holds a write lock longer than a single insert.
- **Same lockdown for Slack reads.** The import uses the existing `ClaudeConnectorSlack` read path (R15, R42, the parser and the anti-spoof checks). It reads with `slack_read_channel` and `slack_read_thread` only.
- **Development happens in a git worktree.** It's never done in the live tree from 14:25 to 16:05 on workdays. Work is merged only when the tests are green.

## The how: decided with Haseeb

- **Pages:** Live, History, Decks, Logs, Score. A "Waiting for you" inbox of pending drafts sits on Live and acts as the main place for actions.
- **History covers everyone in the channel**, very well organized. Organization:
  - **One section per day:**
    - the huddle: Slackbot's AI notes, plus a row for each person's Next Todo;
    - the insights: each one has its feedback replies nested under it.
  - **People chips**, each person with a stable color. Clicking a chip filters to that person.
  - **Views:**
    - by day (the default);
    - by person (one lane per teammate);
    - by topic (themes such as pricing, crypto, agents).
  - **Search:** full-text search with `person:` and `kind:` filters.
  - **Haseeb's own items** are outlined in the accent color.
  - **Week list and activity grid:** a list of weeks since the program began, and an activity heat grid.
  - **Each item:** opens the full text and has an "open in Slack" link.
- **Both themes**, with a toggle that is remembered, following the macOS appearance by default.
- **Both action places:** the page and the DM.

## The how: Claude's calls (Haseeb can override)

- **Server:**
  - A small Python web server in the existing package: Starlette plus uvicorn, as two new dependencies.
  - Live updates through Server-Sent Events.
  - Started by `haseeb-agent dashboard`, which prints and opens the secret link.
  - A launchd agent, `com.haseeb.agent.dashboard` with KeepAlive, so the page is always up after login. `haseeb-agent dashboard open` opens it again.
  - The key is stored in a 0600 file.
- **Frontend:** plain HTML, CSS and JS modules with no build step, plus vendored fonts and icons. The mockups' CSS variables become the theme system. Animations respect `prefers-reduced-motion`.
- **Live activity source:** a new append-only `events` table, which the jobs write at each step. Each event records the job, the step, the model, the start and end, and ok/fail with a kind. It holds no message text, in line with the logs' no-text rule. The Live page streams that table. The log page reads the existing log files plus `runs`.
- **History storage:** new tables in the existing SQLite store:
  - `messages`: channel, ts, thread_ts, user, text, kind, day;
  - `people`: id, display name, color;
  - `topics`;
  - a full-text index (FTS5) over messages.
  - Haseeb's own drafts, and their outcomes, stay in `items`, and are linked to the messages that were posted.
- **Classifying messages:**
  - **Rules come first**, based on the program's message formats: Slackbot's "AI huddle notes are ready" thread means huddle notes; the "Next todo" format means a todo; daily-insight posts mean an insight; replies in an insight's thread mean feedback.
  - **The cheap model handles leftovers:** the `summarize` step's model (deepseek-v4.1-flash, then gemini-3.5-flash) labels what the rules leave unlabelled.
  - **Topics:** embeddings (qwen3-embedding) plus clustering, with labels written by the cheap model. They are recomputed nightly.
  - All of this is local, and the text passes only to the models the agent already uses.
- **Import:** `haseeb-agent history-import`.
  - It pages the channel from the program start (or the oldest reachable message), then reads the threads of huddle and insight posts.
  - It is paced, with a 2 s gap between calls and at most about 150 claude calls. It is resumable through cursors.
  - It reports counts at the end.

## The how: Claude's calls after the builder check

1. **Page actions go through a queue, and the tick is the only writer.**
   - A click writes a row to a `page_commands` table (item id, verb, edit text, the hash of the item version shown on the page, created_at). Then the dashboard runs `launchctl kickstart gui/<uid>/com.haseeb.agent.tick`, so the tick starts within seconds and doesn't wait for its next minute. The existing tick lock prevents overlapping runs.
   - Every tick, a new job `process_page` (cheap when the queue is empty) feeds each row through the same `_Runner` handlers as DM replies:
     - The created_at time replaces the reply ts for R50 (deck-before-command, both on the Mac clock) and for the `post_cmd_ts` cursor.
     - N2 ("post the version Haseeb saw"): the stored hash must match the item's current body. Otherwise the action is refused with "draft changed, reload".
   - The page shows the status as it changes: queued → 👀 → ✅/⚠️. The dashboard process itself never calls Slack.
2. **The Next Todo from the page.**
   - "Cancel auto-post" queues a `cancel` command. The todo job checks the queue before its auto-post decision, the same way it checks for a DM cancel. This is the only change to the todo job's behavior.
   - "Post now" queues a `post` command with the same content guards (R25/R27/R28).
   - Races are settled by a compare-and-set on the item's status: whichever comes first wins, and the other one answers "already posted" or "already cancelled". Nothing ever posts twice.
3. **The DM after a page action:**
   - the 👀/✅/⚠️ reactions go on the item's own DM message (`dm_ts`);
   - the "done from the dashboard" note goes out through `send_dm`, so own-ts, R23 and R43 apply.
4. **The Decks and Score pages:**
   - **Decks:** each deck with a thumbnail of its first page, the model, the time, the page and image counts, the fact-check result (counts only) and a PDF preview, plus the `attach` action for late decks.
   - **Score:** a page view of `metrics.py`: the weekly +5/−10 score, % human intervention, Next Todos on time, failed job runs, and Slack latency, with a week-over-week sparkline.
   - **Logs:** the log files plus `runs`, with filters and search.
5. **Import and quota:**
   - The import cap of about 150 claude calls is its own budget, apart from the jobs' per-day budgets.
   - The import, the cheap-model classification and the nightly topic recompute are all barred from 14:25–16:05 on workdays. By default they run at night or on weekends.
   - The import floor is the oldest reachable channel message.
6. **SQLite sharing.**
   - The live DB switches to WAL once, after a backup copy of the file is made. All connections, from the agent and the dashboard, get `busy_timeout = 5000`.
   - The dashboard reads through its own connections. Its only writes are `page_commands` rows and the local re-labels.

## What's left to the builder

- Exact table columns and indexes, the SSE message shapes, the component structure, and the animation details, within the mockups' look.
- How each job emits `events`, with no behavior change to the jobs.
- Test strategy: server security tests (Host/Origin, key, CSRF, CSP, XSS rendering of hostile Slack text), action idempotency across the page and the DM, import resume and pacing, and classification on real-shaped fixtures.

## Open risks

- **Classification quality on old messages.** The rules plus the cheap model may mislabel some messages. The page lets Haseeb re-label an item, stored locally only.
- **Import volume.** If the channel history needs many more than about 150 calls, the import stops and reports progress. It continues the next night.
- **Teammates' content stored on the Mac.** It's local and never uploaded. Haseeb may want to confirm the program is fine with that.
