"""The ONE real-Chrome test of the site review.

It renders a local test page (a server on 127.0.0.1) through the real filtering proxy, with a real headless Chrome, and
checks what a real Chrome does: the screenshots are real JPEGs of the right size, the page's scripts ran, the phone page's
overflow is measured, and, the point of the test, that Chrome sent EVERYTHING through the proxy, loopback included
(`--proxy-bypass-list=<-loopback>`): a page that asks for a canary server on another loopback port, for a documentation
address, for the cloud metadata address and for a name that rebinds to loopback is refused every time, and the canary never
sees a connection. It also checks the settings of the browser contexts the pages are rendered in, in a real Chrome: every permission a page
could ask for reads "denied" from inside the page, each size starts with no cookie and no stored value, the download denial
reaches each context under its own id before a tab exists in it, and a page that tries to download a file (a frame and a
link) gets none (a control run with downloads allowed into a folder of the test's own shows the page does download). An HTML
attachment is then rendered offline from a temp file: a `file:` image cannot load into it and a page that navigates to another
local file is discarded.

Everything it touches is on this Mac, and nothing in it can reach anything else (security review M8): the policy it is given
allows exactly the test server's address and port (an injected policy, for this test only); the resolver RAISES for every name
it does not know, so a name that Chrome asks for on its own (an update or a safe-browsing check, say) is refused as unresolved;
the connector dials ONLY the test server's (ip, port) and records anything else it is asked to dial (the test asserts there is
nothing), so even a destination that passed the policy could not leave this Mac; this Mac's networks are a fake. Only the guard
of the Chrome launch is lifted; the proxy's own outbound connect keeps its guard (it is never used here). It is skipped when
Chrome is missing."""
from __future__ import annotations

import asyncio
import glob
import http.server
import os
import socket
import struct
import tempfile
import threading

import pytest

from haseeb_agent.site_review import chrome as chrome_mod
from haseeb_agent.site_review.cdp import CdpPipe
from haseeb_agent.site_review.chrome import RenderError, render_html, render_link
from haseeb_agent.site_review.localnet import LocalNetworks
from haseeb_agent.site_review.policy import Policy
from tests.test_dashboard_frontend import CHROME, needs_chrome


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) of a JPEG, read from its start-of-frame marker."""
    assert data[:2] == b"\xff\xd8"
    i = 2
    while i < len(data):
        assert data[i] == 0xFF
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise AssertionError("no start of frame")


class Canary:
    """A TCP server on 127.0.0.1 that counts the connections it gets (and answers nothing)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.count = 0
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.count += 1
            conn.close()

    def close(self):
        self._stop = True
        self.sock.close()


