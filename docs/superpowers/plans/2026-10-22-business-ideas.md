# Daily AI Business Ideas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daily insights job drafts 1–3 AI business ideas (product, buyer, price, build plan, first step) instead of
news insights. Ideas are judged on 12 idea axes, and the DM, the post, the deck and the dashboard show the new format.
Insights stored earlier keep working.

**Architecture:**
- A new `Idea` type sits next to `Insight` in render.py. A stored item's body carries `"v": 2` for an idea; v1 bodies
  stay valid.
- Every reader dispatches on the version. The job, the deck builders and the dashboard get the draft through one helper,
  `insights.draft_from(meta)`.
- The item kind stays `insight` everywhere. The idea dict is stored under the same `"insight"` key, so
  `content_hash(meta["insight"])` in the deck job and the dashboard keeps working unchanged.

**Tech Stack:** Python 3.12, uv, pytest. Tests always run offline (`tests/conftest.py` sets `HASEEB_AGENT_OFFLINE`).

**Spec:** docs/superpowers/specs/2026-10-22-business-ideas-design.md

## Global Constraints

**Internal names stay:**
- the item kind `insight`, the job name `insights`, every cursor name and every config key;
- the writer step `insight_writer` and its chain, and the research step `research = ["claude:sonnet"]`;
- the research tools, `RESEARCH_TIMEOUT_S`, `RESEARCH_MAX_TURNS` and `MAX_RESEARCH_TRIES`;
- `N_CANDIDATES = 6` and `MAX_CANDIDATES = 5`.

**Stored body.** A new item's body is
`{"v": 2, "insight": <idea dict>, "candidate": <idea candidate dict>, "n", "batch", "flagged", "weakest", "judged", "self_judged"}`.
- A body with `"v": 1` stays fully valid: it displays, posts, edits (with the v1 writer and judge kind `insight`),
  shortens, skips, attaches, and builds decks as today.
- Any other version is unreadable, and is marked failed as today.
- Nothing converts old items.

**Scoring and limits:**
- `pass_score` stays at 85 and is never lowered.
- `HOPELESS_BELOW = 60`, `max_rewrites = 2` and `REVISE_REWRITES = 1` are unchanged.
- Judging from another model family (M1) is unchanged.
- `SIGNAL_DAYS = 14`: the newest dated source of an idea candidate is at most 14 days old. Undated sources are allowed.
- `dedupe_days = 30` in config.toml.

**Values:**
- `MARKETS = ("PK", "GCC", "AFRICA", "GLOBAL")`.
- `MARKET_LABELS = {"PK": "Pakistan", "GCC": "GCC", "AFRICA": "Africa", "GLOBAL": "Global"}`.
- Price: one line, at most 30 characters after whitespace is collapsed, non-empty.

**Exact copy.** Section headings, in this order: "The idea", "Why now", "Who pays, and how much", "How we build it with
AI", "First step this week", "Evidence", "Risks · Not verified". The idea emoji is 💡.
- DM header: `**{n}. 💡 {title} · {price}** · {read_min} min read`.
- Post header: `**💡 {title} · {price}**`.
- Index: `☀️ {day label} · Today's {n} idea` (or `ideas`), then lines `{i}. 💡 {title} · {price}`, then
  `Reply in an idea's thread: post · edit: … · shorter · skip`.

**Haseeb-facing wording.** The insights job's own DM lines, banners and alerts say "idea(s)" where they say
"insight(s)" today. v1 renderers keep their v1 wording.

**Safety:**
- Every web-derived or model-written field passes through the same cleaning as v1 fields (`_clean_field`, `_defuse`,
  `_data`, `_text`/`_line`).
- The data rule and the seen-URL check are unchanged.
- The writer may cite only URLs from its candidate.
- Nothing posts to the channel except `InsightHandlers.post`.

**Where work happens:**
- Work only in /Users/cb/Projects/Must/my-ai-agent/.worktrees/ideas (branch feat/ideas).
- Never touch /Users/cb/Projects/Must/my-ai-agent itself, which is the live tree running under launchd, or any other
  worktree. Never run the agent's jobs or CLI against real services.
