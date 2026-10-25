"""Task 8: the dashboard frontend (shell, themes, Live and Inbox).

Static checks read the files under dashboard/static. The render checks start a real uvicorn server on 127.0.0.1
(port picked by the OS) against a tmp_path store, log in through a freshly minted nonce exactly as
`dashboard open` does, and let headless Chrome (settings.chrome_path, like the deck tests) run the page and dump
its DOM. They are skipped when Chrome is missing. Offline guard on; tmp data only.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pytest
import uvicorn

from haseeb_agent.config import load_settings
from haseeb_agent.dashboard.app import STATIC_DIR, create_app, serve_config
from haseeb_agent.dashboard.security import mint_nonce
from haseeb_agent.models import Item, StoredMessage
from haseeb_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
KEY = "cd" * 32
SVG_NS = "http://www.w3.org/2000/svg"  # an XML namespace name inside SVG markup, never fetched
HOSTILE_IMG = "<img src=x onerror=alert(1)>"
HOSTILE_SCRIPT = "</script><script>alert(2)</script>"
RLO, LRI, PDI = "\u202e", "\u2066", "\u2069"  # bidi controls: right-to-left override, left-to-right isolate, pop
HOSTILE_BIDI = f"{RLO}gnp.exe{LRI} javascript:alert(3)"

JS = sorted(STATIC_DIR.glob("*.js"))
TEXT_FILES = [p for p in STATIC_DIR.rglob("*") if p.is_file() and p.suffix in (".js", ".css", ".html")]


def _settings(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return replace(load_settings(ROOT / "tests" / "fixtures" / "config.toml", env), db_path=str(tmp_path / "agent.sqlite"))


def _chrome() -> str:
    """settings.chrome_path, as the deck job uses it (a test .env, never the real one)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        return _settings(Path(d)).chrome_path


CHROME = _chrome()
needs_chrome = pytest.mark.skipif(not Path(CHROME).exists(), reason="Google Chrome is not installed")


# ---------------------------------------------------------------- static checks

def test_static_files_present():
    for name in ("index.html", "app.css", "app.js", "dom.js", "client.js", "live.js", "inbox.js", "actions.js",
                 "theme.js", "history.js", "decks.js", "logs.js", "score.js",
                 "jobs.js", "runs.js", "teamposts.js", "feedbackbtn.js"):
        assert (STATIC_DIR / name).is_file(), name


