"""Designer decks: Codex's deck.html is rebuilt tag by tag before Chrome sees it (deck/sanitize.py)."""
import os
import re
from pathlib import Path

import pytest

from haseeb_agent.deck.sanitize import CSP, sanitize_html

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    (d / "img1.png").write_bytes(PNG)
    (d / "sub").mkdir()
    (d / "sub" / "img2.png").write_bytes(PNG)
    return d


def clean(html, work, **kw):
    out, removed = sanitize_html(html, work, **kw)
    return out, removed


def test_scripts_and_their_content_are_removed(work):
    out, removed = clean('<p>a</p><script>alert(1)</script><SCRIPT src="x.js"></SCRIPT><p>b</p>', work)
    assert "script" not in out.lower() and "alert" not in out
    assert "<p>a</p>" in out and "<p>b</p>" in out
    assert removed["tag:script"] == 2


def test_svg_script_and_animation_are_removed(work):
    html = ('<svg><script>alert(1)</script><a href="javascript:alert(1)"><text>t</text></a>'
            '<set attributeName="href" to="https://evil.example/x.png"/>'
            '<animate attributeName="href" values="file:///etc/passwd"/></svg>')
    out, _ = clean(html, work)
    low = out.lower()
    assert "alert" not in low and "javascript" not in low and "evil" not in low and "passwd" not in low
    assert "<text>t</text>" in out


@pytest.mark.parametrize("attr", ['onload="x()"', 'ONCLICK="x()"', 'onerror=x', 'onmouseover = "x"'])
def test_event_handlers_are_removed(work, attr):
    out, removed = clean(f'<div {attr} class="a">hi</div>', work)
    assert "on" not in out.split("<div", 1)[1].split(">", 1)[0].replace("class", "")
    assert 'class="a"' in out and removed["handler"] == 1


@pytest.mark.parametrize("tag", [
    '<iframe src="img1.png"></iframe>', '<object data="img1.png"></object>', '<embed src="img1.png">',
    '<link rel="stylesheet" href="img1.png">', '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<base href="https://evil.example/">', '<frame src="x">', '<video src="img1.png"></video>',
    '<audio src="img1.png"></audio>', '<form action="https://evil.example"><input name=a></form>',
    '<noscript><img src="https://evil.example/x.png"></noscript>', '<template><img src="x"></template>',
])
def test_dangerous_tags_are_removed(work, tag):
    out, _ = clean(f"<p>keep</p>{tag}", work)
    body = out.split(CSP, 1)[1]
    for bad in ("iframe", "object", "embed", "<link", "refresh", "<base", "<frame", "video", "audio", "form",
                "noscript", "template", "evil", "<input"):
        assert bad not in body.lower()
    assert "<p>keep</p>" in out


def b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


DATA_PNG = f"data:image/png;base64,{b64(PNG)}"


def test_our_csp_and_page_size_lead_and_close_the_document(work):
    out, _ = clean('<html><head><meta http-equiv="Content-Security-Policy" content="default-src *">'
                   '<style>@page { size: A4 }</style></head><body><p>x</p></body></html>', work)
    assert out.startswith("<!doctype html>")
    head = out[:400]
    assert f'<meta http-equiv="Content-Security-Policy" content="{CSP}">' in head
    assert CSP == "default-src 'none'; img-src data:; style-src 'unsafe-inline'"  # R46: no file:; M3: no fonts
    assert "default-src *" not in out  # Codex's own CSP is gone
    assert out.rstrip().endswith("</style>") and "@page{size:1280px 720px;margin:0}" in out[-300:]


@pytest.mark.parametrize("src", [
    "https://evil.example/a.png", "http://x/a.png", "//evil.example/a.png", "file:///etc/passwd",
    "/etc/passwd", "../outside.png", "sub/../../outside.png", "%2e%2e/outside.png", "img1.png?x=1",
    "javascript:alert(1)", " java\tscript:alert(1)", "data:text/html,<script>1</script>",
    "data:image/svg+xml;base64,PHN2Zz4=", "missing.png", "C:\\x.png", "sub\\..\\..\\x.png",
])
def test_bad_image_sources_are_dropped(work, src):
    out, removed = clean(f'<img src="{src}" alt="a">', work)
    assert "src=" not in out.split("<img", 1)[1].split(">", 1)[0]
    assert removed["url"] == 1


