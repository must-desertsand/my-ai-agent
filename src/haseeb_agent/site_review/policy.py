"""Which destinations a reviewed page may reach. This is the security core of the site review.

A page that Chrome renders runs scripts and can make the browser ask for ANY address, this Mac's own services (the
dashboard on 127.0.0.1:8765, the claude-mem worker on 127.0.0.1:37777) and the LAN included. Chrome therefore sends all
its traffic through the filtering proxy (`proxy.py`), and the proxy asks this module, for every connection, one question:
may I connect to this host and port? The answer comes back as the addresses to connect to, already resolved and
checked, or as a `Refused` with a short fixed reason. Nothing else decides.

What is refused
  - any port but 80 and 443;
  - every address that is not a public, globally routable one: private (10/8, 172.16/12, 192.168/16), loopback,
    link-local (169.254/16), multicast, reserved, unspecified, CGNAT 100.64/10, documentation and benchmarking ranges;
  - IPv6, altogether (reason `ipv6`): names are resolved for IPv4 only, an IPv6 literal is refused whatever it is, and a
    name that only has AAAA records is refused. The router, the printers, the NAS and the Mac itself usually have global
    IPv6 addresses that look "public" to a class check, and nothing here can tell them from a site;
  - IP-literal hosts that are not public, and any IP literal written in an odd form (`0x7f.1`, `2130706433`, `127.1`);
  - a host name that resolves to ANY non-public address (one bad answer among good ones refuses the host: a rebinding
    resolver's mixed answers get nothing), and names that can only mean an internal network (single labels, `localhost`,
    `*.local`, `*.internal`, ...);
  - Haseeb's own networks (`localnet.py`): every address inside a prefix configured on one of this Mac's interfaces, this
    Mac's own addresses, and every address whose route leaves by an interface other than the default route's (a VPN split
    tunnel, any other routed network that has public-looking addresses); a failed lookup of the interfaces or of a route
    refuses (fail closed). `Policy.refused` adds the networks of the setting `site_review_refused_networks`;
  - everything, in offline mode (the HTML attachment's render, which has no business on any network).

Pinning. A host name is resolved ONCE, by the proxy itself, here; the answers are checked and the caller connects to the
IP addresses it was given, never to the name again. So a DNS rebinding answer that changes between a check and a
connect cannot swap the destination: there is no second lookup.

A `Policy` has no way to loosen a rule except `allow`, a set of exact (ip, port) pairs that the tests inject to let a
local test page through. Production code never passes it.

What stays open: public addresses that only the network behind the DEFAULT ROUTE can reach. The route check compares an address's
interface with the default route's, so anything that leaves by the default route passes. That is (1) the public IPv4 address of
the network the Mac is on (many home routers answer on it from inside, and port forwards apply), (2) public subnets that the
network behind the default gateway keeps for itself beyond the Mac's own prefix (the provider's, an office's or a campus's own
ranges, a lab behind a home router), and (3) the internal public ranges of a full-tunnel VPN that takes the default route itself.
All of them are reachable until they are listed in `site_review_refused_networks` (or the review is switched off while on such a
VPN); docs/architecture.md says so too.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..offline import OfflineError, guard

if TYPE_CHECKING:  # localnet.py raises Refused, so it imports this module; the annotation needs no import at run time
    from .localnet import LocalNetworks

PORTS = (80, 443)
MAX_ADDRESSES = 8  # the first answers of a resolver; the rest are never looked at
RESOLVE_TIMEOUT_S = 8

# Addresses that Python's `is_global` calls global but that no page has any business reaching.
_EXTRA_REFUSED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "192.88.99.0/24",    # the old 6to4 relay anycast
    "100.64.0.0/10",     # CGNAT (shared address space)
    "198.18.0.0/15",     # benchmarking
    "240.0.0.0/4",       # reserved, and the broadcast address
))
_EXTRA_REFUSED_V6 = tuple(ipaddress.ip_network(n) for n in (
    "2001::/32",          # Teredo
    "2002::/16",          # 6to4
    "2001:db8::/32",      # documentation
    "3fff::/20",          # documentation
    "100::/64",           # discard-only
    "2001:2::/48",        # benchmarking
    "2001:10::/28",       # ORCHID
    "2001:20::/28",       # ORCHIDv2
    "64:ff9b:1::/48",     # local-use NAT64
    "fec0::/10",          # deprecated site-local
    "::/96",              # IPv4-compatible (deprecated), :: and ::1 included
))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_ULA = ipaddress.ip_network("fc00::/7")

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_NAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
_INTERNAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan", ".home", ".corp",
                      ".private", ".home.arpa", ".invalid", ".onion")

# what the page shows when it is told why (the DM line names one of these, never a host or an address)
DESCRIPTIONS = {
    "offline": "the page may not use a network",
    "port": "port not allowed",
    "address": "not a public address",
    "ipv6": "IPv6 addresses are not used",
    "network": "couldn't check this Mac's network",
    "name": "not a public web address",
    "odd_ip": "not a public address",
    "resolve": "the address could not be resolved",
    "credentials": "the link has a password in it",
    "scheme": "not an http(s) link",
}


class Refused(Exception):
    """A destination the policy refuses. `reason` is a short fixed code (a key of DESCRIPTIONS) and `detail` a fixed
    word for logs ("loopback", "cgnat", ...); neither ever holds page content or a host name."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail

    @property
    def description(self) -> str:
        return DESCRIPTIONS.get(self.reason, "refused")


