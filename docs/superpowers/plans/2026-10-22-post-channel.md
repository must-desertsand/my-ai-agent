# Post-Channel Choice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An idea or Haseeb's own composed message can be posted to any channel he is a member of, picked on the
dashboard or written as `post #name` in the DM. The leadership channel stays the default, and nothing else changes
where it posts.

**Architecture:**
- A read-only gateway call lists his channels. The list is cached in the store and refreshed by the tick.
- One small module, `channels.py`, owns the cache, how a target is resolved, and the post's recorded channel.
- The two post handlers take an optional channel id and record it.
- Every reader of a post asks `channels.post_channel()` for its channel.

**Tech Stack:** Python 3.12, uv, pytest, and vanilla JS under a strict CSP. Tests run offline.

**Spec:** docs/superpowers/specs/2026-10-22-post-channel-design.md

## Global Constraints

- **The default:** the leadership channel is `settings.channel_id` (C0C62C128D8). It is always a valid target, and it
  is listed first.
- **The covered kinds:** only item kinds `insight` and `message` take a channel. `feedback` and `todo` refuse one,
  with the reply "this draft posts only to its thread in the leadership channel", and nothing is sent.
- **The connector call:** `slack_list_user_channels` with
  `{"types": "public_channel,private_channel", "exclude_archived": true, "limit": 200}`. It is a read: allowed while
  paused, and the tool is added to the gateway's read-only set.
- **Accepted shapes:**
  - an id matches `^[CG][A-Z0-9]{8,12}$`;
  - a name matches `^[a-z0-9][a-z0-9._-]{0,79}$`.
  - A section that fails either is dropped.
  - A page that says it is cut ("showing N of M" with N < M), or that parses to no channel, raises `SlackError`.
- **Cursors:**
  - `slack_channels`: JSON `{"fetched_at": <iso>, "channels": [{"id", "name", "private"}]}`;
  - `post_channel:<item id>`: the channel id a draft was posted to.
- **Reading a post's channel:** a post without `post_channel:` is in the leadership channel.
- **Safety:**
  - A post goes only to the leadership channel or a channel in the cached list. It never goes to a D…/U… id, a group
    DM or an archived channel.
  - Channel names are text only in the page, and are never put into a model prompt.
  - There is one post per draft.
  - The pause refuses posts.
- **Where work happens:** only in /Users/cb/Projects/Must/my-ai-agent/.worktrees/post-channel (branch
  feat/post-channel). Never touch the live tree /Users/cb/Projects/Must/my-ai-agent or any other worktree, and never run
  the agent's CLI or jobs.
- **Tests:** `uv run pytest -q`.

## Review Focus

1. **A stale list.** A channel Haseeb left after the page was drawn is refused when the command runs, with a short
   reason, and nothing is sent. Covered in Task 2.
2. **Slack's channel markup in the DM.** When he picks a channel by autocomplete, `post #survive-ai` arrives as
   `post <#C093L3Y8UM6|survive-ai>` or `post <#C093L3Y8UM6>`. Both resolve by id. Covered in Task 2.
3. **An old post.** An insight posted before this change has no `post_channel:`. Its link, deck attach and thread
   watch still use the leadership channel. Covered in Task 2.
4. **A deck after a post elsewhere.** `attach` and the deck job upload into the post's thread in that channel. Covered
   in Task 2.
5. **Hostile channel names** in the cached JSON (`<img onerror>`, a very long name) are rendered as text or dropped by
   the shape check. Covered in Tasks 1 and 3.

---

### Task 1: The channel list and its cache

