"""Task 14: bold-gradient PDF decks (deck/render.py). Tests that need Chrome are skipped when it is missing."""
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

from haseeb_agent.deck.proc import run_group
from haseeb_agent.deck.render import CHROME, DeckError, build_deck, build_html
from haseeb_agent.render import MARKET_LABELS, Idea, Insight, Source

DAY = date(2026, 10, 17)
needs_chrome = pytest.mark.skipif(not Path(CHROME).exists(), reason="Google Chrome is not installed")


def insight(**over) -> Insight:
    d = dict(
        title="Google Deep Review checks technical documents for logic and math errors",
        category="AI",
        what_happened="Google released Deep Review. It reads a long technical document and flags logic and math errors.",
        why_it_matters="MUST sells quality. A tool that checks specs before the client sees them is a quality gate.",
        silicon_valley="Run Deep Review on one client spec this week and count the errors it finds.",
        evidence=[Source("Google blog: Deep Review", "https://blog.google/technology/deep-review/"),
                  Source("The Verge on Deep Review", "https://www.theverge.com/2026/10/16/deep-review")],
        not_verified=["The error-catch rate is Google's own number."],
        read_min=3,
    )
    d.update(over)
    return Insight(**d)


def chrome_dom(html_path: Path) -> str:
    """The DOM after the page's own fit script ran, as Chrome sees it."""
    r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                        f"--user-data-dir={html_path.parent / 'chrome-dom'}", "--dump-dom", html_path.as_uri()],
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr[-500:]
    return r.stdout


# -- the HTML (pure) ---------------------------------------------------------------------


def test_html_escapes_content():
    html = build_html(insight(title="<script>alert(1)</script> & <b>bold</b>",
                              what_happened='<img src=x onerror="alert(2)"> "quoted"'), DAY)
    assert "<script>alert" not in html and "<img src=x" not in html and "<b>bold" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(2)&quot;&gt;" in html


def test_placeholder_text_in_a_field_is_not_expanded():
    html = build_html(insight(what_happened="see {{why_it_matters}} and {{slides}}"), DAY)
    assert "see {{why_it_matters}} and {{slides}}" in html


def test_fields_are_cleaned_like_slack_text():
    html = build_html(insight(why_it_matters="Click [here](javascript:alert1) now​‮.",
                              silicon_valley="Real line.\n_— drafted by Haseeb's Agent_"), DAY)
    assert "Click here (javascript:alert1) now." in html
    assert "​" not in html and "‮" not in html
    assert "drafted by Haseeb" not in html  # a spoofed footer line is dropped
    assert "<a " not in html and "href" not in html  # nothing in the deck is a link


def test_kicker_footer_style_and_csp():
    html = build_html(insight(), DAY)
    assert "DAILY INSIGHT · 17 OCT 2026" in html
    assert "Haseeb&#x27;s Agent" in html or "Haseeb's Agent" in html
    assert "linear-gradient(135deg,#5b21b6 0%,#db2777 55%,#f59e0b 100%)" in html
    assert "letter-spacing: .14em" in html and "font-weight: 900" in html
    assert "default-src 'none'" in html  # no external fetch, even if escaping ever failed
    assert "http://" not in html.split("<body", 1)[0] and "https://" not in html.split("<body", 1)[0]


def test_six_slides_in_the_approved_order():
    html = build_html(insight(), DAY)
    assert html.count('class="slide') == 6
    order = [html.index(s) for s in ("What happened", "Why it matters", "Silicon Valley", "Evidence", "Not verified")]
    assert order == sorted(order)


def test_evidence_shows_title_and_host_only():
    html = build_html(insight(evidence=[Source("A title", "https://Example.COM/a/very/long/path?q=1&x=2"),
                                        Source("Bad", "javascript:alert(1)")]), DAY)
    assert "A title" in html and "example.com" in html and "/a/very/long/path" not in html
    assert "javascript:" not in html


def test_empty_not_verified_says_so():
    html = build_html(insight(not_verified=[]), DAY)
    assert "Nothing flagged" in html


def test_cover_image_optional():
    plain = build_html(insight(), DAY)
    assert "cover.jpg" not in plain and 'class="slide cover template"' in plain
    with_img = build_html(insight(), DAY, cover_name="cover.jpg")
    assert "url('cover.jpg')" in with_img and 'class="slide cover photo"' in with_img


