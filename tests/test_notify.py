"""notify(): one Mac banner per delivery, through terminal-notifier (click opens the DM) or osascript."""
import logging
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from haseeb_agent.config import load_settings
from haseeb_agent.notify import FALLBACK_BIN, TITLE, notify

ROOT = Path(__file__).resolve().parent.parent
TN = "/somewhere/terminal-notifier"


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text("LITELLM_API_KEY=test-key\n")
    return load_settings(ROOT / "tests" / "fixtures" / "config.toml", tmp_path / ".env")


class Runner:
    def __init__(self, *codes, raises=None):
        self.codes, self.raises, self.calls = list(codes), raises, []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.codes.pop(0) if self.codes else 0, stdout=b"", stderr=b"")


def found(name):
    return TN


def missing(name):
    return None


def test_config_carries_the_team_id_and_the_switch(settings):
    assert settings.slack_team_id == "T03G4CFLXPH" and settings.notify_enabled is True


def test_terminal_notifier_gets_title_message_group_and_the_dm_link(settings):
    r = Runner()
    assert notify(settings, "insights", "☀️ 2 insights ready", runner=r, which=found) is True
    cmd, kw = r.calls[0]
    assert cmd == [TN, "-title", "Haseeb's Agent", "-message", "☀️ 2 insights ready",
                   "-group", "haseeb-agent-insights",
                   "-open", "slack://channel?team=T03G4CFLXPH&id=D08D9HFB8BB"]
    assert TITLE == "Haseeb's Agent"
    assert kw["timeout"] == 5


def test_sound_adds_sound_default_only_when_asked(settings):
    quiet, loud = Runner(), Runner()
    notify(settings, "todo", "m", runner=quiet, which=found)
    notify(settings, "todo", "m", sound=True, runner=loud, which=found)
    assert "-sound" not in quiet.calls[0][0]
    cmd = loud.calls[0][0]
    assert cmd[cmd.index("-sound") + 1] == "default"


def test_the_team_and_dm_come_from_settings(settings):
    r = Runner()
    notify(replace(settings, slack_team_id="TX", self_dm_channel="DY"), "k", "m", runner=r, which=found)
    cmd = r.calls[0][0]
    assert cmd[cmd.index("-open") + 1] == "slack://channel?team=TX&id=DY"


def test_the_group_is_per_kind_so_a_newer_banner_replaces_an_older_one(settings):
    r = Runner()
    notify(settings, "todo", "a", runner=r, which=found)
    notify(settings, "feedback", "b", runner=r, which=found)
    groups = [c[c.index("-group") + 1] for c, _ in r.calls]
    assert groups == ["haseeb-agent-todo", "haseeb-agent-feedback"]


def test_the_homebrew_path_is_the_fallback_when_path_has_no_terminal_notifier(settings):
    r = Runner()
    notify(settings, "alert", "m", runner=r, which=missing, is_file=lambda p: p == FALLBACK_BIN)
    assert r.calls[0][0][0] == FALLBACK_BIN == "/opt/homebrew/bin/terminal-notifier"


def test_without_terminal_notifier_osascript_gets_the_message_as_an_argv_item(settings):
    r = Runner()
    nasty = 'He said "hi" \\ end tell -- do shell script "x"'
    assert notify(settings, "alert", nasty, runner=r, which=missing, is_file=lambda p: False) is True
    cmd, kw = r.calls[0]
    assert cmd[0] == "osascript" and cmd[-2:] == [nasty, "Haseeb's Agent"]
    script = [c for i, c in enumerate(cmd) if cmd[i - 1] == "-e"]
    assert "argv" in " ".join(script) and "hi" not in " ".join(script) and "shell" not in " ".join(script)
    assert kw["timeout"] == 5


def test_osascript_sound_is_a_literal_in_the_script(settings):
    r = Runner()
    notify(settings, "alert", "m", sound=True, runner=r, which=missing, is_file=lambda p: False)
    assert 'sound name "default"' in " ".join(r.calls[0][0])


def test_a_failing_terminal_notifier_falls_back_to_osascript(settings):
    r = Runner(1, 0)
    assert notify(settings, "alert", "m", runner=r, which=found) is True
    assert [c[0][0] for c in r.calls] == [TN, "osascript"]


def test_both_failing_returns_false(settings):
    assert notify(settings, "alert", "m", runner=Runner(1, 1), which=found) is False


def test_a_timeout_of_terminal_notifier_does_not_start_osascript(settings):
    """A banner may already be up when terminal-notifier times out: a second one from osascript would double it."""
    r = Runner(raises=subprocess.TimeoutExpired("x", 5))
    assert notify(settings, "alert", "m", runner=r, which=found) is False
    assert [c[0][0] for c in r.calls] == [TN]


@pytest.mark.parametrize("exc", [OSError("gone"), PermissionError("no")])
def test_terminal_notifier_failing_to_start_falls_back_to_osascript(settings, exc):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == TN:
            raise exc
        return subprocess.CompletedProcess(cmd, 0)
    assert notify(settings, "alert", "m", runner=runner, which=found) is True
    assert calls == [TN, "osascript"]


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("x", 5), OSError("gone"), RuntimeError("odd")])
def test_it_never_raises(settings, exc):
    assert notify(settings, "alert", "m", runner=Runner(raises=exc), which=found) is False


def test_a_failure_logs_one_line_without_the_message(settings, caplog):
    secret = "SECRET-DRAFT-TEXT"
    with caplog.at_level(logging.INFO, logger="haseeb_agent"):
        notify(settings, "todo", secret, runner=Runner(raises=OSError(secret)), which=found)
    lines = [r for r in caplog.records if r.name == "haseeb_agent.notify"]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert secret not in caplog.text and "todo" in caplog.text


def test_disabled_runs_nothing(settings):
    r = Runner()
    assert notify(replace(settings, notify_enabled=False), "todo", "m", runner=r, which=found) is False
    assert r.calls == []


@pytest.mark.parametrize("message", ["", "   ", "\n"])
def test_an_empty_message_is_not_sent(settings, message):
    r = Runner()  # terminal-notifier reads stdin when -message is empty
    assert notify(settings, "todo", message, runner=r, which=found) is False
    assert r.calls == []


def test_a_kind_is_reduced_to_a_safe_group_name(settings):
    r = Runner()
    notify(settings, "To Do; rm -rf", "m", runner=r, which=found)
    cmd = r.calls[0][0]
    assert cmd[cmd.index("-group") + 1] == "haseeb-agent-to-do-rm-rf"


def test_the_default_runner_is_the_module_hook_so_tests_cannot_fire_a_banner(settings, banners):
    assert notify(settings, "feedback", "💬 x") is True  # no runner given: conftest's recorder takes it
    assert banners.messages == ["💬 x"] and banners[0].kind == "feedback"