def _code(src: str) -> str:
    """JS without its comments, so a comment may name what the code never does."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"(^|[^:])//.*$", r"\1", line) for line in src.splitlines())


def _function(code: str, name: str) -> str:
    m = re.search(r"(?:async )?function " + name + r"\(.*?\n\}", code, re.S)
    assert m, name
    return m.group(0)


def test_no_innerhtml_with_data():
    """No HTML-parsing sink at all outside comments: data only ever reaches the DOM as text."""
    for p in JS:
        code = _code(p.read_text())
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "setHTMLUnsafe", "parseHTMLUnsafe",
                     "document.write", "DOMParser", "createContextualFragment"):
            assert sink not in code, f"{p.name}: {sink}"
        assert not re.search(r"\[\s*['\"`]\w*HTML\w*['\"`]\s*\]", code), p.name  # el["innerHTML"] and friends
        assert not re.search(r"\beval\s*\(|new Function\b|setTimeout\(\s*['\"]|setInterval\(\s*['\"]", code), p.name


def test_no_external_urls():
    """Nothing loads from another origin. The only http(s) text allowed is the SVG XML namespace name."""
    for p in TEXT_FILES:
        src = p.read_text().replace(SVG_NS, "")
        assert not re.search(r"https?://", src), p.name
        assert not re.search(r"(src|href)\s*=\s*['\"]//", src), p.name  # protocol-relative
        assert "@import" not in src, p.name
        assert not re.search(r"url\(\s*(?!['\"]?data:)", src), p.name  # CSS urls: data: only


def test_index_has_no_inline_script_or_style():
    html = (STATIC_DIR / "index.html").read_text()
    assert '<link rel="icon" href="data:,">' in html  # no /favicon.ico request
    assert "<style" not in html.lower()
    assert not re.search(r"\sstyle\s*=", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)  # no inline handlers
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.I | re.S):
        assert 'src="/' in m.group(1), m.group(0)
        assert m.group(2).strip() == "", "inline script body"
    assert 'src="/app.js"' in html and 'type="module"' in html
    assert 'href="/app.css"' in html


def test_no_inline_style_in_js_markup():
    for p in JS:
        src = p.read_text()
        assert "setAttribute(\"style\"" not in src and "setAttribute('style'" not in src, p.name
        assert ".style.cssText" not in src, p.name


def test_reduced_motion_rule_present():
    css = (STATIC_DIR / "app.css").read_text()
    m = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}", css, re.S)
    assert m, "no reduced-motion block"
    block = m.group(1)
    assert "animation: none !important" in block and "transition: none !important" in block
    # the theme cross-fade (it uses !important too) exists only for people who don't ask for less motion
    anim = re.search(r"@media\s*\(prefers-reduced-motion:\s*no-preference\)\s*\{(.*?)\n\}", css, re.S)
    assert anim and ".theme-anim" in anim.group(1)
    assert ".theme-anim" not in css.replace(anim.group(0), "")


def _token_block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert m, selector
    return m.group(1)


def test_theme_tokens_both():
    css = (STATIC_DIR / "app.css").read_text()
    mid = _token_block(css, ':root[data-theme="midnight"]')
    day = _token_block(css, ':root[data-theme="daylight"]')
    names = lambda b: set(re.findall(r"(--[a-z0-9-]+)\s*:", b))  # noqa: E731
    assert names(mid) == names(day) and len(names(mid)) >= 20  # every token defined in both themes
    for t in ("--bg", "--panel", "--text", "--muted", "--a1", "--a2", "--ok", "--warn", "--bad"):
        assert t in names(mid)
    theme_js = (STATIC_DIR / "theme.js").read_text()
    assert "prefers-color-scheme" in theme_js and "localStorage" in theme_js and "try" in theme_js


def _rgb(v: str):
    v = v.strip()
    if v.startswith("#"):
        return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)), 1.0
    m = re.fullmatch(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", v)
    assert m, v
    return tuple(int(m.group(i)) for i in (1, 2, 3)), float(m.group(4))


def _contrast(fg, bg) -> float:
    def lum(c):
        ch = [x / 255 for x in c]
        ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    a, b = lum(fg), lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_daylight_text_contrast():
    """Every Daylight colour used for text reaches 4.5:1 (WCAG AA) on the card, and on its own soft tint."""
    css = (STATIC_DIR / "app.css").read_text()
    day = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", _token_block(css, ':root[data-theme="daylight"]')))
    sunken, _ = _rgb(day["--sunken"])
    for name, tint in (("--warn", "--warn-soft"), ("--ok", "--ok-soft"), ("--bad", "--bad-soft"),
                       ("--a2-text", None), ("--pink-text", None), ("--muted", None), ("--a1", None)):
        fg, _ = _rgb(day[name])
        assert _contrast(fg, sunken) >= 4.5, name
        if tint:
            rgb, a = _rgb(day[tint])
            under = tuple(round(a * x + (1 - a) * y) for x, y in zip(rgb, sunken))
            assert _contrast(fg, under) >= 4.5, (name, tint)


def test_person_colors_meet_contrast_in_both_themes():
    """The 12 person colours (`color` 0–11 from /api/people) are avatar fills under white initials: each reaches 4.5:1
    in both themes, and each index has its class."""
    css = (STATIC_DIR / "app.css").read_text()
    for theme in ("midnight", "daylight"):
        block = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", _token_block(css, f':root[data-theme="{theme}"]')))
        for i in range(12):
            fill, _ = _rgb(block[f"--p{i}"])
            assert _contrast((255, 255, 255), fill) >= 4.5, (theme, i)
    for i in range(12):
        assert re.search(r"\.pc-%d\s*\{\s*--pc:\s*var\(--p%d\);\s*\}" % (i, i), css), i
    av = _token_block(css, ".av")
    assert "color: #fff" in av and "var(--pc)" in av


def test_system_font_stack_only():
    css = (STATIC_DIR / "app.css").read_text()
    assert "@font-face" not in css
    assert '-apple-system, "SF Pro Display", "SF Pro Text", Inter, system-ui, sans-serif' in css


def test_every_api_call_sends_the_session_header():
    """Only client.js fetches, with exactly two calls: the login exchange and authFetch, the one helper that every
    /api request (JSON, binary, the stream) goes through; it refuses non-/api paths and adds X-Session-Token."""
    for p in JS:
        if p.name != "client.js":
            assert not re.search(r"\bfetch\(", _code(p.read_text())), p.name
    code = _code((STATIC_DIR / "client.js").read_text())
    assert len(re.findall(r"\bfetch\(", code)) == 2
    assert 'window.fetch("/auth/exchange"' in code
    helper = _function(code, "authFetch")
    assert "window.fetch(path" in helper and '"X-Session-Token"' in helper and 'path.startsWith("/api/")' in helper
    for fn in ("api", "apiBlob", "startStream"):
        assert "authFetch(" in _function(code, fn), fn
    assert code.count('"X-Session-Token"') == 1  # nowhere else builds the header


# ---------------------------------------------------------------- a real server for Chrome

def _free_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    return s


@contextmanager
def live_server(tmp_path: Path, seed=None, *, logs: list[str] | None = None, wrong_token: bool = False,
                stream_polls: int = 2, gate: dict | None = None, patch: dict | None = None, sips=None,
                spawn=None, kick=None, started_at: float | None = None, clock=None):
    """uvicorn on 127.0.0.1:<free port> against a tmp store. Yields (base_url, settings, login_dir). `patch`
    overrides settings fields (a test's hostile workspace_url, say); `sips` is the thumbnail runner (by default one
    that fails, so a deck has no preview). `spawn` and `kick` receive what the run controls start (by default
    nothing runs: a spawn is dropped, so no test ever starts a real process). `started_at` is when the dashboard
    process started: by default an hour ago, so a Slack check a test seeds before the server starts is not "from
    before the dashboard". `clock` is the server's clock (seconds, like time.time) for a test that needs the server to
    think it is another moment, say just after midnight in Kuwait; by default the real one."""
    sock = _free_socket()
    port = sock.getsockname()[1]
    settings = replace(_settings(tmp_path), dashboard_port=port, **(patch or {}))
    store = Store(settings.db_path)
    try:
        if seed:
            seed(store, settings)
    finally:
        store.close()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    if logs:
        (logs_dir / "tick.log").write_text("\n".join(logs) + "\n")
    login_dir = tmp_path / "login"
    now = clock or time.time
    app = create_app(settings, lambda: Store(settings.db_path), KEY, kick or (lambda: None), login_dir=login_dir,
                     logs_dir=logs_dir, decks_dir=tmp_path / "decks", sips=sips or (lambda argv: 1),
                     spawn=spawn or (lambda args: None), started_at=now() - 3600 if started_at is None else started_at,
                     stream_poll_s=0.2, stream_max_polls=stream_polls,  # finite: --dump-dom's virtual time settles
                     **({"clock": clock} if clock else {}))
    if wrong_token:  # the session's token stops matching (as after a dashboard restart): every /api call is 403
        inner = app

        async def app(scope, receive, send):  # noqa: F811
            if scope["type"] == "http" and scope["path"].startswith("/api/"):
                scope = dict(scope, headers=[(k, b"0" * 64 if k == b"x-session-token" else v)
                                             for k, v in scope["headers"]])
            await inner(scope, receive, send)
    if gate is not None:  # a test flips paths to a status (e.g. {"/api/live": 500}) while the page runs
        gated = app

        async def app(scope, receive, send):  # noqa: F811
            status = gate.get(scope.get("path")) if scope["type"] == "http" else None
            if status:
                await send({"type": "http.response.start", "status": status,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "gated"}'})
                return
            await gated(scope, receive, send)
    config = serve_config(app, port)
    config.timeout_graceful_shutdown = 2  # an open stream never holds the test up for long
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "server did not start"
    try:
        yield f"http://127.0.0.1:{port}", settings, login_dir
    finally:
        server.should_exit = True
        t.join(10)
        sock.close()


def chrome_argv(tmp_path: Path, *extra: str) -> list[str]:
    profile = tmp_path / f"chrome-{time.monotonic_ns()}"
    return [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--disable-extensions", "--no-proxy-server", "--hide-scrollbars", "--mute-audio",
            f"--user-data-dir={profile}", *extra]


def login_url(base: str, login_dir: Path) -> str:
    return f"{base}/?login={mint_nonce(login_dir)}"


def render_dom(tmp_path: Path, base: str, login_dir: Path, budget_ms: int = 6000) -> str:
    r = subprocess.run(chrome_argv(tmp_path, f"--virtual-time-budget={budget_ms}", "--dump-dom",
                                   login_url(base, login_dir)),
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout


def section(dom: str, label: str) -> str:
    m = re.search(r'<section[^>]*aria-label="' + re.escape(label) + r'"[^>]*>(.*?)</section>', dom, re.S)
    assert m, f"no section {label!r}"
    return m.group(1)


def _item(iid, kind, title, body, day, dm_ts):
    now = datetime.now().astimezone()
    return Item(id=iid, kind=kind, day=day, status="delivered", body=body, title=title, dm_ts=dm_ts,
                source_ts=None, score=None, models={}, attempts=0, edited=False, created_at=now, acted_at=None)


def seed_hostile(store: Store, settings) -> None:
    today = datetime.now(settings.tz).date()
    store.upsert_item(_item("feedback-hostile", "feedback", f"{HOSTILE_IMG} {HOSTILE_BIDI}",
                            f"Nice post {HOSTILE_IMG} really {HOSTILE_BIDI}", today, "10.1"))
    store.upsert_item(_item("todo-hostile", "todo", "Next Todo", f"Next todo: ship {HOSTILE_SCRIPT} today", today, "10.2"))


def _bdi_texts(html: str) -> list[str]:
    return re.findall(r"<bdi>(.*?)</bdi>", html, re.S)


@needs_chrome
def test_render_hostile_text(tmp_path):
    line = f"{datetime.now().astimezone().isoformat()} tick {HOSTILE_IMG} {HOSTILE_BIDI}"
    with live_server(tmp_path, seed_hostile, logs=[line]) as (base, _s, login):
        dom = render_dom(tmp_path, base, login)
    inbox = section(dom, "Waiting for you")
    assert "&lt;img src=x onerror=alert(1)&gt;" in inbox  # shown literally: title and body
    assert "&lt;/script&gt;&lt;script&gt;alert(2)&lt;/script&gt;" in inbox
    assert "<img" not in inbox.lower() and "<script" not in inbox.lower()
    assert len(re.findall(r"<img\b", dom, re.I)) == 0  # not one img element anywhere on the page
    assert dom.lower().count("<script") == 2  # only theme.js and app.js: nothing was injected
    assert "javascript:alert(3)" in inbox and not re.search(r"<a\b", inbox)  # text, never a link
    assert not re.search(r"href=\"\s*javascript:", dom, re.I)
    # every bidi control sits inside a <bdi>, so it can't reorder the UI around the user's text
    text = re.sub(r"<[^>]+>", "", dom)  # the rendered text (attribute values such as aria-label left out)
    isolated = "".join(_bdi_texts(dom))
    assert text.count(RLO) == isolated.count(RLO) >= 3 and text.count(LRI) == isolated.count(LRI) >= 3
    log = section(dom, "Live log")
    assert "&lt;img src=x onerror=alert(1)&gt;" in log and RLO in "".join(_bdi_texts(log))
    assert "Post now" in inbox and "Shorter" in inbox and "Skip" in inbox


@needs_chrome
def test_empty_state_renders(tmp_path):
    with live_server(tmp_path) as (base, _s, login):
        dom = render_dom(tmp_path, base, login)
    assert "Nothing waiting for you" in section(dom, "Waiting for you")
    assert "Quiet right now" in section(dom, "Right now")
    assert "No activity yet today." in section(dom, "Today")
    assert "No log lines yet." in section(dom, "Live log")
    assert "Session expired" not in dom
    assert re.search(r'data-theme="(midnight|daylight)"', dom)
    assert 'aria-current="page"' in dom and 'data-route="live"' in dom


@needs_chrome
def test_rejected_token_shows_session_expired(tmp_path):
    with live_server(tmp_path, seed_hostile, wrong_token=True) as (base, _s, login):
        dom = render_dom(tmp_path, base, login, budget_ms=3000)
    assert "Session expired" in dom and "haseeb-agent dashboard open" in dom
    assert "Waiting for you" not in dom and "onerror" not in dom  # nothing from the API was shown


def _done(store, job, step, started, secs, ok=True, kind=None):
    eid = store.add_event(job, step, model=None, started=started, ended=None, ok=None, kind=None)
    store.finish_event(eid, started + timedelta(seconds=secs), ok, kind)


def seed_running(store: Store, settings) -> None:
    now = datetime.now(settings.tz)
    store.set_cursor("tick_seen", (now - timedelta(seconds=20)).isoformat())
    _done(store, "feedback", "deliver", now - timedelta(minutes=40), 2, ok=False, kind="SlackUnavailable")  # older run
    _done(store, "feedback", "read_channel", now - timedelta(seconds=90), 5, ok=False, kind="SlackUnavailable")
    _done(store, "feedback", "read_channel", now - timedelta(seconds=50), 8)  # the retry worked
    store.add_event("feedback", "write_draft", model="deepseek-v4-pro", started=now - timedelta(seconds=30),
                    ended=None, ok=None, kind=None)


def seed_failed_step(store: Store, settings) -> None:
    now = datetime.now(settings.tz)
    store.set_cursor("tick_seen", (now - timedelta(seconds=20)).isoformat())
    _done(store, "todo", "find_huddle", now - timedelta(seconds=80), 3)
    _done(store, "todo", "read_notes", now - timedelta(seconds=60), 4, ok=False, kind="SlackTimeout")
    store.add_event("todo", "write_todo", model="m", started=now - timedelta(seconds=20), ended=None, ok=None,
                    kind=None)


@needs_chrome
def test_running_job_renders_pipeline(tmp_path):
    with live_server(tmp_path, seed_running) as (base, _s, login):
        dom = render_dom(tmp_path, base, login)
    now = section(dom, "Right now")
    assert "Drafting feedback" in now and "Quiet right now" not in now
    assert re.search(r'class="step done".*?Read channel', now, re.S)
    assert re.search(r'class="step live"[^>]*aria-current="step".*?Write draft', now, re.S)
    assert re.search(r'class="step todo".*?Your DM', now, re.S)
    assert 'class="link flow"' in now
    assert "deepseek-v4-pro" in now
    assert 'class="step fail"' not in now  # the failed first try was retried; the older run's failure is not this run's
    assert "Mac" in dom and "awake" in dom


@needs_chrome
def test_failed_step_shows_in_pipeline_and_timeline(tmp_path):
    with live_server(tmp_path, seed_failed_step) as (base, _s, login):
        dom = render_dom(tmp_path, base, login)
    now = section(dom, "Right now")
    assert re.search(r'class="step done".*?Find huddle', now, re.S)
    assert re.search(r'class="step fail".*?Read notes.*?failed', now, re.S)
    assert re.search(r'class="step live".*?Write todo', now, re.S)
    assert 'class="link failed"' in now
    today = section(dom, "Today")
    assert re.search(r'class="ev w[^"]*".*?Read notes.*?failed · SlackTimeout', today, re.S)


# ---------------------------------------------------------------- interaction, in real time (CDP over a pipe)

class Cdp:
    """Headless Chrome driven over --remote-debugging-pipe (fd 3 in, fd 4 out, NUL-separated JSON): real time, no
    websocket library, no extra dependency."""

    def __init__(self, tmp_path: Path):
        import fcntl
        import os
        r3, w3 = os.pipe()
        r4, w4 = os.pipe()
        # Chrome's ends move above fd 4 first: otherwise dup2 onto 3 or 4 can clobber the other end or be a no-op
        # that leaves FD_CLOEXEC set, and Chrome starts without its pipe ("pipe file descriptors are not open").
        cr, cw = fcntl.fcntl(r3, fcntl.F_DUPFD, 5), fcntl.fcntl(w4, fcntl.F_DUPFD, 5)
        os.close(r3)
        os.close(w4)

        def child():
            os.dup2(cr, 3)
            os.dup2(cw, 4)
            os.set_inheritable(3, True)
            os.set_inheritable(4, True)
            os.close(cr)
            os.close(cw)

        self.proc = subprocess.Popen([*chrome_argv(tmp_path), "--remote-debugging-pipe"], preexec_fn=child,
                                     close_fds=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(cr)
        os.close(cw)
        self.w = os.fdopen(w3, "wb", buffering=0)
        self.r = os.fdopen(r4, "rb", buffering=0)
        self.buf, self.n, self.sid = b"", 0, None

    def _msg(self) -> dict:
        while b"\0" not in self.buf:
            chunk = self.r.read(65536)
            if not chunk:
                raise RuntimeError("chrome went away")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method: str, params: dict | None = None, *, session: bool = True) -> dict:
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session and self.sid:
            msg["sessionId"] = self.sid
        self.w.write(json.dumps(msg).encode() + b"\0")
        while True:
            d = self._msg()
            if d.get("id") == self.n:
                assert "error" not in d, d["error"]
                return d.get("result", {})

    def open(self, url: str, *, dark: bool = True, width: int = 1280, reduced: bool = False,
             init_js: str | None = None) -> None:
        tid = self.call("Target.createTarget", {"url": "about:blank"}, session=False)["targetId"]
        self.sid = self.call("Target.attachToTarget", {"targetId": tid, "flatten": True}, session=False)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1,
                                                         "mobile": width < 500})
        feats = [{"name": "prefers-color-scheme", "value": "dark" if dark else "light"}]
        if reduced:
            feats.append({"name": "prefers-reduced-motion", "value": "reduce"})
        self.call("Emulation.setEmulatedMedia", {"features": feats})
        self.call("Page.enable")
        if init_js:
            self.call("Page.addScriptToEvaluateOnNewDocument", {"source": init_js})
        self.call("Page.navigate", {"url": url})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        assert "exceptionDetails" not in r, r["exceptionDetails"]
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float = 12.0):
        deadline = time.time() + timeout
        # a node counts as found; an exception (the card isn't there yet) as not yet
        probe = f"(() => {{ try {{ const v = ({expr}); return v instanceof Node ? true : v; }} catch (e) {{ return null; }} }})()"
        while time.time() < deadline:
            v = self.js(probe)
            if v:
                return v
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for: {expr}")

    def reload(self) -> None:
        """Reload and wait for the NEW document: right after location.reload() the old one is still answering."""
        self.js("window.__old_doc = true; location.reload()")
        self.wait("!window.__old_doc && document.readyState === 'complete'")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.call("Browser.close", session=False)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for f in (self.w, self.r):  # our pipe ends: closed here, never left to a finalizer
            try:
                f.close()
            except OSError:
                pass


@pytest.fixture
def cdp(tmp_path):
    c = Cdp(tmp_path)
    yield c
    c.close()  # a no-op when the test already closed it (each closes Chrome before its server stops)


def seed_two(store: Store, settings) -> None:
    today = datetime.now(settings.tz).date()
    store.upsert_item(_item("feedback-a", "feedback", "Feedback for Sara", "Great post, Sara. One thought: lead with the number.",
                            today, "20.1"))
    store.upsert_item(_item("todo-b", "todo", "Next Todo", "Next todo: ship the shortlist", today, "20.2"))
    store.set_cursor("autopost:todo-b", (datetime.now(settings.tz) + timedelta(minutes=10)).isoformat())


CARD = "document.querySelector('article[aria-label^=\"{kind}\"]')"
BTN = ("[...{card}.querySelectorAll('button')].find(b => b.textContent.trim() === '{label}')")


def _btn(kind: str, label: str) -> str:
    return BTN.format(card=CARD.format(kind=kind), label=label)


def _commands(settings) -> list:
    st = Store(settings.db_path)
    try:
        return st.recent_page_commands(50)
    finally:
        st.close()


@needs_chrome
def test_post_confirms_disables_and_follows_the_command(tmp_path, cdp):
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({_btn('Feedback', 'Post')})")
        cdp.js(f"{_btn('Feedback', 'Post')}.click()")
        # a confirm step first: nothing is queued until Confirm, and Enter would land on Cancel
        assert cdp.wait(f"{CARD.format(kind='Feedback')}.querySelector('.confirm-q')?.textContent") == \
            "Post to the program channel as you?"
        assert cdp.wait("document.activeElement.textContent.trim() === 'Cancel'")
        assert _commands(settings) == []
        cdp.js(f"{{ const b = {_btn('Feedback', 'Confirm')}; b.click(); b.click(); }}")  # a double click
        cdp.wait(f"{CARD.format(kind='Feedback')}.querySelector('.cstatus.s-queued')")
        assert cdp.js(f"[...{CARD.format(kind='Feedback')}.querySelectorAll('.actions button')].every(b => b.disabled)")
        assert cdp.js(f"document.activeElement === {CARD.format(kind='Feedback')}")  # focus is not lost to <body>
        cmds = _commands(settings)
        assert [(c.item_id, c.verb, c.state) for c in cmds] == [("feedback-a", "post", "queued")]  # exactly one
        # the tick picks it up: 👀, then ✅ with the runner's status text
        st = Store(settings.db_path)
        try:
            st.set_page_command(cmds[0].id, "running")
            cdp.wait(f"{CARD.format(kind='Feedback')}.querySelector('.cstatus.s-running')")
            assert "👀" in cdp.js(f"{CARD.format(kind='Feedback')}.querySelector('.cstatus').textContent")
            assert cdp.js(f"[...{CARD.format(kind='Feedback')}.querySelectorAll('.actions button')].every(b => b.disabled)")
            st.set_page_command(cmds[0].id, "done", "✅ Posted · <b>link</b>")
        finally:
            st.close()
        text = cdp.wait(f"{CARD.format(kind='Feedback')}?.querySelector('.cstatus.s-done')?.textContent")
        assert text.startswith("✅") and text.endswith("Posted · <b>link</b>")  # one glyph; the result as text
        assert cdp.js(f"{CARD.format(kind='Feedback')}.querySelectorAll('b, a').length") == 0
        cdp.close()


@needs_chrome
def test_posted_permalink_is_a_safe_link_until_the_card_leaves(tmp_path, cdp):
    link = "https://mustcompany.slack.com/archives/C0C62C128D8/p1760000000000100"
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        card = CARD.format(kind="Next Todo")
        cdp.wait(f"!!({_btn('Next Todo', 'Post now')})")
        cdp.js(f"{_btn('Next Todo', 'Post now')}.click()")
        cdp.wait(f"!!({_btn('Next Todo', 'Confirm')})")
        cdp.js(f"{_btn('Next Todo', 'Confirm')}.click()")
        cdp.wait(f"{card}.querySelector('.cstatus.s-queued')")
        (cmd,) = _commands(settings)
        st = Store(settings.db_path)
        try:
            st.set_page_command(cmd.id, "done", f"✅ Posted · {link}")
            st.mark("todo-b", "posted")
        finally:
            st.close()
        cdp.wait(f"{card}?.querySelector('.cstatus.s-done')")
        t0 = time.time()
        a = f"{card}.querySelector('.cstatus a')"
        assert cdp.js(f"{a}.getAttribute('href')") == link
        assert cdp.js(f"{a}.rel") == "noopener noreferrer" and cdp.js(f"{a}.target") == "_blank"
        assert cdp.js("document.querySelector('.count-badge').textContent") == "1"  # a posted card isn't waiting
        cdp.wait(f"!{card}", timeout=4)
        assert time.time() - t0 < 3.5  # it leaves within about 3 s
        cdp.close()


@needs_chrome
def test_failed_command_reenables_and_edit_is_an_instruction(tmp_path, cdp):
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({_btn('Next Todo', 'Cancel auto-post')})")
        assert cdp.js(f"{CARD.format(kind='Next Todo')}.querySelector('.auto-chip').textContent").startswith("Auto-posts in ")
        cdp.js(f"{_btn('Next Todo', 'Shorter')}.click()")
        cdp.wait(f"{CARD.format(kind='Next Todo')}.querySelector('.cstatus.s-queued')")
        (cmd,) = _commands(settings)
        assert (cmd.verb, cmd.shown_hash) == ("shorter", __import__("hashlib").sha256(b"Next todo: ship the shortlist").hexdigest())
        st = Store(settings.db_path)
        try:
            st.set_page_command(cmd.id, "failed", "draft changed, reload")
        finally:
            st.close()
        assert "draft changed, reload" in cdp.wait(f"{CARD.format(kind='Next Todo')}.querySelector('.cstatus.s-failed')?.textContent")
        assert cdp.js(f"[...{CARD.format(kind='Next Todo')}.querySelectorAll('.actions button')].every(b => !b.disabled)")
        cdp.js(f"{CARD.format(kind='Next Todo')}.querySelector('.cstatus').firstChild.__kept = 1")
        time.sleep(2.5)  # past the reload that follows a settled command: same words, so the line isn't touched
        assert cdp.js(f"{CARD.format(kind='Next Todo')}.querySelector('.cstatus').firstChild.__kept === 1")
        # Edit (D14): an instruction to revise, like the DM's `edit:`. The box starts empty, capped at the runner's
        # 500 characters; the draft stays readable above it
        card = CARD.format(kind="Next Todo")
        cdp.js(f"{_btn('Next Todo', 'Edit')}.click()")
        ta = f"{card}.querySelector('textarea')"
        cdp.wait(ta)
        assert cdp.js(f"{ta}.value") == ""
        assert cdp.js(f"{ta}.placeholder") == "Tell the agent what to change — e.g. 'lead with the deadline'"
        assert cdp.js(f"{ta}.maxLength") == 500
        assert cdp.js(f"{card}.querySelector('.draft').textContent") == "Next todo: ship the shortlist"
        assert cdp.js(f"{card}.querySelector('.counter').textContent") == "0 / 500"
        assert cdp.js(f"{_btn('Next Todo', 'Revise')}.disabled") is True  # nothing to send yet
        assert cdp.js(f"{_btn('Next Todo', 'Write an instruction instead')} === undefined")
        cdp.js(f"{{ const t = {ta}; t.value = 'lead with the deadline'; t.dispatchEvent(new Event('input')); }}")
        assert cdp.js(f"{card}.querySelector('.counter').textContent") == "22 / 500"
        cdp.js(f"{_btn('Next Todo', 'Revise')}.click()")
        cdp.wait(f"{card}.querySelector('.cstatus.s-queued')")
        edit = [c for c in _commands(settings) if c.verb == "edit"]
        assert len(edit) == 1 and edit[0].arg == "lead with the deadline"
        cdp.close()


@needs_chrome
def test_theme_toggle_is_remembered(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login), dark=True)
        cdp.wait("document.querySelector('.empty-t')")
        assert cdp.js("document.documentElement.dataset.theme") == "midnight"  # follows prefers-color-scheme
        cdp.js("document.querySelector('[data-theme-pick=midnight]').click()")  # the active one: pins it
        assert cdp.js("localStorage.getItem('ha_theme')") == "midnight"
        cdp.js("document.querySelector('[data-theme-pick=daylight]').click()")
        assert cdp.js("document.documentElement.dataset.theme") == "daylight"
        assert cdp.js("localStorage.getItem('ha_theme')") == "daylight"
        assert cdp.js("document.querySelector('[data-theme-pick=daylight]').getAttribute('aria-pressed')") == "true"
        cdp.reload()
        assert cdp.js("document.documentElement.dataset.theme") == "daylight"  # the toggle beats the OS setting
        cdp.close()


@needs_chrome
def test_phone_width_has_no_horizontal_scroll(tmp_path, cdp):
    def seed(store, settings):
        seed_two(store, settings)
        seed_running(store, settings)
        store.upsert_item(_item("todo-long", "todo", "W" * 300, "x" * 3000, datetime.now(settings.tz).date(), "20.3"))
    with live_server(tmp_path, seed, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login), width=380)
        cdp.wait("document.querySelectorAll('article.icard').length === 3 && !!document.querySelector('.step.live')")
        time.sleep(0.8)  # the entrance animations settle
        assert cdp.js("document.documentElement.scrollWidth") <= 380
        assert cdp.js("[...document.querySelectorAll('.card')].every(c => c.getBoundingClientRect().right <= 380)")
        cdp.close()


@needs_chrome
def test_reduced_motion_turns_animation_off(tmp_path, cdp):
    with live_server(tmp_path, seed_running, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login), reduced=True)
        cdp.wait("!!document.querySelector('.step.live .node')")
        names = cdp.js("""[...document.querySelectorAll('.step.live .node, .link.flow i, .dot, .logo, .card, .aurora i')]
            .map(e => getComputedStyle(e).animationName)""")
        assert names and set(names) == {"none"}, names
        cdp.close()



# ---------------------------------------------------------------- review round 1 (I1–I4, M1–M9)

HOSTILE_HREFS = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "https://evil.example",
                 "https://evil.example/archives/C1/p1", "https://mustcompany.slack.com@evil.example/",
                 "https://mustcompany.slack.com.evil.example/", "/\\evil.example", "//evil.example/archives/x",
                 "http://mustcompany.slack.com/archives/C1/p1", "https://mustcompany.slack.com:8443/archives/C1/p1",
                 "https://u:p@mustcompany.slack.com/archives/C1/p1", "https://mustcompany.slack.com/messages/C1",
                 "https://slack.com/archives/C1/p1", "data:text/html,<script>alert(1)</script>", "#/live", ""]
GOOD_HREFS = ["https://mustcompany.slack.com/archives/C0C62C128D8/p1760000000000100",
              "https://mustcompany.slack.com/archives/C1/p2?thread_ts=1.2&cid=C1"]


@needs_chrome
def test_safe_url_accepts_only_workspace_permalinks(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelector('.empty-t')")
        rows = cdp.js("import('/dom.js').then(m => %s.map(u => [u, m.safeUrl(u), "
                      "m.el('a', {attrs: {href: u}}).getAttribute('href'), m.extLink(u, 'x').getAttribute('href'), "
                      "m.safeUrl(u, 'src')]))" % json.dumps(HOSTILE_HREFS + GOOD_HREFS))
        srcs = cdp.js("import('/dom.js').then(m => ['/api/decks/x/thumb.png', 'blob:" + base + "/0a1b', '//evil.x', "
                      "'/\\\\evil.x', 'javascript:x', 'data:image/png;base64,AA', 'https://evil.x/a.png']"
                      ".map(u => m.safeUrl(u, 'src')))")
        cdp.close()
    for u, ok, href, ext, _src in rows:
        if u in GOOD_HREFS:
            assert ok is True and href == u and ext == u, u
        else:
            assert ok is False and href is None and ext is None, u
    assert srcs == [True, True, False, False, False, False, False]


SIGNALS = """(() => { const f = window.fetch; window.__streams = []; window.__actions = 0;
  window.fetch = function (u, o) { const url = String(u);
    if (url.startsWith('/api/stream')) window.__streams.push(o && o.signal);
    if (url === '/api/action') window.__actions += 1;
    return f.apply(this, arguments); }; })();"""


def _all_disabled(kind: str) -> str:
    return f"[...{CARD.format(kind=kind)}.querySelectorAll('.actions button')].every(b => b.disabled)"


@needs_chrome
def test_pending_command_survives_navigation_and_reload(tmp_path, cdp):
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login), init_js=SIGNALS)
        card = CARD.format(kind="Feedback")
        cdp.wait(f"!!({_btn('Feedback', 'Post')})")
        cdp.js(f"{_btn('Feedback', 'Post')}.click()")
        cdp.wait(f"!!({_btn('Feedback', 'Confirm')})")
        cdp.js(f"{_btn('Feedback', 'Confirm')}.click()")
        cdp.wait(f"{card}.querySelector('.cstatus.s-queued')")
        for leave in ("navigate", "reload"):
            if leave == "navigate":
                cdp.js("location.hash = '#/history'")
                cdp.wait("!!document.querySelector('.page-history')")
                cdp.js("location.hash = '#/live'")
            else:
                cdp.reload()
            cdp.wait(f"{card}?.querySelector('.cstatus.s-queued')")  # still waiting on its command
            assert cdp.js(_all_disabled("Feedback")), leave
            cdp.js(f"{_btn('Feedback', 'Post')}.click()")  # a disabled button: nothing happens
            time.sleep(0.3)
            assert cdp.js(f"!{_btn('Feedback', 'Confirm')}"), leave
        assert cdp.js("window.__actions") == 0  # the reload reset the counter: no POST since
        assert [(c.verb, c.state) for c in _commands(settings)] == [("post", "queued")]  # exactly one, ever
        (cmd,) = _commands(settings)
        st = Store(settings.db_path)
        try:  # and the card still follows its command to the end
            st.set_page_command(cmd.id, "running")
            cdp.wait(f"{card}.querySelector('.cstatus.s-running')")
            st.set_page_command(cmd.id, "done", "⏭️ Done")
        finally:
            st.close()
        cdp.wait(f"{card}?.querySelector('.cstatus.s-done')")
        cdp.close()


BANNER = "⚠️ Post may have gone out — check the channel before posting again"
CARD_STATE = """(() => {{ const c = {card}; return c && {{busy: c.classList.contains('busy'),
  banner: c.querySelector('.banner').hidden ? null : c.querySelector('.banner').textContent,
  line: c.querySelector('.cstatus').classList.contains('on') ? c.querySelector('.cstatus').textContent : null}}; }})()"""


@needs_chrome
def test_failed_post_warning_persists(tmp_path, cdp):
    from haseeb_agent.commands import MAY_HAVE_POSTED, mark_post_failed
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        card = CARD.format(kind="Feedback")
        cdp.wait(f"!!({_btn('Feedback', 'Post')})")
        cdp.js(f"{_btn('Feedback', 'Post')}.click()")
        cdp.wait(f"!!({_btn('Feedback', 'Confirm')})")
        cdp.js(f"{_btn('Feedback', 'Confirm')}.click()")
        cdp.wait(f"{card}.querySelector('.cstatus.s-queued')")
        # meanwhile the todo's confirm row is open, and its own auto-post fails underneath it
        cdp.js(f"{_btn('Next Todo', 'Post now')}.click()")
        cdp.wait(f"!!({_btn('Next Todo', 'Confirm')})")
        (cmd,) = _commands(settings)
        st = Store(settings.db_path)
        try:  # what the runner's _fail does for a post that may have gone out: C1 first, then the item, then the row
            mark_post_failed(st, "todo-b")
            st.mark("todo-b", "failed", attempts=1)
            mark_post_failed(st, "feedback-a")
            st.mark("feedback-a", "failed", attempts=1)
            st.set_page_command(cmd.id, "failed", MAY_HAVE_POSTED.format(reason="SlackTimeout"))
        finally:
            st.close()
        # the moment the failure arrives (well before the reload 1.5 s later), the banner is the one warning, with
        # the runner's reason, and the status line says nothing more
        deadline = time.time() + 10
        while (state := cdp.js(CARD_STATE.format(card=card)))["busy"] and time.time() < deadline:
            time.sleep(0.05)
        assert state == {"busy": False, "banner": BANNER + " (SlackTimeout)", "line": None}, state
        # the reload that follows flips the todo too: its open confirm row turns into the explicit check
        cdp.wait(f"!!({_btn('Next Todo', 'I checked — post again')})")
        assert cdp.js(f"!{_btn('Next Todo', 'Confirm')}")
        cdp.js(f"{_btn('Next Todo', 'Cancel')}.click()")
        time.sleep(3)  # past every reload: still one warning
        assert cdp.js(CARD_STATE.format(card=card)) == {"busy": False, "banner": BANNER + " (SlackTimeout)", "line": None}
        cdp.reload()  # the server says so too, reason included
        cdp.wait(f"{card}?.querySelector('.banner:not([hidden])')")
        assert cdp.js(CARD_STATE.format(card=card)) == {"busy": False, "banner": BANNER + " (SlackTimeout)", "line": None}
        # posting again takes an explicit "I checked"
        cdp.js(f"{_btn('Feedback', 'Post')}.click()")
        cdp.wait(f"!!({_btn('Feedback', 'I checked — post again')})")
        assert cdp.js(f"!{_btn('Feedback', 'Confirm')}")
        cdp.js(f"{_btn('Feedback', 'I checked — post again')}.click()")
        cdp.wait(f"{card}.querySelector('.cstatus.s-queued')")
        assert [c.verb for c in _commands(settings)] == ["post", "post"]
        # a network error on POST can't know whether the command landed
        cdp.js("(() => { const f = window.fetch; window.fetch = (u, o) => String(u) === '/api/action' "
               "? Promise.reject(new TypeError('Failed to fetch')) : f(u, o); })()")
        cdp.js(f"{_btn('Next Todo', 'Shorter')}.click()")
        text = cdp.wait(f"{CARD.format(kind='Next Todo')}.querySelector('.cstatus.s-failed')?.textContent")
        assert "may have been queued — check the status" in text
        cdp.close()


def seed_may_have_gone_to_survive(store: Store, settings) -> None:
    """A message whose post may have gone out to #survive-ai (its target was recorded before the send), and the list that names it."""
    from haseeb_agent import channels
    from haseeb_agent.commands import mark_post_failed
    today = datetime.now(settings.tz).date()
    store.upsert_item(_item("message-s", "message", "Message to the channel", "Standup moved to 3 pm.", today, "30.1"))
    store.mark("message-s", "failed", attempts=1)
    mark_post_failed(store, "message-s")
    channels.record_post_channel(store, "message-s", "C093L3Y8UM6")
    store.set_cursor(channels.CACHE_KEY, json.dumps({"fetched_at": datetime.now().astimezone().isoformat(), "channels": [
        {"id": "C093L3Y8UM6", "name": "survive-ai", "private": False}]}))


