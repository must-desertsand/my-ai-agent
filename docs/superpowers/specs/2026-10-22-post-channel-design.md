# Choosing the channel when posting an idea or your own message (2026-10-22)

Requested by Haseeb on 2026-10-22: "when we hit the post button, since we have other channels too where we are posting
stuff like: https://mustcompany.slack.com/archives/C093L3Y8UM6 how about we add a channel selection? We read the
channels we currently have we select that and then we post? possible or no? The feedbacks and everything stay in our
leadership channel we dont change that". He approved this design in chat:
- scope: ideas and his own composed messages;
- the DM command: `post #channel` as well;
- the default: the leadership channel.

## Why

Haseeb posts in more channels than #tf-leadership-q4-2026 (C0C62C128D8), for example #survive-ai (C093L3Y8UM6).
Today every top-level post from the agent goes to the leadership channel. A daily idea, or a message he polished with
the composer, may belong elsewhere. On 2026-10-08 Lion also moved product-idea pitches to their own channel.

## What it does

**The posts it covers.** Two kinds of item get a channel choice when they are posted:
- an idea, or an insight from before the switch (item kind `insight`);
- Haseeb's own composed message (kind `message`).

These stay exactly as they are, replying in their threads in the leadership channel:
- feedback, follow-backs and replies in watched threads (kind `feedback`);
- the Next Todo (kind `todo`).

**The list of channels.**
- **Source:** the connector's read-only tool `slack_list_user_channels`, called with:
  - `types="public_channel,private_channel"`, so DMs and group DMs are never listed;
  - `exclude_archived=true`;
  - `limit=200`.
- **Gateway method:** a new read method, `list_channels()`, returns `(id, name, private)` for each channel.
- **The connector's real format (read live on 2026-10-22, 39 channels):** a Markdown page.
  - It starts with `## My Channels (showing N of M total)`.
  - Then there is one section per channel, starting `### #<name>`, followed by bullet lines:
    - `- **ID:** <id>`;
    - `- **Type:** Public Channel` or `Private Channel`;
    - an optional `- **Topic:** …` or `- **Purpose:** …`, which may run over several lines;
    - `- **Archived:** No`.
  - A test fixture keeps this format with made-up names and ids.
- **Parsing:** the same discipline as the other reads.
  - An id must look like a channel id (`C` or `G` followed by 8–12 capital letters or digits).
  - A name must look like a Slack channel name (lower-case letters, digits, `-`, `_` and `.`, at most 80 characters).
  - A section that fails is dropped. A page whose "showing N of M" says it was cut, or that parses to nothing, is an
    error, never a short list.
- **Cache:** the list is kept in the store (cursor `slack_channels`, holding JSON `{fetched_at, channels}`).
  - The tick refreshes it at most once a day.
  - The dashboard can ask for a refresh. A queued page command is run by the tick, because the dashboard never calls
    Slack itself.
  - A refresh is a read, so it is allowed while the agent is paused.
- **The leadership channel is always a valid target,** even if the list is stale or empty.

**Posting from the dashboard.**
- The card of an idea or a message has a "Post to" choice:
  - the leadership channel first, as the default;
  - then Haseeb's other channels A–Z, with private ones marked as private;
  - the list's age, and a "Refresh list" link.
- The second click names the channel: "Post to #survive-ai as you?".
- The page command `post` carries the chosen channel id. The runner checks it again when it runs (the list may have
  changed since the page was drawn). A channel it does not accept is refused with a short reason, and nothing is sent.
- Every other part of the post click is unchanged, including the `shown_hash` rule and the double-post rules.

**Posting from the DM.**
- `post` alone posts to the leadership channel, as today.
- `post #survive-ai` posts to that channel. The name is matched, ignoring case, against the cached list. What Slack makes of
  a channel picked by autocomplete (`post <#C093L3Y8UM6|survive-ai>`, `post <#C093L3Y8UM6>`) is read the same way, by its id.