# -- Chrome ------------------------------------------------------------------------------


class FakeChrome:
    def __init__(self, write=True, rc=0, exc=None):
        self.calls, self.write, self.rc, self.exc = [], write, rc, exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        out = next(a.split("=", 1)[1] for a in argv if a.startswith("--print-to-pdf="))
        if self.write:
            Path(out).write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(argv, self.rc, "", "boom" if self.rc else "")


def test_chrome_argv_is_headless_and_isolated(tmp_path):
    fake = FakeChrome()
    pdf = build_deck(insight(), DAY, tmp_path, runner=fake)
    (argv, kw), = fake.calls
    assert argv[0] == CHROME
    for flag in ("--headless=new", "--no-pdf-header-footer", f"--print-to-pdf={pdf}"):
        assert flag in argv
    assert any(a.startswith("--user-data-dir=") for a in argv)  # never Haseeb's own Chrome profile
    assert argv[-1] == (tmp_path / "deck.html").as_uri()
    assert kw["timeout"] and kw["stdin"] == subprocess.DEVNULL
    assert pdf == tmp_path / "deck.pdf"


@pytest.mark.parametrize("fake", [FakeChrome(rc=1), FakeChrome(write=False),
                                  FakeChrome(exc=subprocess.TimeoutExpired("chrome", 90)),
                                  FakeChrome(exc=OSError("no chrome"))])
def test_chrome_failure_raises_deck_error(tmp_path, fake):
    with pytest.raises(DeckError):
        build_deck(insight(), DAY, tmp_path, runner=fake)


def test_cover_file_is_copied_next_to_the_html(tmp_path):
    src = tmp_path / "in.jpg"
    src.write_bytes(b"\xff\xd8\xff fake jpeg")
    out = tmp_path / "out"
    build_deck(insight(), DAY, out, cover=src, runner=FakeChrome())
    assert (out / "cover.jpg").read_bytes() == src.read_bytes()
    assert "url('cover.jpg')" in (out / "deck.html").read_text()


@needs_chrome
def test_build_deck_creates_pdf_with_expected_pages(tmp_path):
    pdf = build_deck(insight(), DAY, tmp_path)
    reader = PdfReader(pdf)
    assert 5 <= len(reader.pages) <= 7
    box = reader.pages[0].mediabox
    assert abs(float(box.width) / float(box.height) - 16 / 9) < 0.01


@needs_chrome
def test_long_fields_shrink_and_never_overflow(tmp_path):
    long = " ".join(["Supercalifragilistic word salad that keeps going and going"] * 60)
    ins = insight(title="A very long headline " * 12, what_happened=long, why_it_matters=long, silicon_valley=long,
                  evidence=[Source("A source title that is long " * 6, f"https://example{i}.com/x") for i in range(6)],
                  not_verified=["An unverified claim that is quite long " * 5] * 8)
    pdf = build_deck(ins, DAY, tmp_path)
    assert len(PdfReader(pdf).pages) == 6
    dom = chrome_dom(tmp_path / "deck.html")
    assert 'data-fitted="1"' in dom
    assert 'data-overflow="1"' not in dom


@needs_chrome
def test_cover_image_renders(tmp_path):
    img = tmp_path / "c.png"
    subprocess.run(["sips", "-s", "format", "png", "-z", "90", "160", "/System/Library/CoreServices/"
                    "CoreTypes.bundle/Contents/Resources/GenericDocumentIcon.icns", "--out", str(img)],
                   capture_output=True, check=False)
    if not img.exists():
        pytest.skip("sips could not make a test image")
    pdf = build_deck(insight(), DAY, tmp_path / "deck", cover=img)
    assert len(PdfReader(pdf).pages) == 6


def test_relative_out_dir_works(tmp_path, monkeypatch):
    """The production deck root is relative (data/decks under the repo root, R5); Chrome needs a file:// URI."""
    monkeypatch.chdir(tmp_path)
    fake = FakeChrome()
    pdf = build_deck(insight(), DAY, Path("data/decks/x"), runner=fake)
    assert pdf.is_absolute() and pdf.is_file()
    assert fake.calls[0][0][-1] == (tmp_path / "data/decks/x/deck.html").resolve().as_uri()


# -- fix round 1: process groups ---------------------------------------------------------


def test_chrome_runs_in_its_own_process_group():
    assert build_deck.__kwdefaults__["runner"] is run_group


