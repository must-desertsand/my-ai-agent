# Onboarding: run your own copy

This guide is for a leadership-program member who wants their own agent, built from Haseeb's. Setup takes about an hour. At the end the agent drafts for you, in your own self-DM, as your own Slack account.

Read the [README](../README.md) first for what the agent does. [architecture.md](architecture.md) explains how each part works.

## How it works in one minute

- The agent is a Python CLI, `haseeb-agent`. macOS **launchd** runs it on a schedule: `tick` every 60 s, `insights` at 08:15 Mon–Fri, `weekly` on Friday at 17:00. It also keeps the local dashboard alive.
- **Slack has no bot or token.** Every Slack read and write is one headless `claude -p` run that uses the Slack connector on *your* claude.ai account. Each run gets exactly one tool. A gateway (`slack_gateway.py`) checks that the model called that tool with exactly the expected arguments.
- **Writing and judging** go through MUST's LiteLLM proxy (`https://litellm.must.codes`, OpenAI-compatible), except research and the idea writer, which run on Claude.
- **State** lives in one SQLite file, `data/agent.sqlite`. It holds every draft, plus the cursors that make each step run exactly once.
- **Decks** are designed by the Codex CLI and rendered to PDF by headless Chrome. If either is missing, a built-in template deck is used instead.

## 1. What you need

