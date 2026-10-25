# Follow-back feedback: answering a reply to Haseeb's feedback (2026-10-21)

Requested by Haseeb on 2026-10-21, after Iqbal answered his reach.io review with four fixes and a new build: "can we
also add followback feedbacks?". He approved this design in chat with "Yes, build it". The trigger is the post's
author replying, or anyone in the thread @mentioning Haseeb.

## Why

When a teammate answers Haseeb's feedback (with fixes done, a question answered, or a new build attached), a good
reviewer checks the claims and replies briefly. On 2026-10-21 a manual follow-back checked each of Iqbal's four fixes
in build 3c5e08e:
- the build opens on Client;
- 0 of 70 page views scroll sideways at 390 px;
- red badges appear only on decisions;
- the build stamp shows.

It then answered his offer of an on-screen timer in two lines. The agent should draft that for Haseeb to approve.

## What it does

### 1. Trigger
A new reply in a thread of the program channel where Haseeb has a posted feedback reply, meaning a `feedback` item
with status `posted` whose post is in that thread, in the last 7 days. The reply counts only when:
- it is from the thread's author (the teammate whose post it is), or it @mentions Haseeb (`<@U08CZCGJW3E>`);
- it is newer than Haseeb's latest message in that thread;
- it is not Haseeb's own message, a bot's or Slackbot's, and not an edit-only event.

### 2. Detection, inside the existing feedback job
The feedback job already reads the channel page. A tracked thread whose page entry shows a newer latest reply than the
one last seen (cursor `followback_seen:<thread ts>`) is read once with `slack_read_thread`. At most 5 thread reads are
made per run, and they are paced like the feedback job's reads. The history capture's D15 queue already notices new
replies, so reuse what fits.

### 3. Evidence
- Read the reply, plus any link or HTML/PDF attachment in it.
- Run the site review (feature 2026-10-21-site-review) on any new link or attached HTML, with the reply's claims as
  extra context: "verify each claim you can, from the screenshots, page text and checks".
- A claim it cannot check is reported as not checked, never as true.
- Reuse the site review's limits, its network safety and its daily cap. A follow-back counts toward that cap.

### 4. Draft
Use the `feedback_writer` chain with a dedicated prompt. All inputs are fenced as untrusted data. The draft is short,
ideally under 400 characters:
1. what was verified, per claim, in one line, with the measured evidence where there is any;
2. an answer to any question put to Haseeb;
3. at most one next step.

The draft uses plain English and no flattery, and ends with the usual "— drafted by Haseeb's Agent" footer in the
channel post.

### 5. Delivery
- The item has kind `feedback`, with the field or flag `followup: true` and its thread ts.
- Its id is `followback-<reply ts>`: one per reply. It is created insert-only and claimed as in the feedback C1 fix, so
  the tick and any manual path cannot both draft it.
- The DM card reads "Follow-back · <name>'s thread", with the reply quoted in short and the site review's
  "Reviewed: …" line.
- The usual verbs apply: `post` replies in the same thread, plus `edit: …`, `shorter` and `skip`.
- On the dashboard it is a feedback card labelled "Follow-back", in the day group of its day.

### 6. Limits
- At most 2 follow-backs per thread, ever.
- At most 6 a day.
- None while the huddle hold is on (the R33 predicate, as for feedback).
- A follow-back never auto-posts.

## Safety (binding)
- Everything from Slack, the reviewed site or a file is data: it can never pick a code path or change the prompt rules.
- All the site review's network rules apply unchanged.
- Nothing is posted without Haseeb's `post`.
- The double-post rules (R16, D8, the claim) apply.

## Tests
- **Trigger and eligibility:** the author's reply; a reply that @mentions Haseeb; a third person's reply without a
  mention (ignored); Haseeb's own reply (ignored); a reply older than Haseeb's last message (ignored); a bot (ignored).
- **Detection and dedupe:** the cursor's "last seen", one item per reply, the claim race, and the caps of 2 per thread
  and 6 a day.
- **The prompt:** claims and site notes fenced, "not checked" for what can't be verified, and the length.
- **Delivery:** the DM card, a `post` into the same thread, and the dashboard label.
- **Failures:** a failed thread read or site review falls back to a draft from the reply alone, marked "Couldn't
  review …".

## Docs
Add follow-backs to docs/architecture.md (the feedback flow, the limits and the DM card) and to the Notion "Who does
what" table, as a "Follow-back" step under feedback.

## Deviations from this spec, 2026-10-21 (fix round 1, after the review)

- **A failed thread read is retried, not drafted from.** The spec had a failed `read_thread` fall back to a draft from the reply alone. The reply's text is only in the thread read, so there is nothing to draft from: the thread is tried again at the next scan, 3 times in all, then left alone until it has a newer reply.
- **The scan is smaller.** It looks at threads whose post is at most 4 days old (the watch is still 7 days from Haseeb's reply or post), rests 30 minutes in working hours and 2 hours otherwise, and walks back through a full page.
- **Both kinds of draft count toward 3 a thread on an insight,** not only the replies.
- **The follow-back reviews the reply it answers and no other,** says "partly checked (first screen only)" for a claim about more than one first screen, and says "not checked" when nothing was reviewed.
- **The "no invented number or address" check** compares whole tokens and uses the link guard of the feedback drafts.
- **`haseeb-agent watch --dry-run`** was added: the scan for real, with no draft, DM or reaction, no store write by the watch, and no text printed. Its Slack reads are real (headless Haiku runs, up to 16, on the shared quota) and, as on any read, the gateway may write `new_tools_alert:`.
