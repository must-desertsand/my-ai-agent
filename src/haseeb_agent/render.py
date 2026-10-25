"""Slack text for every DM item and for channel posts. Pure functions only.

The Claude Slack connector sends standard markdown (``**bold**``, ``_italic_``,
``> quote``, ``[title](url)``), not Slack mrkdwn, and appends its own
"Sent using @Claude" suffix (Ruling R14), so nothing here adds that.

Everything that leaves this module has been through ``_safe``, which defuses
Slack mass mentions. Link destinations the renderer itself built pass through
``_safe`` as random placeholders and are swapped back in afterwards (``_Links``).
Insight and idea fields come from LLMs that read web and teammate content, so they
are also stripped of lines that imitate our footer, the model line, or a section header.
"""
from __future__ import annotations

import math
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse

FOOTER = "_— drafted by Haseeb's Agent_"
COMMANDS_HINT = "Reply in an insight's thread: post · edit: … · shorter · skip"
IDEA_COMMANDS_HINT = "Reply in an idea's thread: post · edit: … · shorter · skip"

EMOJI = {"AI": "🤖", "MONEY": "💰", "PRODUCT": "🧩", "GAP": "🕳️"}
IDEA_EMOJI = "💡"
MARKETS = ("PK", "GCC", "AFRICA", "GLOBAL")
MARKET_LABELS = {"PK": "Pakistan", "GCC": "GCC", "AFRICA": "Africa", "GLOBAL": "Global"}

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

ZWSP = "​"
# Slack's hard limit is 5,000 chars per element; leave room for " (i/n)".
_HARD_LIMIT = 4990


@dataclass(frozen=True)
class Source:
    title: str
    url: str


@dataclass
class Insight:
    title: str
    category: Literal["AI", "MONEY", "PRODUCT", "GAP"]
    what_happened: str
    why_it_matters: str
    silicon_valley: str
    evidence: list[Source]
    not_verified: list[str]
    read_min: int


@dataclass
class Idea:
    title: str
    market: Literal["PK", "GCC", "AFRICA", "GLOBAL"]
    price: str
    idea: str
    why_now: str
    who_pays: str
    build_with_ai: str
    first_step: str
    risks: list[str]
    evidence: list[Source]
    not_verified: list[str]
    read_min: int


# --------------------------------------------------------------------------
# Safety helpers
# --------------------------------------------------------------------------

# Every "&" that could begin an HTML entity (named or numeric, with any layering of &amp;).
_ENTITY_START = re.compile(r"&(?=[#A-Za-z])")
_AT_MENTION = re.compile(r"@(?=(?:channel|here|everyone)\b)", re.IGNORECASE)


def _safe(text: str) -> str:
    """Defuse Slack control sequences in the final text (Ruling R18).

    A zero-width space goes after every "<", which breaks <!channel>, <!subteam^..>,
    <@U..>, <#C..>, <http..|..> and anything else Slack would parse. @channel, @here
    and @everyone get one after the "@". Every "&" that starts what could be an HTML entity
    gets one too ("AT&T" becomes "AT&\u200bT"), so an HTML-decoding connector can't revive "<".
    Nothing is exempt: link destinations are swapped in afterwards by ``_Links.finish`` (Ruling R21).
    """
    text = text.replace("<", "<\u200b")
    text = _ENTITY_START.sub("&\u200b", text)
    return _AT_MENTION.sub("@\u200b", text)


_new_token = secrets.token_hex  # module-level so tests can pin the token


