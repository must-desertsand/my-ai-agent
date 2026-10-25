"""The filtering proxy of the site review (haseeb_agent/site_review/proxy.py).

The claim to prove is that a refused destination is NEVER connected to. So every test that refuses something checks two
things: the answer (403), and that nothing on the other side was touched: the connector (a fake that records every
connect it is asked to make) and, in the real-socket tests, a canary server on 127.0.0.1 that counts every connection it
receives. A positive control in the same file shows the canary does see a connection when one is allowed, so a zero means
something. The only network used is loopback; the resolver is always a fake; the offline guard stays on (the proxy's
outbound connect is replaced by a fake, or, in the real-socket tests, the guard of the proxy module is lifted for a canary
on 127.0.0.1 only)."""
from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
import threading
import time

import pytest

from haseeb_agent.site_review import policy as policy_mod
from haseeb_agent.site_review import proxy as proxy_mod
from haseeb_agent.site_review.policy import Policy
from haseeb_agent.site_review.proxy import FilteringProxy

PUBLIC = "93.184.216.34"


class Canary:
    """A real TCP server on 127.0.0.1 that records every connection (and what its first bytes were) and either echoes
    what it is sent (mode "echo") or answers one HTTP request (mode "http")."""

    def __init__(self, mode: str = "echo"):
        self.mode = mode
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.heads: list[bytes] = []
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def connections(self) -> int:
        return len(self.heads)

    def _serve(self) -> None:
        self.sock.settimeout(0.1)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        conn.settimeout(3)
        with conn:
            try:
                if self.mode == "echo":
                    first = conn.recv(65536)
                    self.heads.append(first)
                    while first:
                        conn.sendall(first)
                        first = conn.recv(65536)
                    return
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                while len(rest) < length:
                    rest += conn.recv(65536)
                self.heads.append(head + b"\r\n\r\n" + rest)
                body = b"hello"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nContent-Type: text/plain\r\n\r\n" % len(body) + body)
            except OSError:
                pass

    def close(self) -> None:
        self._stop = True
        self.sock.close()


@pytest.fixture
def canary():
    c = Canary()
    yield c
    c.close()


@pytest.fixture
def web():
    c = Canary("http")
    yield c
    c.close()


class MapConnector:
    """The proxy's outbound connect, faked: records the (ip, port) it is asked for and connects to a canary instead."""

    def __init__(self, port: int):
        self.port, self.calls = port, []

    async def __call__(self, ip, port):
        self.calls.append((ip, port))
        return await asyncio.open_connection("127.0.0.1", self.port)


