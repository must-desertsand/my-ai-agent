from datetime import time
from pathlib import Path

import pytest

from haseeb_agent.config import Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent


def write_env(tmp_path, text):
    env = tmp_path / ".env"
    env.write_text(text)
    return env


def test_load_settings_reads_toml_and_env(tmp_path):
    env = write_env(
        tmp_path,
        "# comment\nLITELLM_BASE_URL=https://litellm.example\nLITELLM_API_KEY='sk-test-123'\n",
    )
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)
    assert s.channel_id == "C0C62C128D8"
    assert s.self_dm_id == s.haseeb_id == "U08CZCGJW3E"
    assert s.self_dm_channel == "D08D9HFB8BB"  # reactions need the conversation id, not the user id (R13)
    assert s.lion_id == "U03GRE909MJ"
    assert s.workspace_url == "https://mustcompany.slack.com"
    assert s.pass_score == 85
    assert s.tz.key == "Asia/Kuwait"
    assert s.huddle_time == time(14, 30)
    assert s.insights_ready == time(9, 0)
    assert s.workdays == {0, 1, 2, 3, 4}
    assert (s.insight_min, s.insight_max) == (1, 3)
    assert (s.max_rewrites, s.dedupe_days) == (2, 30)  # ideas: no repeat within 30 days
    assert s.todo_autopost_s == 120
    assert (s.todo_predraft, s.todo_cutoff) == (time(15, 0), time(15, 25))
    assert s.todo_stale_min == 30
    assert s.feedback_include_lion is False
    assert s.db_path == "data/agent.sqlite"
    assert s.litellm_api_key == "sk-test-123"
    assert s.litellm_base_url == "https://litellm.example"
    assert s.models["judge"][0] == "litellm:gemini/gemini-3.1-pro-preview"
    assert s.models["insight_writer"] == ["claude:sonnet", "litellm:kimi-k3"]
    assert set(s.models) == {
        "research", "insight_writer", "judge", "feedback_writer",
        "todo_writer", "summarize", "embed", "slack", "site_review",
    }


def test_base_url_defaults_when_absent(tmp_path):
    env = write_env(tmp_path, "LITELLM_API_KEY=k\n")
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)
    assert s.litellm_base_url == "https://litellm.must.codes"


def test_api_key_not_in_repr(tmp_path):
    env = write_env(tmp_path, "LITELLM_API_KEY=sk-secret-xyz\n")
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)
    assert "sk-secret-xyz" not in repr(s)


def test_missing_api_key_raises(tmp_path):
    env = write_env(tmp_path, "LITELLM_BASE_URL=https://litellm.example\n")
    with pytest.raises(ValueError, match="LITELLM_API_KEY"):
        load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)


def test_missing_env_file_raises_for_api_key(tmp_path):
    with pytest.raises(ValueError, match="LITELLM_API_KEY"):
        load_settings(ROOT / "tests" / "fixtures" / "config.toml", tmp_path / "nope.env")


def test_dedupe_days_default_is_the_30_days_of_ideas():
    """config.toml sets 30 (no idea repeats within 30 days); the default follows it, so a Settings built without the
    key does not fall back to the 7 days of the news insights."""
    assert Settings.__dataclass_fields__["dedupe_days"].default == 30


def test_notification_settings_default_on_and_can_be_overridden(tmp_path):
    env = write_env(tmp_path, "LITELLM_API_KEY=k\n")
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)
    assert s.slack_team_id == "T03G4CFLXPH" and s.notify_enabled is True
    cfg = tmp_path / "config.toml"
    cfg.write_text((ROOT / "tests" / "fixtures" / "config.toml").read_text().replace("notify_enabled = true", "notify_enabled = false"))
    assert load_settings(cfg, env).notify_enabled is False


def test_deck_settings_have_explicit_tool_paths(tmp_path):
    """R44: Codex lives under nvm, which is not on launchd's PATH, so its path is configured explicitly."""
    env = write_env(tmp_path, "LITELLM_API_KEY=k\n")
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)
    assert s.codex_path == "/Users/cb/.nvm/versions/node/v24.21.0/bin/codex"
    assert s.chrome_path == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert Settings.__dataclass_fields__["decks_enabled"].default is True
    base = "\n".join(ln for ln in (ROOT / "tests" / "fixtures" / "config.toml").read_text().splitlines() if not ln.startswith("decks_enabled"))
    cfg = tmp_path / "config.toml"
    for value in (True, False):
        cfg.write_text(f"decks_enabled = {str(value).lower()}\n" + base)
        assert load_settings(cfg, env).decks_enabled is value


