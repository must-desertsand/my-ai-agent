"""Designer decks: rebuild Codex's deck.html tag by tag, keeping only what is safe to print.

Codex's HTML is model output, so it is treated like data. `sanitize_html` parses it with the standard library's
HTMLParser and writes a new document:
- dropped with their content: script, iframe, object, embed, frames, media, forms, noscript, template, math,
  foreignObject, SVG animation (`set`, `animate*` can rewrite an href) and the like; dropped tags: link, meta, base,
  source, input...; comments and declarations go too;
- `<style>` survives only as a properly closed element outside svg/math. A self-closing `<style/>` (browsers ignore
  the slash) is dropped with everything up to its `</style>`, an unclosed one is dropped, and one inside svg is
  dropped (there the browser decodes character references after us). Style text is HTML-unescaped before it is
  cleaned, and every `&` and `<` in the CSS we write is a CSS escape, so no reference survives to be decoded later;
- no element is written self-closed: `<x/>` becomes `<x></x>` (void elements stay `<br>`), so a slash can never
  mean one thing to us and another to Chrome;
- `alt` is blanked and `title`, `aria-label` and similar text-only attributes go: a printed slide never shows them,
  but Chrome can carry them into the PDF's tags, where the fact check's PDF reading would not see them;
- every `on*` handler goes, and so does any attribute whose value holds `javascript:`, `vbscript:` or `livescript:`
  (after removing whitespace and control characters, which browsers ignore there). A non-style attribute with a
  backslash goes (SVG presentation attributes are CSS, where `u\\72l(` is `url(`); one with `(` is cleaned as CSS;
- R46: the page gets no file access at all. Every image it shows is inlined as a `data:image/...;base64` URI: an
  `<img src>`, an SVG `<image href>` or a CSS `url()` must name a plain file inside the work dir (relative, no `..`,
  no symlink anywhere on the path, no query) of at most 8 MB whose magic bytes say PNG, JPEG, GIF or WebP; a data
  URI from Codex is decoded and checked the same way. Every other `href` may only be a `#fragment` (SVG gradients,
  `<use>`). Paths into Codex's own generated_images folder are first rewritten to their copies in the work dir
  (`aliases`). Anything else is dropped;
- CSS (style tags and style attributes): comments are stripped and escapes decoded first, so `u\\72 l(` or
  `@\\69mport` cannot hide; then every `@import` goes, `url()` is checked as above, and `image-set`, `cross-fade`,
  `element`, `paint`, `expression`, `behavior` and `-moz-binding` go;
- R47: CSS that makes the page show text that is not in the HTML goes too, so the fact check's two readings agree:
  `content:` (except `""`, `''`, `none`, `normal`), anything using `attr()`, `counter()`, `counters()`,
  `open-quote`/`close-quote`, `quotes:`, `@counter-style`, `list-style` with a string, counter properties, and the
  case changers `text-transform`, `font-variant*`, `font-feature-settings` and `small-caps` in `font`. Live run 1
  added `letter-spacing` and `word-spacing`: pypdf read a big number with tight tracking as two ("$1.9 9"), so
  spacing is stripped and reset to normal, and the PDF reading sees the words and numbers the slide shows.

- R50a: CSS and SVG attributes that hide text from the PDF reading go: `opacity`, `filter`, `backdrop-filter`,
  `mix-blend-mode`, `mask*`, `clip-path`, `clip`, any `transform` other than translations, `rotate`, `scale`,
  `perspective`, `box-reflect`, `offset*`, `animation*`, `writing-mode`, `text-orientation`, `direction`,
  `unicode-bidi`, `background-clip: text`, SVG `rotate` and the HTML `dir` attribute. `<wbr>`, `<title>`, `<desc>`
  and `<metadata>` go, and text nodes lose Unicode format and default-ignorable characters (C2). R50b: `text-shadow`
  goes too (Chrome draws the shadow as more text in the PDF); `box-shadow` stays.

The result starts with our own doctype, charset and CSP (`img-src data:` only, no fonts) and ends with the page size rule, so
Codex's styles can change neither. Returns (html, removed counts by type) for the log, never text.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"  # M3: no fonts at all, so no glyph remapping
PAGE_CSS = ("@page{size:1280px 720px;margin:0}"
            "*{-webkit-print-color-adjust:exact;print-color-adjust:exact}"
            "*{letter-spacing:normal!important;word-spacing:normal!important}")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_INLINE_BYTES = 32 * 1024 * 1024  # R48: all inlined data in one deck, counted per reference
MAX_IMAGE_REFS = 40
CSS_PASSES = 5

DROP_WITH_CONTENT = frozenset({
    "script", "iframe", "object", "embed", "applet", "frame", "frameset", "noframes", "noscript", "template",
    "video", "audio", "canvas", "form", "button", "select", "textarea", "dialog", "portal", "fencedframe",
    "set", "animate", "animatetransform", "animatemotion", "animatecolor", "discard", "handler", "listener",
    "math", "xmp", "plaintext", "noembed", "slot", "foreignobject",
    # never rendered on a page: their text would only be in the HTML reading (the coverage gate would refuse it)
    "title", "desc", "metadata",
    # N1(a): drawn only by reference, as a tile, mask, clip, symbol instance or marker that the PDF reading never
    # sees (the re-review's pattern probe), and <use>, which draws any referenced element; <textPath> lays glyphs
    # along a path, rotated (R50a)
    "pattern", "mask", "clippath", "symbol", "marker", "use", "textpath",
})
# N1(a): inside <defs> nothing is in a direct render context, so text there goes too; gradients and filters stay
DEFS_TEXT = frozenset({"text", "tspan", "textpath"})
# M4: <meta> always goes. Our CSP does not cover <meta http-equiv="refresh">, which navigates even under
# default-src 'none' (the reviewer's net.py: the unsanitized vectors under our CSP head still fetched /refresh).
DROP_TAG = frozenset({"link", "meta", "base", "source", "track", "param", "input", "keygen", "isindex", "html",
                      "head", "body", "wbr"})
VOID = frozenset({"area", "br", "col", "hr", "img", "wbr"})
IMAGE_REFS = frozenset({("img", "src"), ("image", "href"), ("image", "xlink:href"), ("feimage", "href"),
                        ("feimage", "xlink:href")})
URL_ATTRS = frozenset({"src", "href", "xlink:href", "poster"})
TEXT_ONLY_ATTRS = frozenset({"alt", "title", "aria-label", "aria-description", "aria-roledescription",
                             "aria-placeholder", "aria-valuetext", "placeholder"})
DROP_ATTRS = frozenset({"srcset", "imagesrcset", "action", "formaction", "data", "codebase", "background",
                        "lowsrc", "dynsrc", "ping", "longdesc", "usemap", "manifest", "cite", "xml:base",
                        "attributename", "srcdoc", "sandbox", "allow", "http-equiv", "content"})
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
_ATTR_NAME = re.compile(r"[a-zA-Z_][-a-zA-Z0-9_:.]*")
_TAG_NAME = re.compile(r"[a-zA-Z][-a-zA-Z0-9]*")
_SCRIPT_URL = re.compile(r"(?:java|vb|live)script:|data:text|data:application")
_DATA_IMAGE = re.compile(r"data:image/(?:png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=\s]*)", re.IGNORECASE)
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")  # a plain file name: nothing to quote in CSS or HTML
_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:")
_FRAGMENT = re.compile(r"#[A-Za-z_][-A-Za-z0-9_:.]*")
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", re.DOTALL)
_CSS_IMPORT = re.compile(r"@import\b[^;{}]*;?", re.IGNORECASE)
_CSS_URL = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^)\s]*))\s*\)", re.IGNORECASE)
_CSS_BAD_FN = re.compile(r"(?:-webkit-|-moz-)?(?:image-set|cross-fade|element|paint|expression)\s*\([^)]*\)?",
                         re.IGNORECASE)
_CSS_BAD_PROP = re.compile(r"(?:behavior|-moz-binding)\s*:[^;}]*;?", re.IGNORECASE)
_CSS_COUNTER_STYLE = re.compile(r"@counter-style\b[^{]*\{[^}]*\}?", re.IGNORECASE)
_CSS_DECL = re.compile(r"(?P<prop>--[-\w]*|-?[a-zA-Z][-a-zA-Z]*)\s*:\s*(?P<val>[^;{}]*)(?:;|(?=\})|$)")
_VAR = re.compile(r"\b(?:var|env)\s*\(", re.IGNORECASE)
_TEXT_MAKERS = re.compile(r"\b(?:attr|counters?)\s*\(|\b(?:no-)?(?:open|close)-quote\b", re.IGNORECASE)
_CASE_PROPS = frozenset({"font-variation-settings", "letter-spacing", "word-spacing", "text-transform", "font-variant", "font-variant-caps", "font-feature-settings",
                         "font-variant-alternates", "font-synthesis-small-caps", "quotes", "counter-reset",
                         "counter-increment", "counter-set"})
_CSS_SYNTAX = set("\"'\\<>(){};:@/*&")

# R50a: properties that render text in a way pypdf's PDF reading cannot see (a Form XObject for opacity and blend
# modes, a raster for filters and masks, a clip, a rotated or mirrored matrix, reordered glyphs), so the authoritative
# PDF check would be blind to it. R50b adds text-shadow. Colors, gradients, images and box-shadow stay.
_BLIND_PROPS = frozenset({"opacity", "filter", "backdrop-filter", "mix-blend-mode", "clip-path", "clip",
                          "writing-mode", "text-orientation", "direction", "unicode-bidi", "rotate", "scale",
                          "perspective", "box-reflect", "glyph-orientation-vertical", "glyph-orientation-horizontal",
                          "text-combine-upright",
                          # R50b: Chrome draws a text-shadow as more copies of the text in the PDF (false refusals,
                          # and spare fragments for the coverage gate); box-shadow stays
                          "text-shadow"})
_BLIND_PREFIXES = ("mask", "offset", "animation")
_TRANSLATE_ONLY = re.compile(r"\s*(?:none|(?:translate(?:x|y|z|3d)?\([^()]*\)\s*)+)\s*", re.IGNORECASE)
# SVG presentation attributes with the same effect (rotate is per-glyph rotation on <text>; dir reverses HTML text)
BLIND_ATTRS = frozenset({"opacity", "filter", "mask", "clip-path", "writing-mode", "direction", "unicode-bidi",
                         "rotate", "dir", "glyph-orientation-vertical", "glyph-orientation-horizontal"})

# C2: format characters and other default-ignorables are invisible but split a token in one reading only
_IGNORABLE_EXTRA = frozenset(
    [chr(c) for c in (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180E, 0x180F, 0x3164, 0xFFA0)]
    + [chr(c) for c in range(0xFE00, 0xFE10)] + [chr(c) for c in range(0xE0100, 0xE01F0)])


def strip_ignorable(text: str) -> str:
    """Delete Unicode format (Cf) characters and the other default-ignorables: U+200B, U+00AD, U+2060, U+200C/D,
    U+FEFF, U+180E, bidi controls, variation selectors..."""
    return "".join(c for c in text if c not in _IGNORABLE_EXTRA and unicodedata.category(c) != "Cf")


def _blind(prop: str, val: str) -> bool:
    p = re.sub(r"^-(?:webkit|moz|ms|o|epub)-", "", prop)  # N3: -epub-writing-mode is Chrome's alias too
    if p in _BLIND_PROPS or p.startswith(_BLIND_PREFIXES):
        return True
    if p == "transform":
        return not _TRANSLATE_ONLY.fullmatch(val)
    return p in ("background-clip", "background") and re.search(r"\btext\b", val, re.IGNORECASE) is not None


def _squash(value: str) -> str:
    """The value as a browser's URL check sees it: no whitespace or control characters, lower case."""
    return "".join(c for c in value if c > " " and c != "\x7f").lower()


