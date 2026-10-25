# Daily AI business ideas instead of news insights (2026-10-22)

Requested by Haseeb on 2026-10-22: "the agent is not giving me business ideas, its giving us info only which we dont
want. We need business ideas we need tech startup ideas which we can do with the help of Ai which can potentially bring
us money in this Ai era. Please implement that instead of just giving me random information". He approved this design
in chat with "Yes, switch to this (Recommended)": 1–3 ideas a day in the format below, judged on idea axes.

## Why

The insights job finds fresh AI news and explains why it matters to MUST. Haseeb reads that as information, not as
opportunity. He wants each daily draft to be a tech startup idea that his team can build with AI and sell: a concrete
product, a paying customer, a price, a build plan and a first step.

The ideas must still rest on real, recent, sourced signals, because the program judges posts on evidence. Lion's themes
apply: "if I'm wrong, show evidence", frontier topics and not old stories, the client pays first, and execution over
ideas.

## Goals

- Every daily draft is one AI business idea. A small team can build it within weeks, and someone would pay for it.
- Each idea rests on real, recent signals with sources: the demand, a price point and a "why now".
- The judge scores ideas on idea axes, against the same fixed bar (85).
- The DM, the channel post, the dashboard and the deck all show the new format.
- The switch is live before the next scheduled run that it can make, ideally Fri 23 Oct.

## Non-goals

- No change to the schedule, the retry rules, the delivery flow, the verbs (`post`, `edit: …`, `shorter`, `skip`,
  `attach`) or the safety rules. (The research budget grows: see Research.)
- No change to feedback, follow-backs, the Next Todo, history or the composer.
- No market-sizing reports, investment advice or financial projections beyond one line of money math.

## Anti-goals (a failure even if it works)

- A news summary with an idea tacked on at the end. The idea leads; the news is only the "why now" evidence.
- Generic ideas, such as "an AI chatbot for X" or "an AI marketing agency", with no specific buyer, wedge or price.
- Ideas the team can't do. These need big capital, a license (a bank, an insurer, medical diagnosis, legal advice as a
  service), hardware, or a sales force we don't have.
- Invented demand or numbers. Our own estimates (a price, a customer count, revenue) are fine only when they are
  labelled as estimates.
- Hype, crypto speculation, token launches, or anything that relies on spam, scraped personal data, fake reviews or
  breaking a platform's rules.
- The same idea twice within 30 days.

## The format (approved)

The channel post:

```
💡 *AI receptionist for clinics: books, reminds and re-books patients by WhatsApp · Rs 15k/month*

*[The idea]* …
*[Why now]* …
*[Who pays, and how much]* …
*[How we build it with AI]* …
*[First step this week]* …
*[Evidence]* …
*[Risks · Not verified]* …

— drafted by Haseeb's Agent
```

The sections:
- **Header:** the title, then " · " and the price. The title names the product and its buyer, is specific, and stays
  under 90 characters. The price is one short line of at most 30 characters, such as "Rs 15k/month", "$49/month" or
  "KWD 40/month per branch".
- **The idea** (40–80 words), written as-is → to-be: the buyer's pain today, with a sourced number when there is one,
  then what we build. This also meets the channel's [Problem] / [Idea] norm.
- **Why now** (40–90 words): the recent signal that makes the idea possible or urgent now, with its source. Examples: a
  new model or API, a price drop, a regulation or deadline, a funding round or traction abroad with no local version
  yet, or people asking for the tool.
- **Who pays, and how much** (40–90 words) covers:
  - the buyer (a role and a type of business);
  - what they pay today for the problem, when a source says;
  - our price, labelled as our estimate;
  - the money path, in one line of arithmetic, such as "50 clinics × Rs 15k = Rs 750k/month". The arithmetic must be
    right.
- **How we build it with AI** (50–110 words): the MVP that 2–3 people can ship in at most 2 weeks. It names the models,
  APIs and tools, what is automated and what stays human, and what v1 leaves out.