def test_designer_deck_settings(tmp_path):
    """Designer decks: our runs pass --ignore-user-config, so the models are explicit, in fallback order."""
    env = write_env(tmp_path, "LITELLM_API_KEY=k\n")
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", env)
    assert s.deck_models == ("gpt-6.1-sol", "gpt-6-astra")
    assert s.deck_effort in ("low", "medium", "high")
    assert isinstance(s.deck_designer, bool)
    assert Settings.__dataclass_fields__["deck_designer"].default is False  # on only where config.toml says so
    cfg = tmp_path / "config.toml"
    text = (ROOT / "tests" / "fixtures" / "config.toml").read_text()
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith(("deck_models", "deck_effort", "deck_designer")))
    cfg.write_text('deck_models = ["gpt-6-astra"]\ndeck_effort = "high"\ndeck_designer = true\n' + text)
    s = load_settings(cfg, env)
    assert (s.deck_models, s.deck_effort, s.deck_designer) == (("gpt-6-astra",), "high", True)


def test_the_site_reviews_refused_networks_are_empty_by_default_and_read_from_the_toml(tmp_path):
    """Networks a reviewed page may never reach, refused like private space (security review I1)."""
    env = write_env(tmp_path, "LITELLM_API_KEY=k\n")
    assert Settings.__dataclass_fields__["site_review_refused_networks"].default == ()
    assert load_settings(ROOT / "tests" / "fixtures" / "config.toml", env).site_review_refused_networks == ()  # the shipped file lists none
    text = (ROOT / "tests" / "fixtures" / "config.toml").read_text()
    assert "site_review_refused_networks = []" in text  # and documents the setting
    stripped = "\n".join(ln for ln in text.splitlines() if not ln.startswith("site_review_refused_networks"))
    cfg = tmp_path / "config.toml"
    cfg.write_text('site_review_refused_networks = ["203.0.113.7/32", " 198.51.100.77/24 ", "10.1.2.3", "2001:db8::/32"]\n'
                   + stripped)
    s = load_settings(cfg, env)
    assert s.site_review_refused_networks == ("203.0.113.7/32", "198.51.100.0/24", "10.1.2.3/32", "2001:db8::/32")
    cfg.write_text(stripped)
    assert load_settings(cfg, env).site_review_refused_networks == ()


@pytest.mark.parametrize("bad", ['["203.0.113.7/33"]', '["not a network"]', '["203.0.113.256/24"]', '[7]', '"203.0.113.7/32"',
                                 '["203.0.113.7/32", "oops"]', '""', '7', 'true'])  # a string, even an empty one, is not a list
def test_a_refused_network_that_is_not_one_stops_the_load_instead_of_being_ignored(tmp_path, bad):
    env = write_env(tmp_path, "LITELLM_API_KEY=k\n")
    text = (ROOT / "tests" / "fixtures" / "config.toml").read_text()
    stripped = "\n".join(ln for ln in text.splitlines() if not ln.startswith("site_review_refused_networks"))
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"site_review_refused_networks = {bad}\n" + stripped)
    with pytest.raises(ValueError, match="site_review_refused_networks"):
        load_settings(cfg, env)


def test_the_template_has_every_setting_of_the_real_config_and_loads(tmp_path):
    """config.example.toml is what a teammate copies to config.toml: it must name every setting the agent's own config sets,
    in the same tables, and load as it is. Only the owner's own ids and paths are placeholders."""
    import tomllib
    real = tomllib.loads((ROOT / "tests" / "fixtures" / "config.toml").read_text())
    example = tomllib.loads((ROOT / "config.example.toml").read_text())
    assert example.keys() == real.keys() and example["models"] == real["models"]
    assert {k for k in real if example[k] != real[k]} == {"self_dm_id", "self_dm_channel", "haseeb_id", "codex_path"}
    s = load_settings(ROOT / "config.example.toml", write_env(tmp_path, "LITELLM_API_KEY=k\n"))
    assert s.haseeb_id == s.self_dm_id == "YOUR_SLACK_USER_ID"
