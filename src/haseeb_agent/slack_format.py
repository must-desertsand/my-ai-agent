"""Parse the Slack connector's formatted text into Msg objects.

The connector returns messages as text, not structured data, and wraps that text in JSON
(`{"messages": "<text>", "pagination_info": "..."}`). `unwrap_tool_text` undoes the wrapper;
every parser here calls it, so raw tool text and already unwrapped text both work.

Page shape (slack_read_channel, response_format "detailed"):

    Channel: #name (C0123)

    === Message from <Name> <<email>> (<UID>) at 2026-10-16 13:37:57 +03 ===
    Message TS: 1792147077.827169
    <body, may span several lines and contain blank lines>
    Thread: 1 replies (latest: 2026-10-16 14:17:37 +03)     optional trailer lines,
    Reactions: eyes (1)                                      at the bottom of the block
    Files: <name> (ID: F0..., application/pdf, 949.4 KB), ...

Slackbot has no email. Thread shape (slack_read_thread): `=== THREAD PARENT MESSAGE ===` with
`From:`/`Time:`/`Message TS:` lines, then `=== THREAD REPLIES (N total) ===` and `--- Reply i of N ---` blocks.

Security (ruling R8): a teammate controls message bodies, and a body can mimic connector formatting.
Header lines are accepted only when the ts sequence is consistent (channel pages: strictly decreasing;
thread replies: strict, every reply-marker-shaped line must be one of exactly N replies with indices 1..N in order
and strictly increasing ts, else ValueError). Trailer lines are peeled
only from the end, in the order the connector emits them (Thread, Reactions, Files), each once. Anything
else is body text. This cannot catch a fake that is itself consistent (for example a fake header with a
ts between two real ones, or a fake Files line as the very last line of a body that quotes no framing),
and a Files trailer is ignored for any message whose body quotes connector framing. Callers must not
trust sender or file ids for authority: Task 9 also requires user_id == USLACKBOT and an F-prefixed file id.

The channel list (slack_list_user_channels, `parse_channels`) is a Markdown page inside the connector's `{"result": ...}`
wrapper, read as strictly as a thread: see the comment above `parse_channels`.

Msg.latest_reply follows ruling R1: epoch seconds as a string, parsed from the "(latest: ...)" datetime
with the offset as given. Msg.thread_ts is the parent ts for a parent that has replies and for every reply,
and None for a message that is not part of a thread.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .models import CHANNEL_ID, CHANNEL_NAME, Channel, FileRef, Msg

log = logging.getLogger(__name__)

# `.+?` for the name is lazy, and the uid group is anchored at the end, so names that contain
# parentheses ("Lion cho (조중현,Cho Chung Hyun)") still yield the last (U...) group as the user id.
_WHO = re.compile(r"^(?P<name>.+?)(?: <(?P<email>[^<>]*)>)? \((?P<uid>[UW][A-Z0-9]+)\)$")
_TIME = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}(?::?\d{2})?"
_HEADER = re.compile(rf"^=== Message from (?P<who>.+?) at (?P<time>{_TIME}) ===\s*$")
_TS_LINE = re.compile(r"^Message TS: (?P<ts>\d+\.\d+)\s*$")
_FROM = re.compile(r"^From: (?P<who>.+?)\s*$")
_TIME_LINE = re.compile(rf"^Time: (?P<time>{_TIME})\s*$")
_THREAD_LINE = re.compile(rf"^Thread: (?P<n>\d+) repl(?:y|ies) \(latest: (?P<latest>{_TIME})\)\s*$")
_REACTION = r"[^\s,()]+ \(\d+\)"
_REACTIONS_LINE = re.compile(rf"^Reactions: {_REACTION}(?:, {_REACTION})*\s*$")
_FILES_LINE = re.compile(r"^Files: (?P<files>.+)$")
_FILE = re.compile(r"\(ID: (?P<id>[A-Z0-9]+), (?P<mime>[^,()]+), [^()]*\)")
_PARENT_MARK = re.compile(r"^=== THREAD PARENT MESSAGE ===\s*$")
_REPLIES_MARK = re.compile(r"^=== THREAD REPLIES \((?P<n>\d+) total\) ===\s*$")
_REPLY_MARK = re.compile(r"^--- Reply (?P<i>\d+) of (?P<n>\d+) ---\s*$")

SLACKBOT_ID = "USLACKBOT"
_BOT_EMAIL_DOMAIN = "@slack-bots.com"


def unwrap_tool_text(text: str) -> str:
    """Return the formatted text inside the connector's JSON wrapper.

    Takes the `messages` string (or `result`, which other connector tools use) of a JSON object.
    Decoding also resolves `\\uXXXX` escapes. Anything that is not such an object is returned unchanged.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(data, dict):
        for key in ("messages", "result"):
            if isinstance(data.get(key), str):
                return data[key]
    return text


