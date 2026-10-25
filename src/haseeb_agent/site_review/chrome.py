"""Render a page in headless Chrome for the site review: two screenshots, the visible text, and a few checks.

What a render is. One Chrome process in a throwaway profile (a temp `--user-data-dir`, deleted after), started in its own
process group so a timeout kills the whole group, driven over `--remote-debugging-pipe` (cdp.py): no debugging port. The
page is loaded twice, each time in a fresh browser context (no cookie or storage carried over from the first load): once
at desktop size (1440x900) and once at phone size (390x844, a mobile user agent, touch). For each size it keeps a JPEG of
the first viewport (at most 1,600 px), the page title and visible text after the scripts ran (at most 6,000 characters),
the horizontal overflow (`scrollWidth` against `clientWidth`), whether the page has a viewport meta tag, and counts and
short types of failed requests and console errors. Nothing is clicked and no form is submitted.

How it is kept from reaching anything else (the policy is policy.py's, the enforcement proxy.py's):
  - ALL traffic goes through the filtering proxy: `--proxy-server=http://127.0.0.1:<port>` and
    `--proxy-bypass-list=<-loopback>` (which stops Chrome keeping loopback and link-local for itself), so a page's request
    to 127.0.0.1:37777, to 192.168.x.x or to a name that rebinds to either arrives at the proxy and is refused there;
    `--host-resolver-rules` makes any name Chrome would resolve for itself fail, so nothing can go around the proxy by name;
  - no QUIC, WebRTC only through the proxy, no extensions, no sync, pop-ups blocked, a clean environment and a profile
    with nothing of Haseeb's in it; and, for the browser context each size is actually rendered in (a setting made on the
    browser as a whole reaches only its default context): downloads denied, the permissions a page could ask for denied
    (geolocation, notifications, camera, microphone, ...), the proxy set, and an assertion that the context starts with no
    cookie. If the download denial cannot be made, the render fails. `--download-default-directory` points inside the
    throwaway profile as a backstop, and anything found there afterwards is counted in a warning;
  - a link is opened only after its URL passed the policy (scheme, port, credentials, non-public literals), and a
    redirect is followed for at most 3 hops, each hop a new connection that the proxy checks again;
  - an HTML attachment is rendered from a temp file that holds it alone, never served from a port, with the proxy refusing
    EVERYTHING (offline) and a Content-Security-Policy added to the page that lets it load nothing from `file:` (a page
    must not be able to put another local file into the screenshot through a frame or an image); if the page navigates
    anywhere else, the render is discarded;
  - every real step starts with the offline guard.
"""
from __future__ import annotations

import base64
import fcntl
import ipaddress
import logging
import os
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from ..offline import guard
from .cdp import CdpClosed, CdpError, CdpPipe, CdpTimeout, Deadline, Event
from .localnet import LocalNetworks
from .policy import DESCRIPTIONS, Policy, Refused, Resolver, system_resolver
from .proxy import Connector, FilteringProxy, open_connection

log = logging.getLogger(__name__)

TEXT_CHARS = 6000
MAX_SIDE = 1600
JPEG_QUALITY = 80
REDIRECT_HOPS = 3
LOAD_WAIT_S = 25.0       # the load event, at most (the target's budget is the real bound)
SETTLE_MIN_S = 0.4       # a moment for the first paint and the first scripts
SETTLE_IDLE_S = 0.7      # no request in flight for this long: the page has settled
SETTLE_MAX_S = 6.0       # a page that never goes quiet (polling) is taken as it is after this
CLOSE_WAIT_S = 2.0
PHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.5 Mobile/15E148 Safari/604.1")
FALLBACK_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130.0.0.0 Safari/537.36")
CHROME_MAX_AGE = timedelta(days=45)   # a Chrome whose build is older than this does not render a hostile page (M11)
FILE_CSP = ("default-src data: blob: https: http: 'unsafe-inline' 'unsafe-eval'; frame-src data: blob: https: http:; "
            "object-src 'none'; base-uri 'none'; form-action 'none'")


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int
    mobile: bool
    scale: float   # the screenshot's scale, so its longest side stays within MAX_SIDE


DESKTOP = Viewport("desktop", 1440, 900, False, 1.0)
PHONE = Viewport("phone", 390, 844, True, 1.8)

# the short phrases a DM line may carry (never a host, an address, a page's words or an error message)
REASONS = {
    "timeout": "timed out",
    "load": "couldn't load the page",
    "connect": "couldn't connect",
    "dns": "address not found",
    "cert": "certificate problem",
    "redirects": "too many redirects",
    "not_page": "not a web page",
    "navigated": "the page navigated away",
    "chrome": "Chrome failed",
    "setup": "browser setup",
    "empty": "the page came up blank",
}
_NET_REASONS = (
    ("ERR_NAME_NOT_RESOLVED", "dns"), ("ERR_CERT", "cert"), ("ERR_SSL", "cert"), ("ERR_TOO_MANY_REDIRECTS", "redirects"),
    ("ERR_CONNECTION", "connect"), ("ERR_TIMED_OUT", "connect"), ("ERR_ADDRESS", "connect"), ("ERR_NETWORK", "connect"),
    ("ERR_INTERNET_DISCONNECTED", "connect"), ("ERR_ABORTED", "not_page"), ("ERR_INVALID_RESPONSE", "not_page"),
)


