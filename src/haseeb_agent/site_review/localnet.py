"""Haseeb's own networks, as the leash of the site review sees them.

The class of an address says nothing about whose it is. On a network with IPv6 the router, the printers, a NAS and this
Mac itself have global addresses that look "public"; a VPN can route a public-looking range to a private network; and a
home network's own public IPv4 address is answered by the router from inside. A page that Chrome renders can ask for any
of them, on port 80 or 443, through the proxy. `policy.py` therefore refuses IPv6 altogether, and asks this module about
every IPv4 address it is about to pin:

  1. Is it inside a prefix configured on one of this Mac's interfaces, or one of this Mac's own addresses (or the far end
     of a point-to-point link)? That is the LAN, whatever its addresses look like. Refused (`own_network`).
  2. Does its route leave by an interface other than the default route's? That is a VPN split tunnel, or any other routed
     network. Refused (`other_route`).
  3. Could this Mac not say? Refused (`network`): the leash fails closed.

Both families are read, so the check stays right if IPv6 is ever let in. What is read, and how:
  - the interfaces: `ifconfig -a`, parsed strictly (a line of `inet` or `inet6` that is not understood is an error, not a
    gap in the list);
  - the routes: `route -n get default` once, and `route -n get <ipv4 address>` once per address, cached.
Both are read lazily, on the first destination of a render, and kept for the render: the Mac's networks do not change in the
45 seconds it takes. The two lookups are injectable (`interfaces=`, `route=`); the defaults are the real ones, behind the
offline guard, so a test that forgets to replace them fails loudly instead of running `ifconfig`.

What stays open: public addresses that only the network behind the DEFAULT ROUTE can reach, which nothing here can learn without
asking an outside service. The route check compares an address's interface with the default route's, so anything that leaves by
the default route passes: (1) the network's own public IPv4 address (the router answers on it from inside), (2) public subnets that
the network behind the default gateway keeps for itself beyond the Mac's own prefix (the provider's, an office's or a campus's own
ranges, a lab behind a home router), and (3) the internal public ranges of a full-tunnel VPN that takes the default route itself
(the default interface is then the tunnel). They are refused only when listed in `site_review_refused_networks`; a VPN that
sends everything through a tunnel with two `/1` routes and leaves the default on Wi-Fi is the opposite case: every address looks
routed away, and the check refuses all of them (it fails closed).
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import subprocess
import threading
from collections.abc import Callable, Iterable

from ..offline import OfflineError, guard
from .policy import Refused

IFCONFIG = "/sbin/ifconfig"
ROUTE = "/sbin/route"
LOOKUP_TIMEOUT_S = 5.0
CHECK_TIMEOUT_S = 12.0   # one address, all lookups it needs (the interfaces and the default route the first time)
OUTPUT_LIMIT = 1_000_000
_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}

Interface = ipaddress.IPv4Interface | ipaddress.IPv6Interface
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
InterfaceProvider = Callable[[], Iterable["Interface | str"]]   # the Mac's addresses with their prefixes
RouteProvider = Callable[[str], str]                            # "default" or an IPv4 address -> the interface it leaves by


class NetworkError(Exception):
    """This Mac could not say what its networks or routes are. The leash fails closed. The text is a fixed phrase."""


# ------------------------------------------------------------------ the real lookups

def _run(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, timeout=LOOKUP_TIMEOUT_S, check=False, env=_ENV,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise NetworkError(type(e).__name__) from None
    if done.returncode != 0:
        raise NetworkError(f"exit {done.returncode}")
    return done.stdout[:OUTPUT_LIMIT].decode("utf-8", "replace")


_INET_LINE = re.compile(r"^\s+inet6?\s")
_INET4 = re.compile(r"^\s+inet\s+(?P<addr>\d{1,3}(?:\.\d{1,3}){3})(?:\s+-->\s+(?P<peer>\d{1,3}(?:\.\d{1,3}){3}))?"
                    r"\s+netmask\s+(?P<mask>0x[0-9a-fA-F]{1,8}|\d{1,3}(?:\.\d{1,3}){3})(?:\s|$)")
_INET6 = re.compile(r"^\s+inet6\s+(?P<addr>[0-9a-fA-F:.]+)(?:%[A-Za-z0-9._-]+)?(?:\s+-->\s+\S+)?"
                    r"\s+prefixlen\s+(?P<len>\d{1,3})(?:\s|$)")


def parse_ifconfig(text: str) -> list[Interface]:
    """Every address in the output of `ifconfig -a` (macOS), with its prefix; the far end of a point-to-point link comes as
    a host address of its own. NetworkError for a line that has an address in it and cannot be read."""
    out: list[Interface] = []
    for line in text.splitlines():
        if not _INET_LINE.match(line):
            continue
        try:
            m4 = _INET4.match(line)
            if m4:
                mask = m4["mask"]
                dotted = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
                out.append(ipaddress.IPv4Interface(f"{m4['addr']}/{dotted}"))
                if m4["peer"]:
                    out.append(ipaddress.IPv4Interface(f"{m4['peer']}/32"))
                continue
            m6 = _INET6.match(line)
            if m6:
                out.append(ipaddress.IPv6Interface(f"{m6['addr']}/{m6['len']}"))
                continue
        except ValueError:  # a bad address, a netmask with holes in it, a prefix over 128
            pass
        raise NetworkError("an address line of ifconfig was not understood")
    return out


def system_interfaces() -> list[Interface]:
    """The addresses on this Mac's interfaces, all of them, up or not. A real step (a process), so the offline guard comes
    first."""
    guard("site review interface lookup")
    return parse_ifconfig(_run([IFCONFIG, "-a"]))


_ROUTE_INTERFACE = re.compile(r"^\s*interface:\s*([A-Za-z0-9._-]{1,32})\s*$", re.MULTILINE)


def parse_route(text: str) -> str:
    """The interface a `route -n get` answer says the packets leave by. NetworkError when there is none."""
    m = _ROUTE_INTERFACE.search(text)
    if m is None:
        raise NetworkError("no interface in the route")
    return m.group(1)


def system_route(dest: str) -> str:
    """The interface a packet to `dest` ("default" or an IPv4 address) leaves by: `route -n get`. A real step, so the
    offline guard comes first."""
    guard("site review route lookup")
    if dest != "default":
        try:
            ipaddress.IPv4Address(dest)
        except ValueError:
            raise NetworkError("not an IPv4 address") from None
    return parse_route(_run([ROUTE, "-n", "get", dest]))


# ------------------------------------------------------------------ one render's view of this Mac

class LocalNetworks:
    """What this Mac's networks and routes look like, read once (on first use) and kept for one render. `check` refuses an
    address that belongs to the Mac's own networks or leaves by another route than the default's, and refuses when the Mac
    could not say. Thread-safe: the lookups run in worker threads, and a second caller waits for the first."""

    def __init__(self, interfaces: InterfaceProvider | None = None, route: RouteProvider | None = None):
        self._interfaces = interfaces or system_interfaces
        self._route = route or system_route
        self._lock = threading.Lock()
        self._nets: tuple[Network, ...] | None = None
        self._default = ""
        self._failure: NetworkError | None = None
        self._routes: dict[str, str | NetworkError] = {}

    # the lookups (worker threads)

    def _load(self) -> None:
        with self._lock:
            if self._nets is not None:
                return
            if self._failure is not None:
                raise self._failure
            try:
                nets: list[Network] = []
                for item in self._interfaces():
                    nets.append(ipaddress.ip_interface(item).network)  # the prefix holds the Mac's own address too
                default = self._route("default")
                if not isinstance(default, str) or not default:
                    raise NetworkError("no default route")
            except OfflineError:
                raise
            except Exception:  # noqa: BLE001 - whatever went wrong, this Mac could not say
                self._failure = NetworkError("the interfaces or the default route could not be read")
                raise self._failure from None
            self._nets, self._default = tuple(nets), default

    def _route_of(self, ip: str) -> str:
        with self._lock:
            known = self._routes.get(ip)
            if known is None:
                try:
                    got = self._route(ip)
                    known = got if isinstance(got, str) and got else NetworkError("no interface in the route")
                except OfflineError:
                    raise
                except Exception:  # noqa: BLE001
                    known = NetworkError("the route could not be read")
                self._routes[ip] = known
        if isinstance(known, NetworkError):
            raise known
        return known

    def _check_one(self, ip: str) -> None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise Refused("address", "unparsable") from None
        try:
            self._load()
            assert self._nets is not None
            if any(net.version == addr.version and addr in net for net in self._nets):
                raise Refused("address", "own_network")
            if addr.version != 4:
                raise NetworkError("routes are read for IPv4 only")  # never reached today: policy refuses IPv6 first
            interface = self._route_of(ip)
        except NetworkError:
            raise Refused("network", "unavailable") from None
        if interface != self._default:
            raise Refused("address", "other_route")

    # the question (the proxy's event loop)

    async def check(self, addresses: Iterable[str]) -> None:
        """Refused if any of these addresses belongs to this Mac's own networks, leaves by another route than the default's,
        or could not be looked at."""
        loop = asyncio.get_running_loop()
        for ip in addresses:
            try:
                await asyncio.wait_for(loop.run_in_executor(None, self._check_one, ip), CHECK_TIMEOUT_S)
            except asyncio.TimeoutError:  # a lookup that hangs (its own timeout is shorter) is a Mac that cannot say
                raise Refused("network", "timeout") from None
