"""Designer decks: Codex may rephrase and lay out the insight, never add a fact (R45, R47).

Two readings of the deck must both pass the same text check:
- `fact_check_pdf`, the AUTHORITATIVE one (R47): the text pypdf extracts from every page of the rendered PDF, in
  its layout mode and again with rotated text kept (see `pdf_text`). It shows generated content, case changes and
  reordering as printed, but NOT text inside a Form XObject (opacity, blend modes), rasterized text (filters,
  masks) or rotated and mirrored text: the sanitizer strips those (R50a), and `coverage` refuses a deck whose HTML
  text the PDF reading does not show (C1a);
- `fact_check`, an extra layer on the sanitized HTML source: `visible_text` collects text nodes (SVG text too),
  alt/title/aria-label and similar attributes, and CSS `content:` strings. Inline tags (span, b, em, tspan...) glue
  their text to the neighbours, as the page shows it, so `<span>1</span><span>00</span>` is read as 100.

`check_text` requires, against the draft's own text (`source_text`: every field of an insight or an idea, the evidence
titles and hosts, the date and our branding; an idea's market label and price are fields too):
0. Unicode (R47): any non-ASCII look-alike of `@ . : /` fails (fullwidth, one-dot leader, ratio, fraction and
   division slashes...). Then the text is NFKC-normalized, and any non-ASCII letter that is not in the insight fails.
1. Numbers: every number token, after normalizing ($ , % stripped; ×2 = 2x = 2; 1M = 1 million; 20k = 20,000;
   spelled-out numbers such as "twenty", "two million", "seven percent"), is a number in the source. Dates are read
   first: "Nov 30", "November 30", "30 Nov" and 2026-11-30 are one date; a deck date with a year needs that year in
   the source. The source is read generously (raw and scaled values, "10-14" as a date too); the deck strictly.
2. Words (R49): every word of two or more letters (possessive dropped, simple inflections tried) is a word of the
   source (any case) or on the allowlist (branding, months, weekdays, slide labels, acronyms). Otherwise only a
   plain-shaped word (lowercase, Capitalized or ALL CAPS letters) passes, when it is on the bundled common list
   (`common_words.txt`) or a LOWERCASE entry of /usr/share/dict/words, and is not a known brand (BRAND_WORDS).
   Mixed case or internal caps (OpenAI, iPhone), digits (GPT5) or no dictionary match fail. Without the
   dictionary, the bundled list alone is used (one log line).
3. Banned: any `://` or `www.`, a domain other than an evidence host, any `@`, and `<!`, `<@`, `<#` (Slack control
   sequences).
A failure is reported as counts by type, never text, so the log carries no deck content. The refused tokens are
kept apart (`FactCheck.refused`, not in the repr) for the designer's one repair turn (R49).
"""
from __future__ import annotations

import functools
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from ..render import MARKET_LABELS, Idea, Insight
from .sanitize import strip_ignorable

log = logging.getLogger(__name__)

COMMON_WORDS = Path(__file__).with_name("common_words.txt")
# N2: IANA's root-zone list, vendored (https://data.iana.org/TLD/tlds-alpha-by-domain.txt, version 2026101700);
# never fetched at runtime. Refresh by replacing the file. Read lazily (F1, see iana_tlds).
TLD_FILE = Path(__file__).with_name("tlds-alpha-by-domain.txt")
_OWNER = re.compile(r"\bHaseeb[ \t\u00a0]+Zafar\b")
PAGES_REFUSED = "(the deck's slides did not print as one page each)"  # F2: what the repair turn is told
PAGE_BREAK = "\f"  # between pages in pdf_text's reading
KEPT_READING = "\v"  # before a page's second reading, the one with rotated text kept
DICT_WORDS = Path("/usr/share/dict/words")  # R49: its lowercase entries are plain English
MAX_REFUSED = 30

INLINE = frozenset({"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font", "i", "kbd", "mark",
                    "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "tspan", "u", "var", "textpath",
                    "nobr", "big", "strike", "tt", "ins", "del", "label", "wbr"})  # C2: <wbr> joins, never splits
TEXT_ATTRS = ("alt", "title", "aria-label", "aria-description", "aria-roledescription", "placeholder", "value",
              "label", "summary")