def _v4_refusal(ip: ipaddress.IPv4Address) -> str | None:
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip in _CGNAT:
        return "cgnat"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if any(ip in net for net in _EXTRA_REFUSED_V4) or not ip.is_global:
        return "reserved"
    return None


def address_refusal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address may not be connected to (a short fixed word), or None when it is a public, globally routable
    address. A transition form is judged by the IPv4 address it carries. This is the class check only. `Policy` refuses
    every IPv6 address before it asks (see the module doc), so the IPv6 half is a second line that costs nothing and is
    there if IPv6 ever comes back (a NAT64-only network, say)."""
    if isinstance(ip, ipaddress.IPv4Address):
        return _v4_refusal(ip)
    if ip in _MAPPED:  # ::ffff:a.b.c.d is the IPv4 address a.b.c.d
        inner = _v4_refusal(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        return None if inner is None else f"mapped_{inner}"
    if ip in _NAT64:  # 64:ff9b::a.b.c.d, the NAT64 form of an IPv4 address
        inner = _v4_refusal(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        return None if inner is None else f"nat64_{inner}"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip in _ULA:
        return "ula"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if any(ip in net for net in _EXTRA_REFUSED_V6) or not ip.is_global:
        return "reserved"
    return None


def literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host written as an IP literal stands for, or None when it is not one. A literal in any odd form that
    the system resolver would still read as a number (`0x7f.1`, `2130706433`, `127.1`, a zone id) raises Refused."""
    h = host.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
        if ":" not in h:
            raise Refused("odd_ip", "brackets")
    if "%" in h:
        raise Refused("odd_ip", "zone_id")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if ":" in h:
        raise Refused("odd_ip", "ipv6_form")  # colons, but not an address Python accepts: not a name either
    try:
        socket.inet_aton(h)  # the forms getaddrinfo accepts as numbers: decimal, octal, hex, 1-3 parts
    except (OSError, ValueError):  # not a number (ValueError: an embedded NUL; the name check will refuse it)
        return None
    raise Refused("odd_ip", "ipv4_form")


def check_name(host: str) -> str:
    """The host name in its plain form, or Refused. Lower case, no trailing dot, ASCII (a name with other letters
    reaches here in its punycode form, since Chrome sends it so)."""
    h = host.strip().lower()
    if h.endswith("."):  # the fully-qualified form: one dot, not many
        h = h[:-1]
    if not h or len(h) > 253 or not _NAME.fullmatch(h):
        raise Refused("name", "form")
    if h == "localhost" or any(h.endswith(s) for s in _INTERNAL_SUFFIXES):
        raise Refused("name", "internal")
    return h


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _has_aaaa(loop: asyncio.AbstractEventLoop, host: str, port: int) -> bool:
    """Whether the name has IPv6 addresses (asked only after it had no IPv4 ones, to say why it is refused)."""
    try:
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET6, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos)


async def system_resolver(host: str, port: int) -> list[str]:
    """The IPv4 addresses the system resolver gives a name, in its order, at most MAX_ADDRESSES, without repeats. IPv4
    only (`family=AF_INET`): a name that has only IPv6 addresses is refused here as `ipv6`, a name that has none at all
    raises the resolver's own error. A real DNS lookup, so the offline guard comes first."""
    guard("site review DNS lookup")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        if await _has_aaaa(loop, host, port):
            raise Refused("ipv6") from None
        raise
    out: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0]).split("%", 1)[0]
        if ip not in out:
            out.append(ip)
        if len(out) >= MAX_ADDRESSES:
            break
    return out


