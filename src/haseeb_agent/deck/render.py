"""Task 14: one insight or idea as a 16:9 PDF deck, in the bold-gradient style Haseeb approved.

`build_html` fills `template.html` for an insight (6 slides: cover, what happened, why it matters, Silicon Valley move,
evidence, not verified / next step) and `template_idea.html` for an idea (8 slides: the cover, then one slide for each
section of the idea). `build_deck` writes the HTML next to an optional cover image and prints it to PDF with
headless Chrome, in a throwaway profile.

Safety: the deck text comes only from the draft's fields. Each field is cleaned the way Slack text is
(render._clean_field: no format characters, no markdown links, no lines that imitate our furniture; an idea's fields
are also kept from imitating the idea's headings), capped, and HTML-escaped; the template is filled in one pass, so a
field can never expand another placeholder. Nothing in the deck is a link (evidence shows a title and a host), and the
page's CSP forbids every fetch but local files, so the render reaches no network even if escaping ever failed. The cover
image name is chosen here, never taken from data.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from ..render import MARKET_LABELS, _IDEA_SECTION_NAMES, _SECTION_NAMES, Idea, Insight, _clean_field
from .proc import run_group

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_TIMEOUT_S = 90
TEMPLATE = Path(__file__).with_name("template.html")
TEMPLATE_IDEA = Path(__file__).with_name("template_idea.html")

MAX_TITLE = 160
MAX_PRICE = 40         # the writer keeps a price to 30 characters
MAX_BODY = 1500        # about 250 words: still fits a panel at the 14px floor
MAX_ITEM = 180
MAX_EVIDENCE = 5
MAX_UNVERIFIED = 6
MAX_DECK_RISKS = 4     # an idea's risks: the writer is asked for 2-4, and 4 + 6 lines is the most its last slide fits

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_SLOT = re.compile(r"\{\{(\w+)\}\}")
_COVER_NAME = re.compile(r"cover\.(?:png|jpe?g)")


class DeckError(Exception):
    """The deck could not be built (Chrome failed, timed out, or wrote no PDF)."""


def kicker(day: date, label: str = "DAILY INSIGHT") -> str:
    return f"{label} · {day.day:02d} {_MONTHS[day.month - 1]} {day.year}"


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _text(value: str, n: int, sections: frozenset[str] = _SECTION_NAMES) -> str:
    """A cleaned, capped, escaped multi-line field. `sections`: the headings a line may not imitate."""
    return html.escape(_cap(_clean_field(value or "", sections), n), quote=True)


def _line(value: str, n: int, sections: frozenset[str] = _SECTION_NAMES) -> str:
    return html.escape(_cap(" ".join(_clean_field(value or "", sections).split()), n), quote=True)


def _host(url: str) -> str:
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    return host if re.fullmatch(r"[a-z0-9.-]+", host) else ""


def _evidence(draft: Insight | Idea, sections: frozenset[str] = _SECTION_NAMES) -> str:
    items = []
    for src in draft.evidence[:MAX_EVIDENCE]:
        host = _host(src.url)
        if not host:
            continue  # not plain http(s): no line at all
        title = _line(src.title, MAX_ITEM, sections) or "Source"
        items.append(f'<li>{title}<span class="src-host">{html.escape(host)}</span></li>')
    return "".join(items) or "<li>No sources listed.</li>"


def _lines(items: list[str], limit: int, sections: frozenset[str] = _SECTION_NAMES) -> list[str]:
    """The items that are not empty once cleaned, each as one escaped line, at most `limit` of them."""
    lines = [_line(x, MAX_ITEM, sections) for x in items]
    return [x for x in lines if x][:limit]


def _unverified(ins: Insight) -> str:
    lines = _lines(ins.not_verified, MAX_UNVERIFIED)
    return "".join(f"<li>{x}</li>" for x in lines) or "<li>Nothing flagged: every claim is backed by the sources.</li>"


def _tagged(tag: str, lines: list[str]) -> str:
    return "".join(f'<li><span class="tag">{tag}</span>{x}</li>' for x in lines)


def _insight_values(ins: Insight, day: date) -> dict[str, str]:
    title = _line(ins.title, MAX_TITLE) or "Daily insight"
    return {
        "doc_title": title,
        "kicker": html.escape(kicker(day)),
        "category": html.escape(ins.category if ins.category in ("AI", "MONEY", "PRODUCT", "GAP") else "INSIGHT"),
        "title": title,
        "short_title": _line(ins.title, 90),
        "what_happened": _text(ins.what_happened, MAX_BODY),
        "why_it_matters": _text(ins.why_it_matters, MAX_BODY),
        "silicon_valley": _text(ins.silicon_valley, MAX_BODY),
        "evidence": _evidence(ins),
        "not_verified": _unverified(ins),
    }


def _idea_values(idea: Idea, day: date) -> dict[str, str]:
    """The slots of template_idea.html. Every field is cleaned against the idea's headings, not just the insight's."""
    s = _IDEA_SECTION_NAMES
    title = _line(idea.title, MAX_TITLE, s) or "Daily idea"
    return {
        "doc_title": title,
        "kicker": html.escape(kicker(day, "DAILY IDEA")),
        "market": html.escape(MARKET_LABELS.get(idea.market, "IDEA")),
        "price": _line(idea.price, MAX_PRICE, s),
        "title": title,
        "short_title": _line(idea.title, 90, s),
        "idea": _text(idea.idea, MAX_BODY, s),
        "why_now": _text(idea.why_now, MAX_BODY, s),
        "who_pays": _text(idea.who_pays, MAX_BODY, s),
        "build_with_ai": _text(idea.build_with_ai, MAX_BODY, s),
        "first_step": _text(idea.first_step, MAX_BODY, s),
        "evidence": _evidence(idea, s),
        "risks": _tagged("Risk", _lines(idea.risks, MAX_DECK_RISKS, s)),
        "not_verified": _tagged("Not verified", _lines(idea.not_verified, MAX_UNVERIFIED, s) or ["Nothing flagged."]),
    }


def build_html(draft: Insight | Idea, day: date, cover_name: str | None = None) -> str:
    if cover_name is not None and not _COVER_NAME.fullmatch(cover_name):
        raise ValueError(f"bad cover name {cover_name!r}")
    if isinstance(draft, Idea):
        template, values = TEMPLATE_IDEA, _idea_values(draft, day)
    else:
        template, values = TEMPLATE, _insight_values(draft, day)
    values["read_min"] = str(int(draft.read_min)) if isinstance(draft.read_min, int) else "3"
    values["cover_class"] = "photo" if cover_name else "template"
    values["cover_name"] = cover_name or ""
    return _SLOT.sub(lambda m: values[m.group(1)], template.read_text(encoding="utf-8"))  # one pass: never rescanned


def build_deck(ins: Insight | Idea, day: date, out_dir: Path, *, cover: Path | None = None, chrome: str = CHROME,
               runner=run_group, timeout_s: int = CHROME_TIMEOUT_S) -> Path:
    """Write out_dir/deck.html (plus cover.<ext> when a cover is given) and print it to out_dir/deck.pdf."""
    out_dir = Path(out_dir).resolve()  # the production root is relative (data/decks); a file:// URI needs absolute
    out_dir.mkdir(parents=True, exist_ok=True)
    cover_name = None
    if cover is not None:
        ext = Path(cover).suffix.lower()
        cover_name = "cover" + (ext if ext in (".png", ".jpg", ".jpeg") else ".png")
        shutil.copyfile(cover, out_dir / cover_name)
    page = out_dir / "deck.html"
    page.write_text(build_html(ins, day, cover_name), encoding="utf-8")
    return print_pdf(page, out_dir / "deck.pdf", chrome=chrome, runner=runner, timeout_s=timeout_s)


def print_pdf(page: Path, pdf: Path, *, chrome: str = CHROME, runner=run_group,
              timeout_s: int = CHROME_TIMEOUT_S) -> Path:
    """Print the HTML file `page` to `pdf` with headless Chrome in a throwaway profile. DeckError on any failure."""
    page, pdf = Path(page).resolve(), Path(pdf).resolve()
    pdf.unlink(missing_ok=True)
    profile = tempfile.mkdtemp(prefix="haseeb-deck-chrome-")
    argv = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking", "--disable-sync", "--hide-scrollbars",
            f"--user-data-dir={profile}", "--no-pdf-header-footer", f"--print-to-pdf={pdf}", page.as_uri()]
    try:
        r = runner(argv, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise DeckError(f"chrome timed out after {timeout_s}s") from e
    except OSError as e:
        raise DeckError(f"could not run chrome: {type(e).__name__}") from e
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if r.returncode != 0:
        raise DeckError(f"chrome exit {r.returncode}")
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise DeckError("chrome wrote no PDF")
    return pdf
