"""The suite can never reach the outside world (2026-10-19: a tick test started a real insights run)."""
import subprocess

import httpx
import pytest

from haseeb_agent import cli
from haseeb_agent.claude_cli import run_claude
from haseeb_agent.deck.proc import run_group
from haseeb_agent.offline import OfflineError


def test_offline_is_set_for_every_test():
    import os
    assert os.environ.get("HASEEB_AGENT_OFFLINE") == "1"


def test_real_claude_run_is_blocked(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: called.append(a))
    with pytest.raises(OfflineError):
        run_claude("hi", model="haiku", allowed_tools=["mcp__claude_ai_Slack__slack_send_message"])
    assert called == []


def test_injected_runner_still_works():
    res = run_claude("hi", model="haiku", allowed_tools=["WebSearch"],
                     runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    assert res is not None


@pytest.mark.parametrize("spawn", [cli.spawn_insights, cli.spawn_decks])
def test_default_spawns_are_blocked(spawn, monkeypatch):
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(OfflineError):
        spawn()
    assert ran == []


def test_codex_through_run_group_is_blocked():
    with pytest.raises(OfflineError):
        run_group(["/usr/local/bin/codex", "exec", "x"], timeout=5)


def test_litellm_real_client_is_blocked():
    from haseeb_agent.litellm import LLM
    from tests.test_litellm import make_settings
    llm = LLM(make_settings(), http=httpx.Client())
    with pytest.raises(OfflineError):
        llm._post("/v1/chat/completions", {})


def test_tick_catch_up_cannot_start_a_real_run(monkeypatch):
    """The exact incident path: the tick's catch-up calls spawn_insights() with no argv."""
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(OfflineError):
        cli.spawn_insights()
    assert ran == []


# -- the final review (I1): a guard that compared against the module attribute at call time was bypassed by a test
# that had faked that attribute: the default argument, bound at import, then ran unguarded. Each test below fakes
# the global first and then calls with the defaults. If a guard ever regressed, what would run is harmless: the
# claude binary is pinned to /usr/bin/false, HTTP goes to a proxy on a closed local port, codex is /usr/bin/false.

DEAD_PROXY = "http://127.0.0.1:9"  # the discard port: nothing listens, a connection is refused at once


@pytest.fixture
def no_network(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, DEAD_PROXY)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def test_run_claude_default_runner_is_guarded_after_subprocess_run_is_faked(monkeypatch, tmp_path):
    from haseeb_agent import claude_cli
    monkeypatch.setattr(claude_cli, "_claude_bin", lambda: "/usr/bin/false")
    monkeypatch.setattr(claude_cli, "ensure_agent_home", lambda *a, **k: None, raising=False)
    faked = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: faked.append(a))  # "nothing real runs", a test thinks
    with pytest.raises(OfflineError):
        claude_cli.run_claude("hi", model="haiku", allowed_tools=["WebSearch"], cwd=tmp_path)
    assert faked == []


def test_slack_upload_default_http_post_is_guarded_after_httpx_post_is_faked(monkeypatch, tmp_path, no_network):
    from haseeb_agent.config import load_settings
    from haseeb_agent.slack_gateway import ClaudeConnectorSlack
    from tests.test_slack_gateway import FakeRun, tool, url_result
    (tmp_path / ".env").write_text("LITELLM_API_KEY=k\n")
    settings = load_settings(cli.Path(__file__).resolve().parent / "fixtures" / "config.toml", tmp_path / ".env")
    pdf = tmp_path / "deck.pdf"
    pdf.write_bytes(b"%PDF-1.4 bytes")
    posted = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posted.append(a))
    run = FakeRun(tool("slack_get_file_upload_url", url_result()), tool("slack_complete_file_upload", "ok"))
    gw = ClaudeConnectorSlack(settings, run=run)  # the default http_post: the real httpx.post
    with pytest.raises(OfflineError):
        gw.upload_pdf("D1", pdf, "T", None, None)
    assert posted == [] and len(run.calls) == 1  # refused before the bytes, and nothing was finalised


def test_litellm_default_client_is_guarded_after_the_transport_class_is_faked(monkeypatch, no_network):
    from haseeb_agent.litellm import LLM
    from tests.test_litellm import make_settings

    class NotTheRealTransport:
        pass

    monkeypatch.setattr(httpx, "HTTPTransport", NotTheRealTransport)
    llm = LLM(make_settings())  # the default client, built with httpx's own (real) transport
    with pytest.raises(OfflineError):
        llm._post("/v1/chat/completions", {})


def test_litellm_any_client_but_a_mock_transport_is_guarded(no_network):
    from haseeb_agent.litellm import LLM
    from tests.test_litellm import make_settings

    class NoTransport:  # a client whose transport cannot be told: treated as real (fail closed)
        def post(self, *a, **k):
            raise AssertionError("must not be reached")

    with pytest.raises(OfflineError):
        LLM(make_settings(), http=NoTransport())._post("/v1/chat/completions", {})
    mocked = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert LLM(make_settings(), http=mocked)._post("/v1/x", {}).status_code == 200  # a test double still works


@pytest.mark.parametrize("call", ["version", "preflight"])
def test_codex_checks_default_runner_is_guarded_after_subprocess_run_is_faked(monkeypatch, tmp_path, call):
    from haseeb_agent.deck import codexcheck
    codexcheck.reset()
    faked = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: faked.append(a))
    with pytest.raises(OfflineError):
        if call == "version":
            codexcheck.version_ok("/usr/bin/false")
        else:
            codexcheck.codex_check("/usr/bin/false", kind="deck", argv=["/usr/bin/false", "x"], cwd=tmp_path,
                                   write=True)
    assert faked == []
    codexcheck.reset()


def test_codex_checks_with_an_injected_runner_still_work():
    from haseeb_agent.deck import codexcheck
    codexcheck.reset()
    r = lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.163.0", stderr="")  # noqa: E731
    assert codexcheck.version_ok("/opt/codex", runner=r) is True
    codexcheck.reset()
