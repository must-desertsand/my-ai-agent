"""The checks before any designer or cover Codex run (review of 466e622): the version pin and the permissions
preflight (`codex debug prompt-input`, no model call). Both fail closed: the template deck or cover."""
import subprocess
from pathlib import Path

import pytest

from haseeb_agent.deck import codexcheck as cc
from haseeb_agent.deck.codex import codex_argv, permission_config
from haseeb_agent.deck.designer import designer_argv

ENV = (Path(__file__).parent / "fixtures" / "codex_prompt_input_deck.txt").read_text()  # real 0.163.0 output
# prompt-input's permissions instructions (real wording; the rest of that text is left out)
RESTRICTED = '"text": "<permissions instructions>\\nFilesystem sandboxing ... Network access is restricted.\\n"'
FIXTURE = RESTRICTED + "\n" + ENV
WORK = Path("/Users/cb/.haseeb-agent/codex-home/probe-tmp")  # the fixture's -C dir


@pytest.fixture(autouse=True)
def fresh():
    cc.reset()
    yield
    cc.reset()


def test_the_real_preflight_output_passes():
    assert cc.check_profile(FIXTURE, WORK, write=True) is None


def test_whole_disk_read_is_refused():
    out = FIXTURE.replace('<entry access=\\"read\\"><special>:minimal</special></entry>',
                          '<entry access=\\"read\\"><special>:root</special></entry>')
    assert cc.check_profile(out, WORK, write=True) is not None
    out = FIXTURE.replace("</file_system>", '<entry access=\\"read\\"><special>:root</special></entry></file_system>')
    assert cc.check_profile(out, WORK, write=True) is not None


@pytest.mark.parametrize("entry", ['<entry access=\\"write\\"><special>:slash_tmp</special></entry>',
                                   '<entry access=\\"write\\"><path>/private/tmp</path></entry>',
                                   '<entry access=\\"read\\"><path>/tmp</path></entry>',
                                   '<entry access=\\"read\\"><path>/Users/cb</path></entry>'])
def test_tmp_or_any_other_readable_path_is_refused(entry):
    out = FIXTURE.replace("</file_system>", entry + "</file_system>")
    assert cc.check_profile(out, WORK, write=True) is not None


def test_a_missing_or_unrestricted_profile_is_refused():
    assert cc.check_profile("<environment_context><cwd>/x</cwd></environment_context>", WORK, write=True)
    assert cc.check_profile(FIXTURE.replace('type=\\"restricted\\"', 'type=\\"unrestricted\\"'), WORK, write=True)
    no_work = FIXTURE.replace(f'<entry access=\\"write\\"><path>{WORK}</path></entry>', "")
    assert cc.check_profile(no_work, WORK, write=True)
    assert cc.check_profile(FIXTURE, WORK, write=False)  # the cover's -C dir must be read-only


class Runner:
    def __init__(self, version="codex-cli 0.163.0", profile=FIXTURE, rc=0):
        self.version, self.profile, self.rc, self.calls = version, profile, rc, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = self.version if argv[1:] == ["--version"] else self.profile
        return subprocess.CompletedProcess(argv, self.rc, out + "\n", "")


def test_the_preflight_runs_prompt_input_with_the_identical_config_from_the_work_dir():
    r = Runner()
    argv = designer_argv("/opt/codex", "gpt-6.1-sol", "low", WORK)
    assert cc.codex_check("/opt/codex", kind="deck", argv=argv, cwd=WORK, write=True, runner=r) is None
    pre, kw = r.calls[1]
    assert pre[:3] == ["/opt/codex", "debug", "prompt-input"] and kw["cwd"] == WORK
    configs = lambda a: [a[i + 1] for i, x in enumerate(a) if x == "-c"]  # noqa: E731
    assert configs(pre) == configs(argv) and set(permission_config("deck", write=True)) <= set(configs(pre))
    assert "--sandbox" not in pre and "exec" not in pre
    # once per process: no more runs
    assert cc.codex_check("/opt/codex", kind="deck", argv=argv, cwd=WORK, write=True, runner=r) is None
    assert len(r.calls) == 2


def test_a_version_other_than_the_pin_is_refused_once():
    r = Runner(version="codex-cli 0.164.0")
    argv = codex_argv("/opt/codex", "AI: x", WORK)
    assert cc.codex_check("/opt/codex", kind="cover", argv=argv, cwd=WORK, write=False, runner=r) == "version"
    assert cc.codex_check("/opt/codex", kind="deck", argv=argv, cwd=WORK, write=True, runner=r) == "version"
    assert len(r.calls) == 1 and cc.version_ok("/opt/codex", runner=r) is False


@pytest.mark.parametrize("runner", [Runner(rc=1), Runner(profile="garbage"), Runner(version="")])
def test_any_failure_refuses(runner):
    argv = designer_argv("/opt/codex", "m", "low", WORK)
    assert cc.codex_check("/opt/codex", kind="deck", argv=argv, cwd=WORK, write=True, runner=runner) is not None


def test_the_profile_denies_tmp():
    fs = next(c for c in permission_config("deck", write=True) if ".filesystem=" in c)
    for p in ('":slash_tmp"="none"', '":tmpdir"="none"', '"/tmp"="none"', '"/private/tmp"="none"',
              '"/var/folders"="none"', '"/private/var/folders"="none"'):
        assert p in fs


def test_two_profiles_are_refused():
    assert cc.check_profile(FIXTURE + "\n" + ENV, WORK, write=True) is not None


def test_network_must_show_as_restricted():
    assert cc.check_profile(FIXTURE.replace("Network access is restricted.", "Network access is enabled."), WORK,
                            write=True) is not None
    assert cc.check_profile(ENV, WORK, write=True) is not None  # no network line at all