class Resolver:
    def __init__(self, *answers, by_host: dict | None = None):
        self.answers, self.by_host, self.calls = list(answers), by_host or {}, []

    async def __call__(self, host, port):
        self.calls.append((host, port))
        if host in self.by_host:
            return self.by_host[host]
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def talk(port: int, data: bytes, timeout: float = 5) -> bytes:
    """Send `data` to the proxy and read until it closes the connection."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        out = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return out
                out += chunk
        except socket.timeout:
            return out


def status(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP/1.1 ") else 0


def connect_req(target: str) -> bytes:
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()


@pytest.fixture
def proxy_with():
    made = []

    def make(policy=None, *, resolver=None, connector=None, **kw):
        p = FilteringProxy(policy or Policy(), resolver=resolver or Resolver([PUBLIC]),
                           connector=connector or MapConnector(1), **kw)
        p.start()
        made.append(p)
        return p
    yield make
    for p in made:
        p.stop()


# ---------------------------------------------------------------- refused: 403, and nothing is connected

REFUSED_TARGETS = [
    "127.0.0.1:37777",   # the claude-mem worker
    "127.0.0.1:8765",    # the dashboard
    "127.0.0.1:80", "127.0.0.1:443", "[::1]:443", "[::1]:80", "localhost:80", "localhost:443", "foo.localhost:80",
    "192.168.1.10:80", "192.168.1.10:443", "192.168.0.1:80", "10.0.0.1:443", "10.255.255.255:80", "172.16.0.1:80",
    "172.31.1.1:443", "169.254.169.254:80", "169.254.169.254:443", "100.64.0.1:443", "224.0.0.251:80", "0.0.0.0:80",
    "255.255.255.255:80", "[fe80::1]:80", "[fd00::1]:443", "[::ffff:127.0.0.1]:80", "[::ffff:192.168.1.1]:443",
    "[64:ff9b::7f00:1]:443", "[2002:7f00:1::]:80", "[::]:80",
    "0x7f.1:80", "2130706433:80", "127.1:80", "0177.0.0.1:80", "[127.0.0.1]:80",
    "example.com:8080", "example.com:22", "example.com:25", "example.com:3000", "example.com:8765", "example.com:37777",
    "example.com:81", "93.184.216.34:8080", "intranet:80", "printer.local:80", "nas.lan:443",
]


@pytest.mark.parametrize("target", REFUSED_TARGETS)
def test_connect_to_a_refused_destination_gets_403_and_nothing_is_connected(proxy_with, canary, target):
    connector = MapConnector(canary.port)
    p = proxy_with(resolver=Resolver([PUBLIC]), connector=connector)
    raw = talk(p.port, connect_req(target))
    assert status(raw) == 403, raw[:80]
    assert connector.calls == [] and canary.connections == 0
    assert p.log.refused_count == 1 and p.log.connected == []


@pytest.mark.parametrize("host", ["internal.example.com", "rebind.example.com", "evil.example.org"])
@pytest.mark.parametrize("answer", [["10.0.0.5"], ["127.0.0.1"], ["192.168.1.9"], ["169.254.169.254"], ["::1"],
                                    ["fd00::1"], ["100.64.1.1"], [PUBLIC, "10.0.0.5"], ["10.0.0.5", PUBLIC]])
def test_a_name_that_resolves_to_a_non_public_address_is_refused_and_not_connected(proxy_with, canary, host, answer):
    connector = MapConnector(canary.port)
    resolver = Resolver(answer)
    p = proxy_with(resolver=resolver, connector=connector)
    assert status(talk(p.port, connect_req(f"{host}:443"))) == 403
    assert status(talk(p.port, f"GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())) == 403
    assert connector.calls == [] and canary.connections == 0
    assert len(resolver.calls) == 2  # one lookup per request, and each was refused on its answer


def test_a_name_that_cannot_be_resolved_is_refused_and_not_connected(proxy_with, canary):
    connector = MapConnector(canary.port)
    p = proxy_with(resolver=Resolver([OSError("no such host")]), connector=connector)
    assert status(talk(p.port, connect_req("nowhere.example.com:443"))) == 403
    assert connector.calls == [] and canary.connections == 0


def test_plain_http_requests_to_refused_destinations_get_403_and_nothing_is_connected(proxy_with, canary):
    connector = MapConnector(canary.port)
    p = proxy_with(connector=connector)
    for target in ("http://127.0.0.1:8765/api/inbox", "http://127.0.0.1:37777/api/search", "http://localhost/",
                   "http://192.168.1.1/admin", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                   "http://10.0.0.1:80/", "http://example.com:8080/", "http://0x7f.1/", "http://2130706433/"):
        raw = talk(p.port, f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert status(raw) == 403, target
    assert connector.calls == [] and canary.connections == 0


def test_the_offline_policy_refuses_everything_and_looks_nothing_up(proxy_with, canary):
    connector, resolver = MapConnector(canary.port), Resolver([PUBLIC])
    p = proxy_with(Policy(offline=True), resolver=resolver, connector=connector)
    for req in (connect_req("example.com:443"), connect_req("93.184.216.34:80"), connect_req("127.0.0.1:80"),
                b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",
                b"GET http://93.184.216.34/ HTTP/1.1\r\nHost: x\r\n\r\n"):
        assert status(talk(p.port, req)) == 403
    assert connector.calls == [] and resolver.calls == [] and canary.connections == 0
    assert set(p.log.reasons) == {"offline"} and p.log.refused_count == 5


# ---------------------------------------------------------------- the real connect: a canary on loopback

@pytest.fixture
def real_connect(monkeypatch):
    """The proxy's own outbound connect, for a canary on 127.0.0.1 only: its offline guard is lifted in this module."""
    monkeypatch.setattr(proxy_mod, "guard", lambda what: None)


