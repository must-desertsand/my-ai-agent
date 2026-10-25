"""The filtering proxy: the only way out of the browser that renders a site for review.

Chrome is started with `--proxy-server=http://127.0.0.1:<port>` and `--proxy-bypass-list=<-loopback>` (the second flag
stops Chrome keeping loopback and link-local for itself), so EVERY request, to any address, arrives here as a plain HTTP
proxy request (`GET http://host/...`) or a CONNECT tunnel (`CONNECT host:443`). For each one the proxy asks the policy
(policy.py) whether the destination may be reached, and only then opens a connection, to an IP address the policy has
resolved and checked (pinned: the name is never looked up again). A refused destination gets `403` and is never
connected to: not a socket, not a SYN.

What the proxy forwards: a CONNECT tunnel is a blind byte pipe to the pinned address (TLS runs end to end between Chrome and
the site). A plain HTTP request is forwarded to the pinned address as one request with `Connection: close`, its hop-by-hop
headers (`Proxy-*`, `Connection`, `Upgrade`, ...) removed. Nothing is added: no cookie, no credential, no forwarding
header. The proxy keeps no state between requests, so a second request on the same client connection cannot reach any
other place than the one first connection's pinned address.

Every real step is behind the offline guard: a DNS lookup (policy.system_resolver) and an outbound connect (`open_connection`).
The proxy listens on 127.0.0.1 only, on a random port, and lives for one render.

The blocking steps of a lookup (the system resolver's `getaddrinfo`, `ifconfig` and `route` in localnet.py) run in the worker
threads of the proxy's own executor (`DaemonExecutor`, the default executor of its event loop). Those threads are daemons that
nothing joins, and stopping the proxy shuts the executor down without waiting and cancels what was still queued, so a lookup that
hangs in the system resolver (a page can ask for names under a name server that never answers) delays neither the end of the
render nor the exit of the process: `stop()` returns at once, and the lookup, which can no longer reach anything, ends on its own.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import socket
import threading
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..offline import OfflineError, guard
from .policy import Policy, Refused, Resolver, system_resolver

log = logging.getLogger(__name__)

HEAD_LIMIT = 16 * 1024            # a request head larger than this is refused
HEAD_TIMEOUT_S = 10
CONNECT_TIMEOUT_S = 10
IDLE_TIMEOUT_S = 30               # a connection with nothing moving for this long is closed
MAX_CONNECTIONS = 64
MAX_BYTES_PER_CONNECTION = 64 * 1024 * 1024
REFUSED_KEPT = 200                # the refusals remembered for the render's checks

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

_HOP_BY_HOP = {"connection", "proxy-connection", "proxy-authorization", "proxy-authenticate", "keep-alive", "te",
               "trailer", "upgrade", "expect"}
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
MAX_DIAL = 4                      # a name's first addresses are tried; the rest never are


async def open_connection(ip: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The proxy's one outbound connect: to an address the policy resolved and checked. A real network call, so the
    offline guard comes first."""
    guard("site review proxy connect")
    return await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT_S)


