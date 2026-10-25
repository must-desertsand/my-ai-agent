# Haseeb's Agent — design

Date: 2026-10-17 · Owner: Haseeb Zafar (Slack `U08CZCGJW3E`, mustcompany.slack.com) · Status: draft for approval

Items marked **(builder's call)** are left to whoever builds this. Items marked **(Claude's call)** were not decided by Haseeb. They are sensible defaults Haseeb can override.

---

## 1. Why this exists

Haseeb is a member of Lion Cho's 3-month leadership program. It runs in the Slack channel **#tf-leadership-q4-2026** (`C0C62C128D8`). The program has three daily rituals:

1. **Share an insight before the daily huddle.** The huddle is Mon–Fri at 14:30 Kuwait time (UTC+3), which is 16:30 PKT. The insight should be a frontier topic tied to current events, backed by evidence, in plain language, and framed as "if we were in Silicon Valley". Members often attach a designed PDF deck.
2. **Give feedback under teammates' insights.** Teammates post insights, ideas, daily reports and PDFs. Members reply in the thread with feedback.
3. **Post your "Next todo" in the huddle thread within 5 minutes of the huddle ending.** Lion enforces this strictly; a member got a "final warning" for missing it.

On 2026-10-16 Lion changed the rules. Each member must post their daily insight **using their own AI agent**. Arshman Afzal scores the posts: **+5 for a daily quality post, −10 for a missed day**. The success metric is **% human intervention**, where 0% is ideal and 100% is poor. Quality improves over time, and the agent must stay legally compliant. Haseeb committed to:

- quality training by an AI agent
- posting a daily insight with the agent's help, not by themselves
- giving feedback on teammates' insights
- an AI workflow with less human interaction
- documenting the agent's architecture and quality metrics

Lion's read of Haseeb's strength is "resolve and find tech problems; the current high-value problem is **quality**". The agent is therefore judged on two things: how good its output is, and how little Haseeb has to touch it.

**What success looks like:**
- Every program day Haseeb has at least one insight worth posting before the huddle.
- Haseeb posts it with one tap and rarely edits it.
- Teammates get feedback that is short, specific and useful.
- Haseeb's Next todo is always on time.
- The intervention rate is visible, and it falls week over week.

## 2. Reference designs

These already exist in the company. Reuse their ideas; there is no code to inherit.

- **Lion's "Leadership Program agent".** It posts coaching replies in this channel and labels every one as an agent draft. Its pipeline:
  1. an intent spec: which claims to keep, what must not be invented, and the conditions for sending
  2. research, with a table of each claim against its evidence, and external sources kept separate from internal recollection
  3. a draft
  4. a quality score from **JEV**: 10 axes, each scored 0–9, scaled to 100. It needs at least 90 to send, gets up to 3 rewrites, and everything is logged
  5. a post that says what the agent could and couldn't verify