def _epoch(stamp: str) -> str | None:
    """'2026-10-16 14:17:37 +03' -> epoch seconds as a string (R1); None if the stamp is not a real time.

    The offset is used as given, but must be under 24 h (and minutes under 60).
    """
    try:
        date_part, clock, offset = stamp.split(" ")
        m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", offset)
        if not m or int(m.group(2)) >= 24 or int(m.group(3) or 0) >= 60:
            return None
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        tz = timezone(delta if m.group(1) == "+" else -delta)
        dt = datetime.strptime(f"{date_part} {clock}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        return str(int(dt.timestamp()))
    except (ValueError, OverflowError):
        return None


def _ts_key(ts: str) -> Decimal:
    return Decimal(ts)


def _who(text: str) -> tuple[str, str, str | None]:
    m = _WHO.match(text.strip())
    if not m:
        raise ValueError(f"cannot parse sender ({len(text)} chars)")  # M6: never the line itself
    return m.group("uid"), m.group("name").strip(), m.group("email")


def _who_ok(text: str) -> bool:
    return bool(_WHO.match(text.strip()))


def _is_bot(uid: str, email: str | None) -> bool:
    return uid == SLACKBOT_ID or bool(email and email.lower().endswith(_BOT_EMAIL_DOMAIN))


def _parse_files(rest: str) -> list[FileRef]:
    """'name (ID: F1, mime, size), name2 (ID: F2, mime, size)'. Names may contain commas and parentheses,
    so each file is found by its '(ID: ...)' group and the name is the text since the previous group."""
    files: list[FileRef] = []
    pos = 0
    for m in _FILE.finditer(rest):
        name = rest[pos:m.start()].strip().lstrip(",").strip()
        files.append(FileRef(id=m.group("id"), name=name, mimetype=m.group("mime").strip()))
        pos = m.end()
    return files


def _split_trailer(lines: list[str], allow_files: bool = True) -> tuple[list[str], list[FileRef], int, str | None]:
    """Peel the optional trailer lines off the bottom of a message block.

    The connector emits them in the order Thread, Reactions, Files, so from the bottom they are tried as
    Files, then Reactions, then Thread, each at most once and with no gaps. A line that does not fit its
    slot (wrong order, malformed, unparseable datetime) is body text. With allow_files=False a Files line
    in its slot is not trusted: it takes no files and stays in the text.
    """
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    files: list[FileRef] = []
    reply_count, latest = 0, None
    ignored_files_line = None
    if lines and (m := _FILES_LINE.match(lines[-1])) and (found := _parse_files(m.group("files"))):
        if allow_files:
            files = found
        else:
            ignored_files_line = lines[-1]
        lines.pop()
    if lines and _REACTIONS_LINE.match(lines[-1]):
        lines.pop()
    if lines and (m := _THREAD_LINE.match(lines[-1])) and (epoch := _epoch(m.group("latest"))):
        reply_count, latest = int(m.group("n")), epoch
        lines.pop()
    if ignored_files_line is not None:
        lines.append(ignored_files_line)
    return lines, files, reply_count, latest


def _text(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n").rstrip()


def _mimics_framing(body: list[str]) -> bool:
    """True if a body line looks like connector framing (a message header or a thread marker)."""
    return any(
        _HEADER.match(l) or _REPLY_MARK.match(l) or _PARENT_MARK.match(l) or _REPLIES_MARK.match(l)
        for l in body
    )


def _msg(ts: str, who: str, body: list[str], thread_ts: str | None = None) -> Msg:
    uid, name, email = _who(who)
    # A body that quotes connector framing is hostile or confusing: trust no Files trailer from it.
    body, files, reply_count, latest = _split_trailer(body, allow_files=not _mimics_framing(body))
    if thread_ts is None and reply_count:
        thread_ts = ts
    return Msg(
        ts=ts, user_id=uid, user_name=name, text=_text(body), thread_ts=thread_ts,
        reply_count=reply_count, latest_reply=latest, files=files, is_bot=_is_bot(uid, email),
    )


def _channel_header(lines: list[str], i: int) -> tuple[str, str] | None:
    """(who, ts) if lines[i] is a well-formed message header followed by its Message TS line."""
    h = _HEADER.match(lines[i])
    if not h or i + 1 >= len(lines):
        return None
    t = _TS_LINE.match(lines[i + 1])
    if not t or not _who_ok(h.group("who")) or _epoch(h.group("time")) is None:
        return None
    return h.group("who"), t.group("ts")


def parse_channel(text: str) -> list[Msg]:
    """Messages of a slack_read_channel page, in page order (newest first, as the connector returns them).

    A header only starts a message when its ts is strictly lower than the previous accepted message's ts;
    otherwise the header and its TS line are body text of the current message.

    Channel-page messages are untrusted for authorship: a body can forge a header that is still
    consistent with the ts order. Verify any message you will act on with a thread read (parse_thread,
    which is strict) before acting.
    """
    lines = unwrap_tool_text(text).splitlines()
    starts: list[tuple[int, str, str]] = []  # (line index, who, ts)
    for i in range(len(lines)):
        found = _channel_header(lines, i)
        if found and (not starts or _ts_key(found[1]) < _ts_key(starts[-1][2])):
            starts.append((i, found[0], found[1]))
    msgs: list[Msg] = []
    for n, (i, who, ts) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        try:
            msgs.append(_msg(ts, who, lines[i + 2:end]))
        except ValueError:
            continue  # one bad message must not block the page
    return msgs


def _thread_block(lines: list[str]) -> tuple[str, str, str, list[str]] | None:
    """From/Time/Message TS header lines, then the body. Returns (who, time, ts, body_lines) or None."""
    if len(lines) < 3:
        return None
    f, t, s = _FROM.match(lines[0]), _TIME_LINE.match(lines[1]), _TS_LINE.match(lines[2])
    if not (f and t and s and _who_ok(f.group("who")) and _epoch(t.group("time")) is not None):
        return None
    return f.group("who"), t.group("time"), s.group("ts"), lines[3:]


def parse_thread(text: str) -> tuple[Msg, list[Msg]]:
    """(parent, replies) of a slack_read_thread result.

    Strict, because a body can mimic the framing. Every line shaped like a reply marker counts as a
    candidate, whether or not a valid block follows it. The thread is readable only if there are exactly N
    candidates (N from `THREAD REPLIES (N total)`), their indices are exactly 1..N in order, each says
    "of N", each is followed by well-formed From/Time/Message TS lines, and the ts values are strictly
    increasing and later than the parent's. A second `THREAD REPLIES` line, or any reply marker in the
    parent's region, also makes it unreadable. Otherwise ValueError: the caller must treat the thread as
    unreadable and must not skip the odd candidate out.
    """
    lines = unwrap_tool_text(text).splitlines()
    try:
        p = next(i for i, line in enumerate(lines) if _PARENT_MARK.match(line))
    except StopIteration:
        raise ValueError("no '=== THREAD PARENT MESSAGE ===' marker in thread text") from None
    r = next((i for i in range(p + 1, len(lines)) if _REPLIES_MARK.match(lines[i])), len(lines))
    declared = int(_REPLIES_MARK.match(lines[r]).group("n")) if r < len(lines) else 0

    if any(_REPLY_MARK.match(l) for l in lines[p + 1:r]):
        raise ValueError("reply marker inside the thread parent")
    if any(_REPLIES_MARK.match(l) for l in lines[r + 1:]) or any(_PARENT_MARK.match(l) for l in lines[p + 1:]):
        raise ValueError("duplicate thread framing line")

    block = _thread_block(lines[p + 1:r])
    if block is None:
        raise ValueError(f"unexpected thread parent header ({len(lines[p + 1:p + 4])} lines)")
    who, _, parent_ts, body = block
    parent = _msg(parent_ts, who, body, thread_ts=parent_ts)

    marks = [i for i in range(r + 1, len(lines)) if _REPLY_MARK.match(lines[i])]
    if len(marks) != declared:
        raise ValueError(f"thread declares {declared} replies but has {len(marks)} reply markers")
    blocks = []
    prev_ts = _ts_key(parent_ts)
    for k, i in enumerate(marks, 1):
        m = _REPLY_MARK.match(lines[i])
        if int(m.group("i")) != k or int(m.group("n")) != declared:
            raise ValueError(f"reply marker {m.group('i')} of {m.group('n')} out of sequence (expected {k} of {declared})")
        blk = _thread_block(lines[i + 1:i + 4])
        if blk is None or _ts_key(blk[2]) <= prev_ts:
            raise ValueError(f"reply {k} has a malformed or out-of-order header")
        prev_ts = _ts_key(blk[2])
        blocks.append(blk)

    replies: list[Msg] = []
    for n, (i, (who, _, ts, _)) in enumerate(zip(marks, blocks)):
        end = marks[n + 1] if n + 1 < len(marks) else len(lines)
        replies.append(_msg(ts, who, lines[i + 4:end], thread_ts=parent_ts))
    parent.reply_count = declared
    parent.latest_reply = _epoch(blocks[-1][1]) if blocks else None
    return parent, replies


_SENT_TS = re.compile(r"(?<![A-Za-z0-9_])(?:message_ts|ts)[\"']?\s*[:=]\s*[\"']?(\d{10}\.\d{6})(?!\d)")
_SENT_LINK = re.compile(r"/archives/[A-Z0-9]+/p(\d{10})(\d{6})(?!\d)")


def _flatten(data) -> str:
    if isinstance(data, dict):
        return "\n".join(f"{k}: {_flatten(v)}" for k, v in data.items())
    if isinstance(data, list):
        return "\n".join(_flatten(v) for v in data)
    return str(data)


def parse_sent(text: str) -> str:
    """The new message's ts, from a send result's `ts` field or its permalink (`.../p1792150147376979`).

    A `thread_ts` query parameter on the link names the parent, not the new message, and is ignored.
    """
    try:
        flat = _flatten(json.loads(text))
    except (TypeError, ValueError):
        flat = text
    if m := _SENT_TS.search(flat):
        return m.group(1)
    if m := _SENT_LINK.search(flat):
        return f"{m.group(1)}.{m.group(2)}"
    raise ValueError(f"no message ts or link in send result ({len(text or '')} chars)")


# --------------------------------------------------------------------------------------------------------------------
# The channel list: `slack_list_user_channels`
#
# The connector returns JSON whose `result` is a Markdown page (seen live on 2026-10-22; tests/fixtures/user_channels.txt):
#
#     ## My Channels (showing 39 of 39 total)
#
#     ### #<name>
#     - **ID:** C0123456789
#     - **Type:** Public Channel                  (or Private Channel)
#     - **Topic:** ...                            optional; a Topic or a Purpose may run over several lines
#     - **Purpose:** ...                          optional
#     - **Archived:** No
#
# A Topic and a Purpose are free text that other members of a channel edit, so they can hold anything, a line that looks
# like a `### #name` heading among it. They are never read. A page is trusted only when all of this holds, and one
# failure fails the read (the caller keeps the list it had, and nothing is ever posted from a short or doubtful one):
#   - the reply is the complete JSON object with a text `result`, and the header's "showing N of M" has N == M;
#   - the sections found (every line that starts `### #`), the ones dropped below included, number exactly N: a section
#     forged inside a Purpose adds one, a cut page lacks some;
#   - no id and no name appears twice among them (whatever their case), so a name cannot be put on another channel's id.
# A section is dropped, and only counted in a log line (never named), when its id or name does not have the shape of a
# Slack channel's (models.CHANNEL_ID, CHANNEL_NAME: ASCII names only), its type is neither Public nor Private Channel, or its
# Archived line says anything but No. The id and the type are the two lines right after the heading, and the section's own
# Archived line is the last `- **Archived:** ...` line in it (after any Topic or Purpose, so a forged one in free text comes
# before it, and nothing after it hides it); nothing else in a section is read. Lines end at `\n` only: a U+2028 or U+0085 in
# a Purpose does not start a line.
# --------------------------------------------------------------------------------------------------------------------

_CHANNELS_HEAD = re.compile(r"## My Channels \(showing ([0-9]{1,9}) of ([0-9]{1,9}) total\)")
_CHANNEL_HEADING = re.compile(r"### #(.*)")
_CHANNEL_ID_LINE = re.compile(r"- \*\*ID:\*\* (\S+)\s*")
_CHANNEL_TYPE_LINE = re.compile(r"- \*\*Type:\*\* (.+?)\s*")
_CHANNEL_ARCHIVED_LINE = re.compile(r"- \*\*Archived:\*\* (.*?)\s*")
_CHANNEL_TYPES = {"Public Channel": False, "Private Channel": True}  # -> Channel.private


def _leading_fields(body: list[str]) -> tuple[str | None, str | None, int]:
    """The id and the type of a section: the id line and the type line at its very top (in either order, each once), and how
    many lines they took. The first line that is neither ends them, so a Topic, a Purpose or a forged line is never read."""
    found: dict[str, str] = {}
    used = 0
    for line in body:
        if (m := _CHANNEL_ID_LINE.fullmatch(line)) and "id" not in found:
            found["id"] = m.group(1)
        elif (m := _CHANNEL_TYPE_LINE.fullmatch(line)) and "type" not in found:
            found["type"] = m.group(1)
        else:
            break
        used += 1
    return found.get("id"), found.get("type"), used


def _archived(rest: list[str]) -> bool:
    """True when the section's own Archived line does not say No. That is the LAST `- **Archived:** ...` line among the lines after
    the id and the type: the connector prints it after any Topic or Purpose, so a line of free text that looks like one comes before
    it and never stands in for it, and what comes after it (a field the connector adds later, a note after the last section, which
    is part of the last section's lines) does not hide it. A section with no such line is not archived."""
    for line in reversed(rest):
        if m := _CHANNEL_ARCHIVED_LINE.fullmatch(line):
            return m.group(1).strip().casefold() != "no"
    return False


def parse_channels(text: str) -> list[Channel]:
    """The channels of a `slack_list_user_channels` result, in page order. ValueError (a message with numbers and no name or
    id in it) for a reply that is not the complete page, a page that says it is cut, one whose sections do not add up or
    repeat an id or a name, and one with no channel in it. See the comment above."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError("read truncated: the reply is not the connector's complete JSON object") from None
    page = data.get("result") if isinstance(data, dict) else None
    if not isinstance(page, str):
        raise ValueError("the reply has no `result` text")
    lines = [line.rstrip("\r") for line in page.split("\n")]
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    head = _CHANNELS_HEAD.fullmatch(lines[first].strip()) if first is not None else None
    if head is None:
        raise ValueError("the page does not start with '## My Channels (showing N of M total)'")
    shown, total = int(head.group(1)), int(head.group(2))
    if shown != total:
        raise ValueError(f"read truncated: the page shows {shown} of {total} channels")
    headings = [i for i in range(first + 1, len(lines)) if _CHANNEL_HEADING.fullmatch(lines[i].rstrip())]
    if len(headings) != shown:
        cut = "read truncated: " if len(headings) < shown else ""
        raise ValueError(f"{cut}the page announces {shown} channel(s) and has {len(headings)} sections")

    kept: list[Channel] = []
    ids: set[str] = set()
    names: set[str] = set()
    for n, i in enumerate(headings):
        body = lines[i + 1:headings[n + 1] if n + 1 < len(headings) else len(lines)]
        name = _CHANNEL_HEADING.fullmatch(lines[i].rstrip()).group(1)
        cid, kind, used = _leading_fields(body)
        # repeats are looked for among every section, the dropped ones too (a forged one is usually a dropped one)
        if name.casefold() in names or (cid is not None and cid.casefold() in ids):
            raise ValueError("a channel id or name appears twice on the page")
        names.add(name.casefold())
        if cid is not None:
            ids.add(cid.casefold())
        if (cid is not None and CHANNEL_ID.fullmatch(cid) and CHANNEL_NAME.fullmatch(name) and kind in _CHANNEL_TYPES
                and not _archived(body[used:])):
            kept.append(Channel(cid, name, _CHANNEL_TYPES[kind]))
    if len(kept) != shown:
        log.info("channel list: %d of %d section(s) dropped (a bad id, name or type, or archived)", shown - len(kept), shown)
    if not kept:
        raise ValueError("no channel on the page could be read")
    return kept
