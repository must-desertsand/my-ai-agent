"""Task 6 (ruling D10): the dashboard's lock. TestClient and direct ASGI calls only: no real server, launchctl or browser."""
import asyncio
import hashlib
import os
import plistlib
import re
import stat
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from haseeb_agent import cli
from haseeb_agent.config import load_settings
from haseeb_agent.dashboard import app as dash
from haseeb_agent.dashboard import security
from haseeb_agent.dashboard.app import create_app, kick_tick, serve_config
from haseeb_agent.dashboard.security import (COOKIE, CSP, KeyFileError, consume_nonce, load_or_create_key,
                                             mint_nonce, rotate_key)
from haseeb_agent.offline import OfflineError

ROOT = Path(__file__).resolve().parent.parent
KEY = "ab" * 32
BASE = "http://127.0.0.1:8765"
HOUR, DAY = 3600, 86400


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LITELLM_API_KEY=k\n")
    return replace(load_settings(ROOT / "tests" / "fixtures" / "config.toml", env), db_path=str(tmp_path / "agent.sqlite"))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def login_dir(tmp_path):
    return tmp_path / "home" / "dashboard"


@pytest.fixture
def hits():
    return []


@pytest.fixture
def app(settings, clock, login_dir, hits):
    a = create_app(settings, lambda: None, KEY, lambda: None, clock=clock, login_dir=login_dir)

    async def handler(request):
        hits.append(request.method)
        return PlainTextResponse("ok")

    async def evil(request):
        return Response("x", headers={"Content-Security-Policy": "default-src *", "Access-Control-Allow-Origin": "*",
                                      "Referrer-Policy": "unsafe-url"})
    a.router.routes.insert(0, Route("/api/x", handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]))
    a.router.routes.insert(0, Route("/mut", handler, methods=["POST", "PUT", "DELETE", "PATCH"]))
    a.router.routes.insert(0, Route("/evil", evil))
    return a


def new_client(app):
    return TestClient(app, base_url=BASE, follow_redirects=False)


@pytest.fixture
def anon(app):
    return new_client(app)


def open_login(c, nonce, **headers):
    return c.get(f"/?login={nonce}", headers=headers)


def exchange(c, code, origin=BASE, **headers):
    h = {"Origin": origin, **headers} if origin else dict(headers)
    return c.post("/auth/exchange", json={"code": code}, headers=h)


def do_login(app, login_dir, clock, client=None):
    """A browser: mint a nonce (as `dashboard open` does), open the login URL, exchange the fragment code.
    Returns (client, API token)."""
    c = client or new_client(app)
    nonce = mint_nonce(login_dir, clock())
    r = open_login(c, nonce)
    assert r.status_code == 303, r.text
    m = re.fullmatch(r"/#c=([0-9a-f]{64})", r.headers["location"])
    assert m, r.headers["location"]
    r = exchange(c, m.group(1))
    assert r.status_code == 200, r.text
    return c, r.json()["token"]


@pytest.fixture
def session(app, login_dir, clock):
    return do_login(app, login_dir, clock)


@pytest.fixture
def client(session):
    return session[0]


@pytest.fixture
def token(session):
    return session[1]


def origin_headers(token=None, origin=BASE, **extra):
    h = dict(extra)
    if origin is not None:
        h["Origin"] = origin
    if token is not None:
        h["X-Session-Token"] = token
    return h


def asgi(app, scope_type="http", path="/", method="GET", headers=(), messages=()):
    sent = []
    queue = list(messages) or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if queue:
            return queue.pop(0)
        await asyncio.sleep(0.2)
        return {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)
    scope = {"type": scope_type, "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": list(headers), "server": ("127.0.0.1", 8765), "scheme": "http", "http_version": "1.1",
             "state": {}}
    if scope_type == "lifespan":
        scope = {"type": "lifespan", "state": {}}

    async def run():
        try:
            await asyncio.wait_for(app(scope, receive, send), 1)
        except asyncio.TimeoutError:
            pass
    asyncio.run(run())
    return sent


# ---------------------------------------------------------------- login: cookie, 401s

def test_no_cookie_401(anon):
    assert anon.get("/").status_code == 401
    assert anon.get("/app.js").status_code == 401
    assert anon.get("/api/x").status_code == 401