class RenderError(Exception):
    """The page could not be rendered. `reason` is the short phrase for the DM line (a REASONS value, or the policy's
    description of a refusal, or `HTTP 404`); `detail` is a few fixed words for the log."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


OUT_OF_DATE = "Chrome is out of date \u2014 update Chrome"


class ChromeOutOfDate(RenderError):
    """The Chrome at `chrome_path` was built more than CHROME_MAX_AGE ago, so the page is not rendered (M11). `version` and
    `age_days` are for the log."""

    def __init__(self, version: str, age_days: int):
        super().__init__(OUT_OF_DATE, "chrome age")
        self.version, self.age_days = version, age_days


@dataclass
class Shot:
    """One size's capture."""
    size: str
    jpeg: bytes
    width_px: int
    height_px: int
    title: str
    text: str
    scroll_width: int
    client_width: int
    viewport_meta: bool
    status: int | None
    console_errors: int
    exceptions: int
    log_errors: int
    dialogs: int
    failed: dict[str, int] = field(default_factory=dict)  # short error type -> count, e.g. {"ERR_BLOCKED_BY_CLIENT": 2}


@dataclass
class Rendered:
    """A target's two captures, and what the proxy did on the way: what it refused (reason codes and counts, no addresses)
    and how many connections it made (everything the page loaded came through it)."""
    desktop: Shot
    phone: Shot
    refused: dict[str, int]
    connections: int
    offline: bool
    downloads: int = 0   # files found in the profile's download folder afterwards: always 0 unless a denial failed


# ------------------------------------------------------------------ Chrome itself

def _bundle_plist(chrome: str) -> Path | None:
    """The Info.plist of the app bundle the Chrome binary is in (`/Applications/Google Chrome.app/Contents/Info.plist`), after
    symlinks; None when the binary is not in a bundle or the bundle has none."""
    for parent in Path(os.path.realpath(chrome)).parents:
        if parent.suffix == ".app":
            plist = parent / "Contents" / "Info.plist"
            return plist if plist.is_file() else None
    return None


def chrome_build(chrome: str) -> tuple[str, float]:
    """(version, built) of the Chrome at `chrome`: the app bundle's Info.plist version and the modification time of that file
    (a Chrome update replaces the bundle, so it is when this build was made or installed); for a binary that is not in a
    bundle, or a plist that cannot be read, "unknown" and the binary's own modification time. RenderError when there is
    nothing to read."""
    plist = _bundle_plist(chrome)
    if plist is not None:
        try:
            with open(plist, "rb") as f:
                info = plistlib.load(f)
            version = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "unknown")
            return re.sub(r"[^0-9A-Za-z._-]", "", version)[:30] or "unknown", os.stat(plist).st_mtime
        except Exception:  # noqa: BLE001 - a plist that cannot be read: the binary's own date still says how old it is
            pass
    try:
        return "unknown", os.stat(os.path.realpath(chrome)).st_mtime
    except OSError:
        raise RenderError(REASONS["chrome"], "chrome path") from None