def image_kind(data: bytes) -> str | None:
    """'png' | 'jpeg' | 'gif' | 'webp' from the magic bytes, else None."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _data_uri(data: bytes) -> str | None:
    kind = image_kind(data)
    if kind is None or not 0 < len(data) <= MAX_IMAGE_BYTES:
        return None
    return f"data:image/{kind};base64,{base64.b64encode(data).decode('ascii')}"


class _Refs:
    def __init__(self, work: Path, aliases: dict[str, str] | None, provenance: dict[str, str] | None = None):
        self.work = Path(work).resolve()
        self.aliases = {str(Path(k)): v for k, v in (aliases or {}).items()}
        # Image provenance: {work-dir name: sha256 of the bytes the generator copy step wrote}. When given, only those
        # files, still holding those bytes, are shown; data: URIs and every other file are dropped.
        self.provenance = provenance
        self.rejected = 0
        self._inlined: dict[str, str | None] = {}  # each distinct image is read, checked and encoded once
        self.refs = 0
        self.bytes = 0

    def image(self, value: str) -> str | None:
        """An image reference as a checked data URI, or None to drop it (also beyond the deck's caps, R48)."""
        uri = self._resolve(value)
        if uri is None:
            return None
        if self.refs + 1 > MAX_IMAGE_REFS or self.bytes + len(uri) > MAX_INLINE_BYTES:
            return None
        self.refs += 1
        self.bytes += len(uri)
        return uri

    def _resolve(self, value: str) -> str | None:
        v = (value or "").strip()
        if not v:
            return None
        m = _DATA_IMAGE.fullmatch(v)
        key = v if m else self._local(v)
        if key is None and not m:
            return None
        if self.provenance is not None and (m or key not in self.provenance):
            self.rejected += 1
            return None
        if key not in self._inlined:
            self._inlined[key] = self._encode(key, m)
        if self._inlined[key] is None and self.provenance is not None:
            self.rejected += 1
        return self._inlined[key]

    def _encode(self, key: str, m: re.Match | None) -> str | None:
        if m:
            try:
                return _data_uri(base64.b64decode("".join(m.group(1).split()), validate=True))
            except (binascii.Error, ValueError):
                return None
        try:
            p = self.work / key
            if p.stat().st_size > MAX_IMAGE_BYTES:
                return None
            data = p.read_bytes()
        except OSError:
            return None
        if self.provenance is not None and hashlib.sha256(data).hexdigest() != self.provenance.get(key):
            return None  # not the bytes the copy step wrote (a later Codex turn changed the file)
        return _data_uri(data)

    def _local(self, v: str) -> str | None:
        plain = unquote(v)
        if plain.lower().startswith("file://"):
            plain = plain[7:]
        plain = self.aliases.get(plain, plain)
        if _SCHEME.match(plain) or plain.startswith(("/", "~")) or "\\" in plain or "\0" in plain:
            return None
        if any(c in plain for c in "?#") or any(ord(c) < 0x20 for c in plain):
            return None
        parts = [p for p in plain.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or not all(_PART.fullmatch(p) for p in parts):
            return None
        p = self.work
        for part in parts:
            p = p / part
            if p.is_symlink():
                return None
        try:
            if not p.is_file() or not p.resolve().is_relative_to(self.work):
                return None
        except OSError:
            return None
        return "/".join(parts)


def _decode_css_escapes(css: str) -> str:
    def one(m: re.Match) -> str:
        if m.group(1):
            try:
                ch = chr(int(m.group(1), 16))
            except (ValueError, OverflowError):
                return " "
        else:
            ch = m.group(2)
        if ch in _CSS_SYNTAX or ord(ch) < 0x20 or ch == "\x7f" or 0xD800 <= ord(ch) <= 0xDFFF:
            return " "
        return ch
    return _CSS_ESCAPE.sub(one, css)


_WEIGHT = re.compile(r"(?<![\w.#-])(\d{2,4})(?![\w.%])")


def _snap_weight(n: str) -> str:
    return str(min(900, max(100, int((int(n) + 50) // 100 * 100))))


def _snap_weights(decl: str) -> str:
    """Live run 1: SF Pro at weights 650 and 750 made pypdf read "$1.99" as "$1.9 9" in the full deck; multiples
    of 100 read cleanly. Every unitless number in a font-weight or font declaration snaps to one (100-900)."""
    prop, _, val = decl.partition(":")
    return prop + ":" + _WEIGHT.sub(lambda m: _snap_weight(m.group(1)), val)


def _strip_text_features(css: str, removed: Counter) -> str:
    """R47: no declaration that shows text the HTML doesn't hold, or changes its case. R48: no custom property and
    no var()/env(), which could assemble any of these from parts."""
    n = len(_CSS_COUNTER_STYLE.findall(css))
    if n:
        removed["css-text"] += n
        css = _CSS_COUNTER_STYLE.sub(" ", css)

    def decl(m: re.Match) -> str:
        prop, val = m.group("prop").lower(), m.group("val").strip()
        if _blind(prop, val):
            removed["css-blind"] += 1
            return " "
        bad = (prop.startswith("--") or _VAR.search(val)
               or prop in _CASE_PROPS or prop.endswith(("text-transform", "font-variant"))
               or (prop == "content" and val.lower() not in ('""', "''", "none", "normal"))
               or (prop.startswith("list-style") and re.search(r"[\"']", val))
               or (prop == "font" and "small-caps" in val.lower())
               or _TEXT_MAKERS.search(val))
        if bad:
            removed["css-text"] += 1
            return " "
        return _snap_weights(m.group(0)) if prop in ("font-weight", "font") else m.group(0)

    return _CSS_DECL.sub(decl, css)


def _clean_pass(css: str, refs: _Refs, removed: Counter, kept: list[str]) -> str:
    """One pass of the pipeline. Every removed construct becomes a space, so removal never joins two halves."""
    css = _CSS_COMMENT.sub(" ", css)
    css = _decode_css_escapes(css)
    n = len(_CSS_IMPORT.findall(css))
    if n:
        removed["css-import"] += n
        css = _CSS_IMPORT.sub(" ", css)

    def url(m: re.Match) -> str:
        target = next(g for g in m.groups() if g is not None).strip()
        ok = target if _FRAGMENT.fullmatch(target) else refs.image(target)
        if ok is None:
            removed["url"] += 1
            return "none"
        kept.append(f"url({ok})")
        return f"\0{len(kept) - 1}\0"  # a placeholder until the end, so later passes never see our own url()

    css = _CSS_URL.sub(url, css)
    for rx, kind in ((_CSS_BAD_FN, "css-function"), (_CSS_BAD_PROP, "css-property")):
        n = len(rx.findall(css))
        if n:
            removed[kind] += n
            css = rx.sub("none" if kind == "css-function" else " ", css)
    if re.search(r"url\s*\(", css, re.IGNORECASE):  # anything the url pattern could not parse
        removed["url"] += 1
        css = re.sub(r"url\s*\(", "none(", css, flags=re.IGNORECASE)
    return _strip_text_features(css, removed)


def _clean_css(css: str, refs: _Refs, removed: Counter) -> str:
    """Cleaned CSS, or "" (R48). The pipeline repeats until its output stops changing, at most CSS_PASSES times, so
    no removal can leave behind a construct an earlier step would have caught; still changing after that, or still
    holding an @import or a url( of its own at the end, the whole block is dropped. The caller escapes the result
    for its context (style text: _style_text; attributes: html.escape)."""
    kept: list[str] = []
    css = css.replace("\0", "")
    for _ in range(CSS_PASSES):
        nxt = _clean_pass(css, refs, removed, kept)
        if nxt == css:
            break
        css = nxt
    else:
        removed["css-unstable"] += 1
        return ""
    if re.search(r"@import|url\s*\(|\bvar\s*\(|\benv\s*\(", css, re.IGNORECASE):  # all our urls are placeholders
        removed["css-unstable"] += 1
        return ""
    return re.sub(r"\0(\d+)\0", lambda m: kept[int(m.group(1))], css)


def _style_text(css: str) -> str:
    """CSS for a <style> element: `<` and `&` as CSS escapes, so the text can't close its tag or carry a reference."""
    return css.replace("&", "\\26 ").replace("<", "\\3c ")


class _Sanitizer(HTMLParser):
    def __init__(self, refs: _Refs):
        super().__init__(convert_charrefs=True)
        self.refs, self.out, self.removed = refs, [], Counter()
        self.skip: list[str] = []  # the stack of dropped elements we are inside
        self.style: list[str] | None = None  # an open <style>: its start tag and text, written only once closed
        self.svg = 0
        self.defs = 0  # open <defs> elements

    # -- tags --------------------------------------------------------------------------------------------------
    def _url(self, tag: str, name: str, value: str) -> str | None:
        v = value.strip()
        if (tag, name) in IMAGE_REFS:
            ok = self.refs.image(v)
            if ok is None and tag != "img" and _FRAGMENT.fullmatch(v):
                ok = v
            return ok
        return v if name in ("href", "xlink:href") and _FRAGMENT.fullmatch(v) else None

    def _attrs(self, tag: str, attrs) -> str:
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            value = "" if value is None else value
            if not _ATTR_NAME.fullmatch(name) or name in DROP_ATTRS:
                self.removed["attribute"] += 1
                continue
            if name.startswith("on"):
                self.removed["handler"] += 1
                continue
            if name == "font-weight" and re.fullmatch(r"\d{2,4}", value.strip()):
                value = _snap_weight(value.strip())
            if name in ("letter-spacing", "word-spacing", "font-variation-settings"):  # see _CASE_PROPS
                self.removed["css-text"] += 1
                continue
            if name in BLIND_ATTRS or (name == "transform" and not _TRANSLATE_ONLY.fullmatch(value)):
                self.removed["css-blind"] += 1  # R50a, see _BLIND_PROPS
                continue
            if name in TEXT_ONLY_ATTRS:  # never shown on a printed slide; Chrome may still tag the PDF with it
                self.removed["text-attribute"] += 1
                if name == "alt":
                    kept.append(' alt=""')
                continue
            if name in URL_ATTRS:
                ok = self._url(tag, name, value)
                if ok is None:
                    self.removed["url"] += 1
                    continue
                value = ok
            elif _SCRIPT_URL.search(_squash(value)):
                self.removed["script-url"] += 1
                continue
            elif name == "style":
                value = _clean_css(value, self.refs, self.removed)
            elif "\\" in value:  # SVG presentation attributes are CSS: an escape could hide url(
                self.removed["attribute"] += 1
                continue
            elif "(" in value:  # fill="url(#g)", filter="url(...)", transform="rotate(45)"
                value = _clean_css(value, self.refs, self.removed)
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        return "".join(kept)

    def _open(self, tag: str, attrs, closed: bool) -> None:
        if self.skip:
            if tag == self.skip[-1] and not closed:
                self.skip.append(tag)
            return
        if not _TAG_NAME.fullmatch(tag or ""):
            self.removed["tag:other"] += 1
            return
        if (tag in DROP_WITH_CONTENT or self.defs and tag in DEFS_TEXT
                or tag == "style" and (closed or self.svg or self.style is not None)):
            # <style/>: Chrome ignores the slash and reads everything up to </style> as CSS, so we skip to it too
            self.removed[f"tag:{tag}"] += 1
            if not closed or tag == "style":
                self.skip.append(tag)
            return
        if tag in DROP_TAG:
            if tag not in ("html", "head", "body"):
                self.removed[f"tag:{tag}"] += 1
            return
        start = f"<{tag}{self._attrs(tag, attrs)}>"
        if tag == "style":
            self.style = [start]
            return
        self.out.append(start)
        if closed and tag not in VOID:
            self.out.append(f"</{tag}>")  # never a self-closing slash
        elif tag == "svg" and not closed:
            self.svg += 1
        elif tag == "defs" and not closed:
            self.defs += 1

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, closed=True)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == "style":
            if self.style is not None:
                start, *text = self.style
                css = _clean_css(html.unescape("".join(text)), self.refs, self.removed)
                self.out.append(f"{start}{_style_text(css)}</style>")
                self.style = None
            return
        if tag in DROP_WITH_CONTENT or tag in DROP_TAG or tag in VOID or not _TAG_NAME.fullmatch(tag or ""):
            return
        if tag == "svg" and self.svg:
            self.svg -= 1
        if tag == "defs" and self.defs:
            self.defs -= 1
        self.out.append(f"</{tag}>")

    # -- content -----------------------------------------------------------------------------------------------
    def handle_data(self, data):
        if self.skip:
            return
        if self.style is not None:
            self.style.append(data)
        else:
            self.out.append(html.escape(strip_ignorable(data), quote=False))  # C2

    def handle_comment(self, data):
        self.removed["comment"] += 1

    def handle_decl(self, decl):
        pass

    def unknown_decl(self, data):
        self.removed["declaration"] += 1

    def handle_pi(self, data):
        self.removed["declaration"] += 1


def sanitize_html(text: str, work: Path, *, aliases: dict[str, str] | None = None,
                  provenance: dict[str, str] | None = None) -> tuple[str, Counter]:
    """(safe HTML, removed counts). `work` is the deck's work dir; images must resolve inside it. With
    `provenance` ({name: sha256}), only those files with those bytes are shown (see _Refs)."""
    refs = _Refs(work, aliases, provenance)
    p = _Sanitizer(refs)
    p.feed(text or "")
    p.close()
    if refs.rejected:
        p.removed["image-provenance"] += refs.rejected
    if p.style is not None:  # never closed: dropped
        p.removed["tag:style"] += 1
    body = "".join(p.out)
    head = (f'<!doctype html>\n<html><head><meta charset="utf-8">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n</head><body>\n')
    return head + body + f"\n</body></html>\n<style>{PAGE_CSS}</style>", p.removed