def test_the_real_connect_reaches_a_canary_only_when_the_policy_allows_that_exact_address_and_port(real_connect, canary):
    """The positive control and the claim together. Allowed pair: connected (the canary counts 1). Anything else on
    loopback, including the same address on another port, and every refusal with the default policy: never connected."""
    other = Canary()
    try:
        strict = FilteringProxy(Policy(), resolver=Resolver([PUBLIC]))  # the real connector, the default policy
        strict.start()
        for target in (f"127.0.0.1:{canary.port}", f"localhost:{canary.port}", f"[::1]:{canary.port}"):
            assert status(talk(strict.port, connect_req(target))) == 403
        strict.stop()
        assert canary.connections == 0 and other.connections == 0

        loose = FilteringProxy(Policy(allow=frozenset({("127.0.0.1", canary.port)})), resolver=Resolver([PUBLIC]))
        loose.start()
        with socket.create_connection(("127.0.0.1", loose.port), timeout=5) as s:
            s.sendall(connect_req(f"127.0.0.1:{canary.port}"))
            head = s.recv(4096)
            assert head.startswith(b"HTTP/1.1 200")
            s.sendall(b"ping")
            assert s.recv(4096) == b"ping"  # the canary echoes: a real tunnel
        assert canary.connections == 1  # the control: a connection IS counted when one is allowed
        assert status(talk(loose.port, connect_req(f"127.0.0.1:{other.port}"))) == 403  # same address, another port
        assert status(talk(loose.port, connect_req(f"127.0.0.2:{canary.port}"))) == 403  # same port, another address
        time.sleep(0.2)
        assert canary.connections == 1 and other.connections == 0
        loose.stop()
    finally:
        other.close()


def test_the_default_connector_and_resolver_are_behind_the_offline_guard():
    with pytest.raises(proxy_mod.OfflineError):
        asyncio.run(proxy_mod.open_connection("93.184.216.34", 443))


def test_a_connect_through_the_real_connector_in_offline_mode_ends_without_a_connection(canary):
    """HASEEB_AGENT_OFFLINE is set: even an allowed destination cannot be dialled, and the failure is recorded."""
    p = FilteringProxy(Policy(allow=frozenset({("127.0.0.1", canary.port)})), resolver=Resolver([PUBLIC]))
    p.start()
    try:
        assert talk(p.port, connect_req(f"127.0.0.1:{canary.port}")) == b""  # closed with no answer
        assert canary.connections == 0 and p.log.errors == ["OfflineError"]
    finally:
        p.stop()


# ---------------------------------------------------------------- allowed: relayed to the pinned address

def test_an_allowed_connect_is_a_tunnel_to_the_resolved_address(proxy_with, canary):
    connector, resolver = MapConnector(canary.port), Resolver([PUBLIC])
    p = proxy_with(resolver=resolver, connector=connector)
    with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
        s.sendall(connect_req("Example.com:443"))
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        s.sendall(b"\x16\x03\x01 a pretend TLS hello")
        assert s.recv(4096) == b"\x16\x03\x01 a pretend TLS hello"
    assert connector.calls == [(PUBLIC, 443)]  # the address the one lookup gave, and nothing else
    assert resolver.calls == [("example.com", 443)]
    assert p.log.connected == [("example.com", PUBLIC, 443)] and p.log.refused_count == 0


def test_a_dns_rebinding_answer_cannot_swap_the_destination(proxy_with, canary):
    """The first lookup is public: that address is pinned and connected to, with no second lookup. The next request for
    the same name is answered with a private address and is refused, and never connected."""
    connector = MapConnector(canary.port)
    resolver = Resolver([PUBLIC], ["127.0.0.1"])
    p = proxy_with(resolver=resolver, connector=connector)
    with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
        s.sendall(connect_req("rebind.example.com:443"))
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        s.sendall(b"x")
        assert s.recv(10) == b"x"
    assert connector.calls == [(PUBLIC, 443)] and len(resolver.calls) == 1
    assert status(talk(p.port, connect_req("rebind.example.com:443"))) == 403
    assert connector.calls == [(PUBLIC, 443)] and len(resolver.calls) == 2  # still the one connect
    assert canary.connections == 1


