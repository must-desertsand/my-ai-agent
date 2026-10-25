"""Designer decks: every number and name in Codex's deck must come from the insight (deck/factcheck.py)."""
from dataclasses import replace
from datetime import date
from pathlib import Path

import logging
import pytest

from haseeb_agent.deck.factcheck import (
    date_tokens,
    fact_check,
    fact_check_pdf,
    number_values,
    pdf_text,
    source_parts,
    source_text,
    visible_text,
)
from haseeb_agent.render import Idea, Insight, Source

DAY = date(2026, 10, 17)
COMMON = frozenset("""the a an of to and is in on for it by we our run cost task price rise rises launch week
first step measure goal client real result page one two show compare paid audit product offer sell test numbers
double doubles bill now after today before builder small quality theme pick model models clear timeline
opportunity move key what happened why matters next""".split())


def argon() -> Insight:
    return Insight(
        title="Gemini 4 Argon launch price ends Nov 30: cost per task about doubles",
        category="AI",
        what_happened=("Google made Gemini 4 Argon available on 2026-10-13. The Gemini API went generally available "
                       "around 10-14. Intro price is $2 per million input tokens and $10 per million output tokens. "
                       "On Nov 30, 2026 the price rises to $4 and $20. Google says Argon ranks #1 on the Vals Index. "
                       "It has a 1M-token limit and a 0.7% prompt-injection success rate on Gray Swan. A newsletter "
                       "relaying Artificial Analysis says cost per task is $1.99 now and about $3.98 after."),
        why_it_matters=("If a client builds on Argon and the bill doubles on Nov 30, the client blames the builder. "
                        "The AX department rental model (about KRW 10M per month) is a clear first deliverable."),
        silicon_valley="Run 20 real tasks on Argon and two rivals. Goal: one paid audit signed before Nov 30.",
        evidence=[Source("Introducing Gemini 4 Argon (Google)", "https://blog.google/innovation/introducing-argon/"),
                  Source("AI This Week (Trew Knowledge)", "https://trewknowledge.com/2026/10/16/ai/")],
        not_verified=["Cost per task of $1.99 comes from Artificial Analysis via a newsletter"],
        read_min=2,
    )


def page(*slides: str) -> str:
    body = "".join(f'<section class="slide">{s}</section>' for s in slides)
    return f"<!doctype html><html><head><style>.a{{color:red}}</style></head><body>{body}</body></html>"


def check(html, ins=None):
    return fact_check(html, ins or argon(), DAY)  # the bundled common-word list


GOOD = page(
    "<h1>Gemini 4 Argon: the launch price ends Nov 30</h1><p>Haseeb's Agent · Daily Insight · 17 Oct 2026</p>",
    '<div class="big">$2 → $4</div><p>input, per 1M tokens</p><div class="big">$10 → $20</div>',
    "<h2>Cost per task</h2><svg><text>$1.99</text><text>$3.98</text></svg><p>About ×2 on November 30</p>",
    '<img src="img1.png" alt="A rising price line"><p>0.7% prompt-injection success on Gray Swan</p>',
    "<h2>The move</h2><p>Run 20 real tasks on Argon and two rivals. One paid audit before 2026-11-30.</p>",
    "<h2>Evidence</h2><ul><li>Introducing Gemini 4 Argon (Google) <span>blog.google</span></li>"
    "<li>AI This Week (Trew Knowledge) <span>trewknowledge.com</span></li></ul>",
)


def test_a_rephrased_deck_with_only_the_insights_facts_passes():
    r = check(GOOD)
    assert r.ok, r.problems


@pytest.mark.parametrize("extra, kind", [
    ("<p>Price rises 3x</p>", "number"),
    ("<p>$5 per million</p>", "number"),
    ("<p>From Dec 1</p>", "date"),
    ("<p>Ends Nov 29</p>", "date"),
    ("<p>2027-11-30</p>", "date"),
    ("<p>Claude is cheaper</p>", "name"),
    ("<p>OpenAI responds</p>", "name"),
    ('<img src="img1.png" alt="Sam Altman">', "name"),
    ('<div title="47% of clients"></div>', "number"),
    ("<p>See https://blog.google/x</p>", "url"),
    ("<p>See evil.example.com</p>", "url"),
    ("<p>www.trewknowledge.com</p>", "url"),
    ("<p>Ask @haseeb</p>", "mention"),
    ("<p>&lt;!channel&gt; now</p>", "control"),
    ("<p>&lt;@U123&gt;</p>", "control"),
    ("<style>.x::after{content:\"99%\"}</style>", "number"),
])
def test_a_new_fact_or_banned_content_fails(extra, kind):
    r = check(GOOD.replace("</body>", extra + "</body>"))
    assert not r.ok and r.problems[kind] >= 1