@needs_chrome
def test_the_warning_of_a_post_that_may_have_gone_out_says_which_channel_to_check(tmp_path, cdp):
    """Post-channel design, ruling 2: the card's banner and the "I checked" question name the channel the post was recorded to,
    as the server says it. (Without a record they keep "the channel": test_failed_post_warning_persists.)"""
    with live_server(tmp_path, seed_may_have_gone_to_survive, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login))
        card = CARD.format(kind="Message")
        cdp.wait(f"{card}?.querySelector('.banner:not([hidden])')")
        assert cdp.js(CARD_STATE.format(card=card)) == {
            "busy": False, "banner": "⚠️ Post may have gone out — check #survive-ai before posting again", "line": None}
        cdp.js(f"{_btn('Message', 'Post')}.click()")
        cdp.wait(f"!!({_btn('Message', 'I checked — post again')})")
        assert cdp.js(f"{card}.querySelector('.confirm-q').textContent") == (
            "The last post may have gone out. Check #survive-ai first.")
        cdp.close()


@needs_chrome
def test_new_tab_without_token_shows_session_expired(tmp_path, cdp):
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login))
        cdp.wait(f"!!({_btn('Feedback', 'Post')})")
        cdp.open(base + "/#/live")  # a second tab: the cookie is there, the per-tab token is not
        assert cdp.wait("document.querySelector('main h1')?.textContent") == "Session expired"
        assert cdp.js("getComputedStyle(document.querySelector('.nav')).display") == "none"
        assert cdp.js("document.querySelectorAll('article.icard').length") == 0
        cdp.close()


@needs_chrome
def test_stream_is_aborted_when_the_session_expires(tmp_path, cdp):
    gate: dict = {}
    with live_server(tmp_path, seed_two, stream_polls=600, gate=gate) as (base, _settings, login):
        cdp.open(login_url(base, login), init_js=SIGNALS)
        cdp.wait(f"!!({_btn('Feedback', 'Post')}) && window.__streams.length === 1")
        assert cdp.js("window.__streams[0].aborted") is False  # the stream is open
        gate.update({"/api/inbox": 403, "/api/live": 403, "/api/logs": 403})
        cdp.js("location.hash = '#/history'")
        time.sleep(0.3)
        cdp.js("location.hash = '#/live'")  # the inbox reload meets the 403
        assert cdp.wait("document.querySelector('main h1')?.textContent") == "Session expired"
        assert cdp.wait("window.__streams.every(s => s.aborted)")
        n = cdp.js("window.__streams.length")
        time.sleep(2.5)
        assert cdp.js("window.__streams.length") == n  # no reconnect after expiry
        cdp.close()