def test_old_key_login_is_gone(anon):
    r = anon.get(f"/?key={KEY}")
    assert r.status_code == 401
    assert "set-cookie" not in r.headers
    assert anon.get("/").status_code == 401


def test_login_sets_cookie_and_redirects_to_root_with_code_fragment(app, login_dir, clock):
    c = new_client(app)
    nonce = mint_nonce(login_dir, clock())
    r = c.get(f"/?login={nonce}")
    assert r.status_code == 303
    assert re.fullmatch(r"/#c=[0-9a-f]{64}", r.headers["location"])
    sc = r.headers["set-cookie"].lower()
    assert f"{COOKIE}=" in sc and "httponly" in sc and "samesite=strict" in sc and "path=/" in sc
    assert f"max-age={7 * DAY}" in sc
    assert KEY not in r.headers["set-cookie"] and nonce not in r.headers["set-cookie"]
    assert c.get("/").status_code == 200


def test_login_redirect_is_never_request_derived(app, login_dir, clock):
    for path in ["//evil.com/x", "/%2F%2Fevil.com", "/a%3Fb", "/x%0d%0aSet-Cookie:%20a=b", "/api/x", "/index.html"]:
        c = new_client(app)
        nonce = mint_nonce(login_dir, clock())
        r = c.get(f"{path}?login={nonce}&next=//evil.com")
        assert r.status_code == 303, path
        assert re.fullmatch(r"/#c=[0-9a-f]{64}", r.headers["location"]), (path, r.headers["location"])


def test_stale_login_with_a_live_cookie_just_redirects_home(app, client, token, login_dir, clock):
    r = client.get("/?login=" + "0" * 64)
    assert r.status_code == 303 and r.headers["location"] == "/" and "set-cookie" not in r.headers
    assert client.get("/api/x", headers={"X-Session-Token": token}).status_code == 200


def test_valid_nonce_with_a_live_cookie_always_makes_a_fresh_session(app, client, token, login_dir, clock):
    old_cookie = client.cookies.get(COOKIE)
    nonce = mint_nonce(login_dir, clock())
    r = client.get(f"/?login={nonce}")
    assert r.status_code == 303 and re.fullmatch(r"/#c=[0-9a-f]{64}", r.headers["location"])
    assert "set-cookie" in r.headers and client.cookies.get(COOKIE) != old_cookie
    new_token = exchange(client, r.headers["location"][4:]).json()["token"]
    assert new_token != token
    assert client.get("/api/x", headers={"X-Session-Token": new_token}).status_code == 200
    assert len(app.state.sessions) == 1  # the old session was dropped, not piled up


def test_reopening_gives_the_new_tab_a_working_token(app, login_dir, clock):
    c, first = do_login(app, login_dir, clock)
    c, second = do_login(app, login_dir, clock, client=c)  # `dashboard open` again, same browser
    assert c.get("/api/x", headers={"X-Session-Token": second}).status_code == 200
    assert c.get("/api/x", headers={"X-Session-Token": first}).status_code == 403


def test_wrong_and_missing_nonce_401_no_cookie_and_never_spend_the_real_one(app, anon, login_dir, clock):
    nonce = mint_nonce(login_dir, clock())
    for q in ["/?login=" + "0" * 64, "/?login=", "/?login=%FF", "/?login=%C3%A9", "/?login=x&login=y"]:
        r = anon.get(q)
        assert r.status_code == 401, q
        assert "set-cookie" not in r.headers
    assert new_client(app).get(f"/?login={nonce}").status_code == 303


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "no-cors"},
    {"Sec-Fetch-Mode": "cors"},
])
def test_login_needs_a_top_level_navigation(app, login_dir, clock, headers):
    nonce = mint_nonce(login_dir, clock())
    c = new_client(app)
    for guess in ("x", nonce):  # even the right nonce is refused, and left unspent
        r = open_login(c, guess, **headers)
        assert r.status_code == 403 and "set-cookie" not in r.headers
    assert new_client(app).get(f"/?login={nonce}",
                               headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}).status_code == 303