def test_connect_to_a_public_ipv4_literal_is_a_tunnel_and_to_a_public_ipv6_literal_is_refused(proxy_with, canary):
    connector, resolver = MapConnector(canary.port), Resolver([PUBLIC])
    p = proxy_with(resolver=resolver, connector=connector)
    with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
        s.sendall(connect_req("8.8.8.8:80"))
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
    assert connector.calls == [("8.8.8.8", 80)] and resolver.calls == []
    for target in ("[2001:4860:4860::8888]:443", "[2606:4700:4700::1111]:80", "[::ffff:8.8.8.8]:443"):
        raw = talk(p.port, connect_req(target))  # IPv4 only (security review I1): even a "public" IPv6 address is refused
        assert status(raw) == 403 and raw.endswith(b"refused: ipv6\n"), (target, raw)
    assert connector.calls == [("8.8.8.8", 80)] and p.log.reasons == {"ipv6": 3}


def test_a_name_with_only_ipv6_answers_is_refused_and_one_with_both_is_dialled_on_its_ipv4_address_only(proxy_with, canary):
    connector = MapConnector(canary.port)
    p = proxy_with(resolver=Resolver(by_host={"aaaa.example.com": ["2606:2800:220:1::1"],
                                              "both.example.com": ["2606:2800:220:1::1", PUBLIC]}), connector=connector)
    assert status(talk(p.port, connect_req("aaaa.example.com:443"))) == 403 and connector.calls == []
    with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
        s.sendall(connect_req("both.example.com:443"))
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
    assert connector.calls == [(PUBLIC, 443)]


def test_the_connector_tries_the_next_resolved_address_when_one_does_not_answer(proxy_with, canary):
    class Flaky(MapConnector):
        async def __call__(self, ip, port):
            self.calls.append((ip, port))
            if ip == "93.184.216.1":
                raise ConnectionRefusedError()
            return await asyncio.open_connection("127.0.0.1", self.port)

    connector = Flaky(canary.port)
    p = proxy_with(resolver=Resolver(["93.184.216.1", "93.184.216.2"]), connector=connector)
    with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
        s.sendall(connect_req("example.com:443"))
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
    assert connector.calls == [("93.184.216.1", 443), ("93.184.216.2", 443)]  # only addresses the lookup returned


def test_when_no_resolved_address_answers_the_client_gets_502(proxy_with):
    class Dead(MapConnector):
        async def __call__(self, ip, port):
            self.calls.append((ip, port))
            raise ConnectionRefusedError()

    connector = Dead(1)
    p = proxy_with(resolver=Resolver(["93.184.216.1", "93.184.216.2"]), connector=connector)
    assert status(talk(p.port, connect_req("example.com:443"))) == 502
    assert len(connector.calls) == 2


def test_a_plain_http_request_is_forwarded_as_one_clean_request_with_connection_close(proxy_with, web):
    connector = MapConnector(web.port)
    p = proxy_with(connector=connector)
    req = (b"GET http://example.com/a/b?q=1&r=%20x HTTP/1.1\r\nHost: example.com\r\nProxy-Connection: keep-alive\r\n"
           b"Proxy-Authorization: Basic c2VjcmV0\r\nConnection: keep-alive\r\nKeep-Alive: timeout=5\r\n"
           b"User-Agent: Test\r\nAccept: */*\r\n\r\n")
    raw = talk(p.port, req)
    assert status(raw) == 200 and raw.endswith(b"hello")
    assert connector.calls == [(PUBLIC, 80)]
    (seen,) = web.heads
    lines = seen.split(b"\r\n")
    assert lines[0] == b"GET /a/b?q=1&r=%20x HTTP/1.1"  # origin form: the proxy's own URL form is gone
    got = {ln.split(b":", 1)[0].lower(): ln.split(b":", 1)[1].strip() for ln in lines[1:] if ln}
    assert got == {b"host": b"example.com", b"user-agent": b"Test", b"accept": b"*/*", b"connection": b"close"}
    assert b"secret" not in seen and b"c2VjcmV0" not in seen  # no proxy credential goes on


