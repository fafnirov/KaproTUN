"""Windows route + DNS manipulation for TUN-mode setup.

All operations need admin privileges.

For route add/delete the module talks straight to `CreateIpForwardEntry` /
`DeleteIpForwardEntry` in iphlpapi.dll via ctypes. Each call is ~100 µs;
the older `route add` shell route was ~10 ms per call. With ~9 k bypass
routes for the full geoip:ru list, that's the difference between 1 second
and 90 seconds at connect time.

Session pattern: callers build up a list of routes/DNS changes through
RouteSession, then `restore()` undoes them all at disconnect (or atexit if
the app crashes).
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import ctypes.wintypes as wt
import json
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- Win32 native route API (iphlpapi.dll) --------------------------------

class _MIB_IPFORWARDROW(ctypes.Structure):
    _fields_ = [
        ("dwForwardDest",      wt.DWORD),
        ("dwForwardMask",      wt.DWORD),
        ("dwForwardPolicy",    wt.DWORD),
        ("dwForwardNextHop",   wt.DWORD),
        ("dwForwardIfIndex",   wt.DWORD),
        ("dwForwardType",      wt.DWORD),
        ("dwForwardProto",     wt.DWORD),
        ("dwForwardAge",       wt.DWORD),
        ("dwForwardNextHopAS", wt.DWORD),
        ("dwForwardMetric1",   wt.DWORD),
        ("dwForwardMetric2",   wt.DWORD),
        ("dwForwardMetric3",   wt.DWORD),
        ("dwForwardMetric4",   wt.DWORD),
        ("dwForwardMetric5",   wt.DWORD),
    ]


_MIB_IPROUTE_TYPE_INDIRECT = 4
_MIB_IPPROTO_NETMGMT = 3
_NO_ERROR = 0
_ERROR_ALREADY_EXISTS = 183           # exact duplicate (same dest+mask+next_hop+proto)
_ERROR_NOT_FOUND = 1168
_ERROR_OBJECT_ALREADY_EXISTS = 5010   # same dest+mask but different proto/origin —
                                       # e.g. a stale entry from a dead TUN adapter
                                       # pointing at an ifIndex that no longer exists

if sys.platform == "win32":
    _iphlpapi = ctypes.windll.iphlpapi
    _CreateIpForwardEntry = _iphlpapi.CreateIpForwardEntry
    _CreateIpForwardEntry.argtypes = [ctypes.POINTER(_MIB_IPFORWARDROW)]
    _CreateIpForwardEntry.restype = wt.DWORD
    _DeleteIpForwardEntry = _iphlpapi.DeleteIpForwardEntry
    _DeleteIpForwardEntry.argtypes = [ctypes.POINTER(_MIB_IPFORWARDROW)]
    _DeleteIpForwardEntry.restype = wt.DWORD
else:
    _CreateIpForwardEntry = None
    _DeleteIpForwardEntry = None


def _ip_to_dword_network_order(ip: str) -> int:
    """Pack a dotted IP into a 32-bit value in network byte order."""
    return struct.unpack("=I", socket.inet_aton(ip))[0]


def _build_row(dest: str, mask: str, gateway: str, if_index: int,
               metric: int = 1) -> _MIB_IPFORWARDROW:
    row = _MIB_IPFORWARDROW()
    row.dwForwardDest = _ip_to_dword_network_order(dest)
    row.dwForwardMask = _ip_to_dword_network_order(mask)
    row.dwForwardPolicy = 0
    row.dwForwardNextHop = _ip_to_dword_network_order(gateway)
    row.dwForwardIfIndex = if_index
    row.dwForwardType = _MIB_IPROUTE_TYPE_INDIRECT
    row.dwForwardProto = _MIB_IPPROTO_NETMGMT
    row.dwForwardAge = 0
    row.dwForwardNextHopAS = 0
    row.dwForwardMetric1 = metric
    row.dwForwardMetric2 = 0xFFFFFFFF
    row.dwForwardMetric3 = 0xFFFFFFFF
    row.dwForwardMetric4 = 0xFFFFFFFF
    row.dwForwardMetric5 = 0xFFFFFFFF
    return row


def _create_route_native(dest: str, mask: str, gateway: str, if_index: int,
                         metric: int = 1) -> int:
    """Returns 0 (NO_ERROR) on success, or a Win32 error code."""
    if _CreateIpForwardEntry is None:
        return 1
    row = _build_row(dest, mask, gateway, if_index, metric)
    return int(_CreateIpForwardEntry(ctypes.byref(row)))


def _delete_route_native(dest: str, mask: str, gateway: str, if_index: int,
                         metric: int = 1) -> int:
    if _DeleteIpForwardEntry is None:
        return 1
    row = _build_row(dest, mask, gateway, if_index, metric)
    return int(_DeleteIpForwardEntry(ctypes.byref(row)))


# --- low-level shell wrappers ---------------------------------------------

def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _ps(script: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a single PowerShell command and return (rc, stdout, stderr)."""
    return _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )


# --- discovery ------------------------------------------------------------

@dataclass
class InterfaceInfo:
    name: str               # User-facing alias, e.g. "Беспроводная сеть" or "tun0"
    index: int              # ifIndex
    gateway: str = ""       # IPv4 gateway, empty for TUN
    interface_metric: int = 25  # base metric of the adapter itself


def _get_interface_metric_v4(if_index: int) -> int:
    """Read the IPv4 interface metric for a given adapter index.

    Important for CreateIpForwardEntry — m1 in MIB_IPFORWARDROW is the
    *stored* metric, not the route metric, and Windows rejects (rc=160,
    ERROR_BAD_ARGUMENTS) any value lower than the interface's own metric.
    """
    rc, out, _ = _ps(
        f"Get-NetIPInterface -InterfaceIndex {if_index} -AddressFamily IPv4 "
        f"-ErrorAction SilentlyContinue "
        f"| Select-Object -ExpandProperty InterfaceMetric"
    )
    out = out.strip()
    if not out:
        return 25  # WiFi default — safe ceiling for most systems
    try:
        return int(out.splitlines()[0])
    except (ValueError, IndexError):
        return 25


def get_default_route_v4() -> Optional[InterfaceInfo]:
    """Find the IPv4 default-route interface BEFORE we add our TUN routes."""
    rc, out, err = _ps(
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue "
        "| Sort-Object RouteMetric "
        "| Select-Object -First 1 -Property InterfaceAlias,InterfaceIndex,NextHop "
        "| ConvertTo-Json -Compress"
    )
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    idx = int(data.get("InterfaceIndex", 0))
    return InterfaceInfo(
        name=str(data.get("InterfaceAlias", "")),
        index=idx,
        gateway=str(data.get("NextHop", "")),
        interface_metric=_get_interface_metric_v4(idx),
    )