class _Links:
    """Per-render registry of the link destinations the renderer built (Ruling R21).

    ``dest(url)`` returns an opaque placeholder: a fresh random token wrapped in U+2060
    WORD JOINER. Every input field loses its Cf characters (U+2060 among them) before it is
    composed, so input cannot contain a placeholder; any bare occurrence of the token is
    removed as well. ``finish`` runs ``_safe`` over the whole composed text, then puts the
    destinations back in place of exactly the placeholders issued here.
    """

    def __init__(self) -> None:
        self._tok = _new_token(16)
        self._dests: list[str] = []
        self._ph = re.compile("\u2060" + re.escape(self._tok) + r"-(\d+)\u2060")

    def dest(self, url: str) -> str:
        self._dests.append(_strip_cf(url))
        return f"\u2060{self._tok}-{len(self._dests) - 1}\u2060"

    def _restore(self, m: re.Match) -> str:
        i = int(m.group(1))
        return self._dests[i] if i < len(self._dests) else ""

    def finish(self, text: str) -> str:
        parts = self._ph.split(text)  # even: composed text; odd: indices of issued placeholders
        for i in range(0, len(parts), 2):
            part = _strip_cf(parts[i])
            while self._tok in part:  # a removal can splice a new copy together
                part = part.replace(self._tok, "")
            parts[i] = part
        text = "".join(
            p if i % 2 == 0 else f"\u2060{self._tok}-{p}\u2060" for i, p in enumerate(parts)
        )
        return self._ph.sub(self._restore, _safe(text))


