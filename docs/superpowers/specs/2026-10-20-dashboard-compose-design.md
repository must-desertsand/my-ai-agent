# Dashboard composer: write, polish and send a channel message (2026-10-20)

Requested by Haseeb on 2026-10-20, after posting his architecture link by hand: "Can we also have this send message on
dashboard so we can directly message from there? It should be able to format and write proper english, easy to
understand and short and send it."

## Why

Haseeb writes quick, rough messages for #tf-leadership-q4-2026. He wants the agent to turn them into short, clear,
well-formatted English and post them as him. He wants to do it from the dashboard, and, as for every other action,
from the DM too.

## Goals

**1. A composer on the dashboard (Inbox page, top).**
- A textarea for the rough message, up to 2,000 characters.
- A button: **Polish**.
- The polished draft appears as a normal approvable item of a new kind, `message`, with the existing verbs:
  - Post;
  - Edit (an instruction);
  - Shorter;
  - Skip.

  Post sends it to the channel as Haseeb.

**2. The same from the DM.** Haseeb sends the agent `say: <rough message>` as a top-level DM message. The tick drafts
the same `message` item and answers in the DM like any other draft. `post`, `edit: …`, `shorter` and `skip` work in
its thread as usual.

**3. The polish.**
- The rewrite keeps Haseeb's meaning, facts, links and any names he wrote, and adds nothing he didn't say.
- The English is short, plain and easy to understand. It uses Slack formatting (short paragraphs, and a list only when
  the content is a list).
- The draft carries a one-line note of what changed. The note appears in the DM and on the page, never in the post.
- `@channel` or `@here` survive only when Haseeb typed them himself; they are sent as a real `<!channel>`/`<!here>`.
  Every other mass mention is defused as now.
- It goes through the same outgoing cleaner as every draft: no fake footers, links shown with their destination, only
  slack.com permalinks as links. Haseeb's own URLs stay as plain URLs.

## Non-goals

- No other channel, and no DMs to other people.
- No reply-in-a-thread composer. "Draft feedback" covers replies to posts.
- No sending without a Post click (or a `post` reply) after the polished text was shown.
- No auto-post. A `message` item never auto-posts.
- No "drafted by Haseeb's Agent" footer: it is his own message, only polished.

## Constraints (binding)

- **The dashboard makes no model call.** "Polish" queues a page command (new verb `compose`, carrying the raw text),
  and the tick drafts the item, exactly like the other page actions. D7b: the page's Post equals the DM's `post`. The
  double-post rules (D8, R16) apply unchanged.
- **The model is the feedback writer's chain** (`models["feedback"]`, through LiteLLM), with a dedicated prompt:
  - the raw text is fenced as untrusted data;
  - "rewrite, don't add";
  - the output must be plain text under 1,200 characters;
  - when the model chain fails, the draft is Haseeb's own text, cleaned, with the note "not polished (model
    unavailable)".
- **Size limits.** The raw text is capped at 2,000 characters, both on the page (with a counter) and in the API (400 if
  exceeded). Empty or whitespace-only text is refused.
- **The item and its flow.**
  - The item stores the raw text and the polished text.
  - `edit: …` and `shorter` re-polish from the current draft with the instruction, the same way as the other kinds.
  - Every `edit` instruction is saved as a preference only for the `message` kind.
- **Paused.** While the agent is paused (`agent_paused`), compose is refused like any other action (409 `paused`).
- **Tests.** API, command, tick and frontend render tests in the existing styles. Hostile input renders as text. A
  typed `@channel` survives and is sent as `<!channel>`; a model-added one does not.

## Left to the builder

- the exact prompt wording;
- the composer's layout and its empty, waiting and error states;
- how `say:` is detected among other top-level DM messages, without catching ordinary chat.
