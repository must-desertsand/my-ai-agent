"""The renderer of the site review (haseeb_agent/site_review/chrome.py), against a FAKE Chrome.

No real Chrome runs here. `FakeChrome` speaks the DevTools pipe protocol on a thread, answers from a script, and can act as
the page: it can open connections to the filtering proxy exactly as Chrome would (a CONNECT to 127.0.0.1:37777, say), so a
test sees what the proxy does with a page's request inside a whole render. The proxy is the real one, with a fake resolver and
connector; the offline guard stays on. The one real-Chrome test is in test_site_review_chrome_integration.py."""
from __future__ import annotations

import base64
import glob
import json
import logging
import os
import re
import select
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from haseeb_agent.offline import OfflineError
from haseeb_agent.site_review import chrome as chrome_mod
from haseeb_agent.site_review import localnet
from haseeb_agent.site_review.localnet import LocalNetworks
from haseeb_agent.site_review.policy import Policy
from haseeb_agent.site_review.cdp import CdpClosed, CdpPipe, CdpTimeout, Deadline
from haseeb_agent.site_review.chrome import (DESKTOP, PHONE, Launched, RenderError, chrome_argv, clean_text, csp_page,
                                             launch_chrome, render_html, render_link)

JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg" * 10 + b"\xff\xd9"
PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def private_temp(tmp_path, monkeypatch):
    """The clean-up tests compare the temp folder before and after a render. Another process using the machine's shared temp
    folder at the same time (another test run, a session on another branch) would fail them for something that is not a leak,
    so each test gets a temp folder of its own."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


class Fail(Exception):
    """A handler's way to answer with a protocol error."""

    def __init__(self, code=-32000):
        self.code = code


