"""Task 1 of docs/superpowers/plans/2026-10-22-post-channel.md: the agent knows Haseeb's channels.

A read-only gateway call lists the channels he is a member of, `slack_format.parse_channels` reads the connector's page
strictly (a cut page, a forged section or a repeated name or id fails the read and keeps the old list), `channels.py` keeps
the list in the store and says which channels a post may go to, and the tick refreshes it once a day, or when the
dashboard asked, and never while the agent is paused or the huddle poll must not be blinded.

The fixture is the connector's reply in the shape seen live on 2026-10-22 (tests/fixtures/user_channels.txt): a JSON object
whose `result` is a Markdown page. Names and ids are made up.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from haseeb_agent import channels, claude_cli, cli, controls, slack_gateway
from haseeb_agent.alert import SlackHealth
from haseeb_agent.channels import Channel
from haseeb_agent.claude_cli import ClaudeResult, run_claude
from haseeb_agent.config import load_settings
from haseeb_agent.jobs.todo import item_id as todo_item_id
from haseeb_agent.offline import OfflineError
from haseeb_agent.slack_format import parse_channels
from haseeb_agent.slack_gateway import ClaudeConnectorSlack, SlackError, SlackTimeout
from haseeb_agent.store import UNCOUNTED_JOBS, Store
from tests.fakes import FakeSlack
from tests.test_slack_gateway import PREFIX, SEND_OK, FakeRun, args_of, tool

ROOT = Path(__file__).resolve().parent.parent
RAW = (ROOT / "tests" / "fixtures" / "user_channels.txt").read_text(encoding="utf-8")
PAGE = json.loads(RAW)["result"]
KW = ZoneInfo("Asia/Kuwait")
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=KW)  # a Thursday, outside the huddle window
DAY = timedelta(days=1)
LEADERSHIP = "C0C62C128D8"

# What the fixture holds: eight sections. The last one has a Korean name, which is dropped (ASCII names only), and the
# other seven are kept, in page order.
KEPT = [
    ("chat-random", "C01AAAAAAAA", False),
    ("announcements-demo", "C01BBBBBBBB", False),
    ("team-private-dev", "C01CCCCCCCC", True),
    ("survive-demo", "C01DDDDDDDD", False),
    ("tf-leadership-demo", LEADERSHIP, False),
    ("weekly-digest", "C01FFFFFFFF", False),
    ("ops_eu.alerts", "G01EEEEEEEE", True),
]
LIST = [Channel(cid, name, private) for name, cid, private in KEPT]
SURVIVE = Channel("C093L3Y8UM6", "survive-ai", False)


def wrap(page: str) -> str:
    """The connector's reply: its JSON wrapper around the Markdown page."""
    return json.dumps({"result": page})


def section(name, cid, kind="Public Channel", free=(), last="- **Archived:** No") -> str:
    lines = [f"### #{name}", f"- **ID:** {cid}", f"- **Type:** {kind}", *free]
    if last:
        lines.append(last)
    return "\n".join(lines) + "\n"


def page_of(*sections: str, shown: int | None = None, total: int | None = None) -> str:
    """A reply that says `shown` of `total` (the number of sections, by default) and holds `sections`."""
    n = len(sections)
    head = f"## My Channels (showing {n if shown is None else shown} of {n if total is None else total} total)\n\n"
    return wrap(head + "\n".join(sections))


def swap(page: str, old: str, new: str) -> str:
    assert page.count(old) == 1, old
    return page.replace(old, new)


def rows(got) -> list[tuple[str, str, bool]]:
    return [(c.name, c.id, c.private) for c in got]


# --------------------------------------------------------------------------------------------------------------------
# The parser over the real shape
# --------------------------------------------------------------------------------------------------------------------

def test_the_live_shape_parses_in_page_order():
    got = parse_channels(RAW)
    assert rows(got) == KEPT
    assert all(isinstance(c, Channel) for c in got)


def test_a_channel_is_a_frozen_value():
    c = Channel("C01AAAAAAAA", "chat-random", False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.name = "other"  # type: ignore[misc]
    assert c == Channel("C01AAAAAAAA", "chat-random", False) and len({c, Channel("C01AAAAAAAA", "chat-random", False)}) == 1
    assert channels.Channel is Channel


def test_private_channels_are_flagged_and_the_flag_is_the_type_line():
    got = {c.name: c for c in parse_channels(RAW)}
    assert got["team-private-dev"].private is True and got["ops_eu.alerts"].private is True  # a G... id too
    assert got["survive-demo"].private is False and got["tf-leadership-demo"].private is False


def test_a_multi_line_topic_or_purpose_is_not_read():
    """The connector prints a Topic or a Purpose over as many lines as it has. #weekly-digest's Topic holds a list, and
    #announcements-demo's Purpose is numbered: none of their lines is a channel or changes the channel's id."""
    got = {c.name: c for c in parse_channels(RAW)}
    assert got["weekly-digest"] == Channel("C01FFFFFFFF", "weekly-digest", False)
    assert got["announcements-demo"] == Channel("C01BBBBBBBB", "announcements-demo", False)
    assert len(got) == 7


def test_the_reply_may_carry_other_keys_beside_the_result():
    assert rows(parse_channels(json.dumps({"result": PAGE, "pagination_info": ""}))) == KEPT


@pytest.mark.parametrize("old,new", [
    ("C01DDDDDDDD", "D01DDDDDDDD"),            # a DM id is never a channel
    ("C01DDDDDDDD", "U01DDDDDDDD"),            # nor a user
    ("C01DDDDDDDD", "c01dddddddd"),            # lower case is not an id
    ("C01DDDDDDDD", "C0DDDDDD"),               # 7 characters after the letter
    ("C01DDDDDDDD", "C" + "D" * 13),           # 13
    ("C01DDDDDDDD", "C01DDDD-DDDD"),
    ("C01DDDDDDDD", "C01DDDDDDDD extra"),
    ("C01DDDDDDDD", "C01DDDDDDD\u00c9"),       # a non-ASCII capital
])
def test_a_bad_id_drops_that_section_and_only_that_one(old, new):
    got = parse_channels(swap(RAW, old, new))
    assert rows(got) == [r for r in KEPT if r[0] != "survive-demo"]


@pytest.mark.parametrize("old,new", [
    ("### #survive-demo", "### #Survive-Demo"),           # upper case
    ("### #survive-demo", "### #survive demo"),
    ("### #survive-demo", "### #-survive"),               # must start with a letter or a digit
    ("### #survive-demo", "### #" + "a" * 81),            # 80 at most
    ("### #survive-demo", "### #survive-demo!"),
    ("### #survive-demo", "### #survive<b>"),
    ("### #survive-demo", "### #survive-\u00e9"),         # not ASCII
    ("### #survive-demo", "### #surv\u0456ve-demo"),      # a Cyrillic i, a look-alike
    ("### #survive-demo", "### #"),                       # no name
    ("### #survive-demo", "### # survive-demo"),
])
def test_a_bad_name_drops_that_section_and_only_that_one(old, new):
    got = parse_channels(swap(RAW, old, new))
    assert rows(got) == [r for r in KEPT if r[0] != "survive-demo"]


def test_the_edges_of_the_name_and_id_rules_are_kept():
    page = page_of(section("a" * 80, "C" + "1" * 8), section("0" * 80, "G" + "Z" * 12),
                   section("x", "C" + "1" * 7), section("y", "C" + "1" * 13))
    got = parse_channels(page)
    assert [(c.name, c.id) for c in got] == [("a" * 80, "C" + "1" * 8), ("0" * 80, "G" + "Z" * 12)]


def test_non_ascii_names_are_dropped_and_counted_by_count_only(caplog):
    caplog.set_level(logging.INFO)
    got = parse_channels(RAW)
    assert all(c.name.isascii() for c in got) and len(got) == 7  # the Korean #...-demo is the eighth section
    assert "1 of 8 section(s) dropped" in caplog.text
    assert "survive" not in caplog.text and "weekly" not in caplog.text and "\ud300" not in caplog.text


def test_a_page_with_nothing_dropped_logs_nothing(caplog):
    caplog.set_level(logging.INFO)
    parse_channels(page_of(section("alpha", "C01AAAAAAAA")))
    assert "dropped" not in caplog.text


@pytest.mark.parametrize("kind", ["Direct Message", "Group Message", "public channel", "Public Channel (shared)", ""])
def test_a_type_other_than_public_or_private_drops_the_section(kind):
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("beta", "C01BBBBBBBB", kind=kind)))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False)]