def _strip_cf(text: str) -> str:
    """Remove Unicode format characters (zero-width space/joiner, bidi marks, ...)."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u02bc": "'",
                         "\u2032": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"'})
_LEAD_NUM = re.compile(r"^\d+[.)]\s*")
_SECTION_NAMES = frozenset({
    "what happened", "why it matters to us", "if we were in silicon valley", "evidence", "not verified",
})
# An idea field may not imitate an idea's headers either. The v1 names stay in the set, so "Evidence" and
# "Not verified" are still caught.
_IDEA_SECTION_NAMES = _SECTION_NAMES | frozenset({
    "the idea", "why now", "who pays, and how much", "how we build it with ai", "first step this week",
    "risks · not verified",
})
_MD_PUNCT = "#-*_>[] \t"
_EDGE_PUNCT = "#*_>[]():.!\"' \t"
_FOOTER_LINE = re.compile(r"^[—–-]*\s*drafted by haseeb's agent$")


def _normalize_line(line: str) -> str:
    line = unicodedata.normalize("NFKC", line)
    line = _strip_cf(line).translate(_QUOTES)
    line = " ".join(line.split()).lower()
    prev = None
    while prev != line:  # strip leading markdown punctuation, bullets and "1." markers
        prev = line
        line = _LEAD_NUM.sub("", line.lstrip(_MD_PUNCT))
    return line


def _is_spoof(line: str, sections: frozenset[str] = _SECTION_NAMES) -> bool:
    """True only for a full-line imitation of our footer, model line, or a section header (one of `sections`)."""
    norm = _normalize_line(line)
    if not norm:
        return False
    if norm.startswith("wrote:") and "judged:" in norm:  # whatever separator it uses
        return True
    edge = norm.strip(_EDGE_PUNCT)
    return bool(_FOOTER_LINE.match(norm.strip(_EDGE_PUNCT + "—–-")) or _FOOTER_LINE.match(edge)
                or edge in sections)


_MD_LINK = re.compile(r"\[([^\[\]]*)\]\(([^()\s]*)\)")
_LINE_SEPS = re.compile("\r\n|[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def _unlink(text: str) -> str:
    """Free text never carries a live link: `[text](url)` becomes `text (url)` so the real
    destination is always visible, then every remaining bracket becomes a parenthesis, so no
    reference-style, image or hidden-link syntax can survive."""
    prev = None
    while prev != text:
        prev = text
        text = _MD_LINK.sub(r"\1 (\2)", text)
    return text.replace("[", "(").replace("]", ")")


def _clean_field(text: str, sections: frozenset[str] = _SECTION_NAMES) -> str:
    """Render an LLM- or teammate-written field verbatim, minus format characters, links and
    lines that imitate our own furniture (`sections`: the headers that count as furniture)."""
    text = _LINE_SEPS.sub("\n", text or "")
    text = _unlink(_strip_cf(text)).strip()
    return "\n".join(ln for ln in text.splitlines() if not _is_spoof(ln, sections)).strip()


def _one_line(text: str, sections: frozenset[str] = _SECTION_NAMES) -> str:
    return " ".join(_clean_field(text, sections).split())


def _link_title(text: str) -> str:
    return _one_line(text) or "source"


def _link_url(url: str) -> str:
    """Make a URL safe to sit inside `(...)`: no whitespace, parens, angle brackets, "@" or entity
    starts (`&#`, `&lt`, `&gt`, `&amp` get their "&" encoded; ordinary `&key=` query separators are
    kept). `_safe` never sees the result, so "@" is encoded here (Ruling R21)."""
    url = "".join(_strip_cf(url).split())
    for ch, enc in (("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E"), (";", "%3B"), ("@", "%40")):
        url = url.replace(ch, enc)
    return re.sub(r"&(?=#|lt|gt|amp)", "%26", url, flags=re.IGNORECASE)


_HTTP_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)(?::\d+)?(?:[/?#]|$)", re.IGNORECASE)


def _source_line(i: int, src: Source, links: _Links) -> str:
    """A real link, but the link text always ends with the true host, so a title like
    "https://good.com" cannot disguise where the link goes."""
    title = _link_title(src.title)
    url = _link_url(src.url)
    m = _HTTP_HOST.match(url)
    if not m or "\\" in url:  # not plain http(s), or a userinfo/backslash trick: no link
        return f"{i}. {title}"
    return f"{i}. [{title} ({m.group(1).lower()})]({links.dest(url)})"


# --------------------------------------------------------------------------
# Small formatters
# --------------------------------------------------------------------------

def _floor_score(score: float | None) -> int | None:
    """Whole-number score, floored (84.9 -> 84); None for missing, NaN or infinite."""
    if score is None or not math.isfinite(score):
        return None
    return int(math.floor(score))


def model_line(wrote: str, judged: str | None, score: float | None, deck: str | None) -> str:
    """`Wrote: <m> · Judged: <m> <score>/100 · Deck: <m|—>`."""
    if judged:
        n = _floor_score(score)
        judged_part = f"{judged} {n}/100" if n is not None else f"{judged} —"
    else:
        judged_part = "—"
    return f"Wrote: {wrote} · Judged: {judged_part} · Deck: {deck or '—'}"


MAX_NAME_CHARS = 200
MAX_MODEL_LINE_CHARS = 300
MAX_PERMALINK_CHARS = 500


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _model_footer(line: str) -> str:
    line = _unlink(' '.join(_strip_cf(line).split()))
    return f"_{_cap(line, MAX_MODEL_LINE_CHARS)}_"


def _day_label(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day:02d} {_MONTHS[day.month - 1]}"


# --------------------------------------------------------------------------
# Splitting
# --------------------------------------------------------------------------

def _units(text: str, seps: tuple[str, ...], limit: int) -> list[tuple[str, str]]:
    """Break text into (piece, separator_that_preceded_it) pieces, each <= limit."""
    if len(text) <= limit:
        return [(text, "")]
    if not seps:  # a single word longer than the limit: the only place we cut mid-word
        return [(text[i:i + limit], "") for i in range(0, len(text), limit)]
    sep = seps[0]
    out: list[tuple[str, str]] = []
    for i, part in enumerate(text.split(sep)):
        sub = _units(part, seps[1:], limit)
        out.append((sub[0][0], sep if i else ""))
        out.extend(sub[1:])
    return out


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Split on paragraphs, then lines, then words. Adds ` (i/n)` when there are several parts.

    `limit` applies before the suffix and is clamped so a part with its suffix stays
    under Slack's 5,000-char limit. Words are never cut unless one word alone exceeds it.
    """
    limit = max(1, min(limit, _HARD_LIMIT))
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: str | None = None
    for piece, sep in _units(text, ("\n\n", "\n", " "), limit):
        if cur is None:
            cur = piece
        elif len(cur) + len(sep) + len(piece) <= limit:
            cur += sep + piece
        else:
            chunks.append(cur)
            cur = piece
    if cur is not None:
        chunks.append(cur)
    n = len(chunks)
    if n == 1:
        return chunks
    return [f"{c} ({i}/{n})" for i, c in enumerate(chunks, 1)]


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------