def ensure_chrome_fresh(chrome: str, *, now: float | None = None, max_age: timedelta = CHROME_MAX_AGE) -> None:
    """ChromeOutOfDate when the Chrome at `chrome` was built more than `max_age` (45 days) ago. A page that is rendered runs
    scripts as this Mac's user with only Chrome's own sandbox in the way, and Chrome ships security fixes every few weeks: an
    old build is a known hole (security review M11). A real step (it reads the app bundle), so the offline guard comes first."""
    guard("chrome age check (site review)")
    version, built = chrome_build(chrome)
    age = (time.time() if now is None else now) - built
    if age > max_age.total_seconds():
        raise ChromeOutOfDate(version, int(age // 86400))


def downloads_dir(profile: str) -> str:
    """Where Chrome would put a download, inside the throwaway profile: the denial of downloads is made per browser
    context, and this is the backstop (the profile's deletion removes whatever got through)."""
    return os.path.join(profile, "downloads")


def chrome_argv(chrome: str, profile: str, proxy_port: int) -> list[str]:
    """The command line. Kept in one place so a test can check every flag that keeps the page on its leash."""
    return [
        chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps",
        "--disable-breakpad", "--disable-client-side-phishing-detection", "--disable-domain-reliability",
        "--disable-features=DnsOverHttps,Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
        "--metrics-recording-only", "--mute-audio", "--hide-scrollbars", "--password-store=basic", "--use-mock-keychain",
        "--deny-permission-prompts", "--block-new-web-contents", "--lang=en-US",
        f"--user-data-dir={profile}",
        f"--download-default-directory={downloads_dir(profile)}",
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",  # the proxy's own address is the one name that resolves
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--remote-debugging-pipe",
        "about:blank",
    ]


@dataclass
class Launched:
    proc: object                  # a subprocess.Popen, or a stand-in with .wait(timeout)
    read_fd: int                  # our end of Chrome's fd 4 (its replies and events)
    write_fd: int                 # our end of Chrome's fd 3 (our commands)
    kill: Callable[[], None]      # ends the whole process group (a test's stand-in ends its thread)


Launcher = Callable[[list[str], dict], Launched]


def launch_chrome(argv: list[str], env: dict) -> Launched:
    """Start Chrome with the protocol on file descriptors 3 and 4 (a pipe pair each way), in its own session and process
    group. The offline guard comes first."""
    guard("chrome (site review)")
    r3, w3 = os.pipe()  # our commands -> Chrome's fd 3
    r4, w4 = os.pipe()  # Chrome's fd 4 -> our replies
    # Chrome's ends move above fd 4 first: dup2 onto 3 or 4 could otherwise clobber the other end
    cr = fcntl.fcntl(r3, fcntl.F_DUPFD, 5)
    cw = fcntl.fcntl(w4, fcntl.F_DUPFD, 5)
    os.close(r3)
    os.close(w4)

    def child() -> None:
        os.dup2(cr, 3)
        os.dup2(cw, 4)
        os.set_inheritable(3, True)
        os.set_inheritable(4, True)
        os.close(cr)
        os.close(cw)

    try:
        # close_fds with 3 and 4 passed (M9): Chrome renders hostile pages, so it gets the two protocol descriptors and nothing
        # else of the tick's. Python closes the other descriptors after preexec_fn has run, so the dup2'd 3 and 4 survive.
        proc = subprocess.Popen(argv, preexec_fn=child, close_fds=True, pass_fds=(3, 4), start_new_session=True,  # noqa: PLW1509
                                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except BaseException:
        for fd in (cr, cw, w3, r4):
            os.close(fd)
        raise
    os.close(cr)
    os.close(cw)
    return Launched(proc, read_fd=r4, write_fd=w3, kill=lambda: _kill_group(proc.pid))


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)  # start_new_session: the group id is the child's pid
    except (ProcessLookupError, PermissionError, OSError):
        pass


class _Browser:
    """One Chrome, its profile, and the proxy in front of it, for one target. A context manager: leaving it kills the
    whole process group, stops the proxy and deletes the profile, whatever happened (a timeout, the tick's watchdog)."""

    def __init__(self, chrome: str, policy: Policy, deadline: Deadline, *, resolver: Resolver, connector: Connector,
                 launcher: Launcher):
        self.chrome, self.policy, self.deadline = chrome, policy, deadline
        self.proxy = FilteringProxy(policy, resolver=resolver, connector=connector)
        self.launcher = launcher
        self.profile = ""
        self.downloads_found = 0
        self.launched: Launched | None = None
        self.pipe: CdpPipe | None = None

    def __enter__(self) -> "_Browser":
        try:
            self.profile = tempfile.mkdtemp(prefix="haseeb-site-review-chrome-")
            os.makedirs(downloads_dir(self.profile), mode=0o700, exist_ok=True)
            port = self.proxy.bind()
            env = {"HOME": self.profile, "TMPDIR": self.profile, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                   "LANG": "en_US.UTF-8"}  # nothing of the tick's environment goes to Chrome
            self.launched = self.launcher(chrome_argv(self.chrome, self.profile, port), env)
            self.proxy.serve()  # after the launch: the proxy's threads start only now (an earlier target's lookup worker may
            # still be alive; fork+exec runs no Python in the child, so it holds no lock of ours)
            self.pipe = CdpPipe(self.launched.read_fd, self.launched.write_fd, self.deadline)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.pipe is not None:
            try:
                self.pipe.call("Browser.close", timeout=1.0)
            except Exception:  # noqa: BLE001 - it is killed next either way
                pass
        launched = self.launched
        if launched is not None:
            try:
                launched.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                launched.proc.wait(CLOSE_WAIT_S)
            except Exception:  # noqa: BLE001
                pass
            for fd in (launched.read_fd, launched.write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.proxy.stop()
        if self.profile:
            left = sum(len(files) for _d, _dirs, files in os.walk(downloads_dir(self.profile)))
            if left:  # a page got a download through: it is discarded with the profile, and counted here
                log.warning("site review: %d downloaded file(s) were found in the throwaway profile and discarded", left)
            self.downloads_found = left
            shutil.rmtree(self.profile, ignore_errors=True)


# ------------------------------------------------------------------ one page, one size

class PageWatch:
    """What the events of one page tell: whether it loaded, what is in flight, the main document's status and fate, the
    redirect hops, and counts and short types of what went wrong. Events from other sessions are not its business."""

    def __init__(self, session: str):
        self.session = session
        self.loaded = False
        self.inflight: set[str] = set()
        self.last_activity = time.monotonic()
        self.main_id: str | None = None      # the request id of the main document's latest request
        self.status: int | None = None
        self.mime = ""
        self.main_failed = ""
        self.hops = 0
        self.navigated: list[str] = []       # the URL of every commit of the main frame, in order
        self.unreachable = False
        self.console_errors = self.exceptions = self.log_errors = self.dialogs = 0
        self.dialog_open = False
        self.failed: Counter[str] = Counter()
        self.main_frame: str | None = None

    def feed(self, events: list[Event]) -> None:
        for ev in events:
            if ev.session != self.session:
                continue
            p, m = ev.params, ev.method
            if m == "Network.requestWillBeSent":
                rid = str(p.get("requestId", ""))
                self.inflight.add(rid)
                self.last_activity = time.monotonic()
                if p.get("type") == "Document" and (self.main_frame is None or p.get("frameId") == self.main_frame):
                    if p.get("redirectResponse") and self.main_id is not None:
                        self.hops += 1
                    self.main_id = rid
            elif m == "Network.responseReceived":
                if str(p.get("requestId", "")) == self.main_id and p.get("type") == "Document":
                    resp = p.get("response") if isinstance(p.get("response"), dict) else {}
                    st = resp.get("status")
                    self.status = st if isinstance(st, int) else None
                    self.mime = str(resp.get("mimeType", ""))[:100]
            elif m == "Network.loadingFinished":
                self.inflight.discard(str(p.get("requestId", "")))
                self.last_activity = time.monotonic()
            elif m == "Network.loadingFailed":
                rid = str(p.get("requestId", ""))
                self.inflight.discard(rid)
                self.last_activity = time.monotonic()
                kind = re.sub(r"[^A-Za-z0-9_]", "", str(p.get("errorText", "")).replace("net::", ""))[:40] or "unknown"
                if kind == "ERR_ABORTED" and not p.get("canceled"):
                    kind = "ERR_ABORTED"
                self.failed[kind] += 1
                if rid == self.main_id:
                    self.main_failed = kind
            elif m == "Runtime.consoleAPICalled":
                if p.get("type") == "error":
                    self.console_errors += 1
            elif m == "Runtime.exceptionThrown":
                self.exceptions += 1
            elif m == "Log.entryAdded":
                entry = p.get("entry") if isinstance(p.get("entry"), dict) else {}
                if entry.get("level") == "error":
                    self.log_errors += 1
            elif m == "Page.loadEventFired":
                self.loaded = True
            elif m == "Page.frameNavigated":
                frame = p.get("frame") if isinstance(p.get("frame"), dict) else {}
                if not frame.get("parentId"):
                    self.main_frame = str(frame.get("id", "")) or self.main_frame
                    self.navigated.append(str(frame.get("url", ""))[:2000])
                    if frame.get("unreachableUrl"):
                        self.unreachable = True
            elif m == "Page.javascriptDialogOpening":
                self.dialogs += 1
                self.dialog_open = True
            elif m == "Page.javascriptDialogClosed":
                self.dialog_open = False


def _short_reason(text: str) -> str:
    for needle, key in _NET_REASONS:
        if needle in text:
            return key
    return "load"


_EXTRACT = """(() => {
  try {
    const de = document.documentElement, b = document.body;
    return {
      title: String(document.title || "").slice(0, 300),
      text: String((b && b.innerText) || "").slice(0, 30000),
      scrollWidth: Math.max(de ? de.scrollWidth : 0, b ? b.scrollWidth : 0),
      clientWidth: de ? de.clientWidth : 0,
      viewportMeta: !!document.querySelector('meta[name="viewport" i]'),
      contentType: String(document.contentType || ""),
      href: String(location.href || "")
    };
  } catch (e) { return {error: String((e && e.name) || "error")}; }
})()"""


def _count(value) -> int:
    """A measured number from the page's answer as an int; anything that is not a plain, finite number counts as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) > 1e9:
        return 0
    return int(value)


def clean_text(raw: str, limit: int = TEXT_CHARS) -> str:
    """The visible text, tidied: control characters out, runs of blanks and blank lines squeezed, capped."""
    t = "".join(c if c in "\n\t" or ord(c) >= 32 and ord(c) != 127 else " " for c in raw)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit]


class _Page:
    """Drive one tab at one size, from the empty tab to a Shot."""

    def __init__(self, pipe: CdpPipe, deadline: Deadline, vp: Viewport, desktop_ua: str):
        self.pipe, self.deadline, self.vp, self.desktop_ua = pipe, deadline, vp, desktop_ua

    def _left(self, cap: float) -> float:
        left = self.deadline.left()
        if left <= 0:
            raise RenderError(REASONS["timeout"], "budget")
        return min(cap, left)

    def _spin(self, watch: PageWatch, session: str, seconds: float) -> None:
        """Let events arrive for up to `seconds`, answer a dialog (a page that blocks on alert() would hang), and feed them."""
        self.pipe.pump(min(seconds, max(0.0, self.deadline.left())))
        watch.feed(self.pipe.take())
        if watch.dialog_open:
            try:
                self.pipe.call("Page.handleJavaScriptDialog", {"accept": False}, session=session, timeout=2.0)
            except (CdpError, CdpTimeout):
                pass
            watch.dialog_open = False

    def capture(self, ctx: str, open_page: Callable[[CdpPipe, str], str | None], expect: Callable[[str], bool],
                proxy: FilteringProxy) -> Shot:
        """Open a tab in the browser context `ctx`, load the page with `open_page` (it navigates and returns an error text, or
        None), wait for it to settle, and capture. `expect(url)` says whether the page's final URL is where it should be."""
        pipe, vp = self.pipe, self.vp
        target = pipe.call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx}, timeout=self._left(10))["targetId"]
        try:
            session = pipe.call("Target.attachToTarget", {"targetId": target, "flatten": True},
                                timeout=self._left(10))["sessionId"]
            watch = PageWatch(session)
            for method in ("Page.enable", "Network.enable", "Runtime.enable", "Log.enable"):
                pipe.call(method, session=session, timeout=self._left(10))
            watch.main_frame = pipe.call("Page.getFrameTree", session=session,
                                         timeout=self._left(10))["frameTree"]["frame"]["id"]
            pipe.call("Emulation.setDeviceMetricsOverride", {
                "width": vp.width, "height": vp.height, "deviceScaleFactor": 1, "mobile": vp.mobile,
                "screenWidth": vp.width, "screenHeight": vp.height}, session=session, timeout=self._left(10))
            ua = PHONE_UA if vp.mobile else self.desktop_ua
            pipe.call("Emulation.setUserAgentOverride", {"userAgent": ua, **({"platform": "iPhone"} if vp.mobile else {})},
                      session=session, timeout=self._left(10))
            if vp.mobile:
                pipe.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}, session=session,
                          timeout=self._left(10))
            watch.feed(pipe.take())
            error = open_page(pipe, session)
            watch.feed(pipe.take())
            if error:
                raise RenderError(*self._failure(error, watch, proxy))
            self._settle(watch, session)
            self._check_main(watch, proxy, expect)
            shot = self._shoot(watch, session, expect, proxy)
            return shot
        finally:
            try:
                pipe.call("Target.closeTarget", {"targetId": target}, timeout=2.0)
            except Exception:  # noqa: BLE001
                pass

    def _failure(self, error: str, watch: PageWatch, proxy: FilteringProxy) -> tuple[str, str]:
        """(reason, detail) for a main document that did not load: the proxy's refusal when it refused, else the network's."""
        if proxy.log.refused_count:
            _host, why, detail = proxy.log.refused[0]
            return DESCRIPTIONS.get(why, "refused"), f"proxy refused ({why}/{detail})"
        key = _short_reason(error)
        return REASONS[key], re.sub(r"[^A-Za-z0-9_]", "", error)[:60]

    def _settle(self, watch: PageWatch, session: str) -> None:
        load_end = time.monotonic() + self._left(LOAD_WAIT_S)
        while not watch.loaded and time.monotonic() < load_end:
            self._spin(watch, session, 0.1)
            if watch.hops > REDIRECT_HOPS:
                self._stop(session)
                raise RenderError(REASONS["redirects"], "hops")
            if watch.main_failed or watch.unreachable:
                return
        began = time.monotonic()
        settle_end = began + min(SETTLE_MAX_S, max(0.0, self.deadline.left()))
        while time.monotonic() < settle_end:
            self._spin(watch, session, 0.1)
            if watch.hops > REDIRECT_HOPS:
                self._stop(session)
                raise RenderError(REASONS["redirects"], "hops")
            quiet = not watch.inflight and time.monotonic() - watch.last_activity >= SETTLE_IDLE_S
            if quiet and time.monotonic() - began >= SETTLE_MIN_S:
                return
        if self.deadline.expired():
            raise RenderError(REASONS["timeout"], "budget")

    def _stop(self, session: str) -> None:
        try:
            self.pipe.call("Page.stopLoading", session=session, timeout=2.0)
        except (CdpError, CdpTimeout):
            pass

    def _check_main(self, watch: PageWatch, proxy: FilteringProxy, expect: Callable[[str], bool]) -> None:
        if watch.hops > REDIRECT_HOPS:
            raise RenderError(REASONS["redirects"], "hops")
        if watch.main_failed or watch.unreachable:
            reason, detail = self._failure(watch.main_failed or "ERR_UNKNOWN", watch, proxy)
            raise RenderError(reason, detail)
        last = watch.navigated[-1] if watch.navigated else ""
        if watch.navigated and not expect(last):  # before anything is read of where it went
            raise RenderError(REASONS["navigated"], "frame")
        if watch.status is not None and watch.status >= 400:
            raise RenderError(f"HTTP {watch.status}", "status")
        mime = watch.mime.lower()
        if mime and not (mime.startswith("text/html") or mime.startswith("application/xhtml")):
            raise RenderError(REASONS["not_page"], "mime")

    def _evaluate(self, session: str, world: int) -> dict:
        """The checks, read in the isolated world `world` and nowhere else (never in the page's own world: see `_shoot`)."""
        params = {"expression": _EXTRACT, "returnByValue": True, "contextId": world}
        res = self.pipe.call("Runtime.evaluate", params, session=session, timeout=self._left(10))
        value = (res.get("result") or {}).get("value")
        return value if isinstance(value, dict) else {}

    def _shoot(self, watch: PageWatch, session: str, expect: Callable[[str], bool], proxy: FilteringProxy) -> Shot:
        pipe, vp = self.pipe, self.vp
        # The checks (title, text, overflow, viewport) are read in an isolated world: the page's own scripts cannot answer
        # for it. If that world cannot be made, or does not answer, the render FAILS (M3): asking the page's own world would
        # let its scripts replace Math.max or String and forge the numbers the review calls "measured".
        try:
            frame = pipe.call("Page.getFrameTree", session=session, timeout=self._left(10))["frameTree"]["frame"]["id"]
            world = pipe.call("Page.createIsolatedWorld", {"frameId": frame, "worldName": "site-review",
                                                           "grantUniveralAccess": False},
                              session=session, timeout=self._left(10)).get("executionContextId")
        except (CdpError, KeyError, TypeError):
            raise RenderError(REASONS["setup"], "isolated world") from None
        if isinstance(world, bool) or not isinstance(world, int):
            raise RenderError(REASONS["setup"], "isolated world")
        info = self._evaluate(session, world)
        if info.get("error") or not isinstance(info.get("href"), str):
            raise RenderError(REASONS["load"], "evaluate")
        if not expect(info["href"]):
            raise RenderError(REASONS["navigated"], "href")
        ctype = str(info.get("contentType", "")).lower()
        if ctype and not (ctype.startswith("text/html") or ctype.startswith("application/xhtml")):
            raise RenderError(REASONS["not_page"], "content type")
        scale = min(vp.scale, MAX_SIDE / max(vp.width, vp.height))
        shot = pipe.call("Page.captureScreenshot", {
            "format": "jpeg", "quality": JPEG_QUALITY, "fromSurface": True, "captureBeyondViewport": False,
            "clip": {"x": 0, "y": 0, "width": vp.width, "height": vp.height, "scale": scale}},
            session=session, timeout=self._left(15))
        try:
            jpeg = base64.b64decode(shot["data"], validate=True)
        except (KeyError, ValueError, TypeError):
            raise RenderError(REASONS["chrome"], "screenshot") from None
        if not jpeg.startswith(b"\xff\xd8"):
            raise RenderError(REASONS["chrome"], "screenshot") from None
        after = self._evaluate(session, world)  # the page did not move on while it was photographed
        watch.feed(pipe.take())
        if not expect(str(after.get("href", ""))) or (watch.navigated and not expect(watch.navigated[-1])):
            raise RenderError(REASONS["navigated"], "after")
        text = clean_text(str(info.get("text", "")))
        return Shot(
            size=vp.name, jpeg=jpeg, width_px=int(vp.width * scale), height_px=int(vp.height * scale),
            title=clean_text(str(info.get("title", "")), 300), text=text,
            scroll_width=_count(info.get("scrollWidth")), client_width=_count(info.get("clientWidth")),
            viewport_meta=bool(info.get("viewportMeta")), status=watch.status,
            console_errors=watch.console_errors, exceptions=watch.exceptions, log_errors=watch.log_errors,
            dialogs=watch.dialogs, failed=dict(watch.failed))