@pytest.mark.parametrize("last", ["- **Archived:** Yes", "- **Archived:** yes", "- **Archived:** True", "- **Archived:** "])
def test_an_archived_channel_is_dropped_even_though_it_was_not_asked_for(last):
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("beta", "C01BBBBBBBB", last=last)))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False)]


def test_a_purpose_cannot_say_a_channel_is_archived_or_private_or_has_another_id():
    """Topic and Purpose are text other members edit. Nothing in them is read: the id and the type are the two lines
    right after the heading, and the Archived line is the last line of the section."""
    forged = ["- **Purpose:** keep out", "- **ID:** C0FORGED0001", "- **Type:** Private Channel", "- **Archived:** Yes",
              "### not a heading", "## My Channels (showing 1 of 1 total)", "more text"]
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA", free=forged), section("beta", "C01BBBBBBBB")))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False), ("beta", "C01BBBBBBBB", False)]


def test_a_section_with_no_archived_line_is_kept():
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA", last=None)))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False)]


# The Archived check reads the section's own `- **Archived:** ...` line (the last one in the section), not whatever line
# happens to end it (Task 1's review, minor 2). A line after it, or connector text after the last section, is not it.

def test_connector_text_after_the_last_section_cannot_hide_that_a_channel_is_archived():
    head = "## My Channels (showing 2 of 2 total)\n\n"
    alpha, beta = section("alpha", "C01AAAAAAAA"), section("beta", "C01BBBBBBBB", last="- **Archived:** Yes")
    for note in ("Use the cursor to see more.", "(2 channels)\n\nTip: ask me for a channel's members."):
        got = parse_channels(wrap(head + alpha + "\n" + beta + "\n" + note + "\n"))
        assert rows(got) == [("alpha", "C01AAAAAAAA", False)], note


def test_connector_text_after_the_last_section_does_not_drop_a_channel_that_is_not_archived():
    head = "## My Channels (showing 2 of 2 total)\n\n"
    got = parse_channels(wrap(head + section("alpha", "C01AAAAAAAA") + "\n" + section("beta", "C01BBBBBBBB") + "\nA note.\n"))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False), ("beta", "C01BBBBBBBB", False)]


@pytest.mark.parametrize("after", ["- **Members:** 12", "- **Members:** 12\n\n", "- **Purpose:** moved here\nand a second line"])
def test_a_line_after_the_archived_line_does_not_hide_it(after):
    archived = section("beta", "C01BBBBBBBB", last="- **Archived:** Yes\n" + after)
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA"), archived))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False)]


def test_a_line_after_the_archived_line_does_not_drop_a_channel_that_says_no():
    open_ = section("beta", "C01BBBBBBBB", last="- **Archived:** No\n- **Members:** 12")
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA"), open_))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False), ("beta", "C01BBBBBBBB", False)]


def test_the_last_archived_line_of_a_section_is_its_own_and_an_earlier_one_in_a_purpose_is_not():
    """Free text comes before the real line, so a forged Archived line in a Purpose is never the last one: it cannot say No for an
    archived channel, and it cannot say Yes for an open one."""
    says_no = ["- **Purpose:** keep this one", "- **Archived:** No"]
    says_yes = ["- **Purpose:** keep out", "- **Archived:** Yes"]
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA", free=says_yes),  # the real line says No
                                 section("beta", "C01BBBBBBBB", free=says_no, last="- **Archived:** Yes")))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False)]


def test_a_unicode_line_separator_in_a_purpose_does_not_start_a_section():
    """Lines are the connector's own `\\n`. A U+2028 or U+0085 inside a Purpose is part of it, whatever it is followed by."""
    purpose = "- **Purpose:** hello\u2028### #phantom\u2028- **ID:** C0PHANTOM01\u0085### #ghost\u000b### #spook"
    got = parse_channels(page_of(section("alpha", "C01AAAAAAAA", free=[purpose])))
    assert rows(got) == [("alpha", "C01AAAAAAAA", False)]


def test_a_line_break_with_a_carriage_return_is_the_same_line():
    got = parse_channels(wrap(PAGE.replace("\n", "\r\n")))
    assert rows(got) == KEPT


# -- a page that is cut, empty or not the page ------------------------------------------------------------------------

def test_a_page_that_says_it_is_cut_raises():
    with pytest.raises(ValueError, match="truncated.*8 of 80"):
        parse_channels(swap(RAW, "showing 8 of 8 total", "showing 8 of 80 total"))
    with pytest.raises(ValueError, match="truncated.*50 of 80"):
        parse_channels(page_of(*[section(f"c{i}", f"C0{i:09d}") for i in range(50)], shown=50, total=80))


def test_fewer_sections_than_the_page_announces_raises():
    with pytest.raises(ValueError, match="truncated"):
        parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("beta", "C01BBBBBBBB"), shown=3, total=3))


def test_more_sections_than_the_page_announces_raises():
    with pytest.raises(ValueError, match="2 sections"):
        parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("beta", "C01BBBBBBBB"), shown=1, total=1))


@pytest.mark.parametrize("text", [
    page_of(),                                                      # showing 0 of 0
    wrap(""),
    wrap("## My Channels (showing 3 of 3 total)\n\n"),              # announces three, lists none
    page_of(section("Beta", "C01BBBBBBBB"), section("gamma", "D01GGGGGGGG")),   # every section is bad
    wrap("## My Channels (showing 0 of 0 total)\n\nYou are not in any channel.\n"),
])
def test_a_page_with_no_channel_in_it_raises(text):
    with pytest.raises(ValueError):
        parse_channels(text)


@pytest.mark.parametrize("text", [
    PAGE,                                                           # the Markdown page without the connector's wrapper
    RAW[:400],                                                      # a reply cut in the middle
    RAW + "\n[... 9000 characters truncated ...]",
    "Output too large (61.2KB). Full output saved to: /Users/cb/.claude/projects/x/tool-results/abc.txt",
    "",
    "null", "[]", "5", '"## My Channels (showing 1 of 1 total)"',
    json.dumps({"messages": PAGE}),                                  # not the `result` of this tool
    json.dumps({"result": 5}),
    json.dumps({"result": None}),
    json.dumps({"result": "Here are your channels:\n" + PAGE}),     # the header is not where it must be
    wrap("## My Channels (showing 8 total)\n\n" + PAGE.split("\n\n", 1)[1]),
    wrap("## My Channels (showing x of y total)\n\n" + PAGE.split("\n\n", 1)[1]),
])
def test_a_reply_that_is_not_the_connectors_complete_page_raises(text):
    with pytest.raises(ValueError):
        parse_channels(text)


def test_the_parser_takes_none_for_a_reply():
    with pytest.raises(ValueError):
        parse_channels(None)  # type: ignore[arg-type]


# -- a section forged in free text, a name or an id swapped ----------------------------------------------------------

FORGED = "### #fake-channel\n- **ID:** C0FAKE00001\n- **Type:** Public Channel\n- **Archived:** No"


@pytest.mark.parametrize("where", [
    "3. Internal Tech Seminar(Optional)",                # the last line of a multi-line Purpose
    "- **Topic:** improve teamsync company value and goal",
    "- **Purpose:** Share anything. How are you doing in yours?",
])
def test_a_section_forged_inside_a_purpose_or_topic_fails_the_whole_page(where):
    forged = swap(PAGE, where, where + "\n" + FORGED)
    with pytest.raises(ValueError, match="9 sections"):
        parse_channels(wrap(forged))


def test_a_bare_forged_heading_is_a_section_too():
    forged = swap(PAGE, "3. Internal Tech Seminar(Optional)", "3. Internal Tech Seminar(Optional)\n### #fake-channel")
    with pytest.raises(ValueError):
        parse_channels(wrap(forged))


def test_a_forged_section_at_the_end_of_the_last_purpose_fails_the_page():
    head, archived, tail = PAGE.rstrip("\n").rpartition("- **Archived:** No")  # the last section's own Archived line
    forged = head + "- **Purpose:** x\n" + FORGED + "\n" + archived + tail
    with pytest.raises(ValueError, match="9 sections"):
        parse_channels(wrap(forged))


def test_a_name_on_two_ids_fails_the_page():
    """The count is right (two of two) but #alpha has two ids: a name swapped onto another channel's id."""
    with pytest.raises(ValueError, match="twice"):
        parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("alpha", "C01BBBBBBBB")))


def test_an_id_under_two_names_fails_the_page():
    with pytest.raises(ValueError, match="twice"):
        parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("beta", "C01AAAAAAAA")))


def test_the_leadership_channels_id_under_a_second_name_fails_the_page():
    with pytest.raises(ValueError, match="twice"):
        parse_channels(page_of(section("tf-leadership-demo", LEADERSHIP), section("evil", LEADERSHIP)))


