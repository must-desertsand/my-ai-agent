"""The residual of the site review's leash, written in five places (security re-review N1): the architecture doc, the docstrings of
policy.py and localnet.py, the comment of the setting in config.py and the one in config.example.toml. Each must say the same thing: the
route check compares an address's interface with the default route's, so what only the network behind the DEFAULT ROUTE can
reach passes - the network's own public address, public subnets that network keeps for itself, and the internal public ranges of
a full-tunnel VPN that takes the default route - unless it is listed in `site_review_refused_networks`."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLACES = ["docs/architecture.md", "src/haseeb_agent/site_review/policy.py", "src/haseeb_agent/site_review/localnet.py",
          "src/haseeb_agent/config.py", "config.example.toml"]


def words(path: str) -> str:
    """The file's text with comment marks and line breaks folded into single spaces, lower case."""
    return re.sub(r"\s+", " ", re.sub(r"^\s*#", " ", (ROOT / path).read_text(encoding="utf-8"), flags=re.MULTILINE)).lower()


@pytest.mark.parametrize("path", PLACES)
def test_every_place_that_states_the_residual_names_all_three_cases(path):
    text = words(path)
    assert "site_review_refused_networks" in text
    assert "default route" in text                                    # what the route check compares with
    assert "public ipv4 address" in text                              # (1) the network's own public address
    assert "subnets" in text                                          # (2) public subnets behind the default gateway
    assert "full-tunnel vpn" in text                                  # (3) a VPN that takes the default route itself


@pytest.mark.parametrize("path", PLACES)
def test_the_old_incomplete_claim_is_gone(path):
    text = words(path)
    assert "the one thing this cannot learn" not in text
    assert "it cannot learn the home or office network's public ipv4 address (the router answers on it from inside), nor which" not in text


def test_the_two_vpn_cases_are_told_apart_in_the_doc():
    text = words("docs/architecture.md")
    assert "two `/1` routes" in text and "fails closed" in text       # the tunnel that leaves the default on Wi-Fi: refused whole
    assert "takes the default route itself" in text                   # the tunnel that becomes the default: its ranges pass


def test_the_setting_is_above_the_models_table_and_the_comment_says_why():
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert text.index("site_review_refused_networks = []") < text.index("\n[models]\n")  # (the table, not the comment's words)
    assert "above [models]" in text