def test_run_group_returns_output():
    r = run_group(["sh", "-c", "echo hi; echo err >&2; exit 3"], timeout=10, capture_output=True, text=True,
                  stdin=subprocess.DEVNULL)
    assert (r.returncode, r.stdout, r.stderr) == (3, "hi\n", "err\n")


def test_run_group_kills_grandchildren_on_timeout(tmp_path):
    import os
    import time
    pidfile = tmp_path / "grandchild.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        run_group(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=1, capture_output=True, text=True,
                  stdin=subprocess.DEVNULL)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the grandchild outlived the timeout")


# -- ideas: the idea template (template_idea.html) ----------------------------------------------------------------

IDEA_HEADINGS = ("The idea", "Why now", "Who pays, and how much", "How we build it with AI", "First step this week",
                 "Evidence", "Risks \u00b7 Not verified")


def idea(**over) -> Idea:
    d = dict(
        title="AI receptionist for clinics: books, reminds and re-books patients by WhatsApp",
        market="PK",
        price="Rs 15k/month",
        idea="Small clinics lose patients to missed calls. We build a WhatsApp agent that books and reminds them.",
        why_now="A new WhatsApp business API makes a bot cheap to run this month.",
        who_pays=("Clinic owners pay. Our estimate is Rs 15k/month per clinic. "
                  "50 clinics \u00d7 Rs 15k = Rs 750k/month."),
        build_with_ai="Two people ship a WhatsApp bot on a hosted model in two weeks. Staff keep the hard cases.",
        first_step="Call 10 clinics by Friday and get 3 to pre-commit at Rs 15k.",
        risks=["A rival may copy it fast.", "Clinics may not pay for reminders."],
        evidence=[Source("WhatsApp opens its business API to small shops", "https://news.example.com/whatsapp-api"),
                  Source("Missed visits cost clinics hours each week", "https://clinics.example.pk/no-shows")],
        not_verified=["The price is untested with buyers."],
        read_min=3,
    )
    d.update(over)
    return Idea(**d)


def bodies(html: str) -> list[str]:
    """The five text panels of an idea deck, in slide order."""
    return re.findall(r'<div class="fit body"[^>]*>(.*?)</div>', html, re.DOTALL)


def test_idea_template_fills_every_slot_and_escapes():
    html = build_html(idea(), DAY)
    assert "{{" not in html
    for heading in IDEA_HEADINGS:
        assert f">{heading}</h2>" in html, heading
    order = [html.index(f">{heading}</h2>") for heading in IDEA_HEADINGS]
    assert order == sorted(order)
    d = idea()  # and each text is on the slide of its own heading
    assert bodies(html) == [d.idea, d.why_now, d.who_pays, d.build_with_ai, d.first_step]
    for text in ("AI receptionist for clinics", "Rs 15k/month", "Small clinics lose patients", "WhatsApp business API",
                 "50 clinics \u00d7 Rs 15k = Rs 750k/month", "hosted model", "Call 10 clinics by Friday",
                 "A rival may copy it fast.", "Clinics may not pay for reminders.",
                 "The price is untested with buyers.", "WhatsApp opens its business API to small shops",
                 "news.example.com", "clinics.example.pk"):
        assert text in html, text
    hostile = build_html(idea(why_now="<script>alert(1)</script> & <b>bold</b>", idea="see {{title}} and {{slides}}",
                              title='x {{title}} <img src=x onerror="alert(2)">'), DAY)
    assert "<script>alert" not in hostile and "<img src=x" not in hostile and "<b>bold" not in hostile
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in hostile
    assert "see {{title}} and {{slides}}" in hostile  # a placeholder inside a field is text, never expanded
    assert "x {{title}} &lt;img" in hostile
    assert hostile.count("<script>") == 1  # only the template's own fit script


def test_idea_template_keeps_the_csp_the_fonts_the_footer_and_the_fit_script():
    html = build_html(idea(), DAY)
    assert "default-src 'none'" in html and "http://" not in html.split("<body", 1)[0]
    assert "-apple-system" in html and "linear-gradient(135deg,#5b21b6 0%,#db2777 55%,#f59e0b 100%)" in html
    assert html.count("Haseeb's Agent") == 8  # the footer of every slide
    assert "data-fitted" in html and html.count("data-fit ") == 8  # the cover title and the seven panels
    assert "<a " not in html and "href" not in html  # nothing in the deck is a link