def test_a_repeat_is_found_whatever_the_case_and_among_dropped_sections_too():
    # #Alpha has a bad shape (dropped), but it still repeats #alpha
    with pytest.raises(ValueError, match="twice"):
        parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("Alpha", "C01BBBBBBBB")))
    # a dropped section with a bad id that repeats a kept name
    with pytest.raises(ValueError, match="twice"):
        parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("alpha", "D01BBBBBBBB")))
    # a dropped section whose id repeats a kept id
    with pytest.raises(ValueError, match="twice"):
        parse_channels(page_of(section("alpha", "C01AAAAAAAA"), section("Beta", "C01AAAAAAAA")))


def test_an_error_never_repeats_a_channel_name_or_id():
    forged = swap(PAGE, "3. Internal Tech Seminar(Optional)", "3. Internal Tech Seminar(Optional)\n" + FORGED)
    for text in (wrap(forged), page_of(section("alpha", "C01AAAAAAAA"), section("alpha", "C01BBBBBBBB"))):
        with pytest.raises(ValueError) as e:
            parse_channels(text)
        assert "alpha" not in str(e.value) and "fake" not in str(e.value) and "C0FAKE" not in str(e.value)


# --------------------------------------------------------------------------------------------------------------------
# list_channels, the gateway's read
# --------------------------------------------------------------------------------------------------------------------

LIST_TOOL = "slack_list_user_channels"
LIST_ARGS = {"types": "public_channel,private_channel", "exclude_archived": True, "limit": 200}


@pytest.fixture
def settings(tmp_path):
    (tmp_path / ".env").write_text("LITELLM_API_KEY=k\n")
    s = load_settings(ROOT / "tests" / "fixtures" / "config.toml", tmp_path / ".env")
    return replace(s, db_path=str(tmp_path / "data" / "agent.sqlite"))


@pytest.fixture
def store(settings):
    s = Store(settings.db_path)
    yield s
    s.close()


def gateway(settings, *results, **kwargs):
    run = FakeRun(*results)
    return ClaudeConnectorSlack(settings, run=run, sleep=lambda s: None, **kwargs), run


def test_list_channels_sends_exactly_these_arguments_and_returns_the_parsed_list(settings):
    gw, run = gateway(settings, tool(LIST_TOOL, RAW))
    assert rows(gw.list_channels()) == KEPT
    assert args_of(run.calls[0]) == LIST_ARGS == {"types": "public_channel,private_channel", "exclude_archived": True,
                                                  "limit": 200}
    kwargs = run.calls[0][1]
    assert kwargs["allowed_tools"] == [PREFIX + LIST_TOOL]            # the one connector tool, no other
    assert PREFIX + LIST_TOOL not in kwargs["disallowed_tools"]
    assert PREFIX + "slack_send_message" in kwargs["disallowed_tools"]  # and every other Slack tool is shut
    assert PREFIX + "slack_list_channel_members" in kwargs["disallowed_tools"]


@pytest.mark.parametrize("passed", [
    {},                                                                                   # a bare call
    {**LIST_ARGS, "types": "public_channel,private_channel,im,mpim"},                      # DMs and group DMs too
    {k: v for k, v in LIST_ARGS.items() if k != "exclude_archived"},                       # archived channels too
    {**LIST_ARGS, "exclude_archived": False},
    {**LIST_ARGS, "limit": 1000},
    {**LIST_ARGS, "cursor": "abc"},
], ids=["bare", "with DMs", "archived listed", "archived asked", "other limit", "extra cursor"])
def test_a_call_that_is_not_exactly_the_requested_one_is_refused(settings, passed):
    """R15: the model passes the arguments through and nothing else. A bare slack_list_user_channels would list every kind
    of conversation, DMs and archived channels among them, so it is refused as a tool-call mismatch."""
    gw, _ = gateway(settings, tool(LIST_TOOL, RAW, inp=passed))
    with pytest.raises(SlackError, match="tool-call mismatch"):
        gw.list_channels()


def test_a_second_list_call_is_refused_even_if_both_match(settings):
    twice = ClaudeResult("DONE", [claude_cli.ToolResult(PREFIX + LIST_TOOL, RAW, False, LIST_ARGS),
                                  claude_cli.ToolResult(PREFIX + LIST_TOOL, RAW, False, LIST_ARGS)], 0.0, True, None)
    gw, _ = gateway(settings, twice)
    with pytest.raises(SlackError, match="tool-call mismatch"):
        gw.list_channels()


def test_the_list_tool_is_a_read_tool():
    """A hung read aborts in 60 s, and its timeout is never taken for a write that may have landed. Both lists name it."""
    assert LIST_TOOL in slack_gateway.READ_TOOLS and LIST_TOOL in slack_gateway.SLACK_TOOLS
    assert slack_gateway.READ_TOOLS == claude_cli.MCP_READ_TOOLS


def test_the_list_run_gets_the_60_s_mcp_tool_timeout(tmp_path):
    seen = []

    def runner(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, '{"type":"result","result":"ok"}\n', "")

    run_claude("p", model="m", allowed_tools=[PREFIX + LIST_TOOL], runner=runner, cwd=tmp_path,
               user_settings=tmp_path / "none.json")
    assert seen[0]["MCP_TOOL_TIMEOUT"] == "60000" and "PATH" in seen[0]


@pytest.mark.parametrize("error", ["MCP error -32001: Request timed out", "Tool call aborted: timeout"])
def test_a_list_that_times_out_is_a_read_error_not_a_write_that_may_have_landed(settings, error):
    gw, _ = gateway(settings, tool(LIST_TOOL, error, is_error=True))
    with pytest.raises(SlackError) as e:
        gw.list_channels()
    assert not isinstance(e.value, SlackTimeout) and "may have landed" not in str(e.value)
    # the same words from a write are the other kind
    gw, _ = gateway(settings, tool("slack_send_message", error, is_error=True))
    with pytest.raises(SlackTimeout):
        gw.send("C1", "x")


def test_the_list_tool_offered_beside_a_send_still_fails_that_run_closed(settings):
    """Putting the tool in the read set does not make it welcome in other runs: offered next to a send it is another
    connector tool (R42a), and the send is refused."""
    offered = replace(tool("slack_send_message", SEND_OK), permission_mode="dontAsk",
                      init_tools=["ToolSearch", PREFIX + "slack_send_message", PREFIX + LIST_TOOL])
    with pytest.raises(SlackError, match="unexpected tool offered"):
        ClaudeConnectorSlack(settings, run=FakeRun(offered)).send("C1", "x")


def test_list_channels_is_a_read_so_it_works_while_the_gateway_is_paused(settings, store):
    controls.set_paused(store, True, NOW)
    gw, run = gateway(settings, tool(LIST_TOOL, RAW), store=store)
    assert rows(gw.list_channels()) == KEPT and len(run.calls) == 1  # whether to ask while paused is the caller's rule


def test_list_channels_retries_once_when_the_model_did_not_call_the_tool(settings):
    gw, run = gateway(settings, ClaudeResult("DONE", [], 0.0, True, None), tool(LIST_TOOL, RAW))
    assert rows(gw.list_channels()) == KEPT and len(run.calls) == 2


def test_the_default_runner_of_list_channels_is_offline_guarded(settings):
    with pytest.raises(OfflineError):
        ClaudeConnectorSlack(settings).list_channels()  # the real run_claude: the suite never reaches Slack


@pytest.mark.parametrize("text", [
    RAW[:400],                                                       # cut mid-JSON
    RAW + "\n[... 9000 characters truncated ...]",
    "Error: MCP tool output (60000 tokens) exceeds maximum allowed tokens (25000).",
    "Output too large (61.2KB). Full output saved to: /Users/cb/.claude/projects/x/tool-results/abc.txt",
    json.dumps({"result": "Here are your channels:\n" + PAGE}),      # the header is not where it must be
    "",
])
def test_a_cut_or_replaced_list_raises_read_truncated(settings, text):
    gw, _ = gateway(settings, tool(LIST_TOOL, text))
    with pytest.raises(SlackError, match="read truncated"):
        gw.list_channels()


def test_a_page_that_says_it_is_cut_raises_and_names_the_numbers(settings):
    gw, _ = gateway(settings, tool(LIST_TOOL, swap(RAW, "showing 8 of 8 total", "showing 8 of 80 total")))
    with pytest.raises(SlackError, match=r"truncated.*8 of 80"):
        gw.list_channels()