def test_problems_are_counts_by_type_never_text():
    r = check(GOOD.replace("</body>", "<p>Claude and Mistral at $5</p></body>"))
    assert dict(r.problems) == {"name": 2, "number": 1}
    assert "Claude" not in repr(r)


@pytest.mark.parametrize("a, b", [("×2", "2x"), ("$1,000", "1000"), ("1M", "1 million"), ("10M", "10,000,000"),
                                  ("0.70%", "0.7"), ("$3.98", "3.98"), ("20k", "20,000"), ("2.5B", "2.5 billion")])
def test_number_normalization(a, b):
    assert number_values(a) == number_values(b)


@pytest.mark.parametrize("text", ["Nov 30", "November 30", "30 November", "nov. 30th", "2026-11-30",
                                  "Nov 30, 2026", "30 Nov 2026"])
def test_date_normalization(text):
    assert {(m, d) for _, m, d in date_tokens(text)} == {(11, 30)}


def test_a_deck_date_needs_its_year_when_it_gives_one():
    assert check(GOOD.replace("</body>", "<p>Nov 30, 2026</p></body>")).ok
    assert not check(GOOD.replace("</body>", "<p>Nov 30, 2025</p></body>")).ok


def test_the_deck_date_and_branding_are_allowed():
    r = check(page("<p>HASEEB'S AGENT · DAILY INSIGHT · SATURDAY 17 OCTOBER 2026 · MUST</p>"))
    assert r.ok, r.problems


def test_digits_split_across_inline_tags_are_read_as_one_number():
    assert not check(page("<p><span>1</span><span>00</span> tasks</p>")).ok  # renders "100"
    assert not check(page("<p>Open<b>AI</b></p>")).ok  # renders "OpenAI"


def test_block_elements_are_separate_words():
    assert check(page("<h2>Argon</h2><p>Gemini</p>")).ok


def test_evidence_hosts_are_allowed_but_no_other_host():
    assert check(page("<p>blog.google</p><p>trewknowledge.com</p>")).ok
    assert not check(page("<p>google.com</p>")).ok


def test_common_words_and_inflections_pass_but_brand_words_do_not():
    assert check(page("<h2>Opportunity</h2><h3>KEY NUMBERS</h3><p>Clients compare models</p>")).ok
    apple = fact_check(page("<p>Apple</p>"), argon(), DAY, common_words=COMMON | {"apple"})
    assert not apple.ok  # a common word that is also a company
    assert not check(page("<p>Nvidia</p>")).ok


def test_visible_text_includes_alt_title_aria_svg_and_css_content():
    t = visible_text('<img alt="A1"><div title="T1" aria-label="L1"><svg><text>S1</text></svg></div>'
                     '<style>.a::before{content:"C1"}</style><script>X1</script>')
    for part in ("A1", "T1", "L1", "S1", "C1"):
        assert part in t
    assert "X1" not in t and "color" not in t


def test_source_text_holds_every_field_and_the_date():
    t = source_text(argon(), DAY)
    for part in ("Gemini 4 Argon", "Gray Swan", "blog.google", "trewknowledge.com", "Trew Knowledge", "KRW 10M",
                 "Artificial Analysis", "2026-10-17", "17 Oct 2026"):
        assert part in t


def test_the_source_text_is_read_generously():
    # "around 10-14" in the source supports "Oct 14" in the deck
    assert check(page("<p>API generally available around Oct 14</p>")).ok


def test_the_good_deck_passes_with_more_plain_wording():
    r = fact_check(GOOD.replace("</body>", "<h2>Why it matters to us</h2><p>Clients pick a model on the launch "
                                           "price. Our offer: a fixed-price audit.</p></body>"), argon(), DAY)
    assert r.ok, r.problems


# -- R47: the differential bypasses ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["<p>openai is cheaper</p>", "<p>claude</p>", "<p>APPLE</p>", "<p>nvidia gpus</p>",
                                  "<p>zorblax</p>"])
def test_words_are_checked_whatever_their_case(text):
    """text-transform could capitalize a lowercase name: every word counts, in any case."""
    r = check(page(text))
    assert not r.ok and r.problems["name"] >= 1


@pytest.mark.parametrize("ch", ["\u2024", "\uff0e", "\u2236", "\uff1a", "\u2044", "\u2215", "\uff0f", "\uff20",
                                "\ufe6b", "\u3002", "\ua789"])
def test_unicode_look_alikes_of_at_dot_colon_and_slash_fail(ch):
    r = check(page(f"<p>evil{ch}com</p>"))
    assert not r.ok and r.problems["lookalike"] >= 1


def test_an_ellipsis_and_the_middle_dot_are_not_look_alikes():
    assert check(page("<p>Gemini 4 Argon \u00b7 launch price\u2026</p>")).ok