@dataclass(frozen=True)
class Pinned:
    """A destination that passed: connect to these addresses (in order) on this port, and to nothing else."""
    host: str
    addresses: tuple[str, ...]
    port: int


def _is_ipv6(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv6Address)
    except ValueError:
        return False  # not an address at all: check_address refuses it


@dataclass(frozen=True)
class Policy:
    ports: tuple[int, ...] = PORTS
    offline: bool = False  # refuse everything (the HTML attachment's render)
    allow: frozenset[tuple[str, int]] = field(default_factory=frozenset)  # exact (ip, port) pairs: tests only
    # the networks of `site_review_refused_networks` (what the module docstring says stays open: the network's own public
    # address, public subnets behind the default gateway, a full-tunnel VPN's internal public ranges): refused like private space
    refused: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    # this Mac's own networks and routes (localnet.py), read once per render; None: not consulted (tests)
    local: "LocalNetworks | None" = None

    def check_port(self, port: int) -> None:
        if self.offline:
            raise Refused("offline")
        if port not in self.ports and not any(p == port for _ip, p in self.allow):
            raise Refused("port", str(port))

    def check_address(self, ip: str, port: int) -> None:
        """Refused unless `ip` is a public IPv4 address (or an exact pair of `allow`). IPv6 is refused whatever it is. This is
        the part of the question that needs no lookup of this Mac's networks (`pin` asks `local` after it)."""
        if self.offline:
            raise Refused("offline")
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise Refused("address", "unparsable") from None
        if (str(addr), port) in self.allow:
            return
        if isinstance(addr, ipaddress.IPv6Address):
            raise Refused("ipv6")
        why = address_refusal(addr)
        if why is not None:
            raise Refused("address", why)
        if any(net.version == 4 and addr in net for net in self.refused):
            raise Refused("address", "listed")

    async def _check_local(self, addresses: list[str], port: int) -> None:
        """This Mac's own networks and routes, for addresses that passed `check_address`."""
        if self.local is None:
            return
        todo = [a for a in addresses if (a, port) not in self.allow]
        if todo:
            await self.local.check(todo)

    def check_url_parts(self, scheme: str, host: str, port: int | None, has_credentials: bool) -> int:
        """The part of the question that needs no lookup, for a URL about to be opened. Returns the port to use."""
        if self.offline:
            raise Refused("offline")
        if scheme not in ("http", "https"):
            raise Refused("scheme", scheme[:12])
        if has_credentials:
            raise Refused("credentials")
        use = port if port is not None else (80 if scheme == "http" else 443)
        self.check_port(use)
        ip = literal(host)
        if ip is not None:
            self.check_address(str(ip), use)
        else:
            check_name(host)
        return use

    async def pin(self, host: str, port: int, resolver: Resolver = system_resolver) -> Pinned:
        """May a connection to host:port be made? Pinned (the addresses to connect to) or Refused. A name is resolved
        here, once; every answer must be public, or the whole host is refused."""
        if self.offline:
            raise Refused("offline")
        self.check_port(port)
        ip = literal(host)
        if ip is not None:
            self.check_address(str(ip), port)
            await self._check_local([str(ip)], port)
            return Pinned(str(ip), (str(ip),), port)
        name = check_name(host)
        try:
            answers = list(await asyncio.wait_for(resolver(name, port), RESOLVE_TIMEOUT_S))[:MAX_ADDRESSES]
        except (Refused, OfflineError):
            raise  # a refusal is the answer; the offline guard is a bug in a test, and must not look like a refusal
        except Exception:  # noqa: BLE001 - a resolver that fails is a destination that cannot be reached
            raise Refused("resolve") from None
        if not answers:
            raise Refused("resolve")
        v4 = [a for a in answers if not _is_ipv6(a)]  # IPv4 only: an IPv6 answer is never connected to
        if not v4:
            raise Refused("ipv6")  # a name that has only IPv6 addresses
        for a in v4:
            self.check_address(a, port)  # ANY non-public answer refuses the host
        await self._check_local(v4, port)
        return Pinned(name, tuple(v4), port)
