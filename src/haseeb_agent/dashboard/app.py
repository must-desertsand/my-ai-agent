"""The dashboard app: security middleware, the code-for-token exchange route, the index page and the static files,
all served verbatim.

The JSON API routes (api.py) are registered before the static catch-all; the objects they need are on `app.state`
(settings, store_factory, kick, sessions).
"""
from __future__ import annotations

import errno
import mimetypes
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from ..config import Settings
from ..offline import guard
from ..store import Store
from .api import LOGS_DIR, ApiConfig, api_routes, default_spawn, run_sips
from .security import COOKIE, SecurityMiddleware, SessionStore, load_or_create_key, mint_nonce, rotate_key

STATIC_DIR = Path(__file__).resolve().parent / "static"
KEY_PATH = Path.home() / ".haseeb-agent" / "dashboard.key"
LOGIN_DIR = Path.home() / ".haseeb-agent" / "dashboard"  # holds `login`, the hash of the outstanding nonce
HOST = "127.0.0.1"  # never 0.0.0.0
mimetypes.add_type("text/javascript", ".js")


def kick_tick() -> int:
    """Ask launchd to run the tick now. Returns launchctl's exit code: 0 when it kicked the tick, otherwise non-zero
    (113 when the tick is not loaded in launchd, say), and 1 when launchctl could not be run at all. Never raises. A
    page click ignores the code (the queued command is picked up by the next tick anyway); the Check round control
    does not: it answers 409 instead of showing a tick that is running when nothing was started."""
    guard("kick tick")
    try:
        res = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/com.haseeb.agent.tick"],
                             timeout=5, capture_output=True, check=False)
    except Exception:  # noqa: BLE001
        return 1
    return int(getattr(res, "returncode", 0) or 0)


def _static_file(rel: str) -> Path | None:
    """The file under STATIC_DIR for a URL path, or None for anything outside it: absolute paths, `..`, NUL,
    symlinks (the file or any directory on the way) and non-files."""
    if not rel or "\x00" in rel or rel.startswith("/") or "\\" in rel or rel.lower() == "index.html":
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    base = STATIC_DIR.resolve()
    cur = STATIC_DIR
    for p in parts:
        cur = cur / p
        if cur.is_symlink():
            return None
    try:
        resolved = cur.resolve()
        resolved.relative_to(base)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def create_app(settings: Settings, store_factory: Callable[[], Store], key: str, kick: Callable[[], int | None], *,
               clock: Callable[[], float] = time.time, login_dir: Path | None = None,
               logs_dir: Path | None = None, decks_dir: Path | None = None,
               sips: Callable[[list[str]], int] = run_sips, stream_poll_s: float = 1.0,
               stream_max_polls: int | None = None, stream_sleep=None,
               spawn: Callable[[list[str]], None] = default_spawn, started_at: float | None = None) -> Starlette:
    import asyncio
    api = ApiConfig(settings, store_factory, kick, clock, logs_dir=logs_dir or LOGS_DIR, decks_dir=decks_dir,
                    sips=sips, spawn=spawn, stream_poll_s=stream_poll_s, stream_max_polls=stream_max_polls,
                    stream_sleep=stream_sleep or asyncio.sleep,
                    started_at=clock() if started_at is None else started_at)  # this process, on `clock`
    sessions = SessionStore(key, clock)

    async def index(request: Request):
        return FileResponse(STATIC_DIR / "index.html", headers={"cache-control": "no-store"})  # verbatim

    async def exchange(request: Request):
        """The fragment code from the login redirect, for this session's API token. The middleware has already
        required the cookie, Origin and Sec-Fetch-Site; the code itself is the proof of the login."""
        try:
            code = (await request.json()).get("code")
        except Exception:  # noqa: BLE001
            code = None
        if not isinstance(code, str) or not code:
            return PlainTextResponse("bad request", status_code=400)
        token = sessions.redeem_code(code, request.cookies.get(COOKIE, ""))
        if token is None:
            return PlainTextResponse("forbidden", status_code=403)
        return JSONResponse({"token": token}, headers={"cache-control": "no-store"})

    async def static(request: Request):
        path = _static_file(request.path_params["path"])
        index_file = STATIC_DIR / "index.html"
        if path is None or (index_file.exists() and os.path.samefile(path, index_file)):
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path, headers={"cache-control": "no-cache"})  # revalidated: an update is seen at once

    app = Starlette(
        debug=False,
        routes=[Route("/auth/exchange", exchange, methods=["POST"]), *api_routes(api), Route("/", index), Route("/index.html", index), Route("/{path:path}", static)],
        middleware=[Middleware(SecurityMiddleware, sessions=sessions, login_dir=login_dir or LOGIN_DIR,
                               port=settings.dashboard_port, clock=clock)],
    )
    app.state.settings = settings
    app.state.store_factory = store_factory
    app.state.kick = kick
    app.state.sessions = sessions
    return app


def serve_config(app: Starlette, port: int) -> uvicorn.Config:
    # access_log off: a request line can carry ?login=.
    return uvicorn.Config(app, host=HOST, port=port, access_log=False, log_level="warning")


def _bind(port: int) -> socket.socket:
    """The listening address, bound before anything else is set up (uvicorn listens on it)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # a restart may find the old one in TIME_WAIT
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        raise
    return sock


def serve(settings: Settings) -> int:
    """Run the dashboard. The port is bound first, so a port that is already taken (another dashboard, or anything
    else) is one clear line on stderr and exit 1, not a traceback, before the key is read. launchd then retries
    every 30 s (ThrottleInterval in its plist)."""
    port = settings.dashboard_port
    try:
        sock = _bind(port)
    except OSError as e:
        why = (f"port {port} is already in use (another dashboard running? `lsof -nP -iTCP:{port} -sTCP:LISTEN`)"
               if e.errno == errno.EADDRINUSE else f"cannot listen on {HOST}:{port} ({e.strerror or type(e).__name__})")
        print(f"haseeb-agent dashboard: {why}", file=sys.stderr, flush=True)
        return 1
    try:
        key = load_or_create_key(KEY_PATH)
        app = create_app(settings, lambda: Store(settings.db_path), key, kick_tick)
        uvicorn.Server(serve_config(app, port)).run(sockets=[sock])
    finally:
        sock.close()
    return 0


def dashboard_url(settings: Settings, nonce: str) -> str:
    return f"http://{HOST}:{settings.dashboard_port}/?login={nonce}"


def open_browser(url: str) -> None:
    guard("open browser")
    subprocess.run(["open", url], check=False, timeout=10)


def open_dashboard(settings: Settings) -> int:
    """Mint a single-use login nonce and open its URL. The permanent key is never printed or put in argv."""
    guard("open browser")  # before the nonce is written
    url = dashboard_url(settings, mint_nonce(LOGIN_DIR, time.time()))
    print(url, flush=True)
    open_browser(url)
    return 0


def rotate_dashboard_key() -> int:
    """Replace the key, drop any outstanding login nonce and restart the dashboard, which logs every session out."""
    guard("rotate dashboard key")  # before touching anything
    rotate_key(KEY_PATH)
    (LOGIN_DIR / "login").unlink(missing_ok=True)
    try:
        res = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.haseeb.agent.dashboard"],
                             timeout=10, capture_output=True, check=False)
        restarted = getattr(res, "returncode", 0) == 0
    except Exception:  # noqa: BLE001
        restarted = False
    print("dashboard key rotated; every session is logged out. Run `haseeb-agent dashboard open` to log in."
          + ("" if restarted else " The dashboard service could not be restarted: restart it yourself."), flush=True)
    return 0
