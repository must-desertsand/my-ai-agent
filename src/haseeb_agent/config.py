"""Settings: config.toml for behaviour, .env for secrets."""
from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_LITELLM_BASE_URL = "https://litellm.must.codes"


@dataclass(frozen=True)
class Settings:
    channel_id: str
    self_dm_id: str
    haseeb_id: str
    lion_id: str
    tz: ZoneInfo
    huddle_time: time
    insights_ready: time
    workdays: set[int]
    models: dict[str, list[str]]
    litellm_base_url: str
    litellm_api_key: str = field(repr=False)
    insight_min: int = 1
    insight_max: int = 3
    pass_score: int = 85
    max_rewrites: int = 2
    dedupe_days: int = 30
    todo_autopost_s: int = 120
    todo_predraft: time = time(15, 0)
    todo_cutoff: time = time(15, 25)
    todo_stale_min: int = 30
    feedback_include_lion: bool = False
    db_path: str = "data/agent.sqlite"
    # The local dashboard listens on 127.0.0.1 only, on this port.
    dashboard_port: int = 8765
    # Haseeb's self-DM conversation id. Reactions and uploads reject the user id (R13); reads accept self_dm_id.
    self_dm_channel: str = "D08D9HFB8BB"
    # Base of the permalinks the agent builds for its own posts.
    workspace_url: str = "https://mustcompany.slack.com"
    # Mac banners for DM deliveries (notify.py): the banner's click opens slack://channel?team=<id>&id=<self_dm_channel>.
    slack_team_id: str = "T03G4CFLXPH"
    notify_enabled: bool = True
    # Task 14 decks (R44). Codex sits under nvm, which is not on launchd's PATH, so its path is explicit
    # (deck.codex.resolve_codex falls back to `codex` on PATH).
    decks_enabled: bool = True
    codex_path: str = "/Users/cb/.nvm/versions/node/v24.21.0/bin/codex"
    chrome_path: str = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    # Designer decks: Codex designs the whole deck (deck/designer.py), else the template deck. Our Codex runs pass
    # --ignore-user-config, so the models (in fallback order) and the reasoning effort are explicit.
    deck_designer: bool = False
    deck_models: tuple[str, ...] = ("gpt-6.1-sol", "gpt-6-astra")
    deck_effort: str = "low"
    # The Codex version the read restriction was verified on (deck/codexcheck.py): any other version pauses designer
    # decks and covers (the template), with a once-a-day alert to run the smoke checks again.
    codex_version: str = "0.163.0"
    # The site review (site_review/): before a feedback draft, the site or HTML demo the post links to is rendered in
    # headless Chrome (behind a filtering proxy) and a vision model reviews it; at most this many reviews a day.
    site_review_enabled: bool = True
    site_review_daily_cap: int = 10
    # Networks a reviewed page may never reach, refused like private space (CIDRs). The site review already refuses the
    # Mac's own networks and every address routed away from the default route, but it cannot learn the public addresses
    # that only the network behind the default route can reach: the home or office network's own public IPv4 address (the
    # router answers on it from inside), public subnets that network keeps for itself (a provider's, an office's or a
    # campus's own ranges), and the internal public ranges of a full-tunnel VPN that takes the default route. List them here.
    site_review_refused_networks: tuple[str, ...] = ()


def _read_env(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _time(value: str) -> time:
    return time.fromisoformat(value)


def _networks(value) -> tuple[str, ...]:
    """A list of CIDRs, each checked now: a typo in a safety setting must stop the load, not be ignored."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("site_review_refused_networks must be a list of networks such as \"203.0.113.7/32\"")
    out: list[str] = []
    for n in value:
        try:
            out.append(str(ipaddress.ip_network(str(n).strip(), strict=False)))
        except ValueError:
            raise ValueError(f"site_review_refused_networks: {str(n)[:60]!r} is not a network (a CIDR such as "
                             "\"203.0.113.7/32\" or \"198.51.100.0/24\")") from None
    return tuple(out)


def load_settings(path: str | Path = "config.toml", env_path: str | Path = ".env") -> Settings:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    env = _read_env(env_path)

    api_key = env.get("LITELLM_API_KEY", "")
    if not api_key:
        raise ValueError(f"LITELLM_API_KEY is missing or empty in {env_path}")

    d = Settings(
        channel_id=cfg["channel_id"],
        self_dm_id=cfg["self_dm_id"],
        haseeb_id=cfg["haseeb_id"],
        lion_id=cfg["lion_id"],
        tz=ZoneInfo(cfg["tz"]),
        huddle_time=_time(cfg["huddle_time"]),
        insights_ready=_time(cfg["insights_ready"]),
        workdays=set(cfg["workdays"]),
        models={step: list(chain) for step, chain in cfg["models"].items()},
        litellm_base_url=env.get("LITELLM_BASE_URL") or DEFAULT_LITELLM_BASE_URL,
        litellm_api_key=api_key,
    )
    optional = {
        "insight_min": int, "insight_max": int, "pass_score": int, "max_rewrites": int,
        "dedupe_days": int, "todo_autopost_s": int, "todo_stale_min": int,
        "feedback_include_lion": bool, "db_path": str, "dashboard_port": int, "self_dm_channel": str, "workspace_url": str,
        "slack_team_id": str, "notify_enabled": bool,
        "decks_enabled": bool, "codex_path": str, "chrome_path": str,
        "deck_designer": bool, "deck_models": lambda v: tuple(str(m) for m in v), "deck_effort": str, "codex_version": str,
        "site_review_enabled": bool, "site_review_daily_cap": int, "site_review_refused_networks": _networks,
        "todo_predraft": _time, "todo_cutoff": _time,
    }
    overrides = {k: conv(cfg[k]) for k, conv in optional.items() if k in cfg}
    return Settings(**{**d.__dict__, **overrides})