def page_html(canary_port: int) -> bytes:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Reach test page</title>
<style>body{{font-family:sans-serif;margin:0}} .wide{{width:807px;height:40px;background:#ccf}}</style></head>
<body><h1>Account executive view</h1><div class="wide">a wide block</div><p id="js">js-not-run</p>
<p id="fresh">fresh:?</p><p id="perm">perm:?</p>
<iframe src="/dl.bin" style="display:none"></iframe>
<img src="http://127.0.0.1:{canary_port}/pixel.png" alt="">
<img src="http://192.0.2.1/x.png" alt=""><img src="http://169.254.169.254/latest/meta-data/x.png" alt="">
<img src="http://rebind.test/x.png" alt="">
<script>
document.getElementById('js').textContent = 'js-ran';
// nothing of an earlier load (the other size) or of anyone's profile is here: no cookie, no stored value
var fresh = document.cookie === '' && localStorage.getItem('seen') === null;
localStorage.setItem('seen', '1'); document.cookie = 'seen=1';
document.getElementById('fresh').textContent = 'fresh:' + fresh;
// the permissions the page could ask for are already denied on the context it is rendered in
Promise.all(['geolocation', 'notifications', 'camera', 'microphone'].map(n =>
  navigator.permissions.query({{name: n}}).then(s => n + '=' + s.state, () => n + '=unknown'))).then(r => {{
    document.getElementById('perm').textContent = 'perm:' + r.join(',');
}});
var a = document.createElement('a'); a.href = '/dl.bin'; a.download = 'haseeb-site-review-test-download.bin';
document.body.appendChild(a); a.click();   // and a page that tries to put a file on this Mac
fetch('http://127.0.0.1:{canary_port}/api/search').catch(() => {{}});
fetch('http://192.168.1.10/admin').catch(() => {{}});
</script></body></html>""".encode()


class Site(http.server.ThreadingHTTPServer):
    daemon_threads = True
    paths: list[str]


def serve(body: bytes) -> Site:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.paths.append(self.path)
            if self.path == "/dl.bin":  # a file the browser is told to save
                data = b"not for this disk\n" * 64
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="haseeb-site-review-test-download.bin"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            ok = self.path == "/"
            data = body if ok else b"nope"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    site = Site(("127.0.0.1", 0), Handler)
    site.paths = []
    threading.Thread(target=site.serve_forever, daemon=True).start()
    return site


async def rebinding(host, port):
    """The only name that exists is the rebinding one (it answers loopback). Any other name raises, so it is unresolved: the
    test cannot be handed a public address to connect to."""
    if host == "rebind.test":
        return ["127.0.0.1"]
    raise OSError(f"hermetic test: no such host {host!r}")


class OnlyTheSite:
    """The proxy's outbound connect for this test: it dials the test server's address and nothing else. Anything else it is
    asked for is recorded in `strays` and refused with an error, so a destination that somehow passed the policy still could
    not leave this Mac."""

    def __init__(self, ip: str, port: int):
        self.ip, self.port = ip, port
        self.dialled: list[tuple[str, int]] = []
        self.strays: list[tuple[str, int]] = []

    async def __call__(self, ip: str, port: int):
        if (ip, port) != (self.ip, self.port):
            self.strays.append((ip, port))
            raise OSError("hermetic test: this connector dials only the test server")
        self.dialled.append((ip, port))
        return await asyncio.open_connection(ip, port)


def this_mac() -> LocalNetworks:
    """A Mac with one interface and one route, injected: the real `ifconfig` and `route` are never run by this test."""
    return LocalNetworks(lambda: ["192.168.1.23/24"], lambda dest: "en0")


@needs_chrome
def test_a_real_chrome_renders_a_local_page_through_the_proxy_and_everything_else_is_refused(monkeypatch, tmp_path):
    # the one guard that is lifted: the Chrome launch. The proxy's outbound connect keeps its guard (this test's connector
    # replaces it), and so do the resolver and this Mac's lookups (all replaced by fakes below).
    monkeypatch.setattr(chrome_mod, "guard", lambda what: None)
    # (the age of this Mac's Chrome is tested on its own: this test is about the leash, whatever Chrome is installed)
    monkeypatch.setattr(chrome_mod, "ensure_chrome_fresh", lambda chrome, **kw: None)
    temp_before = set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*")))
    canary = Canary()
    site = serve(page_html(canary.port))
    port = site.server_address[1]
    try:
        # ---- a link: the page is rendered, and Chrome had no road but the proxy
        policy = Policy(allow=frozenset({("127.0.0.1", port)}), local=this_mac())
        connector = OnlyTheSite("127.0.0.1", port)
        calls: list[tuple[str, dict, dict]] = []
        real_call = CdpPipe.call

        def spy(self, method, params=None, **kw):
            res = real_call(self, method, params, **kw)
            calls.append((method, params or {}, res))  # recorded once Chrome has accepted it
            return res
        with monkeypatch.context() as m:
            m.setattr(CdpPipe, "call", spy)
            r = render_link(f"http://127.0.0.1:{port}/", chrome=CHROME, policy=policy, resolver=rebinding,
                            connector=connector, budget_s=60)
        assert r.offline is False
        assert site.paths[0] == "/" and r.connections >= 1  # the main page came through the proxy: loopback is not bypassed
        d, p = r.desktop, r.phone
        assert jpeg_size(d.jpeg) == (1440, 900) and d.width_px == 1440 and d.height_px == 900
        pw, ph = jpeg_size(p.jpeg)
        assert max(pw, ph) <= 1600 and (pw, ph) == (p.width_px, p.height_px) and pw < ph  # a tall, narrow phone image
        for shot in (d, p):
            assert shot.title == "Reach test page" and "Account executive view" in shot.text
            assert "js-ran" in shot.text and "js-not-run" not in shot.text  # the scripts ran
            assert shot.viewport_meta is True and shot.status == 200
        assert d.client_width == 1440 and d.scroll_width <= 1440
        for shot in (d, p):  # each size started clean: the second load did not see the first one's cookie or stored value
            assert "fresh:true" in shot.text, shot.text
            # every permission was already denied on the context in use (a denial on the browser as a whole is not enough)
            assert "perm:geolocation=denied,notifications=denied,camera=denied,microphone=denied" in shot.text, shot.text
        # ---- the settings of the browser contexts, in a real Chrome (the fake-Chrome tests check the order of the calls
        # closely; this checks that a real Chrome takes every one): the download denial went to the default context and to
        # each context in use, with its own id, and a tab was opened in a context only after its denial
        order = [(meth, par.get("browserContextId")) for meth, par, _res in calls]
        made = [res["browserContextId"] for meth, _par, res in calls if meth == "Target.createBrowserContext"]
        assert len(made) == 2 and len(set(made)) == 2
        assert order.index(("Browser.setDownloadBehavior", None)) < order.index(("Target.createBrowserContext", None))
        for ctx in made:
            deny = order.index(("Browser.setDownloadBehavior", ctx))
            assert calls[deny][1] == {"behavior": "deny", "browserContextId": ctx}
            assert deny < order.index(("Target.createTarget", ctx))
            assert ("Storage.getCookies", ctx) in order and ("Browser.setPermission", ctx) in order
        # ---- the page tried to download a file (a frame and a link): asked for, refused, and nothing is on this disk
        assert site.paths.count("/dl.bin") >= 1
        assert r.downloads == 0
        assert p.client_width == 390 and p.scroll_width == 807  # the phone page is wider than the phone: found
        # ---- the page asked for this Mac's services and the LAN: refused, and never connected to
        assert canary.count == 0, "Chrome reached a loopback port without the proxy"
        assert r.refused.get("port", 0) >= 1      # 127.0.0.1:<canary port>, 192.168.1.10 is port 80 so it is the address rule
        assert r.refused.get("address", 0) >= 3   # 192.0.2.1, 169.254.169.254, 192.168.1.10, and rebind.test -> loopback
        assert d.log_errors + sum(d.failed.values()) >= 1  # and the page's own console saw the failures
        assert all(len(k) <= 40 for k in d.failed)
        # ---- the control for "nothing was downloaded": the same page, with downloads ALLOWED into a folder of the test's own,
        # does put a file there. So the page really tries, and the empty folder above is the denial's doing.
        allowed = tmp_path / "allowed"
        allowed.mkdir()

        def allow(pipe, context):
            if context is not None:
                pipe.call("Browser.setDownloadBehavior", {"behavior": "allow", "browserContextId": context,
                                                          "downloadPath": str(allowed)}, timeout=5)
        with monkeypatch.context() as m:
            m.setattr(chrome_mod, "_deny_downloads", allow)
            ctl = render_link(f"http://127.0.0.1:{port}/", chrome=CHROME, policy=policy, resolver=rebinding,
                              connector=connector, budget_s=60)
        assert [f.stat().st_size for f in allowed.iterdir()] and ctl.downloads == 0
        # ---- an HTML attachment: offline, from a temp file, and it can load nothing from file:
        local_png = tmp_path / "secret.png"
        local_png.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"))
        attach = f"""<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>Demo</title></head>
<body><h1>Client view</h1><p id="r">pending</p>
<img src="{local_png.as_uri()}" onload="document.getElementById('r').textContent='img:loaded'"
 onerror="document.getElementById('r').textContent='img:blocked'">
<iframe src="file:///etc/hosts"></iframe>
<script>fetch('http://example.com/x').catch(() => {{}});</script></body></html>"""
        a = render_html(attach, chrome=CHROME, budget_s=60, resolver=rebinding, connector=connector)
        assert a.offline is True and a.connections == 0  # nothing was connected to, ever
        assert "Client view" in a.desktop.text and "img:blocked" in a.desktop.text  # the local file could not load in
        assert a.refused.get("offline", 0) >= 1 and a.desktop.log_errors >= 1  # the fetch was refused; the frame was blocked
        assert canary.count == 0
        # ---- an attachment that sends the browser to another local file is thrown away
        nav = "<!doctype html><title>x</title><h1>hi</h1><script>setTimeout(() => { location.href = 'file:///etc/hosts'; }, 100)</script>"
        with pytest.raises(RenderError) as e:
            render_html(nav, chrome=CHROME, budget_s=60, resolver=rebinding, connector=connector)
        assert e.value.reason == "the page navigated away"
        # ---- hermetic: the only road the proxy ever used was the test server's, and it was asked for nothing else
        assert connector.strays == [] and connector.dialled and set(connector.dialled) == {("127.0.0.1", port)}
    finally:
        site.shutdown()
        site.server_close()
        canary.close()
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "haseeb-site-review-*"))) == temp_before  # all cleaned up
