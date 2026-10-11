"""Which physical network the machine is on — to notice when that changes.

The engine's routing is tied to the interface that was the default when the
tunnel came up. When the machine moves — the cable is pulled and Wi-Fi takes
over — the tunnel has to be rebuilt on the new interface, and the window does
that as soon as it is told (main_window._NetworkChangeWatchdog).

Until v4.1.2 it never was told. The check asked the system which local
address it would use to reach the VPN server; under a TUN the system's answer
is the tunnel itself, so the address was always the tunnel's 10.255.0.2,
before the move and after.

So the routing table is read instead: the default routes that are not ours,
the best of them, and the interface, its address and the gateway it leads to.
That triple is the fingerprint, taken when the tunnel comes up. The network
has changed when no default route with that fingerprint is left — the
interface is gone, or it has another address or gateway now. A second network
appearing next to the one in use is not a change, and neither is the system
merely preferring another one while ours is still there (Windows re-ranks
interfaces as the Wi-Fi link speed moves; reconnecting on every re-rank
would be a loop).

Reading is cheap (one system call, or one small file) — it runs every few
seconds for as long as a session lasts.
"""
from __future__ import annotations

import socket
import struct
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

# fingerprint() on a platform with no reader below: the caller falls back.
UNSUPPORTED = object()


@dataclass(frozen=True)
class Route:
    """One IPv4 default route."""
    iface: object       # interface index (Windows) or name (elsewhere)
    gateway: str
    metric: int


def usable(default_routes: Iterable[Route], addresses: dict, tun_v4: str,
           tun_names: tuple = ()) -> list:
    """Fingerprints of the physical networks among `default_routes`, the one
    the system prefers first. Empty if there is none right now (in the middle
    of a switch). `addresses` maps an interface to its IPv4 address(es); the
    tunnel is recognised by its address or by its name."""
    def addrs(iface) -> tuple:
        got = addresses.get(iface, ())
        return (got,) if isinstance(got, str) else tuple(got)

    ours = [r for r in default_routes
            if tun_v4 not in addrs(r.iface) and str(r.iface) not in tun_names]
    ours.sort(key=lambda r: (r.metric, str(r.iface)))
    seen: list = []
    for r in ours:
        fp = f"{r.iface}|{','.join(sorted(addrs(r.iface)))}|{r.gateway}"
        if fp not in seen:
            seen.append(fp)
    return seen


def pick(default_routes: Iterable[Route], addresses: dict, tun_v4: str,
         tun_names: tuple = ()) -> Optional[str]:
    """The fingerprint of the network the system prefers, or None."""
    found = usable(default_routes, addresses, tun_v4, tun_names)
    return found[0] if found else None


def _ip(dword: int) -> str:
    return socket.inet_ntoa(struct.pack("<L", dword))


def _windows() -> tuple[list, dict]:
    import ctypes
    from ctypes import wintypes

    class _RouteRow(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in (
            "dest", "mask", "policy", "next_hop", "if_index", "type", "proto", "age",
            "next_hop_as", "metric1", "metric2", "metric3", "metric4", "metric5")]

    class _AddrRow(ctypes.Structure):
        _fields_ = [("addr", wintypes.DWORD), ("if_index", wintypes.DWORD),
                    ("mask", wintypes.DWORD), ("bcast", wintypes.DWORD),
                    ("reasm", wintypes.DWORD), ("unused", wintypes.USHORT),
                    ("type", wintypes.USHORT)]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    insufficient_buffer = 122

    def table(call, row):
        size = wintypes.ULONG(0)
        call(None, ctypes.byref(size), False)
        for _ in range(3):                      # the table can grow between the two calls
            buf = ctypes.create_string_buffer(max(size.value, ctypes.sizeof(wintypes.DWORD)))
            rc = call(buf, ctypes.byref(size), False)
            if rc == 0:
                count = wintypes.DWORD.from_buffer(buf).value
                return list((row * count).from_buffer(buf, ctypes.sizeof(wintypes.DWORD)))
            if rc != insufficient_buffer:
                raise OSError(rc, "route table could not be read")
        raise OSError(insufficient_buffer, "route table kept changing")

    routes = [Route(r.if_index, _ip(r.next_hop), r.metric1)
              for r in table(iphlpapi.GetIpForwardTable, _RouteRow)
              if r.dest == 0 and r.mask == 0]
    addresses: dict = {}
    for a in table(iphlpapi.GetIpAddrTable, _AddrRow):
        if a.addr:
            addresses.setdefault(a.if_index, []).append(_ip(a.addr))
    return routes, addresses


def _interface_addresses() -> dict:
    try:
        import psutil
    except Exception:
        return {}
    found: dict = {}
    for name, items in psutil.net_if_addrs().items():
        for item in items:
            if item.family == socket.AF_INET and item.address:
                found.setdefault(name, []).append(item.address)
    return found


def _parse_proc_route(text: str) -> list:
    """Default routes from Linux's /proc/net/route (the main table — the
    policy table our own TUN routes live in is not in it)."""
    routes = []
    for line in text.splitlines()[1:]:          # after the header line
        f = line.split()
        # Iface Destination Gateway Flags RefCnt Use Metric Mask ...
        if len(f) >= 8 and f[1] == "00000000" and f[7] == "00000000":
            try:
                routes.append(Route(f[0], _ip(int(f[2], 16)), int(f[6])))
            except ValueError:
                continue
    return routes


def _linux() -> tuple[list, dict]:
    with open("/proc/net/route", encoding="ascii", errors="replace") as fh:
        return _parse_proc_route(fh.read()), _interface_addresses()


def _parse_netstat(text: str) -> list:
    """Default routes from macOS's `netstat -rn -f inet`: the primary one
    and the interface-scoped ones (flag I) of every other network that is up
    — so a network that is still there but no longer preferred is still
    listed. Tunnels (utunN) and on-link pseudo-gateways (link#N) are not
    physical networks."""
    routes = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or f[0] != "default":
            continue
        gateway, flags, iface = f[1], f[2], f[3]
        if iface.startswith("utun") or gateway.startswith("link#"):
            continue
        routes.append(Route(iface, gateway, (1 if "I" in flags else 0) * 1000 + len(routes)))
    return routes


def _macos() -> tuple[list, dict]:
    import subprocess
    out = subprocess.run(["/usr/sbin/netstat", "-rn", "-f", "inet"],
                         capture_output=True, text=True, timeout=3).stdout
    return _parse_netstat(out), _interface_addresses()


def fingerprints(tun_v4: str, tun_names: tuple = ()):
    """Fingerprints of the physical networks available now, the preferred one
    first; None when they cannot be read right now (callers treat that as "no
    signal", never as a change); UNSUPPORTED where this platform has no
    reader."""
    try:
        if sys.platform == "win32":
            routes, addresses = _windows()
        elif sys.platform.startswith("linux"):
            routes, addresses = _linux()
        elif sys.platform == "darwin":
            routes, addresses = _macos()
        else:
            return UNSUPPORTED
    except Exception:
        return None
    return usable(routes, addresses, tun_v4, tun_names)