def test_local_images_are_inlined_as_data_uris(work):
    """R46: the render has no file: access at all; every image the deck shows is inlined after its checks."""
    small = "data:image/png;base64,iVBORw0KGgo="
    out, _ = clean(f'<img src="img1.png"><img src="./sub/img2.png"><img src="{small}">'
                   '<svg><image href="img1.png"/><rect fill="url(#grad)"/></svg>', work)
    assert out.count(f'src="{DATA_PNG}"') == 2 and f'src="{small}"' in out
    assert f'<image href="{DATA_PNG}"></image>' in out
    assert 'fill="url(#grad)"' in out
    assert "img1.png" not in out and "file:" not in out


def test_only_real_images_of_at_most_8_mb_are_inlined(work):
    (work / "fake.png").write_bytes(b"<svg onload=alert(1)>" + b"\0" * 100)
    (work / "big.png").write_bytes(PNG + b"\0" * (8 * 1024 * 1024))
    (work / "notes.txt").write_bytes(b"secret")
    out, removed = clean('<img src="fake.png"><img src="big.png"><img src="notes.txt">'
                         f'<img src="data:image/png;base64,{b64(b"not-an-image-at-all")}">', work)
    assert "data:" not in out.split(CSP, 1)[1] and removed["url"] == 4


@pytest.mark.parametrize("img", [(b"\xff\xd8\xff\xe0" + b"\0" * 64, "jpeg"), (b"GIF89a" + b"\0" * 64, "gif"),
                                 (b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 64, "webp")])
def test_jpeg_gif_and_webp_are_inlined_with_their_own_type(work, img):
    data, kind = img
    (work / "pic.bin").write_bytes(data)
    out, _ = clean('<img src="pic.bin">', work)
    assert f'src="data:image/{kind};base64,{b64(data)}"' in out


def test_links_and_uses_may_only_point_at_fragments(work):
    out, removed = clean('<a href="img1.png">x</a><svg><image href="img1.png#a"/></svg>', work)
    assert "href=" not in out.split(CSP, 1)[1] and removed["url"] == 2