def test_a_post_body_is_forwarded_whole(proxy_with, web):
    p = proxy_with(connector=MapConnector(web.port))
    body = b"a=1&b=" + b"x" * 5000
    req = (b"POST http://example.com/form HTTP/1.1\r\nHost: example.com\r\nContent-Type: application/x-www-form-urlencoded\r\n"
           b"Content-Length: %d\r\n\r\n" % len(body)) + body
    raw = talk(p.port, req)
    assert status(raw) == 200
    assert web.heads[0].endswith(body) and web.heads[0].startswith(b"POST /form HTTP/1.1")


def test_the_proxy_adds_no_cookie_credential_or_forwarding_header(proxy_with, web):
    p = proxy_with(connector=MapConnector(web.port))
    talk(p.port, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    names = {ln.split(b":", 1)[0].lower() for ln in web.heads[0].split(b"\r\n")[1:] if ln}
    assert names == {b"host", b"connection"}


@pytest.mark.parametrize("req", [
    b"GET /just/a/path HTTP/1.1\r\nHost: example.com\r\n\r\n",                 # not for a proxy
    b"GET https://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",         # https goes by CONNECT
    b"GET ftp://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",
    b"GET http:///nohost HTTP/1.1\r\nHost: x\r\n\r\n",
    b"CONNECT example.com HTTP/1.1\r\nHost: x\r\n\r\n",                         # no port
    b"CONNECT example.com:abc HTTP/1.1\r\nHost: x\r\n\r\n",
    b"CONNECT example.com:0 HTTP/1.1\r\nHost: x\r\n\r\n",
    b"CONNECT example.com:70000 HTTP/1.1\r\nHost: x\r\n\r\n",
    b"CONNECT ::1:443 HTTP/1.1\r\nHost: x\r\n\r\n",                              # an unbracketed IPv6 literal
    b"CONNECT example.com:443/x HTTP/1.1\r\nHost: x\r\n\r\n",
    b"nonsense\r\n\r\n",
    b"get http://example.com/ HTTP/1.1\r\nHost: x\r\n\r\n",                     # a lower-case method
    b"GET http://example.com/ HTTP/2.0\r\nHost: x\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nBad Header: 1\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\n folded: 1\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nContent-Length: 4\r\nContent-Length: 5\r\n\r\nabcd",
    b"POST http://example.com/ HTTP/1.1\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"GET http://example.com/ HTTP/1.1\r\nX: a\x00b\r\n\r\n",
])
def test_a_request_that_cannot_be_read_one_way_is_refused_and_nothing_is_connected(proxy_with, canary, req):
    connector = MapConnector(canary.port)
    p = proxy_with(connector=connector)
    assert status(talk(p.port, req)) in (400, 403, 431)
    assert connector.calls == [] and canary.connections == 0


def test_credentials_in_the_url_are_refused(proxy_with, canary):
    connector = MapConnector(canary.port)
    p = proxy_with(connector=connector)
    for url in ("http://user:pass@example.com/", "http://user@example.com/", "http://example.com:80@127.0.0.1/",
                "http://evil.com\\@127.0.0.1/"):
        assert status(talk(p.port, f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())) in (400, 403), url
    assert connector.calls == [] and canary.connections == 0


def test_an_oversized_request_head_is_refused(proxy_with, canary):
    connector = MapConnector(canary.port)
    p = proxy_with(connector=connector)
    for size in (proxy_mod.HEAD_LIMIT + 100, 40000):  # past the proxy's own limit, and past the reader's buffer
        big = b"GET http://example.com/ HTTP/1.1\r\nHost: x\r\nX-Big: " + b"a" * size + b"\r\n\r\n"
        assert status(talk(p.port, big)) in (400, 431)
    ok = b"GET http://example.com/ HTTP/1.1\r\nHost: x\r\nX-Ok: " + b"a" * 8000 + b"\r\n\r\n"
    assert talk(p.port, ok).startswith(b"GET / HTTP/1.1")  # read and forwarded (the echo canary sends it straight back)
    assert len(connector.calls) == 1


def test_a_client_that_never_finishes_its_request_is_cut_off(proxy_with, monkeypatch):
    monkeypatch.setattr(proxy_mod, "HEAD_TIMEOUT_S", 0.3)
    p = proxy_with()
    with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
        s.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: x\r\n")  # no blank line
        assert status(s.recv(4096)) == 504


def test_a_silent_tunnel_is_closed_after_the_idle_time(proxy_with, canary):
    p = proxy_with(connector=MapConnector(canary.port), idle_timeout_s=0.3)
    with socket.create_connection(("127.0.0.1", p.port), timeout=5) as s:
        s.sendall(connect_req("example.com:443"))
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        t0 = time.time()
        assert s.recv(4096) == b""  # the proxy closed it
        assert time.time() - t0 < 3


def test_many_connections_at_once_are_all_served(proxy_with, canary):
    p = proxy_with(connector=MapConnector(canary.port))
    results = []

    def one(i):
        with socket.create_connection(("127.0.0.1", p.port), timeout=10) as s:
            s.sendall(connect_req("example.com:443"))
            s.recv(4096)
            s.sendall(b"n%d" % i)
            results.append(s.recv(4096) == b"n%d" % i)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join(15) for t in threads]
    assert results == [True] * 20