class FakeChrome:
    """A stand-in for Chrome: `launcher` is what render_* are given. Handlers `(chrome, params, session) -> result` can
    emit events first (`chrome.emit`), raise Fail, or call chrome.die() to hang up."""

    def __init__(self, handlers: dict | None = None, info: dict | None = None):
        self.handlers = handlers or {}
        self.info = {"title": "Reach", "text": "Hello\n\n  world  ", "scrollWidth": 390, "clientWidth": 390,
                     "viewportMeta": True, "contentType": "text/html", **(info or {})}
        self.calls: list[tuple[str, dict, str | None]] = []
        self.argv: list[str] = []
        self.env: dict = {}
        self.url = "about:blank"
        self.killed = 0
        self.launches = 0
        self.dir_at_navigate: list[str] | None = None
        self._stop = threading.Event()
        self._cmd_r = self._out_w = -1
        self.profile_existed = False

    # ---- the Launcher
    def launcher(self, argv, env) -> Launched:
        self.launches += 1
        self.argv, self.env = list(argv), dict(env)
        self.profile = next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir="))
        self.profile_existed = os.path.isdir(self.profile)
        self.downloads = next((a.split("=", 1)[1] for a in argv if a.startswith("--download-default-directory=")), "")
        self.downloads_existed = os.path.isdir(self.downloads) if self.downloads else False
        r3, w3 = os.pipe()
        r4, w4 = os.pipe()
        self._cmd_r, self._out_w = r3, w4
        threading.Thread(target=self._serve, daemon=True).start()
        return Launched(self, read_fd=r4, write_fd=w3, kill=self._kill)

    def wait(self, timeout=None):
        return 0

    @property
    def proxy_port(self) -> int:
        return int(next(a for a in self.argv if a.startswith("--proxy-server=")).rsplit(":", 1)[1])

    def _kill(self):
        self.killed += 1
        self._stop.set()

    # ---- the wire
    def _write(self, msg: dict) -> None:
        try:
            os.write(self._out_w, json.dumps(msg).encode() + b"\0")
        except OSError:
            pass

    def emit(self, method: str, params: dict | None = None, session: str | None = "s1") -> None:
        msg = {"method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._write(msg)

    def die(self) -> None:
        self._stop.set()
        try:
            os.close(self._out_w)
        except OSError:
            pass

    def _serve(self) -> None:
        buf = b""
        while not self._stop.is_set():
            ready, _, _ = select.select([self._cmd_r], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self._cmd_r, 65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                self._handle(json.loads(raw))

    def _handle(self, msg: dict) -> None:
        method, params, session, mid = msg["method"], msg.get("params", {}), msg.get("sessionId"), msg["id"]
        self.calls.append((method, params, session))
        handler = self.handlers.get(method) or DEFAULTS.get(method)
        try:
            result = handler(self, params, session) if handler else {}
        except Fail as e:
            self._write({"id": mid, "error": {"code": e.code, "message": "no"}})
            return
        if result is not None:
            self._write({"id": mid, "result": result})

    # ---- helpers for assertions
    def methods(self) -> list[str]:
        return [m for m, _p, _s in self.calls]

    def params(self, method: str) -> list[dict]:
        return [p for m, p, _s in self.calls if m == method]


def _navigate(chrome: FakeChrome, params, session, *, status=200, mime="text/html", hops=0, final=None, error=None):
    url = params["url"]
    chrome.url = final or url
    if error:
        return {"frameId": "F1", "errorText": error}
    if url.startswith("file://"):
        chrome.dir_at_navigate = sorted(os.listdir(os.path.dirname(url[len("file://"):])))
    chrome.emit("Network.requestWillBeSent", {"requestId": "r0", "type": "Document", "frameId": "F1"})
    for i in range(hops):
        chrome.emit("Network.requestWillBeSent", {"requestId": f"r{i + 1}", "type": "Document", "frameId": "F1",
                                                  "redirectResponse": {"status": 302}})
    last = f"r{hops}"
    chrome.emit("Network.responseReceived", {"requestId": last, "type": "Document",
                                             "response": {"status": status, "mimeType": mime}})
    chrome.emit("Page.frameNavigated", {"frame": {"id": "F1", "url": chrome.url}})
    chrome.emit("Network.loadingFinished", {"requestId": last})
    chrome.emit("Page.loadEventFired", {"timestamp": 1})
    return {"frameId": "F1", "loaderId": "L1"}


def _evaluate(chrome: FakeChrome, params, session):
    return {"result": {"type": "object", "value": {**chrome.info, "href": chrome.url}}}


DEFAULTS = {
    "Browser.getVersion": lambda c, p, s: {"userAgent": "Mozilla/5.0 (Macintosh) HeadlessChrome/130.0.0.0 Safari/537.36"},
    "Target.createBrowserContext": lambda c, p, s: {"browserContextId": f"ctx{len(c.params('Target.createBrowserContext'))}"},
    "Target.createTarget": lambda c, p, s: {"targetId": f"t{len(c.params('Target.createTarget'))}"},
    "Target.attachToTarget": lambda c, p, s: {"sessionId": "s1"},
    "Page.getFrameTree": lambda c, p, s: {"frameTree": {"frame": {"id": "F1"}}},
    "Page.createIsolatedWorld": lambda c, p, s: {"executionContextId": 7},
    "Page.navigate": _navigate,
    "Runtime.evaluate": _evaluate,
    "Page.captureScreenshot": lambda c, p, s: {"data": base64.b64encode(JPEG).decode()},
    "Storage.getCookies": lambda c, p, s: {"cookies": []},
}


REAL_FRESH = chrome_mod.ensure_chrome_fresh   # the age check itself is tested below; the fake Chromes have no bundle


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """The settle waits are real time; shorten them so a render takes a fraction of a second. The Chrome age check (M11)
    reads an app bundle that a fake Chrome does not have, so it is replaced here and tested on its own."""
    monkeypatch.setattr(chrome_mod, "ensure_chrome_fresh", lambda chrome, **kw: None)
    monkeypatch.setattr(chrome_mod, "SETTLE_MIN_S", 0.02)
    monkeypatch.setattr(chrome_mod, "SETTLE_IDLE_S", 0.02)
    monkeypatch.setattr(chrome_mod, "SETTLE_MAX_S", 0.5)


async def _public(host, port):
    return [PUBLIC]


async def _no_connect(ip, port):
    raise AssertionError(f"the proxy connected to {ip}:{port}")


def link(chrome: FakeChrome, url="https://reach-poc.vercel.app/", **kw):
    return render_link(url, chrome="/fake/chrome", launcher=chrome.launcher, resolver=_public, connector=_no_connect, **kw)


def html(chrome: FakeChrome, text="<!doctype html><title>x</title><h1>Hi</h1>", **kw):
    return render_html(text, chrome="/fake/chrome", launcher=chrome.launcher, resolver=_public, connector=_no_connect, **kw)


def connect_through(chrome: FakeChrome, target: str) -> bytes:
    """What Chrome would do for a page's request: ask the proxy it was started with."""
    with socket.create_connection(("127.0.0.1", chrome.proxy_port), timeout=5) as s:
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        return s.recv(4096)


# ---------------------------------------------------------------- a whole render

def test_a_link_is_rendered_at_both_sizes_with_what_the_review_needs():
    c = FakeChrome(info={"title": "Reach: command center", "text": "Account executive\n\n\nClient\tview", "scrollWidth": 807,
                         "clientWidth": 390, "viewportMeta": True})
    r = link(c, "https://reach-poc.vercel.app/")
    assert (r.desktop.size, r.phone.size) == ("desktop", "phone") and r.offline is False
    for shot in (r.desktop, r.phone):
        assert shot.jpeg == JPEG and shot.title == "Reach: command center"
        assert shot.text == "Account executive\n\nClient view"  # control characters and blank runs squeezed
        assert (shot.scroll_width, shot.client_width, shot.viewport_meta) == (807, 390, True)
        assert shot.status == 200 and shot.console_errors == shot.exceptions == shot.log_errors == shot.dialogs == 0
    metrics = c.params("Emulation.setDeviceMetricsOverride")
    assert [(m["width"], m["height"], m["mobile"]) for m in metrics] == [(1440, 900, False), (390, 844, True)]
    shots = c.params("Page.captureScreenshot")
    assert [s["format"] for s in shots] == ["jpeg", "jpeg"]
    clips = [s["clip"] for s in shots]
    assert (clips[0]["width"], clips[0]["height"], clips[0]["scale"]) == (1440, 900, 1.0)
    assert (clips[1]["width"], clips[1]["height"]) == (390, 844)
    for clip in clips:  # at most 1,600 px on the longest side
        assert max(clip["width"], clip["height"]) * clip["scale"] <= 1600
    assert (r.desktop.width_px, r.desktop.height_px) == (1440, 900) and max(r.phone.width_px, r.phone.height_px) <= 1600
    uas = [p["userAgent"] for p in c.params("Emulation.setUserAgentOverride")]
    assert "HeadlessChrome" not in uas[0] and "Chrome/130" in uas[0] and "iPhone" in uas[1] and "Mobile" in uas[1]
    assert c.params("Emulation.setTouchEmulationEnabled") == [{"enabled": True, "maxTouchPoints": 5}]
    denies = c.params("Browser.setDownloadBehavior")  # the default context, then each browser context in use
    assert denies == [{"behavior": "deny"}, {"behavior": "deny", "browserContextId": "ctx1"},
                      {"behavior": "deny", "browserContextId": "ctx2"}]


def test_each_size_is_a_fresh_browser_context_so_nothing_carries_over():
    c = FakeChrome()
    link(c)
    assert [p["disposeOnDetach"] for p in c.params("Target.createBrowserContext")] == [True, True]
    assert [p["browserContextId"] for p in c.params("Target.createTarget")] == ["ctx1", "ctx2"]
    assert c.methods().count("Target.disposeBrowserContext") == 2 and c.methods().count("Target.closeTarget") == 2
    assert [p["url"] for p in c.params("Page.navigate")] == ["https://reach-poc.vercel.app/"] * 2


def test_nothing_is_clicked_typed_or_submitted():
    c = FakeChrome()
    link(c)
    assert not [m for m in c.methods() if m.startswith("Input.") or m in ("Page.setInterceptFileChooserDialog",
                                                                         "Fetch.fulfillRequest")]
    for expr in (p["expression"] for p in c.params("Runtime.evaluate")):
        assert "click" not in expr and "submit" not in expr and ".value" not in expr  # it only reads


def test_the_checks_are_read_in_an_isolated_world_the_page_cannot_answer_for():
    c = FakeChrome()
    link(c)
    assert c.params("Page.createIsolatedWorld")[0]["worldName"] == "site-review"
    assert all(p.get("contextId") == 7 for p in c.params("Runtime.evaluate"))


def _no_world(c, p, s):
    raise Fail()


@pytest.mark.parametrize("handlers", [
    {"Page.createIsolatedWorld": _no_world},                                      # Chrome refuses to make it
    {"Page.createIsolatedWorld": lambda c, p, s: {}},                             # and answers without an id
    {"Page.createIsolatedWorld": lambda c, p, s: {"executionContextId": "7"}},    # or with one that is not a number
    {"Page.createIsolatedWorld": lambda c, p, s: {"executionContextId": True}},
])
def test_when_the_isolated_world_cannot_be_made_the_render_fails_and_the_page_is_never_asked_directly(handlers):
    """M3: asking the page's own world would let its scripts replace Math.max or String and forge the measured numbers."""
    c = FakeChrome(handlers)
    with pytest.raises(RenderError) as e:
        link(c)
    assert (e.value.reason, e.value.detail) == ("browser setup", "isolated world")
    assert not c.params("Runtime.evaluate") and not c.params("Page.captureScreenshot")  # nothing was read, nothing was shot


def test_an_isolated_world_that_cannot_make_the_page_fail_cleanly_is_a_failed_render_with_no_second_try_in_the_page():
    sent = []

    def evaluate(c, p, s):
        sent.append(p.get("contextId"))
        return {"result": {"type": "object", "value": {"error": "TypeError"}}}  # the extraction itself threw
    c = FakeChrome({"Runtime.evaluate": evaluate})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "couldn't load the page" and e.value.detail == "evaluate"
    assert sent == [7]  # asked once, in the isolated world; never again in the page's own (no contextId)


def test_a_failed_world_is_a_setup_failure_for_the_dm_line():
    c = FakeChrome({"Page.createIsolatedWorld": _no_world})
    with pytest.raises(RenderError) as e:
        link(c)
    assert (e.value.reason, e.value.detail) == ("browser setup", "isolated world")


def test_a_page_that_replaces_its_own_math_and_string_cannot_change_what_is_measured():
    """The numbers come from the isolated world: whatever the page does to its own globals, the values read are the ones
    the fake isolated world returns, and every evaluation is addressed to it."""
    c = FakeChrome(info={"scrollWidth": 807, "clientWidth": 390})
    r = link(c)
    assert (r.phone.scroll_width, r.phone.client_width) == (807, 390)
    assert [p["contextId"] for p in c.params("Runtime.evaluate")] == [7, 7, 7, 7]  # two sizes, before and after the shot


def test_the_text_is_capped_and_cleaned():
    c = FakeChrome(info={"text": ("word " * 5000)})
    r = link(c)
    assert len(r.desktop.text) == 6000
    assert clean_text("a\x00b\x07c d  e\n\n\n\nf \n g") == "a b c d e\n\nf\ng"


def test_failed_requests_and_errors_are_counted_by_short_type_only():
    def navigate(c, p, s):
        res = _navigate(c, p, s)
        c.emit("Network.requestWillBeSent", {"requestId": "x1", "type": "Image"})
        c.emit("Network.loadingFailed", {"requestId": "x1", "errorText": "net::ERR_BLOCKED_BY_CLIENT <script>alert(1)</script>"})
        c.emit("Network.requestWillBeSent", {"requestId": "x2", "type": "Script"})
        c.emit("Network.loadingFailed", {"requestId": "x2", "errorText": "net::ERR_TUNNEL_CONNECTION_FAILED"})
        c.emit("Network.requestWillBeSent", {"requestId": "x3", "type": "Script"})
        c.emit("Network.loadingFailed", {"requestId": "x3", "errorText": "net::ERR_TUNNEL_CONNECTION_FAILED"})
        c.emit("Runtime.consoleAPICalled", {"type": "error", "args": [{"value": "secret page words"}]})
        c.emit("Runtime.consoleAPICalled", {"type": "log", "args": []})
        c.emit("Runtime.exceptionThrown", {"exceptionDetails": {"text": "Uncaught ReferenceError: secret"}})
        c.emit("Log.entryAdded", {"entry": {"level": "error", "text": "Failed to load resource"}})
        c.emit("Log.entryAdded", {"entry": {"level": "warning", "text": "careful"}})
        return res
    r = link(FakeChrome({"Page.navigate": navigate}))
    s = r.desktop
    assert s.failed == {"ERR_BLOCKED_BY_CLIENTscriptalert1script": 1, "ERR_TUNNEL_CONNECTION_FAILED": 2} or \
        s.failed == {"ERR_BLOCKED_BY_CLIENT": 1, "ERR_TUNNEL_CONNECTION_FAILED": 2} or \
        sum(s.failed.values()) == 3
    assert all(len(k) <= 40 and k.replace("_", "").isalnum() for k in s.failed)  # a short type, never a message
    assert (s.console_errors, s.exceptions, s.log_errors) == (1, 1, 1)
    assert "secret" not in repr(r.desktop.failed)


def test_a_dialog_is_dismissed_and_counted_so_the_page_cannot_hang_the_render():
    def navigate(c, p, s):
        res = _navigate(c, p, s)
        c.emit("Page.javascriptDialogOpening", {"type": "alert", "message": "hi"})
        return res
    c = FakeChrome({"Page.navigate": navigate, "Page.handleJavaScriptDialog": lambda c, p, s: {}})
    r = link(c)
    assert r.desktop.dialogs == 1 and c.params("Page.handleJavaScriptDialog")[0] == {"accept": False}


# ---------------------------------------------------------------- what stops a render

@pytest.mark.parametrize("error,reason", [
    ("net::ERR_NAME_NOT_RESOLVED", "address not found"), ("net::ERR_CONNECTION_REFUSED", "couldn't connect"),
    ("net::ERR_CONNECTION_TIMED_OUT", "couldn't connect"), ("net::ERR_CERT_AUTHORITY_INVALID", "certificate problem"),
    ("net::ERR_SSL_PROTOCOL_ERROR", "certificate problem"), ("net::ERR_TOO_MANY_REDIRECTS", "too many redirects"),
    ("net::ERR_ABORTED", "not a web page"), ("net::ERR_SOMETHING_ELSE", "couldn't load the page"),
])
def test_a_navigation_that_fails_is_a_short_reason(error, reason):
    c = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, error=error)})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == reason and c.killed == 1


