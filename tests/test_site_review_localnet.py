"""Haseeb's own networks, in the leash of the site review (haseeb_agent/site_review/localnet.py), and how the policy asks
about them (security review I1).

The point: an address that looks public can still be this Mac's own LAN, the Mac itself, or a network a VPN routes. The
interfaces and the routes are always injected (the real lookups run `ifconfig` and `route`, behind the offline guard, and a
test that forgets to replace them fails loudly). No test here makes a call outside this process."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import subprocess
import threading

import pytest

from haseeb_agent.offline import OfflineError
from haseeb_agent.site_review import localnet as ln
from haseeb_agent.site_review.localnet import LocalNetworks, NetworkError, parse_ifconfig, parse_route
from haseeb_agent.site_review.policy import Policy, Refused
from haseeb_agent.site_review.proxy import FilteringProxy

PUBLIC = "93.184.216.34"


def run(coro):
    return asyncio.run(coro)


IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
\tnd6 options=201<PERFORMNUD,DAD>
gif0: flags=8010<POINTOPOINT,MULTICAST> mtu 1280
stf0: flags=0<> mtu 1280
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
\tether aa:bb:cc:dd:ee:ff
\tinet6 fe80::1c2e:aaaa:bbbb:cccc%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 2a02:1234:5678:9abc:1c2e:aaaa:bbbb:cccc prefixlen 64 autoconf secured
\tinet6 2a02:1234:5678:9abc:7d1f:1111:2222:3333 prefixlen 64 autoconf temporary
\tnd6 options=201<PERFORMNUD,DAD>
\tmedia: autoselect
\tstatus: active
awdl0: flags=8943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::a4b1:22ff:fe33:4455%awdl0 prefixlen 64 scopeid 0xd
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff
utun5: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 81.2.69.142 --> 81.2.69.1 netmask 0xff000000
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
\tmember: vmenet0 flags=3<LEARNING,DISCOVER>
"""


def iface_set(text=IFCONFIG):
    return {str(i) for i in parse_ifconfig(text)}


# ---------------------------------------------------------------- reading ifconfig and route

def test_ifconfig_is_read_for_every_address_and_its_prefix_in_both_families():
    got = iface_set()
    assert {"127.0.0.1/8", "::1/128", "fe80::1/64", "192.168.1.23/24", "192.168.64.1/24", "10.8.0.2/32"} <= got
    assert "2a02:1234:5678:9abc:1c2e:aaaa:bbbb:cccc/64" in got and "fe80::1c2e:aaaa:bbbb:cccc/64" in got
    assert "81.2.69.142/8" in got  # a public address on a tunnel, with a prefix of its own


def test_the_far_end_of_a_point_to_point_link_is_an_address_of_its_own():
    got = iface_set()
    assert "10.8.0.1/32" in got and "81.2.69.1/32" in got


@pytest.mark.parametrize("line", [
    "\tinet 192.168.1.23 netmask 0xff00ff00 broadcast 192.168.1.255",   # a netmask with holes in it
    "\tinet 192.168.1.999 netmask 0xffffff00",                          # not an address
    "\tinet 192.168.1.23",                                              # no netmask
    "\tinet6 2a02::1 prefixlen 129",                                    # a prefix over 128
    "\tinet6 2a02::1",                                                  # no prefix
    "\tinet something strange",
])
def test_an_address_line_that_is_not_understood_is_an_error_not_a_gap(line):
    with pytest.raises(NetworkError):
        parse_ifconfig(f"en0: flags=1<UP>\n{line}\n")


def test_lines_without_an_address_are_ignored():
    assert parse_ifconfig("en0: flags=1<UP>\n\tether aa:bb\n\tstatus: active\n\tinet6_not_a_line x\n") == []


ROUTE_DEFAULT = """\
   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0
"""


def test_the_interface_of_a_route_is_read_and_a_route_without_one_is_an_error():
    assert parse_route(ROUTE_DEFAULT) == "en0"
    assert parse_route(ROUTE_DEFAULT.replace("en0", "utun4")) == "utun4"
    for text in ("", "route: writing to routing socket: not in table\n", "   route to: x\n  interface:\n"):
        with pytest.raises(NetworkError):
            parse_route(text)


# ---------------------------------------------------------------- the real lookups

def test_the_real_lookups_are_behind_the_offline_guard():
    with pytest.raises(OfflineError):
        ln.system_interfaces()
    with pytest.raises(OfflineError):
        ln.system_route("default")