def test_a_page_with_a_forged_section_or_a_repeated_name_raises_unreadable(settings):
    forged = swap(PAGE, "3. Internal Tech Seminar(Optional)", "3. Internal Tech Seminar(Optional)\n" + FORGED)
    for text in (wrap(forged), page_of(section("alpha", "C01AAAAAAAA"), section("alpha", "C01BBBBBBBB")), page_of()):
        gw, _ = gateway(settings, tool(LIST_TOOL, text))
        with pytest.raises(SlackError, match="channel list unreadable"):
            gw.list_channels()


def test_words_like_saved_to_or_truncated_inside_a_purpose_are_data_not_a_notice(settings):
    page = swap(PAGE, "Short notes, one a week.", "I saved to drive; output truncated, too large, exceeds maximum.")
    gw, _ = gateway(settings, tool(LIST_TOOL, wrap(page)))
    assert rows(gw.list_channels()) == KEPT


def test_the_outage_counting_wrapper_counts_the_list_call():
    fake = FakeSlack()
    fake.channels = list(LIST)
    health = SlackHealth(fake)
    assert health.list_channels() == LIST and (health.calls, health.failures) == (1, 0)
    fake.fail_next("boom", method="list_channels")
    with pytest.raises(SlackError):
        health.list_channels()
    assert (health.calls, health.failures) == (2, 1)


def test_the_runtimes_gateway_can_list_channels(settings):
    rt = cli.build_runtime(settings)
    try:
        assert isinstance(rt.slack, SlackHealth)
        with pytest.raises(OfflineError):  # the real run_claude, offline in the suite
            rt.slack.list_channels()
        assert rt.slack.calls == 1
    finally:
        rt.store.close()


def test_the_fake_scripts_the_list_and_counts_reads():
    fake = FakeSlack()
    assert fake.list_channels() == [] and fake.channel_reads == 1
    fake.channels = list(LIST)
    assert fake.list_channels() == LIST and fake.list_channels() is not fake.channels
    fake.fail_next("boom", method="read_channel")
    assert fake.list_channels() == LIST  # armed for another method: untouched
    fake.fail_next("boom", method="list_channels")
    with pytest.raises(SlackError):
        fake.list_channels()
    assert fake.list_channels() == LIST and fake.channel_reads == 6


# --------------------------------------------------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------------------------------------------------

def put(store, channels_, fetched_at=NOW.isoformat()):
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": fetched_at, "channels": channels_}))


def entry(c: Channel) -> dict:
    return {"id": c.id, "name": c.name, "private": c.private}


def scripted(*chs: Channel) -> FakeSlack:
    fake = FakeSlack()
    fake.channels = list(chs)
    return fake


def test_the_cache_is_one_cursor_with_the_fetch_time_and_the_channels(store):
    assert channels.CACHE_KEY == "slack_channels"
    assert channels.refresh(scripted(*LIST), store, NOW) is True
    assert json.loads(store.get_cursor("slack_channels")) == {"fetched_at": NOW.isoformat(),
                                                              "channels": [entry(c) for c in LIST]}


def test_nothing_cached_is_no_time_and_no_channel(store):
    assert channels.cached(store) == (None, [])


def test_cached_returns_what_was_stored_with_an_aware_time(store):
    channels.refresh(scripted(*LIST), store, NOW)
    at, found = channels.cached(store)
    assert at == NOW and at.utcoffset() == timedelta(hours=3) and found == LIST


@pytest.mark.parametrize("raw", ["", "not json", "[]", "5", "null", '"x"', "{}", '{"channels": "x"}', '{"channels": {"a": 1}}',
                                 '{"fetched_at": 5, "channels": 7}'])
def test_a_cache_that_is_not_the_cache_is_no_channel(store, raw):
    store.set_cursor(channels.CACHE_KEY, raw)
    assert channels.cached(store)[1] == []


@pytest.mark.parametrize("bad", [
    {"id": "C01AAAAAAAA", "name": "<img src=x onerror=alert(1)>", "private": False},   # markup
    {"id": "C01AAAAAAAA", "name": "a" * 5000, "private": False},                        # a very long name
    {"id": "C01AAAAAAAA", "name": "a" * 81, "private": False},
    {"id": "C01AAAAAAAA", "name": "Upper", "private": False},
    {"id": "C01AAAAAAAA", "name": "with space", "private": False},
    {"id": "C01AAAAAAAA", "name": "\u0441hat", "private": False},                       # a Cyrillic es
    {"id": "C01AAAAAAAA", "name": "chat-random\n", "private": False},                   # a trailing newline
    {"id": "D08D9HFB8BB", "name": "a-dm", "private": False},                            # a DM
    {"id": "U08CZCGJW3E", "name": "a-user", "private": False},
    {"id": "<script>alert(1)</script>", "name": "x", "private": False},
    {"id": "C01AAAAAAAA\n", "name": "x", "private": False},
    {"id": "C01AAAAAAAA", "name": "x", "private": "yes"},                               # not a bool
    {"id": "C01AAAAAAAA", "name": "x", "private": 1},
    {"id": "C01AAAAAAAA", "name": "x"},
    {"id": "C01AAAAAAAA", "private": False},
    {"name": "x", "private": False},
    {"id": 5, "name": "x", "private": False},
    {"id": "C01AAAAAAAA", "name": 5, "private": False},
    "C01AAAAAAAA", 5, None, ["C01AAAAAAAA", "x", False],
])
def test_a_cached_channel_that_fails_the_shape_check_is_dropped_the_rest_stay(store, bad):
    """Review focus 5: the cached JSON is read back through the same shape rules as the page."""
    put(store, [entry(SURVIVE), bad, entry(LIST[0])])
    assert channels.cached(store)[1] == [SURVIVE, LIST[0]]


def test_a_repeated_id_in_the_cache_is_kept_once(store):
    put(store, [entry(SURVIVE), entry(LIST[0]), {**entry(LIST[1]), "id": SURVIVE.id}])
    assert channels.cached(store)[1] == [SURVIVE, LIST[0]]


@pytest.mark.parametrize("stamp", [None, 5, "", "yesterday", "2026-10-22T11:00:00"])  # the last is naive
def test_a_cache_without_a_readable_aware_time_keeps_its_channels_and_has_no_age(store, stamp):
    put(store, [entry(SURVIVE)], fetched_at=stamp)
    assert channels.cached(store) == (None, [SURVIVE])


def test_cached_ignores_other_keys(store):
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": NOW.isoformat(), "channels": [entry(SURVIVE)], "x": 1}))
    assert channels.cached(store) == (NOW, [SURVIVE])


# --------------------------------------------------------------------------------------------------------------------
# refresh: at most once a day, and only a good read replaces the list
# --------------------------------------------------------------------------------------------------------------------

def test_the_first_refresh_reads_and_stores_the_list(store):
    fake = scripted(*LIST)
    assert channels.refresh(fake, store, NOW) is True and fake.channel_reads == 1
    assert channels.cached(store) == (NOW, LIST)


def test_a_refresh_within_a_day_makes_no_call(store):
    fake = scripted(*LIST)
    channels.refresh(fake, store, NOW)
    for later in (timedelta(seconds=1), timedelta(hours=12), DAY - timedelta(seconds=1)):
        assert channels.refresh(fake, store, NOW + later) is False
    assert fake.channel_reads == 1 and channels.cached(store)[0] == NOW


def test_a_refresh_after_a_day_reads_again_and_replaces_the_list(store):
    fake = scripted(*LIST)
    channels.refresh(fake, store, NOW)
    fake.channels = [SURVIVE]
    assert channels.refresh(fake, store, NOW + DAY) is True
    assert channels.cached(store) == (NOW + DAY, [SURVIVE])


def test_force_reads_within_a_day(store):
    fake = scripted(*LIST)
    channels.refresh(fake, store, NOW)
    fake.channels = [SURVIVE]
    assert channels.refresh(fake, store, NOW + timedelta(minutes=5), force=True) is True
    assert fake.channel_reads == 2 and channels.cached(store) == (NOW + timedelta(minutes=5), [SURVIVE])


def test_a_clock_that_went_backwards_does_not_stall_the_refresh(store):
    channels.refresh(scripted(*LIST), store, NOW)
    assert channels.due(store, NOW - timedelta(hours=1)) is True  # fetched "in the future": ask again


