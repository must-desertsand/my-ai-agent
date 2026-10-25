"""The lock on the dashboard (ruling D10): login nonce, server-side sessions, per-session API token, Host and Origin
checks, security headers, and the files that hold the permanent key and the nonce.

Login: `dashboard open` mints a single-use nonce (32 random bytes, 60 s TTL; only its sha256 is stored in a 0700
dir). `GET /?login=<nonce>` must be a top-level navigation (Sec-Fetch-Site none/absent, Sec-Fetch-Mode navigate/absent,
else 403 with the nonce untouched). The guess is compared with the stored hash in constant time first; only a match
is consumed (atomic rename, then re-checked, putting a newer nonce back). A valid nonce always creates a fresh
server-side session (random 32-byte id in memory: at most 16, 12 h idle, 7 d absolute), sets the HttpOnly
SameSite=Strict `ha_session` cookie and answers 303 to `/#c=<one-time code>` (single use, 60 s, bound to that session).
Page JS POSTs the code to `/auth/exchange` (cookie, Origin and Sec-Fetch-Site checked, no token needed) and gets the
per-session API token, HMAC-SHA256(key, session id), which it keeps in sessionStorage; the token never appears in a
URL. Every /api request sends it as `X-Session-Token`; that header is also the CSRF defence for every method except
GET, HEAD and OPTIONS. All secret comparisons use hmac.compare_digest.

The permanent key (`dashboard.key`, 0600) only keys the per-session tokens: it is never in a URL, argv or output.
Rotating it (and restarting the server) invalidates every session. A server restart logs everyone out.

Residual, documented: cookies aren't port-isolated, so another 127.0.0.1 port can see `ha_session`; without the
sessionStorage token it cannot call the API.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "ha_session"
TOKEN_HEADER = "x-session-token"
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
}
NONCE_TTL_S = 60
CODE_TTL_S = 60
EXCHANGE_PATH = "/auth/exchange"
MAX_SESSIONS = 16
IDLE_S = 12 * 3600
ABSOLUTE_S = 7 * 86400
COOKIE_MAX_AGE = 7 * 86400
JUNK_AGE_S = 10  # a key file this young that isn't a key is left alone (someone may be writing it)
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


class KeyFileError(RuntimeError):
    """The key file is a symlink or holds something we must not overwrite."""


# ---------------------------------------------------------------- private files

def _private_dir(d: Path) -> None:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)


def _write_temp(d: Path, text: str) -> Path:
    """A fresh 0600 file in d (never an existing path, never through a symlink), flushed to disk."""
    tmp = d / f".tmp-{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def _read_nofollow(path: Path) -> tuple[str, float]:
    """(text, mtime) of a regular file; FileNotFoundError if absent; KeyFileError for a symlink. Tightens the mode."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise KeyFileError(f"{path} is a symlink; refusing to follow it") from e
        raise
    with os.fdopen(fd, "r") as f:
        st = os.fstat(f.fileno())
        if not stat_is_regular(st.st_mode):
            raise KeyFileError(f"{path} is not a regular file")
        os.fchmod(f.fileno(), 0o600)
        return f.read(), st.st_mtime


def stat_is_regular(mode: int) -> bool:
    import stat
    return stat.S_ISREG(mode)


def load_or_create_key(path: Path, *, wait_s: float = 0.5) -> str:
    """The permanent key: 32 random bytes as hex, mode 0600 in a 0700 directory. Created via a temp file and
    os.link, so a reader never sees a half-written key and concurrent creators agree on one winner. A symlink is
    refused; a file that isn't a key is replaced only when it is older than JUNK_AGE_S."""
    path = Path(path)
    _private_dir(path.parent)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            text, mtime = _read_nofollow(path)
        except FileNotFoundError:
            key = secrets.token_hex(32)
            tmp = _write_temp(path.parent, key + "\n")
            try:
                os.link(tmp, path)
                return key
            except FileExistsError:  # lost the race (or a dangling symlink): read what is there
                continue
            finally:
                tmp.unlink(missing_ok=True)
        if _KEY_RE.match(text.strip()):
            return text.strip()
        if time.time() - mtime > JUNK_AGE_S:
            return rotate_key(path)
        if time.monotonic() > deadline:
            raise KeyFileError(f"{path} exists but is not a dashboard key; left alone")
        time.sleep(0.05)