def render_index(day: date, drafts: list[Insight | Idea], flagged: bool, score: float | None = None,
                 note: str | None = None) -> str:
    """Day header, numbered titles, and the commands hint. An insight's line has its category emoji, an idea's its price.

    A batch of ideas gets the idea wording; any other batch, an empty one included, keeps the v1 form.
    When `flagged`, `score` (the best draft's score) is required; a non-finite score shows as "—". `note` is one
    more warning line of our own (e.g. the judge was unavailable).
    """
    if flagged and score is None:
        raise ValueError("render_index(flagged=True) needs the best draft's score")
    n = len(drafts)
    ideas = bool(drafts) and all(isinstance(d, Idea) for d in drafts)
    lines = [f"☀️ {_day_label(day)} · Today's {n} {'idea' if ideas else 'insight'}{'' if n == 1 else 's'}"]
    if flagged:
        n = _floor_score(score)
        tail = f" (score {n}/100)" if n is not None else " (score —)"
        lines.append(f"⚠️ None passed the quality bar today; this is the best draft{tail}.")
    if note:
        lines.append(note)
    lines.append("")
    for i, d in enumerate(drafts, 1):
        if isinstance(d, Idea):
            lines.append(f"{i}. {_idea_title(d)}")
        else:
            lines.append(f"{i}. {EMOJI.get(d.category, '')} {_one_line(d.title)}")
    lines += ["", IDEA_COMMANDS_HINT if ideas else COMMANDS_HINT]
    return _safe("\n".join(lines))


def _insight_body(ins: Insight, links: _Links) -> str:
    evidence = "\n".join(_source_line(i, s, links) for i, s in enumerate(ins.evidence, 1))
    evidence = evidence or "_No sources listed._"
    unverified = "\n".join(f"- {_one_line(x)}" for x in ins.not_verified if _one_line(x)) or "- Nothing flagged."
    return "\n\n".join([
        f"**[What happened]**\n{_clean_field(ins.what_happened)}",
        f"**[Why it matters to us]**\n{_clean_field(ins.why_it_matters)}",
        f"**[If we were in Silicon Valley]**\n{_clean_field(ins.silicon_valley)}",
        f"**[Evidence]**\n{evidence}",
        f"**[Not verified]**\n{unverified}",
    ])


def render_insight_dm(n: int, ins: Insight, model_line: str) -> list[str]:
    """The DM version: numbered header, five sections, then the model line. No footer."""
    emoji = EMOJI.get(ins.category, "")
    header = f"**{n}. {emoji} {_one_line(ins.title)}** · {ins.read_min} min read"
    links = _Links()
    text = "\n\n".join([header, _insight_body(ins, links), _model_footer(model_line)])
    return split_message(links.finish(text))


def render_insight_post(ins: Insight) -> list[str]:
    """The channel version: same sections, ending with the footer. Never a model line."""
    emoji = EMOJI.get(ins.category, "")
    header = f"**{emoji} {_one_line(ins.title)}**"
    links = _Links()
    text = "\n\n".join([header, _insight_body(ins, links), FOOTER])
    return split_message(links.finish(text))


def _idea_text(text: str) -> str:
    return _clean_field(text, _IDEA_SECTION_NAMES)


def _idea_line(text: str) -> str:
    return _one_line(text, _IDEA_SECTION_NAMES)


def _idea_title(idea: Idea) -> str:
    """`💡 <title> · <price>`, the head of an idea's DM header, post header and index line. The title and the price
    are each one clean line."""
    return f"{IDEA_EMOJI} {_idea_line(idea.title)} · {_idea_line(idea.price)}"


def _idea_bullets(label: str, items: list[str]) -> list[str]:
    """`- <label>: <item>` for each item that is not empty once cleaned."""
    return [f"- {label}: {line}" for x in items if (line := _idea_line(x))]