@needs_chrome
def test_skip_link_and_router_keys(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, _settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelector('.empty-t')")
        cdp.js("location.hash = '#/decks'")
        cdp.wait("!!document.querySelector('.page-decks')")
        cdp.js("document.querySelector('a.skip').click()")
        time.sleep(0.3)
        assert cdp.js("location.hash") == "#/decks" and cdp.js("document.activeElement.id") == "view"
        for key in ("constructor", "toString", "__proto__", "hasOwnProperty"):
            cdp.js(f"location.hash = '#/{key}'")
            assert cdp.wait("location.hash === '#/live' && document.title.startsWith('Live')"), key
        cdp.close()


@needs_chrome
def test_refresh_does_not_replay_the_entrance(tmp_path, cdp):
    def seed(store, settings):
        seed_two(store, settings)
        seed_running(store, settings)
    with live_server(tmp_path, seed, stream_polls=600) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.querySelectorAll('.nx').length === 3 && !!document.querySelector('.step.live')")
        time.sleep(1.2)  # the first paint's entrance has finished
        cdp.js("document.querySelectorAll('.nx, .step.live .node').forEach(n => n.__kept = 1)")
        n_ev = cdp.js("document.querySelectorAll('.today .ev').length")
        now = datetime.now(settings.tz)
        st = Store(settings.db_path)
        try:  # a finished step of another job: Today changes, Right now and Coming up don't
            st.add_event("process_dm", "read_dm", model=None, started=now, ended=now + timedelta(seconds=1), ok=True,
                         kind=None)
        finally:
            st.close()
        cdp.wait(f"document.querySelectorAll('.today .ev').length > {n_ev} || "
                 "[...document.querySelectorAll('.today .ev')].some(e => e.textContent.includes('Read DM'))")
        time.sleep(0.2)
        assert cdp.js("[...document.querySelectorAll('.nx')].every(n => n.__kept)")  # Coming up: not rebuilt
        assert cdp.js("document.querySelector('.step.live .node').__kept === 1")  # the pipeline: not rebuilt
        rising = cdp.js("[...document.querySelectorAll('.today .ev, .nx, .step, .icard')]"
                        ".flatMap(e => e.getAnimations()).filter(a => a.animationName === 'rise' && "
                        "a.playState === 'running').length")
        assert rising == 0
        cdp.close()


@needs_chrome
def test_offline_pill_sticks_until_the_dashboard_answers(tmp_path, cdp):
    gate: dict = {}
    with live_server(tmp_path, seed_running, stream_polls=600, gate=gate) as (base, settings, login):
        cdp.open(login_url(base, login))
        cdp.wait("document.getElementById('pills').textContent.includes('awake')")
        gate["/api/live"] = 500
        now = datetime.now(settings.tz)
        st = Store(settings.db_path)
        try:
            st.add_event("process_dm", "read_dm", model=None, started=now, ended=now, ok=True, kind=None)
        finally:
            st.close()
        cdp.wait("document.getElementById('pills').textContent.includes('Offline')")
        time.sleep(2.5)  # the 1 s pill tick must not paint the stale data back
        assert "Offline" in cdp.js("document.getElementById('pills').textContent")
        gate.clear()
        st = Store(settings.db_path)
        try:
            st.add_event("process_dm", "command", model=None, started=now, ended=now, ok=True, kind=None)
        finally:
            st.close()
        cdp.wait("!document.getElementById('pills').textContent.includes('Offline')")
        cdp.close()


def test_static_files_are_revalidated(tmp_path):
    from tests.test_dashboard_security import BASE, do_login

    class Clock:
        t = 1_000_000.0

        def __call__(self):
            return self.t
    settings = _settings(tmp_path)
    app = create_app(settings, lambda: Store(settings.db_path), KEY, lambda: None, clock=Clock(),
                     login_dir=tmp_path / "login")
    client, _token = do_login(app, tmp_path / "login", Clock())
    for path in ("/app.js", "/app.css", "/dom.js", "/actions.js"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
    assert client.get("/").headers["cache-control"] == "no-store"


@needs_chrome
def test_second_tab_follows_the_command_already_in_flight(tmp_path, cdp):
    with live_server(tmp_path, seed_two, stream_polls=200) as (base, settings, login):
        cdp.open(login_url(base, login))
        card = CARD.format(kind="Feedback")
        cdp.wait(f"!!({_btn('Feedback', 'Shorter')})")
        st = Store(settings.db_path)
        try:  # another tab posted it a moment ago; this page hasn't reloaded its inbox since
            it = st.get_item("feedback-a")
            cid = st.enqueue_page_command(it.id, "post", None, __import__("hashlib").sha256(it.body.encode()).hexdigest(),
                                          datetime.now(settings.tz))
        finally:
            st.close()
        cdp.js(f"{_btn('Feedback', 'Shorter')}.click()")
        assert "Post: queued" in cdp.wait(f"{card}.querySelector('.cstatus.s-queued')?.textContent")  # its command
        assert cdp.js(_all_disabled("Feedback"))
        assert [c.id for c in _commands(settings)] == [cid]  # 409: no second row
        st = Store(settings.db_path)
        try:
            st.set_page_command(cid, "done", "⏭️ Done")
        finally:
            st.close()
        cdp.wait(f"{card}?.querySelector('.cstatus.s-done')")
        cdp.close()


# ---------------------------------------------------------------- Task 9: the History page

CH9 = "C0CHANNEL"
ANN, BOB = ("U01ANN", "Ann"), ("U02BOB", "Bob")
PWN = '<img src=x onerror="window.__pwned=1">'
MORE_HOSTILE = ('<svg onload="window.__pwned=1">', '<iframe srcdoc="<script>parent.__pwned=1</script>">',
                '<style>*{display:none!important}</style>', '<base href="https://evil.example/">',
                '<a href="javascript:window.__pwned=1">click</a>')
EVIL = ("U0EVIL", f"{RLO}evil{LRI} {HOSTILE_IMG}")
HIST_READY = ("document.querySelector('.hist')?.dataset.ready === '1' && "
              "document.querySelector('.hist-main')?.getAttribute('aria-busy') !== 'true'")
# every /api/history request (URL and time) and every /api/label request (its token header and body); a test can
# hold /api/label back for a moment (window.__labelDelay) to see the optimistic state
RECORD = """(() => { const f = window.fetch; window.__hist = []; window.__label = []; window.__labelDelay = 0;
  window.fetch = function (u, o) { const url = String(u), args = arguments;
    if (url.startsWith('/api/history')) window.__hist.push({url, t: performance.now()});
    if (url === '/api/label') {
      window.__label.push({token: o && o.headers ? o.headers['X-Session-Token'] : null, body: o && o.body});
      if (window.__labelDelay) return new Promise(r => setTimeout(r, window.__labelDelay)).then(() => f.apply(this, args));
    }
    return f.apply(this, args); }; })();"""
PARAMS = "window.__hist.map(r => Object.fromEntries(new URL(r.url, location.origin).searchParams))"
TYPE = """(() => { const i = document.querySelector('.search input'); i.focus(); i.value = %s;
  i.dispatchEvent(new Event('input', {bubbles: true})); })()"""


def _last_week(settings):
    """Monday of last week: always wholly in the past."""
    today = datetime.now(settings.tz).date()
    return today - timedelta(days=today.weekday() + 7)


def _ts_at(settings, day, at) -> str:
    return f"{datetime.combine(day, dtime(*at), tzinfo=settings.tz).timestamp():.6f}"


def put(store, settings, day, at, text, who, kind="other", thread=None, topic=None) -> str:
    """A channel message as the import stores it, its author in people too. Returns its ts."""
    ts = _ts_at(settings, day, at)
    store.upsert_person(*who)
    store.upsert_message(StoredMessage(channel=CH9, ts=ts, thread_ts=thread, user_id=who[0], user_name=who[1],
                                       text=text, kind=kind, day=day, topic=topic))
    return ts


def seed_team(store, settings) -> None:
    me, bot = (settings.haseeb_id, "Haseeb"), ("USLACKBOT", "Slackbot")
    mon = _last_week(settings)
    wed = mon + timedelta(days=2)
    ins_ann = put(store, settings, wed, (9, 0), "Daily insight: Voice agents for clinic booking\n"
                  "Clinics in Lahore lose 30% of calls.", ANN, "insight", topic="agents")
    ins_me = put(store, settings, wed, (9, 12), "Daily insight: Gemini Argon's launch price ends Nov 30\n"
                 "The pricing window closes soon.", me, "insight", topic="pricing")
    put(store, settings, wed, (9, 30), "Strong, but the 3x claim needs a source.", me, "feedback", thread=ins_ann,
        topic="agents")
    put(store, settings, wed, (10, 0), "Add a cost table to the pricing slide.", BOB, "feedback", thread=ins_me,
        topic="pricing")
    put(store, settings, wed, (10, 5), "Who pays when the price doubles?", ANN, "feedback", thread=ins_me,
        topic="pricing")
    put(store, settings, wed, (11, 0), "Lunch at 1?", BOB, "other")
    put(store, settings, wed, (15, 12), "*Agents go live*\n• Everyone posts insights by 09:00\n"
        "• Shortlist 3 Africa events by Friday", bot, "huddle_notes")
    put(store, settings, wed, (15, 16), "Next todo: ship the dashboard", me, "todo")
    put(store, settings, wed, (15, 20), "Next todo: collect three quotes", ANN, "todo")
    put(store, settings, mon, (9, 5), "Daily insight: Stablecoin payroll for remote teams", BOB, "insight",
        topic="crypto")
    put(store, settings, mon, (12, 0), "Good morning team", ANN, "other")


def seed_hostile_history(store, settings) -> None:
    me, bot = (settings.haseeb_id, "Haseeb"), ("USLACKBOT", f"Slackbot {PWN}")
    mon = _last_week(settings)
    ins = put(store, settings, mon, (9, 0), f"Daily insight: {HOSTILE_IMG} {PWN}\n{HOSTILE_SCRIPT} {HOSTILE_BIDI}",
              EVIL, "insight", topic=f"{HOSTILE_IMG} {RLO}topic")
    put(store, settings, mon, (9, 30), f"{HOSTILE_SCRIPT} {HOSTILE_BIDI} <javascript:alert(4)|click me>", me,
        "feedback", thread=ins)
    put(store, settings, mon, (15, 10), f"*{HOSTILE_IMG}*\n• {HOSTILE_SCRIPT}\n• {HOSTILE_BIDI}", bot, "huddle_notes")
    put(store, settings, mon, (15, 15), f"Next todo: {HOSTILE_IMG} {HOSTILE_BIDI}", EVIL, "todo")
    put(store, settings, mon, (16, 0), f"{PWN} {HOSTILE_BIDI} <https://evil.example/x|&lt;img src=x onerror=alert(1)&gt;>",
        EVIL, "other", topic=f"{RLO}x")
    put(store, settings, mon, (16, 5), f"orphan reply {HOSTILE_BIDI}", EVIL, "feedback", thread="1.000001")
    svg, frame, style, base, anchor = MORE_HOSTILE
    ins2 = put(store, settings, mon, (10, 0), f"Daily insight: {svg}\n{frame}", EVIL, "insight")
    put(store, settings, mon, (10, 5), f"{anchor} {style}", EVIL, "feedback", thread=ins2)
    put(store, settings, mon, (15, 20), f"Next todo: {frame} {base}", EVIL, "todo")
    put(store, settings, mon, (16, 10), f"{base} {style} {svg}", bot, "huddle_notes")
    put(store, settings, mon, (16, 15), f"{anchor}", EVIL, "other")


def open_history(cdp, base, login, **kw) -> None:
    cdp.open(login_url(base, login), **kw)
    cdp.wait("!!document.querySelector('.page-live')")
    cdp.js("location.hash = '#/history'")
    cdp.wait(HIST_READY)


# what the page shows, checked in the page: no element or script from the data, no link from a hostile permalink,
# every bidi control isolated inside a <bdi>, and no Slack text in any attribute
VIEW_SAFE = r"""(() => { const v = document.getElementById('view');
  const w = document.createTreeWalker(v, NodeFilter.SHOW_TEXT); let n, seen = 0, loose = 0;
  while ((n = w.nextNode())) if (/[\u202e\u2066]/.test(n.data)) { seen++; if (!n.parentElement.closest('bdi')) loose++; }
  const attrs = [...v.querySelectorAll('*')].some(e => [...e.attributes].some(a => /[\u202e\u2066<>]/.test(a.value)));
  return {seen, loose, attrs, imgs: document.querySelectorAll('img').length, scripts: document.scripts.length,
    links: v.querySelectorAll('a').length, js: !!document.querySelector('[href^="javascript" i], [src^="javascript" i]'),
    tags: document.querySelectorAll('svg, iframe, style, base, object, embed, link:not([rel=stylesheet]):not([rel=icon])').length,
    base: new URL(document.baseURI).origin === location.origin, sheets: document.styleSheets.length,
    pwned: window.__pwned === 1, text: v.textContent}; })()"""


@needs_chrome
def test_history_hostile_text_literal(tmp_path, cdp):
    hostile_link = {"workspace_url": "javascript:alert(1)//"}  # every permalink the API builds is now hostile
    with live_server(tmp_path, seed_hostile_history, stream_polls=200, patch=hostile_link) as (base, _s, login):
        open_history(cdp, base, login)
        cdp.wait("document.querySelectorAll('.hday').length === 1")
        for view in ("day", "person", "topic", "search"):
            if view == "search":
                cdp.js(TYPE % json.dumps(HOSTILE_IMG))
                cdp.wait("!!document.querySelector('.results') && " + HIST_READY)
            elif view != "day":
                cdp.js(f"document.querySelector('.vbtn[data-view={view}]').click()")
                cdp.wait(f"document.querySelector('.vbtn[data-view={view}]').getAttribute('aria-pressed') === 'true'"
                         f" && document.querySelectorAll('.lane').length > 0 && " + HIST_READY)
            s = cdp.js(VIEW_SAFE)
            assert (s["imgs"], s["scripts"], s["pwned"]) == (0, 2, False), view
            assert (s["tags"], s["base"], s["sheets"]) == (0, True, 1), view  # no svg, iframe, style or base from data
            assert s["links"] == 0 and not s["js"], view  # a javascript: permalink is never a link
            assert s["seen"] >= 1 and s["loose"] == 0 and not s["attrs"], (view, s["seen"], s["loose"], s["attrs"])
            text = s["text"]
            assert HOSTILE_IMG in text and "javascript:alert(3)" in text, view
            if view != "search":
                assert HOSTILE_SCRIPT in text and PWN in text and "<javascript:alert(4)|click me>" in text, view
                for vec in MORE_HOSTILE:
                    assert vec in text, (view, vec)
        assert f"Results for “{HOSTILE_IMG}”" in s["text"]
        cdp.js("document.querySelector('.results .clear').click()")
        cdp.wait("!document.querySelector('.results') && document.querySelectorAll('.lane').length > 0 && " + HIST_READY)
        assert f"{HOSTILE_IMG} {RLO}topic" in cdp.js("document.querySelector('.hist-main').textContent")  # topic lanes
        cdp.close()


@needs_chrome
def test_history_empty_state(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, _s, login):
        open_history(cdp, base, login, init_js=RECORD)
        text = cdp.wait("document.querySelector('.hist-empty')?.textContent")
        assert "No history yet — run haseeb-agent history-import" in text
        assert cdp.js("document.querySelector('.hist-empty code').textContent") == "haseeb-agent history-import"
        assert cdp.js("window.__hist.length") == 0  # no week to load
        assert cdp.js("document.querySelectorAll('.load-error, .hday').length") == 0
        assert "Session expired" not in cdp.js("document.body.textContent")
        cdp.close()


@needs_chrome
def test_history_day_order(tmp_path, cdp):
    with live_server(tmp_path, seed_team, stream_polls=200) as (base, settings, login):
        open_history(cdp, base, login, init_js=RECORD)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        mon = _last_week(settings)
        wed = mon + timedelta(days=2)
        assert cdp.js("[...document.querySelectorAll('.hday')].map(d => d.dataset.day)") == [wed.isoformat(), mon.isoformat()]
        assert cdp.js(PARAMS) == [{"view": "day", "week": mon.isoformat()}]  # one week, the newest with messages
        day = "document.querySelector('.hday[data-day=\"%s\"]')" % wed.isoformat()
        # the fixed order: the huddle, then the insights with their feedback, then the rest, collapsed
        assert cdp.js(f"[...{day}.querySelectorAll('.sec')].map(s => s.dataset.sec)") == ["huddle", "insights", "other"]
        huddle = cdp.js(f"{day}.querySelector('.sec-huddle .notes').textContent")
        assert "Agents go live" in huddle and "Shortlist 3 Africa events by Friday" in huddle
        rows = cdp.js(f"[...{day}.querySelectorAll('.todo-row')].map(r => [r.querySelector('.who').textContent, "
                      "r.classList.contains('mine'), r.querySelector('.txt').textContent])")
        assert rows == [["Haseeb", True, "ship the dashboard"], ["Ann", False, "collect three quotes"]]
        cards = cdp.js(f"""[...{day}.querySelectorAll('.ins')].map(c => ({{title: c.querySelector('.ins-t').textContent,
            mine: c.classList.contains('mine'), fb: [...c.querySelectorAll('.fb-row .txt')].map(t => t.textContent),
            fbMine: [...c.querySelectorAll('.fb-row')].map(r => r.classList.contains('mine'))}}))""")
        assert cards == [
            {"title": "Voice agents for clinic booking", "mine": False, "fb": ["Strong, but the 3x claim needs a source."],
             "fbMine": [True]},
            {"title": "Gemini Argon's launch price ends Nov 30", "mine": True,
             "fb": ["Add a cost table to the pricing slide.", "Who pays when the price doubles?"], "fbMine": [False, False]},
        ]
        other = f"{day}.querySelector('details.sec-other')"
        assert cdp.js(f"{other}.open") is False
        assert cdp.js(f"{other}.querySelector('summary').textContent").strip() == "Other messages (1)"
        assert "Lunch at 1?" in cdp.js(f"{other}.textContent") and "cost table" not in cdp.js(f"{other}.textContent")
        # every item links to its Slack message (the API permalink) and can be re-labelled
        assert cdp.js(f"{day}.querySelectorAll('a.slack-link').length") == 9
        assert cdp.js(f"{day}.querySelectorAll('button.lbl').length") == 9
        link = cdp.js(f"(() => {{ const a = [...{day}.querySelectorAll('.ins')][1].querySelector(':scope > .tools a.slack-link');"
                      " return [a.getAttribute('href'), a.rel, a.target, a.textContent.replace(/\\s+/g, ' ').trim()]; })()")
        ts = _ts_at(settings, wed, (9, 12))
        assert link[0] == f"{settings.workspace_url}/archives/{CH9}/p{ts.replace('.', '')}"
        assert link[1:3] == ["noopener noreferrer", "_blank"]
        # each tool's accessible name says whose item it is and what kind (as text, never in an attribute)
        assert link[3] == "Open in Slack ↗ — Insight by Haseeb"
        names = cdp.js(f"[...{day}.querySelectorAll('.todo-row')].map(r => [r.querySelector('.lbl').textContent"
                       ".replace(/\\s+/g, ' ').trim(), r.querySelector('.lbl').getAttribute('aria-label')])")
        assert names == [["Change the label (now Next Todo) of the message by Haseeb", None],
                         ["Change the label (now Next Todo) of the message by Ann", None]]
        # an insight's own tools come before its replies (reading and Tab order)
        assert cdp.js(f"[...{day}.querySelectorAll('.ins')].every(c => c.querySelector(':scope > .tools')"
                      ".compareDocumentPosition(c.querySelector('.fb')) & Node.DOCUMENT_POSITION_FOLLOWING)")
        # the week list loads another week only when picked
        today = datetime.now(settings.tz).date()
        this_week = (today - timedelta(days=today.weekday())).isoformat()
        wk = "document.querySelector('.wk[data-week=\"%s\"]')" % this_week
        assert cdp.js(f"document.querySelector('.wk[data-week=\"{mon.isoformat()}\"]').getAttribute('aria-current')") == "true"
        cdp.js(f"{wk}.click()")
        cdp.wait(f"{wk}.getAttribute('aria-current') === 'true' && !!document.querySelector('.week-empty') && " + HIST_READY)
        assert cdp.js(PARAMS)[1:] == [{"view": "day", "week": this_week}]
        cdp.close()


@needs_chrome
def test_person_filter(tmp_path, cdp):
    with live_server(tmp_path, seed_team, stream_polls=200) as (base, settings, login):
        open_history(cdp, base, login, init_js=RECORD)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        st = Store(settings.db_path)
        try:
            color = {p.user_id: p.color for p in st.people()}[BOB[0]]
        finally:
            st.close()
        mon = _last_week(settings).isoformat()
        chip = "[...document.querySelectorAll('.pchip')].find(c => c.querySelector('.pname')?.textContent === 'Bob')"
        everyone = "document.querySelector('.pchip.all')"
        assert cdp.js(f"{chip}.querySelector('.av').classList.contains('pc-{color}')")  # the stable colour
        assert cdp.js(f"{chip}.getAttribute('aria-pressed')") == "false"
        assert cdp.js(f"{everyone}.getAttribute('aria-pressed')") == "true"
        cdp.js(f"{chip}.click()")
        cdp.wait(f"{chip}.getAttribute('aria-pressed') === 'true' && window.__hist.length === 2 && " + HIST_READY)
        assert cdp.js(PARAMS)[-1] == {"view": "day", "week": mon, "person": BOB[0]}
        assert cdp.js(f"{everyone}.getAttribute('aria-pressed')") == "false"
        text = cdp.js("document.querySelector('.hist-main').textContent")
        for t in ("Add a cost table to the pricing slide.", "Lunch at 1?", "Stablecoin payroll for remote teams"):
            assert t in text, t
        for t in ("Voice agents", "ship the dashboard", "Who pays", "Agents go live", "Good morning"):
            assert t not in text, t
        # his reply to Haseeb's insight has no insight to sit under here: it shows as feedback, not hidden in Other
        assert "Add a cost table" in cdp.js("document.querySelector('.sec-feedback').textContent")
        assert cdp.js("document.querySelector('.sec-feedback .sec-h').textContent") == "Feedback · not under a shown insight"
        # under a filter an insight can't say "No feedback yet": its replies may be filtered out
        assert cdp.js("[...document.querySelectorAll('.ins .ins-foot .meta')].map(m => m.textContent)") == \
            ["No replies shown"]
        cdp.js(f"{chip}.click()")  # again: the filter clears
        cdp.wait(f"{chip}.getAttribute('aria-pressed') === 'false' && window.__hist.length === 3 && " + HIST_READY)
        assert cdp.js(PARAMS)[-1] == {"view": "day", "week": mon}
        assert cdp.js(f"{everyone}.getAttribute('aria-pressed')") == "true"
        assert "Voice agents" in cdp.js("document.querySelector('.hist-main').textContent")
        cdp.close()


@needs_chrome
def test_relabel_sends_session_token_and_rolls_back(tmp_path, cdp):
    gate: dict = {}
    with live_server(tmp_path, seed_team, stream_polls=200, gate=gate) as (base, settings, login):
        open_history(cdp, base, login, init_js=RECORD)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        wed = _last_week(settings) + timedelta(days=2)
        day = "document.querySelector('.hday[data-day=\"%s\"]')" % wed.isoformat()
        row = "[...document.querySelectorAll('.todo-row')].find(r => r.textContent.includes('collect three quotes'))"
        cdp.js(f"{row}.querySelector('.lbl').click()")
        menu = cdp.wait("document.querySelector('.menu[role=menu]') && [...document.querySelectorAll("
                        "'.menu [role=menuitemradio]')].map(i => [i.textContent.trim(), i.getAttribute('aria-checked')])")
        assert menu == [["Huddle notes", "false"], ["Next Todo", "true"], ["Insight", "false"], ["Feedback", "false"],
                        ["Other", "false"]]
        assert cdp.js(f"{row}.querySelector('.lbl').getAttribute('aria-expanded')") == "true"
        cdp.js("document.querySelector('.menu [data-kind=other]').click()")
        cdp.wait("window.__label.length === 1")
        sent = cdp.js("window.__label[0]")
        token = cdp.js("sessionStorage.getItem('ha_token')")
        assert token and sent["token"] == token  # the request carries this session's X-Session-Token
        ts = _ts_at(settings, wed, (15, 20))
        assert json.loads(sent["body"]) == {"channel": CH9, "ts": ts, "kind": "other"}
        # once the server has it, the item moves to its new section
        cdp.wait(f"!{row} && {day}.querySelector('details.sec-other summary').textContent.trim() === 'Other messages (2)'")
        st = Store(settings.db_path)
        try:
            assert ts in [m.ts for m in st.messages(kind="other")]
        finally:
            st.close()
        # a 4xx: the new label shows at once, then rolls back, with a note
        gate["/api/label"] = 404
        cdp.js("window.__labelDelay = 600")
        card = "[...document.querySelectorAll('.ins')].find(c => c.textContent.includes('Gemini Argon'))"
        lbl = f"{card}.querySelector(':scope > .tools .lbl')"
        assert cdp.js(f"{lbl}.querySelector('.lbl-t').textContent") == "Insight"
        cdp.js(f"{lbl}.click()")
        cdp.wait("!!document.querySelector('.menu [data-kind=feedback]')")
        cdp.js("document.querySelector('.menu [data-kind=feedback]').click()")
        assert cdp.js(f"{lbl}.querySelector('.lbl-t').textContent") == "Feedback" and cdp.js(f"{lbl}.classList.contains('pending')")
        err = cdp.wait(f"{card}.querySelector('.tool-err')?.textContent")
        assert "Couldn't change the label" in err
        assert cdp.js(f"{lbl}.querySelector('.lbl-t').textContent") == "Insight" and not cdp.js(f"{lbl}.classList.contains('pending')")
        assert cdp.js("window.__label.length") == 2 and cdp.js("window.__label[1].token") == token
        st = Store(settings.db_path)
        try:
            assert _ts_at(settings, wed, (9, 12)) in [m.ts for m in st.messages(kind="insight")]  # unchanged
        finally:
            st.close()
        cdp.close()


@needs_chrome
def test_history_search_debounce_and_clear(tmp_path, cdp):
    with live_server(tmp_path, seed_team, stream_polls=200) as (base, settings, login):
        open_history(cdp, base, login, init_js=RECORD)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        assert cdp.js("document.querySelector('.search input').placeholder") == \
            'Search… “pricing”, “Africa”, person:"Sara Ali"'
        cdp.js("""(() => { const i = document.querySelector('.search input'); i.focus();
          const type = (v) => { i.value = v; i.dispatchEvent(new Event('input', {bubbles: true})); };
          window.__typed = performance.now(); type('pri');
          setTimeout(() => type('pric'), 60); setTimeout(() => type('pricing'), 120); })()""")
        time.sleep(0.2)
        assert cdp.js("window.__hist.length") == 1  # still typing: nothing sent yet
        cdp.wait("!!document.querySelector('.results') && " + HIST_READY)
        sent = cdp.js("window.__hist.slice(1).map(r => [Object.fromEntries(new URL(r.url, location.origin).searchParams),"
                      " r.t - window.__typed])")
        assert len(sent) == 1 and sent[0][0] == {"view": "day", "q": "pricing"}  # one request, for the last words
        assert sent[0][1] >= 120 + 240  # 250 ms after the last keystroke
        assert cdp.js("document.querySelector('.results h2').textContent") == "Results for “pricing”"
        text = cdp.js("document.querySelector('.hist-main').textContent")
        assert "Gemini Argon" in text and "Add a cost table to the pricing slide." in text and "Voice agents" not in text
        # person: and kind: go to the server as they are
        cdp.js(TYPE % json.dumps(f"pricing person:{BOB[0]}"))
        cdp.wait(f"document.querySelector('.results h2')?.textContent === 'Results for “pricing person:{BOB[0]}”' && "
                 + HIST_READY)
        assert cdp.js(PARAMS)[-1] == {"view": "day", "q": f"pricing person:{BOB[0]}"}
        text = cdp.js("document.querySelector('.hist-main').textContent")
        assert "Add a cost table" in text and "Gemini Argon" not in text
        # Clear: back to the week, as it was
        cdp.js("document.querySelector('.results .clear').click()")
        cdp.wait("!document.querySelector('.results') && document.querySelectorAll('.hday').length === 2 && " + HIST_READY)
        assert cdp.js("document.querySelector('.search input').value") == ""
        assert cdp.js(PARAMS)[-1] == {"view": "day", "week": _last_week(settings).isoformat()}
        cdp.close()


@needs_chrome
def test_history_phone_width_has_no_horizontal_scroll(tmp_path, cdp):
    def seed(store, settings):
        seed_team(store, settings)
        mon = _last_week(settings)
        long_one = ("U03LONG", "Maximilian " + "Long" * 20)
        put(store, settings, mon, (13, 0), "Daily insight: " + "W" * 400 + "\n" + "x" * 3000, long_one, "insight",
            topic="t" * 200)
        put(store, settings, mon, (13, 5), "Next todo: " + "y" * 2000, long_one, "todo")
        put(store, settings, mon, (13, 9), "z" * 2000, long_one, "other")
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        open_history(cdp, base, login, width=380)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        for touch in (False, True):  # a mouse (tools float on hover), then a touch screen (tools sit at each foot)
            if touch:
                cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                assert cdp.js("matchMedia('(hover: none)').matches")
            for view in ("day", "person", "topic"):
                cdp.js(f"document.querySelector('.vbtn[data-view={view}]').click()")
                cdp.wait(f"document.querySelector('.vbtn[data-view={view}]').getAttribute('aria-pressed') === 'true' && "
                         + ("document.querySelectorAll('.hday').length === 2" if view == "day" else
                            "document.querySelectorAll('.lane').length > 0") + " && " + HIST_READY)
                cdp.js("document.querySelectorAll('details.sec-other').forEach(d => d.open = true)")
                time.sleep(0.8)  # the entrance settles
                assert cdp.js("document.documentElement.scrollWidth") <= 380, (touch, view)
                assert cdp.js("[...document.querySelectorAll('#view .card, #view .box, #view .ins, #view .mrow, #view .tools')]"
                              ".every(c => c.getBoundingClientRect().right <= 380.5)"), (touch, view)
            assert cdp.js("document.querySelector('.hist-side').getBoundingClientRect().height") < 80  # one strip of weeks
        cdp.close()


# ---------------------------------------------------------------- Task 9 review: ReDoS, focus, mine, entrance, scope

KEYS = {"Enter": dict(key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r"),
        "ArrowDown": dict(key="ArrowDown", code="ArrowDown", windowsVirtualKeyCode=40),
        "Tab": dict(key="Tab", code="Tab", windowsVirtualKeyCode=9)}
# the page's 60 s refresh, sped up to 300 ms
FAST_REFRESH = """(() => { const si = window.setInterval;
  window.setInterval = function (f, ms) { return si.call(this, f, ms === 60000 ? 300 : ms); }; })();"""
# when the /api/history answer arrived, and when the page had rendered it (aria-busy back to false)
TIMING = """(() => { window.__t = {};
  const f = window.fetch;
  window.fetch = function (u) { const p = f.apply(this, arguments);
    if (String(u).startsWith('/api/history')) p.then(() => { window.__t.resp = performance.now(); });
    return p; };
  document.addEventListener('DOMContentLoaded', () => new MutationObserver(() => {
    const m = document.querySelector('.hist-main');
    if (m && m.getAttribute('aria-busy') === 'false' && window.__t.resp && !window.__t.done) window.__t.done = performance.now();
  }).observe(document.body, {attributes: true, subtree: true, attributeFilter: ['aria-busy']})); })();"""


def press(cdp, name: str) -> None:
    """A real key press (keyDown then keyUp), as the keyboard sends it."""
    for kind in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", dict(KEYS[name], type=kind))


def seed_long_markup(store, settings) -> None:
    """40k-character messages built to make a backtracking regex go quadratic."""
    mon, n = _last_week(settings), 40000
    rows = ["<!" + "a" * n, "<!" + "a" * n + "<", "Daily insight: x" + " " * n + "y", "x" + "." * n + "y",
            " *" + "a" * n, ":" + "a" * n, "<https://x.co/" + "a" * n + "|" + "b" * n, "<@U" + "A" * n]
    for i, text in enumerate(rows):
        put(store, settings, mon, (9, i), text, ANN, "insight" if text.startswith("Daily") else "other")


@needs_chrome
def test_history_long_markup_renders_fast(tmp_path, cdp):
    with live_server(tmp_path, seed_long_markup, stream_polls=200) as (base, _s, login):
        open_history(cdp, base, login, init_js=TIMING)
        cdp.wait("document.querySelectorAll('.hday').length === 1 && !!window.__t.done", timeout=30)
        took = cdp.js("window.__t.done - window.__t.resp")
        assert took < 300, f"rendering took {took:.0f} ms"
        assert "<!" + "a" * 50 in cdp.js("document.querySelector('.hist-main').textContent")  # shown as typed
        cdp.close()


@needs_chrome
def test_history_keyboard_keeps_focus(tmp_path, cdp):
    with live_server(tmp_path, seed_team, stream_polls=200) as (base, settings, login):
        open_history(cdp, base, login, init_js=RECORD + FAST_REFRESH)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        mon = _last_week(settings)
        today = datetime.now(settings.tz).date()
        this_week = (today - timedelta(days=today.weekday())).isoformat()
        # Enter on a control: that control keeps the focus (it isn't rebuilt), and the new count is spoken
        steps = [("document.querySelector('.ktab[data-kind=todo]')", "2 messages"),
                 ("[...document.querySelectorAll('.pchip')].find(c => c.querySelector('.pname')?.textContent === 'Ann')",
                  "1 message"),
                 ("document.querySelector('.vbtn[data-view=person]')", "1 message"),
                 (f"document.querySelector('.wk[data-week=\"{this_week}\"]')", "0 messages")]
        for ctl, said in steps:
            n = cdp.js("window.__hist.length")
            cdp.js(f"{ctl}.focus()")
            press(cdp, "Enter")
            cdp.wait(f"window.__hist.length === {n + 1} && " + HIST_READY)
            assert cdp.js(f"document.activeElement === {ctl}"), ctl
            assert cdp.js("document.getElementById('announce').textContent") == said, ctl
        for ctl in ("[...document.querySelectorAll('.ktab')][0]", "document.querySelector('.pchip.all')",
                    "document.querySelector('.vbtn[data-view=day]')", f"document.querySelector('.wk[data-week=\"{mon}\"]')"):
            cdp.js(f"{ctl}.click()")
        cdp.wait("document.querySelectorAll('.hday').length === 2 && document.querySelectorAll('.todo-row').length === 2 && "
                 + HIST_READY)
        # re-label by keyboard: the item moves to Other, which opens for it, and its label button keeps the focus
        row = "[...document.querySelectorAll('.todo-row')].find(r => r.textContent.includes('collect three quotes'))"
        cdp.js(f"{row}.querySelector('.lbl').focus()")
        press(cdp, "Enter")
        cdp.wait("document.activeElement?.getAttribute('role') === 'menuitemradio'")
        assert cdp.js("document.activeElement.textContent.trim()") == "Next Todo"
        assert cdp.js("(() => { const c = getComputedStyle(document.activeElement); return [c.outlineStyle, c.outlineWidth]; })()") \
            == ["solid", "2px"]  # the focused menu item shows the focus ring
        for _ in range(3):
            press(cdp, "ArrowDown")
        assert cdp.js("document.activeElement.textContent.trim()") == "Other"
        press(cdp, "Enter")
        moved = "[...document.querySelectorAll('details.sec-other .mrow')].find(r => r.textContent.includes('collect three quotes'))"
        cdp.wait(f"!!{moved} && document.activeElement === {moved}.querySelector('.lbl')")
        assert cdp.js(f"{moved}.closest('details').open") and cdp.js("window.__label.length") == 1
        # it moved: the page follows it into view (the one re-render that may scroll)
        assert cdp.js("(() => { const r = document.activeElement.getBoundingClientRect(); "
                      "return r.top >= 0 && r.bottom <= innerHeight; })()")
        # the 60 s refresh re-renders around him when a message arrives: the focus stays on the same item
        wed = mon + timedelta(days=2)
        st = Store(settings.db_path)
        try:
            put(st, settings, wed, (12, 30), "A brand new message", BOB, "other")
        finally:
            st.close()
        day = "document.querySelector('.hday[data-day=\"%s\"]')" % wed.isoformat()
        cdp.wait(f"{day}.querySelector('details.sec-other summary').textContent.trim() === 'Other messages (3)'")
        assert cdp.js(f"document.activeElement === {moved}.querySelector('.lbl')")
        # Tab out of an open menu carries on from its button: to the item's Slack link
        press(cdp, "Enter")
        cdp.wait("!!document.querySelector('.menu')")
        press(cdp, "Tab")
        cdp.wait("!document.querySelector('.menu')")
        assert cdp.js(f"document.activeElement === {moved}.querySelector('a.slack-link')")
        cdp.close()


@needs_chrome
def test_history_mine_follows_the_id_not_the_name(tmp_path, cdp):
    def seed(store, settings):
        mon = _last_week(settings)
        real, impostor = (settings.haseeb_id, "Haseeb Zafar"), ("U0IMPOSTOR", "Haseeb Zafar")
        ins = put(store, settings, mon, (9, 0), "Daily insight: the real one", real, "insight")
        put(store, settings, mon, (9, 5), "Daily insight: the impostor's", impostor, "insight")
        put(store, settings, mon, (9, 10), "impostor reply", impostor, "feedback", thread=ins)
        put(store, settings, mon, (15, 0), "Next todo: real todo", real, "todo")
        put(store, settings, mon, (15, 1), "Next todo: impostor todo", impostor, "todo")
        put(store, settings, mon, (16, 0), "impostor named You", ("U0YOU", "You"), "other")
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        open_history(cdp, base, login)
        cdp.wait("document.querySelectorAll('.hday').length === 1")
        mine = cdp.js("[...document.querySelectorAll('#view .mine')].map(e => e.querySelector('.ins-t, .txt').textContent)")
        assert mine == ["real todo", "the real one"]  # Haseeb's id only, never his name
        own = ("[...document.querySelectorAll('.item')].filter(i => (i.querySelector(':scope > .ins-t, :scope > .row-main .txt')"
               "?.textContent || '').includes('impostor'))")  # an item's own words, not its replies'
        assert cdp.js(f"{own}.length") == 4
        assert cdp.js(f"{own}.every(i => !i.classList.contains('mine') && !i.querySelector(':scope > .row-main .you, .you-meta'))")
        cdp.close()


@needs_chrome
def test_history_rise_on_first_paint_only(tmp_path, cdp):
    running = "document.getAnimations().filter(a => a.animationName === 'rise' && a.playState === 'running').length"
    with live_server(tmp_path, seed_team, stream_polls=200) as (base, _s, login):
        open_history(cdp, base, login, init_js=RECORD)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        assert cdp.js("document.querySelectorAll('.hist-main .hday.rise').length") == 2  # the entrance, once
        time.sleep(0.9)
        for ctl, ready, other_open in (
                ("document.querySelector('.ktab[data-kind=other]')", "document.querySelectorAll('.hday').length === 2", True),
                ("[...document.querySelectorAll('.ktab')][0]", "document.querySelectorAll('.hday').length === 2", False),
                ("document.querySelector('.vbtn[data-view=person]')", "document.querySelectorAll('.lane').length > 0", None),
                ("document.querySelector('.vbtn[data-view=day]')", "document.querySelectorAll('.hday').length === 2", False)):
            n = cdp.js("window.__hist.length")
            cdp.js(f"{ctl}.click()")
            cdp.wait(f"window.__hist.length === {n + 1} && {ready} && " + HIST_READY)
            time.sleep(0.1)  # a <details> toggle event is queued: let it run
            assert cdp.js("document.querySelectorAll('.hist-main .rise').length") == 0, ctl  # no replay
            assert cdp.js(running) == 0, ctl
            if other_open is not None:  # the Other tab opens "Other messages"; leaving it closes them again
                assert cdp.js("[...document.querySelectorAll('details.sec-other')].map(d => d.open)") == [other_open] * 2, ctl
        cdp.close()


def _selectors(css: str) -> list[str]:
    """Every selector of every rule (not @-rules, not keyframe steps), split at top-level commas."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*\s*\}", "", css)
    out = []
    for m in re.finditer(r"([^{};]+)\{", css):
        head = m.group(1).strip()
        if head.startswith("@"):
            continue
        depth, cur = 0, ""
        for ch in head:
            if ch == "," and depth == 0:
                out.append(cur.strip())
                cur = ""
                continue
            depth += (ch == "(") - (ch == ")")
            cur += ch
        out.append(cur.strip())
    return out


def test_history_css_is_scoped_to_the_page():
    """Every History rule sits under .hist, so none of it leaks into the other pages."""
    css = (STATIC_DIR / "app.css").read_text()
    sec = css[css.index("/* ---------- history (Task 9) */"):css.index("/* ---------- responsive */")]
    sels = _selectors(sec)
    loose = [x for x in sels if not re.match(r"\.hist(?![\w-])", x)]
    assert len(sels) > 100 and not loose, loose


# ---------------------------------------------------------------- Task 9 review 2: scroll kept, lane focus

@needs_chrome
def test_history_refresh_keeps_the_scroll_and_the_focus(tmp_path, cdp):
    """A quiet re-render gives the focus back without scrolling the page to it: he stays where he was reading."""
    def seed(store, settings):
        seed_team(store, settings)
        wed = _last_week(settings) + timedelta(days=2)
        for i in range(40):  # a tall week
            put(store, settings, wed, (11, i), f"Chatter line {i} " + "word " * 30, BOB, "other")

    def add(settings, at, who):  # on Monday, the last day card: below where he is reading
        st = Store(settings.db_path)
        try:
            put(st, settings, _last_week(settings), at, "A new message", who, "other")
        finally:
            st.close()
    with live_server(tmp_path, seed, stream_polls=200) as (base, settings, login):
        open_history(cdp, base, login, init_js=RECORD + FAST_REFRESH)
        cdp.wait("document.querySelectorAll('.hday').length === 2")
        cdp.js("document.querySelectorAll('details.sec-other').forEach(d => d.open = true)")
        mon = "document.querySelector('.hday[data-day=\"%s\"]')" % _last_week(settings).isoformat()
        first = "document.querySelector('.hist-main .lbl')"
        steps = [(first, (13, 0), BOB, "Other messages (2)"),  # an item's label in the content
                 ("document.querySelector('.pchip.all')", (13, 1), ("U0NEW", "Nia"), "Other messages (3)")]  # a chip, rebuilt
        for ctl, at, who, other in steps:
            cdp.js(f"{ctl}.focus()")
            cdp.js("window.scrollTo(0, 800)")
            time.sleep(0.2)
            assert cdp.js("window.scrollY") == 800
            add(settings, at, who)
            cdp.wait(f"{mon}.querySelector('details.sec-other summary').textContent.trim() === '{other}'")
            time.sleep(0.4)
            assert cdp.js("window.scrollY") == 800, ctl  # not pulled back up to the focused control
            assert cdp.js(f"document.activeElement === {ctl}"), ctl
        assert cdp.js("[...document.querySelectorAll('.pchip')].some(c => c.textContent.includes('Nia'))")
        # on a phone the week strip isn't sticky: a rebuilt week list must not pull the page up either
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 380, "height": 900, "deviceScaleFactor": 1, "mobile": True})
        wk = "document.querySelector('.wk[aria-current=\"true\"]')"
        cdp.js(f"{wk}.focus()")
        cdp.js("window.scrollTo(0, 2000)")
        time.sleep(0.2)
        y = cdp.js("window.scrollY")
        assert y > 1500
        add(settings, (13, 2), BOB)
        cdp.wait(f"{mon}.querySelector('details.sec-other summary').textContent.trim() === 'Other messages (4)'")
        time.sleep(0.4)
        assert cdp.js("window.scrollY") == y and cdp.js(f"document.activeElement === {wk}")
        cdp.close()


@needs_chrome
def test_history_lane_show_more_moves_the_focus_to_the_first_new_row(tmp_path, cdp):
    def seed(store, settings):
        mon = _last_week(settings)
        for i in range(30):
            put(store, settings, mon, (10, i), f"Bob's note {i}", BOB, "other")
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        open_history(cdp, base, login)
        cdp.wait("document.querySelectorAll('.hday').length === 1")
        cdp.js("document.querySelector('.vbtn[data-view=person]').click()")
        cdp.wait("!!document.querySelector('.lane-more') && " + HIST_READY)
        assert cdp.js("document.querySelectorAll('.lane-list > li').length") == 25
        assert cdp.js("document.querySelector('.lane-more').textContent") == "Show 5 more"
        cdp.js("document.querySelector('.lane-more').focus()")
        press(cdp, "Enter")
        cdp.wait("document.querySelectorAll('.lane-list > li').length === 30")
        # the button is gone: the focus goes to the first row it revealed, never to <body>
        assert cdp.js("document.activeElement === document.querySelectorAll('.lane-list > li')[25].querySelector('.lbl')")
        cdp.close()


# ---------------------------------------------------------------- Task 10: the Decks, Logs and Score pages

TASK10 = (("decks", "/* ---------- decks (Task 10) */", "/* ---------- logs (Task 10) */"),
          ("logs", "/* ---------- logs (Task 10) */", "/* ---------- score (Task 10) */"),
          ("score", "/* ---------- score (Task 10) */", "/* ---------- icons: monochrome masks"))


def test_task10_css_is_scoped_to_its_pages():
    """Every Decks, Logs and Score rule sits under its page's root (.decks, .logs, .score), like .hist, so none of it
    leaks into another page; and the Live page's log box no longer borrows the .logs name."""
    css = (STATIC_DIR / "app.css").read_text()
    for scope, start, end in TASK10:
        sels = _selectors(css[css.index(start):css.index(end)])
        loose = [x for x in sels if not re.match(r"\." + scope + r"(?![\w-])", x)]
        assert len(sels) > 25 and not loose, (scope, loose)
    before = _selectors(css[:css.index(TASK10[0][1])])
    assert not [x for x in before if re.search(r"\.(decks|logs|score)(?![\w-])", x)]
    for p in JS:
        if p.name != "logs.js":
            assert '"logs"' not in _code(p.read_text()).replace('"#/logs"', ""), p.name


def _surfaces(theme: str):
    """A theme's tokens, and its text surfaces as opaque colours: a card over the page, and a sunken tile in it."""
    css = (STATIC_DIR / "app.css").read_text()
    t = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", _token_block(css, f':root[data-theme="{theme}"]')))

    def over(top, under):
        rgb, a = _rgb(t[top])
        return tuple(round(a * x + (1 - a) * y) for x, y in zip(rgb, under))
    card = over("--panel", _rgb(t["--bg"])[0])
    return t, over, card, over("--sunken", card)


@pytest.mark.parametrize("theme", ["midnight", "daylight"])
def test_task10_text_colours_meet_aa(theme):
    """Every text colour the Decks, Logs and Score pages use reaches 4.5:1 (WCAG AA) where it sits: on a card, on a
    sunken tile, and on its own soft tint (a badge, a problem row, a delta)."""
    t, over, card, sunken = _surfaces(theme)
    for name in ("--text", "--text2", "--muted", "--a2-text", "--violet-text", "--pink-text", "--ok", "--warn",
                 "--bad"):
        fg = _rgb(t[name])[0]
        for where, bg in (("card", card), ("sunken", sunken), ("panel2", over("--panel2", card))):
            assert _contrast(fg, bg) >= 4.5, (theme, name, where)
    for name, tint in (("--ok", "--ok-soft"), ("--warn", "--warn-soft"), ("--bad", "--bad-soft"),
                       ("--a2-text", "--a2-soft"), ("--violet-text", "--a1-soft"), ("--text", "--a1-soft")):
        for base in (card, sunken):
            assert _contrast(_rgb(t[name])[0], over(tint, base)) >= 4.5, (theme, name, tint)
    # rows under the pointer: a plain log row or a table row on panel2, and a problem row on its own tint (it keeps
    # the tint on hover, with a ring instead of a fill), every colour a row's text uses; the level tag is outlined,
    # so its text sits on the row's tint too
    texts = ("--text", "--text2", "--muted", "--a2-text", "--violet-text", "--pink-text", "--ok", "--warn", "--bad")
    for where, bg in (("hover", over("--panel2", card)), ("this week's row", over("--a1-soft", card)),
                      ("warn row", over("--warn-soft", card)), ("bad row", over("--bad-soft", card))):
        for name in texts:
            assert _contrast(_rgb(t[name])[0], bg) >= 4.5, (theme, name, where)
    css = (STATIC_DIR / "app.css").read_text()
    assert ".logs .lrow:not(.lv-warn):not(.lv-bad):hover { background-color: var(--panel2); }" in css
    assert not re.search(r"\.logs \.lv-(warn|bad) \.lvl \{[^}]*background:", css)


def page_ready(name: str) -> str:
    return f"document.querySelector('#view > .{name}')?.dataset.ready === '1'"


def open_page(cdp, base, login, name, **kw) -> None:
    cdp.open(login_url(base, login), **kw)
    cdp.wait("!!document.querySelector('.page-live')")
    cdp.js(f"location.hash = '#/{name}'")
    cdp.wait(page_ready(name))


def tiny_png(w: int = 16, h: int = 9) -> bytes:
    """A real (tiny) PNG, so the page's <img> really decodes the thumbnail."""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes([124, 92, 255]) * w for _ in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def tiny_pdf(pages: int = 1) -> bytes:
    """A small but real PDF (catalog, page tree, xref): a viewer opens it and the API counts its pages."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{3 + i} 0 R' for i in range(pages))}] /Count {pages} >>"]
    objs += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] >>"] * pages
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def fake_sips(argv: list[str]) -> int:
    """Where sips would write the deck's first page, a real tiny PNG."""
    Path(argv[argv.index("--out") + 1]).write_bytes(tiny_png())
    return 0


def put_deck(store, settings, iid, title, *, status="posted", model="codex gpt-6.1-sol", attachable=False,
             attached=False, pages=3, day=None, dm_at=None) -> None:
    """An insight with a finished deck, as the deck job leaves it: a real PDF in the tmp decks dir, and its state.
    `attachable`: posted and its post's ts known, with no attach tried yet: what `attach` accepts (the API's
    `_attachable`). `attached`: the deck job's `attach:<id>` cursor says "done"."""
    from haseeb_agent.jobs.decks import content_hash
    from tests.test_insights import good_meta
    meta = good_meta(batch=iid)
    day = day or datetime.now(settings.tz).date()
    store.upsert_item(replace(_item(iid, "insight", title, json.dumps(meta), day, None), status=status))
    pdf = Path(settings.db_path).parent / "decks" / iid / f"{iid}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(tiny_pdf(pages))
    st = {"state": "done", "pdf": str(pdf), "model": model, "sha": content_hash(meta["insight"])}
    if dm_at is not None:
        st["dm_at"] = f"{dm_at.timestamp():.6f}"
    store.set_cursor(f"deck:{iid}", json.dumps(st))
    if attachable or attached:
        store.set_cursor(f"post_ts:{iid}", "1760000000.000100")
    if attached:
        store.set_cursor(f"attach:{iid}", "done")


D1, D2, D3, D4, D5 = "insight-t10-1", "insight-t10-2", "insight-t10-3", "insight-t10-4", "insight-t10-5"
DECK = "document.querySelector('.deck[data-item=\"{iid}\"]')"


def deck_btn(iid: str, label: str) -> str:
    return f"[...{DECK.format(iid=iid)}.querySelectorAll('.actions button')].find(b => b.textContent.trim() === '{label}')"


def seed_decks(store, settings) -> None:
    today = datetime.now(settings.tz).date()
    put_deck(store, settings, D1, "Gemini Argon's launch price ends Nov 30", attachable=True,
             model="codex gpt-6.1-sol (repaired)", pages=11)
    put_deck(store, settings, D2, "Voice agents for clinic booking", status="delivered", model="template (fact-check)",
             pages=6)
    put_deck(store, settings, D3, "Stablecoin payroll for remote teams", attached=True, pages=9,
             day=today - timedelta(days=1))


# window.open, recorded instead of opening a tab, and every blob: URL made and revoked (the page's own CSP keeps
# fetch() off blob: URLs, connect-src 'self', so a test reads the Blob as it was made); and every /api/logs request
OPEN_SPY = """(() => { window.__opened = []; window.__blobs = {}; window.__revoked = [];
  window.open = function (u) { window.__opened.push(String(u)); return {}; };
  const make = URL.createObjectURL.bind(URL), drop = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = function (b) { const u = make(b); window.__blobs[u] = {type: b.type, size: b.size}; return u; };
  URL.revokeObjectURL = function (u) { window.__revoked.push(String(u)); return drop(u); }; })();"""
IMG_LOADS = """new Promise(r => { const i = new Image(); i.onload = () => r('loaded'); i.onerror = () => r('gone');
  i.src = %s; })"""
LOGS_REC = """(() => { const f = window.fetch; window.__logs = [];
  window.fetch = function (u) { const url = String(u);
    if (url.startsWith('/api/logs')) window.__logs.push({url, t: performance.now()});
    return f.apply(this, arguments); }; })();"""
LOG_PARAMS = "window.__logs.map(r => Object.fromEntries(new URL(r.url, location.origin).searchParams))"
LOGS_IDLE = "document.querySelector('.logs .lg-list')?.getAttribute('aria-busy') === 'false'"
ROWS = "[...document.querySelectorAll('.logs .lrow .lmsg')].map(m => m.textContent)"


@needs_chrome
def test_decks_empty_state(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "decks")
        text = cdp.wait("document.querySelector('.decks-empty')?.textContent")
        assert "No decks yet" in text
        assert cdp.js("document.querySelectorAll('.deck, .load-error').length") == 0
        assert "Session expired" not in cdp.js("document.body.textContent")
        cdp.close()


@needs_chrome
def test_logs_empty_state_with_no_log_files_at_all(tmp_path, cdp):
    """The final review (M9): a Mac where the agent has never written a log (no files, not even the folder) shows the
    Logs page's empty state, not an error or a spinner."""
    import shutil
    with live_server(tmp_path, stream_polls=200) as (base, _s, login):
        shutil.rmtree(tmp_path / "logs")  # no log folder at all
        open_page(cdp, base, login, "logs", init_js=LOGS_REC)
        cdp.wait(LOGS_IDLE)
        assert cdp.wait("document.querySelector('.logs .lempty .lempty-t')?.textContent") == "No log lines yet"
        assert "appear here as they happen" in cdp.js("document.querySelector('.logs .lempty .lempty-s').textContent")
        assert cdp.js("document.querySelectorAll('.logs .lrow, .logs .load-error').length") == 0
        assert cdp.js("window.__logs.length") >= 1  # it did ask, and got an empty answer
        assert "Session expired" not in cdp.js("document.body.textContent")
        cdp.close()


@needs_chrome
def test_attach_button_only_when_attachable(tmp_path, cdp):
    def seed(store, settings):
        seed_decks(store, settings)
        put_deck(store, settings, D4, "A Codex cover on the template", model="codex")  # no fact check ran
        put_deck(store, settings, D5, "The template alone", model="template")
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "decks")
        cdp.wait("document.querySelectorAll('.deck').length === 5")
        tiles = cdp.js("""Object.fromEntries([...document.querySelectorAll('.deck')].map(d => [d.dataset.item, {
            attach: [...d.querySelectorAll('.actions button')].filter(b => b.textContent.trim() === 'Attach').length,
            badge: d.querySelector('.fc').textContent.trim(), stats: d.querySelector('.deck-stats').textContent,
            model: d.querySelector('.deck-model').textContent}]))""")
        assert {k: v["attach"] for k, v in tiles.items()} == {D1: 1, D2: 0, D3: 0, D4: 0, D5: 0}  # posted, ts known
        assert {k: v["badge"] for k, v in tiles.items()} == {D1: "Repaired", D2: "Template", D3: "Passed",
                                                              D4: "Designer", D5: "Template"}
        assert "11" in tiles[D1]["stats"] and tiles[D1]["stats"].count("—") == 2  # seconds and images are unknown
        assert tiles[D1]["model"] == "codex gpt-6.1-sol" and tiles[D2]["model"] == "template"
        cdp.close()


@needs_chrome
def test_attach_confirms_and_enqueues_exactly_one_command(tmp_path, cdp):
    with live_server(tmp_path, seed_decks, stream_polls=200) as (base, settings, login):
        open_page(cdp, base, login, "decks")
        tile = DECK.format(iid=D1)
        cdp.wait(f"!!({deck_btn(D1, 'Attach')})")
        cdp.js(f"{deck_btn(D1, 'Attach')}.click()")
        assert cdp.wait(f"{tile}.querySelector('.confirm-q')?.textContent") == "Attach this deck to your channel post?"
        assert cdp.wait("document.activeElement.textContent.trim() === 'Cancel'")  # Enter lands on Cancel
        cdp.js(f"{deck_btn(D1, 'Cancel')}.click()")
        cdp.wait(f"!!({deck_btn(D1, 'Attach')})")
        assert _commands(settings) == []  # nothing is queued until Confirm
        cdp.js(f"{deck_btn(D1, 'Attach')}.click()")
        cdp.wait(f"!!({deck_btn(D1, 'Confirm')})")
        cdp.js(f"{{ const b = {deck_btn(D1, 'Confirm')}; b.click(); b.click(); }}")  # a double click
        cdp.wait(f"{tile}.querySelector('.cstatus.s-queued')")
        assert cdp.js(f"{deck_btn(D1, 'Attach')}.disabled") is True
        cmds = _commands(settings)
        assert [(c.item_id, c.verb, c.state) for c in cmds] == [(D1, "attach", "queued")]  # exactly one
        cdp.reload()  # the server's `pending` keeps it disabled after a reload too
        cdp.wait(page_ready("decks") + f" && !!{tile}?.querySelector('.cstatus.s-queued')")
        assert cdp.js(f"{deck_btn(D1, 'Attach')}.disabled") is True
        cdp.js(f"{deck_btn(D1, 'Attach')}.click()")
        assert cdp.js(f"!{tile}.querySelector('.confirm-q')")
        st = Store(settings.db_path)
        try:
            st.set_page_command(cmds[0].id, "running")
            cdp.wait(f"{tile}.querySelector('.cstatus.s-running')")
            st.set_cursor(f"attach:{D1}", "done")
            st.set_page_command(cmds[0].id, "done", "✅ Deck attached to your post")
        finally:
            st.close()
        text = cdp.wait(f"{tile}?.querySelector('.cstatus.s-done')?.textContent")
        assert text.startswith("✅") and text.endswith("Deck attached to your post")
        cdp.wait(f"!({deck_btn(D1, 'Attach')})")  # attached: the server no longer offers it
        assert "Deck attached to your post" in cdp.js(f"{tile}.querySelector('.cstatus').textContent")
        assert len(_commands(settings)) == 1
        cdp.close()


@needs_chrome
def test_deck_thumb_is_a_blob_url_and_the_pdf_opens_from_one(tmp_path, cdp):
    with live_server(tmp_path, seed_decks, stream_polls=200, sips=fake_sips) as (base, _s, login):
        open_page(cdp, base, login, "decks", init_js=OPEN_SPY)
        cdp.wait("document.querySelectorAll('.deck img').length === 3 && "
                 "[...document.querySelectorAll('.deck img')].every(i => i.complete && i.naturalWidth === 16)")
        srcs = cdp.js("[...document.querySelectorAll('.deck img')].map(i => i.getAttribute('src'))")
        assert len(srcs) == 3 and all(s.startswith("blob:") for s in srcs)  # fetched with the header, never a link
        assert cdp.js("document.querySelectorAll('#view a').length") == 0
        cdp.js(f"{DECK.format(iid=D1)}.querySelector('.deck-thumb').click()")
        url = cdp.wait("window.__opened[0]")
        assert url.startswith("blob:")  # the PDF, fetched with the header, opened from a blob: URL
        assert cdp.js(f"window.__blobs[{json.dumps(url)}]") == {"type": "application/pdf", "size": len(tiny_pdf(11))}
        assert url not in cdp.js("window.__revoked")  # kept a while, so the new tab can read it
        assert cdp.js(IMG_LOADS % json.dumps(srcs[0])) == "loaded"
        cdp.js("location.hash = '#/live'")
        cdp.wait("!!document.querySelector('.page-live')")
        assert set(srcs) <= set(cdp.js("window.__revoked"))  # leaving the page revoked every thumbnail's URL...
        assert cdp.js(IMG_LOADS % json.dumps(srcs[0])) == "gone"  # ...and a revoked one really is gone
        cdp.close()


@needs_chrome
def test_deck_hostile_title_renders_as_text(tmp_path, cdp):
    hostile = f"{PWN} {HOSTILE_SCRIPT} {HOSTILE_BIDI}"

    def seed(store, settings):
        put_deck(store, settings, D1, hostile, attachable=True, model=f"codex {HOSTILE_IMG}")
    with live_server(tmp_path, seed, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "decks")
        cdp.wait("document.querySelectorAll('.deck').length === 1")
        s = cdp.js(VIEW_SAFE)
        assert (s["imgs"], s["scripts"], s["pwned"], s["tags"], s["links"]) == (0, 2, False, 0, 0)
        assert s["seen"] >= 1 and s["loose"] == 0 and not s["attrs"], (s["seen"], s["loose"], s["attrs"])
        assert cdp.js(f"{DECK.format(iid=D1)}.querySelector('.deck-t').textContent") == hostile
        assert f"codex {HOSTILE_IMG}" in s["text"]
        cdp.close()


def _tz():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        return _settings(Path(d)).tz


@needs_chrome
def test_logs_problem_filter(tmp_path, cdp):
    now = datetime.now(_tz())
    t = lambda s: now - timedelta(seconds=s)  # noqa: E731
    lines = [f"{t(50).isoformat()} process_dm ok 0 command(s) (1.0s)",
             f"{t(40).isoformat()} process_dm fail slack_read_channel error (7.7s)",
             f"{t(30).isoformat()} run_todo ok today's todo: none (0.0s)",
             f"{t(20).isoformat()} tick ok (0.4s)"]
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "tick.err.log").write_text(
        f"{t(35).strftime('%Y-%m-%d %H:%M:%S')},100 WARNING haseeb_agent.slack_gateway: slack unavailable: claude exit 1\n")
    with live_server(tmp_path, logs=lines, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "logs", init_js=LOGS_REC)
        cdp.wait("document.querySelectorAll('.logs .lrow').length === 5")
        rows = cdp.js("[...document.querySelectorAll('.logs .lrow')].map(r => [r.querySelector('.lsrc').textContent, r.className])")
        assert [s for s, _ in rows] == ["tick", "run_todo", "slack_gateway", "process_dm", "process_dm"]  # newest first
        assert "lv-info" in rows[0][1] and "lv-warn" in rows[2][1] and "lv-bad" in rows[3][1]  # amber and red
        assert cdp.js("[...document.querySelectorAll('.logs .lchip')].map(c => c.textContent.trim())") == \
            ["All", "Slack", "Models", "Decks", "Problems"]
        cdp.js("document.querySelector('.logs .lchip[data-f=problems]').click()")
        cdp.wait("document.querySelector('.logs .lchip[data-f=problems]').getAttribute('aria-pressed') === 'true' && "
                 "document.querySelectorAll('.logs .lrow').length === 2 && " + LOGS_IDLE)
        assert cdp.js(LOG_PARAMS)[-1] == {"filter": "problems"}
        assert cdp.js("[...document.querySelectorAll('.logs .lrow')].map(r => r.querySelector('.lsrc').textContent)") == \
            ["slack_gateway", "process_dm"]
        assert cdp.js("[...document.querySelectorAll('.logs .lrow')].every(r => /\\blv-(bad|warn)\\b/.test(r.className))")
        cdp.close()


@needs_chrome
def test_logs_search_debounce(tmp_path, cdp):
    now = datetime.now(_tz())
    lines = [f"{(now - timedelta(seconds=9)).isoformat()} process_dm read DM via slack ok",
             f"{(now - timedelta(seconds=8)).isoformat()} run_todo ok",
             f"{(now - timedelta(seconds=7)).isoformat()} slack_gateway reconnected"]
    with live_server(tmp_path, logs=lines, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "logs", init_js=LOGS_REC)
        cdp.wait("document.querySelectorAll('.logs .lrow').length === 3")
        n = cdp.js("window.__logs.length")
        cdp.js("""(() => { const i = document.querySelector('.logs .lsearch input'); i.focus();
          const type = (v) => { i.value = v; i.dispatchEvent(new Event('input', {bubbles: true})); };
          window.__typed = performance.now(); type('sl'); setTimeout(() => type('slac'), 60);
          setTimeout(() => type('slack'), 120); })()""")
        time.sleep(0.2)
        assert cdp.js("window.__logs.length") == n  # still typing: nothing sent yet
        cdp.wait("document.querySelectorAll('.logs .lrow').length === 2 && " + LOGS_IDLE)
        sent = cdp.js(f"window.__logs.slice({n}).map(r => [Object.fromEntries(new URL(r.url, location.origin)"
                      ".searchParams), r.t - window.__typed])")
        typed = [x for x in sent if "q" in x[0]]  # (a quiet 15 s refresh may also have run meanwhile, without q)
        assert typed and all(x[0] == {"filter": "all", "q": "slack"} for x in typed)  # only the last words went out
        assert typed[0][1] >= 120 + 240  # 250 ms after the last keystroke
        assert cdp.js(ROWS) == ["reconnected", "read DM via slack ok"]
        cdp.js("""(() => { const i = document.querySelector('.logs .lsearch input');
          i.value = 'nothing like this'; i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
        assert "No lines match" in cdp.wait("document.querySelector('.logs .lempty')?.textContent")
        cdp.close()


@needs_chrome
def test_logs_hostile_text_literal(tmp_path, cdp):
    now = datetime.now(_tz())
    src = "<svg/onload=window.__pwned=1>"
    lines = [f"{(now - timedelta(seconds=9)).isoformat()} tick {PWN} {HOSTILE_SCRIPT}",
             f"{(now - timedelta(seconds=5)).isoformat()} {src} fail {HOSTILE_BIDI} {MORE_HOSTILE[1]}"]
    with live_server(tmp_path, logs=lines, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "logs")
        cdp.wait("document.querySelectorAll('.logs .lrow').length === 2")
        s = cdp.js(VIEW_SAFE)
        assert (s["imgs"], s["scripts"], s["pwned"], s["tags"], s["links"]) == (0, 2, False, 0, 0)
        assert s["seen"] >= 1 and s["loose"] == 0 and not s["attrs"], (s["seen"], s["loose"], s["attrs"])
        assert f"{PWN} {HOSTILE_SCRIPT}" in s["text"] and src in s["text"] and MORE_HOSTILE[1] in s["text"]
        cdp.close()


@needs_chrome
def test_logs_new_lines_slide_in_and_the_dom_stays_bounded(tmp_path, cdp):
    start = datetime.now(_tz()) - timedelta(hours=1)
    line = lambda i: f"{(start + timedelta(seconds=i)).isoformat()} run_feedback ok line {i:03d}"  # noqa: E731
    with live_server(tmp_path, logs=[line(i) for i in range(250)], stream_polls=400) as (base, settings, login):
        open_page(cdp, base, login, "logs", init_js=LOGS_REC)
        cdp.wait("document.querySelectorAll('.logs .lrow').length === 200")
        shown = cdp.js(ROWS)
        assert shown[0] == "ok line 249" and shown[-1] == "ok line 050"  # the API's newest 200, newest first
        with open(tmp_path / "logs" / "tick.log", "a") as f:  # 200 new lines, then a step event on the stream
            f.write("".join(line(i) + "\n" for i in range(250, 450)))
        st = Store(settings.db_path)
        try:
            st.add_event("tick", "x", model=None, started=datetime.now(settings.tz), ended=None, ok=None, kind=None)
        finally:
            st.close()
        cdp.wait(f"({ROWS})[0] === 'ok line 449'")
        shown = cdp.js(ROWS)
        assert len(shown) == 300 and shown[-1] == "ok line 150" and len(set(shown)) == 300  # bounded, no repeats
        assert cdp.js("document.querySelectorAll('.logs .lrow.enter').length") > 0  # the new ones slid in
        # Load older pages back with before= (just past the oldest line shown), still within the bound
        cdp.js("document.querySelector('.logs .lmore').click()")
        cdp.wait(f"({ROWS}).includes('ok line 000') && " + LOGS_IDLE)
        p = [x for x in cdp.js(LOG_PARAMS) if "before" in x][-1]
        assert set(p) == {"filter", "before"}
        assert abs(datetime.fromisoformat(p["before"]) - (start + timedelta(seconds=150))) < timedelta(seconds=1)
        shown = cdp.js(ROWS)
        assert len(shown) == 300 and shown[0] == "ok line 299" and shown[-1] == "ok line 000" and len(set(shown)) == 300
        # the newest 150 left at the top: the page holds still, says so, and knows nothing older is left (a short page)
        assert "Back to newest" in cdp.js("document.querySelector('.logs .lnew:not([hidden])').textContent")
        assert cdp.js("!document.querySelector('.logs .lmore') && !!document.querySelector('.logs .lend')")
        cdp.js("document.querySelector('.logs .lnew button').click()")
        cdp.wait(f"({ROWS})[0] === 'ok line 449' && " + LOGS_IDLE)
        assert len(cdp.js(ROWS)) == 200 and cdp.js("document.querySelector('.logs .lnew').hidden") is True
        cdp.close()


@needs_chrome
def test_logs_new_lines_bring_load_older_back(tmp_path, cdp):
    """A short first page is the start of the log, until new lines push its oldest lines out at the bottom: then
    there is something older again, and Load older comes back."""
    start = datetime.now(_tz()) - timedelta(hours=1)
    line = lambda i: f"{(start + timedelta(seconds=i)).isoformat()} run_todo ok line {i:03d}"  # noqa: E731
    with live_server(tmp_path, logs=[line(i) for i in range(150)], stream_polls=400) as (base, settings, login):
        open_page(cdp, base, login, "logs")
        cdp.wait("document.querySelectorAll('.logs .lrow').length === 150 && !!document.querySelector('.logs .lend')")
        assert cdp.js("!document.querySelector('.logs .lmore')")  # fewer than a page: nothing older
        with open(tmp_path / "logs" / "tick.log", "a") as f:
            f.write("".join(line(i) + "\n" for i in range(150, 350)))
        st = Store(settings.db_path)
        try:
            st.add_event("tick", "x", model=None, started=datetime.now(settings.tz), ended=None, ok=None, kind=None)
        finally:
            st.close()
        cdp.wait(f"({ROWS})[0] === 'ok line 349'")
        shown = cdp.js(ROWS)
        assert len(shown) == 300 and shown[-1] == "ok line 050"  # the oldest 50 went out at the bottom...
        assert cdp.js("!!document.querySelector('.logs .lmore') && !document.querySelector('.logs .lend')")  # ...so
        cdp.js("document.querySelector('.logs .lmore').click()")
        cdp.wait(f"({ROWS}).includes('ok line 000') && " + LOGS_IDLE)
        cdp.close()


def seed_score(store, settings, notes: bool = True) -> None:
    """Eight weeks of the agent's work: insights posted on each week's first days (some edited first), a Next Todo
    each Monday (no other day has a huddle) and a few failed runs."""
    today = datetime.now(settings.tz).date()
    monday = today - timedelta(days=today.weekday())
    for w in range(8):
        mon = monday - timedelta(weeks=7 - w)
        for k in range(1 + w % 3):
            d = mon + timedelta(days=k)
            if d <= today:
                store.upsert_item(replace(_item(f"insight-s{w}-{k}", "insight", "t", "{}", d, None), status="posted",
                                          edited=(k == 0 and w % 2 == 0)))
        store.upsert_item(replace(_item(f"todo-s{w}", "todo", "Next Todo", "x", mon, None), status="posted",
                                  acted_at=datetime.combine(mon, dtime(15, 3), tzinfo=settings.tz)))
        if notes:  # the huddle notes came at 15:00, so 15:03 is within 5 minutes (D16)
            store.set_cursor(f"notes:todo-s{w}", f"{datetime.combine(mon, dtime(15, 0), tzinfo=settings.tz).timestamp():.6f}")
        for _ in range(w % 4):
            store._db.execute("INSERT INTO runs (job, ok, detail, at) VALUES ('tick', 0, 'x', ?)",
                              (datetime.combine(mon, dtime(9, 0), tzinfo=settings.tz).isoformat(),))
    store._db.commit()


def _pending(today) -> bool:
    """seed_score posts this week's insights on Monday and Tuesday: Wednesday to Friday, today's isn't out yet."""
    return 2 <= today.weekday() <= 4


def _score_delta(today) -> str:
    """seed_score's weekly-score change as the tile words it: last week was 1 posted day of 5 (5 − 40 = −35); this
    week posts Monday and Tuesday. A pending today is neither posted nor missed (M4), and a week still under way is
    compared by its pace, per settled workday."""
    workdays = min(today.weekday() + 1, 5) - _pending(today)
    days = min(2, today.weekday() + 1)
    now = 5 * days - 10 * (workdays - days)
    if workdays < 5:
        d = round((now / workdays - (-35) / 5) * 10) / 10
        return f"{'Up' if d > 0 else 'Down'} {abs(d):g} a day vs last week"
    return f"{'Up' if now > -35 else 'Down'} {abs(now + 35)} vs last week"


@needs_chrome
def test_score_sparkline_points(tmp_path, cdp):
    with live_server(tmp_path, seed_score, stream_polls=200) as (base, settings, login):
        open_page(cdp, base, login, "score")
        tiles = cdp.js("""[...document.querySelectorAll('.score .stat')].map(t => ({k: t.dataset.k,
            pts: (t.querySelector('svg.spark path.spark-line').getAttribute('d').match(/[ML]/g) || []).length,
            dot: t.querySelectorAll('svg.spark .spark-dot').length,
            label: t.querySelector('svg.spark').getAttribute('aria-label'), v: t.querySelector('.stat-v').textContent,
            d: t.querySelector('.stat-d').className}))""")
        assert [t["k"] for t in tiles] == ["score", "intervention", "todo", "failed"]
        for t in tiles:
            assert t["pts"] == 8 and t["dot"] == 1, t  # 8 weeks, 8 points; a dot on the last one
            assert len(re.findall(r"−?\d+", t["label"].split(":", 1)[1])) >= 8, t  # every week's number, read out
        failed = tiles[3]
        assert failed["v"] == "3" and "up" in failed["d"] and "bad" in failed["d"]  # 2 → 3 failed runs: worse
        # a partial week isn't measured against a whole one: a Monday's +5 is a pace, not "20 behind"
        assert cdp.js("document.querySelector('.stat[data-k=score] .stat-d').textContent") == \
            _score_delta(datetime.now(settings.tz).date())
        assert cdp.js("document.querySelectorAll('.score .wk-table tbody tr').length") == 8
        assert cdp.js("document.querySelector('.score .wk-table tbody tr').textContent").startswith("This week")
        # D16: on time is within 5 minutes of the huddle notes, and the tile and the table say so
        assert cdp.js("document.querySelector('.stat[data-k=todo] .stat-h').textContent") == "Next Todo within 5 min"
        assert "Next Todo within 5 min" in cdp.js("document.querySelector('.score .wk-table thead').textContent")
        # M4: a pending today is neither posted nor missed, and the tile says so
        today = datetime.now(settings.tz).date()
        assert ("Today pending" in cdp.js("document.querySelector('.stat[data-k=score]').textContent")) == _pending(today)
        cdp.close()


@needs_chrome
def test_score_todo_tile_says_by_1605_without_the_notes_time(tmp_path, cdp):
    """D16's fallback: with no huddle notes time recorded, a Next Todo is on time by 16:05, and the labels say so."""
    with live_server(tmp_path, lambda st, se: seed_score(st, se, notes=False), stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "score")
        assert cdp.js("document.querySelector('.stat[data-k=todo] .stat-h').textContent") == "Next Todo by 16:05"
        assert "Next Todo by 16:05" in cdp.js("document.querySelector('.score .wk-table thead').textContent")
        assert cdp.js("document.querySelector('.stat[data-k=todo] .stat-v').textContent") == "100%"  # 15:03 ≤ 16:05
        cdp.close()


@needs_chrome
def test_score_empty_state(tmp_path, cdp):
    with live_server(tmp_path, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "score")
        text = cdp.wait("document.querySelector('.score-empty')?.textContent")
        assert "No score yet" in text
        assert cdp.js("document.querySelectorAll('.score .stat, .score svg, .score table, .load-error').length") == 0
        cdp.close()


@needs_chrome
def test_task10_pages_have_no_horizontal_scroll_at_380(tmp_path, cdp):
    now = datetime.now(_tz())

    def seed(store, settings):
        seed_decks(store, settings)
        seed_score(store, settings)
        put_deck(store, settings, "insight-t10-long", "W" * 300, model="codex " + "m" * 200)
    lines = [f"{(now - timedelta(seconds=i)).isoformat()} {'s' * 120} {'x' * 600} fail" for i in range(5)]
    with live_server(tmp_path, seed, logs=lines, stream_polls=200, sips=fake_sips) as (base, _s, login):
        open_page(cdp, base, login, "decks", width=380)
        for name in ("decks", "logs", "score"):
            cdp.js(f"location.hash = '#/{name}'")
            cdp.wait(page_ready(name))
            time.sleep(0.8)  # the entrance settles
            assert cdp.js("document.documentElement.scrollWidth") <= 380, name
            assert cdp.js("[...document.querySelectorAll('#view .card, #view .deck, #view .lrow, #view .stat, "
                          "#view .lchip, #view .actions')].every(c => c.getBoundingClientRect().right <= 380.5)"), name
        cdp.close()


@needs_chrome
def test_task10_pages_respect_reduced_motion(tmp_path, cdp):
    with live_server(tmp_path, seed_decks, stream_polls=200) as (base, _s, login):
        open_page(cdp, base, login, "decks", reduced=True)
        names = cdp.js("[...document.querySelectorAll('#view *')].map(e => getComputedStyle(e).animationName)")
        assert names and set(names) == {"none"}, set(names)
        cdp.close()