MONTHS = {m: i for i, ms in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")), 1) for m in ms}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun")
ALLOW = frozenset({
    # branding
    "haseeb", "agent", "agents", "must", "daily", "insight", "insights",
    # slide labels a designer adds
    "what", "happened", "why", "matters", "matter", "silicon", "valley", "evidence", "sources", "source", "verified",
    "unverified", "not", "next", "step", "steps", "takeaway", "takeaways", "summary", "agenda", "overview",
    "timeline", "key", "numbers", "number", "facts", "fact", "today", "now", "then", "before", "after", "move",
    "moves", "plan", "action", "actions", "goal", "goals", "measure", "metric", "metrics", "risk", "risks",
    "opportunity", "impact", "context", "playbook", "offer", "pricing", "price", "prices", "cost", "costs", "week",
    "weeks", "day", "days", "intro", "chart", "deck", "slide", "us",
    # acronyms
    "ai", "api", "apis", "roi", "kpi", "kpis", "ceo", "cto", "usd", "llm", "llms", "saas", "b2b", "gpu", "gpus",
})  # I1: month and weekday names are not here; they pass only when the insight's dates or text have them
WEEKDAY_OF = {w: i for i, ws in enumerate((("monday", "mon"), ("tuesday", "tue", "tues"), ("wednesday", "wed"),
                                           ("thursday", "thu", "thur", "thurs"), ("friday", "fri"),
                                           ("saturday", "sat"), ("sunday", "sun"))) for w in ws}
# Common English words that are also companies or AI products: never "common", they need the source.
BRAND_WORDS = frozenset({
    "apple", "meta", "oracle", "anthropic", "tesla", "llama", "mistral", "grok", "perplexity", "pinecone", "cohere",
    "alphabet", "intel", "notion", "slack", "discord", "stripe", "zoom", "cursor", "copilot", "azure", "alexa",
    "siri", "kindle", "android", "safari", "firefox", "chrome", "coinbase", "binance", "solana", "gemini", "claude",
    "sonnet", "opus", "haiku", "codex", "sora", "amazon", "windows", "twitter", "snap", "uber", "samsung", "openai",
    "google", "microsoft", "nvidia", "deepseek", "kimi", "qwen", "glm",
    # M1: AI and crypto names
    "xai", "salesforce", "tether", "ripple", "visa", "musk", "linear",
})
_ASCII_TARGETS = set("@.:/")
_LOOKALIKES_EXTRA = frozenset(
    "∶꞉ː˸։׃܃܄᛬᠃᠉⁚⸬︓﹕ꓽꓸ"
    "⁄∕⧸╱〳ノ㇓丿܁܂۔⸼꘎。｡ᐧ་"
    "․﹒．：／＠﹫")
_HARMLESS = frozenset("…‥")  # ellipsis, two-dot leader: decks use them; never part of a URL


@cache
def lookalikes() -> frozenset[str]:
    """Every non-ASCII character that NFKC turns into, or that just looks like, @ . : or /."""
    found = set(_LOOKALIKES_EXTRA)
    for cp in range(0x80, 0x30000):
        ch = chr(cp)
        norm = unicodedata.normalize("NFKC", ch)
        if norm != ch and _ASCII_TARGETS & set(norm):
            found.add(ch)
    return frozenset(found - _HARMLESS)


_MONTH_RX = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
             r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_SP = r"[ \t\u00a0]{1,3}"  # one line, at most 3 spaces: a layout reading's column gap never joins two tokens
# I1: a month then its day may sit on two lines or across a column gap (a "DEC" over "30" calendar tile)
_GAP = r"(?P<gap>\s{1,120}?)"
_DATE = re.compile(
    r"\b(?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})\b"
    rf"|\b{_MONTH_RX.replace('(', '(?P<mm>', 1)}{_GAP}(?:the{_SP})?(?P<md>\d{{1,2}})(?:st|nd|rd|th)?\b(?![\d.,]\d)"
    rf"(?:,?{_SP}(?P<my>\d{{4}})\b)?"
    rf"|(?<![\d.\-/])\b(?P<dd>\d{{1,2}})(?:st|nd|rd|th)?{_SP}(?:of{_SP})?{_MONTH_RX.replace('(', '(?P<dm>', 1)}"
    rf"(?:,?{_SP}(?P<dy>\d{{4}})\b)?"
    rf"|\b{_MONTH_RX.replace('(', '(?P<ym>', 1)}{_SP}(?P<yy>\d{{4}})\b"
    r"|(?<![\d/.])\b(?P<sa>\d{1,2})/(?P<sb>\d{1,2})(?:/(?P<sy>\d{4}|\d{2}))?\b(?![/\d])",  # 10/30, 30/10/2026
    re.IGNORECASE)