def test_login_refused_navigation_check_also_applies_with_a_live_cookie(client):
    r = client.get("/?login=x", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_race_with_a_newer_open_logs_nobody_in_with_the_old_nonce(login_dir, clock, monkeypatch):
    old = mint_nonce(login_dir, clock())
    real_rename, new = os.rename, []

    def rename_after_a_newer_open(src, dst):
        new.append(mint_nonce(login_dir, clock()))  # `dashboard open` lands between our read and our rename
        real_rename(src, dst)
    monkeypatch.setattr(security.os, "rename", rename_after_a_newer_open)
    assert consume_nonce(login_dir, old, clock()) is False
    monkeypatch.setattr(security.os, "rename", real_rename)
    assert consume_nonce(login_dir, old, clock()) is False
    assert consume_nonce(login_dir, new[0], clock()) is True  # the newer nonce was put back and still works


def test_nonce_is_single_use(app, login_dir, clock):
    nonce = mint_nonce(login_dir, clock())
    assert new_client(app).get(f"/?login={nonce}").status_code == 303
    assert new_client(app).get(f"/?login={nonce}").status_code == 401


def test_nonce_expires_after_60_seconds(app, login_dir, clock):
    nonce = mint_nonce(login_dir, clock())
    clock.t += 61
    assert new_client(app).get(f"/?login={nonce}").status_code == 401
    nonce = mint_nonce(login_dir, clock())
    clock.t += 59
    assert new_client(app).get(f"/?login={nonce}").status_code == 303


def test_newer_nonce_replaces_older(app, login_dir, clock):
    first = mint_nonce(login_dir, clock())
    second = mint_nonce(login_dir, clock())
    assert first != second
    assert new_client(app).get(f"/?login={first}").status_code == 401


def test_nonce_stored_hashed_in_a_private_dir(login_dir, clock):
    nonce = mint_nonce(login_dir, clock())
    assert re.fullmatch(r"[0-9a-f]{64}", nonce)
    assert stat.S_IMODE(login_dir.stat().st_mode) == 0o700
    f = login_dir / "login"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    text = f.read_text()
    assert hashlib.sha256(nonce.encode()).hexdigest() in text and nonce not in text
    assert [p.name for p in login_dir.iterdir()] == ["login"]  # no temp file left behind


def test_consume_nonce_race_only_one_winner(login_dir, clock):
    nonce = mint_nonce(login_dir, clock())
    results = []
    threads = [threading.Thread(target=lambda: results.append(consume_nonce(login_dir, nonce, clock())))
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_nonce_dir_chmodded_if_it_exists(tmp_path, clock):
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    mint_nonce(d, clock())
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_nonce_symlink_at_login_path_is_not_followed(tmp_path, clock):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("precious")
    (d / "login").symlink_to(target)
    nonce = mint_nonce(d, clock())  # replaces the link itself, never writes through it
    assert target.read_text() == "precious"
    assert not (d / "login").is_symlink()
    assert consume_nonce(d, nonce, clock())


# ---------------------------------------------------------------- one-time code exchange

def login_code(app, login_dir, clock, client=None):
    c = client or new_client(app)
    r = open_login(c, mint_nonce(login_dir, clock()))
    return c, r.headers["location"][4:]


def test_token_never_appears_in_a_url(app, login_dir, clock):
    c, code = login_code(app, login_dir, clock)
    token = exchange(c, code).json()["token"]
    assert token != code and token not in code


def test_exchange_code_is_single_use(app, login_dir, clock):
    c, code = login_code(app, login_dir, clock)
    assert exchange(c, code).status_code == 200
    assert exchange(c, code).status_code == 403


def test_exchange_code_expires_after_60_seconds(app, login_dir, clock):
    c, code = login_code(app, login_dir, clock)
    clock.t += 61
    assert exchange(c, code).status_code == 403
    c, code = login_code(app, login_dir, clock)
    clock.t += 59
    assert exchange(c, code).status_code == 200


def test_exchange_code_is_bound_to_its_session(app, login_dir, clock):
    c1, code1 = login_code(app, login_dir, clock)
    c2, code2 = login_code(app, login_dir, clock)
    assert exchange(c2, code1).status_code == 403
    assert exchange(c1, code2).status_code == 403
    assert exchange(c1, code1).status_code == 200 and exchange(c2, code2).status_code == 200


def test_exchange_without_cookie_401(app, login_dir, clock):
    c, code = login_code(app, login_dir, clock)
    assert exchange(new_client(app), code).status_code == 401


def test_exchange_checks_origin_and_sec_fetch_site_but_needs_no_token(app, login_dir, clock):
    c, code = login_code(app, login_dir, clock)
    assert exchange(c, code, origin="http://evil.com").status_code == 403
    assert exchange(c, code, origin=None).status_code == 403
    assert exchange(c, code, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert exchange(c, code, **{"Sec-Fetch-Site": "same-origin"}).status_code == 200  # no X-Session-Token sent


def test_exchange_rejects_junk_bodies_and_wrong_methods(app, login_dir, clock):
    c, code = login_code(app, login_dir, clock)
    h = {"Origin": BASE}
    assert c.post("/auth/exchange", content=b"not json", headers=h).status_code == 400
    assert c.post("/auth/exchange", json={"code": 5}, headers=h).status_code == 400
    assert c.post("/auth/exchange", json={}, headers=h).status_code == 400
    assert c.get("/auth/exchange").status_code == 404  # POST only: a GET falls through to static
    assert c.put("/auth/exchange", json={"code": code}, headers={"Origin": BASE}).status_code == 403  # token needed
    assert exchange(c, code).status_code == 200


def test_dropped_session_takes_its_codes_with_it(app, login_dir, clock):
    c, code = login_code(app, login_dir, clock)
    clock.t += 8 * DAY
    other = do_login(app, login_dir, clock)[0]
    assert other and exchange(c, code).status_code == 401


# ---------------------------------------------------------------- sessions

def test_session_idle_expiry_and_activity_extends(app, login_dir, clock):
    c, token = do_login(app, login_dir, clock)
    clock.t += 11 * HOUR
    assert c.get("/").status_code == 200        # activity resets the idle timer
    clock.t += 11 * HOUR
    assert c.get("/").status_code == 200
    clock.t += 12 * HOUR + 1
    assert c.get("/").status_code == 401        # idle > 12 h
    assert c.get("/api/x", headers={"X-Session-Token": token}).status_code == 401


def test_session_absolute_expiry(app, login_dir, clock):
    c, _ = do_login(app, login_dir, clock)
    for _ in range(15):                         # 15 x 11 h = 6.9 d, always active
        clock.t += 11 * HOUR
        assert c.get("/").status_code == 200
    clock.t += 11 * HOUR                        # 7.0+ d since login
    assert c.get("/").status_code == 401


def test_session_cap_16_evicts_the_oldest(app, login_dir, clock):
    clients = []
    for _ in range(17):
        clients.append(do_login(app, login_dir, clock)[0])
        clock.t += 1
    assert clients[0].get("/").status_code == 401
    assert all(c.get("/").status_code == 200 for c in clients[1:])
    assert len(app.state.sessions) <= 16


def test_expired_sessions_are_pruned(app, login_dir, clock):
    for _ in range(3):
        do_login(app, login_dir, clock)
    clock.t += 8 * DAY
    do_login(app, login_dir, clock)
    assert len(app.state.sessions) == 1


def test_forged_cookie_401(anon):
    anon.cookies.set(COOKIE, "forged")
    assert anon.get("/").status_code == 401


def test_each_session_has_its_own_token(app, login_dir, clock):
    (c1, t1), (c2, t2) = do_login(app, login_dir, clock), do_login(app, login_dir, clock)
    assert t1 != t2
    assert c1.get("/api/x", headers={"X-Session-Token": t2}).status_code == 403


# ---------------------------------------------------------------- /api token, Origin, Sec-Fetch-Site

def test_api_needs_cookie_and_token_on_every_method(client, token, hits):
    assert client.get("/api/x").status_code == 403
    assert client.get("/api/x", headers={"X-Session-Token": "0" * 64}).status_code == 403
    assert client.get("/api/x", headers={"X-Session-Token": token}).status_code == 200
    assert client.head("/api/x").status_code == 403
    assert client.options("/api/x").status_code == 403
    assert hits == ["GET"]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_every_unsafe_method_checks_origin_and_token(client, token, hits, method):
    for path in ("/api/x", "/mut"):
        for headers in (
            origin_headers(token, origin="http://evil.com"),
            origin_headers(token, origin="http://127.0.0.1:9999"),
            origin_headers(token, origin=None),            # missing Origin
            origin_headers(token, origin="null"),
            origin_headers(None),                           # no token
            origin_headers("0" * 64),                       # wrong token
            origin_headers(token, **{"Sec-Fetch-Site": "cross-site"}),
            origin_headers(token, **{"Sec-Fetch-Site": "same-site"}),
        ):
            r = client.request(method, path, headers=headers)
            assert r.status_code == 403, (method, path, headers)
    assert hits == []
    ok = client.request(method, "/mut", headers=origin_headers(token))
    assert ok.status_code == 200
    ok = client.request(method, "/mut", headers=origin_headers(token, origin="http://localhost:8765",
                                                               **{"Sec-Fetch-Site": "same-origin"}))
    assert ok.status_code == 200
    assert hits == [method, method]


def test_post_without_cookie_401(anon, token):
    assert anon.post("/api/x", headers=origin_headers(token)).status_code == 401


def test_key_query_never_authenticates_a_post(anon):
    assert anon.post(f"/api/x?key={KEY}", headers=origin_headers()).status_code == 401


# ---------------------------------------------------------------- Host

@pytest.mark.parametrize("host,status", [
    ("127.0.0.1:8765", 200), ("localhost:8765", 200), ("LOCALHOST:8765", 200),
    ("localhost.:8765", 421), ("127.0.0.1.:8765", 421), ("[::1]:8765", 421), ("[::1]", 421),
    ("127.0.0.1", 421), ("localhost", 421), ("127.0.0.1:9999", 421), ("127.0.0.1:08765", 421),
    ("2130706433:8765", 421), ("evil.com:8765", 421), ("evil.com", 421), ("127.0.0.1:8765.evil.com", 421),
])
def test_host_variants(client, host, status):
    assert client.get("/", headers={"Host": host}).status_code == status


def test_missing_host_421(app):
    sent = asgi(app)
    assert sent[0]["status"] == 421


def test_host_is_checked_before_login(app, login_dir, clock):
    nonce = mint_nonce(login_dir, clock())
    r = new_client(app).get(f"/?login={nonce}", headers={"Host": "evil.com:8765"})
    assert r.status_code == 421 and "set-cookie" not in r.headers
    assert new_client(app).get(f"/?login={nonce}").status_code == 303  # the nonce was not spent


# ---------------------------------------------------------------- headers

EXPECTED = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
}


def check_headers(r):
    for k, v in EXPECTED.items():
        assert r.headers.get_list(k) == [v], (k, r.headers.get_list(k))
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


def test_csp_value_is_verbatim():
    assert CSP == ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
                   "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def test_headers_on_every_response(app, anon, client, token, login_dir, clock):
    nonce = mint_nonce(login_dir, clock())
    responses = {
        401: anon.get("/"),
        303: new_client(app).get(f"/?login={nonce}"),
        421: anon.get("/", headers={"Host": "evil.com"}),
        200: client.get("/"),
        404: client.get("/nope.js"),
        403: client.post("/api/x", headers=origin_headers()),
    }
    for status, r in responses.items():
        assert r.status_code == status
        check_headers(r)
    check_headers(client.get("/app.js"))  # a static 200


def test_inner_headers_cannot_override_ours(client):
    r = client.get("/evil")
    assert r.status_code == 200
    check_headers(r)


def test_no_cors_headers(anon, client):
    for c in (anon, client):
        r = c.options("/", headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "POST"})
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]