def test_non_ascii_letters_must_be_in_the_insight():
    r = check(page("<p>Argon caf\u00e9</p>"))
    assert not r.ok and r.problems["unicode"] == 1
    ins = argon()
    ins.why_it_matters += " A caf\u00e9 offer."
    assert check(page("<p>caf\u00e9</p>"), ins).ok


def test_nfkc_runs_first():
    r = check(page("<p>\uff21\uff50\uff50\uff4c\uff45</p>"))  # fullwidth "Apple"
    assert not r.ok and r.problems["name"] == 1


@pytest.mark.parametrize("text, ok", [("twenty real tasks", True), ("two rivals", True), ("seventy percent", False),
                                      ("five million clients", False), ("one paid audit", True),
                                      ("twenty-one tasks", False), ("a million tokens", True)])
def test_spelled_out_numbers_are_checked(text, ok):
    assert check(page(f"<p>{text}</p>")).ok is ok


def test_spelled_out_numbers_normalize_like_digits():
    assert number_values("two million") == number_values("2M") == {"2000000"}
    assert number_values("seven percent") == number_values("7%")
    assert number_values("one hundred and twenty") == {"120"}


def test_the_pdf_reading_is_authoritative():
    clean_pdf = lambda p: "Gemini 4 Argon: the launch price ends Nov 30"  # noqa: E731
    injected = lambda p: "Gemini 4 Argon: the launch price ends Nov 30 $99"  # noqa: E731
    assert fact_check_pdf(Path("x.pdf"), argon(), DAY, reader=clean_pdf).ok
    r = fact_check_pdf(Path("x.pdf"), argon(), DAY, reader=injected)
    assert not r.ok and r.problems == {"number": 1}


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def chrome_pdf(tmp_path, html: str) -> Path:
    import subprocess
    src, pdf = tmp_path / "raw.html", tmp_path / "raw.pdf"
    src.write_text(html, encoding="utf-8")
    subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
                    f"--user-data-dir={tmp_path / 'profile'}", "--no-pdf-header-footer", f"--print-to-pdf={pdf}",
                    src.as_uri()], capture_output=True, timeout=90, check=True)
    return pdf


@pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")
@pytest.mark.parametrize("trick, body", [
    (".x::after{content:attr(data-n)}", '<p class="x" data-n=" $99">Run 20 real tasks</p>'),
    (".x{counter-reset:n 98}.x p::before{counter-increment:n;content:counter(n) \" \"}", '<div class="x"><p>tasks</p></div>'),
    (".x{text-transform:capitalize}", '<p class="x">apple</p>'),
    (".x{display:flex;flex-direction:row-reverse;justify-content:flex-end}", '<p class="x"><span>1</span><span>4</span></p>'),
])
def test_a_css_injected_fact_fails_on_the_pdf_though_the_html_text_is_clean(tmp_path, trick, body):
    """R47: unsanitized HTML on purpose. The HTML reading passes (the attribute, counter, case or order lives in CSS),
    the PDF reading, which is what people see, does not."""
    html = (f"<!doctype html><html><head><style>@page{{size:1280px 720px;margin:0}} {trick}</style></head>"
            f"<body><h1>Gemini 4 Argon</h1>{body}</body></html>")
    if "apple" not in body:
        assert fact_check(html.replace(trick, ""), argon(), DAY).ok  # without the CSS, the deck says nothing new
    pdf = chrome_pdf(tmp_path, html)
    r = fact_check_pdf(pdf, argon(), DAY)
    assert not r.ok, pdf_text(pdf)


@pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")
def test_absolutely_positioned_boxes_are_read_apart(tmp_path):
    html = ("<!doctype html><html><head><style>@page{size:1280px 720px;margin:0} span{position:absolute;top:300px;"
            "font-size:60px}</style></head><body><span style=\"left:600px\">tasks</span>"
            "<span style=\"left:100px\">Argon</span></body></html>")
    assert fact_check_pdf(chrome_pdf(tmp_path, html), argon(), DAY).ok


# -- live run 1 (2026-10-17) -----------------------------------------------------------------------------------


def test_a_date_never_spans_layout_columns():
    """pypdf's layout reading put "Around 10-14" and "Nov 30, 2026" in one line with 20+ spaces between them."""
    text = "Around 10-14                       Nov 30, 2026"
    assert date_tokens(text) == [(2026, 11, 30)]
    assert date_tokens("14\nNov") == []
    assert number_values("two                million") == {"2", "1000000"}


def test_colour_legends_and_plain_design_words_pass():
    assert check(page("<p>Blue: intro price · Coral: Nov 30, 2026 price</p><p>The source trail</p>")).ok


@pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")
def test_tight_tracking_on_a_big_number_reads_as_one_number(tmp_path):
    """Live run 1: letter-spacing:-6px at 112px made pypdf read $1.99 as "$1.9 9". The sanitizer strips spacing."""
    from haseeb_agent.deck.sanitize import sanitize_html
    raw = ('<style>.big{font-size:112px;font-weight:750;letter-spacing:-6px}.k{letter-spacing:8px}</style>'
           '<div class="big">$1.99</div><div class="big" style="letter-spacing:-6px">$3.98</div>'
           '<p class="k">DAILY INSIGHT 17 OCT 2026</p>')
    safe, _ = sanitize_html(raw, tmp_path)
    pdf = chrome_pdf(tmp_path, safe)
    r = fact_check_pdf(pdf, argon(), DAY)
    assert r.ok, (r, pdf_text(pdf))


def test_the_pdf_reading_repairs_a_spurious_space_inside_a_number_or_word():
    """Live run 1: pypdf read Chrome's per-glyph run "$1.99" as "$1.9 9" (the font widths were consistent)."""
    read = lambda text: (lambda p: text)  # noqa: E731
    assert fact_check_pdf(Path("x"), argon(), DAY, reader=read("$1.9 9 → $3.9 8 per task")).ok
    assert fact_check_pdf(Path("x"), argon(), DAY, reader=read("Gemi ni 4 Ar gon")).ok
    for bad in ("$1.9 tasks", "$9 9", "Claude 4", "$1.99 9", "Ope nai"):
        assert not fact_check_pdf(Path("x"), argon(), DAY, reader=read(bad)).ok, bad


def test_the_html_reading_never_repairs():
    assert not check(page("<p>$1.9 9</p>")).ok


def test_ment_nouns_stem_to_their_verb():
    """Live run 2: "A longer client commitment." was refused although "commit" is a common word."""
    assert check(page("<p>A longer client commitment, a clear agreement</p>")).ok
    assert not check(page("<p>openaiment</p>")).ok


def test_live_run_3_wording_passes():
    """Live run 3: "THE PUBLISHED PRICES" was refused on "published"."""
    assert check(page("<p>THE PUBLISHED PRICES</p>")).ok


# -- R49: the vocabulary -----------------------------------------------------------------------------------------

from haseeb_agent.deck import factcheck as fc  # noqa: E402


@pytest.mark.parametrize("text", ["the published prices", "a zebra crossing", "Clients pay first.",
                                  "THE PUBLISHED PRICES", "Commitments matter", "A longer client commitment"])
def test_plain_english_from_the_dictionary_passes(text):
    r = check(page(f"<p>{text}</p>"))
    assert r.ok, (r, r.refused)


@pytest.mark.parametrize("token", ["OpenAI", "GPT", "GPT5", "iPhone", "DeepSeek", "McKinsey", "Kimi", "Claude",
                                   "openai", "slack", "Slack", "APPLE", "Nvidia", "b3b"])
def test_brand_and_product_like_tokens_fail_unless_in_the_insight(token):
    r = check(page(f"<p>Argon {token} price</p>"))
    assert not r.ok and r.problems["name"] == 1, (token, r)
    ins = argon()
    ins.why_it_matters += f" {token} matters."
    assert check(page(f"<p>Argon {token} price</p>"), ins).ok


def test_gpt_5_fails_on_the_word_and_the_number():
    r = check(page("<p>GPT-5</p>"))
    assert not r.ok and r.problems["name"] == 1


def test_numbers_dates_urls_and_mentions_stay_strict():
    for bad in ("<p>$99</p>", "<p>Dec 31</p>", "<p>evil.com</p>", "<p>a @team</p>", "<p>café</p>"):
        assert not check(page(bad)).ok, bad


def test_refused_tokens_are_kept_for_the_repair_turn_but_never_in_the_repr():
    r = check(page("<p>Claude costs $5 on Dec 31 at evil.com</p>"))
    assert {"Claude", "5", "Dec 31", "evil.com"} <= set(r.refused)
    assert "Claude" not in repr(r) and "$5" not in repr(r)


def test_without_the_dictionary_the_bundled_list_is_used_and_logged_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(fc, "DICT_WORDS", tmp_path / "missing")
    fc.dictionary_words.cache_clear()
    try:
        with caplog.at_level(logging.INFO):
            assert not check(page("<p>a zebra crossing</p>")).ok
            assert check(page("<p>the published prices</p>")).ok  # "publish" is on the bundled list
        assert sum("dict" in rec.getMessage() for rec in caplog.records) == 1
    finally:
        fc.dictionary_words.cache_clear()


def test_the_dictionary_keeps_only_lowercase_entries(monkeypatch, tmp_path):
    d = tmp_path / "words"
    d.write_text("Zorblax\nquux\nAbc\n")
    monkeypatch.setattr(fc, "DICT_WORDS", d)
    fc.dictionary_words.cache_clear()
    try:
        assert fc.dictionary_words() == frozenset({"quux"})
        assert check(page("<p>quux Quux</p>")).ok
        assert not check(page("<p>zorblax</p>")).ok
    finally:
        fc.dictionary_words.cache_clear()