def _idea_body(idea: Idea, links: _Links) -> str:
    evidence = "\n".join(_source_line(i, s, links) for i, s in enumerate(idea.evidence, 1))
    evidence = evidence or "_No sources listed._"
    risks = _idea_bullets("Risk", idea.risks)
    unverified = _idea_bullets("Not verified", idea.not_verified) or ["- Not verified: nothing flagged."]
    return "\n\n".join([
        f"**[The idea]**\n{_idea_text(idea.idea)}",
        f"**[Why now]**\n{_idea_text(idea.why_now)}",
        f"**[Who pays, and how much]**\n{_idea_text(idea.who_pays)}",
        f"**[How we build it with AI]**\n{_idea_text(idea.build_with_ai)}",
        f"**[First step this week]**\n{_idea_text(idea.first_step)}",
        f"**[Evidence]**\n{evidence}",
        "**[Risks · Not verified]**\n" + "\n".join(risks + unverified),  # one flat list: the risks, then the rest
    ])


def render_idea_dm(n: int, idea: Idea, model_line: str) -> list[str]:
    """The DM version: numbered header, seven sections, then the model line. No footer."""
    header = f"**{n}. {_idea_title(idea)}** · {idea.read_min} min read"
    links = _Links()
    text = "\n\n".join([header, _idea_body(idea, links), _model_footer(model_line)])
    return split_message(links.finish(text))


def render_idea_post(idea: Idea) -> list[str]:
    """The channel version: same sections, ending with the footer. Never a model line."""
    header = f"**{_idea_title(idea)}**"
    links = _Links()
    text = "\n\n".join([header, _idea_body(idea, links), FOOTER])
    return split_message(links.finish(text))


MAX_DM_CHARS = 3800


def _truncate_words(text: str, n: int) -> str:
    """Cut text to at most n chars at a word boundary, ending with an ellipsis."""
    if len(text) <= n:
        return text
    n = max(n, 1)
    cut = text[: n - 1]
    if not text[n - 1].isspace():
        ws = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
        if ws > 0:
            cut = cut[:ws]
    return cut.rstrip() + "…"


def _fit_draft(draft: str, build) -> str:
    """Render build(draft), truncating the draft so the result stays within MAX_DM_CHARS."""
    out = build(draft)
    allowed = len(draft)
    while len(out) > MAX_DM_CHARS and allowed > 1:
        allowed = max(1, min(allowed - 1, allowed - (len(out) - MAX_DM_CHARS)))
        draft = _truncate_words(draft, allowed)
        out = build(draft)
    return out


_PERMALINK = re.compile(
    r"https://(?i:[a-z0-9-]+(?:\.enterprise)?\.slack\.com)/archives/[A-Z0-9]+/p\d+(?:\?[A-Za-z0-9_=&.%-]*)?"
)


def _is_slack_permalink(url: str) -> bool:
    """Strict whole-string check; host is case-insensitive, the path is not."""
    if len(url) > MAX_PERMALINK_CHARS or "\\" in url or "@" in url:
        return False
    if any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in url):
        return False
    return _PERMALINK.fullmatch(url) is not None


REVIEW_LINE_CHARS = 240


def thread_mention_note(name: str) -> str:
    """The line on a card whose `post` goes into a thread of Haseeb's insight: it will mention the teammate."""
    return f"Posts in the thread of your insight, mentioning {_cap(_one_line(name), MAX_NAME_CHARS)}."


def render_feedback_dm(author: str, permalink: str, title: str, draft: str, model_line: str, *,
                       review_line: str | None = None, note: str | None = None) -> str:
    """The feedback card. `review_line` (the site review's "Reviewed: ..." or "Couldn't review ...") is one line under the
    title, and `note` (what `post` will do, when it is not the usual) another, both cleaned like every other piece of text that
    reaches the DM."""
    clean_title = _cap(_link_title(title), MAX_NAME_CHARS)
    link = permalink  # checked as given: whitespace or control characters mean no link
    links = _Links()
    title_md = f"[{clean_title}]({links.dest(_link_url(link))})" if _is_slack_permalink(link) else clean_title
    seen = _cap(_one_line(review_line or ""), REVIEW_LINE_CHARS)
    extra = _cap(_one_line(note or ""), REVIEW_LINE_CHARS)

    def build(d: str) -> str:
        quoted = "\n".join(f"> {ln}".rstrip() for ln in d.splitlines())
        head = f"**Feedback draft for {_cap(_one_line(author), MAX_NAME_CHARS)}**\n{title_md}"
        return links.finish("\n\n".join([head, *([seen] if seen else []), *([extra] if extra else []), quoted,
                                          _model_footer(model_line)]))

    return _fit_draft(_clean_field(draft), build)