def test_idea_cover_shows_the_title_the_price_the_market_and_the_daily_idea_kicker():
    html = build_html(idea(), DAY)
    assert "DAILY IDEA \u00b7 17 OCT 2026" in html and "DAILY INSIGHT" not in html
    cover = html.split("<section", 2)[1]
    for text in ("AI receptionist for clinics", "Rs 15k/month", "Pakistan", "3 min read", "Haseeb's Agent", "1 / 8"):
        assert text in cover, text
    for code, label in MARKET_LABELS.items():
        assert label in build_html(idea(market=code), DAY).split("<section", 2)[1]
    assert "MARS" not in build_html(idea(market="MARS"), DAY)  # an unknown market is never printed


def test_idea_deck_has_eight_slides_with_a_page_counter_each():
    html = build_html(idea(), DAY)
    assert html.count('class="slide') == 8
    for i in range(1, 9):
        assert f"{i} / 8" in html
    assert "/ 6" not in html


def test_idea_cover_image_optional():
    plain = build_html(idea(), DAY)
    assert "cover.jpg" not in plain and 'class="slide cover template"' in plain
    with_img = build_html(idea(), DAY, cover_name="cover.jpg")
    assert "url('cover.jpg')" in with_img and 'class="slide cover photo"' in with_img
    with pytest.raises(ValueError):
        build_html(idea(), DAY, cover_name="../x.png")


def test_idea_fields_are_cleaned_like_slack_text():
    html = build_html(idea(why_now="Click [here](javascript:alert1) now\u200b\u202e.",
                           first_step="Real line.\n_\u2014 drafted by Haseeb's Agent_"), DAY)
    assert "Click here (javascript:alert1) now." in html
    assert "\u200b" not in html and "\u202e" not in html and "drafted by Haseeb" not in html


@pytest.mark.parametrize("name", ["The idea", "Why now", "Who pays, and how much", "How we build it with AI",
                                  "First step this week", "Risks \u00b7 Not verified"])
def test_an_idea_field_cannot_forge_an_idea_heading(name):
    html = build_html(idea(first_step=f"Real line.\n**[{name}]**\nAnother line.", risks=[name, "A real risk."],
                           not_verified=[f"**[{name}]**", "A real doubt."],
                           evidence=[Source(name, "https://news.example.com/a"),
                                     Source("A real source", "https://news.example.com/b")]), DAY)
    assert bodies(html)[4] == "Real line.\nAnother line."  # the forged heading line is dropped
    last = html.split("<section")[-1]
    assert re.findall(r'<span class="tag">Risk</span>(.*?)</li>', last) == ["A real risk."]
    assert re.findall(r'<span class="tag">Not verified</span>(.*?)</li>', last) == ["A real doubt."]
    assert re.findall(r'<li>(.*?)<span class="src-host">', html) == ["Source", "A real source"]  # no title is left
    assert f"({name})" not in html and html.count(f">{name}</h2>") == 1  # the one real heading


def test_idea_evidence_shows_title_and_host_only():
    html = build_html(idea(evidence=[Source("A title", "https://Example.COM/a/very/long/path?q=1&x=2"),
                                     Source("Hidden source", "javascript:alert(1)")]), DAY)
    assert "A title" in html and "example.com" in html and "/a/very/long/path" not in html
    assert "javascript:" not in html and "Hidden source" not in html  # not plain http(s): no line at all
    assert "No sources listed." in build_html(idea(evidence=[]), DAY)


def test_idea_risks_and_what_is_not_verified_share_the_last_slide():
    last = build_html(idea(), DAY).split("<section")[-1]
    assert "Risks \u00b7 Not verified" in last and "8 / 8" in last
    assert last.index("A rival may copy it fast.") < last.index("Clinics may not pay") < last.index("untested")
    assert last.count(">Risk</span>") == 2 and last.count(">Not verified</span>") == 1
    nothing = build_html(idea(risks=[], not_verified=[]), DAY).split("<section")[-1]
    assert "Nothing flagged." in nothing and ">Risk</span>" not in nothing