# -- the designer-deck review (C1, C2, I1, M1) -------------------------------------------------------------------

from haseeb_agent.deck.factcheck import check_text, coverage  # noqa: E402


@pytest.mark.parametrize("ch", ["​", "­", "⁠", "‌", "‍", "﻿", "᠎", "‮",
                                "️"])
def test_invisible_characters_never_split_a_token(ch):
    """C2: "Open​AI" is drawn as OpenAI and read as OpenAI in both readings."""
    assert not check_text(f"Open{ch}AI costs $4{ch}20", argon(), DAY).ok
    r = check_text(f"Open{ch}AI", argon(), DAY)
    assert r.problems == {"name": 1} and "OpenAI" in r.refused
    assert not check(page(f"<p>Open{ch}AI</p>")).ok
    assert not check(page("<p>Open<wbr>AI</p>")).ok


@pytest.mark.parametrize("text", ["DEC\n30", "DEC\n\n   30", "Dec          30", "December\n30th", "the 30th of December",
                                  "30th of Dec", "12/30", "30/12/2026", "Dec 30"])
def test_dates_in_every_form_must_be_in_the_insight(text):
    r = check_text(f"Argon {text}", argon(), DAY)
    assert not r.ok and r.problems.get("date") == 1, (text, r, r.refused)


@pytest.mark.parametrize("text", ["NOV\n30", "Nov          30", "the 30th of November", "11/30", "30/11/2026",
                                  "November 30, 2026"])
def test_the_insights_dates_pass_in_every_form(text):
    r = check_text(f"Argon {text}", argon(), DAY)
    assert r.ok, (text, r, r.refused)


@pytest.mark.parametrize("word", ["December", "DEC", "March", "Friday", "MON"])
def test_month_and_weekday_names_need_the_insight(word):
    r = check_text(f"Argon price rises in {word}", argon(), DAY)
    assert not r.ok and r.problems == {"name": 1}, (word, r)


@pytest.mark.parametrize("text", ["Argon price rises in November", "on Saturday", "October", "clients may judge Argon",
                                  "17 Oct 2026", "Saturday, October 17, 2026"])
def test_the_insights_months_and_today_pass(text):
    assert check_text(text, argon(), DAY).ok


@pytest.mark.parametrize("text", ["17 clients", "2026 clients", "Argon in 2026"])
def test_todays_date_gives_no_numbers(text):
    """I1: "17" and "2026" come only from today's date in this insight."""
    ins = argon()
    ins.what_happened = ins.what_happened.replace("2026-10-13", "Oct 13").replace("Nov 30, 2026", "Nov 30")
    r = check_text(text, ins, DAY)
    assert not r.ok and r.problems.get("number") == 1, (text, r)


@pytest.mark.parametrize("text", ["Deep Seek", "Open AI", "DEEP SEEK", "Perplex ity"[:0] + "Perple xity",
                                  "Deep\nSeek", "Sales force", "x AI"])
def test_split_brands_are_read_together(text):
    r = check_text(f"Argon {text} price", argon(), DAY)
    assert not r.ok and r.problems.get("name", 0) >= 1, (text, r)


@pytest.mark.parametrize("word", ["openai", "deepseek", "anthropic", "xai", "salesforce", "tether", "ripple", "visa",
                                  "musk", "linear", "mistral", "cohere", "perplexity", "nvidia", "meta"])
def test_m1_brand_stems(word):
    assert not check_text(f"Argon {word}", argon(), DAY).ok


def test_glued_number_runs_in_the_html_reading():
    """C1(d): "$", "4" and "20" in three flex items are drawn as "$420"."""
    r = check(page('<div style="display:flex"><div>$</div><div>4</div><div>20</div></div>'))
    assert not r.ok and r.problems == {"number": 1} and "$420" in r.refused
    assert check(page("<div><div>$4</div><p>and</p><div>$20</div></div>")).ok


def test_coverage_gate_needs_every_html_token_in_the_pdf_reading():
    html = page("<h1>Gemini 4 Argon</h1><div>Deep</div><div>Seek</div><p>costs $4.20</p>")
    assert coverage(html, "Gemini 4 Argon\nDeepSeek\ncosts $4.20").ok
    assert coverage(html, "G e m i n i 4\nArgon DeepSeek costs $4. 20").ok  # whitespace never matters
    r = coverage(html, "Gemini 4 Argon\ncosts $4.20")  # the XObject case: the PDF reading misses the flex row
    assert not r.ok and r.problems == {"coverage": 2} and set(r.refused) == {"Deep", "Seek"}
    assert coverage(page("<p>ﬁnal Open​AI</p>"), "final OpenAI").ok  # NFKC, invisible characters