- Test command: `uv run pytest -q` from the worktree root.

## Review Focus

1. **A v1 item stored before the switch** (today's three). Post, `edit: …`, `shorter`, `skip`, `attach`, its deck and
   its dashboard text must all work exactly as before. Covered in Tasks 2, 3 and 4.
2. **A drafted-but-undelivered v1 batch** delivered by the new code must get the v1 index ("Today's N insights") and v1
   item cards, not idea cards. Covered in Task 2.
3. **A writer price that is multi-line, over 30 characters or empty** makes the draft invalid, so the next model is
   asked. Covered in Task 2.
4. **The 14-day signal edge.** A newest source 15 days old drops the candidate; 14 days keeps it; all-undated keeps it.
   Covered in Task 2.
5. **Text in an idea field that tries to break out** (`</research_candidate>`, `<!channel>`, `{{title}}`, `<script>`)
   is defused in the prompts, the Slack text, the deck brief and the template deck, exactly like v1 fields. Covered in
   Tasks 1, 2 and 3.

---

### Task 1: The Idea type, its Slack rendering, and the idea judge

**Files:**
- Modify: `src/haseeb_agent/render.py`, `src/haseeb_agent/judge.py`
- Test: `tests/test_render.py`, `tests/test_judge.py`

**Interfaces:**
- Produces (render.py):
  - `MARKETS`, `MARKET_LABELS` and `IDEA_EMOJI = "💡"`;
  - `IDEA_COMMANDS_HINT = "Reply in an idea's thread: post · edit: … · shorter · skip"`;
  - the dataclass `Idea(title: str, market: Literal["PK", "GCC", "AFRICA", "GLOBAL"], price: str, idea: str, why_now: str, who_pays: str, build_with_ai: str, first_step: str, risks: list[str], evidence: list[Source], not_verified: list[str], read_min: int)`;
  - `render_idea_dm(n: int, idea: Idea, model_line: str) -> list[str]` and `render_idea_post(idea: Idea) -> list[str]`;
  - `render_index(day, drafts: list[Insight | Idea], flagged, score=None, note=None) -> str`. When every draft is an
    `Idea`, it uses the idea header, idea lines and `IDEA_COMMANDS_HINT`. Otherwise the output is unchanged.
- Produces (judge.py): `IDEA_AXES`, the 12 axes in the spec's table order. `judge(llm, "idea", text, context, writer=...)`
  scores against them, with `_KIND_BLURB["idea"]` naming the 7 sections in order.

- [ ] **Step 1: Write the failing tests**
  - `test_idea_dm_header_sections_and_model_line`:
    - part 1 starts with `**1. 💡 AI receptionist for clinics · Rs 15k/month** · 3 min read`;
    - the 7 headings appear as `**[The idea]**` … `**[Risks · Not verified]**`, in order;
    - the risks are listed first, then the not-verified lines;
    - the text ends with the model line.
  - `test_idea_post_has_footer_and_no_model_line`: it ends with `FOOTER`, and "Wrote:" is absent.
  - `test_idea_fields_are_cleaned_like_v1`: `<!channel>` and `<@U123>` in `why_now` come out exactly as the v1 test
    expects for `what_happened`.
  - `test_index_for_ideas`: "Today's 3 ideas", "1. 💡 T1 · Rs 15k/month", `IDEA_COMMANDS_HINT`, and the singular
    "Today's 1 idea".
  - The existing v1 index tests stay green and unchanged.
  - `test_idea_judge_requires_the_twelve_axes`: a reply with all 12 `IDEA_AXES` parses. A reply missing `money_path`
    is malformed, so the next judge is asked. The system prompt contains "Who pays, and how much".
- [ ] **Step 2: Run them and see them fail:**
  `uv run pytest -q tests/test_render.py tests/test_judge.py`
- [ ] **Step 3: Implement.** Reuse `_insight_body`'s building blocks: `_Links`, `_source_line`, `_clean_field`,
  `split_message` and `_one_line`. The empty-list wording follows v1: "- Nothing flagged." for not-verified.
- [ ] **Step 4: Run the tests again and see them pass.**
- [ ] **Step 5: Commit:**
  `feat(ideas): the Idea type, its DM, post and index, and the idea judge's 12 axes`

### Task 2: The insights job writes ideas, and old insights keep working

**Files:**
- Modify: `src/haseeb_agent/jobs/insights.py`, `config.toml`
- Test: `tests/test_insights.py`, plus `tests/test_commands.py` and `tests/test_cli.py` only where they build insight
  bodies or assert this job's DM wording.

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces (insights.py):
  - `SCHEMA_V = 2`, `SIGNAL_DAYS = 14`;
  - the frozen dataclass `IdeaCandidate(title, market, buyer, problem, idea, signal, price_hint, sources)`, with
    `as_dict()` and `urls()`;
  - `draft_from(meta: dict) -> Insight | Idea`, a public helper used by Tasks 3 and 4 in place of
    `_insight_from(meta["insight"])`;
  - `candidate_from(meta: dict) -> Candidate | IdeaCandidate`;
  - `draft_text(meta: dict) -> str`, the draft's readable text:
    - v1 gives the title, what happened, why it matters and Silicon Valley, as the dashboard shows them today;
    - v2 gives `"{title} · {price}"`, then the idea, why now, who pays, the build and the first step;
    - each part is separated by a blank line.

    The dashboard (Task 4) and, at merge time, the followback branch's readers use it.
- `_meta(item)` accepts v1 and v2, and validates each whole.
- The v1 functions (`_system`, `_parse_insight`, `_insight_from`, `_as_text`, `Candidate`, `_candidate_from`) stay for v1
  items.

**What changes:**
- **Research.**
  - `_research_prompt` becomes the idea prompt:
    - the five signal kinds, the four markets, and team fit (as the spec says);
    - the source rules, the data rule, preferences and recent topics, as today;
    - the reply shape `{"candidates": [{"title", "market", "buyer", "problem", "idea", "signal", "price_hint", "sources": [{"title", "url", "published"}]}]}`;
    - it says `price_hint` is "no price found" when there is none.
  - `_candidate` builds an `IdeaCandidate`:
    - every field is a non-empty string, and `market` (upper-cased) is in `MARKETS`;
    - at least 2 sources the tools really returned;
    - the newest dated source is at most `SIGNAL_DAYS` old;
    - length caps: title 200, buyer 300, problem/idea/signal 1000, price_hint 300.
- **Writer.**
  - `_idea_system(must)` lists the spec's fields, word ranges, the facts-versus-estimates rule and the style rules. The
    v1 style rules carry over word for word.
  - `_user` accepts either kind of candidate and draft, and keeps the same tags.
  - `_parse_idea(text, cand) -> Idea | None` checks:
    - every text field is non-empty, and `market` is in `MARKETS`;
    - the price is valid (see Global Constraints);
    - `risks` is a list of strings with at least one non-empty entry, and at most 6 are kept;
    - `not_verified` is a list of strings;
    - evidence has 2–4 entries, URLs only from the candidate, an invented URL gives None, and duplicates are dropped;
    - `read_min` is an integer from 1 to 10, and an integral float is accepted, as in v1.
  - `_write` tries the next model on None, as today.
- **Compose and judge.** `_compose` writes and judges in the candidate's own version: an idea uses `_idea_as_text`
  (plain text, every URL visible) and judge kind `"idea"`, and v1 uses its existing path. `_judge_context` is
  unchanged.
- **Store.** `_store_batch` writes v2 bodies.
- **Delivery.**
  - `_deliver_item` uses `render_idea_dm` for v2 and `render_insight_dm` for v1.
  - `_deliver_pending` gives `render_index` the result of `draft_from(m)` for each member.
- **Handlers.** `InsightHandlers.post` renders with `render_idea_post` for v2. `revise` composes in the item's own
  version and stores the same version back.
- **Wording.** Change this module's Haseeb-facing text to "idea(s)": `RESEARCH_FAILED`, `NO_FRESH_TOPICS`,
  `WRITING_FAILED`, `BANNER_NONE`, `BANNER_DELAYED`, `FINAL_UNREACHABLE`, the `_batch_banner` text ("☀️ 3 ideas ready")
  and the `request_alert` text. `RESEARCH_DOWN*`, `MODELS_DOWN` and the judged notes have no such word and stay as
  they are.
- **config.toml.** Set `dedupe_days = 30` with a comment: "ideas: no repeat within 30 days".

- [ ] **Step 1: Move the test helpers to v2 and add the v1 ones.**
  - `cand(name, *, market="PK", sources=2, title=None, urls=None, published="2026-10-18")` returns an idea candidate.
  - `idea_for(c, **over)` returns valid writer JSON with `price` "Rs 15k/month".
  - `verdict(each)` scores `IDEA_AXES`.
  - Keep `v1_cand`, `v1_insight_for` and `v1_verdict` (the old shapes, for the compatibility tests), plus a helper that
    stores a ready v1 item.
  - Update each existing test to the v2 helpers. Its assertion stays the same unless it checks v1 text, and then it
    checks the v2 equivalent: `test_stale_candidate_dropped` becomes the 14-day rule, and
    `test_research_prompt_names_topics_and_must_themes` checks the idea prompt.
- [ ] **Step 2: Write the new failing tests:**
  - `test_research_prompt_asks_for_ideas`: it names the markets, `price_hint`, the facts-versus-estimates rule (in the
    writer system) and the reply keys.
  - `test_candidate_needs_idea_fields_and_a_known_market`: a missing `buyer` drops the candidate, and so does
    `market` "EU".
  - `test_signal_age_14_days`: newest sources of 2026-10-05, 2026-10-04 and undated, with today 2026-10-19 (the
    14-day edge): kept, dropped, kept.
  - `test_run_stores_v2_and_delivers_idea_cards`:
    - the body has `v` == 2 and the idea fields;
    - the DM index says "Today's 3 ideas", and part 1 starts `**1. 💡`;
    - the judge call's system names "Who pays, and how much".
  - `test_parse_idea_rejects_bad_fields`, parametrized over: market "EU", a 31-character price, a two-line price,
    empty `risks`, a URL not in the candidate, `read_min` 0. Each makes the writer fall through to the second model.
  - `test_dedupe_window_is_30_days`: an item titled like a candidate from 20 days ago drops that candidate, and
    `settings.dedupe_days == 30`.
  - `test_v1_item_posts_and_revises_as_v1`:
    - `post` gives the v1 sections ("**[What happened]**");
    - `edit: …` calls the judge with the v1 blurb and stores `v` 1;
    - `shorter` works.
  - `test_v1_batch_delivers_with_the_v1_index`: a drafted v1 batch is delivered with "Today's 2 insights" and v1
    cards.
  - `test_unknown_schema_version_is_failed`: `v` 3 is marked failed, and the others still deliver.
  - `test_failure_lines_say_ideas`: the research content failure line is "⚠️ Research failed today — no ideas".
- [ ] **Step 3: Run them and see them fail:** `uv run pytest -q tests/test_insights.py`
- [ ] **Step 4: Implement the changes above.** Write the research and writer prompts in the codebase's prompt style:
  plain words, tagged data, and one JSON reply.
- [ ] **Step 5: Run the full suite and see it pass:** `uv run pytest -q`
- [ ] **Step 6: Commit:**
  `feat(ideas): the insights job researches, writes and judges AI business ideas; v1 insights keep working`

### Task 3: Decks for ideas

**Files:**
- Modify: `src/haseeb_agent/deck/designer.py`, `src/haseeb_agent/deck/factcheck.py`, `src/haseeb_agent/deck/render.py`,
  `src/haseeb_agent/deck/codex.py`, `src/haseeb_agent/jobs/decks.py`
- Create: `src/haseeb_agent/deck/template_idea.html`
- Test: `tests/test_deck.py`, `tests/test_deck_designer.py`, `tests/test_deck_factcheck.py`, `tests/test_decks_job.py`,
  `tests/test_deck_codex.py`

**Interfaces:**
- Consumes: `Idea` and `MARKET_LABELS` (Task 1), and `draft_from` (Task 2).
- Produces:
  - `design_brief(draft, day)` and `repair_brief(draft, …)` accept `Insight | Idea`.
  - An idea brief:
    - the cover shows the title, the price, the market label, "Haseeb's Agent" and "Daily Idea · {kicker}";
    - the suggested flow follows the spec;
    - every other rule is shared with the v1 brief, not copied.
  - `_idea_data(idea, day)` lists every field through `_data()`, with evidence shown as title and host.
  - `source_parts(draft, day)` with idea facts: title, market label, price, the five text fields, risks, evidence
    titles and hosts, and not-verified. Its extras add "Daily Idea".
  - `build_html(draft, day, cover_name=None)` fills `template_idea.html` for an `Idea`. It uses the same cleaning and
    limits as v1, and its slots are the idea fields plus `market`, `price`, `kicker` and the cover slots.
  - `cover_topic` accepts a market label.
  - `jobs/decks.py` reads drafts with `draft_from(meta)` everywhere it called `_insight_from(meta["insight"])`, and gives
    the cover the idea's market.

- [ ] **Step 1: Write the failing tests:**
  - `test_idea_brief_names_daily_idea_price_and_market` checks each field is fenced with `_data`: a `<` in `why_now`
    is defused.
  - `test_factcheck_accepts_the_idea_numbers`: "Rs 750k" from `who_pays` passes, "Rs 900k" fails, and the "Daily
    Idea" branding passes.
  - `test_idea_template_fills_every_slot_and_escapes`: no `{{` is left, `<script>` in a field is escaped, every heading
    is present, and a `{{title}}` inside a field is not expanded.
  - `test_idea_deck_prints_8_pages`: the cover plus one page per section. It is marked and skipped like the existing
    Chrome print tests.
  - `test_decks_job_designs_an_idea_with_draft_from`: the designer receives an `Idea`.
  - `test_v1_deck_unchanged`: the existing v1 tests stay green.
  - `test_cover_topic_with_a_market`.
- [ ] **Step 2: Run them and see them fail:**
  `uv run pytest -q tests/test_deck.py tests/test_deck_designer.py tests/test_deck_factcheck.py tests/test_decks_job.py tests/test_deck_codex.py`
- [ ] **Step 3: Implement.** The idea template keeps the v1 template's CSP, fonts, footer and fit script, with one slide
  per section.
- [ ] **Step 4: Run the full suite and see it pass:** `uv run pytest -q`
- [ ] **Step 5: Commit:** `feat(ideas): decks for ideas: the designer brief, the fact-check and the template deck`

### Task 4: Dashboard text, labels and docs

**Files:**
- Modify: `src/haseeb_agent/dashboard/api.py` and `src/haseeb_agent/dashboard/static/inbox.js` (only if the label
  needs it), `docs/architecture.md`, `docs/quality-metrics.md`
- Test: `tests/test_dashboard_api.py`, plus the inbox frontend test if the label changes.

**Interfaces:**
- Consumes: `draft_from` (Task 2) and `Idea` (Task 1).
- For an insight, `_body_text(kind, body)` returns `draft_text(json.loads(body))`, which is the same text as today for
  v1. An unreadable body gives "".
- The inbox item for a v2 insight is labelled "Idea", and v1 keeps "Insight". The builder chooses the smallest way, for
  example a `format` field in the item JSON.

- [ ] **Step 1: Write the failing tests:**
  - `test_inbox_text_for_an_idea` and `test_inbox_text_for_a_v1_insight_unchanged`;
  - the label test.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** In docs/architecture.md, the insights data flow becomes ideas:
  - the format;
  - the research rules (signals, markets, `SIGNAL_DAYS`, the 30-day dedupe);
  - the v1/v2 schema note;
  - the deck flow.

  In docs/quality-metrics.md:
  - add "Ideas: 12 axes, pass mark 85/100", with the spec's table;
  - say v1 insights were judged on the 10 insight axes.
- [ ] **Step 4: Run the full suite and see it pass:** `uv run pytest -q`
- [ ] **Step 5: Commit:** `feat(ideas): the dashboard shows ideas; docs describe the idea format and its 12 axes`