def test_the_proxy_listens_on_loopback_only_and_stops(proxy_with):
    p = proxy_with()
    assert p._sock.getsockname()[0] == "127.0.0.1" and p.port > 1024
    p.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", p.port), timeout=1)
    p.stop()  # twice is fine


def test_the_proxy_does_not_start_twice():
    p = FilteringProxy(Policy())
    p.start()
    try:
        with pytest.raises(RuntimeError):
            p.start()
    finally:
        p.stop()


def test_the_log_names_hosts_and_fixed_reasons_only(proxy_with, canary):
    p = proxy_with(resolver=Resolver([PUBLIC], by_host={"bad.example.com": ["10.1.1.1"]}),
                   connector=MapConnector(canary.port))
    talk(p.port, connect_req("bad.example.com:443"))
    talk(p.port, connect_req("127.0.0.1:37777"))
    talk(p.port, b"GET http://example.com:8080/secret/path?token=abc HTTP/1.1\r\nHost: x\r\n\r\n")
    assert p.log.refused == [("bad.example.com", "address", "private"), ("127.0.0.1", "port", "37777"),
                             ("example.com", "port", "8080")]  # the port is judged first
    assert p.log.reasons == {"address": 1, "port": 2} and p.log.refused_count == 3
    flat = repr(p.log.refused)
    assert "secret" not in flat and "token" not in flat and "10.1.1.1" not in flat


def test_the_refusal_log_is_capped(proxy_with):
    p = proxy_with()
    for i in range(proxy_mod.REFUSED_KEPT + 20):
        talk(p.port, connect_req(f"127.0.0.1:{9000 + i}"))
    assert len(p.log.refused) == proxy_mod.REFUSED_KEPT and p.log.refused_count == proxy_mod.REFUSED_KEPT + 20


# ---------------------------------------------------------------- logs (security review M4)

def test_a_flood_of_refused_requests_writes_no_log_line_and_never_a_host_name(proxy_with, caplog):
    """The host names are strings the page chooses, and a fetch loop can ask for thousands in one render: the proxy counts
    them by reason and logs nothing per request."""
    caplog.set_level(logging.DEBUG)
    p = proxy_with(resolver=Resolver([PUBLIC]))
    for i in range(300):
        assert status(talk(p.port, connect_req(f"flood-{i}.attacker-chosen.example:22"))) == 403
    assert p.log.refused_count == 300 and p.log.reasons == {"port": 300}
    assert [r for r in caplog.records if r.name.startswith("haseeb_agent.site_review")] == []
    assert "flood-" not in caplog.text and "attacker-chosen" not in caplog.text
    assert len(p.log.refused) == proxy_mod.REFUSED_KEPT  # and what is kept in memory for the render's checks stays capped


