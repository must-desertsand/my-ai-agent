"""A hard stop on every real outside call while the test suite runs.

The suite sets HASEEB_AGENT_OFFLINE=1 (tests/conftest.py). The variable is inherited, so a process a test spawns
by accident is covered too. On 2026-10-19 a tick test started a real detached `insights` run on a weekday morning,
and it sent real DMs through whatever Claude account was logged in. Every real claude, LiteLLM, Slack-upload and
Codex call, and every real detached spawn, checks this first and refuses.
"""
from __future__ import annotations

import os

ENV = "HASEEB_AGENT_OFFLINE"


class OfflineError(RuntimeError):
    """A real outside call was attempted while HASEEB_AGENT_OFFLINE is set."""


def guard(what: str) -> None:
    if os.environ.get(ENV):
        raise OfflineError(f"{what} blocked: {ENV} is set (tests never make real calls)")