def rotate_key(path: Path) -> str:
    """Replace the key atomically (temp file, then rename). Returns the new key."""
    path = Path(path)
    _private_dir(path.parent)
    key = secrets.token_hex(32)
    tmp = _write_temp(path.parent, key + "\n")
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return key


# ---------------------------------------------------------------- login nonce

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def mint_nonce(login_dir: Path, now: float | None = None) -> str:
    """A new single-use login nonce; only its sha256 and expiry are stored, replacing any outstanding one."""
    login_dir = Path(login_dir)
    _private_dir(login_dir)
    now = time.time() if now is None else now
    nonce = secrets.token_hex(32)
    tmp = _write_temp(login_dir, f"{_sha(nonce)} {now + NONCE_TTL_S}\n")
    try:
        os.replace(tmp, login_dir / "login")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return nonce


def _stored(path: Path) -> tuple[str, float] | None:
    try:
        text, _ = _read_nofollow(path)
        stored, expires = text.split()
        return stored, float(expires)
    except (OSError, KeyFileError, ValueError):
        return None


def _matches(entry: tuple[str, float] | None, nonce: str, now: float) -> bool:
    return entry is not None and hmac.compare_digest(entry[0].encode(), _sha(nonce).encode()) and now <= entry[1]


def consume_nonce(login_dir: Path, nonce: str, now: float | None = None) -> bool:
    """Compare the guess with the stored hash first (constant time); only a match is taken, by atomic rename, so a
    wrong guess never spends the outstanding nonce and only one caller can win. After the rename the taken file is
    compared again: if a newer `dashboard open` replaced the nonce in between, the newer one is linked back."""
    login_dir = Path(login_dir)
    now = time.time() if now is None else now
    live = login_dir / "login"
    if not _matches(_stored(live), nonce, now):
        return False
    taken = login_dir / f"login.used-{secrets.token_hex(8)}"
    try:
        os.rename(live, taken)
    except OSError:
        return False
    try:
        entry = _stored(taken)
        if _matches(entry, nonce, now):
            return True
        try:
            os.link(taken, live)  # not ours: put it back (ignore if an even newer one already sits there)
        except OSError:
            pass
        return False
    finally:
        taken.unlink(missing_ok=True)


# ---------------------------------------------------------------- sessions

@dataclass
class Session:
    created: float
    last_seen: float
    token: str


@dataclass
class _Code:
    session: str  # the session's store key
    expires: float