REPLY_EXCERPT_CHARS = 160
_MARKDOWN = str.maketrans({"`": "'", "*": "", "_": " ", "~": ""})


def _excerpt(text: str, limit: int) -> str:
    """A teammate's words quoted in a card: cleaned like every field, and without the characters that make Markdown, so a ` or a
    * or a ~ in them cannot turn the draft and the footer below into code or bold. One line, cut short."""
    return _cap(" ".join(_one_line(text).translate(_MARKDOWN).split()), limit)



def _reply_card(head: str, who: str, permalink: str, reply: str, draft: str, model_line: str, review_line: str | None,
                note: str | None) -> str:
    """A DM card for a draft that answers a teammate's reply in a thread: a bold head, the reply quoted in short with a
    link to it, the site review's line (if any), a note (if any), the draft quoted and the model footer. Every piece is
    cleaned like the rest of what reaches the DM."""
    links = _Links()
    label = _cap(_one_line(who), MAX_NAME_CHARS)
    title = f"{label}'s reply"
    title_md = f"[{title}]({links.dest(_link_url(permalink))})" if _is_slack_permalink(permalink) else title
    quote = f'{title_md}: "{_excerpt(reply, REPLY_EXCERPT_CHARS)}"'
    seen = _cap(_one_line(review_line or ""), REVIEW_LINE_CHARS)
    extra = _cap(_one_line(note or ""), REVIEW_LINE_CHARS)

    def build(d: str) -> str:
        quoted = "\n".join(f"> {ln}".rstrip() for ln in d.splitlines())
        parts = [f"**{_cap(_one_line(head), MAX_NAME_CHARS)}**", quote, *([seen] if seen else []),
                 *([extra] if extra else []), quoted, _model_footer(model_line)]
        return links.finish("\n\n".join(parts))

    return _fit_draft(_clean_field(draft), build)


def render_followback_dm(thread_author: str, replier: str, permalink: str, reply: str, draft: str, model_line: str, *,
                         review_line: str | None = None) -> str:
    """The card of a follow-back: "Follow-back · <author>'s thread", the reply it answers quoted in short, the site review's
    "Reviewed: ..." line, the draft."""
    return _reply_card(f"Follow-back \u00b7 {_one_line(thread_author)}'s thread", replier, permalink, reply, draft,
                       model_line, review_line, None)


def render_insreply_dm(replier: str, permalink: str, reply: str, draft: str, model_line: str) -> str:
    """The card of a reply on Haseeb's insight: "Reply on your insight · <name>", their reply quoted in short, the draft. The
    note says what `post` will do, since it mentions them in a thread that is his."""
    name = _cap(_one_line(replier), MAX_NAME_CHARS)
    return _reply_card(f"Reply on your insight \u00b7 {name}", replier, permalink, reply, draft, model_line, None,
                       thread_mention_note(name))


def render_todo_dm(draft: str, autopost_at: datetime | None, model_line: str) -> str:
    def build(d: str) -> str:
        parts = [d]
        if autopost_at is not None:
            parts.append(f"Auto-posts at {autopost_at:%H:%M} unless you reply")
        parts.append(_model_footer(model_line))
        return _safe("\n\n".join(parts))

    return _fit_draft(_clean_field(draft), build)


def render_todo_post(draft: str) -> str:
    """The channel version of a Next Todo: the draft only (no model line, no footer), cleaned and defused."""
    return _safe(_clean_field(draft))