# ------------------------------------------------------------------ a target

def _desktop_ua(pipe: CdpPipe) -> str:
    try:
        ua = str(pipe.call("Browser.getVersion", timeout=5).get("userAgent", ""))
    except (CdpError, CdpTimeout):
        return FALLBACK_DESKTOP_UA
    return ua.replace("HeadlessChrome", "Chrome") if "Mozilla/" in ua else FALLBACK_DESKTOP_UA


# The permissions a page could ask for; each is denied on the browser context in use (a grant or denial made on the browser
# as a whole reaches only its default context). `--deny-permission-prompts` denies a prompt that is still shown, for any
# name this Chrome does not know.
DENIED_PERMISSIONS = ("geolocation", "notifications", "camera", "microphone", "midi", "clipboard-read", "clipboard-write",
                      "persistent-storage", "push", "background-sync", "display-capture", "window-management",
                      "local-fonts", "idle-detection", "screen-wake-lock", "payment-handler", "storage-access")


def _deny_downloads(pipe: CdpPipe, context: str | None) -> None:
    """Downloads are denied on this browser context (None: the default one). A failure is a failed render: a page that
    could download a file would be writing to this Mac."""
    params: dict = {"behavior": "deny"}
    if context is not None:
        params["browserContextId"] = context
    try:
        pipe.call("Browser.setDownloadBehavior", params, timeout=5)
    except CdpError:
        raise RenderError(REASONS["setup"], "download deny") from None