@pytest.mark.parametrize("status", [401, 403, 404, 410, 500, 503])
def test_an_error_page_is_not_reviewed(status):
    c = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, status=status)})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == f"HTTP {status}"
    assert "Page.captureScreenshot" not in c.methods()  # nothing was photographed


@pytest.mark.parametrize("mime", ["application/pdf", "image/png", "application/zip", "application/json"])
def test_what_is_not_a_web_page_is_not_reviewed(mime):
    c = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, mime=mime)})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "not a web page" and "Page.captureScreenshot" not in c.methods()


def test_up_to_three_redirects_are_followed_and_a_fourth_is_not():
    ok = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, hops=3, final="https://elsewhere.example.com/")})
    assert link(ok).desktop.status == 200
    bad = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, hops=4)})
    with pytest.raises(RenderError) as e:
        link(bad)
    assert e.value.reason == "too many redirects" and "Page.captureScreenshot" not in bad.methods()


def test_a_page_that_never_finishes_loading_runs_out_of_its_budget_and_chrome_is_killed():
    def hang(c, p, s):
        c.url = p["url"]
        c.emit("Network.requestWillBeSent", {"requestId": "r0", "type": "Document", "frameId": "F1"})
        return {"frameId": "F1"}  # no load event, ever
    c = FakeChrome({"Page.navigate": hang})
    t0 = time.time()
    with pytest.raises(RenderError) as e:
        link(c, budget_s=1.0)
    assert e.value.reason == "timed out" and time.time() - t0 < 4 and c.killed == 1


def test_a_chrome_that_stops_answering_is_a_timeout_not_a_hang():
    c = FakeChrome({"Page.enable": lambda c, p, s: None})  # never replies
    t0 = time.time()
    with pytest.raises(RenderError) as e:
        link(c, budget_s=1.0)
    assert e.value.reason == "timed out" and time.time() - t0 < 4 and c.killed == 1


def test_a_chrome_that_dies_mid_render_is_a_chrome_failure():
    def die(c, p, s):
        c.die()
        return None
    c = FakeChrome({"Page.navigate": die})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "Chrome failed" and c.killed == 1


def test_a_chrome_that_will_not_start_a_tab_is_a_chrome_failure():
    def no(c, p, s):
        raise Fail()
    with pytest.raises(RenderError) as e:
        link(FakeChrome({"Target.createTarget": no}))
    assert e.value.reason == "Chrome failed"


def test_an_attachment_that_moves_on_between_the_first_look_and_the_photograph_is_discarded():
    """The page navigated (to another local file, say) after it was checked and before it was photographed: nothing of
    it is kept."""
    calls = {"n": 0}

    def evaluate(c, p, s):
        calls["n"] += 1
        href = c.url if calls["n"] % 2 == 1 else "file:///etc/hosts"  # each size: fine at the first look, moved by the second
        return {"result": {"value": {**c.info, "href": href}}}
    c = FakeChrome({"Runtime.evaluate": evaluate})
    with pytest.raises(RenderError) as e:
        html(c)
    assert e.value.reason == "the page navigated away"
    assert c.methods().count("Page.captureScreenshot") == 1  # it was photographed once, then thrown away


def test_a_link_may_move_on_to_another_public_page_by_itself():
    """A landing page that sends the browser on (a script, a meta refresh): every address it goes to was checked by the
    proxy when it was connected to, so the render takes the page it ends up on."""
    c = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, final="https://app.example.com/home")})
    assert link(c).desktop.title == "Reach"


def test_a_link_cannot_end_up_on_a_page_that_is_not_http():
    def evaluate(c, p, s):
        return {"result": {"value": {**c.info, "href": "file:///etc/hosts"}}}
    c = FakeChrome({"Runtime.evaluate": evaluate})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "the page navigated away"
    assert "Page.captureScreenshot" not in c.methods()  # not even photographed: it was caught at the first look


def test_a_screenshot_that_is_not_a_jpeg_is_a_chrome_failure():
    c = FakeChrome({"Page.captureScreenshot": lambda c, p, s: {"data": base64.b64encode(b"<html>not a jpeg").decode()}})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "Chrome failed"


def test_a_blank_evaluate_is_a_load_failure():
    c = FakeChrome({"Runtime.evaluate": lambda c, p, s: {"result": {"value": None}}})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "couldn't load the page"


# ---------------------------------------------------------------- the leash: URL checks, argv, cleanup

