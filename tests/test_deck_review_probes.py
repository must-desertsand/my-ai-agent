"""The designer-deck review's proofs (scratchpad/probe/probe2.py, shot.py), as regressions: CSS that hid text from
pypdf's PDF reading. Each must be refused, by the sanitizer (R50a) and, independently, by the PDF check or the
coverage gate (C1a) when the CSS reaches Chrome unsanitized."""
import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest

from haseeb_agent.deck.factcheck import coverage, fact_check, fact_check_pdf, pdf_text
from haseeb_agent.deck.render import CHROME, print_pdf
from haseeb_agent.deck.sanitize import CSP, sanitize_html
from tests.test_deck_designer import argon_live

pytestmark = pytest.mark.skipif(not Path(CHROME).exists(), reason="Google Chrome is not installed")
DAY = date(2026, 10, 17)
HIDERS = ["opacity:.95", "filter:blur(0.1px)", "mix-blend-mode:multiply", "transform:rotate(180deg)",
          "transform:scaleX(-1)", "mask-image:linear-gradient(#000,#000)", "writing-mode:vertical-rl",
          "direction:rtl;unicode-bidi:bidi-override", "clip-path:inset(0)", "backdrop-filter:blur(1px)"]


def flex(css: str) -> str:
    return (f'<div style="position:absolute;top:200px;left:200px;display:flex;font-size:120px;{css}"><div>Deep</div>'
            '<div>Seek</div><div>$</div><div>4</div><div>20</div></div><p>Argon cost per task</p>')


def page(body: str) -> str:
    return ('<!doctype html><html><body><section style="width:1280px;height:720px;position:relative;'
            f'overflow:hidden">{body}</section></body></html>')


def render(html: str) -> str:
    work = Path(tempfile.mkdtemp(prefix="probe-"))
    try:
        (work / "p.html").write_text(html)
        return pdf_text(print_pdf(work / "p.html", work / "p.pdf"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.parametrize("css", HIDERS)
def test_the_sanitized_deck_is_refused_by_both_readings(css):
    safe, removed = sanitize_html(page(flex(css)), Path(tempfile.mkdtemp()))
    assert removed["css-blind"] >= 1
    assert not fact_check(safe, argon_live(), DAY).ok
    text = render(safe)
    assert "DeepSeek" in " ".join(text.split()).replace(" ", "")  # the PDF reading sees it again
    assert not fact_check_pdf(Path("x"), argon_live(), DAY, reader=lambda p: text).ok


@pytest.mark.parametrize("css", HIDERS)
def test_unsanitized_css_is_still_caught_by_the_pdf_check_or_the_coverage_gate(css):
    """Defense in depth: the CSS reaches Chrome as is (only our CSP head). Either pypdf sees the text and the PDF
    check refuses it, or it doesn't and the coverage gate refuses the deck."""
    head = f'<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="{CSP}"></head>'
    raw = head + page(flex(css)).split("<html>", 1)[1]
    text = render(raw)
    pdf_ok = fact_check_pdf(Path("x"), argon_live(), DAY, reader=lambda p: text).ok
    gate_ok = coverage(raw, text).ok
    assert not (pdf_ok and gate_ok), (css, text)


def test_the_reviewers_bypass_slide_is_refused():
    body = ('<section style="width:1280px;height:720px;background:#7c3aed;color:#fff;padding:60px;box-sizing:border-box">'
            '<h1 style="font-size:80px;margin:0">Open&#x200b;AI costs $4&#xad;20</h1>'
            '<div style="display:flex;font-size:80px;opacity:.95;margin-top:40px"><div>Deep</div><div>Seek</div>'
            '<div>&nbsp;$</div><div>4</div><div>20</div></div>'
            '<div style="display:flex;font-size:60px;margin-top:40px"><div style="display:flex;flex-direction:column">'
            '<div>DEC</div><div style="font-size:120px">30</div></div><div>Argon price rises in March</div></div>'
            '</section>')
    safe, _ = sanitize_html(f"<!doctype html><html><body style='margin:0'>{body}</body></html>", Path(tempfile.mkdtemp()))
    h = fact_check(safe, argon_live(), DAY)
    text = render(safe)
    p = fact_check_pdf(Path("x"), argon_live(), DAY, reader=lambda _p: text)
    assert not h.ok and {"OpenAI", "DeepSeek", "420", "DEC 30", "March"} <= set(h.refused), h.refused
    # pypdf lays the calendar tile out beside the text ("DECArgon ... March" over "30"): refused all the same
    assert not p.ok and {"OpenAI", "DeepSeek", "420"} <= set(p.refused) and p.problems["date"] >= 1, p.refused


# The re-review's p5.py (N1): an SVG <pattern> tile draws "$4" "20" over "Open" "AI"; pypdf does not see a pattern's
# text, and the old coverage gate let "$4", "20", "Open" and "AI" borrow from the visible sentence below.
P5_VISIBLE = ('<p style="position:absolute;top:600px;font-size:22px">Open the AI tab. Intro price $2 and $10; on Nov 30 '
              'it rises to $4 and $20. Next: Index.</p>')
P5 = ('<svg width="1280" height="500"><defs><pattern id="p" width="1280" height="500" patternUnits="userSpaceOnUse">'
      '<text x="20" y="180" font-size="140" font-family="Helvetica">$4</text><text x="-900" y="200" font-size="10">a</text>'
      '<text x="176" y="180" font-size="140" font-family="Helvetica">20</text>'
      '<text x="20" y="400" font-size="140" font-family="Helvetica">Open</text><text x="-900" y="200" font-size="10">a</text>'
      '<text x="345" y="400" font-size="140" font-family="Helvetica">AI</text></pattern></defs>'
      '<rect width="1280" height="500" fill="url(#p)"/></svg>' + P5_VISIBLE)


def test_the_pattern_probe_is_sanitized_to_its_visible_text():
    safe, removed = sanitize_html(page(P5), Path(tempfile.mkdtemp()))
    assert removed["tag:pattern"] == 1 and "<text" not in safe and "<pattern" not in safe
    text = render(safe)
    assert coverage(safe, text).ok and " ".join(text.split()) == " ".join(
        "Open the AI tab. Intro price $2 and $10; on Nov 30 it rises to $4 and $20. Next: Index.".split())


def test_the_unsanitized_pattern_probe_fails_the_counting_coverage_gate():
    """N1(b), independent of the sanitizer: every tile token needs its own occurrence in the PDF reading."""
    head = f'<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="{CSP}"></head>'
    raw = head + page(P5).split("<html>", 1)[1]
    text = render(raw)
    assert fact_check_pdf(Path("x"), argon_live(), DAY, reader=lambda p: text).ok  # pypdf is blind to the tile
    r = coverage(raw, text)
    assert not r.ok and {"$4", "20", "Open", "AI"} <= set(r.refused), (r, r.refused)


def test_a_text_shadow_is_stripped_and_reads_once():
    """R50b (the re-review's p6.py): Chrome drew "Swani" with a text-shadow as two texts in the PDF."""
    safe, removed = sanitize_html(page('<div style="font-size:90px;text-shadow:6px 6px 0 red">Swani</div>'),
                                  Path(tempfile.mkdtemp()))
    assert removed["css-blind"] == 1 and "text-shadow" not in safe
    assert render(safe).split() == ["Swani"]