def find_interface_by_name(name: str, timeout: float = 8.0) -> Optional[InterfaceInfo]:
    """Poll Get-NetAdapter until an interface with the given alias appears.

    tun2socks creates the TUN asynchronously after launch, so we wait.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, err = _ps(
            f"Get-NetAdapter -Name '{name}' -ErrorAction SilentlyContinue "
            f"| Select-Object -Property Name,ifIndex,Status "
            f"| ConvertTo-Json -Compress"
        )
        if rc == 0 and out.strip():
            try:
                data = json.loads(out)
                return InterfaceInfo(name=data["Name"], index=int(data["ifIndex"]))
            except (json.JSONDecodeError, KeyError):
                pass
        time.sleep(0.4)
    return None


def configure_tun_interface(iface: InterfaceInfo, address: str = "10.255.0.2",
                            mask: str = "255.255.255.0") -> None:
    """Give the TUN interface an IPv4 address and bring it up."""
    _run([
        "netsh", "interface", "ipv4", "set", "address",
        f"name={iface.name}", "source=static",
        f"addr={address}", f"mask={mask}",
    ])


def set_dns(iface_name: str, servers: list[str]) -> None:
    """Pin DNS servers on an interface (replaces DHCP-assigned ones)."""
    if not servers:
        return
    _run([
        "netsh", "interface", "ipv4", "set", "dns",
        f"name={iface_name}", "source=static", f"addr={servers[0]}",
        "register=primary", "validate=no",
    ])
    for s in servers[1:]:
        _run([
            "netsh", "interface", "ipv4", "add", "dns",
            f"name={iface_name}", f"addr={s}", "validate=no",
        ])


def reset_dns(iface_name: str) -> None:
    _run([
        "netsh", "interface", "ipv4", "set", "dns",
        f"name={iface_name}", "source=dhcp",
    ])


# --- route add/remove -----------------------------------------------------

def add_route(dest: str, mask: str, gateway: str, if_index: int,
              metric: int = 1) -> bool:
    rc, out, err = _run([
        "route", "add", dest, "mask", mask, gateway,
        "if", str(if_index), "metric", str(metric),
    ])
    return rc == 0


def delete_route(dest: str, mask: str, gateway: str = "") -> bool:
    args = ["route", "delete", dest, "mask", mask]
    if gateway:
        args.append(gateway)
    rc, out, err = _run(args)
    return rc == 0


# --- session --------------------------------------------------------------

@dataclass
class _DnsEntry:
    iface_name: str


@dataclass
class _RouteEntry:
    dest: str
    mask: str
    gateway: str
    if_index: int = 0
    metric: int = 1


@dataclass
class RouteSession:
    """Tracks routes/DNS changes so they can all be undone on disconnect."""
    routes: list[_RouteEntry] = field(default_factory=list)
    dns_changed: list[_DnsEntry] = field(default_factory=list)
    # Last non-zero rc from a failed CreateIpForwardEntry call. The
    # controller reads this to surface a useful error message instead
    # of "Не удалось добавить host-route" with no detail.
    last_error_rc: int = 0

    def add_route(self, dest: str, mask: str, gateway: str, if_index: int,
                  metric: int = 1) -> bool:
        """Add a single route via the native Win32 API.

        Auto-recovers from two "already exists" variants:

          - ERROR_ALREADY_EXISTS (183): exact duplicate — a previous
            session left a stale row with identical
            (dest, mask, next_hop, proto). Native DeleteIpForwardEntry
            matches by all those fields, so the args we'd reuse for
            create also work for delete.

          - ERROR_OBJECT_ALREADY_EXISTS (5010): same (dest, mask) but
            different proto / ifIndex — typically a /32 left over from
            a TUN adapter that died ungracefully (the route still
            points at a now-dead ifIndex). Native delete CAN'T match
            this — we don't know the dead ifIndex. The shell
            `route delete <dest>` form matches by destination only and
            removes it regardless of next_hop/index/proto.

        In both cases we try native delete first (cheap), then shell
        delete (catches the 5010 mismatched-proto case), then retry
        create. If the retry still fails the caller raises a clean
        error with the rc as a hint.
        """
        rc = _create_route_native(dest, mask, gateway, if_index, metric)
        if rc in (_ERROR_ALREADY_EXISTS, _ERROR_OBJECT_ALREADY_EXISTS):
            _delete_route_native(dest, mask, gateway, if_index, metric)
            if rc == _ERROR_OBJECT_ALREADY_EXISTS:
                # The proto/ifIndex mismatch means native delete almost
                # certainly missed. Fall back to the shell form that
                # ignores those fields. Shelling out is ~10 ms but we
                # only do it on the recovery path — single-digit times
                # per connect, not per-route.
                delete_route(dest, mask)
            rc = _create_route_native(dest, mask, gateway, if_index, metric)
        if rc == _NO_ERROR:
            self.routes.append(_RouteEntry(dest, mask, gateway, if_index, metric))
            return True
        # Stash the last rc on the session so the controller can
        # include it in its error message — turns the generic
        # "Не удалось добавить host-route" into something diagnosable.
        self.last_error_rc = rc
        return False

    def add_bypass_routes(self, ips: list[str], gateway: str, if_index: int,
                          metric: int = 1) -> int:
        """Add /32 host-routes for a list of IPs. Returns count succeeded."""
        ips = sorted({ip for ip in ips if ip})
        if not ips:
            return 0
        entries = [(ip, "255.255.255.255") for ip in ips]
        return self.add_bypass_cidrs(entries, gateway, if_index, metric)

    def add_bypass_cidrs(self, entries: list[tuple[str, str]],
                         gateway: str, if_index: int, metric: int = 1) -> int:
        """Add many (dest, mask) routes via native API.

        ~100 µs per call → 8000+ routes finish in under a second, where
        shelling out to `route add` would take >70 s.
        """
        if not entries:
            return 0
        added = 0
        for dest, mask in entries:
            rc = _create_route_native(dest, mask, gateway, if_index, metric)
            if rc == _NO_ERROR:
                self.routes.append(_RouteEntry(dest, mask, gateway, if_index, metric))
                added += 1
            # Silently skip already-exists / invalid — they're not failures
            # we can do anything useful about per-route.
        return added

    def set_dns(self, iface_name: str, servers: list[str]) -> None:
        set_dns(iface_name, servers)
        self.dns_changed.append(_DnsEntry(iface_name))

    def restore(self) -> None:
        # Reverse order so more-specific routes go away before any catchalls.
        for r in reversed(self.routes):
            try:
                _delete_route_native(r.dest, r.mask, r.gateway, r.if_index, r.metric)
            except Exception:
                pass
        self.routes.clear()
        for d in self.dns_changed:
            try:
                reset_dns(d.iface_name)
            except Exception:
                pass
        self.dns_changed.clear()


# --- domain resolution (for building bypass-route list) -------------------

def resolve_domains_parallel(domains: list[str], workers: int = 32,
                             timeout: float = 3.0) -> dict[str, list[str]]:
    """Resolve domains to IPv4 addresses concurrently.

    Returns {domain: [ips]}, with empty list for domains that fail to resolve.
    Used at TUN-mode connect-time to know which IPs need bypass routes.
    """
    def _resolve_one(d: str) -> tuple[str, list[str]]:
        try:
            socket.setdefaulttimeout(timeout)
            infos = socket.getaddrinfo(d, None, socket.AF_INET)
            return d, sorted({info[4][0] for info in infos})
        except (socket.gaierror, socket.timeout, OSError):
            return d, []
        finally:
            socket.setdefaulttimeout(None)

    results: dict[str, list[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for domain, ips in ex.map(_resolve_one, domains):
            results[domain] = ips
    return results