# --------------------------------------------------------------------------
# A message Haseeb wrote himself (the composer, or `say:` in the DM), polished by the agent
# --------------------------------------------------------------------------

MAX_NOTE_CHARS = 120  # the one cap: the parser (message_prompt) cuts a note here, and the card never shows more
# What Haseeb typed himself: @channel or @here at the start of the text or after a space, and not inside one of his
# links (`https://x.com/@channel` is a link, not a mention). Slack's own `<!channel>` form is turned into this by
# `message_prompt.normalize_raw`, before anything is counted; one that only appears after a `&lt;!channel&gt;` was
# decoded stays text.
_TYPED_MASS = re.compile(r"(?<!\S)@(channel|here)\b", re.IGNORECASE)
# What `_safe` makes of those two in a finished text: the "@" followed by a zero-width space. Only where a mention can
# be one: at the start or after a space (`foo@channel.com` is an address).
_DEFUSED_MASS = re.compile("(?<!\\S)@\u200b(channel|here)\\b", re.IGNORECASE)
PING_WORDS = {"channel": "everyone in the channel (@channel)", "here": "everyone who is online (@here)"}


def _outside_links(text: str) -> str:
    """`text` with each of its links replaced by a space: what he typed around them."""
    from .message_prompt import link_spans  # not at the top: message_prompt imports the deck check, which imports this
    out, last = [], 0
    for a, b in link_spans(text):
        out += [text[last:a], " "]
        last = b
    return "".join(out) + text[last:]


def mass_mentions(raw: str) -> dict[str, int]:
    """How many @channel and @here Haseeb typed in `raw` (his own words, before any model saw them), outside his links."""
    counts = {"channel": 0, "here": 0}
    for m in _TYPED_MASS.finditer(_outside_links(raw or "")):
        counts[m.group(1).lower()] += 1
    return counts


def _restore_mentions(text: str, typed: dict[str, int]) -> str:
    """Turn defused @channel / @here back into real <!channel> / <!here>, as many of each as Haseeb typed. A
    mention the model added is left defused."""
    left = dict(typed)

    def swap(m: re.Match) -> str:
        kind = m.group(1).lower()
        if left.get(kind, 0) > 0:
            left[kind] -= 1
            return f"<!{kind}>"
        return m.group(0)

    return _DEFUSED_MASS.sub(swap, text)


_USER_ID = re.compile(r"[UW][A-Z0-9]{8,13}")


def typed_people(raw: str, people: dict[str, str] | None) -> dict[str, tuple[str, int]]:
    """The people Haseeb mentioned by name in `raw` (`people`: {Name: user id}, from `message_prompt.slack_to_text`):
    {Name: (user id, how many times he typed `@Name`)}, outside his links. Only a well-formed user id counts."""
    text = _outside_links(raw or "")
    found = {}
    for name, uid in (people or {}).items():
        n = len(re.findall(rf"(?<!\S)@{re.escape(name)}(?!\w)", text)) if _USER_ID.fullmatch(uid or "") else 0
        if n:
            found[name] = (uid, n)
    return found


def _restore_people(text: str, typed: dict[str, tuple[str, int]]) -> str:
    """Turn `@Name` back into a real `<@U...>` mention for a person Haseeb mentioned, as many times as he typed it. A
    name the model added, or one more time than he did, stays plain text. The longest name goes first."""
    for name in sorted(typed, key=len, reverse=True):
        uid, left = typed[name]
        budget = [left]

        def swap(m: re.Match, uid: str = uid, budget: list[int] = budget) -> str:
            if budget[0] <= 0:
                return m.group(0)
            budget[0] -= 1
            return f"<@{uid}>"

        text = re.sub(rf"(?<!\S)@{re.escape(name)}(?!\w)", swap, text)
    return text