class SessionStore:
    """In-memory sessions, keyed by sha256 of the random session id. A restart forgets them all."""

    def __init__(self, key: str, clock: Callable[[], float] = time.time):
        self._key = key
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._codes: dict[str, _Code] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def _expired(self, s: Session, now: float) -> bool:
        return now - s.last_seen > IDLE_S or now - s.created > ABSOLUTE_S

    def prune(self) -> None:
        now = self._clock()
        for k in [k for k, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[k]
        for k in [k for k, c in self._codes.items() if c.expires < now or c.session not in self._sessions]:
            del self._codes[k]

    def drop(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(_sha(sid), None)
            self.prune()

    def issue_code(self, sid: str) -> str:
        """A one-time code (60 s) bound to this session, to be exchanged for its API token."""
        code = secrets.token_hex(32)
        self._codes[_sha(code)] = _Code(_sha(sid), self._clock() + CODE_TTL_S)
        return code

    def redeem_code(self, code: str, sid: str) -> str | None:
        """The session's API token for its own unexpired code, which is then spent; None otherwise. Another
        session's code is refused and left unspent."""
        self.prune()
        entry = self._codes.get(_sha(code))
        if entry is None or not hmac.compare_digest(entry.session.encode(), _sha(sid).encode()):
            return None
        session = self.lookup(sid)
        if session is None:
            return None
        del self._codes[_sha(code)]
        return session.token

    def create(self) -> tuple[str, str]:
        """(session id for the cookie, API token)."""
        self.prune()
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda k: (self._sessions[k].last_seen, self._sessions[k].created))
            del self._sessions[oldest]
        sid = secrets.token_hex(32)
        token = hmac.new(self._key.encode(), sid.encode(), hashlib.sha256).hexdigest()
        now = self._clock()
        self._sessions[_sha(sid)] = Session(now, now, token)
        return sid, token

    def lookup(self, sid: str | None) -> Session | None:
        """The live session for a cookie value, touching its idle timer; None if unknown or expired."""
        if not sid:
            return None
        k = _sha(sid)
        s = self._sessions.get(k)
        if s is None:
            return None
        now = self._clock()
        if self._expired(s, now):
            del self._sessions[k]
            return None
        s.last_seen = now
        return s


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


# ---------------------------------------------------------------- middleware

class SecurityMiddleware:
    """Pure ASGI, so the headers land on every response, including the 4xx ones this class produces."""

    def __init__(self, app: ASGIApp, *, sessions: SessionStore, login_dir: Path, port: int,
                 clock: Callable[[], float] = time.time):
        self.app = app
        self.sessions = sessions
        self.login_dir = Path(login_dir)
        self.clock = clock
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # websockets (and anything else) are refused outright
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope, receive)
        refusal = self._check(request)
        if refusal is None:
            async def send_with_headers(message):
                if message["type"] == "http.response.start":
                    headers = [(k, v) for k, v in message.get("headers", [])
                               if k.decode("latin-1").lower() not in SECURITY_HEADERS
                               and not k.decode("latin-1").lower().startswith("access-control-")]
                    headers += [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                    message = {**message, "headers": headers}
                await send(message)
            await self.app(scope, receive, send_with_headers)
            return
        for k, v in SECURITY_HEADERS.items():
            refusal.headers[k] = v
        await refusal(scope, receive, send)

    def _check(self, request: Request) -> Response | None:
        host = request.headers.get("host")
        if host is None or host.lower() not in self.hosts:
            return PlainTextResponse("misdirected request", status_code=421)

        session = self.sessions.lookup(request.cookies.get(COOKIE))
        method = request.method

        if method in ("GET", "HEAD") and "login" in request.query_params:
            if (request.headers.get("sec-fetch-site", "none") != "none"
                    or request.headers.get("sec-fetch-mode", "navigate") != "navigate"):
                return PlainTextResponse("forbidden", status_code=403)  # not a user-started navigation: nonce untouched
            if not consume_nonce(self.login_dir, request.query_params["login"], self.clock()):
                # a stale link in a logged-in browser just lands on the page
                return self._redirect("/") if session is not None else PlainTextResponse("unauthorized", status_code=401)
            self.sessions.drop(request.cookies.get(COOKIE))
            sid, _ = self.sessions.create()
            code = self.sessions.issue_code(sid)
            resp = self._redirect(f"/#c={code}")  # always the root, never anything from the request
            resp.set_cookie(COOKIE, sid, max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict", path="/")
            return resp

        if session is None:
            return PlainTextResponse("unauthorized", status_code=401)

        path = request.url.path
        is_api = path == "/api" or path.startswith("/api/")
        unsafe = method not in _SAFE_METHODS
        if unsafe:
            if request.headers.get("origin") not in self.origins:  # a missing Origin is refused too
                return PlainTextResponse("forbidden", status_code=403)
            if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return PlainTextResponse("forbidden", status_code=403)
        if (unsafe or is_api) and not (method == "POST" and path == EXCHANGE_PATH):  # the exchange gets the token
            sent = request.headers.get(TOKEN_HEADER)
            if sent is None or not _eq(sent, session.token):
                return PlainTextResponse("forbidden", status_code=403)
        return None

    @staticmethod
    def _redirect(location: str) -> RedirectResponse:
        resp = RedirectResponse(location, status_code=303)
        resp.headers["cache-control"] = "no-store"
        return resp