def test_symlinks_out_of_the_work_dir_are_refused(work, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    os.symlink(secret, work / "link.png")
    os.symlink(tmp_path, work / "linkdir")
    out, removed = clean('<img src="link.png"><img src="linkdir/secret.png">', work)
    assert "link" not in out and removed["url"] == 2


def test_a_symlink_inside_the_work_dir_is_refused_too(work):
    os.symlink(work / "img1.png", work / "alias.png")
    out, _ = clean('<img src="alias.png">', work)
    assert "alias" not in out


def test_generated_image_paths_are_rewritten_to_their_local_copies(work, tmp_path):
    gen = tmp_path / "generated_images" / "t1" / "exec-1.png"
    aliases = {str(gen): "img1.png"}
    out, removed = clean(f'<img src="{gen}"><img src="file://{gen}"><div style="background:url(\'{gen}\')"></div>',
                         work, aliases=aliases)
    assert out.count(DATA_PNG) == 3 and str(gen) not in out and not removed.get("url")


def test_links_never_lead_out(work):
    out, _ = clean('<a href="https://evil.example">x</a><a href="#s2">y</a><a href="mailto:a@b.c">z</a>', work)
    assert "evil" not in out and "mailto" not in out and 'href="#s2"' in out


def test_css_imports_and_remote_urls_are_removed(work):
    css = ("@import url(https://evil.example/a.css); @IMPORT 'x.css';"
           ".a{background:url(https://evil.example/a.png) center} .b{background-image:url('img1.png')}"
           ".c{background:image-set('file:///etc/x.png' 1x)} .d{behavior:url(x.htc)} .e{width:expression(alert(1))}"
           ".f{background:url(\"file:///Users/cb/.ssh/id_rsa\")}")
    out, removed = clean(f"<style>{css}</style><div style=\"background:url(https://evil.example/b.png)\">x</div>", work)
    low = out.lower()
    assert "@import" not in low and "evil" not in low and "/etc/" not in low and ".ssh" not in low
    assert "expression" not in low and "image-set" not in low
    assert f"url({DATA_PNG})" in out
    assert removed["css-import"] == 2 and removed["url"] >= 4


def test_css_escapes_and_comments_cannot_hide_a_url_or_import(work):
    css = r".a{background:u\72 l(https://evil.example/x.png)} @\69mport 'x.css'; .b{background:ur/**/l(https://evil.example)}"
    out, _ = clean(f"<style>{css}</style>", work)
    assert not re.search(r"url\s*\(\s*['\"]?https", out, re.I) and not re.search(r"@\s*import", out, re.I)
    assert "ur l(" in out  # R48: a comment is a space, as in CSS itself: never glued into url(


def test_css_glyph_escapes_survive_as_characters(work):
    out, _ = clean(r'<style>.q{font-family:"\201C Serif"}</style>', work)
    assert "\u201c" in out


def test_style_text_cannot_close_its_tag(work):
    out, _ = clean('<style>.a{content:"</sty" "le><script>x</script>"}</style>', work)
    assert "<script" not in out.lower()


def test_javascript_urls_in_any_attribute_are_removed(work):
    out, _ = clean('<div data-x="javascript:alert(1)" class="ok">x</div><svg><a xlink:href="javascript:1">y</a></svg>',
                   work)
    assert "javascript" not in out.lower() and 'class="ok"' in out


def test_text_and_attributes_are_reescaped(work):
    out, _ = clean('<p data-k="a&quot;b">1 &lt; 2 &amp; <b>bold</b></p>', work)
    assert 'data-k="a&quot;b"' in out and "1 &lt; 2 &amp; <b>bold</b>" in out


def test_comments_and_declarations_are_dropped(work):
    out, _ = clean('<!-- secret --><![CDATA[x]]><p>a</p>', work)
    assert "secret" not in out and "CDATA" not in out


def test_unclosed_dangerous_tag_swallows_the_rest(work):
    out, _ = clean("<p>a</p><iframe><p>b</p>", work)
    assert "<p>a</p>" in out and "<p>b</p>" not in out


# -- the security review of 4ecb2b7 -------------------------------------------------------------------------------


def test_a_self_closing_style_is_dropped_with_what_follows_it(work):
    """HIGH: browsers ignore the slash, so `<style/>` opens a style element whose CSS skipped _clean_css."""
    out, removed = clean("<p>a</p><style/>@import url(https://evil.example/x.css); .a{background:"
                         "url(https://evil.example/y.png)}</style><p>b</p>", work)
    assert "evil" not in out and "@import" not in out and "<p>a</p>" in out and "<p>b</p>" in out
    assert removed["tag:style"] == 1


def test_no_non_void_element_is_ever_self_closed(work):
    out, _ = clean('<div/><svg><path d="M0 0"/><title/></svg><br/><img src="img1.png"/>', work)
    assert "/>" not in out
    assert "<div></div>" in out and '<path d="M0 0"></path>' in out and "<br>" in out


def test_an_unclosed_style_is_dropped(work):
    out, _ = clean("<p>a</p><style>.a{color:red}", work)
    assert "color:red" not in out and out.count("<style>") == 1  # only our page-size rule


def test_character_references_in_style_text_are_decoded_before_cleaning(work):
    """MEDIUM: an SVG <style> is parsed as normal markup, so the browser decodes &#64; and &#40; after us."""
    out, _ = clean("<style>&#64;import 'x.css'; .a{background:url&#40;https://evil.example/a.png)}"
                   ".b{font-family:'A&amp;B'}</style>", work)
    assert "evil" not in out and "@import" not in out and "&#" not in out
    assert "\\26 " in out  # every & in CSS output is a CSS escape, so no reference survives to be decoded


def test_style_inside_svg_or_math_is_dropped(work):
    out, removed = clean("<svg><style>.a{fill:red}</style><rect/></svg><math><style>.b{}</style></math>", work)
    assert "fill:red" not in out and ".b{}" not in out and removed["tag:style"] >= 1


@pytest.mark.parametrize("attr", [r'fill="u\72l(https://evil.example/a.png)"', r'filter="\75rl(https://evil.example)"',
                                  r'stroke="\000075rl(x)"'])
def test_css_escapes_in_presentation_attributes_are_dropped(work, attr):
    """MEDIUM: SVG presentation attributes are CSS too; a backslash could hide url(."""
    out, removed = clean(f"<svg><rect {attr}/></svg>", work)
    assert "evil" not in out and "\\" not in out.split(CSP, 1)[1] and removed["attribute"] + removed["css-blind"] >= 1


def test_presentation_attributes_with_functions_are_cleaned(work):
    out, _ = clean('<svg><rect fill="url(https://evil.example/a.png)" transform="translate(10,20)"/>'
                   '<rect transform="translate(10,20) rotate(45)"/></svg>', work)
    assert "evil" not in out and 'transform="translate(10,20)"' in out and "rotate" not in out  # R50a


def test_file_urls_and_outside_images_never_reach_the_page(work, tmp_path):
    """R46: no reference to a local file survives except as checked inline data."""
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    out, _ = clean(f'<img src="file://{outside}"><img src="{outside}"><img src="../outside.png">'
                   f'<div style="background:url(file:///Users/cb/.ssh/id_rsa)"></div>'
                   f'<svg><image href="file:///Users/cb/x.png"/></svg>', work)
    assert "file:" not in out and "outside" not in out and "/Users/" not in out and "data:image" not in out


# -- R47: CSS that shows text the HTML doesn't hold, or changes its case ---------------------------------------


@pytest.mark.parametrize("css", [
    '.a::before{content:"$99"}', ".a::after{content:attr(data-x)}", ".a::before{content:counter(s)}",
    '.a::before{content:counters(s, ".")}', ".a::before{content:open-quote}", ".a::after{content:close-quote}",
    '.a{quotes:"99" "%"}', '@counter-style x{system:cyclic;symbols:"$99"}', '.a{list-style:"99% "}',
    '.a{list-style-type:"99"}', ".a{text-transform:uppercase}", ".a{font-variant:small-caps}",
    ".a{font-variant-caps:all-small-caps}", '.a{font-feature-settings:"smcp"}', ".a{font:small-caps 12px serif}",
    ".a{counter-reset:s 98}", ".a{counter-increment:s}", ".a{-webkit-text-transform:uppercase}",
])
def test_css_text_and_case_features_are_stripped(work, css):
    out, removed = clean(f'<style>{css}</style><div class="a" style="{css.split("{",1)[1].rstrip("}")}">x</div>', work)
    body = out.split(CSP, 1)[1].lower()
    for bad in ("$99", "attr(", "counter", "quote", "99%", "text-transform", "small-caps", "smcp", "font-variant"):
        assert bad not in body
    assert removed["css-text"] >= 1


@pytest.mark.parametrize("css", ['.a::before{content:""}', ".a::before{content:none}", ".a{color:red;font-weight:900}",
                                 ".a{list-style:none}", "a:hover{color:red}", "@media (max-width:100px){.a{color:red}}"])
def test_harmless_css_is_kept(work, css):
    out, removed = clean(f"<style>{css}</style>", work)
    assert css.replace(" ", "") in out.replace(" ", "") and not removed.get("css-text")


# -- R48 ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("css", [".a{--x:url(https://evil.example/a.png)}", ".a{--c: \"$99\"}",
                                 ".a{background:var(--x)}", ".a{content:var(--c)}", ".a{padding:env(safe-area-inset-top)}",
                                 ":root{--bg:red}.a{color:var(--bg, blue)}"])