**Files:**
- Modify: `src/haseeb_agent/slack_format.py` (the parser), `src/haseeb_agent/slack_gateway.py` (`list_channels`, the
  read set, the Protocol), `src/haseeb_agent/cli.py` (the tick's daily refresh), `tests/fakes.py` (FakeSlack)
- Create: `src/haseeb_agent/channels.py`, `tests/fixtures/user_channels.txt` (the real format, made-up names and ids),
  `tests/test_channels.py`

**Interfaces (produced):**
- `channels.Channel(id: str, name: str, private: bool)`, a frozen dataclass.
- `slack_format.parse_channels(text: str) -> list[Channel]`, which raises ValueError on a cut or empty page.
- `SlackGateway.list_channels() -> list[Channel]`, with `FakeSlack.channels` to script it.
- `channels.cached(store) -> tuple[datetime | None, list[Channel]]`.
- `channels.refresh(slack, store, now, *, force=False) -> bool`: at most once a day unless forced, and only on a
  successful read.
- `channels.targets(store, settings) -> list[Channel]`: the leadership channel first, then the rest A–Z by name, with
  no duplicates.
- `channels.resolve(store, settings, text: str) -> Channel | None`. It takes an id, `#name`, `name`, `<#ID|name>` or
  `<#ID>`, ignores case, and returns only targets.
- `channels.post_channel(store, settings, item_id) -> str` and `channels.record_post_channel(store, item_id,
  channel_id) -> None`.
- A refresh request that the dashboard can make: a cursor `slack_channels_refresh` that the tick consumes. The builder
  may choose a page-command verb instead; if so, say which in the report.

- [ ] **Step 1: Write the failing tests:**
  - **The parser over the fixture:** the private flag, a multi-line Topic, a bad id or name dropped, a cut page raises,
    an empty page raises.
  - **`list_channels`:** sends the exact arguments; a bare `slack_list_user_channels` is refused by R42a as a
    non-read.
  - **`refresh`:** once a day, with `force`, while paused, and a failed read keeps the old cache.
  - **`targets`:** the order.
  - **`resolve`:** each input form, an unknown name, a D… id, and a name not in the list.
  - **`post_channel`:** the default.
- [ ] **Step 2: Run them and see them fail:** `uv run pytest -q tests/test_channels.py`
- [ ] **Step 3: Implement.** The tick refreshes in its housekeeping when the cache is older than a day, or when the
  refresh cursor is set. The cost is one Haiku read.
- [ ] **Step 4: Run the full suite and see it pass:** `uv run pytest -q`
- [ ] **Step 5: Commit:** `feat(channels): the agent knows Haseeb's channels (read-only list, cached, refreshed daily)`

### Task 2: Posting to a chosen channel (handlers, DM, page commands, readers)

**Files:**
- Modify: `src/haseeb_agent/commands.py` (`post <channel>`, and the Handlers protocol's `post`),
  `src/haseeb_agent/page_commands.py` (`post` with a channel id; the refusal for feedback and todo),
  `src/haseeb_agent/jobs/insights.py` and `jobs/message.py` (`post(..., channel_id=None)`, which records the channel),
  `jobs/feedback.py` and `jobs/todo.py` (refuse a channel), `jobs/decks.py` (attach and upload into the post's
  channel), `jobs/followback.py` (watch only leadership-channel posts), `dashboard/api.py` (the posted link)
- Test: the existing test modules of each file, plus `tests/test_post_channel.py`

**Interfaces:**
- Consumes the Task 1 interfaces.
- `Handlers.post(item, reply_ts=None, channel_id=None) -> str`. It returns the permalink in the channel the post went
  to.
- `Command("post", arg=<the text after post>)`.
- The page command `post` takes `arg` = a channel id, or None for the default.
- The runner calls `channels.resolve` again just before sending.

- [ ] **Step 1: Write the failing tests:**
  - DM `post`, `post #name`, `post name`, `post <#ID|name>`, `post <#ID>`, and an unknown name (one reply, nothing
    sent);
  - `post #x` on feedback and on the todo;
  - a dashboard post with a channel id; an id not in the list (refused, nothing sent); a stale id;
  - `post_channel` is recorded; the permalink and the dashboard link use it;
  - the deck attach and the deck job upload into the post's channel;
  - an old post without the cursor (Review Focus 3);
  - the thread watch skips an insight posted elsewhere;
  - the pause refuses;
  - one post per draft.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite and see it pass.**
- [ ] **Step 5: Commit:** `feat(post): an idea or a message can be posted to any of Haseeb's channels; feedback and the todo stay put`

### Task 3: The dashboard's channel choice, and the docs

**Files:**
- Modify: `src/haseeb_agent/dashboard/api.py` (`GET /api/channels`: the targets and the list's age;
  `POST /api/channels/refresh`, behind the same security middleware as the other writes),
  `src/haseeb_agent/dashboard/static/inbox.js` and CSS (the "Post to" choice on idea and message cards; "Post to #name
  as you?"), `docs/architecture.md`
- Test: `tests/test_dashboard_api.py` and a new `tests/test_dashboard_post_channel_frontend.py` (headless Chrome, like
  the other frontend tests)

**Interfaces:** consumes `channels.targets`, `channels.cached` and the refresh request from Task 1, and the page
command's `arg` from Task 2.

- [ ] **Step 1: Write the failing tests:**
  - the API's shape and order, the age, and the refresh;
  - the choice shows only on idea and message cards, with the leadership channel preselected;
  - the confirm names the chosen channel;
  - the click sends the id;
  - a hostile name is shown as text;
  - private channels are marked.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** In docs/architecture.md cover the post flow, the channel list and its cache, the safety
  rules, and that the thread watch covers only the leadership channel.
- [ ] **Step 4: Run the full suite and see it pass.**
- [ ] **Step 5: Commit:** `feat(dashboard): post an idea or a message to a channel of your choice`