- **First step this week** (25–60 words): one cheap test that ends by Friday with a number that decides go or stop, such
  as "call 10 clinics, get 3 to pre-commit at Rs 15k". Prefer tests where the client commits or pays first.
- **Evidence:** 2–4 sources from the research, with exact URLs, as today.
- **Risks · Not verified:** first 2–4 risks that could kill the idea (competition, distribution, regulation, model cost,
  trust). Then every claim we could not confirm, including our price guess unless a source supports it.

The DM card is the same, with the numbered header, the read time and the model line, as today. The read time stays an
integer from 1 to 10.

*My call: Evidence comes before Risks · Not verified, so the post ends with the "Not verified" list, as the channel norm
requires. The approved preview had these two the other way round.*

## Research

The research run stays the same in most ways: one run a day, the same model (`research = ["claude:sonnet"]`), the same
tools (WebSearch and WebFetch) and the same retry rules. It still asks for 6 candidates. The budget grows to 45 turns
and 15 minutes (it was 30 turns and 10 minutes, `RESEARCH_MAX_TURNS` and `RESEARCH_TIMEOUT_S`), because an idea needs a
signal, the demand and a price: the 2026-10-22 trial used all 30 turns for one candidate. The prompt and the
candidate's shape change too. The prompt gives a two-pass method (collect leads, then check the best ones for the
demand and a price), aims for 3 to 6 candidates, and says to reply with the candidates it has before the budget runs
out. That is a request, not a guarantee: a run that stops at its turn limit gives no reply, and that ends the day with
its own DM line, with no automatic retry (Force insights tries again).

**What it looks for.** The prompt points the research at signals like these:
1. a new AI capability, model, API or price drop that makes a product newly cheap to build;
2. an AI product with real traction or a fresh funding round abroad, with no good version yet in our markets;
3. a regulation, mandate or deadline that forces businesses to buy something, such as e-invoicing, data protection or
   digital records;
4. people publicly asking for a tool, or paying for one, on forums, in job posts, on freelance marketplaces or in
   app-store reviews;
5. a manual service that businesses already pay for, which AI can now do much more cheaply.

**Markets.** Pakistan, Kuwait and the GCC, Africa (through MUST's network and Global MPC's Africa push), or global
online. The prompt asks for a mix across the day's candidates.

**Team fit.** The team is small and strong in AI-assisted development, AI agents and blockchain. An MVP must take at most
2 weeks, need little capital, and be legally clean. MUST's assets are welcome hooks but not required: the AX-department
clients, and Global MPC's network.