def _setup_context(pipe: CdpPipe, proxy_port: int) -> str:
    """A fresh browser context for one size, set up BEFORE any page exists in it: downloads denied (fatal if it fails),
    every permission denied, and a check that it starts without a cookie. Its proxy is set on the context too."""
    ctx = pipe.call("Target.createBrowserContext", {
        "disposeOnDetach": True, "proxyServer": f"http://127.0.0.1:{proxy_port}", "proxyBypassList": "<-loopback>"},
        timeout=10)["browserContextId"]
    try:
        _deny_downloads(pipe, ctx)
        for name in DENIED_PERMISSIONS:
            try:
                pipe.call("Browser.setPermission", {"permission": {"name": name}, "setting": "denied",
                                                    "browserContextId": ctx}, timeout=5)
            except CdpError:
                continue  # a name this Chrome does not know: the flag still denies its prompt
        try:
            cookies = pipe.call("Storage.getCookies", {"browserContextId": ctx}, timeout=5).get("cookies")
        except CdpError:
            raise RenderError(REASONS["setup"], "cookie check") from None
        if cookies:  # never: the profile is empty and the context is new
            raise RenderError(REASONS["setup"], "cookies in a new context")
    except BaseException:
        try:
            pipe.call("Target.disposeBrowserContext", {"browserContextId": ctx}, timeout=3)
        except Exception:  # noqa: BLE001 - it dies with the browser either way
            pass
        raise
    return ctx