def test_connections_that_break_are_counted_by_type_and_not_logged_one_by_one(canary, caplog):
    caplog.set_level(logging.DEBUG)
    p = FilteringProxy(Policy(allow=frozenset({("127.0.0.1", canary.port)})), resolver=Resolver([PUBLIC]))
    p.start()
    try:
        for _ in range(proxy_mod.REFUSED_KEPT + 10):
            talk(p.port, connect_req(f"127.0.0.1:{canary.port}"))  # the guard refuses the real connect: OfflineError each time
        assert p.log.error_kinds == {"OfflineError": proxy_mod.REFUSED_KEPT + 10}
        assert len(p.log.errors) == proxy_mod.REFUSED_KEPT  # capped in memory
        assert [r for r in caplog.records if r.name.startswith("haseeb_agent.site_review")] == []
    finally:
        p.stop()


# ---------------------------------------------------------------- stopping with a lookup that never answers (security re-review N2)

def lookup_workers():
    return [t for t in threading.enumerate() if t.name == "site-review-lookup" and t.is_alive()]


def send_and_wait_for_a_worker(p, request: bytes) -> socket.socket:
    """Send a request whose lookup hangs, and wait until a worker thread has picked it up."""
    s = socket.socket()  # (not create_connection: some of these tests replace socket.getaddrinfo with a call that never returns)
    s.settimeout(5)
    s.connect(("127.0.0.1", p.port))
    s.sendall(request)
    deadline = time.monotonic() + 5
    while not lookup_workers() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lookup_workers(), "the lookup never reached a worker thread"
    return s


def test_stop_returns_at_once_though_a_resolver_is_hung_in_a_worker_thread():
    """A page can ask for names under a name server that never answers. The proxy must not wait for that lookup: it was 10 s
    (the join) before, with the thread still running; the worker is a daemon that nothing joins."""
    release = threading.Event()

    async def hung(host, port):
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 60)
        return [PUBLIC]
    p = FilteringProxy(Policy(), resolver=hung, connector=MapConnector(1))
    p.start()
    try:
        s = send_and_wait_for_a_worker(p, connect_req("hung.example.com:443"))
        began = time.monotonic()
        p.stop()
        took = time.monotonic() - began
        assert took < 2.0, f"stop() took {took:.1f} s"
        workers = lookup_workers()
        assert workers and all(t.daemon for t in workers)  # still hung, and nothing waits for it
        assert p._thread is not None and not p._thread.is_alive()  # the proxy's own thread is gone
        s.close()
    finally:
        release.set()  # let the worker end, so the test leaves nothing behind
        deadline = time.monotonic() + 5
        while lookup_workers() and time.monotonic() < deadline:
            time.sleep(0.01)


def test_stop_returns_at_once_though_the_system_resolver_hangs_in_getaddrinfo(monkeypatch):
    """The real `system_resolver` (its lookup is the loop's `getaddrinfo`, in the loop's executor), with a `getaddrinfo` that
    never returns."""
    release = threading.Event()
    monkeypatch.setattr(policy_mod, "guard", lambda what: None)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: release.wait(60) or [])
    p = FilteringProxy(Policy(), connector=MapConnector(1))  # the default resolver
    p.start()
    try:
        s = send_and_wait_for_a_worker(p, connect_req("hung.example.com:443"))
        began = time.monotonic()
        p.stop()
        assert time.monotonic() - began < 2.0
        assert lookup_workers() and all(t.daemon for t in lookup_workers())
        s.close()
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while lookup_workers() and time.monotonic() < deadline:
            time.sleep(0.01)