class FakeRun:
    """subprocess.run, replaced: records the call, answers from a script."""

    def __init__(self, stdout=b"", returncode=0, exc=None):
        self.stdout, self.returncode, self.exc, self.calls = stdout, returncode, exc, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr=b"")


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(ln, "guard", lambda what: None)


def test_the_real_lookups_run_the_system_tools_by_absolute_path_with_a_clean_environment(live, monkeypatch):
    fake = FakeRun(IFCONFIG.encode())
    monkeypatch.setattr(ln.subprocess, "run", fake)
    assert "192.168.1.23/24" in {str(i) for i in ln.system_interfaces()}
    fake.stdout = ROUTE_DEFAULT.encode()
    assert ln.system_route("default") == "en0" and ln.system_route("93.184.216.34") == "en0"
    assert [c[0] for c in fake.calls] == [["/sbin/ifconfig", "-a"], ["/sbin/route", "-n", "get", "default"],
                                          ["/sbin/route", "-n", "get", "93.184.216.34"]]
    for _argv, kw in fake.calls:
        assert kw["env"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"} and kw["timeout"] <= 10
        assert kw["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("dest", ["", "example.com", "93.184.216.34; reboot", "::1", "-e", "127.1"])
def test_only_default_or_an_ipv4_address_is_ever_handed_to_route(live, monkeypatch, dest):
    fake = FakeRun(ROUTE_DEFAULT.encode())
    monkeypatch.setattr(ln.subprocess, "run", fake)
    with pytest.raises(NetworkError):
        ln.system_route(dest)
    assert fake.calls == []


@pytest.mark.parametrize("fake", [FakeRun(returncode=1), FakeRun(exc=subprocess.TimeoutExpired("route", 5)),
                                  FakeRun(exc=FileNotFoundError("route")), FakeRun(b"no interface here\n")])
def test_a_route_lookup_that_fails_is_a_network_error(live, monkeypatch, fake):
    monkeypatch.setattr(ln.subprocess, "run", fake)
    with pytest.raises(NetworkError):
        ln.system_route("default")


@pytest.mark.parametrize("fake", [FakeRun(returncode=1), FakeRun(exc=subprocess.TimeoutExpired("ifconfig", 5)),
                                  FakeRun(exc=PermissionError("ifconfig")), FakeRun(b"\tinet 10.0.0.5 strange\n")])
def test_an_interface_lookup_that_fails_or_is_not_understood_is_a_network_error(live, monkeypatch, fake):
    monkeypatch.setattr(ln.subprocess, "run", fake)
    with pytest.raises(NetworkError):
        ln.system_interfaces()


# ---------------------------------------------------------------- the checks, with injected interfaces and routes

class Mac:
    """A Mac for the tests: its interfaces, and the interface each destination leaves by (default for the rest)."""

    def __init__(self, interfaces, routes=None, default="en0"):
        self.interfaces, self.routes, self.default = list(interfaces), dict(routes or {}), default
        self.interface_reads = 0
        self.route_calls: list[str] = []

    def read_interfaces(self):
        self.interface_reads += 1
        return self.interfaces

    def route(self, dest):
        self.route_calls.append(dest)
        if dest == "default":
            return self.default
        got = self.routes.get(dest, self.default)
        if isinstance(got, Exception):
            raise got
        return got

    def networks(self):
        return LocalNetworks(self.read_interfaces, self.route)


# the Mac of the tests: Wi-Fi on a LAN whose /24 is a public range (so only this layer can tell it from the Internet), an
# address of its own on that LAN, and a VPN (utun4) with a public address that routes 34.120.0.0/16 to a private network
LAN = "81.2.69.0/24"
HOME = ["81.2.69.142/24", "fe80::1c2e:aaaa:bbbb:cccc/64", "2a02:1234:5678:9abc::5/64", "10.8.0.2/32"]


def mac(**kw):
    routes = {f"34.120.{a}.{b}": "utun4" for a in (0, 5, 255) for b in (1, 77)}
    routes.update(kw.pop("routes", {}))
    return Mac(kw.pop("interfaces", HOME), routes, **kw)


def refused(local, *ips):
    with pytest.raises(Refused) as e:
        run(local.check(list(ips)))
    return e.value


def test_a_public_address_on_the_default_route_and_outside_every_prefix_passes():
    m = mac()
    run(m.networks().check([PUBLIC, "151.101.1.69"]))


@pytest.mark.parametrize("ip", ["81.2.69.1", "81.2.69.77", "81.2.69.142", "81.2.69.254"])
def test_a_destination_inside_a_prefix_of_one_of_the_macs_interfaces_is_the_lan_and_is_refused(ip):
    e = refused(mac().networks(), ip)
    assert (e.reason, e.detail) == ("address", "own_network") and e.description == "not a public address"


def test_the_edges_of_the_lan_prefix_are_the_lan_and_the_next_address_is_not():
    local = mac().networks()
    refused(local, "81.2.69.0")
    refused(local, "81.2.69.255")
    run(local.check(["81.2.68.255", "81.2.70.0"]))


def test_the_macs_own_address_is_refused_with_a_prefix_of_one_address_as_well_as_inside_a_lan():
    m = mac(interfaces=["151.101.1.200/32", "81.2.69.142/24"])  # a public address of its own, and one on the LAN
    assert refused(m.networks(), "151.101.1.200").detail == "own_network"
    assert refused(m.networks(), "81.2.69.142").detail == "own_network"
    run(m.networks().check(["151.101.1.201", "151.101.1.199"]))  # and its neighbours are not it


def test_the_far_end_of_a_point_to_point_link_is_refused_too():
    ifaces = parse_ifconfig(IFCONFIG)
    local = LocalNetworks(lambda: ifaces, lambda dest: "en0")
    assert refused(local, "10.8.0.1").detail == "own_network" and refused(local, "81.2.69.1").detail == "own_network"


@pytest.mark.parametrize("ip", ["34.120.0.1", "34.120.5.77", "34.120.255.1"])
def test_a_public_range_that_a_vpn_routes_elsewhere_is_refused(ip):
    m = mac()
    e = refused(m.networks(), ip)
    assert (e.reason, e.detail) == ("address", "other_route")
    assert m.route_calls == ["default", ip]


def test_one_bad_address_among_good_ones_refuses_the_lot():
    refused(mac().networks(), PUBLIC, "34.120.0.1", "151.101.1.69")
    refused(mac().networks(), PUBLIC, "81.2.69.20")


def test_the_macs_own_ipv6_networks_are_refused_too_if_one_is_ever_asked_about():
    """IPv6 never gets this far today (the policy refuses it first), but both families are read, so the check stays right."""
    local = mac().networks()
    for ip in ("2a02:1234:5678:9abc::99", "2a02:1234:5678:9abc::5", "fe80::1c2e:aaaa:bbbb:cccc", "fe80::7"):
        assert refused(local, ip).detail == "own_network", ip
    assert refused(local, "2606:4700:4700::1111").reason == "network"  # no way to read an IPv6 route: fail closed


def test_an_ipv4_address_is_not_confused_with_an_ipv6_prefix():
    run(mac(interfaces=["2a02:1234::5/16"]).networks().check([PUBLIC]))  # the IPv6 prefix covers no IPv4 address


# ---- fail closed

def test_a_route_lookup_that_fails_refuses_the_destination():
    m = mac(routes={"8.8.8.8": RuntimeError("route: writing to routing socket: not in table")})
    e = refused(m.networks(), "8.8.8.8")
    assert (e.reason, e.detail) == ("network", "unavailable") and e.description == "couldn't check this Mac's network"


@pytest.mark.parametrize("bad", ["", None, 7])
def test_a_route_that_names_no_interface_refuses_the_destination(bad):
    class Odd(Mac):
        def route(self, dest):
            return "en0" if dest == "default" else bad
    assert refused(Odd(HOME).networks(), PUBLIC).reason == "network"


def test_a_default_route_that_cannot_be_read_refuses_everything():
    m = mac()
    m.route = lambda dest: (_ for _ in ()).throw(NetworkError("no default route")) if dest == "default" else "en0"
    local = m.networks()
    assert refused(local, PUBLIC).reason == "network" and refused(local, "151.101.1.69").reason == "network"


def test_an_interface_list_that_cannot_be_read_refuses_everything():
    def broken():
        raise NetworkError("exit 1")
    local = LocalNetworks(broken, lambda dest: "en0")
    assert refused(local, PUBLIC).reason == "network"


def test_a_nonsense_interface_entry_refuses_everything():
    local = LocalNetworks(lambda: ["not an address"], lambda dest: "en0")
    assert refused(local, PUBLIC).reason == "network"


# ---- once per render

def test_the_interfaces_and_the_default_route_are_read_once_and_each_address_is_looked_up_once():
    m = mac()
    local = m.networks()
    for _ in range(3):
        run(local.check([PUBLIC]))
        run(local.check(["151.101.1.69", PUBLIC]))
        refused(local, "34.120.0.1")
    assert m.interface_reads == 1
    assert m.route_calls == ["default", PUBLIC, "151.101.1.69", "34.120.0.1"]


def test_a_failure_is_remembered_for_the_render_and_not_asked_again():
    m = mac(routes={"8.8.8.8": RuntimeError("no")})
    local = m.networks()
    for _ in range(3):
        assert refused(local, "8.8.8.8").reason == "network"
    assert m.route_calls == ["default", "8.8.8.8"]
    bad = Mac(HOME)
    bad.read_interfaces = lambda: (_ for _ in ()).throw(NetworkError("exit 1"))
    broken = bad.networks()
    for _ in range(3):
        assert refused(broken, PUBLIC).reason == "network"
    assert bad.route_calls == []


def test_two_destinations_asked_at_the_same_moment_read_the_interfaces_once():
    gate = threading.Event()
    m = mac()
    real = m.read_interfaces

    def slow():
        gate.wait(1)
        return real()
    m.read_interfaces = slow
    local = m.networks()

    async def both():
        async def go(ip):
            await local.check([ip])
        task = asyncio.gather(go(PUBLIC), go("151.101.1.69"), go("8.8.8.8"))
        await asyncio.sleep(0.05)
        gate.set()
        await task
    run(both())
    assert m.interface_reads == 1


def test_the_lookups_run_off_the_event_loop():
    seen = []
    local = LocalNetworks(lambda: [], lambda dest: (seen.append(threading.current_thread() is threading.main_thread()), "en0")[1])
    run(local.check([PUBLIC]))
    assert seen and not any(seen)  # a slow `route` never stops the proxy from answering its other connections


def test_a_lookup_that_hangs_is_a_mac_that_cannot_say(monkeypatch):
    import time
    monkeypatch.setattr(ln, "CHECK_TIMEOUT_S", 0.2)
    local = LocalNetworks(lambda: [], lambda dest: (time.sleep(0.7), "en0")[1])  # (asyncio.run waits for the worker at the end)
    started = time.monotonic()
    e = refused(local, PUBLIC)
    assert (e.reason, e.detail) == ("network", "timeout") and time.monotonic() - started < 3


def test_the_proxy_does_not_wait_for_a_lookup_of_the_macs_networks_that_is_still_running_when_it_stops():
    """The lookups run in daemon threads that nothing joins (`proxy.DaemonExecutor`): stop() returns at once, the worker ends on
    its own, and neither the next render's Chrome nor the process's exit is held back by it (security re-review N2; before
    that the proxy waited for the worker, without a limit)."""
    import time
    before = {t.ident for t in threading.enumerate()}
    local = LocalNetworks(lambda: [], lambda dest: (time.sleep(0.5), "en0")[1])  # two lookups of 0.5 s each

    async def resolver(host, port):
        return [PUBLIC]
    p = FilteringProxy(Policy(local=local), resolver=resolver, connector=Connector())
    p.start()
    s = socket.socket()
    s.connect(("127.0.0.1", p.port))
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    time.sleep(0.15)  # the lookup is under way in a worker thread...
    s.close()
    began = time.monotonic()
    p.stop()          # ...and the proxy is stopped (a render that ended) before it is done
    assert time.monotonic() - began < 0.4  # it did not wait for the 1 s the lookup needs
    leftovers = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert leftovers and all(t.daemon for t in leftovers)  # still running, a daemon, nothing joins it...
    deadline = time.monotonic() + 5
    while any(t.is_alive() for t in leftovers) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(t.is_alive() for t in leftovers)  # ...and it ends by itself


def test_the_offline_guard_is_not_taken_for_a_refusal():
    def guarded():
        raise OfflineError("a real step was reached")
    with pytest.raises(OfflineError):
        run(LocalNetworks(guarded, lambda dest: "en0").check([PUBLIC]))
    with pytest.raises(OfflineError):
        run(LocalNetworks(lambda: [], lambda dest: (_ for _ in ()).throw(OfflineError("x"))).check([PUBLIC]))


# ---------------------------------------------------------------- through the policy

class Resolver:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def __call__(self, host, port):
        self.calls.append((host, port))
        return self.answers[0]


def pin(policy, host, answers, port=443):
    return run(policy.pin(host, port, Resolver(answers)))


def test_a_name_that_resolves_into_the_lan_prefix_is_refused_with_its_reason_in_the_policys_words():
    policy = Policy(local=mac().networks())
    for host, answers in (("nas.example.com", ["81.2.69.50"]), ("self.example.com", ["81.2.69.142"]),
                          ("work.example.com", ["34.120.5.77"]), ("both.example.com", [PUBLIC, "34.120.5.77"])):
        with pytest.raises(Refused) as e:
            pin(policy, host, answers)
        assert e.value.reason == "address", host
    assert pin(policy, "example.com", [PUBLIC]).addresses == (PUBLIC,)


def test_an_ip_literal_is_checked_against_the_macs_networks_too():
    policy = Policy(local=mac().networks())
    for literal in ("81.2.69.77", "81.2.69.142", "34.120.5.77"):
        with pytest.raises(Refused):
            pin(policy, literal, [])
    assert pin(policy, PUBLIC, []).addresses == (PUBLIC,)


def test_a_fail_closed_lookup_refuses_the_name_with_the_network_reason():
    m = mac(routes={PUBLIC: RuntimeError("route: not in table")})
    with pytest.raises(Refused) as e:
        pin(Policy(local=m.networks()), "example.com", [PUBLIC])
    assert e.value.reason == "network"


def test_the_static_checks_come_first_so_a_private_address_costs_no_lookup():
    m = mac()
    for host, answers in (("a.example.com", ["10.0.0.5"]), ("b.example.com", ["2606:4700::1"]), ("c.example.com", ["127.0.0.1"])):
        with pytest.raises(Refused):
            pin(Policy(local=m.networks()), host, answers)
    with pytest.raises(Refused):
        pin(Policy(local=m.networks()), "192.168.1.5", [])
    assert m.interface_reads == 0 and m.route_calls == []


def test_a_listed_network_is_refused_before_the_macs_networks_are_asked():
    m = mac()
    with pytest.raises(Refused) as e:
        pin(Policy(refused=(ipaddress.ip_network(f"{PUBLIC}/32"),), local=m.networks()), "example.com", [PUBLIC])
    assert e.value.detail == "listed" and m.interface_reads == 0


def test_the_test_allowance_skips_the_macs_networks():
    m = mac()
    policy = Policy(allow=frozenset({("81.2.69.77", 4555)}), local=m.networks())
    assert pin(policy, "81.2.69.77", [], port=4555).addresses == ("81.2.69.77",)
    assert m.interface_reads == 0


def test_offline_policy_never_asks():
    m = mac()
    with pytest.raises(Refused) as e:
        pin(Policy(offline=True, local=m.networks()), "example.com", [PUBLIC])
    assert e.value.reason == "offline" and m.interface_reads == 0


# ---------------------------------------------------------------- through the real proxy

def talk(port, raw):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(raw)
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                return data
            data += chunk
            if b"\r\n\r\n" in data:
                return data


class Connector:
    def __init__(self):
        self.calls = []

    async def __call__(self, ip, port):
        self.calls.append((ip, port))
        raise OSError("no road in a test")


def test_the_proxy_refuses_the_lan_the_mac_itself_and_the_vpn_range_and_connects_to_none_of_them():
    connector = Connector()
    answers = {"nas.example.com": "81.2.69.50", "me.example.com": "81.2.69.142", "work.example.com": "34.120.5.77"}

    async def resolver(host, port):
        return [answers.get(host, PUBLIC)]
    p = FilteringProxy(Policy(local=mac().networks()), resolver=resolver, connector=connector)
    p.start()
    try:
        for target in ("nas.example.com:443", "me.example.com:443", "work.example.com:443", "81.2.69.77:443",
                       "34.120.0.1:80"):
            raw = talk(p.port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            assert raw.startswith(b"HTTP/1.1 403") and raw.endswith(b"refused: address\n"), (target, raw)
        assert connector.calls == [] and p.log.reasons == {"address": 5}
        # the control: a destination outside all of it is let through (to the connector, which has no road)
        talk(p.port, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert connector.calls == [(PUBLIC, 443)]
    finally:
        p.stop()


def test_the_proxy_refuses_with_the_network_reason_when_the_mac_cannot_say():
    connector = Connector()
    m = mac()
    m.route = lambda dest: (_ for _ in ()).throw(RuntimeError("route is broken"))

    async def resolver(host, port):
        return [PUBLIC]
    p = FilteringProxy(Policy(local=m.networks()), resolver=resolver, connector=connector)
    p.start()
    try:
        raw = talk(p.port, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert raw.startswith(b"HTTP/1.1 403") and raw.endswith(b"refused: network\n")
        assert connector.calls == []
    finally:
        p.stop()