def test_the_pdf_is_also_read_with_rotated_text(monkeypatch):
    calls = []

    class Page:
        def extract_text(self, extraction_mode="plain", layout_mode_strip_rotated=True):
            calls.append(layout_mode_strip_rotated)
            return "Argon" + ("" if layout_mode_strip_rotated else "\nDeepSeek")

    class Reader:
        def __init__(self, path):
            self.pages = [Page()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    assert pdf_text(Path("x.pdf")) == "Argon\n\vArgon\nDeepSeek" and calls == [True, False]


def test_a_sentence_boundary_glued_by_the_layout_is_not_a_domain():
    """Live run 5: pypdf put two columns side by side as "approximate.Input". Only the PDF reading (repair) skips
    it, and only for one dot and a right-hand label that is not an IANA TLD (N2)."""
    glued = "The Gemini API date is approximate.Input and output token prices"
    assert check_text(glued, argon(), DAY, repair=True).ok
    assert not check_text(glued, argon(), DAY).ok  # the HTML reading never skips
    for bad in ("evil.com", "EVIL.COM", "evil.co.uk", "Zorblax.Com", "openai.Com"):
        assert not check_text(f"Argon {bad}", argon(), DAY, repair=True).ok, bad
    assert check_text("blog.google and trewknowledge.com", argon(), DAY, repair=True).ok


def test_a_capitalized_tld_is_still_a_url():
    """Security reviews of the sentence-boundary rule (N2): every one of these is a domain in the PDF reading."""
    for bad in ("Visit evil.Com today", "Scam.Io", "pay.Net now", "Deep.Com", "argon.Ai", "x.Co", "mail.Info",
                "evil.com.Then", "login.evil.com.Visit", "pay.evil.Visit", "evil.Click", "evil.Kitchen", "evil.Bond"):
        for repair in (True, False):
            r = check_text(f"Argon {bad}", argon(), DAY, repair=repair)
            assert not r.ok and r.problems.get("url"), (bad, repair)
    for repair in (True, False):  # an evidence host, mixed case
        assert check_text("Read it on Blog.Google and BLOG.GOOGLE", argon(), DAY, repair=repair).ok


def test_the_html_reading_refuses_a_capitalized_gtld():
    r = check(page("<p>Pay at pay.evil.Click</p>"))
    assert not r.ok and r.problems.get("url")


def test_the_tld_list_is_iana_and_bundled():
    from haseeb_agent.deck.factcheck import iana_tlds
    tlds = iana_tlds()
    assert {"com", "click", "kitchen", "bond", "io", "google", "uk"} <= tlds and len(tlds) > 1000
    assert "input" not in tlds


def test_an_unreadable_tld_list_fails_closed(monkeypatch, tmp_path, caplog):
    """F1: the list is read lazily; when it cannot be read, the sentence-boundary exception is off (fail closed)
    and one line with no content is logged."""
    from haseeb_agent.deck import factcheck as fc
    monkeypatch.setattr(fc, "TLD_FILE", tmp_path / "missing.txt")
    fc.iana_tlds.cache_clear()
    try:
        glued = "The Gemini API date is approximate.Input and output token prices"
        with caplog.at_level("WARNING", logger=fc.__name__):
            r = check_text(glued, argon(), DAY, repair=True)
            assert check_text("Argon evil.Click", argon(), DAY, repair=True).problems.get("url")
        assert not r.ok and r.problems.get("url")
        lines = [x for x in caplog.records if x.name == fc.__name__]
        assert len(lines) == 1 and "approximate" not in lines[0].getMessage()
    finally:
        fc.iana_tlds.cache_clear()


def test_the_agent_imports_without_the_tld_list(tmp_path):
    """F1 / R44: cli -> jobs.decks -> designer -> factcheck must not read the list at import."""
    import subprocess
    import sys
    code = """
import pathlib
real = pathlib.Path.read_text
def read_text(self, *a, **k):
    if self.name == "tlds-alpha-by-domain.txt":
        raise PermissionError(13, "Permission denied")
    return real(self, *a, **k)
pathlib.Path.read_text = read_text
import haseeb_agent.cli
from datetime import date
from tests.test_deck_factcheck import argon
from haseeb_agent.deck.factcheck import check_text
r = check_text("The date is approximate.Input and output", argon(), date(2026, 10, 17), repair=True)
print("refused" if r.problems.get("url") else "passed")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=Path(__file__).parent.parent)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "refused"


def test_coverage_counts_each_occurrence():
    """N1(b): a token shown twice in the HTML needs two occurrences in the PDF reading; fragments of other text
    cannot stand in for it (the pattern probe: "$4" and "20" borrowed from "$4 and $20")."""
    html = page("<p>$4</p><p>20</p><p>it rises to $4 and $20</p>")
    r = coverage(html, "it rises to $4 and $20")
    assert not r.ok and set(r.refused) == {"$4", "20"}
    assert coverage(html, "$4 20\nit rises to $4 and $20").ok
    r = coverage(page("<p>Open</p><p>Open the AI tab</p>"), "Open the AI tab")
    assert not r.ok and r.refused == ("Open",)
    assert coverage(page("<p>Open</p><p>Open the AI tab</p>"), "Open\nOpen the AI tab").ok


def test_coverage_is_per_page_when_slides_map_to_pages():
    html = page("<h1>Gemini 4 Argon</h1>", "<p>Argon costs $4</p>")
    assert coverage(html, "Gemini 4 Argon\n\f\nArgon costs $4").ok
    r = coverage(html, "Gemini 4\n\f\nArgon Argon costs $4")  # slide 1's "Argon" is only on page 2
    assert not r.ok and r.refused == ("Argon",)
    # F2: three pages for two slides cannot be mapped, so the deck is refused (repair turn, then the template)
    r = coverage(html, "Gemini 4\n\f\nArgon\n\f\nArgon costs $4")
    assert not r.ok and r.problems == {"pages": 1}
    assert not coverage("<p>Gemini 4 Argon</p>" + html, "Gemini 4 Argon\n\f\nArgon costs $4").ok  # text outside
    assert coverage(html, "Gemini 4 Argon\n\f\nArgon costs $4\n\f\n  \n").ok  # a trailing blank page
    assert not coverage(html, "Gemini 4 Argon\n\f\n \n\f\nArgon costs $4").ok  # a blank page in between
    # a page's rotated-kept reading (after \v) is the one counted, never added to the layout reading
    assert not coverage(page("<p>Argon Argon</p>"), "Argon\n\vArgon").ok
    assert coverage(page("<p>Argon DeepSeek</p>"), "Argon\n\vArgon\nDeepSeek").ok


def test_the_briefs_own_words_for_haseeb_pass():
    """Haseeb's deck-test at 466e622: the design brief says "Haseeb Zafar's team at MUST", so "Zafar" on a slide
    is our branding, not an invented name (it was refused as name=1, and the repair turn could not fix it)."""
    for t in ("Made for Haseeb Zafar's team at MUST", "Haseeb Zafar", "If we were in Silicon Valley"):
        assert check_text(t, argon(), DAY).ok, t
        assert check_text(t, argon(), DAY, repair=True).ok, t
    assert not check_text("Made for Haseeb Zorblax", argon(), DAY).ok
    # review of 0e09dc0: the exact two-word phrase only; "Zafar" alone is still a name
    for t in ("Zafar Capital backs Argon", "Ali Zafar", "Zafar", "Haseeb and Zafar"):
        for repair in (False, True):
            r = check_text(t, argon(), DAY, repair=repair)
            assert not r.ok and "Zafar" in r.refused, (t, repair)


# -- ideas: the same check, against an idea's own text -----------------------------------------------------------


def receptionist(**over) -> Idea:
    """An idea as the writer returns it. Its text never says "Pakistan": only the market does."""
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


IDEA_DECK = page(
    "<h1>AI receptionist for clinics</h1><p>Haseeb's Agent \u00b7 Daily Idea \u00b7 17 Oct 2026</p>",
    "<div class=\"big\">Rs 15k/month</div><p>50 clinics \u00d7 Rs 15k = Rs 750k/month</p>",
    "<h2>First step</h2><p>Call 10 clinics by Friday. Get 3 to pre-commit.</p>",
    "<h2>Evidence</h2><ul><li>WhatsApp opens its business API to small shops <span>news.example.com</span></li>"
    "<li>Missed visits cost clinics hours each week <span>clinics.example.pk</span></li></ul>",
    "<h2>Risks</h2><p>A rival may copy it fast. The price is untested with buyers.</p>",
)


def test_factcheck_accepts_the_idea_numbers():
    idea = receptionist()
    ok = fact_check(IDEA_DECK, idea, DAY)  # "Rs 750k" is in who_pays; "Daily Idea" is our branding
    assert ok.ok, ok.problems
    bad = fact_check(IDEA_DECK.replace("</body>", "<p>Rs 900k a year</p></body>"), idea, DAY)
    assert not bad.ok and dict(bad.problems) == {"number": 1} and "900k" in bad.refused
    assert fact_check_pdf(Path("x.pdf"), idea, DAY, reader=lambda p: "Daily Idea \u00b7 Rs 750k/month").ok
    assert not fact_check_pdf(Path("x.pdf"), idea, DAY, reader=lambda p: "Daily Idea \u00b7 Rs 900k/month").ok


def test_the_idea_branding_is_daily_idea_and_a_v1_deck_keeps_daily_insight():
    assert fact_check(page("<p>HASEEB'S AGENT \u00b7 DAILY IDEA \u00b7 SATURDAY 17 OCTOBER 2026 \u00b7 MUST</p>"),
                      receptionist(), DAY).ok
    idea_extra, v1_extra = source_parts(receptionist(), DAY)[1], source_parts(argon(), DAY)[1]
    assert "Daily Idea" in idea_extra and "Daily Insight" not in idea_extra
    assert "Daily Insight" in v1_extra and "Daily Idea" not in v1_extra
    assert "Haseeb's Agent" in idea_extra and "2026-10-17" in idea_extra and "17 Oct 2026" in idea_extra


def test_the_idea_source_text_holds_every_field():
    facts, extra = source_parts(receptionist(), DAY)
    for part in ("AI receptionist for clinics", "Pakistan", "Rs 15k/month", "Small clinics lose patients",
                 "business API", "50 clinics", "hosted model", "pre-commit", "A rival may copy it fast.",
                 "Clinics may not pay for reminders.", "WhatsApp opens its business API", "Missed visits cost",
                 "news.example.com", "clinics.example.pk", "untested with buyers"):
        assert part in facts, part
    assert "2026-10-17" not in facts and "17 Oct 2026" not in facts  # the date is in the extras, not the facts
    assert source_text(receptionist(), DAY) == facts + "\n" + extra


PLAIN = receptionist(title="Clinic bot", price="Free trial", idea="Clinics lose patients.", why_now="Bots are cheap.",
                     who_pays="Clinic owners pay.", build_with_ai="Two people ship a bot.", first_step="Call clinics.",
                     risks=["A rival may copy it."], not_verified=["Prices are untested."],
                     evidence=[Source("A survey", "https://news.example.com/x")])


@pytest.mark.parametrize("over, shown", [
    ({"title": "Clinic bot for 40 clinics"}, "40 clinics"),
    ({"price": "Rs 15k/month"}, "Rs 15k"),
    ({"idea": "Clinics lose 6 patients a day."}, "6 patients"),
    ({"why_now": "Bots fell to Rs 3k."}, "Rs 3k"),
    ({"who_pays": "Clinic owners pay Rs 750k."}, "Rs 750k"),
    ({"build_with_ai": "Two people ship a bot in 9 days."}, "9 days"),
    ({"first_step": "Call 25 clinics."}, "25 clinics"),
    ({"risks": ["Rivals charge Rs 8k."]}, "Rs 8k"),
    ({"not_verified": ["Only 12 clinics were asked."]}, "12 clinics"),
    ({"evidence": [Source("A survey of 300 clinics", "https://news.example.com/x")]}, "300 clinics"),
])
def test_every_idea_field_is_a_source_of_numbers(over, shown):
    deck = page(f"<p>{shown}</p>")
    assert not fact_check(deck, PLAIN, DAY).ok  # without it the number is new
    assert fact_check(deck, replace(PLAIN, **over), DAY).ok, shown


def test_idea_numbers_still_come_only_from_the_ideas_own_text():
    # today is 2026-10-17, and the deck date is never a source of numbers
    for shown in ("17 clinics", "2026 clinics", "Rs 16k", "Rs 749k"):
        r = fact_check(page(f"<p>{shown}</p>"), receptionist(), DAY)
        assert not r.ok and r.problems["number"] >= 1, shown


def test_the_market_label_is_part_of_the_ideas_text():
    assert fact_check(page("<p>Pakistan</p>"), receptionist(market="PK"), DAY).ok
    assert not fact_check(page("<p>Pakistan</p>"), receptionist(market="GCC"), DAY).ok  # a name the idea never gave
    assert fact_check(page("<p>GCC</p>"), receptionist(market="GCC"), DAY).ok
    assert not fact_check(page("<p>GCC</p>"), receptionist(market="PK"), DAY).ok


def test_an_idea_deck_may_show_only_the_ideas_evidence_hosts():
    assert fact_check(page("<p>news.example.com</p><p>clinics.example.pk</p>"), receptionist(), DAY).ok
    assert not fact_check(page("<p>example.org</p>"), receptionist(), DAY).ok
    assert not fact_check(page("<p>Claude is cheaper</p>"), receptionist(), DAY).ok
    assert not fact_check(page("<p>Ask @haseeb</p>"), receptionist(), DAY).ok


def test_v1_source_text_unchanged():
    """A v1 insight is checked against exactly the text it always was (the digests are of the text before ideas)."""
    import hashlib
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()[:16]  # noqa: E731
    facts, extra = source_parts(argon(), DAY)
    assert (h(facts), h(extra)) == ("375ead8b37ec5567", "3f291d9237a42968")