def test_debug_is_off(app):
    assert app.debug is False


# ---------------------------------------------------------------- ASGI scopes

def test_websocket_refused(app):
    sent = asgi(app, scope_type="websocket", path="/ws",
                headers=[(b"host", b"127.0.0.1:8765")], messages=[{"type": "websocket.connect"}])
    assert sent and sent[0]["type"] == "websocket.close"
    assert not any(m["type"] == "websocket.accept" for m in sent)


def test_lifespan_passes_through(app):
    sent = asgi(app, scope_type="lifespan", messages=[{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# ---------------------------------------------------------------- static

def test_index_is_served_verbatim_with_no_substitution(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.text == (ROOT / "src/haseeb_agent/dashboard/static/index.html").read_text()
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/index.html").text == r.text


def test_shipped_page_and_login_js():
    """The shipped page (Task 8 replaced Task 6's placeholder): verbatim, external scripts only, and the page JS
    trades the #c= code for the token, keeps it in sessionStorage and strips the fragment."""
    static = ROOT / "src/haseeb_agent/dashboard/static"
    html = (static / "index.html").read_text()
    assert "{{" not in html and "csrf" not in html.lower()
    assert '<script type="module" src="/app.js"></script>' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)  # no inline script
    assert "onclick" not in html.lower() and "style=" not in html.lower()
    js = "\n".join(p.read_text() for p in sorted(static.glob("*.js")))
    assert "sessionStorage" in js and "#c=" in js and "/auth/exchange" in js and "replaceState" in js
    assert "#t=" not in js


def test_static_files_served_and_path_safety(client, tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "sub").mkdir(parents=True)
    (static / "index.html").write_text("<p>INDEX {{csrf}}</p>")
    (static / "app.js").write_text("// {{csrf}}")
    (static / "sub" / "a.css").write_text("a{}")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (static / "link.txt").symlink_to(secret)
    (static / "linkdir").symlink_to(tmp_path)
    monkeypatch.setattr(dash, "STATIC_DIR", static)
    r = client.get("/app.js")
    assert r.status_code == 200 and r.text == "// {{csrf}}"
    assert "javascript" in r.headers["content-type"]
    assert client.get("/").text == "<p>INDEX {{csrf}}</p>"  # verbatim, no substitution
    assert client.get("/sub/a.css").status_code == 200
    for p in ["/link.txt", "/linkdir/secret.txt", "/../secret.txt", "/%2e%2e/secret.txt",
              "/sub/%2e%2e/%2e%2e/secret.txt", "//etc/passwd", "/%2fetc/passwd", "/sub/", "/sub", "/..%5csecret.txt",
              "/INDEX.HTML", "/Index.Html", "/sub/..%2fsecret.txt", "/app.js%00"]:
        assert client.get(p).status_code == 404, p
    assert client.get("/index.html").status_code == 200  # the one explicit route


# ---------------------------------------------------------------- key file

def test_key_file_0600_in_a_0700_dir(tmp_path):
    path = tmp_path / "d" / "dashboard.key"
    key = load_or_create_key(path)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["dashboard.key"]
    assert load_or_create_key(path) == key
    path.chmod(0o644)
    assert load_or_create_key(path) == key and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_key_dir_is_chmodded(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o755)
    load_or_create_key(d / "dashboard.key")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_key_race_everyone_gets_the_same_key(tmp_path):
    path = tmp_path / "d" / "dashboard.key"
    keys = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        keys.append(load_or_create_key(path))
    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(keys) == 8 and len(set(keys)) == 1
    assert path.read_text().strip() == keys[0]


def test_symlink_at_key_path_refused(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    target = tmp_path / "target"
    target.write_text("precious")
    (d / "dashboard.key").symlink_to(target)
    with pytest.raises(KeyFileError):
        load_or_create_key(d / "dashboard.key")
    assert target.read_text() == "precious" and (d / "dashboard.key").is_symlink()


def test_young_junk_is_kept_old_junk_is_replaced(tmp_path):
    path = tmp_path / "dashboard.key"
    path.write_text("junk")
    with pytest.raises(KeyFileError):
        load_or_create_key(path)
    assert path.read_text() == "junk"
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))
    key = load_or_create_key(path)
    assert re.fullmatch(r"[0-9a-f]{64}", key) and path.read_text().strip() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rotate_key_replaces_atomically(tmp_path):
    path = tmp_path / "d" / "dashboard.key"
    old = load_or_create_key(path)
    new = rotate_key(path)
    assert new != old and load_or_create_key(path) == new
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["dashboard.key"]


# ---------------------------------------------------------------- serve, CLI, launchd

def test_binds_localhost_only(app, settings):
    cfg = serve_config(app, settings.dashboard_port)
    assert cfg.host == "127.0.0.1" and cfg.port == 8765
    assert cfg.access_log is False


def test_default_port_in_config(settings):
    assert settings.dashboard_port == 8765


def test_kick_failure_swallowed(monkeypatch):
    monkeypatch.delenv("HASEEB_AGENT_OFFLINE")
    seen = []

    def boom(cmd, **kw):
        seen.append((cmd, kw))
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(dash.subprocess, "run", boom)
    kick_tick()
    assert seen[0][0] == ["launchctl", "kickstart", f"gui/{os.getuid()}/com.haseeb.agent.tick"]
    assert seen[0][1]["timeout"] == 5
    monkeypatch.setattr(dash.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    kick_tick()


def test_kick_returns_launchctls_exit_code(monkeypatch):
    """M3: the Check round control needs to know whether the tick was kicked (113: not loaded in launchd)."""
    monkeypatch.delenv("HASEEB_AGENT_OFFLINE")
    for rc in (0, 113, 1):
        monkeypatch.setattr(dash.subprocess, "run",
                            lambda cmd, rc=rc, **kw: subprocess.CompletedProcess(cmd, rc, stdout=b"", stderr=b"x"))
        assert kick_tick() == rc

    def no_launchctl(cmd, **kw):
        raise FileNotFoundError("launchctl")

    def hangs(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)
    for broken in (no_launchctl, hangs):
        monkeypatch.setattr(dash.subprocess, "run", broken)
        assert kick_tick() == 1  # launchctl could not be run at all: never an exception, never a pass


def test_kick_is_guarded_in_tests(monkeypatch):
    monkeypatch.setattr(dash.subprocess, "run", lambda *a, **k: pytest.fail("real launchctl"))
    with pytest.raises(OfflineError):
        kick_tick()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "KEY_PATH", tmp_path / "h" / "dashboard.key")
    monkeypatch.setattr(dash, "LOGIN_DIR", tmp_path / "h" / "dashboard")
    return tmp_path / "h"


def test_dashboard_open_prints_only_the_login_url(settings, home, monkeypatch, capsys):
    key = load_or_create_key(dash.KEY_PATH)
    opened = []
    monkeypatch.setattr(dash.subprocess, "run", lambda cmd, **k: opened.append(cmd))
    monkeypatch.delenv("HASEEB_AGENT_OFFLINE")
    assert dash.open_dashboard(settings) == 0
    out = capsys.readouterr().out
    url = out.strip()
    assert re.fullmatch(r"http://127\.0\.0\.1:8765/\?login=[0-9a-f]{64}", url)
    assert key not in out and "key=" not in out
    assert opened == [["open", url]] and key not in " ".join(opened[0])
    nonce = url.split("login=")[1]
    assert consume_nonce(dash.LOGIN_DIR, nonce, __import__("time").time())  # the printed nonce is real


def test_dashboard_open_is_guarded_in_tests_and_writes_no_nonce(settings, home, monkeypatch):
    monkeypatch.setattr(dash.subprocess, "run", lambda *a, **k: pytest.fail("real browser"))
    with pytest.raises(OfflineError):
        dash.open_dashboard(settings)
    assert not dash.LOGIN_DIR.exists()  # the guard comes before the nonce is written


def test_app_state_does_not_expose_the_key(app):
    assert not hasattr(app.state, "key") and KEY not in repr(vars(app.state))


def test_rotate_key_restarts_dashboard_and_logs_everyone_out(settings, home, monkeypatch, capsys, clock):
    old = load_or_create_key(dash.KEY_PATH)
    old_app = create_app(settings, lambda: None, old, lambda: None, clock=clock, login_dir=dash.LOGIN_DIR)
    c, token = do_login(old_app, dash.LOGIN_DIR, clock)
    calls = []
    monkeypatch.delenv("HASEEB_AGENT_OFFLINE")
    monkeypatch.setattr(dash.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    assert dash.rotate_dashboard_key() == 0
    new = load_or_create_key(dash.KEY_PATH)
    assert new != old
    assert calls == [["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.haseeb.agent.dashboard"]]
    out = capsys.readouterr()
    assert old not in out.out + out.err and new not in out.out + out.err
    assert not (dash.LOGIN_DIR / "login").exists()  # an outstanding nonce is dropped too
    new_app = create_app(settings, lambda: None, new, lambda: None, clock=clock, login_dir=dash.LOGIN_DIR)
    stale = new_client(new_app)
    for name, value in c.cookies.items():
        stale.cookies.set(name, value)
    assert stale.cookies and stale.get("/").status_code == 401  # the old cookie is dead


def test_rotate_key_guarded_and_leaves_key_alone(settings, home, monkeypatch):
    old = load_or_create_key(dash.KEY_PATH)
    monkeypatch.setattr(dash.subprocess, "run", lambda *a, **k: pytest.fail("real launchctl"))
    with pytest.raises(OfflineError):
        dash.rotate_dashboard_key()
    assert load_or_create_key(dash.KEY_PATH) == old


def test_cli_dashboard_subcommands_parse():
    p = cli._parser()
    for sub in ("serve", "open", "rotate-key"):
        assert p.parse_args(["dashboard", sub]).dashboard_command == sub


def test_static_not_git_ignored():
    r = subprocess.run(["git", "check-ignore", "src/haseeb_agent/dashboard/static/index.html",
                        "src/haseeb_agent/dashboard/static/app.js"], cwd=ROOT, capture_output=True)
    assert r.returncode == 1


def test_dashboard_plist_and_scripts(tmp_path):
    for script in ("install-launchd.sh", "uninstall-launchd.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / script)], check=True)
        assert "com.haseeb.agent.dashboard" in (ROOT / "scripts" / script).read_text()
    out = tmp_path / "r"
    subprocess.run(["bash", str(ROOT / "scripts" / "install-launchd.sh"), "--render-only", str(out)],
                   check=True, capture_output=True)
    p = plistlib.loads((out / "com.haseeb.agent.dashboard.plist").read_bytes())
    assert p["Label"] == "com.haseeb.agent.dashboard"
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    assert p["ProgramArguments"][-2:] == ["dashboard", "serve"]
    home = str(Path.home())
    assert p["StandardOutPath"] == f"{home}/Library/Logs/haseeb-agent/dashboard.log"
    assert p["StandardErrorPath"] == f"{home}/Library/Logs/haseeb-agent/dashboard.err.log"


# -- the final review: `dashboard serve` fails fast on a busy port; launchd waits 30 s between restarts ----------

def _listener() -> "socket.socket":
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn binds
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def test_serve_says_the_port_is_in_use_instead_of_a_traceback(settings, home, monkeypatch, capsys):
    import uvicorn
    busy = _listener()
    try:
        port = busy.getsockname()[1]
        monkeypatch.setattr(uvicorn.Server, "run", lambda *a, **k: pytest.fail("no server on a busy port"))
        rc = dash.serve(replace(settings, dashboard_port=port))
    finally:
        busy.close()
    err = capsys.readouterr().err
    assert rc == 1 and f"port {port} is already in use" in err and "Traceback" not in err
    assert not dash.KEY_PATH.exists()  # it stops before touching the key


def test_serve_hands_its_bound_socket_to_uvicorn(settings, home, monkeypatch):
    import socket
    import uvicorn
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    seen = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: seen.append(
        [s.getsockname() for s in sockets or []]))
    assert dash.serve(replace(settings, dashboard_port=port)) == 0
    assert seen == [[("127.0.0.1", port)]]


def test_dashboard_plist_throttles_restarts():
    p = plistlib.loads((ROOT / "deploy" / "launchd" / "com.haseeb.agent.dashboard.plist").read_bytes())
    assert p["ThrottleInterval"] == 30 and p["KeepAlive"] is True


def test_starlette_is_pinned_and_installed_as_pinned():
    """The final review (M8): an exact pin to a release at least about two weeks old, and the venv has exactly it."""
    import re as _re
    from importlib.metadata import version
    pins = _re.findall(r'"starlette==([0-9.]+)"', (ROOT / "pyproject.toml").read_text())
    assert len(pins) == 1 and version("starlette") == pins[0]
    lock = (ROOT / "uv.lock").read_text()
    assert _re.search(r'name = "starlette"\nversion = "' + _re.escape(pins[0]) + '"', lock)