def test_only_well_formed_channels_are_stored(store, caplog):
    """The page parser has checked them, but the cache holds nothing else whatever gateway the list came from."""
    caplog.set_level(logging.INFO)
    fake = scripted(SURVIVE, Channel("C1", "short-id", False), Channel("C01AAAAAAAA", "Bad Name", False),
                    Channel("D08D9HFB8BB", "a-dm", False), Channel("C01BBBBBBBB", "<b>x</b>", True), LIST[0])
    assert channels.refresh(fake, store, NOW) is True
    assert [c["id"] for c in json.loads(store.get_cursor(channels.CACHE_KEY))["channels"]] == [SURVIVE.id, LIST[0].id]
    assert channels.cached(store) == (NOW, [SURVIVE, LIST[0]])
    assert "4 of 6 channel(s) not kept" in caplog.text and "Bad Name" not in caplog.text


def test_an_answer_with_no_well_formed_channel_keeps_the_list(store):
    stale(store)
    assert channels.refresh(scripted(Channel("C1", "x", False), Channel("C01AAAAAAAA", "Bad Name", False)), store, NOW) is False
    assert channels.cached(store) == (NOW - timedelta(hours=25), LIST)


def test_a_cache_with_no_readable_time_is_refreshed(store):
    put(store, [entry(SURVIVE)], fetched_at="yesterday")
    fake = scripted(*LIST)
    assert channels.refresh(fake, store, NOW) is True and channels.cached(store) == (NOW, LIST)


# -- while paused -----------------------------------------------------------------------------------------------------

def test_a_paused_agent_refreshes_nothing_and_makes_no_call(store):
    """Ruling 1: no refresh while paused. Posting is refused anyway, and the list is read again after Resume."""
    controls.set_paused(store, True, NOW - timedelta(hours=1))
    fake = scripted(*LIST)
    assert channels.refresh(fake, store, NOW) is False
    assert channels.refresh(fake, store, NOW, force=True) is False  # a forced refresh does not override the pause
    assert fake.channel_reads == 0 and store.cursors("slack_channels") == {}
    controls.set_paused(store, False, NOW)
    assert channels.refresh(fake, store, NOW + timedelta(minutes=1)) is True and fake.channel_reads == 1


def test_the_cache_is_still_read_while_paused(store, settings):
    """Only the Slack call stops. The list is a cursor, so the dashboard and a refused DM `post` can still look at it."""
    channels.refresh(scripted(*LIST), store, NOW)
    controls.set_paused(store, True, NOW + timedelta(hours=1))
    assert channels.cached(store) == (NOW, LIST)
    assert [c.name for c in channels.targets(store, settings)][:2] == ["tf-leadership-demo", "announcements-demo"]
    assert channels.resolve(store, settings, "#survive-demo") == Channel("C01DDDDDDDD", "survive-demo", False)
    assert channels.post_channel(store, settings, "insight-1") == LEADERSHIP


def test_a_time_without_an_offset_is_refused(store):
    naive = datetime(2026, 10, 22, 11, 0)
    for call in (lambda: channels.due(store, naive), lambda: channels.wanted(store, naive),
                 lambda: channels.request_refresh(store, naive), lambda: channels.refresh(scripted(*LIST), store, naive),
                 lambda: channels.refresh(scripted(*LIST), store, naive, force=True)):  # force skips due(): the guard is its own
        with pytest.raises(ValueError, match="timezone-aware"):
            call()
    assert store.cursors("slack_channels") == {}


def test_a_pause_state_that_cannot_be_read_means_no_call(settings):
    class Unreadable(Store):
        def get_cursor(self, key):
            if key == controls.PAUSE_KEY:
                raise RuntimeError("database is locked")
            return super().get_cursor(key)

    st = Unreadable(settings.db_path)
    try:
        fake = scripted(*LIST)
        assert channels.refresh(fake, st, NOW) is False and fake.channel_reads == 0
    finally:
        st.close()


# -- a failed read ----------------------------------------------------------------------------------------------------

def stale(store, at=NOW - timedelta(hours=25)):
    """A cache from before, a day and an hour old at NOW."""
    put(store, [entry(c) for c in LIST], fetched_at=at.isoformat())


def test_a_failed_read_keeps_the_old_list_and_its_age(store):
    stale(store)
    fake = scripted(SURVIVE)
    fake.fail_next("slack_list_user_channels: boom", method="list_channels")
    assert channels.refresh(fake, store, NOW) is False
    assert channels.cached(store) == (NOW - timedelta(hours=25), LIST)  # the last good read, and when it was


def test_a_failed_read_with_no_list_leaves_none(store):
    fake = scripted(SURVIVE)
    fake.fail_next("boom", method="list_channels")
    assert channels.refresh(fake, store, NOW) is False and channels.cached(store) == (None, [])


@pytest.mark.parametrize("exc", [SlackError("boom"), ValueError("a cut page"), slack_gateway.SlackNotCalled("no call"),
                                 SlackTimeout("timeout"), slack_gateway.SlackUnavailable("down")])
def test_any_failed_read_keeps_the_list(store, exc):
    class Failing:
        def list_channels(self):
            raise exc

    stale(store)
    assert channels.refresh(Failing(), store, NOW) is False
    assert channels.cached(store)[1] == LIST


def test_an_empty_answer_never_replaces_the_list(store):
    stale(store)
    assert channels.refresh(scripted(), store, NOW) is False  # the gateway raises for a page with no channel; a fake may not
    assert channels.cached(store) == (NOW - timedelta(hours=25), LIST)


def test_after_a_failed_read_the_next_automatic_try_waits_six_hours(store):
    stale(store)
    fake = scripted(SURVIVE)
    fake.fail_next("boom", method="list_channels")
    channels.refresh(fake, store, NOW)
    assert fake.channel_reads == 1
    for wait in (timedelta(minutes=1), timedelta(hours=3), timedelta(hours=6) - timedelta(seconds=1)):
        assert channels.due(store, NOW + wait) is False
        assert channels.refresh(fake, store, NOW + wait) is False
    assert fake.channel_reads == 1  # not asked again
    assert channels.due(store, NOW + timedelta(hours=6)) is True
    assert channels.refresh(fake, store, NOW + timedelta(hours=6)) is True
    assert channels.cached(store) == (NOW + timedelta(hours=6), [SURVIVE])


def test_a_requested_refresh_ignores_the_six_hours(store):
    stale(store)
    fake = scripted(SURVIVE)
    fake.fail_next("boom", method="list_channels")
    channels.refresh(fake, store, NOW)
    assert channels.refresh(fake, store, NOW + timedelta(minutes=2), force=True) is True
    assert channels.cached(store)[1] == [SURVIVE]


def test_a_failed_forced_refresh_also_starts_the_six_hours(store):
    stale(store)
    fake = scripted(SURVIVE)
    fake.fail_next("boom", method="list_channels")
    assert channels.refresh(fake, store, NOW, force=True) is False
    assert channels.due(store, NOW + timedelta(hours=5)) is False and channels.due(store, NOW + timedelta(hours=6)) is True


def test_a_crash_in_the_read_still_starts_the_six_hours(store):
    """The try is marked before the call: an unexpected error, or a tick stopped by its watchdog, must not make every
    following tick (each a paid headless run) ask again."""
    class Crashing:
        def list_channels(self):
            raise RuntimeError("not a Slack error")

    stale(store)
    with pytest.raises(RuntimeError):
        channels.refresh(Crashing(), store, NOW)
    assert channels.due(store, NOW + timedelta(minutes=1)) is False and channels.due(store, NOW + timedelta(hours=6)) is True


def test_after_a_good_read_the_list_is_fresh_for_a_day_whatever_failed_before(store):
    stale(store)
    fake = scripted(SURVIVE)
    fake.fail_next("boom", method="list_channels")
    channels.refresh(fake, store, NOW)
    channels.refresh(fake, store, NOW + timedelta(hours=6))
    assert channels.due(store, NOW + timedelta(hours=7)) is False
    assert channels.due(store, NOW + timedelta(hours=6) + DAY) is True


def test_a_failed_read_logs_its_kind_and_no_text(store, caplog):
    stale(store)
    fake = scripted(SURVIVE)
    fake.fail_next("slack_list_user_channels: SECRET-PURPOSE-TEXT", method="list_channels")
    with caplog.at_level(logging.INFO):
        channels.refresh(fake, store, NOW)
    assert "SlackError" in caplog.text and "SECRET" not in caplog.text


# -- the request ------------------------------------------------------------------------------------------------------

def test_a_refresh_request_is_a_cursor_the_tick_takes(store):
    assert channels.REFRESH_KEY == "slack_channels_refresh"
    assert channels.requested(store) is False and channels.take_request(store) is False
    channels.request_refresh(store, NOW)
    assert channels.requested(store) is True and store.get_cursor("slack_channels_refresh")
    assert channels.take_request(store) is True
    assert channels.requested(store) is False and channels.take_request(store) is False