| Need | Why | Check |
|---|---|---|
| A Mac set to Kuwait time (UTC+3), awake during working hours | launchd and Mac banners; the schedule uses local time | `date +%z` prints `+0300` |
| Python 3.12+ and [uv](https://docs.astral.sh/uv/) | runs the project | `uv --version` (the install script expects `~/.local/bin/uv`, or set `UV=`) |
| Claude Code, logged in to your own claude.ai account (Max plan) | every Slack call and the research | `claude --version`, then `claude` once to log in |
| The **Slack connector** on that claude.ai account, signed in to *your* MUST Slack | the agent's only path to Slack | claude.ai → Settings → Connectors → Slack |
| A LiteLLM API key for `litellm.must.codes` | drafts, judging, summaries | ask the team's LiteLLM admin |
| Google Chrome | deck PDFs and the site review | `/Applications/Google Chrome.app` |
| Codex CLI 0.163.0, logged in (optional) | designer decks; without it you get template decks | `codex --version` |
| terminal-notifier (optional) | a Mac banner for each draft, since Slack never notifies you about your own DM | `brew install terminal-notifier` |

**Quota:** the agent shares your Claude plan's weekly limit with your own Claude Code sessions. Once the limit runs out, the agent stops entirely (no Slack at all) until the limit resets. Keep heavy multi-agent dev work light while the agent is live.

## 2. Install

```bash
git clone <this repo> my-ai-agent && cd my-ai-agent
uv sync
uv run pytest -q          # about 5,700 tests, 5–8 minutes; all offline
```

The suite sets `HASEEB_AGENT_OFFLINE=1`, so every real Claude, LiteLLM, Slack, Codex and spawn call refuses during tests. Tests read their own frozen config (`tests/fixtures/config.toml`), so your `config.toml` never affects them.

## 3. Configure

```bash
cp config.example.toml config.toml
cp .env.example .env && chmod 600 .env
```

In `.env`, set `LITELLM_API_KEY=` to your key. `.env` and `config.toml` are git-ignored: never commit them.

In `config.toml`, fill in the lines marked `YOUR_`:

| Setting | What to put | How to find it |
|---|---|---|
| `haseeb_id` | your Slack member ID (`U…`). The name is historical: it means "the owner". `slack-check` refuses to go on unless the connector's account is this one. | Slack → your profile → ⋮ → Copy member ID |
| `self_dm_id` | the same member ID | |
| `self_dm_channel` | your notes-to-self DM as a conversation ID (`D…`) | open your own DM in Slack in a browser: the URL ends in `/D…` |
| `codex_path` | the output of `which codex` (launchd's PATH does not include nvm) | leave it as is if you skip Codex |

Leave everything else alone at first: the leadership channel (`channel_id`), Lion's ID, the workspace, the schedule, the quality bar (`pass_score = 85`) and the model chains. To turn features off, set `decks_enabled`, `deck_designer`, `site_review_enabled` or `notify_enabled` to `false`.

## 4. Make it yours

The prompts are written for Haseeb and name him. Replace **"Haseeb Zafar"** (and "Haseeb" in prompt text) with your name in these files:

- `src/haseeb_agent/jobs/insights.py`: the idea research and writer
- `src/haseeb_agent/jobs/feedback.py` and `src/haseeb_agent/followback_prompt.py`: feedback, follow-backs and insight replies
- `src/haseeb_agent/jobs/todo.py`: the Next Todo
- `src/haseeb_agent/message_prompt.py`: message polish
- `src/haseeb_agent/deck/designer.py`, `src/haseeb_agent/deck/factcheck.py`, `src/haseeb_agent/deck/template*.html`: decks

`context/must.md` is the company background that every draft reads. Edit it to match what you know.

Renaming changes test expectations, so run `uv run pytest -q` again afterwards and update the tests that quote the old prompt text.

## 5. First checks (before going live)

Run them in this order. Each one is safe.

```bash
uv run haseeb-agent slack-check     # read-only: asks the connector whose account it is. Must print your member ID.
uv run haseeb-agent smoke           # sends ONE test message to your own DM and reads it back
```

- If `slack-check` names someone else, stop. The connector on your Claude account is signed in to the wrong Slack account. Fix it on claude.ai before anything else.
- `smoke` also checks the headless lockdown: the run must get only the one tool it asked for. If it fails on a dangerous tool, don't work around it. If it only warns about an unknown connector, add that connector's prefix to `OTHER_CONNECTORS` in `slack_gateway.py`.

To try the dashboard before going live, run `uv run haseeb-agent dashboard serve` in one terminal. Then run `uv run haseeb-agent dashboard open` in another.

## 6. Go live

```bash
scripts/install-launchd.sh
```

This installs four launchd agents (`com.haseeb.agent.tick`, `.insights`, `.weekly` and `.dashboard`). From the next minute on, the agent reads the channel and drafts for you. **One action is automatic:** after the huddle, the Next Todo posts in the huddle thread 2 minutes after it reaches your DM, unless you reply `edit:`, `skip` or `post` first.

- **Stop quickly:** the dashboard's Pause button stops every Slack write within seconds.
- **Stop the schedule:** `scripts/uninstall-launchd.sh` (logs and data are kept).
- **Logs:** `~/Library/Logs/haseeb-agent/` (`tick.log`, `tick.err.log`, `insights.log`, `dashboard.log`), plus the dashboard's Logs page.
- **Optional:** `uv run haseeb-agent history-import` fills the dashboard with the channel's past posts. Run it in an evening, and repeat it until it prints `finished=yes`.

## 7. Daily use

Every draft arrives in your self-DM. Answer in its thread, or use the dashboard:

- `post`: posts the draft as you
- `edit: <instruction>`: rewrites it, and you review the new version
- `shorter`: a shorter version
- `skip`: drops it
- `say: <rough message>` (a new DM message): polishes your own words for the channel
- `post #channel`: posts an idea to another channel you are a member of

The agent learns from your edits and skips (the preferences in the store). The weekly report on Friday shows the human-intervention rate.

## 8. Keep it healthy

- **After every Claude Code update**, run `uv run haseeb-agent slack-check` and `smoke`, and look in `tick.err.log` for "not call", "not_ready" or "unexpected tool". Updates have changed how headless runs load connectors before (2.1.308; see architecture.md, Schedule).
- **The Mac must be awake.** A run missed during sleep starts when the Mac wakes. If the Mac is off, nothing runs.
- `data/` holds copies of Slack messages and every draft. It is git-ignored. Never commit it or share it.

## 9. Working on the code

- **Never edit the live checkout.** launchd runs straight from the working tree, so an edit goes live on the next tick, within a minute. Work in a git worktree on a branch (`git worktree add .worktrees/<name> -b <branch>`). Merge with `git merge --ff-only` only when `uv run pytest -q` passes, and only between ticks (`until ! pgrep -f "haseeb-agent (tick|decks|insights)"; do sleep 2; done`).
- **Never merge from 14:25 to 16:05 Mon–Fri**, or until 18:00 when the huddle runs long. The Next Todo is going out in that window.
- After a merge that touches the dashboard's Python, restart it: `launchctl kickstart -k gui/$(id -u)/com.haseeb.agent.dashboard`. After a dependency change, run `uv sync`.
- **Tests first.** Every behaviour has a test. A new outside call must check `offline.guard()`. A new Slack tool must go through the gateway's one-tool, exact-arguments rule.
- **Where things are:**
  - `src/haseeb_agent/jobs/`: one module per job
  - `slack_gateway.py`: every Slack call
  - `claude_cli.py`: the headless runner and its lockdown
  - `litellm.py`: the proxy
  - `store.py`: SQLite
  - `cli.py`: the commands and the tick's schedule
  - `dashboard/`: the local web app
  - `deck/`: PDF decks
  - `site_review/`: the sandboxed site review
- **Specs and plans** for each feature are in `docs/superpowers/`. Each new feature gets a spec, then a plan.

## Security rules (keep them)

- Secrets only in `.env` (mode 600). Never in `config.toml`, code, tests or logs.
- The agent posts only as its owner. Check with `slack-check` after any change of Claude account or connector.
- Every headless run uses `--permission-mode dontAsk`, user settings off, hooks off and exactly one allowed tool. Don't loosen it.
- The dashboard listens on 127.0.0.1 only, behind a single-use login link.
- The site review opens linked pages in headless Chrome behind a proxy that refuses private addresses. Read the Site review section of architecture.md before you change it.