def _host_label(host: str) -> str:
    """The main document's host as a log may carry it: lower-case letters, digits, dots and hyphens, at most 100."""
    return re.sub(r"[^a-z0-9.\-]", "", host.lower())[:100]


def log_summary(plog, host: str | None) -> None:
    """The ONE line a render logs about the proxy's traffic (M4): how many connections it made, how many requests it refused
    with the count per reason code, how many broke with the count per exception type, and at most the main document's host
    (the reviewed link's own, cleaned; an attachment has none). The hosts a page asked for are strings it chose, so none of them
    is logged, and neither is any other per-request line."""
    refused = ", ".join(f"{k} x{n}" for k, n in sorted(plog.reasons.items()))
    broke = ", ".join(f"{k} x{n}" for k, n in sorted(plog.error_kinds.items()))
    log.info("site review: render of %s: %d connection(s), %d refused%s, %d broke%s", _host_label(host) if host else "an attachment",
             len(plog.connected), plog.refused_count, f" ({refused})" if refused else "", sum(plog.error_kinds.values()),
             f" ({broke})" if broke else "")


def _render(chrome: str, policy: Policy, open_page: Callable[[CdpPipe, str], str | None],
            expect: Callable[[str], bool], *, budget_s: float, resolver: Resolver, connector: Connector,
            launcher: Launcher, clock: Callable[[], float] = time.monotonic, host: str | None = None) -> Rendered:
    ensure_chrome_fresh(chrome)  # before anything is started: an out-of-date Chrome renders nothing (M11)
    deadline = Deadline(budget_s, clock)
    b = _Browser(chrome, policy, deadline, resolver=resolver, connector=connector, launcher=launcher)
    try:
        with b:
            pipe = b.pipe
            assert pipe is not None
            _deny_downloads(pipe, None)  # the default context too (its first tab is about:blank); never ignored
            ua = _desktop_ua(pipe)
            shots: list[Shot] = []
            for vp in (DESKTOP, PHONE):
                ctx = _setup_context(pipe, b.proxy.port)
                try:
                    shots.append(_Page(pipe, deadline, vp, ua).capture(ctx, open_page, expect, b.proxy))
                finally:
                    try:
                        pipe.call("Target.disposeBrowserContext", {"browserContextId": ctx}, timeout=3)
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        log_summary(b.proxy.log, host)  # once, however the render ended (the proxy has stopped: its counts are final)
    return Rendered(shots[0], shots[1], dict(b.proxy.log.reasons), len(b.proxy.log.connected), policy.offline,
                    b.downloads_found)