# The dashboard's "Refresh list" is throttled (Task 1's review, minor 6): the request itself, `request_refresh`, is unchanged, and
# `ask_refresh` is the door the dashboard uses.

def test_the_dashboards_refresh_request_is_throttled_to_one_in_ten_minutes(store):
    assert channels.ASKED_KEY == "slack_channels_asked" and channels.ASK_EVERY == timedelta(minutes=10)
    assert channels.ask_refresh(store, NOW) is None  # made
    assert channels.requested(store) is True and store.get_cursor(channels.REFRESH_KEY) == NOW.isoformat()
    assert channels.take_request(store) is True  # the tick took it and read the list
    for wait in (timedelta(seconds=1), timedelta(minutes=5), timedelta(minutes=10) - timedelta(seconds=1)):
        assert channels.ask_refresh(store, NOW + wait) == NOW  # too soon: the time of the request that was made
        assert channels.requested(store) is False  # and nothing was requested again
    again = NOW + timedelta(minutes=10)
    assert channels.ask_refresh(store, again) is None and channels.requested(store) is True
    assert store.get_cursor(channels.REFRESH_KEY) == again.isoformat()
    assert channels.ask_refresh(store, again + timedelta(minutes=1)) == again  # the window moved with the new request


def test_a_request_still_waiting_is_not_requested_again_inside_the_window(store):
    channels.ask_refresh(store, NOW)  # nobody takes it (a pause, the hold, a Mac asleep)
    assert channels.ask_refresh(store, NOW + timedelta(minutes=3)) == NOW
    assert store.get_cursor(channels.REFRESH_KEY) == NOW.isoformat()  # its time is the first request's, not the second's
    assert channels.ask_refresh(store, NOW + timedelta(minutes=11)) is None  # a long wait may be asked for again
    assert store.get_cursor(channels.REFRESH_KEY) == (NOW + timedelta(minutes=11)).isoformat()


def test_a_clock_that_went_back_or_a_damaged_time_never_holds_a_request_off(store):
    for junk in ((NOW + timedelta(hours=3)).isoformat(), "", "x", "2026-10-22T11:00:00", "null", "5"):  # the last two are no time
        store.set_cursor(channels.ASKED_KEY, junk)
        assert channels.ask_refresh(store, NOW) is None, junk
        channels.take_request(store)


def test_the_throttle_asks_for_a_time_with_an_offset(store):
    with pytest.raises(ValueError, match="timezone-aware"):
        channels.ask_refresh(store, datetime(2026, 10, 22, 11, 0))
    assert store.cursors("slack_channels") == {}


def test_wanted_is_a_request_or_a_refresh_that_is_due(store):
    assert channels.wanted(store, NOW) is True            # no list yet
    channels.refresh(scripted(*LIST), store, NOW)
    assert channels.wanted(store, NOW + timedelta(hours=1)) is False
    channels.request_refresh(store, NOW)
    assert channels.wanted(store, NOW + timedelta(hours=1)) is True
    channels.take_request(store)
    assert channels.wanted(store, NOW + DAY) is True


# --------------------------------------------------------------------------------------------------------------------
# targets and resolve
# --------------------------------------------------------------------------------------------------------------------

def test_targets_put_the_leadership_channel_first_then_the_rest_a_to_z(store, settings):
    assert settings.channel_id == LEADERSHIP
    put(store, [entry(c) for c in reversed(LIST)])
    got = channels.targets(store, settings)
    assert [c.name for c in got] == ["tf-leadership-demo", "announcements-demo", "chat-random", "ops_eu.alerts",
                                     "survive-demo", "team-private-dev", "weekly-digest"]
    assert got[0].id == LEADERSHIP  # its display name is the list's own
    assert [c.private for c in got] == [False, False, False, True, False, True, False]  # the flag travels with the channel


def test_the_leadership_channel_is_first_even_when_the_list_lacks_it(store, settings):
    put(store, [entry(c) for c in LIST if c.id != LEADERSHIP])
    got = channels.targets(store, settings)
    assert got[0].id == LEADERSHIP and got[0].private is False and got[0].name
    assert [c.id for c in got[1:]] == [c.id for c in sorted(LIST, key=lambda c: c.name) if c.id != LEADERSHIP]


def test_with_no_list_the_leadership_channel_is_the_only_target(store, settings):
    got = channels.targets(store, settings)
    assert [c.id for c in got] == [LEADERSHIP]


def test_targets_have_no_duplicates_and_no_bad_channel(store, settings):
    put(store, [entry(SURVIVE), entry(SURVIVE), entry(Channel(LEADERSHIP, "tf-leadership-demo", False)),
                {"id": LEADERSHIP, "name": "second-name", "private": False},
                {"id": "D08D9HFB8BB", "name": "a-dm", "private": False},
                {"id": "C01XXXXXXXX", "name": "<b>x</b>", "private": False}])
    got = channels.targets(store, settings)
    assert [(c.id, c.name) for c in got] == [(LEADERSHIP, "tf-leadership-demo"), (SURVIVE.id, "survive-ai")]


@pytest.mark.parametrize("text", [
    "C093L3Y8UM6", "c093l3y8um6", "#survive-ai", "survive-ai", "SURVIVE-AI", "#Survive-AI", "  #survive-ai  ", "\tsurvive-ai\n",
    "<#C093L3Y8UM6|survive-ai>", "<#C093L3Y8UM6>", "<#c093l3y8um6>", "<#C093L3Y8UM6|survive-ai-with-another-label>",
    " <#C093L3Y8UM6|survive-ai> ", "#C093L3Y8UM6",
])
def test_resolve_takes_an_id_a_name_with_or_without_the_hash_and_slacks_markup(store, settings, text):
    put(store, [entry(c) for c in LIST] + [entry(SURVIVE)])
    assert channels.resolve(store, settings, text) == SURVIVE


def test_resolve_finds_the_leadership_channel_by_id_name_and_markup(store, settings):
    put(store, [entry(c) for c in LIST])
    lead = Channel(LEADERSHIP, "tf-leadership-demo", False)
    for text in (LEADERSHIP, LEADERSHIP.lower(), "#tf-leadership-demo", "<#C0C62C128D8|whatever>", "<#C0C62C128D8>"):
        assert channels.resolve(store, settings, text) == lead


def test_the_leadership_channel_resolves_by_id_even_when_the_list_lacks_it(store, settings):
    got = channels.resolve(store, settings, LEADERSHIP)
    assert got is not None and got.id == LEADERSHIP
    assert channels.resolve(store, settings, "<#C0C62C128D8|tf-leadership-q4-2026>").id == LEADERSHIP
    assert channels.resolve(store, settings, "#tf-leadership-q4-2026") is None  # a name nobody listed


@pytest.mark.parametrize("text", [
    "#not-a-channel", "not-a-channel", "", "   ", "#", "##survive-ai", "#survive-ai please", "survive-ai\nC0C62C128D8",
    "D08D9HFB8BB", "<#D08D9HFB8BB|a-dm>", "<#D08D9HFB8BB>", "U08CZCGJW3E", "<@U08CZCGJW3E>", "<@C093L3Y8UM6>",
    "C0NOTINLIST1", "<#C0NOTINLIST1|survive-ai>", "<#C093L3Y8UM6", "C093L3Y8UM6>", "<C093L3Y8UM6>",
    "survive-a", "urvive-ai", "@here", "@channel", "everyone", "<!channel>", "#survive_ai",
    "surv\u0456ve-ai",              # a Cyrillic i
    "survive-ai\u200b",             # a zero-width space
    "\u212aappa-room",              # the Kelvin sign, which case-folds to an ASCII k
])
def test_resolve_refuses_what_is_not_a_target(store, settings, text):
    put(store, [entry(c) for c in LIST] + [entry(SURVIVE), entry(Channel("C01KAPPA000", "kappa-room", False))])
    assert channels.resolve(store, settings, text) is None


def test_resolve_refuses_a_name_that_is_not_in_the_list_even_if_it_is_a_real_channel_somewhere(store, settings):
    put(store, [entry(SURVIVE)])  # he is not in #chat-random, as far as the list knows
    assert channels.resolve(store, settings, "#chat-random") is None
    assert channels.resolve(store, settings, "C01AAAAAAAA") is None


def test_resolve_refuses_a_name_that_is_also_another_channels_id(store, settings):
    """A text that matches two different channels (one by id, one by name) is not guessed at."""
    tricky = Channel("C01TRICKY00", "c093l3y8um6", False)
    put(store, [entry(SURVIVE), entry(tricky)])
    assert channels.resolve(store, settings, "c093l3y8um6") is None
    assert channels.resolve(store, settings, "<#C093L3Y8UM6>") == SURVIVE  # markup is an id and nothing else
    assert channels.resolve(store, settings, "C01TRICKY00") == tricky