class DaemonExecutor(concurrent.futures.ThreadPoolExecutor):
    """The default executor of the proxy's event loop: its work runs in DAEMON threads that nothing joins.

    asyncio insists that a default executor is a ThreadPoolExecutor, so this is one in name only: it never starts the base
    class's threads, which `concurrent.futures` joins at interpreter exit and which a lookup stuck in the system resolver would keep
    waiting. `shutdown` never waits: it cancels what is still queued, refuses new work, and leaves a worker that is mid-call to
    finish (or not) on its own, as a daemon thread of a process that may already be exiting. At most `max_workers` calls run at a
    time; the rest wait in a queue, and one whose caller gave up (a lookup that timed out) is skipped when its turn comes."""

    def __init__(self, max_workers: int = 32):
        super().__init__(max_workers=1)
        self._dx_max = max_workers
        self._dx_lock = threading.Lock()
        self._dx_queue: deque = deque()
        self._dx_running = 0
        self._dx_closed = False

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._dx_lock:
            if self._dx_closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._dx_queue.append((future, fn, args, kwargs))
            if self._dx_running < self._dx_max:
                self._dx_running += 1
                threading.Thread(target=self._dx_work, name="site-review-lookup", daemon=True).start()
        return future

    def _dx_work(self) -> None:
        while True:
            with self._dx_lock:
                if not self._dx_queue:
                    self._dx_running -= 1
                    return
                future, fn, args, kwargs = self._dx_queue.popleft()
            if not future.set_running_or_notify_cancel():  # its caller gave up while it waited
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as e:  # noqa: BLE001 - whatever the call raised is the caller's to see
                future.set_exception(e)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Never waits (`wait` is ignored) and always cancels what is queued: see the class."""
        with self._dx_lock:
            self._dx_closed = True
            queued, self._dx_queue = self._dx_queue, deque()
        for future, *_ in queued:
            future.cancel()


@dataclass
class ProxyLog:
    """What the proxy did, for the render's checks, for the one summary line a render logs, and for tests. No URL path, no
    header, no body: hosts and fixed reason codes only, and nothing here is ever written to a log line one request at a time
    (M4: the host names are strings the page chooses, and a fetch loop can ask for thousands in a render)."""
    refused: list[tuple[str, str, str]] = field(default_factory=list)   # (host, reason, detail), at most REFUSED_KEPT
    refused_count: int = 0
    connected: list[tuple[str, str, int]] = field(default_factory=list)  # (host, ip, port)
    reasons: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)                      # exception types of connections that broke, at most REFUSED_KEPT
    error_kinds: Counter = field(default_factory=Counter)                # the same, counted by type, all of them

    def refuse(self, host: str, e: Refused) -> None:
        self.refused_count += 1
        self.reasons[e.reason] += 1
        if len(self.refused) < REFUSED_KEPT:
            self.refused.append((host[:100], e.reason, e.detail))


class _Bad(Exception):
    """A request the proxy will not read: a status and a fixed word."""

    def __init__(self, status: int, word: str):
        super().__init__(word)
        self.status, self.word = status, word


_STATUS_TEXT = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 431: "Request Header Fields Too Large",
                502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}


def _response(status: int, word: str) -> bytes:
    body = f"{word}\n".encode("ascii", "replace")
    head = (f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Error')}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n")
    return head.encode("ascii") + body


def split_hostport(target: str, default_port: int | None = None) -> tuple[str, int]:
    """`host:port` or `[v6]:port` (a CONNECT target) as (host, port). A bracketed host keeps its brackets for policy."""
    t = target.strip()
    if t.startswith("["):
        end = t.find("]")
        if end < 0:
            raise _Bad(400, "bad target")
        host, rest = t[: end + 1], t[end + 1:]
        if rest and not rest.startswith(":"):
            raise _Bad(400, "bad target")
        port_text = rest[1:]
    else:
        if t.count(":") > 1:
            raise _Bad(400, "bad target")  # an unbracketed IPv6 literal is not a valid target
        host, _, port_text = t.partition(":")
    if not host or any(c.isspace() or c in "/\\@?#" for c in host):
        raise _Bad(400, "bad target")
    if port_text == "":
        if default_port is None:
            raise _Bad(400, "bad target")
        return host, default_port
    if not port_text.isascii() or not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise _Bad(400, "bad target")
    return host, int(port_text)


def _parse_head(raw: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    """(method, target, headers) of a request head, or _Bad. Strict: a head that could be read two ways is refused."""
    try:
        text = raw.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes every byte
        raise _Bad(400, "bad request") from None
    lines = text.split("\r\n")
    if lines[-2:] != ["", ""]:
        raise _Bad(400, "bad request")
    lines = lines[:-2]
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _Bad(400, "bad request")
    method, target = parts[0], parts[1]
    if not method.isalpha() or not method.isupper() or len(method) > 10 or not target or len(target) > 4096:
        raise _Bad(400, "bad request")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not _TOKEN.fullmatch(name) or line[0] in " \t":
            raise _Bad(400, "bad header")  # also refuses obsolete line folding
        if any(ord(c) < 32 and c != "\t" for c in value) or "\x7f" in value:
            raise _Bad(400, "bad header")
        headers.append((name, value.strip()))
    lengths = {v for n, v in headers if n.lower() == "content-length"}
    if len(lengths) > 1 or (lengths and any(n.lower() == "transfer-encoding" for n, _ in headers)):
        raise _Bad(400, "ambiguous request")  # the two ways of smuggling a request: refused
    return method, target, headers


class FilteringProxy:
    """An HTTP/CONNECT proxy on 127.0.0.1 that connects only where `policy` says it may. `resolver` and `connector` are
    the two real steps (a DNS lookup, an outbound connect): both behind the offline guard by default, and replaceable
    in tests."""

    def __init__(self, policy: Policy, *, resolver: Resolver = system_resolver, connector: Connector = open_connection,
                 idle_timeout_s: float = IDLE_TIMEOUT_S, max_connections: int = MAX_CONNECTIONS):
        self.policy, self.resolver, self.connector = policy, resolver, connector
        self.idle_timeout_s, self.max_connections = idle_timeout_s, max_connections
        self.log = ProxyLog()
        self.port = 0
        self._sock: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: set[asyncio.Task] = set()
        self._started = threading.Event()
        self._stopped = False
        self._error: BaseException | None = None

    # ------------------------------------------------------------ lifecycle

    def bind(self) -> int:
        """Take a random port on 127.0.0.1 and listen on it (connections queue until `serve`); returns the port. Split
        from `serve` so Chrome can be started, with this port, before the proxy's thread exists (a fork with no other
        thread running is the safe kind)."""
        if self._sock is not None:
            raise RuntimeError("the proxy is already bound")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        self._sock, self.port = sock, sock.getsockname()[1]
        return self.port

    def serve(self) -> None:
        """Start answering on the bound port, in a thread of its own."""
        if self._sock is None:
            raise RuntimeError("bind the proxy first")
        if self._thread is not None:
            raise RuntimeError("the proxy is already running")
        self._thread = threading.Thread(target=self._run, name="site-review-proxy", daemon=True)
        self._thread.start()
        if not self._started.wait(5) or self._error is not None:
            self.stop()
            raise RuntimeError("the proxy did not start")

    def start(self) -> int:
        """Listen on 127.0.0.1, random port, and serve; returns the port."""
        port = self.bind() if self._sock is None else self.port
        self.serve()
        return port

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(10)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def __enter__(self) -> "FilteringProxy":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.set_default_executor(DaemonExecutor())  # every blocking lookup of this render runs in its daemon threads
        server = None
        try:
            server = loop.run_until_complete(asyncio.start_server(self._client, sock=self._sock, limit=HEAD_LIMIT * 2))
            self._started.set()
            loop.run_forever()
        except BaseException as e:  # noqa: BLE001 - reported to start(); the thread must end either way
            self._error = e
            self._started.set()
        finally:
            try:
                if server is not None:
                    server.close()
                for t in list(self._tasks):
                    t.cancel()
                if self._tasks:
                    loop.run_until_complete(asyncio.gather(*self._tasks, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()  # shuts the executor down without waiting: a lookup that is hung must not hold the render back

    # ------------------------------------------------------------ one client connection

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            if len(self._tasks) > self.max_connections:
                writer.write(_response(503, "too many connections"))
                return
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        except Exception as e:  # noqa: BLE001 - one bad connection never takes the proxy down (OfflineError included:
            # a real step reached without a test having replaced it ends that connection, and shows in `log.errors`). Counted,
            # not logged: the render's one summary line says how many broke and of what type (M4).
            self.log.error_kinds[type(e).__name__] += 1
            if len(self.log.errors) < REFUSED_KEPT:
                self.log.errors.append(type(e).__name__)
        finally:
            if task is not None:
                self._tasks.discard(task)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
            if len(raw) > HEAD_LIMIT:
                raise _Bad(431, "request head too large")
            method, target, headers = _parse_head(raw)
            if method == "CONNECT":
                await self._tunnel(target, reader, writer)
            else:
                await self._forward(method, target, headers, reader, writer)
        except _Bad as e:
            writer.write(_response(e.status, e.word))
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            writer.write(_response(431, "request head too large or cut short"))
        except asyncio.TimeoutError:
            writer.write(_response(504, "timed out"))
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _pin(self, host: str, port: int):
        """The pinned destination, or None after answering 403 (the caller returns)."""
        try:
            return await self.policy.pin(host, port, self.resolver)
        except Refused as e:
            self.log.refuse(host, e)  # counted by reason; the host is a string the page chose, so it is never logged (M4)
            raise _Bad(403, f"refused: {e.reason}") from None

    async def _dial(self, pinned) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to the pinned addresses, in order, and to nothing else."""
        last: BaseException | None = None
        for ip in pinned.addresses[:MAX_DIAL]:
            try:
                up = await self.connector(ip, pinned.port)
            except OfflineError:
                raise
            except (OSError, asyncio.TimeoutError) as e:
                last = e
                continue
            self.log.connected.append((pinned.host[:100], ip, pinned.port))
            return up
        raise _Bad(502 if not isinstance(last, asyncio.TimeoutError) else 504, "could not connect")

    async def _tunnel(self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        host, port = split_hostport(target)
        pinned = await self._pin(host, port)
        up_reader, up_writer = await self._dial(pinned)
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer, one_shot=False)
        finally:
            up_writer.close()

    async def _forward(self, method: str, target: str, headers: list[tuple[str, str]],
                       reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A plain HTTP proxy request (`GET http://host/path`): one request to the pinned address, then the response."""
        if not target.lower().startswith("http://"):
            raise _Bad(400, "an absolute http:// target is required")  # https goes by CONNECT; a bare path is not for a proxy
        try:
            parts = urlsplit(target)
            port = parts.port
        except ValueError:
            raise _Bad(400, "bad target") from None
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise _Bad(403, "refused: credentials")
        if not parts.hostname:
            raise _Bad(400, "bad target")
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        pinned = await self._pin(host, port if port is not None else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        out = [f"{method} {path} HTTP/1.1"]
        for name, value in headers:
            if name.lower() in _HOP_BY_HOP:
                continue
            out.append(f"{name}: {value}")
        out.append("Connection: close")
        up_reader, up_writer = await self._dial(pinned)
        try:
            up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            await up_writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer, one_shot=True)
        finally:
            up_writer.close()

    async def _pipe(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter,
                    u_reader: asyncio.StreamReader, u_writer: asyncio.StreamWriter, *, one_shot: bool) -> None:
        """Move bytes both ways until both sides are done, one side idles too long, or a cap is hit. `one_shot` (a plain
        HTTP request, sent with `Connection: close`): the connection is over when the response is."""
        budget = [MAX_BYTES_PER_CONNECTION]

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            while True:
                data = await asyncio.wait_for(src.read(65536), self.idle_timeout_s)
                if not data:
                    if dst.can_write_eof():
                        dst.write_eof()
                    return
                budget[0] -= len(data)
                if budget[0] < 0:
                    raise OSError("too many bytes")
                dst.write(data)
                await dst.drain()

        up = asyncio.ensure_future(pump(c_reader, u_writer))
        down = asyncio.ensure_future(pump(u_reader, c_writer))
        tasks = [up, down]
        try:
            if one_shot:
                await asyncio.wait([down], return_when=asyncio.ALL_COMPLETED)  # the response, then done
                if down.done() and not down.cancelled():
                    down.result()
            else:
                done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for t in done:
                    t.result()  # an error (idle, cap, reset) ends the connection; EOF on one side lets the other finish
        except (asyncio.TimeoutError, OSError):
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