def test_a_hung_lookup_of_this_macs_networks_does_not_hold_stop_back_either():
    from haseeb_agent.site_review.localnet import LocalNetworks
    release = threading.Event()
    local = LocalNetworks(lambda: [], lambda dest: (release.wait(60), "en0")[1])

    async def resolver(host, port):
        return [PUBLIC]
    p = FilteringProxy(Policy(local=local), resolver=resolver, connector=MapConnector(1))
    p.start()
    try:
        s = send_and_wait_for_a_worker(p, connect_req("example.com:443"))
        began = time.monotonic()
        p.stop()
        assert time.monotonic() - began < 2.0
        s.close()
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while lookup_workers() and time.monotonic() < deadline:
            time.sleep(0.01)


def test_a_worker_that_is_still_hung_does_not_keep_the_process_alive():
    """`concurrent.futures` joins its (non-daemon) worker threads when the interpreter exits, so a lookup stuck in the system
    resolver would keep the tick's process waiting after its work was done. A child process hangs a lookup, stops the proxy and
    ends: it must end, long before the 60 s the lookup would take."""
    script = """
import socket, threading, time
import haseeb_agent.site_review.policy as pol
pol.guard = lambda what: None
release = threading.Event()
socket.getaddrinfo = lambda *a, **k: release.wait(60) or []
from haseeb_agent.site_review.policy import Policy
from haseeb_agent.site_review.proxy import FilteringProxy
p = FilteringProxy(Policy())
p.start()
s = socket.socket()
s.connect(("127.0.0.1", p.port))
s.sendall(b"CONNECT hung.example.com:443 HTTP/1.1\\r\\nHost: hung.example.com\\r\\n\\r\\n")
for _ in range(500):
    if any(t.name == "site-review-lookup" for t in threading.enumerate()):
        break
    time.sleep(0.01)
began = time.monotonic()
p.stop()
print("stopped in %.2f s with %d worker(s) hung" % (time.monotonic() - began, sum(t.name == "site-review-lookup" for t in threading.enumerate())), flush=True)
"""
    done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=30)  # a hang is 60 s
    assert done.returncode == 0, done.stderr[-500:]
    line = done.stdout.strip().splitlines()[-1]
    assert line.startswith("stopped in ") and line.endswith("with 1 worker(s) hung"), (line, done.stderr[-300:])
    assert float(line.split()[2]) < 2.0


def test_the_executor_runs_work_in_daemon_threads_queues_past_its_limit_and_skips_what_was_given_up():
    ex = proxy_mod.DaemonExecutor(max_workers=2)
    assert isinstance(ex, proxy_mod.concurrent.futures.ThreadPoolExecutor)  # asyncio insists on that for a default executor
    gate = threading.Event()
    seen: list[tuple[str, bool]] = []

    def work(name):
        seen.append((name, threading.current_thread().daemon))
        gate.wait(5)
        return name.upper()
    a, b, c, d = (ex.submit(work, n) for n in "abcd")
    time.sleep(0.1)
    assert sorted(seen) == [("a", True), ("b", True)]  # two at a time, in daemon threads; c and d wait
    assert d.cancel() is True  # the caller of d gave up while it waited
    gate.set()
    assert (a.result(5), b.result(5), c.result(5)) == ("A", "B", "C") and d.cancelled()
    assert sorted(n for n, _ in seen) == ["a", "b", "c"]  # d never ran
    assert all(daemon for _n, daemon in seen)
    boom = ex.submit(lambda: 1 / 0)
    assert isinstance(boom.exception(5), ZeroDivisionError)  # what a call raises is its caller's to see


def test_shutdown_never_waits_cancels_the_queue_and_refuses_new_work():
    ex = proxy_mod.DaemonExecutor(max_workers=1)
    release = threading.Event()
    running = ex.submit(release.wait, 30)
    queued = ex.submit(lambda: "never")
    time.sleep(0.1)
    began = time.monotonic()
    ex.shutdown(wait=True, cancel_futures=False)  # the arguments cannot make it wait or keep the queue
    assert time.monotonic() - began < 1.0
    assert queued.cancelled() and not running.done()
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        ex.submit(lambda: 1)
    release.set()
    assert running.result(5) is True
