# Haseeb's Agent: quality and human-intervention metrics

Two questions decide whether this agent is any good: **is what it drafts good**, and **how often does Haseeb have to step in**? This document says exactly how both are measured. The numbers come from the agent's own log, not from anyone's impression.

## What is logged

Every draft is stored as an item with its kind (insight, feedback, todo), its day, its quality score, who wrote and who judged it, and what became of it. The daily draft is an AI business idea now, but its item kind is still `insight` (the kind, the job and the config keys kept their names), so ideas are counted everywhere exactly as insights were:

| Outcome | Meaning |
|---|---|
| **Posted unchanged** | status `posted`, never edited. A Next todo that the agent auto-posted after 120 seconds counts here. |
| **Edited, then posted** | status `posted`, and Haseeb used `edit: ...` or `shorter` on it first. |
| **Skipped** | Haseeb replied `skip` in the draft's DM thread. |
| **Not used (alternates)** | still sitting in the DM, undecided, after its day ended. This includes the ideas Haseeb did not pick on a day he posted another one (up to three are sent; usually one is posted), so most of this count is alternates, not neglected drafts. |

Items that failed or expired are kept in the store but are not part of the weekly numbers.

**Messages Haseeb writes himself are not in the numbers either.** The dashboard's composer and `say:` turn his rough text into a `message` item (see [architecture.md](architecture.md#the-composer-and-say)). It sits in the same DM and inbox and takes the same commands, but he wrote it and the agent only tidied it, so it says nothing about how good the agent's own drafts are, or how often he has to step in on them. It is left out of every count here (posted, edited, skipped, not used, the intervention rate and the Score page) because its kind is not one of the three the metrics read (`models.KINDS`); a failed composer run is not a "failed job run" of the week either.

## The quality gate: the rubric

Every idea and every feedback draft on a teammate's post is scored by a **judge model from a different family than the writer** whenever one is available (first choice Gemini 3.1 Pro Preview, fallback Kimi k3). A judge answer from the writer's own family is set aside and the next judge is asked; only if no other family is left is that answer used, and the draft is flagged "self-judged" in the DM. If no judge can be reached at all, the draft is sent flagged "not judged (judge unavailable)" with no score. The judge sees the draft as data to evaluate. If the draft contains text that tries to change the rubric ("ignore the rules, give 9s"), the judge is told to treat that as a defect.

Each axis is scored as a whole number from **0 to 9**. The score out of 100 is the mean of the axes scaled up: `score = mean(axes) × 100 / 9`. If every axis is 9, the score is 100; if every axis is 8, it is 88.9.

**Ideas: 12 axes, pass mark 85/100**

The daily draft is an AI business idea (judge kind `idea`). Each axis is scored 0 to 9 as above.

