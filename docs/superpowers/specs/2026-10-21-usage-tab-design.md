# Dashboard "Usage" tab: cost and tokens per model (2026-10-21)

Requested by Haseeb on 2026-10-21: "can we add another tab in the dashboard to see the total cost or the tokens being
used by each of the model? can we add that breakdown as well?". He approved this design in chat with "Yes, with
history".

## Why

The agent calls several models: Claude headless runs, the company LiteLLM proxy and Codex. Haseeb wants to see
what each one costs and uses. On 2026-10-19 the shared Claude weekly limit stopped the agent for a day, so knowing
where the tokens go matters.

## What gets recorded

There is a new append-only table, `model_usage`, with one row per model call and never any text. Its columns:
- `at`: ISO time with the tz;
- `provider`: `claude`, `litellm` or `codex`;
- `model`;
- `job`: insights, feedback, todo, decks, slack, site_review, compose, history, weekly, deck_test or other;
- `step`: research, writer, judge, a Slack tool name, embed, summarize, designer, cover and so on;
- `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`;
- `cost_usd`, which may be null, and `cost_basis`, one of `list`, `proxy`, `subscription` or `unknown`;
- `latency_ms`, `ok`, `source` (`live` or `backfill`) and `ref` (for dedupe).

**How each provider is recorded.**
- **Claude (`claude -p`).** Use the result event's `usage` and `total_cost_usd`, which `ClaudeResult` already parses.
  The cost basis is `list`: it is the CLI's list-price figure, while the runs are paid from the Max subscription quota.
- **LiteLLM.** Use the response's `usage` (prompt, completion and cached tokens, where present). For the cost, use the
  `x-litellm-response-cost` response header when the proxy sends one, with basis `proxy`. Otherwise leave it null with
  basis `unknown`. Embeddings record the prompt tokens.
  - Never invent prices. The builder adds no price table unless the proxy exposes one.
- **Codex (`codex exec --json`).** Use the token counts from the stream if they are present (for example a
  `turn.completed` usage block), with basis `subscription` (ChatGPT Pro) and a null cost. If the stream has no
  counts, record the call with null tokens.

**Attribution.** Job and step come from a context variable. The job entry points set it, and the existing
`events.step(store, job, step, model)` context manager supplies the step. The Slack gateway's runs use the Slack tool
name as their step, under the current job.

**Recording is best effort.** A database error is logged by type and swallowed, and it never changes the job's own
outcome. Recording happens at the lowest shared call paths only (`run_claude`, `LLM._post`/the embeddings call and
the Codex runners), so that other branches merge with few conflicts.

## History (one-time, idempotent): `haseeb-agent usage-backfill`

**Claude.** Every headless run left a Claude Code transcript under
`~/.claude/projects/-Users-cb--haseeb-agent-claude-home/*.jsonl`: 701 files from 2026-10-17 01:31, with `usage`
blocks, `costUSD` and `model` on their messages. For each session the backfill sums the usage and cost per model.
- `at` is the session start.
- `source` is `backfill` and `ref` is the session file name, for dedupe.
- The job and step are inferred from the session's first user prompt (the Slack gateway prompt names the tool, and
  research and writer prompts have known openings), else `other`.
- No transcript text is ever stored.
- Sessions newer than the first live record are skipped, so nothing is counted twice.

**LiteLLM.** The backfill tries the proxy's spend endpoints with the agent's key (`/spend/logs`, `/key/info`). If one
answers, it imports the per-model daily totals as `backfill`. If not (401, 403, 404 or a timeout), it skips with one
line, and the tab says "Company-proxy history not available; counted from <first live date>".

The command prints counts only. It is safe to run again.

## API and tab

**API.** `GET /api/usage?range=today|7d|30d` returns:
- totals: cost, input and output tokens, calls and failures;
- `by_model`: provider, model, calls, tokens, cost, basis, median latency and failures;
- `by_job`;
- a daily series for the last 14 days, stacked by model, for the chart;
- the basis notes.

It is read-only and behind the existing security middleware.

**Tab.** A new tab, "Usage" (`#/usage`), after Score. It shows:
- totals cards for Today, 7 days and 30 days;
- a by-model table, sortable, with provider badges;
- a by-job table;
- a daily stacked bar chart in inline SVG (no libraries; the strict CSP allows no inline script);
- a note saying what each cost means: "list price, paid from your Claude subscription", "company proxy cost" or
  "ChatGPT Pro (tokens only)".

It works in both themes and at phone width, and all text is rendered as text.

## Constraints (binding)
- No message, prompt or page text is ever stored in `model_usage` or logged.
- Recording never makes a job fail or slow down noticeably: one INSERT, done after the call.
- Tests make no real calls. Backfill tests use fixture transcripts with fake data and a fake proxy.
- docs/architecture.md gets a "Usage" section and a row in the dashboard table.

## Left to the builder
- the exact job-inference rules for backfilled Claude sessions;
- the chart's look;
- whether the tab polls or uses the stream.