@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1:8765/", "port not allowed"), ("http://127.0.0.1/", "not a public address"),
    ("http://127.0.0.1:37777/api/search", "port not allowed"), ("https://192.168.1.10/admin", "not a public address"),
    ("http://169.254.169.254/latest/meta-data/", "not a public address"), ("http://[::1]/", "IPv6 addresses are not used"),
    ("https://[2606:4700:4700::1111]/", "IPv6 addresses are not used"),
    ("http://localhost/", "not a public web address"), ("http://intranet/", "not a public web address"),
    ("https://example.com:8443/", "port not allowed"), ("https://user:secret@example.com/", "the link has a password in it"),
    ("ftp://example.com/", "not an http(s) link"), ("http://2130706433/", "not a public address"),
    ("http://0x7f.1/", "not a public address"),
])
def test_a_link_the_policy_refuses_never_starts_chrome(url, reason):
    c = FakeChrome()
    with pytest.raises(RenderError) as e:
        link(c, url)
    assert e.value.reason == reason and c.launches == 0


def test_check_link_is_the_part_of_the_policy_that_needs_no_lookup_and_no_chrome():
    chrome_mod.check_link("https://reach-poc.vercel.app/x?y=1")
    chrome_mod.check_link("http://93.184.216.34/")
    for url, reason in (("http://10.0.0.1/", "not a public address"), ("https://example.com:8443/", "port not allowed"),
                        ("http://[2606:4700::1111]/", "IPv6 addresses are not used"), ("http://localhost/", "not a public web address"),
                        ("https://u:p@example.com/", "the link has a password in it"), ("ftp://example.com/", "not an http(s) link")):
        with pytest.raises(RenderError) as e:
            chrome_mod.check_link(url)
        assert e.value.reason == reason and e.value.detail.startswith("url refused"), url
    for odd in ("http://example.com:99999/", "http://[::1/", "http://example.com:abc/"):  # a form urlsplit itself rejects
        with pytest.raises(RenderError) as e:
            chrome_mod.check_link(odd)
        assert e.value.reason == "not a public web address" and e.value.detail == "url refused (form)"
    with pytest.raises(RenderError):
        chrome_mod.check_link("http://93.184.216.34/", refused_networks=["93.184.216.0/24"])
    with pytest.raises(RenderError) as e:
        chrome_mod.check_link("https://reach-poc.vercel.app/", refused_networks=["nonsense"])
    assert e.value.detail == "url refused (form)"


def test_chrome_is_started_with_every_flag_that_keeps_the_page_on_its_leash():
    argv = chrome_argv("/fake/chrome", "/tmp/profile-x", 54321)
    assert argv[0] == "/fake/chrome" and argv[-1] == "about:blank"
    for flag in ("--headless=new", "--user-data-dir=/tmp/profile-x", "--proxy-server=http://127.0.0.1:54321",
                 "--proxy-bypass-list=<-loopback>", "--disable-quic", "--remote-debugging-pipe",
                 "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-extensions", "--disable-sync",
                 "--deny-permission-prompts", "--block-new-web-contents", "--disable-background-networking",
                 "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"):
        assert flag in argv, flag
    assert not any(a.startswith(("--remote-debugging-port", "--no-proxy-server", "--proxy-auto-detect", "--no-sandbox",
                                 "--disable-web-security", "--allow-file-access", "--enable-quic")) for a in argv)
    assert "--proxy-bypass-list=<-loopback>" in argv  # the one that stops Chrome keeping loopback for itself


def test_the_chrome_that_was_started_has_a_throwaway_profile_the_proxys_port_and_a_clean_environment():
    c = FakeChrome()
    link(c)
    assert c.profile_existed and "haseeb-site-review-chrome-" in c.profile
    assert f"--proxy-server=http://127.0.0.1:{c.proxy_port}" in c.argv
    assert set(c.env) == {"HOME", "TMPDIR", "PATH", "LANG"} and c.env["HOME"] == c.profile
    assert os.environ.get("LITELLM_API_KEY") not in c.env.values()


def test_everything_is_cleaned_up_after_a_render_whatever_happened(monkeypatch):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*")))
    ok = FakeChrome()
    link(ok)
    bad = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, error="net::ERR_CONNECTION_REFUSED")})
    with pytest.raises(RenderError):
        link(bad)
    for c in (ok, bad):
        assert not os.path.exists(c.profile) and c.killed == 1
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", c.proxy_port), timeout=1)  # the proxy is gone
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*"))) == before


def test_the_real_launcher_is_behind_the_offline_guard_and_leaves_nothing_behind():
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*")))
    with pytest.raises(OfflineError):
        render_link("https://example.com/", chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    launcher=launch_chrome, resolver=_public, connector=_no_connect)
    with pytest.raises(OfflineError):
        render_html("<h1>x</h1>", chrome="/x", launcher=launch_chrome)
    with pytest.raises(OfflineError):
        launch_chrome(["/bin/echo"], {})
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*"))) == before


# ---------------------------------------------------------------- an out-of-date Chrome renders nothing (security review M11)

def fake_bundle(tmp_path, age_days, *, version="155.0.8059.85", plist=True, binary_age_days=None, name="Google Chrome"):
    """An app bundle shaped like Chrome's, with an Info.plist `age_days` old; returns the path of its binary."""
    app = tmp_path / "Applications" / f"{name}.app"
    binary = app / "Contents" / "MacOS" / name
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    now = time.time()
    os.utime(binary, (now - (binary_age_days if binary_age_days is not None else age_days) * 86400,) * 2)
    if plist:
        p = app / "Contents" / "Info.plist"
        import plistlib
        p.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version, "CFBundleVersion": "8059.85"}))
        os.utime(p, (now - age_days * 86400,) * 2)
    return str(binary)


@pytest.fixture
def real_age_check(monkeypatch):
    """The real age check: it is a real step behind the offline guard, so the guard is lifted for a bundle made in tmp."""
    monkeypatch.setattr(chrome_mod, "ensure_chrome_fresh", REAL_FRESH)
    monkeypatch.setattr(chrome_mod, "guard", lambda what: None)


def test_the_version_and_the_date_come_from_the_app_bundles_info_plist(tmp_path):
    binary = fake_bundle(tmp_path, 10, version="155.0.8059.85", binary_age_days=400)
    version, built = chrome_mod.chrome_build(binary)
    assert version == "155.0.8059.85" and abs((time.time() - built) / 86400 - 10) < 0.01  # the plist's date, not the binary's
    this_mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(this_mac):  # (a read of this Mac's own bundle: nothing is started)
        v, b = chrome_mod.chrome_build(this_mac)
        assert re.fullmatch(r"\d+(\.\d+)+", v) and b > 1.6e9


def test_a_chrome_built_within_45_days_renders_and_an_older_one_does_not(tmp_path, real_age_check):
    for age in (0, 10, 44.9):
        REAL_FRESH(fake_bundle(tmp_path / f"a{age}", age))
    for age in (45.1, 46, 62, 400):
        with pytest.raises(chrome_mod.ChromeOutOfDate) as e:
            REAL_FRESH(fake_bundle(tmp_path / f"b{age}", age))
        assert (e.value.reason, e.value.detail) == ("Chrome is out of date \u2014 update Chrome", "chrome age")
        assert e.value.version == "155.0.8059.85" and e.value.age_days == int(age)
    assert chrome_mod.CHROME_MAX_AGE.days == 45 and isinstance(e.value, RenderError)


def test_the_limit_and_the_clock_are_arguments(tmp_path, real_age_check):
    binary = fake_bundle(tmp_path, 30)
    REAL_FRESH(binary)
    with pytest.raises(chrome_mod.ChromeOutOfDate):
        REAL_FRESH(binary, now=time.time() + 20 * 86400)  # in 20 days it will be 50 days old
    with pytest.raises(chrome_mod.ChromeOutOfDate):
        REAL_FRESH(binary, max_age=chrome_mod.timedelta(days=10))