def test_custom_properties_var_and_env_are_dropped(work, css):
    out, removed = clean(f"<style>{css}</style><div style=\"{css.split('{',1)[1].split('}',1)[0]}\">x</div>", work)
    body = out.split(CSP, 1)[1].lower()
    assert "--" not in body and "var(" not in body and "env(" not in body and "evil" not in body
    assert removed["css-text"] >= 1


@pytest.mark.parametrize("css", [
    "@imp@import x;ort 'https://evil.example/a.css';",          # removing the inner @import rebuilds the outer
    "@im/**/port 'https://evil.example/a.css';",                 # a comment is a space, never glue
    ".a{background:ur\\6c(https://evil.example)}",              # an escape decoded on a later pass
    ".a{background:u\\72\\6c\\28 https://evil.example)}",
])
def test_removal_can_never_rebuild_a_blocked_construct(work, css):
    out, _ = clean(f"<style>{css}</style>", work)
    assert not re.search(r"url\s*\(\s*['\"]?https", out, re.I) and not re.search(r"@\s*import", out, re.I)


def test_a_block_that_never_settles_is_dropped(work, monkeypatch):
    from haseeb_agent.deck import sanitize

    calls = []

    def endless(css, *a):
        calls.append(1)
        return css + "x"

    monkeypatch.setattr(sanitize, "_clean_pass", endless)
    out, removed = clean("<style>.a{color:red}</style><p style=\"color:red\">t</p>", work)
    assert "color:red" not in out and removed["css-unstable"] == 2 and len(calls) == 2 * sanitize.CSS_PASSES