_LOOSE_MD = re.compile(r"(?<![\d.-])(\d{1,2})-(\d{1,2})(?![\d-])")
_SCALE = {"k": 3, "thousand": 3, "m": 6, "million": 6, "mn": 6, "b": 9, "bn": 9, "billion": 9, "t": 12,
          "trillion": 12}
_NUMBER = re.compile(
    r"(?<![\d.,])(?P<num>\d+(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"(?:\s?(?P<scale>thousand|million|billion|trillion|mn|bn|[kKmMbBtT])(?![A-Za-z]))?",
)
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_WORD_SCALES = {"thousand": 3, "million": 6, "billion": 9, "trillion": 12}
_NUM_WORD = "|".join([*_UNITS, *_TENS, "hundred", *_WORD_SCALES, "percent"])
_WORD_NUMBER = re.compile(rf"\b(?:{_NUM_WORD})(?:(?:{_SP}|-)(?:and{_SP})?(?:{_NUM_WORD}))*\b", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:['’][A-Za-z]+)?")
_DOMAIN = re.compile(r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}\b",
                     re.IGNORECASE)
@functools.lru_cache(maxsize=1)
def iana_tlds() -> frozenset[str] | None:
    """F1: the bundled TLD list, read on first use, never at import (cli imports this module through the deck job,
    so a bad file must not stop tick, insights or post: R44). None if it cannot be read: the caller then fails
    closed and never applies the sentence-boundary exception."""
    try:
        text = TLD_FILE.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        text = ""
    tlds = frozenset(x.strip().lower() for x in text.splitlines() if x.strip() and not x.startswith("#"))
    if len(tlds) < 100:
        log.warning("fact check: the bundled TLD list could not be read; the sentence-boundary exception is off")
        return None
    return tlds


def _sentence_boundary(match: str) -> bool:
    """N2: "approximate.Input", two layout columns side by side in the PDF reading. Only one dot, a lowercase letter
    before it and a Capitalized word after it that is not an IANA TLD: "evil.com.Then", "pay.evil.Visit" and
    "evil.Click" are all domains. The caller applies this to the PDF reading only."""
    m = re.fullmatch(r"[a-z0-9-]*[a-z]\.([A-Z][a-z]+)", match)
    if not m:
        return False
    tlds = iana_tlds()
    return tlds is not None and m.group(1).lower() not in tlds  # F1: no list, no exception


_CSS_CONTENT = re.compile(r"content\s*:\s*([^;}]*)", re.IGNORECASE)
_CSS_STRING = re.compile(r"\"([^\"]*)\"|'([^']*)'")


@dataclass
class FactCheck:
    ok: bool
    problems: Counter = field(default_factory=Counter)
    # The refused tokens themselves, for the designer's repair turn only (R49). Never logged; not in the repr.
    refused: tuple[str, ...] = field(default=(), repr=False)
    # The same tokens with their kind ("name", "number", ...), for `deck-test`'s terminal output only. Never logged.
    kinds: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __repr__(self) -> str:
        return f"FactCheck(ok={self.ok}, problems={dict(self.problems)})"


# -- the HTML reading ---------------------------------------------------------------------------------------------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.extra: list[str] = []
        self.skip = 0  # inside script (never in sanitized HTML) or style

    def _tag(self, tag, attrs):
        if tag not in INLINE:
            self.parts.append("\n")
        for name, value in attrs:
            if value and name in TEXT_ATTRS:
                self.extra.append(value)
            elif value and name == "style":
                self.extra.extend(_css_strings(value))

    def handle_starttag(self, tag, attrs):
        self._tag(tag, attrs)
        if tag in ("script", "style"):
            self.skip += 1

    def handle_startendtag(self, tag, attrs):
        self._tag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag not in INLINE:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            if self.lasttag == "style":
                self.extra.extend(_css_strings(data))
            return
        self.parts.append(data)


def _css_strings(css: str) -> list[str]:
    out = []
    for decl in _CSS_CONTENT.findall(css):
        out += [a or b for a, b in _CSS_STRING.findall(decl)]
    return out


def visible_text(page: str, *, extras: bool = True) -> str:
    """The page's text nodes, a block boundary as a newline; with `extras`, text attributes and CSS strings too."""
    p = _Text()
    p.feed(page or "")
    p.close()
    text = "".join(p.parts)
    return text + "\n" + "\n".join(p.extra) if extras else text


# -- the PDF reading ----------------------------------------------------------------------------------------------

def pdf_text(pdf: Path) -> str:
    """Each page's text as pypdf lays it out from the page geometry, which is the order a reader sees: measured on
    Chrome's output, it keeps a flex row-reverse as shown ("41" for <span>1</span><span>4</span>) and keeps two
    absolutely positioned boxes apart, where the plain mode glues them in source order ("tasksArgon"). C1(c): the
    page is read again with rotated text kept (pypdf strips it by default); that reading is added when it differs.
    The plain mode is used only for a page whose layout reading fails.

    pypdf does not see everything Chrome draws: text inside a Form XObject (opacity, blend modes), rasterized text
    (filters, masks) or rotated and mirrored text. The sanitizer strips those (R50a) and the coverage gate refuses
    a deck whose HTML text the PDF reading does not show (C1a)."""
    from pypdf import PdfReader

    out = []
    for page in PdfReader(str(pdf)).pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
            rotated = page.extract_text(extraction_mode="layout", layout_mode_strip_rotated=False) or ""
            out.append(text if rotated == text else f"{text}\n{KEPT_READING}{rotated}")
        except Exception:  # noqa: BLE001 - pypdf's layout mode is newer; fall back to the plain reading
            out.append(page.extract_text() or "")
    return f"\n{PAGE_BREAK}\n".join(out)


class _Slides(_Text):
    """The HTML reading split by outermost <section>: one text per slide, and whether any text was outside one."""
    def __init__(self):
        super().__init__()
        self.depth, self.slides, self.outside = 0, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "section":
            if not self.depth:
                self.slides.append(len(self.parts))
            self.depth += 1
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "section" and self.depth:
            self.depth -= 1
            if not self.depth:
                self.slides.append(len(self.parts))

    def handle_data(self, data):
        if not self.depth and not self.skip and data.strip():
            self.outside = True
        super().handle_data(data)


def _slide_texts(page: str) -> list[str] | None:
    p = _Slides()
    p.feed(page or "")
    p.close()
    if p.outside or p.depth or len(p.slides) % 2:
        return None
    return ["".join(p.parts[a:b]) for a, b in zip(p.slides[::2], p.slides[1::2])]


def _match(tokens: list[str], reading: str) -> list[str]:
    """The tokens with no occurrence of their own in `reading` (N1(b)): each token occurrence takes one
    non-overlapping occurrence in the whitespace-stripped reading. Longest tokens first, and first only occurrences
    that start and end where the reading had a token boundary, so a short token cannot spoil a longer one."""
    words = reading.split()
    blob, cuts, at = "".join(words), {0}, 0
    for w in words:
        at += len(w)
        cuts.add(at)
    used = bytearray(len(blob))
    todo = sorted(range(len(tokens)), key=lambda k: -len(tokens[k]))
    for aligned in (True, False):
        rest = []
        for k in todo:
            t, start = tokens[k], 0
            while (a := blob.find(t, start)) >= 0:
                b = a + len(t)
                if not any(used[a:b]) and (not aligned or (a in cuts and b in cuts)):
                    used[a:b] = b"\x01" * len(t)
                    break
                start = a + 1
            else:
                rest.append(k)
        todo = rest
    return [tokens[k] for k in sorted(todo)]


def coverage(page: str, pdf_reading: str) -> FactCheck:
    """C1(a), the coverage gate: every token of the HTML reading's text (NFKC, invisible characters deleted) must
    appear in the PDF reading once both are stripped of whitespace. Text the PDF reading cannot see (an XObject, a
    raster, a clip, a rotation) fails here even if a sanitizer rule missed the CSS that hid it.

    N1(b): it counts. Each token occurrence needs its own occurrence in the PDF reading, so fragments of other
    text (the SVG pattern probe: "$4" and "20" out of "$4 and $20") cannot stand in for text the reading does not
    show. Slide i (the deck's outermost <section> elements) is matched against page i (PAGE_BREAK) only. F2: when
    there is text outside a section or the counts differ, the deck is refused ("pages"). A page's rotated-kept
    reading (after KEPT_READING) is the one matched, since it holds the plain reading's text too."""
    norm = lambda t: unicodedata.normalize("NFKC", strip_ignorable(t))  # noqa: E731
    pages = [p.split(KEPT_READING)[-1] for p in pdf_reading.split(PAGE_BREAK)]
    while len(pages) > 1 and not pages[-1].strip():
        pages.pop()  # a trailing page with no text at all (a margin's overflow) holds nothing to match
    slides = _slide_texts(page)
    if slides is None or len(slides) != len(pages):  # F2: no whole-deck fallback; the repair turn, then the template
        return FactCheck(ok=False, problems=Counter({"pages": 1}), refused=(PAGES_REFUSED,),
                         kinds=(("pages", PAGES_REFUSED),))
    missing = []
    for slide, reading in zip(slides, pages):
        missing += _match(norm(slide).split(), norm(reading))
    problems = Counter({"coverage": len(missing)}) if missing else Counter()
    refused = tuple(dict.fromkeys(m[:60] for m in missing))[:MAX_REFUSED]
    return FactCheck(ok=not missing, problems=problems, refused=refused, kinds=tuple(("coverage", r) for r in refused))


# -- the source ---------------------------------------------------------------------------------------------------

def _host(url: str) -> str:
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def evidence_hosts(draft: Insight | Idea) -> set[str]:
    return {h for h in (_host(s.url) for s in draft.evidence) if h}


def source_parts(draft: Insight | Idea, day: date) -> tuple[str, str]:
    """(the draft's own text: its fields, evidence titles and hosts; the date and our branding). For an idea the
    fields are its title, market label, price, five text fields, risks and not-verified claims, and its branding is
    "Daily Idea" (an insight's is "Daily Insight"). I1: numbers come from the first only, so today's "17" or "2026"
    never supports "17 clients"; dates and words come from both."""
    if isinstance(draft, Idea):
        facts = [draft.title, MARKET_LABELS.get(draft.market, ""), draft.price, draft.idea, draft.why_now,
                 draft.who_pays, draft.build_with_ai, draft.first_step, *draft.risks]
        brand = "Daily Idea"
    else:
        facts = [draft.title, draft.category, draft.what_happened, draft.why_it_matters, draft.silicon_valley]
        brand = "Daily Insight"
    facts += [*(s.title for s in draft.evidence), *sorted(evidence_hosts(draft)), *draft.not_verified]
    extra = [day.isoformat(), f"{day.day} {day:%b} {day.year}", f"{day:%B} {day.day}, {day.year}", f"{day:%A}",
             "Haseeb's Agent", brand, "MUST"]
    return "\n".join(x for x in facts if x), "\n".join(extra)


def source_text(draft: Insight | Idea, day: date) -> str:
    """Everything Codex was given: the draft's fields, evidence titles and hosts, the date, our branding."""
    return "\n".join(source_parts(draft, day))


# -- numbers and dates --------------------------------------------------------------------------------------------

def _month(name: str | None) -> int | None:
    return MONTHS.get((name or "").lower().rstrip(".")) if name else None


def _dates(text: str) -> list[tuple[tuple[int | None, int, int | None], tuple[int, int]]]:
    out = []
    for m in _DATE.finditer(text):
        g = m.groupdict()
        if g["iy"]:
            y, mo, d = int(g["iy"]), int(g["im"]), int(g["id"])
        elif g["md"]:
            gap = g["gap"] or ""
            if ("\n" in gap or len(gap) > 3) and g["mm"].islower():
                continue  # "we may\n 20 ...": only a typeset month name ("Dec", "DEC") reaches across lines
            y, mo, d = (int(g["my"]) if g["my"] else None), _month(g["mm"]), int(g["md"])
        elif g["dd"]:
            y, mo, d = (int(g["dy"]) if g["dy"] else None), _month(g["dm"]), int(g["dd"])
        elif g["ym"]:
            y, mo, d = int(g["yy"]), _month(g["ym"]), None
        else:
            a, b = int(g["sa"]), int(g["sb"])
            mo, d = (a, b) if a <= 12 else (b, a)
            y = None if not g["sy"] else int(g["sy"]) + (2000 if len(g["sy"]) == 2 else 0)
        if mo and 1 <= mo <= 12 and (d is None or 1 <= d <= 31):
            out.append(((y, mo, d), m.span()))
    return out


def date_tokens(text: str) -> list[tuple[int | None, int, int | None]]:
    return [d for d, _ in _dates(text)]


def _mask(text: str, spans) -> str:
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def _fmt(v: Decimal) -> str:
    return format(v.normalize(), "f") if v else "0"


def _value(num: str, scale: str | None) -> str | None:
    try:
        v = Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None
    if scale:
        v = v.scaleb(_SCALE[scale.lower()])
    return _fmt(v)


def _spelled(phrase: str) -> str | None:
    """'two million' -> '2000000', 'twenty-three' -> '23', 'seven percent' -> '7'; a lone 'percent' -> None."""
    total, cur, seen = Decimal(0), Decimal(0), False
    for w in re.split(r"[\s\u00a0-]+", phrase.lower()):
        if w in _UNITS:
            cur, seen = cur + _UNITS[w], True
        elif w in _TENS:
            cur, seen = cur + _TENS[w], True
        elif w == "hundred":
            cur, seen = (cur or 1) * 100, True
        elif w in _WORD_SCALES:
            total, cur, seen = total + (cur or 1) * Decimal(10) ** _WORD_SCALES[w], Decimal(0), True
    return _fmt(total + cur) if seen else None


def _spelled_values(text: str) -> set[str]:
    rest = _mask(text, [m.span() for m in _NUMBER.finditer(text)])  # "1.5 million" is a digit token already
    return {v for m in _WORD_NUMBER.finditer(rest) if (v := _spelled(m.group(0))) is not None}


def number_tokens(text: str) -> list[tuple[str, str]]:
    """(the token as written, its normalized value) for every digit and spelled-out number in `text`."""
    out = [(m.group(0), v) for m in _NUMBER.finditer(text) if (v := _value(m.group("num"), m.group("scale"))) is not None]
    rest = _mask(text, [m.span() for m in _NUMBER.finditer(text)])
    out += [(m.group(0), v) for m in _WORD_NUMBER.finditer(rest) if (v := _spelled(m.group(0))) is not None]
    return out


def number_values(text: str) -> set[str]:
    """The deck side: each number token's normalized value (scaled by any k/M/B/million suffix), spelled-out too."""
    return {v for _, v in number_tokens(text)}


def _source_numbers(text: str) -> set[str]:
    out = _spelled_values(text)
    for m in _NUMBER.finditer(text):
        for scale in {m.group("scale"), None}:  # generous: "1M" supports 1M and 1
            if (v := _value(m.group("num"), scale)) is not None:
                out.add(v)
    return out


def _source_dates(text: str) -> set[tuple[int | None, int, int | None]]:
    found = set(date_tokens(text))
    for a, b in _LOOSE_MD.findall(text):  # "around 10-14"
        if 1 <= int(a) <= 12 and 1 <= int(b) <= 31:
            found.add((None, int(a), int(b)))
    return found


def _date_ok(d, dates, numbers) -> bool:
    y, mo, day = d
    year_ok = y is None or str(y) in numbers
    for sy, smo, sd in dates:
        if smo != mo:
            continue
        if day is not None and sd != day:
            continue
        if y is None or sy == y or year_ok:
            return True
    return False


# -- words --------------------------------------------------------------------------------------------------------

@cache
def default_common_words() -> frozenset[str]:
    lines = COMMON_WORDS.read_text(encoding="utf-8").splitlines()
    return frozenset(w.strip() for w in lines if w.strip() and not w.startswith("#")) - BRAND_WORDS


@cache
def dictionary_words() -> frozenset[str] | None:
    """R49: the lowercase entries of /usr/share/dict/words (capitalized ones are names), or None if it's missing."""
    try:
        text = DICT_WORDS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.info("fact check: %s is missing; using the bundled common-word list only", DICT_WORDS)
        return None
    return frozenset(w for w in text.split() if w.isascii() and w.isalpha() and w.islower())


def _stems(w: str) -> list[str]:
    out = [w]
    for suf, rep in (("ies", "y"), ("ied", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("d", ""),
                     ("ing", ""), ("ing", "e"), ("er", ""), ("er", "e"), ("est", ""), ("ly", ""), ("ment", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: len(w) - len(suf)] + rep
            out.append(base)
            if len(base) > 3 and base[-1] == base[-2] and not rep:  # planned -> plan
                out.append(base[:-1])
    return out


def _words(text: str) -> list[str]:
    return [re.sub(r"['’][sS]$", "", w) for w in _WORD.findall(text)]


# -- the check ----------------------------------------------------------------------------------------------------

def _plain_shape(w: str) -> bool:
    """lowercase, Capitalized or ALL CAPS letters only: not OpenAI, iPhone, GPT5 (R49: product-like)."""
    return w.isalpha() and (w.islower() or w.isupper() or (w[0].isupper() and w[1:].islower()))


def _word_ok(w: str, source_words: set[str], common: frozenset[str], dictionary: frozenset[str] | None,
             calendar: frozenset = frozenset()) -> bool:
    """R49. A word passes if it is in the insight (any case), or on our allowlist; otherwise only a plain-shaped
    word passes, when it (or a simple inflection) is on the common list or a lowercase dictionary entry and is not
    a known brand. Case is meaningful again because the sanitizer strips text-transform (R48). I1: a typeset month
    or weekday name ("Dec", "MARCH", "Friday") passes only if that month or weekday is in `calendar`, the insight's
    dates and text plus today's; a lowercase "may" or "march" is an ordinary word."""
    low = w.lower()
    if (low in MONTHS or low in WEEKDAY_OF) and not w.islower():
        return (("m", MONTHS[low]) if low in MONTHS else ("w", WEEKDAY_OF[low])) in calendar
    if sum(c.isalpha() for c in low) < 2 or low in ALLOW:
        return True
    stems = _stems(low)
    if any(s in source_words for s in stems):
        return True
    if not _plain_shape(w) or any(s in BRAND_WORDS for s in stems):
        return False
    return any(s in common or (dictionary is not None and s in dictionary) for s in stems)


def _repair_spaces(text: str, src_numbers: set[str], source_words: set[str], common: frozenset[str],
                   dictionary: frozenset[str] | None, calendar: frozenset = frozenset()) -> str:
    """The PDF reading only. pypdf can read one of Chrome's per-glyph runs with a space that the slide doesn't show
    (live run 1: "$1.99" as "$1.9 9", with consistent font widths). Two fragments separated by exactly one space are
    joined only when the joined token passes and a fragment alone does not, so a repair can only turn a misread
    allowed token back into itself, never let a new fact through."""
    def ok(tok: str) -> bool:
        core = tok.strip(".,;:!?()[]{}\"'")
        if not core:
            return False
        nums = number_values(core)
        words = _words(core)
        return bool(nums or words) and all(v in src_numbers for v in nums) and all(
            _word_ok(w, source_words, common, dictionary, calendar) for w in words)

    out = []
    for line in text.split("\n"):
        parts = re.split(r"( +)", line)
        i = 0
        while i + 2 < len(parts):
            a, gap, b = parts[i], parts[i + 1], parts[i + 2]
            if gap == " " and a and b and (a[-1].isalnum() and b[0].isalnum()) and ok(a + b) and not (ok(a) and ok(b)):
                parts[i:i + 3] = [a + b]
                continue
            i += 2
        out.append("".join(parts))
    return "\n".join(out)


_FRAGMENT_RUN = re.compile(r"[$€£]?\d[\d,.]*%?|[$€£]")


def _glued_brands(rest: str, source_words: set[str]) -> dict[str, str]:
    """M1/C1(d): two words with only whitespace (a space, a line, a block boundary) between them, read together:
    "Deep Seek", "Open AI", or "Deep" and "Seek" in two flex items, which Chrome draws as "DeepSeek"."""
    out = {}
    words = list(_WORD.finditer(rest))
    for m1, m2 in zip(words, words[1:]):
        if rest[m1.end():m2.start()].strip():
            continue
        joined = m1.group(0) + m2.group(0)
        stems = _stems(joined.lower())
        if any(s in BRAND_WORDS for s in stems) and not any(s in source_words for s in stems):
            out.setdefault(joined.lower(), joined)
    return out


def _glued_numbers(rest: str, src_numbers: set[str]) -> dict[str, str]:
    """C1(d), the HTML reading only: consecutive text runs that are each just a number fragment ("$", "4", "20" in
    three flex items) are read together, as Chrome draws them ("$420")."""
    out = {}
    runs = [line.strip() for line in rest.split("\n") if line.strip()]
    chain: list[str] = []
    for run in [*runs, ""]:
        if run and _FRAGMENT_RUN.fullmatch(run):
            chain.append(run)
            continue
        if len(chain) > 1:
            glued = "".join(chain)
            for tok, v in number_tokens(glued):
                if v not in src_numbers:
                    out.setdefault(v, glued)
        chain = []
    return out


def check_text(text: str, ins: Insight | Idea, day: date, *, common_words: frozenset[str] | None = None,
               repair: bool = False, glue: bool = False) -> FactCheck:
    """The check both readings go through. `repair` (the PDF reading only): see _repair_spaces. `glue` (the HTML
    reading only): see _glued_numbers."""
    problems: Counter = Counter()
    refused: list[str] = []
    tags: list[tuple[str, int, int]] = []  # (kind, start, end) slices of `refused`

    def tag(kind: str, start: int) -> None:
        tags.append((kind, start, len(refused)))

    text = strip_ignorable(text)  # C2: before tokenizing, in both readings
    looks = [c for c in text if c in lookalikes()]
    problems["lookalike"] += len(looks)
    refused += [f"U+{ord(c):04X}" for c in looks]
    tag("lookalike", 0)
    text = unicodedata.normalize("NFKC", text)
    # The brief's own words for Haseeb ("Haseeb Zafar's team at MUST"): the exact two-word phrase only, read as
    # "Haseeb"; "Zafar" on its own, or with anyone else, is still a name.
    text = _OWNER.sub("Haseeb", text)
    facts, extra = (unicodedata.normalize("NFKC", t) for t in source_parts(ins, day))
    src = facts + "\n" + extra
    common = default_common_words() if common_words is None else common_words - BRAND_WORDS
    source_words = {s for w in _words(src) for s in _stems(w.lower())}
    dictionary = dictionary_words()
    src_numbers, src_dates = _source_numbers(facts), _source_dates(src)  # I1: today's date gives no numbers
    src_low = {w.lower() for w in _words(src)}
    calendar = frozenset({("m", MONTHS[w]) for w in src_low if w in MONTHS} | {("m", mo) for _, mo, _ in src_dates}
                         | {("w", WEEKDAY_OF[w]) for w in src_low if w in WEEKDAY_OF})
    if repair:
        text = _repair_spaces(text, src_numbers, source_words, common, dictionary, calendar)
    src_letters = {c for c in src if ord(c) > 127}
    letters = {c for c in text if ord(c) > 127 and unicodedata.category(c).startswith("L") and c not in src_letters}
    problems["unicode"] += len(letters)
    at = len(refused)
    refused += sorted(letters)
    tag("unicode", at)
    hosts = evidence_hosts(ins)

    # banned content first, so a URL's parts are not also counted as numbers and words
    low = text.lower()
    schemes = re.findall(r"://|\bwww\.", low)
    problems["url"] += len(schemes)
    at = len(refused)
    refused += schemes
    spans = []
    for m in _DOMAIN.finditer(text):
        if repair and _sentence_boundary(m.group(0)):
            continue  # "approximate.Input": a sentence boundary (live run 5: two layout columns side by side)
        spans.append(m.span())
        if m.group(0).lower().removeprefix("www.") not in hosts:
            problems["url"] += 1
            refused.append(m.group(0))
    tag("url", at)
    problems["mention"] += text.count("@")
    at = len(refused)
    refused += ["@"] * min(1, text.count("@"))
    tag("mention", at)
    controls = re.findall(r"<[!@#]", text)
    problems["control"] += len(controls)
    at = len(refused)
    refused += controls
    tag("control", at)
    text = _mask(text, spans)

    found = _dates(text)
    at = len(refused)
    for d, (a, b) in found:
        if not _date_ok(d, src_dates, src_numbers):
            problems["date"] += 1
            refused.append(" ".join(text[a:b].split()))
    tag("date", at)
    rest = _mask(text, [s for _, s in found])
    bad_numbers = {}
    for tok, v in number_tokens(rest):
        if v not in src_numbers:
            bad_numbers.setdefault(v, tok.strip())
    if glue:
        for v, tok in _glued_numbers(rest, src_numbers).items():
            bad_numbers.setdefault(v, tok)
    problems["number"] += len(bad_numbers)
    at = len(refused)
    refused += bad_numbers.values()
    tag("number", at)

    bad_words = {}
    for w in _words(rest):
        if not _word_ok(w, source_words, common, dictionary, calendar):
            bad_words.setdefault(w.lower(), w)
    for k, joined in _glued_brands(rest, source_words).items():
        bad_words.setdefault(k, joined)
    problems["name"] += len(bad_words)
    at = len(refused)
    refused += bad_words.values()
    tag("name", at)

    problems = Counter({k: v for k, v in problems.items() if v})
    seen = list(dict.fromkeys(r[:60] for r in refused if r))[:MAX_REFUSED]
    kinds = list(dict.fromkeys((k, r[:60]) for k, a, b in tags for r in refused[a:b] if r))[:MAX_REFUSED]
    return FactCheck(ok=not problems, problems=problems, refused=tuple(seen), kinds=tuple(kinds))


def fact_check(page: str, ins: Insight | Idea, day: date, *,
               common_words: frozenset[str] | None = None) -> FactCheck:
    """The HTML-source layer: the sanitized page's visible text."""
    return check_text(visible_text(page), ins, day, common_words=common_words, glue=True)


def fact_check_pdf(pdf: Path, ins: Insight | Idea, day: date, *, common_words: frozenset[str] | None = None,
                   reader=pdf_text) -> FactCheck:
    """The authoritative layer (R47): the rendered PDF's own text."""
    return check_text(reader(Path(pdf)), ins, day, common_words=common_words, repair=True)
