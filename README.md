# Haseeb's Agent

A personal Slack agent for the MUST leadership program (#tf-leadership-q4-2026). It runs on its owner's Mac and drafts, in the owner's own self-DM:

- **Daily ideas:** 1 to 3 researched AI business ideas, each scored by a second model, with an optional PDF deck. Ready by 09:00 Kuwait time, Monday to Friday.
- **Feedback drafts:** a short reply to each substantial teammate post in the channel, after looking at the site or demo the post links to.
- **Next Todo:** the "Next Todo:" reply in the huddle thread, drafted from the huddle notes after the 14:30 huddle.
- **Thread replies:** follow-backs when a teammate answers the owner's feedback, plus replies to comments on the owner's ideas.
- **Message polish:** a cleaner version of a rough message the owner writes.

The owner reviews every draft in the DM and replies `post`, `edit: <instruction>`, `shorter` or `skip`, or uses the same buttons on a local dashboard at 127.0.0.1. Nothing goes to the channel without the owner, with one exception: the Next Todo auto-posts 2 minutes after it reaches the DM unless the owner acts first.

## Start here

- **[docs/onboarding.md](docs/onboarding.md)**: set up your own copy, step by step, and learn how to work on the code.
- [docs/architecture.md](docs/architecture.md): how every part works, with its safety rules.
- [docs/quality-metrics.md](docs/quality-metrics.md): the quality gate and the human-intervention numbers.
- `docs/superpowers/specs/` and `docs/superpowers/plans/`: the design and the build plan behind each feature.

## Quick commands

```bash
uv sync                                   # install
uv run pytest -q                          # the test suite (offline: no test makes a real call)
uv run haseeb-agent slack-check           # read-only: which Slack account would the agent post as?
uv run haseeb-agent smoke                 # sends one test message to your own DM and reads it back
uv run haseeb-agent dashboard open        # single-use login link for the local dashboard
scripts/install-launchd.sh                # go live: the schedule starts (read docs/onboarding.md first)
scripts/uninstall-launchd.sh              # stop the schedule (logs and data are kept)
```