def test_a_leftover_import_or_url_drops_the_whole_block(work, monkeypatch):
    from haseeb_agent.deck import sanitize

    monkeypatch.setattr(sanitize, "_clean_pass", lambda css, *a: "@import 'x'; .a{color:red}")
    out, removed = clean("<style>.a{color:red}</style>", work)
    assert "color:red" not in out and removed["css-unstable"] == 1


def test_each_image_is_read_once_and_references_are_capped(work, monkeypatch):
    from haseeb_agent.deck import sanitize

    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self.name) or real(self))
    out, removed = clean("".join('<img src="img1.png">' for _ in range(45)), work)
    assert reads.count("img1.png") == 1
    assert out.count(DATA_PNG) == sanitize.MAX_IMAGE_REFS == 40 and removed["url"] == 5


def test_inlined_bytes_are_capped_per_deck(work, monkeypatch):
    from haseeb_agent.deck import sanitize

    monkeypatch.setattr(sanitize, "MAX_INLINE_BYTES", 3 * len(DATA_PNG))
    out, removed = clean('<img src="img1.png"><img src="img1.png"><div style="background:url(img1.png)"></div>'
                         '<img src="sub/img2.png">', work)
    assert out.count(DATA_PNG) == 3 and removed["url"] == 1


def test_text_only_attributes_are_blanked(work):
    """Live run 1: Codex's alt text described its illustrations in words the insight doesn't have. Alt, title and
    aria text never show on a printed slide, but Chrome can carry them into the PDF's tags: blank them."""
    out, removed = clean('<img src="img1.png" alt="A golden cube" title="t1"><div aria-label="a1" '
                         'aria-description="d1" aria-roledescription="r1" placeholder="p1">x</div>', work)
    body = out.split(CSP, 1)[1]
    assert 'alt=""' in body
    for gone in ("golden", "t1", "a1", "d1", "r1", "p1", "title=", "aria-", "placeholder"):
        assert gone not in body
    assert removed["text-attribute"] == 6


def test_letter_and_word_spacing_are_stripped_and_reset(work):
    """Live run 1: tight tracking split a big number in the PDF reading; spacing never decides what a reading sees."""
    out, removed = clean('<style>.a{letter-spacing:-6px;color:red}.b{word-spacing:20px}</style>'
                         '<p style="letter-spacing:4px">x</p><svg><text letter-spacing="9" word-spacing="9">t</text></svg>',
                         work)
    body = out.split(CSP, 1)[1].split("</body>")[0]
    assert "spacing" not in body and "color:red" in body and removed["css-text"] >= 3
    assert "*{letter-spacing:normal!important;word-spacing:normal!important}" in out.split("</body>")[1]


@pytest.mark.parametrize("decl, out", [("font-weight:750", "font-weight:800"), ("font-weight:650", "font-weight:700"),
                                       ("font-weight:640", "font-weight:600"), ("font-weight:1000", "font-weight:900"),
                                       ("font-weight:50", "font-weight:100"), ("font-weight:bold", "font-weight:bold"),
                                       ("font:750 112px/1 Arial", "font:800 112px/1 Arial")])
def test_font_weights_snap_to_a_multiple_of_100(work, decl, out):
    """Live run 1: SF Pro at weights 650 and 750 made pypdf read "$1.99" as "$1.9 9" in the full deck; standard
    weights read cleanly."""
    html, _ = clean(f"<style>.a{{{decl}}}</style><p style=\"{decl}\">x</p><svg><text font-weight=\"750\">t</text></svg>",
                    work)
    assert html.count(out) == 2 and 'font-weight="800"' in html


def test_font_variation_settings_are_dropped(work):
    out, removed = clean("<style>.a{font-variation-settings:'wght' 750}</style>", work)
    assert "variation" not in out and removed["css-text"] == 1


# -- R50a, C2, M3, M4 (the designer-deck review) ----------------------------------------------------------------