- **The `#` (or Slack's markup) is required.** A bare word after `post` (`post it`, `post now`, `post survive-ai`) is ordinary
  chat and not a command, exactly as before this feature. Why: a reply like "post it" must not become a command.
  - On the Next Todo, any reply cancels the auto-post, and a reply that is a command skips the usual notice ("Auto-post
    cancelled — reply post to post it") because the command's own reply is meant to tell him. A "post it" read as a command
    would cancel the auto-post and tell him nothing.
  - A channel can be called #it or #now, and a chat reply must never post to it with no confirm.
- An unknown name gets one short reply ("#x is not one of your channels (list from <date>) · nothing posted"), and
  nothing is posted.
- `post #x` on a feedback item or the Next Todo gets a reply that this draft posts only to its thread in the
  leadership channel. On the Next Todo the auto-post is still cancelled and Haseeb is still told ("Auto-post cancelled —
  reply post to post it"), as for any reply: that refusal does not say it.
- The success reply names the channel when it is not the leadership channel ("✅ Posted to #survive-ai · <link>"). The
  leadership channel's line is as it always was ("✅ Posted · <link>").

**After the post.**
- The post's channel is recorded, in the cursor `post_channel:<item id>`, next to the existing `post_ts:<item id>`.
- Every reader of a post uses that channel, and falls back to the leadership channel when the cursor is absent (posts
  made before this change):
  - the permalink that `post` returns, and the dashboard's "posted" link;
  - the deck attach, which puts the deck into the post's thread in that channel (the R50 rule is unchanged: a deck is
    attached only if it reached the DM before the post);
  - the thread watch, which watches only insight posts in the leadership channel (a post elsewhere is not watched;
    the docs say so).
- The text posted is the same as today in every channel, footer included ("— drafted by Haseeb's Agent").
- The weekly metrics count an idea posted to any channel as posted, as today.

## Safety (binding)

- Nothing is posted without Haseeb's click or command, and the confirm step names the channel.
- A post goes only to the leadership channel or a channel in his cached list. It never goes to a DM, a group DM, a user
  id, an archived channel or an id typed by hand that is not in the list.
- One post per draft: once a draft is posted to any channel, it is posted. The double-post rules (R16, D8, the claim)
  are unchanged.
- The pause refuses posts, as today.
- Channel names come from Slack and are untrusted: they are validated, shown as text only (strict CSP, no HTML), and
  never reach a model prompt.
- The composer's rules are unchanged: the link guard, and @channel/@here only when Haseeb typed them. The confirm step
  still says when a message would notify people; the channel name is now in that sentence.

## Rulings after the builder check (my calls)

1. **No refresh while paused.** A paused tick makes no Slack call, and the dashboard refuses actions while paused.
   Posting is refused anyway, so a stale list does not matter then. The list refreshes after Resume. (This replaces
   "allowed while paused" above.)
2. **The channel is recorded before the send.** `post_channel:<item id>` is written before the send, together with the
   send-started mark, so an attempt that fails keeps its target.
   - The "may have gone out" lines and the banner name that channel ("check #survive-ai").
   - A retry after a possible post is pinned to the recorded channel. A `post` that names another channel, or a plain
     DM `post` that would mean the leadership channel, is refused with "this draft may already be in #<name>".
   - The R16 rule is unchanged: an error that might have posted locks the draft. Our own refusals before any send (not
     in the list, wrong kind, unknown name) never lock it.
3. **The list's age is only shown.** No age makes a channel invalid.
   - A failed or refused refresh keeps the last good list.
   - The next automatic try waits at least 6 hours.
   - A refresh request from the dashboard makes one try at the next unpaused tick.
4. **The page's shape.** The connector returns JSON whose `result` value is the Markdown page (seen live on 2026-10-22:
   `{"result": "## My Channels (showing 39 of 39 total)\n\n### #…"}`). Completeness has these rules:
   - The wrapper parses, and the page's "showing N of M" has N == M.
   - The sections found, kept plus dropped, number exactly N.
   - No id and no name appears twice.

   Anything else fails the refresh and keeps the old list. A Topic or Purpose is free text that other members can edit,
   and it may span lines. These checks stop it from forging an extra section or swapping a name onto another
   channel's id. The parser never reads Topic or Purpose.
5. **ASCII names only.** A non-ASCII name is dropped from the list, logged by count only, and counted as dropped.

## Non-goals

- No posting of one draft to several channels.
- No change to where feedback, follow-backs, replies in watched threads or the Next Todo go.
- No new channel, no joining a channel, no scheduling.
- The thread watch does not watch other channels.

## Tests (offline)

- **Parsing:** the fixture; a cut page ("showing 50 of 80") is an error; a bad id or name drops its section; DMs never
  appear.
- **The cache:** refreshed at most once a day, the refresh command, and use while paused.
- **Dashboard:**
  - the choice lists the leadership channel first, then A–Z;
  - the confirm names the channel;
  - the command carries the id;
  - the runner refuses an id that is not in the list, and refuses a feedback or Todo post with a channel;
  - names are rendered as text.
- **DM:** `post`, `post #name`, the markup forms, an unknown name, `post #x` on feedback and on the Next Todo (whose
  auto-post is still cancelled with its notice), and a bare word after `post` (chat: no reply, no cancel without the notice).
- **After the post:** `post_channel` is recorded; the permalink, deck attach and dashboard link use it; and an old post
  without the cursor still uses the leadership channel.

## Docs

- docs/architecture.md: the post flow, the channel list and its cache, and the safety rules above.
- The Notion "Who does what" page gets a line on it after it ships.

## Left to the builder

- The cache's exact refresh time.
- The parser's structure.
- The look of the choice on the card.
- Whether the refresh is its own page-command verb or an argument of an existing one.