def test_resolve_refuses_a_name_shared_by_two_cached_channels(store, settings):
    put(store, [entry(SURVIVE), {"id": "C01OTHER000", "name": "survive-ai", "private": False}])
    assert channels.resolve(store, settings, "survive-ai") is None
    assert channels.resolve(store, settings, "C01OTHER000").id == "C01OTHER000"  # by id each is itself


@pytest.mark.parametrize("value", [None, 5, b"survive-ai", ["survive-ai"]])
def test_resolve_takes_only_text(store, settings, value):
    put(store, [entry(SURVIVE)])
    assert channels.resolve(store, settings, value) is None


# --------------------------------------------------------------------------------------------------------------------
# The channel a draft was posted to
# --------------------------------------------------------------------------------------------------------------------

def test_a_post_without_a_record_is_in_the_leadership_channel(store, settings):
    assert channels.post_channel(store, settings, "insight-2026-10-17-1") == LEADERSHIP
    assert channels.recorded_post_channel(store, "insight-2026-10-17-1") is None


def test_the_recorded_channel_is_what_the_post_channel_is(store, settings):
    channels.record_post_channel(store, "insight-2026-10-17-1", SURVIVE.id)
    assert store.get_cursor("post_channel:insight-2026-10-17-1") == SURVIVE.id
    assert channels.post_channel(store, settings, "insight-2026-10-17-1") == SURVIVE.id
    assert channels.recorded_post_channel(store, "insight-2026-10-17-1") == SURVIVE.id
    assert channels.post_channel(store, settings, "insight-2026-10-17-2") == LEADERSHIP  # another draft: untouched


def test_the_leadership_channel_can_be_recorded_too(store, settings):
    channels.record_post_channel(store, "message-1", LEADERSHIP)
    assert channels.recorded_post_channel(store, "message-1") == LEADERSHIP


@pytest.mark.parametrize("bad", ["D08D9HFB8BB", "U08CZCGJW3E", "W08CZCGJW3E", "", "#survive-ai", "survive-ai", "c093l3y8um6",
                                 "C093L3Y8UM6\n", "C093L3Y8UM6 ", "<#C093L3Y8UM6>", "C1", "C" + "A" * 13])
def test_a_channel_that_is_not_a_channel_id_is_never_recorded(store, settings, bad):
    with pytest.raises(ValueError):
        channels.record_post_channel(store, "insight-1", bad)
    assert store.get_cursor("post_channel:insight-1") is None
    assert channels.post_channel(store, settings, "insight-1") == LEADERSHIP


@pytest.mark.parametrize("junk", ["", "D08D9HFB8BB", "<script>", "survive-ai", "c093l3y8um6"])
def test_a_record_that_is_not_a_channel_id_reads_as_no_record(store, settings, junk):
    store.set_cursor("post_channel:insight-1", junk)
    assert channels.recorded_post_channel(store, "insight-1") is None
    assert channels.post_channel(store, settings, "insight-1") == LEADERSHIP


# --------------------------------------------------------------------------------------------------------------------
# The tick
# --------------------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def spy_jobs(calls):
    def make(name):
        def run(now):
            calls.append(name)
            return f"{name} done"
        return run
    return {name: make(name) for name in cli.JOBS}


def runtime(settings, store, slack) -> cli.Runtime:
    return cli.Runtime(settings=settings, store=store, slack=slack, llm=None, handlers={})


def tick_jobs(rt, calls=None, **extra):
    """The four jobs a tick always runs (spies) and the real channel refresh, wired as `default_jobs` wires it."""
    return {**spy_jobs([] if calls is None else calls), cli.CHANNELS_JOB: cli.default_jobs(rt)[cli.CHANNELS_JOB], **extra}


def tick(settings, store, jobs, clock):
    return cli.run_tick(settings, store, jobs, clock=clock)


def test_the_channel_refresh_is_one_of_the_default_jobs_and_not_one_of_the_four():
    assert cli.CHANNELS_JOB == "refresh_channels" and cli.CHANNELS_JOB not in cli.JOBS
    rt = runtime(None, None, FakeSlack())
    assert set(cli.default_jobs(rt)) == {cli.PAGE_JOB, cli.CHANNELS_JOB, *cli.JOBS}


def test_a_tick_reads_the_list_when_there_is_none_then_leaves_it_for_a_day(settings, store, capsys):
    slack = scripted(*LIST)
    clock = Clock(NOW)
    jobs = tick_jobs(runtime(settings, store, slack))
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 1 and channels.cached(store) == (NOW, LIST)
    assert "refresh_channels ok 7 channel(s)" in capsys.readouterr().out
    for later in (timedelta(minutes=1), timedelta(hours=12), DAY - timedelta(minutes=1)):
        clock.now = NOW + later
        assert cli.CHANNELS_JOB not in tick(settings, store, jobs, clock)
    clock.now = NOW + DAY
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 2 and channels.cached(store)[0] == NOW + DAY


def test_the_refresh_runs_after_the_page_commands_and_before_the_jobs(settings, store):
    calls = []
    rt = runtime(settings, store, scripted(*LIST))
    store.enqueue_page_command("i1", "skip", None, "0" * 64, NOW)  # page work is queued
    jobs = tick_jobs(rt, calls, **{cli.PAGE_JOB: lambda now: calls.append("page") or "1 page command(s)"})
    ran = tick(settings, store, jobs, Clock(NOW))
    assert ran == [cli.PAGE_JOB, cli.CHANNELS_JOB, *cli.JOBS]
    assert calls == ["page", *cli.JOBS]


def test_a_tick_without_the_refresh_job_is_as_before(settings, store):
    calls = []
    assert tick(settings, store, spy_jobs(calls), Clock(NOW)) == list(cli.JOBS)


def test_the_refresh_waits_while_the_huddle_poll_must_not_be_blinded(settings, store):
    """Workdays 14:25-16:05 until today's todo is final: no Slack call for a list nobody is waiting on."""
    slack = scripted(*LIST)
    jobs = tick_jobs(runtime(settings, store, slack))
    at = datetime(2026, 10, 22, 15, 0, tzinfo=KW)
    clock = Clock(at)
    assert cli.CHANNELS_JOB not in tick(settings, store, jobs, clock) and slack.channel_reads == 0
    channels.request_refresh(store, at)  # even a request waits
    clock.now = at + timedelta(minutes=1)
    assert cli.CHANNELS_JOB not in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 0 and channels.requested(store) is True
    store.set_cursor(f"nohuddle:{todo_item_id(date(2026, 10, 22))}", "1")  # the todo is settled
    clock.now = at + timedelta(minutes=2)
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 1 and channels.requested(store) is False


def test_a_refresh_that_a_slow_job_pushed_into_the_hold_does_not_read_and_keeps_its_request(settings, store, capsys):
    """The tick samples the hold when it starts (Task 1's review, minor 1). The page job before the refresh can run for minutes,
    so a refresh wanted at 14:24:30 may start at 14:26: the job looks at the hold again when it starts, and one that would begin
    inside the window is left, with its request and with no try marked, for the first tick after the hold."""
    slack = scripted(*LIST)
    start = datetime(2026, 10, 22, 14, 24, 30, tzinfo=KW)  # half a minute before the huddle window opens
    clock = Clock(start)
    store.enqueue_page_command("i1", "skip", None, "0" * 64, start)  # page work is queued, so the page job runs first ...
    channels.request_refresh(store, start)
    slow = {cli.PAGE_JOB: lambda now: setattr(clock, "now", start + timedelta(minutes=2)) or "a slow click"}  # ... and is slow
    jobs = tick_jobs(runtime(settings, store, slack), **slow)
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)  # it was wanted when the tick began
    assert "refresh_channels ok huddle hold; nothing read" in capsys.readouterr().out
    assert slack.channel_reads == 0 and channels.cached(store) == (None, [])
    assert channels.requested(store) is True and store.get_cursor(channels.TRIED_KEY) in (None, "")  # kept, and no try marked
    clock.now = datetime(2026, 10, 22, 16, 6, tzinfo=KW)  # the window is over (no slow page job this time)
    assert cli.CHANNELS_JOB in tick(settings, store, tick_jobs(runtime(settings, store, slack)), clock)
    assert slack.channel_reads == 1 and channels.requested(store) is False and channels.cached(store)[1] == LIST