@pytest.mark.parametrize("decl", [
    "opacity:.95", "filter:blur(0.1px)", "backdrop-filter:blur(2px)", "-webkit-backdrop-filter:blur(2px)",
    "mix-blend-mode:multiply", "mask-image:linear-gradient(#000,#000)", "-webkit-mask-image:url(img1.png)",
    "mask:none", "clip-path:inset(0)", "-webkit-clip-path:inset(0)", "transform:rotate(180deg)",
    "transform:scaleX(-1)", "transform:matrix(-1,0,0,1,0,0)", "transform:translateX(4px) rotate(1deg)",
    "-webkit-transform:skewX(10deg)", "rotate:180deg", "scale:-1 1", "writing-mode:vertical-rl",
    "text-orientation:upright", "direction:rtl", "unicode-bidi:bidi-override", "-webkit-box-reflect:below",
    "offset-rotate:180deg", "animation:spin 1s", "-webkit-background-clip:text", "background-clip:text",
    "perspective:10px", "-epub-writing-mode:vertical-rl", "-epub-text-orientation:upright",
    "text-shadow:0 2px 4px #000",  # R50b: Chrome draws a shadow as more text in the PDF
])
def test_css_the_pdf_reading_is_blind_to_is_stripped(work, decl):
    out, removed = clean(f"<style>.a{{{decl};color:red}}</style><p style=\"{decl}\">x</p>", work)
    body = out.split(CSP, 1)[1].split("</body>")[0]
    prop = decl.split(":")[0]
    assert prop not in body and body.count("color:red") == 1 and removed["css-blind"] == 2, body


@pytest.mark.parametrize("decl", ["transform:translate(10px,20px)", "transform:translateY(-4px)", "transform:none",
                                  "box-shadow:0 0 8px red",
                                  "background:linear-gradient(#fff,#000)", "background-clip:padding-box",
                                  "color:rgba(0,0,0,.8)"])
def test_design_tools_stay(work, decl):
    out, removed = clean(f"<p style=\"{decl}\">x</p>", work)
    assert decl in out and not removed["css-blind"]


@pytest.mark.parametrize("attr", ['opacity=".5"', 'filter="url(#f)"', 'mask="url(#m)"', 'clip-path="url(#c)"',
                                  'transform="rotate(180)"', 'transform="scale(-1,1)"', 'transform="matrix(1 0 0 1 0 0)"',
                                  'rotate="180"', 'writing-mode="tb"', 'direction="rtl"', 'unicode-bidi="bidi-override"'])
def test_svg_attributes_the_pdf_reading_is_blind_to_are_dropped(work, attr):
    out, removed = clean(f"<svg><text x=\"1\" {attr}>Argon</text></svg>", work)
    assert attr.split("=")[0] + "=" not in out.split(CSP, 1)[1] and removed["css-blind"] == 1


def test_dir_rtl_is_dropped(work):
    out, removed = clean('<p dir="rtl">Argon</p><bdo dir="rtl">Argon</bdo>', work)
    assert "dir=" not in out.split(CSP, 1)[1] and removed["css-blind"] == 2


@pytest.mark.parametrize("ch", ["\u200b", "\u00ad", "\u2060", "\u200c", "\u200d", "\ufeff", "\u180e", "\u202e",
                                "\u2066", "\ufe0f", "\u034f", "\U000e0101"])
def test_invisible_characters_are_deleted_from_text(work, ch):
    out, _ = clean(f"<p>Open{ch}AI</p>", work)
    assert "<p>OpenAI</p>" in out


def test_wbr_and_soft_hyphen_entities_are_dropped(work):
    out, _ = clean("<p>Open<wbr>AI &shy;and Deep&#x200b;Seek</p>", work)
    assert "<p>OpenAI and DeepSeek</p>" in out


def test_meta_is_always_dropped_because_the_csp_does_not_cover_meta_refresh(work):
    """M4: <meta http-equiv="refresh"> navigates even under default-src 'none' (the reviewer's net.py)."""
    out, removed = clean('<meta http-equiv="refresh" content="0;url=https://evil.example/r"><meta charset="x">'
                         '<svg><meta http-equiv="refresh" content="0"/></svg><p>x</p>', work)
    body = out.split(CSP, 1)[1]
    assert "<meta" not in body and "refresh" not in out and removed["tag:meta"] == 3