def _message_text(draft: str) -> str:
    """A message draft as `_clean_field` leaves it, except that nothing touches a link: format characters out, `[text](url)`
    shown as `text (url)` (the destination is always visible), and the lines that imitate our own furniture dropped. The
    square brackets are not touched here: `_protect_links` turns the ones outside his links into parentheses."""
    text = _strip_cf(_LINE_SEPS.sub("\n", draft or ""))
    prev = None
    while prev != text:
        prev = text
        text = _MD_LINK.sub(r"\1 (\2)", text)
    return "\n".join(ln for ln in text.strip().splitlines() if not _is_spoof(ln)).strip()


def _protect_links(text: str, links: _Links) -> str:
    """His own links (`message_prompt.link_spans`) become R21 placeholders, so `_safe` cannot change them (a zero-width
    space after the `&` of `?v=abc&t=10s`, a `[1]` turned into `(1)`); every bracket outside them becomes a parenthesis
    as everywhere else. `links.finish` swaps them back after the cleaner, exactly as written."""
    from .message_prompt import link_spans
    out, last = [], 0
    for a, b in link_spans(text):
        out += [text[last:a].replace("[", "(").replace("]", ")"), links.dest(text[a:b])]
        last = b
    return "".join(out) + text[last:].replace("[", "(").replace("]", ")")


def render_message_dm(draft: str, note: str | None, model: str | None = None, *, revised: bool = False,
                      pings: tuple[str, ...] | list[str] = ()) -> str:
    """The DM card of a message draft, in the style of the feedback drafts: a header, the draft quoted, a warning when
    `post` would notify people (`pings`: "channel", "here"), then one line on what changed (never part of the post) and
    the model that wrote it. The draft shows its @channel and @here defused: a card is never a ping."""
    head = "**Message draft" + (" (revised)" if revised else "") + "** · for the channel"
    tail: list[str] = []
    if who := " and ".join(PING_WORDS[p] for p in pings if p in PING_WORDS):
        tail.append(f"⚠️ Replying `post` notifies {who}.")
    if changed := _cap(_one_line(note or ""), MAX_NOTE_CHARS):
        tail.append(f"_Changed: {changed}_")
    if model:
        tail.append(_model_footer(f"Wrote: {model}"))

    def build(d: str) -> str:
        links = _Links()
        quoted = "\n".join(f"> {ln}".rstrip() for ln in _protect_links(d, links).splitlines())
        return links.finish("\n\n".join([head, quoted, *tail]))

    return _fit_draft(_message_text(draft), build)


def render_message_post(draft: str, typed: dict[str, int] | None = None,
                        people: dict[str, tuple[str, int]] | None = None) -> str:
    """The channel version of a message: the draft only (no footer, no note, no model line), cleaned and defused
    like every outgoing text, except that his own links go out exactly as he wrote them (I2). Then, and only then,
    the @channel and @here that Haseeb typed himself (`typed`, from `mass_mentions(raw)`) become real `<!channel>` /
    `<!here>`, and the people he mentioned (`people`, from `typed_people`) real `<@U...>` mentions, each as often as he
    typed it; every other mention stays defused or plain text."""
    links = _Links()
    text = _restore_mentions(links.finish(_protect_links(_message_text(draft), links)), typed or {})
    return _restore_people(text, people or {})


def message_pings(draft: str, raw: str | None) -> list[str]:
    """The mass mentions a post of this draft would really send ("channel", "here"): the ones Haseeb typed in his own
    words that the draft still holds. Worked out by the renderer the post itself uses, so a page or a card can't
    disagree with what goes out."""
    sent = render_message_post(draft, mass_mentions(raw or ""))
    return [w for w in ("channel", "here") if f"<!{w}>" in sent]


_USER_ID = re.compile(r"[UW][A-Z0-9]{2,20}")


def render_feedback_post(draft: str, *, mention: str | None = None) -> str:
    """The channel version of a feedback reply: the draft, cleaned and defused, then the agent footer.
    Never a model line. `mention`: the id of the teammate a reply in Haseeb's own insight thread is for, who gets a real
    mention in front (added after the cleaning, and only for an id that is one)."""
    out = _safe(_clean_field(draft)) + "\n\n" + FOOTER
    return f"<@{mention}> {out}" if mention and _USER_ID.fullmatch(mention) else out
