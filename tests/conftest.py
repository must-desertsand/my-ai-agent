"""No test may raise a real macOS banner. notify() runs its command through `notify._run`; every test gets a
recording stand-in instead, and tests that care read the `banners` fixture. No test may write under the repository's
`data/site-reviews/` either: see `tests/datadir_guard.py`."""
import subprocess
from dataclasses import dataclass

import pytest

from haseeb_agent import notify as notify_mod
from tests import datadir_guard


@dataclass(frozen=True)
class Banner:
    kind: str       # from -group haseeb-agent-<kind>
    message: str
    sound: bool
    argv: tuple


class Banners(list):
    """The banners raised during the test, oldest first."""

    @property
    def messages(self) -> list[str]:
        return [b.message for b in self]

    def of(self, kind: str) -> list[Banner]:
        return [b for b in self if b.kind == kind]


def _parse(cmd: list[str]) -> Banner:
    if "-message" in cmd:  # terminal-notifier
        message = cmd[cmd.index("-message") + 1]
        group = cmd[cmd.index("-group") + 1] if "-group" in cmd else "haseeb-agent-?"
        return Banner(group.removeprefix("haseeb-agent-"), message, "-sound" in cmd, tuple(cmd))
    return Banner("osascript", cmd[-2], any("sound name" in c for c in cmd), tuple(cmd))  # osascript fallback


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test may make a real claude, LiteLLM, Slack-upload or Codex call, or spawn a real run. The variable is
    inherited, so an accidentally spawned child is blocked too (the 2026-10-19 stray-DM incident)."""
    monkeypatch.setenv("HASEEB_AGENT_OFFLINE", "1")


@pytest.fixture(autouse=True)
def tests_leave_the_repositorys_data_folder_alone():
    """A test that writes under the repository's `data/site-reviews/` (see tests/datadir_guard.py) fails. It should give its
    settings a `db_path` inside its tmp_path."""
    watch = datadir_guard.Watch()
    yield
    if changed := watch.finish():
        pytest.fail(watch.message(changed), pytrace=False)


@pytest.fixture(autouse=True)
def banners(monkeypatch) -> Banners:
    seen = Banners()

    def fake_run(cmd, **kwargs):
        seen.append(_parse(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(notify_mod, "_run", fake_run)
    monkeypatch.setattr(notify_mod, "_which", lambda name: "/fake/bin/terminal-notifier")
    return seen