def test_a_binary_outside_a_bundle_or_with_a_plist_that_cannot_be_read_is_dated_by_the_binary(tmp_path, real_age_check):
    loose = tmp_path / "bin" / "chromium"
    loose.parent.mkdir()
    loose.write_bytes(b"x")
    os.utime(loose, (time.time() - 50 * 86400,) * 2)
    with pytest.raises(chrome_mod.ChromeOutOfDate) as e:
        REAL_FRESH(str(loose))
    assert e.value.version == "unknown" and e.value.age_days == 50
    os.utime(loose, (time.time() - 5 * 86400,) * 2)
    REAL_FRESH(str(loose))
    broken = fake_bundle(tmp_path / "broken", 5, binary_age_days=50)
    (Path(broken).parents[1] / "Info.plist").write_bytes(b"this is not a plist")
    with pytest.raises(chrome_mod.ChromeOutOfDate) as e:
        REAL_FRESH(broken)
    assert e.value.version == "unknown" and e.value.age_days == 50


def test_a_symlink_to_the_binary_is_followed_to_its_bundle(tmp_path, real_age_check):
    binary = fake_bundle(tmp_path / "b", 60)
    link_path = tmp_path / "usr" / "local" / "bin" / "chrome"
    link_path.parent.mkdir(parents=True)
    link_path.symlink_to(binary)
    with pytest.raises(chrome_mod.ChromeOutOfDate) as e:
        REAL_FRESH(str(link_path))
    assert e.value.age_days == 60 and e.value.version == "155.0.8059.85"


def test_a_chrome_that_is_not_there_is_a_chrome_failure(tmp_path, real_age_check):
    with pytest.raises(RenderError) as e:
        REAL_FRESH(str(tmp_path / "nowhere" / "Google Chrome"))
    assert (e.value.reason, e.value.detail) == ("Chrome failed", "chrome path")


def test_a_version_string_is_cleaned_before_it_is_logged(tmp_path, real_age_check):
    assert chrome_mod.chrome_build(fake_bundle(tmp_path, 1, version="155.0\n[evil] <x>"))[0] == "155.0evilx"
    assert chrome_mod.chrome_build(fake_bundle(tmp_path / "long", 1, version="1" * 200))[0] == "1" * 30


def test_the_age_check_is_a_real_step_behind_the_offline_guard(tmp_path):
    with pytest.raises(OfflineError):
        REAL_FRESH(fake_bundle(tmp_path, 1))


def test_an_out_of_date_chrome_renders_nothing_and_nothing_is_started(tmp_path, real_age_check):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*")))
    stale = fake_bundle(tmp_path, 62)
    c = FakeChrome()
    with pytest.raises(chrome_mod.ChromeOutOfDate) as e:
        render_link("https://reach-poc.vercel.app/", chrome=stale, launcher=c.launcher, resolver=_public, connector=_no_connect)
    assert e.value.reason == "Chrome is out of date \u2014 update Chrome" and c.launches == 0
    with pytest.raises(chrome_mod.ChromeOutOfDate):
        render_html("<h1>x</h1>", chrome=stale, launcher=c.launcher, resolver=_public, connector=_no_connect)
    assert c.launches == 0
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*"))) == before  # no profile, no page file
    r = render_link("https://reach-poc.vercel.app/", chrome=fake_bundle(tmp_path / "fresh", 3), launcher=c.launcher,
                    resolver=_public, connector=_no_connect)
    assert c.launches == 1 and r.desktop.size == "desktop"  # a current Chrome is let through


def test_the_age_is_checked_before_a_render_starts_anything(tmp_path, real_age_check, monkeypatch):
    order = []
    real = chrome_mod._Browser.__init__
    monkeypatch.setattr(chrome_mod._Browser, "__init__", lambda self, *a, **kw: (order.append("browser"), real(self, *a, **kw))[1])
    monkeypatch.setattr(chrome_mod, "ensure_chrome_fresh", lambda chrome, **kw: (order.append("age"), REAL_FRESH(chrome, **kw))[1])
    render_link("https://reach-poc.vercel.app/", chrome=fake_bundle(tmp_path, 3), launcher=FakeChrome().launcher,
                resolver=_public, connector=_no_connect)
    assert order == ["age", "browser"]