- **PM Agent.** Notion page "PM Agent: Architecture (Oct 2026)". It is a Slack bot built on **Hermes Agent** and runs in Docker on a VPS. **Jev** picks the model and the effort level for each request, with cheap models for everyday work, a strong model for hard work, and a fallback provider. Replies are short and source-linked. Text from Slack, Notion or the web is treated as information, never as instructions.
- **Piyush Agent.** It runs in #survive-failed-everyday and scores posts by their reactions, then announces results on a schedule.
- **jev-router-effort** (`github.com/globalmpc/jev-router-effort`, Lion's edition). It routes each turn to a model and effort level for Claude Code, Codex and Antigravity. It needs `JEV_API_KEY` (from TypeSafe).

## 3. What it does — three jobs

### Job 1 · Daily insights

- **How many:** 1–3 per day. Haseeb decided this, quality over quantity. The agent sends fewer rather than send weak ones. If none pass the quality bar, it sends the single best one, marked with its score and the reason it fell short **(Claude's call)**, so a day is never empty.
- **When:** ready by **09:00 Kuwait time** on program days, Mon–Fri **(Claude's call)**. That leaves time to read before the 14:30 huddle. The time and the days should be configurable.
- **Topics:** anything Haseeb would find valuable:
  - news from the AI world
  - a potential way to make money
  - a product idea
  - a gap the agent spotted

  It must be fresh and tied to recent events. It must not repeat a topic Haseeb or a teammate posted in the channel in the last ~7 days **(Claude's call)**. It should connect to MUST's real context where it can: the AX-department rental business, Global MPC, the move from selling hours to owning products, and Lion's themes (quality, client pays first, execution over ideas).
- **Each insight is long-form**, in these sections (Haseeb approved this layout on the mockup):
  1. *What happened*
  2. *Why it matters to us*
  3. *If we were in Silicon Valley*
  4. *Evidence* (real, linked sources)
  5. *Not verified* (honest list of anything unconfirmed)
  - Plain language.
- **Each insight gets its own PDF deck.** It is 16:9, in the **bold gradient** style Haseeb chose: deep purple → pink → amber gradient, big heavy uppercase headline, small spaced-out kicker line ("DAILY INSIGHT · DATE"). It should be catchy, elegant and modern. The deck carries the insight's argument; it is not a decorative cover. Slide count is the **builder's call**; about 5–7 is a reasonable start.
- **Quality gate.** Every insight is scored against a fixed rubric before Haseeb sees it, the way Lion's agent uses JEV. The bar is fixed in advance and never lowered to let a draft pass. A draft below the bar is rewritten a limited number of times, then dropped. Use JEV if Haseeb has access; otherwise the builder implements an equivalent judge **(builder's call)**. Scores are stored.
- **Delivery:**
  - one short **index** message listing the day's titles
  - then **one message per insight**, with the full text, its PDF attached, and buttons:
    - **Post this as my insight**
    - **Edit** (Haseeb types an instruction, e.g. "make it about Pakistan"; the agent revises it and re-sends that card)
    - **Shorter**
- **Posting:** pressing *Post* puts the insight text and its PDF into #tf-leadership-q4-2026 as a new top-level message **from Haseeb's own account**. The post ends with a small footer such as "_— drafted by Haseeb's Agent_" **(Claude's call)**. The program wants agent-made insights, and Lion's and Piyush's agents both label themselves. After posting, the DM card shrinks to one line ("✅ Posted 10:42").
- **Earlier days:** Haseeb can open insights from previous days and post one (see the History view in §4).

### Job 2 · Feedback drafts on teammates' posts

- **Trigger:** the agent watches the channel. A new **top-level** message from a teammate starts a feedback draft (Claude's call on what counts):
  - **Counts:** substantive posts, i.e. an insight, idea, daily report, plan, or a message with a PDF or document.
  - **Doesn't count:** Haseeb's own posts, bots, Slackbot ("A huddle started"), join messages, and thread replies such as Next todos.
  - **Lion's posts are skipped by default and can be switched on.**
- **Reading:** it reads the message, its attached files (PDFs), and the context already in the thread.
- **The feedback** is short and valuable, the opposite of generic praise:
  - what's strong (pros)
  - the flaws or risks (cons)
  - the real potential
  - one sharp question or concrete next step

  It should match the channel's evidence-first style, be specific to what the person wrote, and stay respectful.
- **Delivery:** a DM card that names the post (author, title, link) and shows the draft, with buttons:
  - **Approve & post** (it goes up as a thread reply under the teammate's post, from Haseeb's account)
  - **Edit**
  - **Skip**

  The card shrinks to one line after Haseeb acts on it.
- **Never posts feedback without Haseeb pressing Approve.**

### Job 3 · Next todo after the huddle

- **Trigger:** the "AI huddle notes are ready" message that Slackbot posts in the huddle thread in the channel. Haseeb decided this.
- **Reading:** it reads the huddle notes file linked in that message. It pulls out Haseeb's action items and what was discussed with Haseeb, plus anything still open from Haseeb's recent todos.
- **Draft:** a numbered Next todo in Haseeb's style. The examples below are from 2026-10-15 and 2026-10-16; they are 3–4 short numbered lines headed "Next Todo:".

  ```
  Next Todo:
  1. Quality training by Ai agent.
  2. Post an insight daily with the help of AI agent not by myself.
  ...
  ```
- **Delivery:** the draft is sent to the DM immediately, with **Post** and **Edit**. *Post* adds it as a reply in the huddle thread from Haseeb's account.
- **Auto-post.** If Haseeb doesn't act within **2 minutes** of the draft arriving, the agent posts it on its own. Haseeb approved auto-posting; the 2 minutes is Claude's call, chosen to stay inside Lion's 5-minute rule. This is the only job that may post without a tap. The DM card then shows "✅ Auto-posted 15:19".
- **Late-notes fallback (Claude's call).** The notes don't always arrive quickly; on 2026-10-16 Haseeb posted at 15:14 and the notes came at 15:17. So the agent pre-drafts a Next todo before the huddle ends, from the day's insight, pending feedback and recent todos. When the notes land, it refines the draft. If the notes haven't arrived by 15:25 Kuwait time, it sends the pre-draft for Haseeb to post (no auto-post on this path).

## 4. Keeping the DM organized

Haseeb wants it very organized and readable, with buttons, and is fine getting several long messages.

- **Where, in two phases** (see §6):
  - **Phase 1, from Mon 19 Oct.** The agent writes into Haseeb's real **notes-to-self DM** through the Slack connection Haseeb's Claude account already has, so messages appear as Haseeb. That connection can't show buttons. Haseeb acts by **replying in the item's thread** with a short command:
    - `post`
    - `edit: <instruction>`
    - `shorter`
    - `skip`

    The Slack connection can't edit a sent message or remove a reaction (checked on 2026-10-17). So the agent shows status with reactions: 👀 when it picks up the command, then ✅ added beside it. It also posts a one-line thread reply ("✅ Posted · link"). Items don't shrink until Phase 2.
  - **Phase 2, once a workspace admin approves a custom app** (Piyush Agent shows this is possible). The agent becomes a private Slack app named **Haseeb's Agent**, with real buttons and a Home tab. Slack only shows working buttons in messages from an app, so this DM moves from notes-to-self to the app's own DM, which sits in the sidebar the same way. Posting into the channel stays **as Haseeb**.
  - The buttons named in §3 are Phase 2. In Phase 1 the thread commands above stand in for them.
- **Rules:**
  - each item is one message
  - a card shrinks to a single status line once Haseeb acts on it
  - a day's insights are grouped under that day's index message
  - no chatter: no "working on it…" messages left behind
- **Status reactions, like PM Agent (Haseeb asked for this).**
  - When the agent picks up one of Haseeb's commands in the DM, it reacts 👀 straight away.
  - When the work is done, it swaps that for ✅. If it failed, it uses ⚠️ and gives a one-line reason.
  - In Phase 1, reactions are added from Haseeb's account. So they go **only on Haseeb's own messages in the DM**, never on teammates' channel posts.
- **Model line (Haseeb liked PM Agent's "Jev picked … · Ran …" footer).** Every DM item ends with a small grey context line naming the models used and the quality score, e.g. `Wrote: claude-opus · Judged: gemini-3.1-pro 92/100 · Deck: codex`. This line appears only in the DM, never in what gets posted to the channel. It also feeds the quality-metrics document.
- **Home tab, Phase 2** (the app's own page in Slack) **(Claude's call)**. Until Phase 2, history is in the self-DM: each day is grouped under its index message, and `post` works on any earlier day's item. It has:
  - **Today:** today's insights
  - **Feedback queue:** pending drafts
  - **History:** previous days' insights, each with a *Post* button
  - **Stats:** posts, edits, skips and intervention rate for this week

## 5. Quality and intervention metrics

Haseeb committed to documenting these, and Lion judges on them.

- **Log every item:** what was generated, its quality score, and what Haseeb did with it:
  - posted unchanged
  - edited, then posted
  - skipped
  - ignored
- **Intervention rate:** the share of posted items that needed an edit (posted unchanged = no intervention). The builder may refine this definition but must document it.
- **Learning (Claude's call).** Version 1 learns from Haseeb's own actions. Every edit instruction and skip is saved to a "preferences" file that later drafts read. Examples: "too long", "make it about Pakistan", "skipped: topic already covered". Arshman's scores and emoji reactions on Haseeb's posts can feed the same file later. There is no automatic model training.
- **Visibility:** weekly numbers are shown on the Home tab in Phase 2. In Phase 1 they go in a weekly summary message in the self-DM. It must be easy to produce a short weekly report for Lion and Arshman.
- **The repo must contain an architecture document and a quality-metrics document,** which Haseeb committed to.

## 6. How it's built

**What Haseeb has to work with (from Haseeb):**
- **Claude Max** and **ChatGPT Pro (Codex)** subscriptions. There are no API keys and no `JEV_API_KEY`.
- A Mac to run it on for now. Haseeb also has **their own server**, which becomes the Phase 2 host. Haseeb wants it run locally first.
- **Company LiteLLM proxy** (`litellm.must.codes`): an OpenAI-compatible API with about 20 models, verified working on 2026-10-17. It includes:
  - GLM 5.2/5.3/5.3-flash, DeepSeek v4-pro and v4.1-flash
  - Gemini 3.1-pro-preview and 3.5–3.8 flash, Kimi k3 / k2.7-code, Grok 4.6, MiniMax m3, Nemotron 3 ultra, Gemma 4
  - `translategemma-12b` and `qwen3-embedding`

  The key currently used belongs to **Ho Young Yang**: it has about $460 already spent and no budget cap. Haseeb should get a personal key. The key lives only in a local `.env`, which is never committed.
- Monday 19 Oct is the start date.
- Slack app approval will come later.

**Consequence (Claude's call, flagged to Haseeb).** Hermes Agent and Jev were Haseeb's first choice, but both need API keys or a JEV key that Haseeb doesn't have. They move to Phase 2. Phase 1 uses the two subscriptions directly, through their command-line tools, which are designed to run unattended (`claude -p`, `codex exec`).

**Phase 1 (from Mon 19 Oct, on Haseeb's Mac):**
- **Engines:**
  - **Claude Code** (Max) for research, writing, judging and feedback. Model choice is split by step **(builder's call)**: a small fast model for scanning and filtering, a strong model for writing and judging.
  - **Codex** (Pro) for the deck: slide design, and images if Codex can generate them. The builder verifies this on day one. If Codex can't do it, decks are built by Claude from an HTML/CSS template in the bold-gradient style.
  - **LiteLLM models** for the cheap, high-volume steps:
    - scanning and filtering news
    - topic de-duplication (`qwen3-embedding`)
    - first-pass summaries of teammates' posts and PDFs
    - any translation (`translategemma-12b`)
  - **A second-opinion judge from a different model family**, for example `gemini-3.1-pro-preview` or `kimi-k3`. It scores Claude's drafts so the writer never grades its own work.
- **"Routing"** is a fixed map of step → engine/model, written in config, with a fallback model for each step. It is Jev's idea without the Jev service. If a LiteLLM model fails or returns an empty reply, the step moves to its fallback.
- **Slack access:** the Slack connection already authorised on Haseeb's Claude account. It reads the channel, writes to the self-DM, uploads files, and posts as Haseeb.
  - **Builder verifies on day one** that this connection works in unattended `claude -p` runs.
  - **If it doesn't,** Phase 2's Slack app is needed sooner, and the builder tells Haseeb immediately.
- **Watching the channel:** with no Slack app, the agent can't receive events. It **polls** the channel and the self-DM on a short schedule. The interval is the **builder's call**, roughly every 2–5 minutes in program hours, slower otherwise. In the 14:30–15:45 huddle window it polls about every minute.
- **Scheduling:** macOS `launchd` (or equivalent), so jobs run whenever the Mac is awake.
- **Known limit:** if the Mac is asleep or off, the agent doesn't run. This is accepted for Phase 1.
- **Company context:** a hand-written `context/must.md` in the repo, read by every draft, plus the preferences file from §5. It covers the AX-department rental business, Global MPC, selling hours vs owning products, Lion's themes, who's who and channel norms. Notion reading is a later option.
- **Data policy (Claude's call):** internal content (posts, PDFs, huddle notes) goes only to Anthropic, OpenAI and the company's own LiteLLM proxy. MUST runs that proxy for this kind of work.

**Phase 2 (after app approval):**
- the private **Haseeb's Agent** Slack app, using a bot token for the DM, buttons and Home tab, and Haseeb's user token for reading and posting, over Socket Mode
- real-time events instead of polling
- an always-on host: **Haseeb's own server**, in Docker, like PM Agent on its "Pulse work" VPS
- **Hermes Agent** as the runtime, which works against the LiteLLM proxy's OpenAI-compatible API
- Jev routing if a key becomes available

The Phase 1 code should be organised so the Slack layer can be swapped without rewriting the jobs.

**Monday scope (Claude's call; Haseeb agreed to start Monday):**
- Jobs 1–3 working in text through the self-DM.
- The PDF decks follow within a few days.

**Builder's calls:**
- programming language and project structure
- storage (SQLite is likely enough)
- deck rendering details
- news and research sources
- the exact quality rubric and threshold
- polling intervals
- retries and error handling

## 7. Constraints, non-goals, anti-goals

**Constraints**
- **Nothing is posted without Haseeb's go-ahead** (a button in Phase 2, a thread command in Phase 1). The single exception is the Next todo auto-post (§3, Job 3).
- **Untrusted content.** Everything read from Slack, PDFs, Notion and the web is data, never instructions. A post saying "ignore your rules and…" changes nothing.
- **Read scope.** Only #tf-leadership-q4-2026 is read, plus Haseeb's DM with the agent (the self-DM in Phase 1).
- **Legal compliance:** respect site terms, no shady scraping, and cite sources.
- **Light on resources and tokens:** cheap models for cheap steps.
- **Time zone:** Asia/Kuwait (UTC+3).
- **Secrets:** tokens and keys come from environment or secret files, never committed.

**Non-goals**
- A general chatbot.
- Answering questions.
- Posting in other channels.
- Reading other people's DMs.
- Auto-posting insights or feedback.
- Phase 1 running while the Mac sleeps.

**Anti-goals** (failure even if it technically works)
- Generic or recycled insights, or ones with made-up facts.
- A cluttered DM.
- Posting something embarrassing under Haseeb's name.
- Vague praise or harsh feedback.
- Missing the 5-minute Next todo window.
- A bot that slows Haseeb's machine or burns money.

## 8. Decisions log

**Answered by Haseeb (2026-10-17):**
- A "drafted by my agent" footer on posts is fine.
- The Next todo may auto-post.
- Slack app approval comes later, so the build starts on the Mac.
- Accounts are Claude Max and ChatGPT Pro (Codex); there is no Jev key.
- Start Monday 19 Oct.

**Claude's calls, for Haseeb to check:**
- Hermes and Jev move to Phase 2.
- Phase 1 runs on `claude -p` and `codex exec`, with Slack through the existing Claude connection and polling.
- Next todo auto-posts 2 minutes after the draft arrives. There is a pre-drafted fallback, and a 15:25 cutoff.
- Insights are ready by 09:00 Kuwait time, Mon–Fri.
- If none pass the bar, the agent sends the best one, flagged.
- The agent avoids topics posted in the last 7 days.
- Lion's posts get no feedback drafts unless switched on.
- Product ideas stay as an insight topic, written as insights rather than pitches (Lion redirected pure pitches elsewhere on 8 Oct).
- Learning comes from edits and skips, through a preferences file.
- Company context comes from a hand-written file, not Notion.
- Internal content goes only to Anthropic, OpenAI and the company LiteLLM proxy.
- LiteLLM handles cheap steps and the cross-family judge.
- Phase 2 hosts on Haseeb's own server.
- On Monday, the text versions of all 3 jobs go live first and decks follow.

**Still unknown (not blocking):**
- Exactly how Arshman will count "human intervention". The metric in §5 is our definition; adjust it once Arshman's method is known.