def test_the_refresh_job_reads_when_the_hold_is_over_even_inside_the_window_hours(settings, store):
    """The hold ends with the Next Todo (settled), not only with the clock: inside 14:25-16:05 a settled todo lets the job read."""
    at = datetime(2026, 10, 22, 15, 0, tzinfo=KW)
    job = cli.default_jobs(runtime(settings, store, scripted(*LIST)))[cli.CHANNELS_JOB]
    assert job(at) == "huddle hold; nothing read"
    store.set_cursor(f"nohuddle:{todo_item_id(date(2026, 10, 22))}", "1")
    assert job(at) == f"{len(LIST)} channel(s)"


def test_the_refresh_job_leaves_a_request_for_the_hold_and_the_pause_alike(settings, store):
    slack = scripted(*LIST)
    job = cli.default_jobs(runtime(settings, store, slack))[cli.CHANNELS_JOB]
    channels.request_refresh(store, NOW)
    in_hold = datetime(2026, 10, 22, 15, 0, tzinfo=KW)
    assert job(in_hold) == "huddle hold; nothing read" and channels.requested(store) is True
    controls.set_paused(store, True, NOW)
    assert job(NOW) == "paused; nothing read" and channels.requested(store) is True
    assert slack.channel_reads == 0


def test_a_requested_refresh_tries_once_at_the_next_tick(settings, store):
    slack = scripted(*LIST)
    jobs = tick_jobs(runtime(settings, store, slack))
    clock = Clock(NOW)
    tick(settings, store, jobs, clock)
    assert slack.channel_reads == 1
    slack.channels = [SURVIVE]
    clock.now = NOW + timedelta(minutes=10)  # the list is fresh; the dashboard asks anyway
    channels.request_refresh(store, clock.now)
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 2 and channels.cached(store) == (clock.now, [SURVIVE])
    assert channels.requested(store) is False  # consumed
    clock.now += timedelta(minutes=1)
    assert cli.CHANNELS_JOB not in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 2


def test_a_requested_refresh_that_fails_is_not_tried_again_by_the_next_tick(settings, store):
    slack = scripted(*LIST)
    jobs = tick_jobs(runtime(settings, store, slack))
    clock = Clock(NOW)
    tick(settings, store, jobs, clock)
    clock.now = NOW + timedelta(minutes=10)
    channels.request_refresh(store, clock.now)
    slack.fail_next("boom", method="list_channels")
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert channels.requested(store) is False and channels.cached(store) == (NOW, LIST)  # consumed, the old list kept
    for later in (1, 30, 240, 24 * 60 - 20):  # minutes: the list is a day old only after the last
        clock.now = NOW + timedelta(minutes=10 + later)
        assert cli.CHANNELS_JOB not in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 2


def test_a_failed_daily_refresh_is_tried_again_after_six_hours_and_not_sooner(settings, store, capsys):
    stale(store)
    slack = scripted(SURVIVE)
    jobs = tick_jobs(runtime(settings, store, slack))
    clock = Clock(NOW)
    slack.fail_next("boom", method="list_channels")
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert "refresh_channels ok list not refreshed" in capsys.readouterr().out  # the fake notes no failure of its own
    for wait in (timedelta(minutes=1), timedelta(hours=3), timedelta(hours=6) - timedelta(minutes=1)):
        clock.now = NOW + wait
        assert cli.CHANNELS_JOB not in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 1
    clock.now = NOW + timedelta(hours=6)
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 2 and channels.cached(store) == (clock.now, [SURVIVE])


def test_a_paused_tick_never_reads_the_list_and_the_request_waits_for_resume(settings, store):
    controls.set_paused(store, True, NOW - timedelta(hours=1))
    slack = scripted(*LIST)
    jobs = tick_jobs(runtime(settings, store, slack))
    clock = Clock(NOW)
    channels.request_refresh(store, NOW - timedelta(minutes=5))
    assert tick(settings, store, jobs, clock) == []
    assert slack.channel_reads == 0 and channels.requested(store) is True and channels.cached(store) == (None, [])
    controls.set_paused(store, False, NOW)
    clock.now = NOW + timedelta(minutes=1)
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, clock)
    assert slack.channel_reads == 1 and channels.requested(store) is False and channels.cached(store)[1] == LIST


def test_a_pause_that_lands_during_the_tick_stops_the_refresh_and_keeps_the_request(settings, store):
    slack = scripted(*LIST)
    store.enqueue_page_command("i1", "skip", None, "0" * 64, NOW)
    pauses = {cli.PAGE_JOB: lambda now: controls.set_paused(store, True, now) or "paused it"}
    jobs = tick_jobs(runtime(settings, store, slack), **pauses)
    channels.request_refresh(store, NOW)
    ran = tick(settings, store, jobs, Clock(NOW))
    assert cli.CHANNELS_JOB in ran  # it was wanted when the tick began ...
    assert slack.channel_reads == 0 and channels.requested(store) is True  # ... and asked nothing


def test_a_failed_read_through_the_real_gateway_shows_in_the_tick_log_and_the_runs(settings, store, capsys):
    run = FakeRun(tool(LIST_TOOL, "channel_list_failed", is_error=True))
    gw = ClaudeConnectorSlack(settings, run=run, store=store)
    jobs = tick_jobs(runtime(settings, store, gw))
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, Clock(NOW))
    assert f"{cli.CHANNELS_JOB} fail slack_list_user_channels error" in capsys.readouterr().out
    assert [(job, ok) for job, ok, _, _ in store.runs() if job == cli.CHANNELS_JOB] == [(cli.CHANNELS_JOB, False)]
    assert channels.cached(store) == (None, []) and len(run.calls) == 1


def test_a_failed_list_read_is_in_the_run_log_but_not_in_the_weeks_failed_runs(settings, store):
    """Task 1's review, minor 4: the tick records the failed read as a failed run of the job, the run log keeps it, and the weekly
    "Failed job runs" does not count it (store.UNCOUNTED_JOBS). Another job's failed read still counts."""
    run = FakeRun(tool(LIST_TOOL, "channel_list_failed", is_error=True))
    jobs = tick_jobs(runtime(settings, store, ClaudeConnectorSlack(settings, run=run, store=store)))
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, Clock(NOW))
    assert [(job, ok) for job, ok, _, _ in store.runs() if job == cli.CHANNELS_JOB] == [(cli.CHANNELS_JOB, False)]
    today = datetime.now().astimezone().date()  # `record_run` stamps the wall clock
    assert cli.CHANNELS_JOB in UNCOUNTED_JOBS and store.failed_runs_on([today]) == 0
    store.record_run("process_dm", False, "slack read failed: slack_read_channel error")
    assert store.failed_runs_on([today]) == 1


def test_the_refresh_through_the_real_gateway_stores_what_the_page_says(settings, store):
    gw = ClaudeConnectorSlack(settings, run=FakeRun(tool(LIST_TOOL, RAW)), store=store)
    jobs = tick_jobs(runtime(settings, store, gw))
    assert cli.CHANNELS_JOB in tick(settings, store, jobs, Clock(NOW))
    assert channels.cached(store) == (NOW, LIST)


def test_an_unexpected_error_in_the_refresh_does_not_stop_the_other_jobs_or_repeat_every_tick(settings, store, capsys):
    class Broken(FakeSlack):
        def list_channels(self):
            self.channel_reads += 1
            raise RuntimeError("not a Slack error")

    slack = Broken()
    calls = []
    jobs = tick_jobs(runtime(settings, store, slack), calls)
    clock = Clock(NOW)
    ran = tick(settings, store, jobs, clock)
    assert ran == [cli.CHANNELS_JOB, *cli.JOBS] and calls == list(cli.JOBS)  # the four still ran
    assert "refresh_channels failed" in capsys.readouterr().err
    clock.now = NOW + timedelta(minutes=1)
    assert cli.CHANNELS_JOB not in tick(settings, store, jobs, clock) and slack.channel_reads == 1


def test_a_database_that_cannot_say_whether_a_refresh_is_due_does_not_stop_the_tick(settings, store, monkeypatch, capsys):
    def boom(st, now):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(channels, "wanted", boom)
    calls = []
    jobs = tick_jobs(runtime(settings, store, scripted(*LIST)), calls)
    assert tick(settings, store, jobs, Clock(NOW)) == list(cli.JOBS) and calls == list(cli.JOBS)
    assert "refresh_channels skipped" in capsys.readouterr().err


def test_the_default_job_says_what_it_did(settings, store):
    slack = scripted(*LIST)
    job = cli.default_jobs(runtime(settings, store, slack))[cli.CHANNELS_JOB]
    assert job(NOW) == "7 channel(s)"
    slack.fail_next("boom", method="list_channels")
    assert job(NOW + DAY) == "list not refreshed (the old one is kept)"