**Candidate shape.** Each candidate is JSON with these fields:
- `title`;
- `market`: one of `PK`, `GCC`, `AFRICA` or `GLOBAL`;
- `buyer`, `problem`, `idea`, `signal` (the why-now) and `price_hint` (what buyers pay today, or a competitor's price);
- `sources`: the same shape as today.

**Validation.** The same as today, with these changes:
- A candidate still needs at least 2 sources that the research tools really returned (the existing seen-URL check).
- **Freshness:** the newest dated source must be at most 14 days old (`SIGNAL_DAYS = 14`). This replaces the 72-hour
  rule. Undated sources are still allowed. *My call:* demand and price evidence can be older; the why-now signal must be
  recent.
- Every required field must be a non-empty string, and `market` must be one of the four values.
- **Dedupe:** the window grows from 7 to 30 days (`dedupe_days = 30` in config.toml). It checks both the stored items
  and the channel's posts, with the existing embedding and title checks. The research prompt's "recent topics" list
  covers the same 30 days.

## Writer

The `insight_writer` chain writes the idea as JSON. The chain is unchanged: claude:sonnet, then kimi-k3. The fields are:
`title`, `market`, `price`, `idea`, `why_now`, `who_pays`, `build_with_ai`, `first_step`, `risks` (a list), `evidence`
(2–4 entries, with URLs only from the candidate), `not_verified` (a list) and `read_min`.

These rules carry over: plain "caveman" language, no invented fact, they/them for everyone, the data rule, and no
greeting, @mentions, hashtags, emoji or headings inside the fields.

**New rule: facts, our plan, and our estimates.** *(My call, after the builder check.)*
- **Facts need a source.** A fact is a claim about the world: a market number, a buyer's pain, a competitor and its
  price, a regulation, a model release, or an API's price or limits. It must come from the candidate or its sources.
  If it doesn't, it is left out or listed under "Not verified".
- **Our plan needs no source and no label.** This is the product we build, the tools we choose for the build, and the
  first step's targets (such as "call 10 clinics, get 3 to pre-commit"). Its section already shows it is our plan.
  Naming a tool is a choice. A claim about that tool's price, limits or abilities is a fact.
- **Our estimates are labelled once, in their section.** These are our price, a customer count, revenue and build time.
  - "Who pays, and how much" says that the price is our estimate. The header shows the price without a label.
  - The money-path arithmetic must be right.
  - Unless a source supports the price, "Not verified" says the price has not been tested with buyers.
- **The judge follows the same line.**
  - `no_invention` marks down unsupported facts, never labelled estimates or plan choices.
  - `money_path` checks the label and the arithmetic.
  - `buildable_with_ai` judges whether the plan is credible, not whether it is sourced.

## Judge

There is a new judge kind, `idea`, with its own blurb and 12 axes.
- Each axis is scored 0–9, as today, and the mean is scaled to 100.
- The bar stays at 85 (`pass_score`), never lowered.
- Rewrites are unchanged: at most 2, and none for a first draft below 60.
- Judging from another model family is unchanged (M1).

The axes:

| Axis | What earns a high score |
|---|---|
| `why_now` | A real, recent signal (14 days or less) makes this possible or urgent now. Not an old story. |
| `real_demand` | A named buyer has this pain today, and evidence shows it: a number, a competitor's traction, or people asking or paying. Not a hunch. |
| `money_path` | Who pays, how much and how often is clear. The price is believable for that buyer and market, it is labelled as our estimate, and the arithmetic is right. |
| `buildable_with_ai` | 2–3 people with today's AI tools can ship a working MVP in at most 2 weeks. The plan names real tools and says what stays human. |
| `first_step` | It can start this week, costs little, and ends with a number that decides go or stop. Better still if the client commits or pays first. |
| `fit_for_us` | We can reach the buyer (Pakistan, GCC, Africa through MUST's network, or online). It needs no capital, license or hardware we lack. |
| `non_obvious` | It has a specific wedge, not a generic "AI chatbot for X" that everyone already sells. |
| `evidence_quality` | Facts rest on named, checkable sources. Weak sources are not passed off as strong. |
| `no_invention` | Every fact, number and name comes from the sources. Our estimates are labelled as estimates. |
| `honest_risks` | The risks name what could really kill the idea, and "Not verified" lists every unconfirmed claim. |
| `plain_language` | Short words and sentences, with no jargon or hype. A busy non-specialist can read it. |
| `structure` | The required sections are present, in order, and the lengths are tight. |

## Delivery and wording

- **DM index:** "☀️ Thu 22 Oct · Today's 3 ideas", then one line per idea: "1. 💡 <title> · <price>".
- **Per-idea DM:** "**1. 💡 <title> · <price>** · 3 min read", then the sections and the model line.
- **Channel post:** "**💡 <title> · <price>**", then the sections and the usual footer.
- **Haseeb-facing wording:** the job's DM lines, banners and notices say "idea(s)" where they say "insight(s)" today.
  Examples are "No ideas today — see DM" and "Research failed today — no ideas".
- **Internal names stay:** the item kind `insight`, the job name, the cursors and the config keys. So the store, the
  commands, the metrics, the dashboard filters and the weekly report need no change beyond reading the new body.

## Deck

- **The designer brief** gets an idea version:
  - The cover shows the title, the price and the market, "Haseeb's Agent", and "Daily Idea · {kicker}".
  - The suggested flow is: the cover; the problem and the idea; why now; who pays and the money math, as one big
    number; how we build it with AI, as a simple flow picture; the first step and its go/stop number; the risks; the
    evidence; not verified.
  - 8–12 slides, and every other designer rule is unchanged.
- **The fact-check** uses the idea's fields as the source text: numbers still come only from the idea's own text. It
  accepts the new branding words.
- **The template deck** (the fallback) gets an idea layout with the same sections.
- **The Codex cover image** prompt uses the idea's title and market.

## Old insights keep working

- Items already stored keep all their behaviour. These are schema v1 and include today's three insights. They still
  work in the DM and with post, edit, shorter, skip and attach, on the dashboard, and with their decks.
- Every reader of an insight item's body dispatches on the stored schema version.
- New items are written in the new schema (v2) only. Nothing converts old items.

## Dashboard

- Live and inbox cards of v2 items show the idea's readable text: the title and price, the idea, why now, who pays, the
  build and the first step. These replace the v1 fields (what happened, why it matters, Silicon Valley) that v1 cards
  show.
- The deck strip is unchanged.

## Constraints (binding)

- The safety rules are unchanged:
  - the data rule, and fencing of all web text;
  - the seen-URL check, and no URL that the research didn't return;
  - nothing is posted without Haseeb's `post`.
- No new model and no new paid API. The research budget is 45 turns and 15 minutes, one run a day (it was 30 turns
  and 10 minutes): ideas need a signal, demand and a price, and the 2026-10-22 trial used all 30 turns for one
  candidate.
- Tests make no real calls (`HASEEB_AGENT_OFFLINE`).
- The live tree is not touched while building. The switch goes live only through the merge, outside 14:25–16:05 on
  workdays.
- docs/architecture.md and docs/quality-metrics.md describe ideas: the format, the research rules and the 12 axes.

## Rollout *(my calls, after the builder check)*

- **Base and merge order.** feat/ideas is based on main (3a68403). Three branches are still unmerged: site-review,
  followback and usage. Whichever merges second resolves the overlap at merge time.
- **A shared text helper.** The followback branch has its own readers of the v1 body (`insight_text()` and `_classify()`
  in its `jobs/followback.py`). feat/ideas adds a public helper, `draft_text(meta)`, that returns the readable text of
  a v1 or v2 body. The dashboard uses it. At merge time, the controller switches followback's readers to it and adds a
  test that an idea's thread gets the idea's text.
- **A real trial before the first scheduled run.**
  - Once the job task passes review, the controller runs one real research, write and judge pass from a scratch
    script, with a scratch DB and no Slack at all. It shows Haseeb the resulting ideas and their scores, and the
    candidates the validator dropped.
  - It costs one day's research run plus its writes and judges. That is the evidence that real model output survives
    the stricter checks and can reach the bar.
  - It changes no product code.
- **The merge.** The controller merges, outside 14:25–16:05 on workdays, after the final review, then restarts the
  dashboard as in earlier deploys.
- **Rollback.** On main the ideas merge does not stand alone: the follow-back reader imports the idea readers
  (`draft_from`, `SCHEMA_V2`) from `jobs/insights.py` (integration commit `1e4ef82`), and one dashboard test pins the
  inbox label that the merge resolution changed (test commit `e49e241`). So a rollback reverts the ideas merge
  together with those two commits, or resets main to the commit before the ideas merge, and then runs
  `haseeb-agent smoke`. Reverting the ideas merge alone would break the feedback job's import. Ideas drafted in
  between have v2 bodies that the old code marks failed. They are drafts, so nothing posted is lost.

## Left to the builder

- The prompt wording.
- The module layout: where the idea type lives, and how readers dispatch between v1 and v2.
- The template deck's look.
- Exact word limits within the ranges above.
