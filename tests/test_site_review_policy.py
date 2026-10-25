"""The destination policy of the site review (haseeb_agent/site_review/policy.py): which addresses, ports and names a
rendered page may reach. Offline: the resolver is always a fake, so no test looks anything up."""
from __future__ import annotations

import asyncio
import ipaddress

import pytest

from haseeb_agent.offline import OfflineError
from haseeb_agent.site_review import policy as pol
from haseeb_agent.site_review.policy import Policy, Refused, address_refusal, check_name, literal

PUBLIC_V4 = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "151.101.1.69", "76.76.21.21"]
PUBLIC_V6 = ["2001:4860:4860::8888", "2606:4700:4700::1111", "2a00:1450:4001:81b::200e"]


def run(coro):
    return asyncio.run(coro)


class Resolver:
    """A resolver that answers from a script and counts its lookups."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host, port):
        self.calls.append((host, port))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


# ---------------------------------------------------------------- the address rules

@pytest.mark.parametrize("ip,why", [
    ("127.0.0.1", "loopback"), ("127.255.255.254", "loopback"), ("::1", "loopback"),
    ("10.0.0.1", "private"), ("10.255.255.255", "private"), ("172.16.0.1", "private"), ("172.31.255.255", "private"),
    ("192.168.0.1", "private"), ("192.168.255.255", "private"),
    ("169.254.169.254", "link_local"), ("169.254.0.1", "link_local"), ("fe80::1", "link_local"),
    ("224.0.0.1", "multicast"), ("239.255.255.250", "multicast"), ("ff02::1", "multicast"),
    ("0.0.0.0", "unspecified"), ("::", "unspecified"),
    ("100.64.0.1", "cgnat"), ("100.127.255.254", "cgnat"),
    ("fc00::1", "ula"), ("fd12:3456:789a::1", "ula"),
    ("240.0.0.1", "reserved"), ("255.255.255.255", "private"), ("192.0.2.7", "private"), ("198.51.100.1", "private"),
    ("203.0.113.9", "private"), ("198.18.0.1", "private"), ("192.88.99.1", "reserved"),
    ("2001:db8::1", "reserved"), ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "reserved"),  # documentation, Teredo
    ("::ffff:127.0.0.1", "mapped_loopback"), ("::ffff:10.1.2.3", "mapped_private"),
    ("::ffff:192.168.1.1", "mapped_private"), ("::ffff:169.254.169.254", "mapped_link_local"),
    ("::ffff:100.64.0.9", "mapped_cgnat"), ("::ffff:0.0.0.0", "mapped_unspecified"),
    ("64:ff9b::7f00:1", "nat64_loopback"), ("64:ff9b::c0a8:101", "nat64_private"),  # NAT64 of 127.0.0.1, 192.168.1.1
    ("::7f00:1", "reserved"),  # IPv4-compatible, deprecated
])
def test_a_non_public_address_is_refused_with_its_reason(ip, why):
    got = address_refusal(ipaddress.ip_address(ip))
    assert got is not None and (got == why or why == "reserved" and got in ("reserved", "private")), (ip, got)


@pytest.mark.parametrize("ip", ["2002:7f00:1::", "2002:c0a8:101::", "2002:808:808::"])
def test_6to4_is_refused_whatever_it_carries(ip):
    assert address_refusal(ipaddress.ip_address(ip)) is not None


@pytest.mark.parametrize("ip", PUBLIC_V4 + PUBLIC_V6 + ["::ffff:8.8.8.8", "64:ff9b::808:808"])
def test_a_public_address_is_allowed(ip):
    assert address_refusal(ipaddress.ip_address(ip)) is None


def test_the_edges_of_every_private_block_are_refused_and_the_next_address_is_not():
    refused = ["10.0.0.0", "10.255.255.255", "172.16.0.0", "172.31.255.255", "192.168.0.0", "192.168.255.255",
               "100.64.0.0", "100.127.255.255", "169.254.0.0", "169.254.255.255", "127.0.0.0", "127.255.255.255"]
    allowed = ["9.255.255.255", "11.0.0.0", "172.15.255.255", "172.32.0.0", "192.167.255.255", "192.169.0.0",
               "100.63.255.255", "100.128.0.0", "169.253.255.255", "169.255.0.0", "126.255.255.255", "128.0.0.0"]
    assert all(address_refusal(ipaddress.ip_address(a)) for a in refused)
    assert not any(address_refusal(ipaddress.ip_address(a)) for a in allowed)


# ---------------------------------------------------------------- literals and names

@pytest.mark.parametrize("host,want", [("8.8.8.8", "8.8.8.8"), ("[2001:4860:4860::8888]", "2001:4860:4860::8888"),
                                       ("2001:4860:4860::8888", "2001:4860:4860::8888"), ("127.0.0.1", "127.0.0.1")])
def test_a_literal_is_read_for_what_it_is(host, want):
    assert str(literal(host)) == want


@pytest.mark.parametrize("host", ["example.com", "a.b.c.example.org", "xn--80ak6aa92e.com", "1e3.example.com",
                                  "0x7f.example.com"])
def test_a_name_is_not_a_literal(host):
    assert literal(host) is None


@pytest.mark.parametrize("host", ["0x7f.0.0.1", "0x7f000001", "2130706433", "127.1", "127.0.1", "0177.0.0.1",
                                  "017700000001", "1.2.3", "3232235777", "0300.0250.0.1", "fe80::1%en0",
                                  "[127.0.0.1]", "::1%lo0", "[fe80::1%25en0]", "12345"])
def test_a_literal_in_an_odd_form_is_refused(host):
    with pytest.raises(Refused) as e:
        literal(host)
    assert e.value.reason == "odd_ip"


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "foo.localhost", "printer.local", "nas.lan", "db.internal",
                                  "x.corp", "x.home.arpa", "intranet", "router", "a", "single", "", ".", "a..b.com",
                                  "-a.example.com", "a-.example.com", "exa mple.com", "exa_mple.com", "example.com/x",
                                  "user@example.com", "exämple.com", "a" * 64 + ".com", ("a." * 130) + "com",
                                  "x.onion", "example.com:80", "ex\x00ample.com"])
def test_a_name_that_is_not_a_public_web_address_is_refused(host):
    with pytest.raises(Refused) as e:
        check_name(host)
    assert e.value.reason == "name"


@pytest.mark.parametrize("host,want", [("Example.COM", "example.com"), ("example.com.", "example.com"),
                                       ("xn--80ak6aa92e.com", "xn--80ak6aa92e.com"), ("a.b", "a.b"),
                                       ("sub-domain.example.co.uk", "sub-domain.example.co.uk")])
def test_a_public_name_is_allowed_in_its_plain_form(host, want):
    assert check_name(host) == want


# ---------------------------------------------------------------- a URL before it is opened

@pytest.mark.parametrize("scheme,host,port,creds,reason", [
    ("ftp", "example.com", None, False, "scheme"), ("file", "example.com", None, False, "scheme"),
    ("javascript", "example.com", None, False, "scheme"), ("data", "example.com", None, False, "scheme"),
    ("https", "example.com", None, True, "credentials"),
    ("https", "example.com", 8443, False, "port"), ("http", "example.com", 8080, False, "port"),
    ("http", "example.com", 3000, False, "port"), ("https", "example.com", 22, False, "port"),
    ("http", "127.0.0.1", None, False, "address"), ("https", "127.0.0.1", 443, False, "address"),
    ("http", "192.168.1.5", None, False, "address"), ("http", "[::1]", None, False, "ipv6"),
    ("http", "[2606:4700:4700::1111]", None, False, "ipv6"), ("https", "[::ffff:8.8.8.8]", 443, False, "ipv6"),
    ("http", "169.254.169.254", None, False, "address"), ("http", "10.0.0.8", 80, False, "address"),
    ("http", "2130706433", None, False, "odd_ip"), ("http", "0x7f.1", None, False, "odd_ip"),
    ("http", "localhost", None, False, "name"), ("https", "intranet", None, False, "name"),
])
def test_a_url_that_may_not_be_opened_is_refused_before_anything_is_looked_up(scheme, host, port, creds, reason):
    with pytest.raises(Refused) as e:
        Policy().check_url_parts(scheme, host, port, creds)
    assert e.value.reason == reason


def test_a_public_url_gets_its_port():
    p = Policy()
    assert p.check_url_parts("https", "example.com", None, False) == 443
    assert p.check_url_parts("http", "example.com", None, False) == 80
    assert p.check_url_parts("http", "example.com", 80, False) == 80
    assert p.check_url_parts("https", "8.8.8.8", 443, False) == 443


# ---------------------------------------------------------------- pinning

def test_a_name_is_resolved_once_and_the_answers_are_what_the_caller_connects_to():
    r = Resolver(["93.184.216.34", "151.101.1.69"])
    got = run(Policy().pin("Example.com", 443, r))
    assert got == pol.Pinned("example.com", ("93.184.216.34", "151.101.1.69"), 443)
    assert r.calls == [("example.com", 443)]  # one lookup, never a second one to be swapped


@pytest.mark.parametrize("answers", [["10.0.0.5"], ["127.0.0.1"], ["192.168.1.9"], ["169.254.169.254"],
                                     ["100.64.1.1"], ["0.0.0.0"],
                                     ["93.184.216.34", "10.0.0.5"],  # one bad answer among good ones
                                     ["10.0.0.5", "93.184.216.34"],
                                     ["93.184.216.34", "2606:2800:220:1::1", "192.168.0.1"]])
def test_a_name_that_resolves_to_any_non_public_address_is_refused_whole(answers):
    with pytest.raises(Refused) as e:
        run(Policy().pin("example.com", 443, Resolver(answers)))
    assert e.value.reason == "address"


def test_a_rebinding_resolver_gets_the_first_answer_pinned_and_the_second_refused():
    """DNS rebinding: public the first time, private the second. The caller connects to what the first lookup said and
    never asks again; a fresh request that is answered with the private address is refused."""
    r = Resolver(["93.184.216.34"], ["127.0.0.1"])
    first = run(Policy().pin("rebind.example.com", 443, r))
    assert first.addresses == ("93.184.216.34",) and len(r.calls) == 1
    with pytest.raises(Refused):
        run(Policy().pin("rebind.example.com", 443, r))
    assert len(r.calls) == 2


@pytest.mark.parametrize("answers", [[], None])
def test_a_name_with_no_answer_is_refused_as_unresolved(answers):
    async def nothing(host, port):
        if answers is None:
            raise OSError("no such host")
        return answers
    with pytest.raises(Refused) as e:
        run(Policy().pin("example.com", 443, nothing))
    assert e.value.reason == "resolve"


def test_the_offline_guard_in_a_resolver_is_not_taken_for_a_refusal():
    async def guarded(host, port):
        raise OfflineError("blocked")
    with pytest.raises(OfflineError):
        run(Policy().pin("example.com", 443, guarded))


def test_the_system_resolver_is_behind_the_offline_guard():
    with pytest.raises(OfflineError):
        run(pol.system_resolver("example.com", 443))


@pytest.mark.parametrize("port", [0, 21, 22, 25, 81, 443 + 1, 3000, 8080, 8443, 8765, 37777, 65535])
def test_any_other_port_is_refused_before_a_lookup(port):
    r = Resolver(["93.184.216.34"])
    with pytest.raises(Refused) as e:
        run(Policy().pin("example.com", port, r))
    assert e.value.reason == "port" and r.calls == []


def test_the_dashboard_and_the_memory_worker_ports_are_refused_on_every_loopback_spelling():
    for host in ("127.0.0.1", "[::1]", "localhost", "0x7f.1", "2130706433", "127.1", "[::ffff:127.0.0.1]"):
        for port in (8765, 37777, 80, 443):
            r = Resolver(["127.0.0.1"])
            with pytest.raises(Refused):
                run(Policy().pin(host, port, r))
            assert r.calls == []  # none of these is ever resolved: they are refused on their face


def test_an_ip_literal_is_checked_and_never_resolved():
    r = Resolver(["10.0.0.1"])
    got = run(Policy().pin("93.184.216.34", 80, r))
    assert got.addresses == ("93.184.216.34",) and r.calls == []
    with pytest.raises(Refused):
        run(Policy().pin("192.168.1.1", 80, r))
    assert r.calls == []


def test_offline_refuses_everything_without_a_lookup():
    r = Resolver(["93.184.216.34"])
    off = Policy(offline=True)
    for host, port in (("example.com", 443), ("93.184.216.34", 80), ("127.0.0.1", 80), ("example.com", 80)):
        with pytest.raises(Refused) as e:
            run(off.pin(host, port, r))
        assert e.value.reason == "offline"
    with pytest.raises(Refused):
        off.check_url_parts("https", "example.com", None, False)
    with pytest.raises(Refused):
        off.check_address("93.184.216.34", 443)
    assert r.calls == []


def test_the_test_allowance_lets_exactly_the_pairs_it_names_through():
    p = Policy(allow=frozenset({("127.0.0.1", 4555)}))
    assert run(p.pin("127.0.0.1", 4555, Resolver([]))).addresses == ("127.0.0.1",)
    for host, port in (("127.0.0.1", 4556), ("127.0.0.1", 80), ("localhost", 4555), ("10.0.0.1", 4555)):
        with pytest.raises(Refused):
            run(p.pin(host, port, Resolver(["127.0.0.1"])))
    # the allowance is not on by default
    with pytest.raises(Refused):
        run(Policy().pin("127.0.0.1", 4555, Resolver([])))


def test_a_refusal_carries_a_fixed_reason_and_never_the_host():
    with pytest.raises(Refused) as e:
        run(Policy().pin("secret-host.example.com", 443, Resolver(["10.1.2.3"])))
    assert "secret-host" not in str(e.value) and "10.1.2.3" not in e.value.reason
    assert e.value.description == "not a public address"


# ---------------------------------------------------------------- IPv4 only (security review I1)

IPV6_LITERALS = ["[::1]", "[2001:4860:4860::8888]", "2606:4700:4700::1111", "[2a00:1450:4001:81b::200e]", "[fe80::1]",
                 "[fd12:3456:789a::1]", "[::ffff:8.8.8.8]", "[::ffff:127.0.0.1]", "[64:ff9b::808:808]", "[2002:808:808::]",
                 "[::]", "[2001:db8::1]"]


@pytest.mark.parametrize("host", IPV6_LITERALS)
def test_an_ipv6_literal_is_refused_as_ipv6_whatever_it_is_and_never_looked_up(host):
    r = Resolver(["93.184.216.34"])
    with pytest.raises(Refused) as e:
        run(Policy().pin(host, 443, r))
    assert e.value.reason == "ipv6" and e.value.description == "IPv6 addresses are not used" and r.calls == []
    with pytest.raises(Refused) as e:
        Policy().check_url_parts("https", host, None, False)
    assert e.value.reason == "ipv6"


@pytest.mark.parametrize("ip", ["::1", "2001:4860:4860::8888", "::ffff:8.8.8.8", "64:ff9b::808:808", "fe80::1"])
def test_check_address_refuses_every_ipv6_address_as_ipv6(ip):
    with pytest.raises(Refused) as e:
        Policy().check_address(ip, 443)
    assert e.value.reason == "ipv6"


def test_a_name_that_has_only_ipv6_answers_is_refused_as_ipv6():
    for answers in (["2606:2800:220:1:248:1893:25c8:1946"], ["2a00:1450:4001:81b::200e", "2606:4700:4700::1111"]):
        with pytest.raises(Refused) as e:
            run(Policy().pin("aaaa-only.example.com", 443, Resolver(answers)))
        assert e.value.reason == "ipv6"


def test_a_name_with_both_kinds_of_answer_is_pinned_to_its_ipv4_addresses_only():
    got = run(Policy().pin("both.example.com", 443, Resolver(["2606:2800:220:1::1", "93.184.216.34", "2606:2800::2"])))
    assert got.addresses == ("93.184.216.34",)


def test_a_private_ipv4_answer_still_refuses_the_whole_host_when_ipv6_answers_come_with_it():
    with pytest.raises(Refused) as e:
        run(Policy().pin("mixed.example.com", 443, Resolver(["2606:2800::1", "93.184.216.34", "10.0.0.5"])))
    assert e.value.reason == "address"


def resolver_with(monkeypatch, script):
    """The system resolver, with the event loop's getaddrinfo replaced by `script(host, port, family)` (a list of
    sockaddrs, or an exception): no DNS lookup is made. Returns the log of the families asked for."""
    asked: list[int] = []

    async def fake(self, host, port, *, family=0, type=0, proto=0, flags=0):
        asked.append(family)
        got = script(host, port, family)
        if isinstance(got, Exception):
            raise got
        return [(family, type, 6, "", (a, port)) for a in got]
    monkeypatch.setattr(pol, "guard", lambda what: None)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake)
    return asked


def test_the_system_resolver_asks_for_ipv4_only(monkeypatch):
    import socket
    asked = resolver_with(monkeypatch, lambda h, p, f: ["93.184.216.34", "93.184.216.34", "151.101.1.69"])
    assert run(pol.system_resolver("example.com", 443)) == ["93.184.216.34", "151.101.1.69"]
    assert asked == [socket.AF_INET]  # never AF_UNSPEC: an AAAA record is never looked at when there is an A record


def test_a_name_with_only_an_aaaa_record_is_refused_as_ipv6_by_the_system_resolver(monkeypatch):
    import socket
    asked = resolver_with(monkeypatch, lambda h, p, f: ["2606:2800:220:1::1"] if f == socket.AF_INET6
                          else socket.gaierror(8, "nodename nor servname provided, or not known"))
    with pytest.raises(Refused) as e:
        run(Policy().pin("aaaa-only.example.com", 443, pol.system_resolver))
    assert e.value.reason == "ipv6" and asked == [socket.AF_INET, socket.AF_INET6]


def test_a_name_that_does_not_exist_is_still_unresolved_for_the_system_resolver(monkeypatch):
    import socket
    asked = resolver_with(monkeypatch, lambda h, p, f: socket.gaierror(8, "nodename nor servname provided, or not known"))
    with pytest.raises(Refused) as e:
        run(Policy().pin("nowhere.example.com", 443, pol.system_resolver))
    assert e.value.reason == "resolve" and asked == [socket.AF_INET, socket.AF_INET6]


def test_the_offline_guard_comes_before_the_second_lookup_too(monkeypatch):
    with pytest.raises(OfflineError):
        run(pol.system_resolver("example.com", 443))  # the real guard: nothing is asked, for either family


# ---------------------------------------------------------------- the configured networks (security review I1)

LISTED = ipaddress.ip_network("93.184.216.0/24")


def test_an_address_in_a_listed_network_is_refused_like_private_space():
    p = Policy(refused=(LISTED, ipaddress.ip_network("151.101.1.69/32"), ipaddress.ip_network("2001:db8:1::/48")))
    for ip in ("93.184.216.0", "93.184.216.34", "93.184.216.255", "151.101.1.69"):
        with pytest.raises(Refused) as e:
            p.check_address(ip, 443)
        assert (e.value.reason, e.value.detail) == ("address", "listed"), ip
        assert e.value.description == "not a public address"
    for ip in ("93.184.215.255", "93.184.217.0", "151.101.1.68", "151.101.1.70", "8.8.8.8"):
        p.check_address(ip, 443)  # the neighbours are not listed


def test_a_listed_network_refuses_a_literal_a_name_and_the_whole_host_when_one_answer_is_in_it():
    p = Policy(refused=(LISTED,))
    with pytest.raises(Refused) as e:
        p.check_url_parts("https", "93.184.216.34", None, False)  # before anything is looked up
    assert e.value.detail == "listed"
    with pytest.raises(Refused):
        run(p.pin("93.184.216.34", 80, Resolver([])))
    with pytest.raises(Refused) as e:
        run(p.pin("example.com", 443, Resolver(["93.184.216.34"])))
    assert (e.value.reason, e.value.detail) == ("address", "listed")
    with pytest.raises(Refused):
        run(p.pin("example.com", 443, Resolver(["8.8.8.8", "93.184.216.9"])))
    assert run(p.pin("example.com", 443, Resolver(["8.8.8.8"]))).addresses == ("8.8.8.8",)


def test_nothing_is_listed_by_default():
    assert Policy().refused == () and Policy().local is None
    Policy().check_address("93.184.216.34", 443)


def test_the_test_allowance_comes_before_the_list():
    p = Policy(refused=(ipaddress.ip_network("127.0.0.0/8"),), allow=frozenset({("127.0.0.1", 4555)}))
    assert run(p.pin("127.0.0.1", 4555, Resolver([]))).addresses == ("127.0.0.1",)