def test_no_font_source_is_allowed(work):
    """M3: a data: font could map one glyph to another letter's shape."""
    out, _ = clean("<style>@font-face{font-family:Z;src:url(data:font/woff2;base64,AAAA)}</style>", work)
    assert "font-src" not in out and "data:font" not in out


def test_title_desc_and_metadata_never_reach_the_page(work):
    out, _ = clean("<title>DeepSeek</title><svg><title>OpenAI</title><desc>Claude</desc><metadata>x</metadata>"
                   "<text>Argon</text></svg>", work)
    for gone in ("DeepSeek", "OpenAI", "Claude", "metadata"):
        assert gone not in out


@pytest.mark.parametrize("tag", ["pattern", "mask", "clipPath", "symbol", "marker", "foreignObject", "use"])
def test_svg_paint_servers_and_references_go_with_their_text(work, tag):
    """N1(a): text inside an SVG <pattern> (and the like) is painted as a tile the PDF reading never sees; <use>
    can draw any of them. The element goes with its content."""
    out, removed = clean(f'<svg><{tag} id="p"><text>OpenAI $420</text></{tag}><rect fill="url(#p)"/></svg>'
                         '<p>after</p>', work)
    body = out.split(CSP, 1)[1]
    assert "OpenAI" not in body and "$420" not in body and "after" in body and removed[f"tag:{tag.lower()}"] == 1
    assert f"<{tag.lower()}" not in body.lower()


def test_svg_text_under_defs_goes_but_gradients_stay(work):
    """N1(a): <defs> content is drawn only by reference; text in it is never in a direct render context."""
    out, removed = clean('<svg><defs><linearGradient id="g"><stop offset="0" stop-color="red"/></linearGradient>'
                         '<text id="t">OpenAI<tspan>$420</tspan></text><g><textPath>x9</textPath></g></defs>'
                         '<rect fill="url(#g)"/><text>Argon</text></svg>', work)
    body = out.split(CSP, 1)[1]
    assert "<lineargradient" in body.lower() and 'fill="url(#g)"' in body and "<text>Argon</text>" in body
    assert "OpenAI" not in body and "$420" not in body and "x9" not in body and removed["tag:text"] == 1


def test_a_pattern_probe_slide_is_clean_and_refused(work):
    """The re-review's p5.py slide: the pattern's "$4", "20", "Open" and "AI" tiles go entirely."""
    vis = ('<p style="position:absolute;top:600px;font-size:22px">Open the AI tab. Intro price $2 and $10; on Nov 30 '
           'it rises to $4 and $20. Next: Index.</p>')
    body = ('<svg width="1280" height="500"><defs><pattern id="p" width="1280" height="500" '
            'patternUnits="userSpaceOnUse"><text x="20" y="180" font-size="140">$4</text>'
            '<text x="176" y="180" font-size="140">20</text><text x="20" y="400" font-size="140">Open</text>'
            '<text x="345" y="400" font-size="140">AI</text></pattern></defs>'
            '<rect width="1280" height="500" fill="url(#p)"/></svg>' + vis)
    out, removed = clean(f'<section class="slide">{body}</section>', work)
    assert "<pattern" not in out and out.count("$4") == 1 and removed["tag:pattern"] == 1


def test_with_provenance_only_the_generators_copies_are_inlined(work):
    """Image provenance: in the designer, an image is shown only if it is one of the generator's copies, with the
    bytes it had when it was copied. Codex's own files and data: URIs never are: an image is not text-checked, so
    it must not be something Codex could paint itself."""
    import hashlib
    (work / "img2.png").write_bytes(PNG + b"own")  # written by Codex itself
    prov = {"img1.png": hashlib.sha256(PNG).hexdigest()}
    out, removed = clean(f'<img src="img1.png"><img src="img2.png"><img src="{DATA_PNG}">'
                         '<div style="background:url(img2.png)">x</div>', work, provenance=prov)
    body = out.split(CSP, 1)[1]
    assert body.count(DATA_PNG) == 1 and "img2.png" not in body and removed["image-provenance"] >= 3
    (work / "img1.png").write_bytes(PNG + b"overwritten by a repair turn")
    out, removed = clean('<img src="img1.png">', work, provenance=prov)
    assert "data:image" not in out.split(CSP, 1)[1] and removed["image-provenance"] == 1