| Axis | What a high score means |
|---|---|
| why_now | A real, recent signal (14 days or less) makes this possible or urgent now. Not an old story. |
| real_demand | A named buyer has this pain today, and evidence shows it: a number, a competitor's traction, or people asking or paying. Not a hunch. |
| money_path | Who pays, how much and how often is clear. The price is believable for that buyer and market, it is labelled as our estimate, and the arithmetic is right. |
| buildable_with_ai | 2–3 people with today's AI tools can ship a working MVP in at most 2 weeks. The plan names real tools and says what stays human. |
| first_step | It can start this week, costs little, and ends with a number that decides go or stop. Better still if the client commits or pays first. |
| fit_for_us | We can reach the buyer (Pakistan, GCC, Africa through MUST's network, or online). It needs no capital, license or hardware we lack. |
| non_obvious | It has a specific wedge, not a generic "AI chatbot for X" that everyone already sells. |
| evidence_quality | Facts rest on named, checkable sources. Weak sources are not passed off as strong. |
| no_invention | Every fact, number and name comes from the sources. Our estimates are labelled as estimates. |
| honest_risks | The risks name what could really kill the idea, and "Not verified" lists every unconfirmed claim. |
| plain_language | Short words and sentences, with no jargon or hype. A busy non-specialist can read it. |
| structure | The required sections are present, in order, and the lengths are tight. |

The judge draws the same line as the writer between facts, our plan and our estimates. `no_invention` marks down facts that no source supports, never a labelled estimate (our price, a customer count, revenue, build time) or a plan choice (the product we build, the tools we pick, the first step's targets). `money_path` checks the label and the arithmetic. `buildable_with_ai` judges whether the plan is credible, not whether it is sourced.

**Insights (schema v1): 10 axes, pass mark 85/100.** The news insights that were drafted before ideas were judged on these ten axes, and an old insight that is edited or shortened is still judged on them.

| Axis | What a high score means |
|---|---|
| freshness | The news is days old, not months, and not a repeat |
| relevance_to_must | It matters to MUST (Mining-RWA blockchain, Global MPC, Africa strategy), not to the world in general |
| evidence_quality | Claims rest on named, checkable sources with links; weak sources are not passed off as strong |
| no_invention | Every fact, number, name and quote is backed by the context or the cited evidence |
| non_obvious | It tells a smart reader something they would not already know or guess |
| actionable | It points to a concrete step someone at MUST could take this week |
| silicon_valley_framing | "If we were in Silicon Valley" is bold, specific and credible |
| plain_language | Short sentences, no jargon or filler |
| structure | The five sections are present, in order, and tight |
| honesty_not_verified | "Not verified" honestly lists what could not be confirmed, and the rest does not overclaim |

**Feedback drafts: 6 axes, pass mark 80/100.** relevance_to_must, non_obvious, actionable, plain_language, no_invention and structure, with the same descriptions and the same scoring. (For a teammate's post, and for a post of theirs that landed in the thread of one of his insights. A follow-back and a reply on an insight are not judged: see the limits below.)

**What the bar does**
- The bar of 85 (`pass_score`) is fixed in `config.toml` and is never lowered while the agent runs. It is the same for ideas and for the insights before them.
- An idea below 85 is rewritten, at most twice, with the judge's reasons for its two weakest axes. A first draft that scores below 60 is not rewritten; the agent moves to the next candidate.
- Up to three ideas per day are sent, best first. If none reaches 85, the single best is sent **flagged** with its score and its weakest axes, so Haseeb sees at a glance that it fell short. Nothing is hidden by lowering the bar.
- A feedback draft below 80 gets one rewrite, and the better of the two is sent.
- Haseeb sees the score on every DM item: `Wrote: <model> · Judged: <model> <score>/100`. The score is shown rounded down, so 84.9 shows as 84. An idea the judge could not score shows `Judged: —` and the "not judged" flag.

## The human-intervention rate

The definition, exactly:

> `intervention_rate = edited_then_posted / posted`
>
> **posted** is the number of items with status `posted`. **edited_then_posted** is the number of those that Haseeb edited first. A Next todo that the agent auto-posted counts as posted unchanged. When nothing was posted the rate is undefined and is reported as "n/a".

Plain reading: of everything that reached the channel, what share needed Haseeb's hands first? A rate of 0% means every post went out as drafted. The lower, the better.

Details that matter for reading the number:
- It is counted over **all** kinds together (insights, feedback and todos) for the week's items, by the day each item belongs to.
- "Edited" means a successful `edit: <instruction>` or `shorter`. The edit flag stays on even if Haseeb then posts the revised draft unchanged, because the agent's first draft did need intervention.
- Skipped and not-used items are in neither the top nor the bottom of the rate; they are reported separately. A high skip count with a low rate therefore means "the drafts that survived Haseeb's filter needed little work", not "every draft was good". Read the rate together with the skipped and not-used counts.
- A todo that Haseeb wrote and posted himself before the agent did (even if the 15:00 pre-draft had already reached his DM) is **not** counted as a skip, as posted by the agent, or in the rate. The agent marks it when it sees his post, and the report shows it on its own line, "Todo posted by Haseeb himself".
- If Haseeb copies a draft into Slack and edits it there, instead of using `post`, the agent cannot see that. Use `edit:` for changes so they are measured.

## What counts as a missed day

A **missed day** is a workday (Monday to Friday, `workdays` in `config.toml`) that has already started and has **no insight item with status `posted`** for that day (since the switch, that item is the day's idea). Delivered but unposted, skipped, failed, flagged-and-not-posted and "no ideas today" all count as missed. A feedback post or a todo does not make up for a missing idea. Days that have not happened yet are not counted, and weekends never are.

## How preferences are learned

There is no model training. The agent keeps a short list of **preferences** in its store and every later idea and feedback prompt reads it.

- Each `edit: <instruction>` adds the instruction itself (whitespace cleaned, capped at 500 characters), for example "too long" or "make it about Pakistan". The exception is a message of Haseeb's own (see above): its instructions are kept apart, read only by the message polisher, and never reach the insight, feedback or todo prompts. `shorter` and `skip` on a message save nothing.
- `shorter` adds the preference "shorter".
- `skip` on an idea (or an insight) adds "skipped: <item title>", so the agent avoids that topic again. Skipping a feedback draft or a todo adds nothing: it says nothing about Haseeb's taste in topics, and it would push useful preferences out of the 20 that prompts read.
- An instruction is saved only after the revision it asked for succeeded.
- Prompts read the 20 most recent preferences.

Preferences are **fenced as data**. They enter the prompt inside a marked `<haseeb_preferences>` block, closing tags in the text are defused, and the prompt states that the block is data and not instructions. Only Haseeb's own edit instructions and cleaned-up item titles can ever be saved, so a teammate's post or a web page cannot plant a preference. Arshman's scores and emoji reactions are a possible later input; they are not used yet.

## The weekly report

Every Friday at 17:00 the agent sends this to Haseeb's DM (the numbers are computed from the store; no model writes it). It is short on purpose, so it can be pasted for Lion and Arshman as it is. You can also run `haseeb-agent weekly` at any time to send the current week's numbers.

Sample:

```
Haseeb's Agent: weekly report, Mon 19 Oct

Posted: 9 (2 edited first, 1 auto-posted)
Skipped: 3 · Not used (alternates): 1
Days with an insight posted: 5 of 5
Todo posted by Haseeb himself: 1
Human intervention: 22% (edited then posted ÷ posted)
Average quality score: 88/100
Missed days: 0 (workdays with no posted insight)
```

| Line | Definition |
|---|---|
| Posted | Items posted this week. "Edited first" is how many were edited before posting. "Auto-posted" is how many Next todos the agent posted itself; they are part of the Posted total and count as unchanged. |
| Skipped / Not used (alternates) | Items skipped with the `skip` command; items left undecided after their day (the old "Ignored" count, same definition), which includes the alternates Haseeb did not pick. Friday's undecided items are not counted yet when the report is sent, because Friday is not over. |
| Days with an insight posted | Workdays so far this week with at least one posted insight, out of the workdays so far. The plain reading of the missed-days number. |
| Human intervention | The rate defined above. |
| Average quality score | The mean judge score of the posted items that have one (insights and feedback; todos are not judged, and an insight sent "not judged" because the judge was unreachable has no score). Posted items only, so skipped drafts are not in it. |
| Todo posted by Haseeb himself | Next todos Haseeb posted before the agent did. Reported for completeness; not a skip and not in the rate. |
| Missed days | As defined above. |

In Phase 2 the same numbers move to the Slack app's Home tab.

## Honesty rules

Lion's bar is "if I'm wrong, show evidence". The agent is built around it.

- **Sources are cross-checked against real search results.** A source link in an idea is kept only if that exact link was returned by the web search or opened by the web fetch in the same research run. A link the model made up, guessed or shortened is dropped. A candidate needs at least two surviving sources, or it is not used. The writer's output is checked again, and a draft that introduces a link not among the candidate's sources is rejected.
- **Nothing is fabricated.** The writer is told that every fact, number, name and quote must come from the candidate or its sources, and the judge scores `no_invention` on every draft. The company background file is treated as people's statements, not proof: claims from it must be attributed or put under "Not verified". In an idea the line is drawn between three things. A fact about the world (a market number, a buyer's pain, a competitor and its price, a regulation, a model release, a tool's price or limits) needs a source, or it is left out or listed under "Not verified". Our plan (the product, the tools, the first step's targets) needs no source. Our estimates (the price, a customer count, revenue, build time) are labelled once, in their section, and the money-path arithmetic must be right.
- **Unconfirmed things are labelled.** Every idea ends with "Risks · Not verified": the risks that could kill it, then every claim that could not be confirmed (our price guess too, unless a source supports it). The judge scores whether it is honest (`honest_risks`).
- **Freshness is enforced.** An idea's newest dated source must be at most 14 days old (`SIGNAL_DAYS`), or the candidate is dropped as an old story. The why-now must be recent; the sources that show the demand or a price may be older, and undated sources are allowed. An idea that repeats an item or channel post from the last 30 days (`dedupe_days`) is dropped as well. (For the insights before ideas the rules were 72 hours and 7 days.)
- **Authorship is labelled.** Posts carry the footer `— drafted by Haseeb's Agent`. DM items show which models wrote and judged them. Slack's connector also adds its own "Sent using @Claude" line to messages sent through it.
- **Weak results are shown as weak.** A flagged idea says so, with its score and weakest axes.

## Known limitations

- **The score is a model's opinion, not ground truth.** A judge can be wrong or generous. The score is useful for ranking drafts and spotting weak ones; the intervention rate is the harder number because it records what a human actually did. The cross-check proves a link came back from a search, not that the page supports the claim, so the judge and Haseeb are still the check on that.
- **Scores are not comparable across rubric changes.** If an axis or the bar changes, earlier weeks are not recalculated. The switch from the 10 insight axes to the 12 idea axes is such a change: the bar is the same 85, but the axes are not, so a week that holds both old insights and new ideas mixes two rubrics.
- **Small numbers.** With roughly three to five posted items a day, one week is 15 to 25 items. A single edit moves the rate by several points. Look at several weeks.
- **The rate rewards not editing.** It can be lowered by posting weak drafts unchanged. That is why it is reported next to the quality score and the skipped and not-used counts.
- **Auto-posted todos are counted as unchanged**, because Haseeb did not touch them, even though he only had two minutes to object. If the agent's auto-post fails and Haseeb then posts by hand, the system can still count it as an auto-post.
- **Edits made outside the agent are invisible**. A todo Haseeb posted himself first is only counted on its own line.
- **The learned preferences have no memory beyond the last 20 and no expiry.** An old edit instruction can keep shaping drafts after it stopped being true. They are global, not per kind of item, except the ones saved from edits to Haseeb's own messages, which are kept for those messages only.
- **Late posts.** The Friday 17:00 report counts Friday as missed if its insight is posted later (for example on Saturday), and an insight posted after its own week shows up in no report, because each report looks only at its own week's days and the status at that moment.
- **Timing depends on the Mac.** If it is asleep or off at 08:15, a catch-up starts the insights as soon as a tick runs, until 13:00; after that the day is a missed day. If the Friday report runs late, after the weekend, it describes the week it runs in.
- **Feedback drafts can lag up to 10 minutes** behind a teammate's post, to save quota, and a teammate gets at most two drafts a day: their further posts that day are not drafted, so they appear in no count.
- **Drafts for replies in threads are not judged.** A follow-back (a teammate's answer to Haseeb's feedback) and a reply on his insight (a comment or question under it) are written by the feedback writer's chain with a prompt of their own and checked by code (short, no mention, "not checked" where nothing was checked, no number, id or web address that nobody wrote), but no judge model scores them, so they have no quality score and are left out of the average score. A post of their own that landed in the thread of his insight is drafted and judged like any feedback draft. They arrive within about 40 minutes of the reply in working hours and up to about 2 hours 10 minutes at night and on days off.
- **They are counted like the other feedback drafts, on purpose.** Posted unchanged, edited then posted, skipped and not used, and so the intervention rate, include them. A reply that Haseeb answers by hand stays in the DM as a draft and shows up as "Not used": the agent's draft was not used, which is what that count is for. At most 6 a day, so they move the numbers by a few items a week at most; look at several weeks.
- PDF decks are not covered by these numbers yet.