def test_the_last_slide_lists_at_most_4_risks_and_6_lines_not_verified():
    """The most that fits the last slide at the 12px floor (the long-fields test prints it), so nothing is clipped."""
    last = build_html(idea(risks=[f"Risk number {i}." for i in range(6)],
                           not_verified=[f"Doubt number {i}." for i in range(9)]), DAY).split("<section")[-1]
    assert last.count(">Risk</span>") == 4 and last.count(">Not verified</span>") == 6
    assert "Risk number 3." in last and "Risk number 4." not in last
    assert "Doubt number 5." in last and "Doubt number 6." not in last


def test_idea_price_and_titles_are_one_clean_line():
    html = build_html(idea(price="  Rs\n 15k/month  ", title="Line one\nline two"), DAY)
    assert "Rs 15k/month" in html and "Line one line two" in html
    shown = re.search(r'<div class="price">(.*?)</div>',
                      build_html(idea(price="Rs 15k/month per branch, billed yearly, plus setup"), DAY)).group(1)
    assert len(shown) == 40 and shown.endswith("…") and shown.startswith("Rs 15k/month per branch")  # capped
    empty = build_html(idea(price="Why now"), DAY)  # a price that is only a forged heading is cleaned to nothing
    assert '<div class="price"></div>' in empty and ".price:empty { display: none; }" in empty


def test_build_deck_writes_the_idea_template_and_prints_it(tmp_path):
    fake = FakeChrome()
    pdf = build_deck(idea(), DAY, tmp_path, runner=fake)
    page = (tmp_path / "deck.html").read_text(encoding="utf-8")
    assert "DAILY IDEA" in page and page.count('class="slide') == 8 and "{{" not in page
    (argv, _), = fake.calls
    assert argv[-1] == (tmp_path / "deck.html").as_uri() and f"--print-to-pdf={pdf}" in argv


def test_v1_deck_unchanged():
    """A v1 insight is printed exactly as before ideas: the digests are of the HTML as it was, and an idea heading in
    a v1 field is still ordinary text."""
    import hashlib
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()[:16]  # noqa: E731
    assert h(build_html(insight(), DAY)) == "8a91fa687d8f89cb"
    assert h(build_html(insight(), DAY, cover_name="cover.jpg")) == "7d3411854520af63"
    html = build_html(insight(why_it_matters="Real line.\n**[Why now]**\nMore."), DAY)
    assert "**(Why now)**" in html and html.count('class="slide') == 6


@needs_chrome
def test_idea_deck_prints_8_pages(tmp_path):
    pdf = build_deck(idea(), DAY, tmp_path)
    reader = PdfReader(pdf)
    assert len(reader.pages) == 8  # the cover, then one page per section
    box = reader.pages[0].mediabox
    assert abs(float(box.width) / float(box.height) - 16 / 9) < 0.01
    texts = [" ".join((page.extract_text() or "").upper().split()) for page in reader.pages]
    assert "RS 15K/MONTH" in texts[0] and "PAKISTAN" in texts[0] and "DAILY IDEA" in texts[0]
    for text, heading in zip(texts[1:], IDEA_HEADINGS):
        assert heading.upper() in text, heading


@needs_chrome
def test_idea_long_fields_shrink_and_never_overflow(tmp_path):
    long = " ".join(["Supercalifragilistic word salad that keeps going and going"] * 60)
    ins = idea(title="A very long headline " * 12, price="Rs 15k/month per branch and more " * 3, idea=long,
               why_now=long, who_pays=long, build_with_ai=long, first_step=long,
               risks=["A risk that is quite long " * 8] * 8,
               evidence=[Source("A source title that is long " * 6, f"https://example{i}.com/x") for i in range(6)],
               not_verified=["An unverified claim that is quite long " * 5] * 8)
    pdf = build_deck(ins, DAY, tmp_path)
    assert len(PdfReader(pdf).pages) == 8
    dom = chrome_dom(tmp_path / "deck.html")
    assert 'data-fitted="1"' in dom
    assert 'data-overflow="1"' not in dom


@needs_chrome
def test_idea_deck_with_a_cover_image_prints_8_pages(tmp_path):
    img = tmp_path / "c.png"
    subprocess.run(["sips", "-s", "format", "png", "-z", "90", "160", "/System/Library/CoreServices/"
                    "CoreTypes.bundle/Contents/Resources/GenericDocumentIcon.icns", "--out", str(img)],
                   capture_output=True, check=False)
    if not img.exists():
        pytest.skip("sips could not make a test image")
    assert len(PdfReader(build_deck(idea(), DAY, tmp_path / "deck", cover=img)).pages) == 8