def _wrap(fn):
    """Chrome's own failures, as RenderErrors with a short reason."""
    try:
        return fn()
    except RenderError:
        raise
    except CdpTimeout:
        raise RenderError(REASONS["timeout"], "cdp") from None
    except (CdpClosed, CdpError):
        raise RenderError(REASONS["chrome"], "cdp") from None
    except (OSError, subprocess.SubprocessError):
        raise RenderError(REASONS["chrome"], "launch") from None
    except (KeyError, ValueError, TypeError, IndexError, AttributeError):
        raise RenderError(REASONS["chrome"], "protocol") from None  # M1: an answer of Chrome's that is not what was expected


def _networks(refused_networks: Iterable[str]) -> tuple:
    return tuple(ipaddress.ip_network(str(n).strip(), strict=False) for n in refused_networks)


def default_policy(refused_networks: Iterable[str] = (), networks: LocalNetworks | None = None) -> Policy:
    """The policy of a render of a link: the public web, except the networks of `site_review_refused_networks` and this
    Mac's own (`networks`: one render's view of its interfaces and routes, read when the first destination asks; the real
    lookups by default). ValueError for an entry that is not a network."""
    return Policy(refused=_networks(refused_networks), local=networks if networks is not None else LocalNetworks())


def check_link(url: str, policy: Policy | None = None, *, refused_networks: Iterable[str] = ()) -> None:
    """RenderError when the policy refuses the URL itself, before anything is opened or looked up: a scheme that is not http(s),
    a password in the link, a port but 80 and 443, an address that is not public (a literal), an IPv6 literal, an address in
    the networks of the setting, a name that can only mean an internal network. This is the part of the policy that needs no
    lookup; the proxy asks the rest for every connection. The service calls it before it counts a review (M10)."""
    try:
        policy = policy or Policy(refused=_networks(refused_networks))
        parts = urlsplit(url)
        policy.check_url_parts(parts.scheme.lower(), parts.hostname or "", parts.port, "@" in parts.netloc)
    except Refused as e:
        raise RenderError(e.description, f"url refused ({e.reason})") from None
    except ValueError:  # a port that is not a number, a bracket that does not close, a network of the setting that is not one
        raise RenderError(DESCRIPTIONS["name"], "url refused (form)") from None