def test_the_real_launcher_hands_the_child_the_two_protocol_descriptors_and_no_other_one(monkeypatch):
    """Security review M9: Chrome renders hostile pages, so it must not inherit a descriptor the tick happens to hold. A child
    process (not Chrome: this interpreter, local only) is started by the real launcher while the tick has an INHERITABLE
    descriptor open; it reports the descriptors it has, and echoes a byte string sent to fd 3 back on fd 4."""
    import sys
    monkeypatch.setattr(chrome_mod, "guard", lambda what: None)
    script = (
        "import json, os\n"
        "fds = []\n"
        "for fd in sorted(int(x) for x in os.listdir('/dev/fd')):\n"
        "    try:\n"
        "        os.fstat(fd); fds.append(fd)\n"
        "    except OSError:\n"
        "        pass\n"
        "os.write(4, json.dumps(fds).encode() + b'\\n')\n"
        "os.write(4, os.read(3, 5) + b'\\n')\n")
    leak = os.open(__file__, os.O_RDONLY)
    os.set_inheritable(leak, True)  # what a library that forgot O_CLOEXEC would leave behind
    launched = None
    try:
        launched = launch_chrome([sys.executable, "-I", "-c", script], {"PATH": "/usr/bin:/bin"})
        os.write(launched.write_fd, b"ping!")
        out = b""
        while out.count(b"\n") < 2:
            ready, _, _ = select.select([launched.read_fd], [], [], 10)
            assert ready, f"the child said nothing: {out!r}"
            chunk = os.read(launched.read_fd, 4096)
            assert chunk, f"the child hung up: {out!r}"
            out += chunk
        listing, echo = out.split(b"\n")[:2]
        assert json.loads(listing) == [0, 1, 2, 3, 4], f"the child holds {listing!r} (the leaked descriptor was {leak})"
        assert echo == b"ping!"  # and 3 and 4 are the pipes: commands in, replies out
        assert launched.proc.wait(10) == 0
    finally:
        os.close(leak)
        if launched is not None:
            launched.kill()
            for fd in (launched.read_fd, launched.write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass


# ---------------------------------------------------------------- the page's requests meet the real proxy

def test_a_pages_requests_to_this_macs_own_services_and_the_lan_are_refused_inside_a_render():
    """The page asks for the claude-mem worker, the dashboard, a LAN host and a name that rebinds to loopback, by the only
    road Chrome has (the proxy it was started with). Every one is refused, nothing is connected, and the render says so."""
    results = {}

    def navigate(c, p, s):
        res = _navigate(c, p, s)
        if not results:
            for target in ("127.0.0.1:37777", "127.0.0.1:8765", "192.168.1.10:80", "169.254.169.254:80",
                           "rebind.example.com:443", "localhost:80", "[::1]:443", "10.0.0.5:443"):
                results[target] = connect_through(c, target)
        return res

    async def rebinding(host, port):
        return ["127.0.0.1"] if host == "rebind.example.com" else [PUBLIC]

    c = FakeChrome({"Page.navigate": navigate})
    r = render_link("https://reach-poc.vercel.app/", chrome="/fake", launcher=c.launcher, resolver=rebinding,
                    connector=_no_connect)  # _no_connect: a connect to anything would fail the test
    assert set(results) and all(v.startswith(b"HTTP/1.1 403") for v in results.values()), results
    assert r.refused == {"port": 2, "address": 4, "name": 1, "ipv6": 1}  # reasons and counts; no address, no host


# ---------------------------------------------------------------- Haseeb's own networks (security review I1)

class Mac:
    """The interfaces and routes of a Mac, injected (the real lookups are behind the offline guard)."""

    def __init__(self, interfaces=("81.2.69.142/24",), routes=None, default="en0"):
        self.interfaces, self.routes, self.default, self.route_calls = list(interfaces), dict(routes or {}), default, []

    def networks(self):
        def route(dest):
            self.route_calls.append(dest)
            got = self.default if dest == "default" else self.routes.get(dest, self.default)
            if isinstance(got, Exception):
                raise got
            return got
        return LocalNetworks(lambda: self.interfaces, route)


def asks(c, *targets):
    out = {}

    def navigate(chrome, p, s):
        res = _navigate(chrome, p, s)
        if not out:
            for t in targets:
                out[t] = connect_through(chrome, t)
        return res
    c.handlers["Page.navigate"] = navigate
    return out


def test_a_page_cannot_reach_the_macs_lan_the_mac_itself_or_a_vpn_range_through_the_proxy_inside_a_render():
    """Public-looking addresses that are not the Internet: a LAN with a public /24, the Mac's own address on it, and a range
    that a VPN routes to a private network. The page asks for each by name and by address; every one is refused and none is
    connected to; the control name goes through to the connector."""
    names = {"nas.example.com": "81.2.69.50", "me.example.com": "81.2.69.142", "work.example.com": "34.120.5.77"}
    dialled = []

    async def resolver(host, port):
        return [names.get(host, PUBLIC)]

    async def connector(ip, port):
        dialled.append((ip, port))
        raise OSError("no road in a test")
    c = FakeChrome()
    out = asks(c, "nas.example.com:443", "me.example.com:80", "work.example.com:443", "81.2.69.7:80", "34.120.0.9:443",
               "reach-poc.vercel.app:443")
    mac = Mac(routes={"34.120.5.77": "utun4", "34.120.0.9": "utun4"})
    r = render_link("https://reach-poc.vercel.app/", chrome="/fake", launcher=c.launcher, resolver=resolver,
                    connector=connector, networks=mac.networks())
    assert all(v.startswith(b"HTTP/1.1 403") for t, v in out.items() if t != "reach-poc.vercel.app:443"), out
    assert dialled == [(PUBLIC, 443)]  # only the control; nothing of the LAN, the Mac or the VPN
    assert r.refused == {"address": 5}


def test_a_route_that_cannot_be_read_refuses_the_destination_with_the_network_reason_inside_a_render():
    c = FakeChrome()
    out = asks(c, "reach-poc.vercel.app:443")
    mac = Mac(routes={PUBLIC: RuntimeError("route: writing to routing socket: not in table")})
    r = render_link("https://reach-poc.vercel.app/", chrome="/fake", launcher=c.launcher, resolver=_public,
                    connector=_no_connect, networks=mac.networks())  # _no_connect: a connect would fail the test
    assert out["reach-poc.vercel.app:443"].endswith(b"refused: network\n") and r.refused == {"network": 1}


def test_the_networks_of_the_setting_are_refused_like_private_space_inside_a_render():
    c = FakeChrome()
    out = asks(c, "example.com:443", "93.184.216.34:80", "8.8.8.8:80")
    r = render_link("https://reach-poc.vercel.app/", chrome="/fake", launcher=c.launcher, resolver=_public,
                    connector=_no_connect, refused_networks=["93.184.216.0/24"], networks=Mac().networks())
    assert out["example.com:443"].startswith(b"HTTP/1.1 403") and out["93.184.216.34:80"].startswith(b"HTTP/1.1 403")
    assert r.refused.get("address", 0) >= 2


def test_a_link_into_a_listed_network_is_refused_before_chrome_starts():
    c = FakeChrome()
    with pytest.raises(RenderError) as e:
        render_link("http://93.184.216.34/", chrome="/fake", launcher=c.launcher, resolver=_public, connector=_no_connect,
                    refused_networks=["93.184.216.0/24"])
    assert e.value.reason == "not a public address" and c.launches == 0


def test_a_setting_that_is_not_a_network_fails_the_render_as_a_setup_problem_and_never_starts_chrome():
    c = FakeChrome()
    with pytest.raises(RenderError) as e:
        link(c, refused_networks=["not a network"])
    assert e.value.reason == "browser setup" and c.launches == 0


def test_the_default_policy_of_a_real_render_asks_this_macs_networks_and_lists_the_setting(monkeypatch):
    p = chrome_mod.default_policy(["203.0.113.7/32", " 198.51.100.0/24 ", "2001:db8::/32"])
    assert [str(n) for n in p.refused] == ["203.0.113.7/32", "198.51.100.0/24", "2001:db8::/32"]
    assert isinstance(p.local, LocalNetworks) and p.local._interfaces is localnet.system_interfaces
    assert p.local._route is localnet.system_route
    assert chrome_mod.default_policy().refused == () and chrome_mod.default_policy().local is not None
    seen = []
    monkeypatch.setattr(chrome_mod, "default_policy", lambda nets, networks=None: (seen.append(list(nets)), Policy())[1])
    link(FakeChrome(), refused_networks=["203.0.113.7/32"])
    assert seen == [["203.0.113.7/32"]]  # render_link builds its policy this way unless a test gives one


# ---------------------------------------------------------------- logs (security review M4)

def site_review_records(caplog):
    return [r for r in caplog.records if r.name.startswith("haseeb_agent.site_review")]


def test_a_render_logs_one_summary_line_with_counts_per_reason_and_the_main_host_and_no_other_host(caplog):
    """A page that asks for hundreds of hosts of its own choosing: the render logs ONE line, with the counts per reason code,
    the main document's host, and none of the hosts the page asked for."""
    caplog.set_level(logging.DEBUG)

    def navigate(c, p, s):
        res = _navigate(c, p, s)
        for i in range(120):
            connect_through(c, f"flood-{i}.attacker-chosen.example:8080")   # port
            connect_through(c, f"10.0.{i % 200}.{i % 250}:80")              # address
        connect_through(c, "localhost:80")                                  # name
        return res
    c = FakeChrome({"Page.navigate": navigate})
    r = link(c, "https://Reach-POC.vercel.app/some/path?q=secret")
    assert r.refused == {"port": 240, "address": 240, "name": 2}  # (the page is loaded at both sizes)
    (rec,) = site_review_records(caplog)
    assert rec.getMessage() == ("site review: render of reach-poc.vercel.app: 0 connection(s), 482 refused "
                                "(address x240, name x2, port x240), 0 broke")
    assert "flood-" not in caplog.text and "attacker-chosen" not in caplog.text and "secret" not in caplog.text
    assert "/some/path" not in caplog.text and "10.0." not in caplog.text


def test_a_render_that_fails_logs_its_summary_once_too(caplog):
    caplog.set_level(logging.DEBUG)

    def navigate(c, p, s):
        connect_through(c, "169.254.169.254:80")
        return _navigate(c, p, s, error="net::ERR_CONNECTION_REFUSED")
    with pytest.raises(RenderError):
        link(FakeChrome({"Page.navigate": navigate}))
    (rec,) = site_review_records(caplog)
    assert "render of reach-poc.vercel.app" in rec.getMessage() and "1 refused (address x1)" in rec.getMessage()


def test_an_attachments_render_names_no_host(caplog):
    caplog.set_level(logging.DEBUG)
    html(FakeChrome())
    (rec,) = site_review_records(caplog)
    assert rec.getMessage() == "site review: render of an attachment: 0 connection(s), 0 refused, 0 broke"


def test_the_main_hosts_label_is_clean(caplog):
    assert chrome_mod._host_label("Reach-POC.vercel.app") == "reach-poc.vercel.app"
    assert chrome_mod._host_label("a b\n[c]\x00.example/../x" + "y" * 200) == "abc.example..x" + "y" * 86
    assert len(chrome_mod._host_label("a" * 500)) == 100


def test_a_main_document_the_proxy_refused_is_reported_with_the_policys_words():
    def navigate(c, p, s):
        connect_through(c, "192.168.1.10:80")  # the redirect target the page was sent to: refused
        c.url = p["url"]
        c.emit("Network.requestWillBeSent", {"requestId": "r0", "type": "Document", "frameId": "F1"})
        c.emit("Network.loadingFailed", {"requestId": "r0", "errorText": "net::ERR_TUNNEL_CONNECTION_FAILED"})
        c.emit("Page.frameNavigated", {"frame": {"id": "F1", "url": "chrome-error://chromewebdata/",
                                                 "unreachableUrl": p["url"]}})
        c.emit("Page.loadEventFired", {})
        return {"frameId": "F1", "loaderId": "L"}
    c = FakeChrome({"Page.navigate": navigate})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "not a public address" and "Page.captureScreenshot" not in c.methods()


# ---------------------------------------------------------------- an HTML attachment

def test_an_html_attachment_is_rendered_from_a_file_that_holds_it_alone_with_the_network_off():
    seen = {}

    def navigate(c, p, s):
        res = _navigate(c, p, s)
        seen["proxy"] = [connect_through(c, t) for t in ("example.com:443", "93.184.216.34:80", "127.0.0.1:80")]
        return res

    c = FakeChrome({"Page.navigate": navigate})
    page = "<!DOCTYPE html>\n<html><head><title>Reach</title></head><body><h1>Account executive</h1></body></html>"
    r = html(c, page)
    assert r.offline is True and r.desktop.size == "desktop"
    (url,) = {p["url"] for p in c.params("Page.navigate")}
    assert url.startswith("file:///") and url.endswith("/page.html")
    assert c.dir_at_navigate == ["page.html"]  # the file is alone in its folder
    assert all(v.startswith(b"HTTP/1.1 403") for v in seen["proxy"])  # the proxy refuses everything: it is offline
    assert r.refused == {"offline": 6}  # three requests at each of the two sizes
    assert not Path(url[len("file://"):]).parent.exists()  # and the folder is gone afterwards
    assert f"--proxy-server=http://127.0.0.1:{c.proxy_port}" in c.argv


def test_the_attachment_is_never_served_from_a_port():
    c = FakeChrome()
    html(c)
    assert all(p["url"].startswith("file://") for p in c.params("Page.navigate"))
    assert not any(a.startswith("--remote-debugging-port") for a in c.argv)


def test_the_attachment_gets_a_csp_that_lets_it_load_nothing_from_file(tmp_path):
    out = csp_page("<!DOCTYPE html>\n<html><head></head><body>x</body></html>")
    assert out.startswith("<!DOCTYPE html><meta http-equiv=\"Content-Security-Policy\"")
    assert "x</body></html>" in out and out.count("Content-Security-Policy") == 1
    csp = out.split('content="', 1)[1].split('"', 1)[0]
    assert "file:" not in csp and "frame-src data: blob: https: http:" in csp and "object-src 'none'" in csp
    assert "form-action 'none'" in csp and "base-uri 'none'" in csp
    assert csp_page("<h1>no doctype</h1>").startswith('<meta http-equiv="Content-Security-Policy"')
    assert csp_page("﻿<!doctype html><p>bom").startswith("<!doctype html><meta")
    assert csp_page("  \n<!DocType HTML PUBLIC \"-//W3C//DTD HTML 4.01//EN\">\n<p>x").count("<meta") == 1


def test_a_page_that_navigates_away_from_its_file_is_discarded():
    def navigate(c, p, s):
        res = _navigate(c, p, s)
        c.emit("Page.frameNavigated", {"frame": {"id": "F1", "url": "file:///etc/passwd"}})
        return res
    c = FakeChrome({"Page.navigate": navigate})
    with pytest.raises(RenderError) as e:
        html(c)
    assert e.value.reason == "the page navigated away" and "Page.captureScreenshot" not in c.methods()


def test_an_attachment_that_cannot_load_is_a_short_reason():
    c = FakeChrome({"Page.navigate": lambda c, p, s: _navigate(c, p, s, error="net::ERR_FILE_NOT_FOUND")})
    with pytest.raises(RenderError):
        html(c)


# ---------------------------------------------------------------- the protocol client

# ---------------------------------------------------------------- the unexpected (security review M1)

def test_a_lone_surrogate_in_an_attachment_is_replaced_and_does_not_fail_the_render():
    from urllib.parse import unquote
    seen = {}

    def navigate(c, p, s):
        seen["bytes"] = Path(unquote(p["url"][len("file://"):])).read_bytes()
        return _navigate(c, p, s)
    r = html(FakeChrome({"Page.navigate": navigate}), "<!doctype html><title>x</title><h1>a \ud800 b \U0001f600</h1>")
    assert r.offline is True and b"a ? b " in seen["bytes"] and "\U0001f600".encode() in seen["bytes"]


def test_the_attachments_file_is_written_inside_the_wrapped_render_so_a_failed_write_is_a_chrome_failure(monkeypatch):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-page-*")))

    def full(self, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(Path, "write_bytes", full)
    c = FakeChrome()
    with pytest.raises(RenderError) as e:
        html(c)
    assert e.value.reason == "Chrome failed" and c.launches == 0
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-page-*"))) == before  # and nothing is left


@pytest.mark.parametrize("method,result", [
    ("Target.createTarget", {}),                        # no targetId: KeyError
    ("Target.attachToTarget", {}),                      # no sessionId: KeyError
    ("Page.getFrameTree", {"frameTree": {}}),           # no frame: KeyError
    ("Page.captureScreenshot", {"data": 7}),            # not base64 text: TypeError
])
def test_an_answer_of_chrome_that_is_not_what_was_expected_is_a_chrome_failure_not_an_unhandled_error(method, result):
    c = FakeChrome({method: lambda c, p, s: result})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "Chrome failed" and e.value.detail in ("protocol", "screenshot", "cdp")
    assert not os.path.exists(c.profile) and c.killed == 1  # and everything is still cleaned up


def test_a_measured_value_that_is_not_a_number_counts_as_zero_and_does_not_fail_the_render():
    c = FakeChrome(info={"scrollWidth": "NaN-ish", "clientWidth": None})
    r = link(c)
    assert (r.desktop.scroll_width, r.desktop.client_width) == (0, 0)
    c = FakeChrome(info={"scrollWidth": float("inf"), "clientWidth": 390.7})
    r = link(c)
    assert (r.desktop.scroll_width, r.desktop.client_width) == (0, 390)


def test_the_pipe_client_matches_replies_to_commands_and_keeps_events_in_order():
    r4, w4 = os.pipe()
    r3, w3 = os.pipe()
    pipe = CdpPipe(r4, w3, Deadline(5))
    try:
        os.write(w4, json.dumps({"method": "A.first", "params": {"n": 1}, "sessionId": "s"}).encode() + b"\0")
        os.write(w4, json.dumps({"id": 99, "result": {"stale": True}}).encode() + b"\0")  # a reply to nothing we sent
        os.write(w4, b"not json\0")
        os.write(w4, json.dumps({"id": 1, "result": {"ok": 1}}).encode() + b"\0")
        os.write(w4, json.dumps({"method": "A.second", "params": {}}).encode() + b"\0")
        assert pipe.call("X.y", {"a": 1}, session="s") == {"ok": 1}
        sent = json.loads(os.read(r3, 4096).rstrip(b"\0"))
        assert sent == {"id": 1, "method": "X.y", "params": {"a": 1}, "sessionId": "s"}
        pipe.pump(0.05)
        assert [(e.method, e.session) for e in pipe.take()] == [("A.first", "s"), ("A.second", None)]
        assert pipe.take() == []
    finally:
        for fd in (r4, w4, r3, w3):
            os.close(fd)


def test_the_pipe_client_raises_on_error_timeout_and_a_closed_pipe():
    r4, w4 = os.pipe()
    r3, w3 = os.pipe()
    pipe = CdpPipe(r4, w3, Deadline(5))
    try:
        os.write(w4, json.dumps({"id": 1, "error": {"code": -32601, "message": "no such method"}}).encode() + b"\0")
        with pytest.raises(chrome_mod.CdpError) as e:
            pipe.call("No.such")
        assert e.value.code == -32601 and "no such method" not in str(e.value)
        with pytest.raises(CdpTimeout):
            pipe.call("Slow.one", timeout=0.1)
        os.close(w4)
        with pytest.raises(CdpClosed):
            pipe.call("After.close", timeout=1)
    finally:
        for fd in (r4, r3, w3):
            try:
                os.close(fd)
            except OSError:
                pass


def test_the_deadline_bounds_every_wait():
    r4, w4 = os.pipe()
    r3, w3 = os.pipe()
    pipe = CdpPipe(r4, w3, Deadline(0.2))
    try:
        t0 = time.time()
        with pytest.raises(CdpTimeout):
            pipe.call("Never.answered", timeout=30)
        assert time.time() - t0 < 2
    finally:
        for fd in (r4, w4, r3, w3):
            os.close(fd)


def test_viewports_stay_within_the_image_limit():
    for vp in (DESKTOP, PHONE):
        assert max(vp.width, vp.height) * vp.scale <= 1600


# ---------------------------------------------------------------- per-context settings (a setting made on the browser as a
# whole reaches only its default context, and the pages are rendered in contexts of their own)

def _index(c: FakeChrome, method: str, **match) -> list[int]:
    return [i for i, (m, p, _s) in enumerate(c.calls) if m == method and all(p.get(k) == v for k, v in match.items())]


def test_every_context_is_denied_downloads_with_its_own_id_before_anything_is_opened_in_it():
    c = FakeChrome()
    link(c)
    first_context = _index(c, "Target.createBrowserContext")[0]
    default_deny = [i for i, (m, p, _s) in enumerate(c.calls) if m == "Browser.setDownloadBehavior" and "browserContextId" not in p]
    assert default_deny and default_deny[0] < first_context  # the default context is denied before any other work
    for ctx in ("ctx1", "ctx2"):
        deny = _index(c, "Browser.setDownloadBehavior", browserContextId=ctx)
        assert len(deny) == 1 and c.calls[deny[0]][1] == {"behavior": "deny", "browserContextId": ctx}
        make = _index(c, "Target.createTarget", browserContextId=ctx)
        assert len(make) == 1 and deny[0] < make[0]  # denied before a tab exists in it...
    navs = _index(c, "Page.navigate")
    assert len(navs) == 2 and _index(c, "Browser.setDownloadBehavior", browserContextId="ctx1")[0] < navs[0]  # ...and so before any page
    assert _index(c, "Browser.setDownloadBehavior", browserContextId="ctx2")[0] < navs[1]
    created = _index(c, "Target.createBrowserContext")
    for ctx, made in zip(("ctx1", "ctx2"), created):  # right after the context is made: nothing else comes between
        assert c.calls[made + 1][0] == "Browser.setDownloadBehavior" and c.calls[made + 1][1]["browserContextId"] == ctx


def test_a_download_denial_that_fails_for_a_context_aborts_the_render_before_any_page():
    def refuse(c, p, s):
        if "browserContextId" in p:
            raise Fail()
        return {}
    c = FakeChrome({"Browser.setDownloadBehavior": refuse})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "browser setup" and e.value.detail == "download deny"
    assert "Target.createTarget" not in c.methods() and "Page.navigate" not in c.methods()
    assert c.methods().count("Target.disposeBrowserContext") == 1 and c.killed == 1  # the context is not left behind


def test_a_download_denial_that_fails_for_the_default_context_aborts_the_render_too():
    def refuse(c, p, s):
        if "browserContextId" not in p:
            raise Fail()
        return {}
    c = FakeChrome({"Browser.setDownloadBehavior": refuse})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "browser setup"
    assert "Target.createBrowserContext" not in c.methods() and "Page.navigate" not in c.methods()


def test_a_download_denial_that_never_answers_is_a_timeout_not_a_page():
    c = FakeChrome({"Browser.setDownloadBehavior": lambda c, p, s: None})
    with pytest.raises(RenderError) as e:
        link(c, budget_s=1.0)
    assert e.value.reason in ("timed out", "browser setup") and "Page.navigate" not in c.methods()


def test_every_permission_a_page_could_ask_for_is_denied_on_the_context_in_use_before_any_page():
    c = FakeChrome()
    link(c)
    for ctx in ("ctx1", "ctx2"):
        names = [p["permission"]["name"] for p in c.params("Browser.setPermission") if p["browserContextId"] == ctx]
        assert {"geolocation", "notifications", "camera", "microphone", "midi", "clipboard-read", "clipboard-write",
                "persistent-storage", "push"} <= set(names) and set(names) == set(chrome_mod.DENIED_PERMISSIONS)
        assert all(p["setting"] == "denied" for p in c.params("Browser.setPermission") if p["browserContextId"] == ctx)
        first_tab = _index(c, "Target.createTarget", browserContextId=ctx)[0]
        assert max(_index(c, "Browser.setPermission", browserContextId=ctx)) < first_tab
    assert not [p for p in c.params("Browser.setPermission") if "browserContextId" not in p]  # never on the browser as a whole
    assert not [m for m in c.methods() if m in ("Browser.grantPermissions", "Emulation.setGeolocationOverride")]


def test_a_permission_name_this_chrome_does_not_know_does_not_fail_the_render():
    def unknown(c, p, s):
        if p["permission"]["name"] in ("local-fonts", "payment-handler"):
            raise Fail(-32602)
        return {}
    r = link(FakeChrome({"Browser.setPermission": unknown}))
    assert r.desktop.title == "Reach"


def test_each_context_gets_the_proxy_too_and_starts_without_a_cookie():
    c = FakeChrome()
    link(c)
    for params in c.params("Target.createBrowserContext"):
        assert params["proxyServer"] == f"http://127.0.0.1:{c.proxy_port}" and params["proxyBypassList"] == "<-loopback>"
    assert [p["browserContextId"] for p in c.params("Storage.getCookies")] == ["ctx1", "ctx2"]
    assert not [m for m in c.methods() if m in ("Network.setCookie", "Network.setCookies", "Storage.setCookies")]


def test_a_context_that_starts_with_a_cookie_is_refused():
    c = FakeChrome({"Storage.getCookies": lambda c, p, s: {"cookies": [{"name": "sid", "value": "x", "domain": "example.com"}]}})
    with pytest.raises(RenderError) as e:
        link(c)
    assert e.value.reason == "browser setup" and "Page.navigate" not in c.methods()


def test_a_cookie_check_that_fails_is_a_failed_setup():
    def no(c, p, s):
        raise Fail()
    with pytest.raises(RenderError) as e:
        link(FakeChrome({"Storage.getCookies": no}))
    assert e.value.reason == "browser setup"


def test_the_backstop_download_folder_is_inside_the_throwaway_profile_and_made_before_chrome_starts():
    argv = chrome_argv("/fake/chrome", "/tmp/profile-x", 54321)
    assert "--download-default-directory=/tmp/profile-x/downloads" in argv
    c = FakeChrome()
    link(c)
    assert c.downloads == os.path.join(c.profile, "downloads") and c.downloads_existed
    assert not os.path.exists(c.downloads)  # gone with the profile


def test_a_file_that_got_through_anyway_is_counted_in_a_warning_and_discarded_with_the_profile(caplog):
    def navigate(c, p, s):
        res = _navigate(c, p, s)
        Path(c.downloads, "slipped.bin").write_bytes(b"x")
        os.makedirs(os.path.join(c.downloads, "sub"), exist_ok=True)
        Path(c.downloads, "sub", "also.bin").write_bytes(b"y")
        return res
    c = FakeChrome({"Page.navigate": navigate})
    r = link(c)
    assert r.downloads == 2 and not os.path.exists(c.profile)
    assert "2 downloaded file(s) were found in the throwaway profile and discarded" in caplog.text


def test_a_render_that_downloaded_nothing_says_so_and_does_not_warn(caplog):
    r = link(FakeChrome())
    assert r.downloads == 0 and "downloaded file" not in caplog.text
