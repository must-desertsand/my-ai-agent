"""What a post points at: the sites and the one HTML demo worth a look.

Up to 2 links (http and https only: Slack's `<url|label>` and `<url>` markup, and bare URLs) and up to 1 HTML attachment.
A slack.com link or a link to a file (a PDF, an image, an archive...) is not a site and is skipped, as is a repeated link.
PDFs keep their existing path in the feedback job and are not targets. Everything here is plain parsing: no network, no
policy (the proxy decides what may be reached); `label` is the only text that goes on to a DM line, and it is built from
a host name or a file name reduced to safe characters.
"""
from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from ..models import FileRef

MAX_LINKS = 2
MAX_URL_CHARS = 2000
HTML_MAX_BYTES = 5 * 1024 * 1024
LABEL_CHARS = 40

_SLACK_LINK = re.compile(r"<(https?://[^|>\s]+)(?:\|[^>]*)?>", re.IGNORECASE)
_BARE = re.compile(r"https?://[^\s<>\"'`|\\^{}]+", re.IGNORECASE)
_TRAILING = ".,;:!?'\"*_~"
_SLACK_HOSTS = ("slack.com", "slack-edge.com", "slack-files.com", "slack-redir.net", "slackb.com")
_FILE_EXTENSIONS = frozenset("""
pdf png jpg jpeg gif webp svg bmp ico tif tiff heic avif zip gz tgz tar rar 7z bz2 xz dmg pkg exe msi apk ipa iso
doc docx xls xlsx ppt pptx odt ods odp pages numbers key csv tsv json xml yaml yml txt md rtf log
mp3 mp4 mov avi wav m4a m4v mkv webm flac ogg epub mobi psd ai sketch fig
""".split())
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._ -]")


@dataclass(frozen=True)
class Target:
    kind: str                    # "link" or "html"
    label: str                   # what a DM line calls it: a host name, or a file name reduced to safe characters
    url: str | None = None       # the link to open (kind "link")
    ref: FileRef | None = None   # the attachment (kind "html")


def _clean_url(raw: str) -> str | None:
    url = html.unescape(raw.strip())  # Slack writes & as &amp; inside a link
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    while url.endswith(")") and url.count(")") > url.count("("):  # a link at the end of "(see https://x.com/y)"
        url = url[:-1]
        while url and url[-1] in _TRAILING:
            url = url[:-1]
    if not url or len(url) > MAX_URL_CHARS or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, parts.fragment))


def _is_slack(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _SLACK_HOSTS)


def _is_file_link(path: str) -> bool:
    last = path.rsplit("/", 1)[-1]
    return "." in last and last.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS


def host_label(url: str) -> str:
    """The host of a URL for a DM line: lower case, letters, digits, dots and hyphens only (punycode for other scripts),
    or "a linked site" when it is anything else."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return "a linked site"
    return host if re.fullmatch(r"[a-z0-9.-]{1,100}", host) else "a linked site"


def file_label(name: str) -> str:
    """A file name reduced to safe characters, at most LABEL_CHARS, for a DM line."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    kept = " ".join(_SAFE_LABEL.sub("", base).split())[:LABEL_CHARS].strip(" .-")
    return kept or "the attached page"


def find_links(text: str, limit: int = MAX_LINKS) -> list[str]:
    """The sites a post links to, in the order it mentions them: at most `limit`, each once."""
    found: list[tuple[int, str]] = []
    blanked = text or ""
    for m in _SLACK_LINK.finditer(text or ""):
        found.append((m.start(), m.group(1)))
        blanked = blanked[: m.start()] + " " * (m.end() - m.start()) + blanked[m.end():]
    for m in _BARE.finditer(blanked):
        found.append((m.start(), m.group(0)))
    out: list[str] = []
    seen: set[str] = set()
    for _pos, raw in sorted(found):
        url = _clean_url(raw)
        if url is None:
            continue
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if _is_slack(host) or _is_file_link(parts.path):
            continue
        key = f"{parts.scheme}://{host}:{parts.port or ''}{parts.path.rstrip('/')}?{parts.query}"
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def is_html_file(ref: FileRef, mimetype: str = "") -> bool:
    name = (ref.name or "").lower()
    kinds = {(ref.mimetype or "").lower().split(";")[0].strip(), (mimetype or "").lower().split(";")[0].strip()}
    return name.endswith((".html", ".htm")) or bool(kinds & {"text/html", "application/xhtml+xml"})


def find_html(files: Sequence[FileRef]) -> FileRef | None:
    """The first HTML attachment (a `.html` or `.htm` name, or a text/html type), or None."""
    return next((f for f in files if is_html_file(f)), None)


def find_targets(text: str, files: Sequence[FileRef]) -> list[Target]:
    """What to review in a post: up to 2 links, then up to 1 HTML attachment."""
    out = [Target("link", host_label(u), url=u) for u in find_links(text)]
    ref = find_html(files)
    if ref is not None:
        out.append(Target("html", file_label(ref.name), ref=ref))
    return out