def render_link(url: str, *, chrome: str, policy: Policy | None = None, budget_s: float = 45.0,
                resolver: Resolver = system_resolver, connector: Connector = open_connection,
                launcher: Launcher = launch_chrome, refused_networks: Iterable[str] = (),
                networks: LocalNetworks | None = None) -> Rendered:
    """Open `url` (after the policy has had its say about the URL itself) at both sizes. RenderError on any failure.
    `refused_networks` and `networks` shape the default policy (see `default_policy`); a `policy` given by a test replaces
    it."""
    try:
        policy = policy or default_policy(refused_networks, networks)
    except ValueError:
        raise RenderError(REASONS["setup"], "refused networks") from None
    check_link(url, policy)
    parts = urlsplit(url)

    def open_page(pipe: CdpPipe, session: str) -> str | None:
        res = pipe.call("Page.navigate", {"url": url}, session=session, timeout=15)
        return str(res["errorText"]) if res.get("errorText") else None

    def expect(href: str) -> bool:
        p = urlsplit(href)
        return p.scheme in ("http", "https") and bool(p.hostname)

    return _wrap(lambda: _render(chrome, policy, open_page, expect, budget_s=budget_s, resolver=resolver,
                                 connector=connector, launcher=launcher, host=parts.hostname or ""))


def csp_page(html_text: str) -> str:
    """The attachment with a Content-Security-Policy meta tag in front, so the page can load nothing from `file:`: no frame,
    image, script or style of another local file (it could otherwise put one into the screenshot). It is added after the
    doctype when there is one, so the page stays out of quirks mode."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{FILE_CSP}">'
    text = html_text.lstrip("﻿")
    m = re.match(r"\s*<!doctype[^>]*>", text, re.IGNORECASE)
    if m:
        return text[: m.end()] + meta + text[m.end():]
    return meta + text


def render_html(html_text: str, *, chrome: str, budget_s: float = 45.0, resolver: Resolver = system_resolver,
                connector: Connector = open_connection, launcher: Launcher = launch_chrome) -> Rendered:
    """Render an HTML attachment at both sizes, from a temp file that holds it alone, with the network off (the proxy
    refuses everything). The file is written inside `_wrap` like the rest of the render (M1), and its text is encoded with
    `replace`, so a stray surrogate in a teammate's file cannot fail the review."""
    def work() -> Rendered:
        page_dir = Path(tempfile.mkdtemp(prefix="haseeb-site-review-page-"))
        try:
            page = page_dir / "page.html"
            page.write_bytes(csp_page(html_text).encode("utf-8", "replace"))
            url = page.resolve().as_uri()

            def open_page(pipe: CdpPipe, session: str) -> str | None:
                res = pipe.call("Page.navigate", {"url": url}, session=session, timeout=15)
                return str(res["errorText"]) if res.get("errorText") else None

            def expect(href: str) -> bool:
                return href.split("#", 1)[0] == url

            return _render(chrome, Policy(offline=True), open_page, expect, budget_s=budget_s,
                           resolver=resolver, connector=connector, launcher=launcher)
        finally:
            shutil.rmtree(page_dir, ignore_errors=True)

    return _wrap(work)
