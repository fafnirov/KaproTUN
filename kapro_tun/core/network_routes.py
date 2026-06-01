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
    """Run a single PowerShell command and return (rc, stdout, stderr).

    PowerShell writes its stdout in the console's OEM codepage (cp866 on a
    Russian Windows), but _run() decodes as UTF-8 — so any Cyrillic the script
    emits (interface aliases like "Беспроводная сеть") came back mojibake
    ("���"), which then polluted logs and route lookups. Forcing the console
    output encoding to UTF-8 from inside the script makes PS encode what _run
    decodes, so non-ASCII interface names survive the round-trip intact.
    """
    script = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;\n" + script
    return _run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )


def reset_dns_by_index(if_index: int) -> bool:
    """Restore an interface's DNS to DHCP, addressing it by ifIndex.

    Index-based so it works during crash recovery, where the saved interface
    *name* may have been captured with a broken encoding or the alias may have
    changed. Uses the DnsClient cmdlet (resets to automatic/DHCP). Returns
    True on rc 0. Never raises — recovery must stay best-effort.
    """
    try:
        rc, _out, _err = _ps(
            f"Set-DnsClientServerAddress -InterfaceIndex {int(if_index)} "
            f"-ResetServerAddresses -ErrorAction Stop"
        )
        return rc == 0
    except Exception:
        return False


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


def _looks_ipv4(ip: str) -> bool:
    """True only for a dotted-decimal IPv4 literal (digits + dots). Also our
    injection guard before interpolating an IP into a PowerShell command."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts) and all(p.isdigit() for p in parts)
    except ValueError:
        return False


def _info_from_route_json(out: str) -> Optional[InterfaceInfo]:
    """Parse the {InterfaceAlias,InterfaceIndex,NextHop} JSON our route
    queries emit into an InterfaceInfo (with the iface metric filled in)."""
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):  # ConvertTo-Json can emit an array; take first
        data = data[0] if data else {}
    try:
        idx = int(data.get("InterfaceIndex", 0) or 0)
    except (TypeError, ValueError):
        return None
    return InterfaceInfo(
        name=str(data.get("InterfaceAlias", "")),
        index=idx,
        gateway=str(data.get("NextHop", "") or "").strip(),
        interface_metric=_get_interface_metric_v4(idx),
    )


def find_egress_to(remote_ip: str) -> Optional[InterfaceInfo]:
    """Return the interface + next-hop Windows would ACTUALLY use to reach
    `remote_ip` (the VPN server), via the OS route resolver `Find-NetRoute`.

    This is the authoritative egress for TUN mode: the server host-route (loop
    prevention) and the bypass routes MUST go out the same path the OS uses to
    reach the server, or the tunnel's own packets to the server blackhole and
    the user sees "подключено, но трафика нет". Picking "the first 0.0.0.0/0"
    instead is wrong on multi-NIC boxes (Ethernet + Wi-Fi with different
    gateways) and when a virtual adapter (Hyper-V / VMware / Tailscale / a
    Wintun leftover) holds a stale default route.

    Returns None (→ caller falls back to get_default_route_v4) when the lookup
    fails or yields no usable gateway (empty / on-link 0.0.0.0 — never true for
    a real public VPN server, so we don't pin a bogus route).
    """
    if not _looks_ipv4(remote_ip):
        return None
    # Find-NetRoute emits a source-IP object AND the matched route object;
    # only the route object carries NextHop, so filter on it.
    rc, out, _ = _ps(
        f"$r = Find-NetRoute -RemoteIPAddress '{remote_ip}' -ErrorAction "
        f"SilentlyContinue | Where-Object {{ $_.NextHop }} | Select-Object -First 1; "
        f"if ($r) {{ $r | Select-Object InterfaceAlias,InterfaceIndex,NextHop "
        f"| ConvertTo-Json -Compress }}"
    )
    if rc != 0:
        return None
    info = _info_from_route_json(out)
    if info is None or not info.gateway or info.gateway == "0.0.0.0" or not info.index:
        return None
    return info


def get_default_route_v4() -> Optional[InterfaceInfo]:
    """Fallback egress: the IPv4 default-route interface with the LOWEST
    EFFECTIVE metric (RouteMetric + InterfaceMetric — Windows' real
    tie-breaker), restricted to Connected interfaces with a real gateway.

    The old version sorted by RouteMetric ALONE and took the first row — on a
    multi-NIC box (Ethernet + Wi-Fi, both RouteMetric 0) that picked an
    arbitrary interface, and could pick a virtual adapter holding a stale
    0.0.0.0/0. find_egress_to() (server-route based) is preferred; this is the
    fallback when that lookup is unavailable.
    """
    rc, out, _ = _ps(
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue "
        "| Where-Object { $_.NextHop -and $_.NextHop -ne '0.0.0.0' -and "
        "    (Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
        "     -ErrorAction SilentlyContinue).ConnectionState -eq 'Connected' } "
        "| Sort-Object @{Expression={ $_.RouteMetric + "
        "    (Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
        "     -ErrorAction SilentlyContinue).InterfaceMetric }} "
        "| Select-Object -First 1 -Property InterfaceAlias,InterfaceIndex,NextHop "
        "| ConvertTo-Json -Compress"
    )
    if rc != 0 or not out.strip():
        # Last-ditch: the old unfiltered behaviour, so a setup with no
        # 'Connected'-state default route still gets a best-effort answer.
        rc, out, _ = _ps(
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue "
            "| Sort-Object RouteMetric | Select-Object -First 1 "
            "-Property InterfaceAlias,InterfaceIndex,NextHop | ConvertTo-Json -Compress"
        )
        if rc != 0:
            return None
    return _info_from_route_json(out)


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
    """Pin DNS servers on an interface (replaces DHCP-assigned ones).

    Empty list → CLEAR all DNS on this interface (source=static,
    address=none). Windows DNS Client then has nothing to query on
    this NIC and falls through to whatever other interface has DNS
    configured. Used in TUN mode to silence the physical NIC so
    parallel resolution can't leak queries to the ISP-DNS that
    DHCP handed us.
    """
    if not servers:
        # Clear path — v1.16.7. The original empty-list early-return
        # was wrong: it meant 'don't touch' but the caller expected
        # 'wipe'. Explicit netsh now.
        _run([
            "netsh", "interface", "ipv4", "set", "dns",
            f"name={iface_name}", "source=static", "address=none",
        ])
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
                          metric: int = 1) -> tuple[int, int]:
        """Add /32 host-routes for a list of IPs.

        Returns (added, adopted) — see add_bypass_cidrs.
        """
        ips = sorted({ip for ip in ips if ip})
        if not ips:
            return (0, 0)
        entries = [(ip, "255.255.255.255") for ip in ips]
        return self.add_bypass_cidrs(entries, gateway, if_index, metric)

    def add_bypass_cidrs(self, entries: list[tuple[str, str]],
                         gateway: str, if_index: int, metric: int = 1) -> tuple[int, int]:
        """Add many (dest, mask) routes via the native API.

        ~100 µs per call → 8000+ routes finish in under a second, where
        shelling out to `route add` would take >70 s.

        Returns (added, adopted):

          - added: routes freshly created this call.
          - adopted: routes that were ALREADY in the table — almost always
            ours, left over from a prior session the app never got to clean
            up (killed / crashed / power-lost). We register them in THIS
            session so disconnect's restore() removes them too. Without this
            they leak into the routing table indefinitely, and if the user
            later joins a different network (new gateway) the stale entries
            blackhole those destinations until reboot.
        """
        if not entries:
            return (0, 0)
        added = 0
        adopted = 0
        for dest, mask in entries:
            rc = _create_route_native(dest, mask, gateway, if_index, metric)
            if rc == _NO_ERROR:
                self.routes.append(_RouteEntry(dest, mask, gateway, if_index, metric))
                added += 1
            elif rc in (_ERROR_ALREADY_EXISTS, _ERROR_OBJECT_ALREADY_EXISTS):
                # Route already in the table — almost always ours, left over
                # from a prior session that didn't get to clean up. Adopt it:
                # track it so restore() removes it on disconnect. Fixes the
                # cross-session leak (and the network-change blackhole).
                #
                # We accept BOTH 183 and 5010: Windows hands back one or the
                # other for a duplicate depending on version, and measured in
                # the field, CreateIpForwardEntry on this class of box returns
                # 5010 even for an EXACT duplicate (8610/8611 geoip CIDRs in
                # one capture). Treating 5010 as "dead adapter, delete+recreate"
                # (as we do for the single server host-route in add_route) would
                # be tens of seconds of per-route shell-outs here AND would flap
                # split-routing on a live connection. Adopting is instant and
                # non-disruptive; restore() has a shell-delete fallback for the
                # rare genuinely-stale (different-ifIndex) entry.
                self.routes.append(_RouteEntry(dest, mask, gateway, if_index, metric))
                adopted += 1
            # else: genuinely invalid (bad gateway / metric below iface) —
            # nothing useful to do per-route, skip.
        return (added, adopted)

    def set_dns(self, iface_name: str, servers: list[str]) -> None:
        set_dns(iface_name, servers)
        self.dns_changed.append(_DnsEntry(iface_name))

    def restore(self) -> None:
        # Reverse order so more-specific routes go away before any catchalls.
        #
        # Native delete keys on dest+mask+next_hop+ifIndex+proto — NOT metric.
        # Every route we ADOPTED matched on exactly those fields (that's what
        # made CreateIpForwardEntry report a duplicate), so native delete finds
        # and removes it regardless of the metric we stored. That means the
        # common case — including the user's metric-1 leftovers from an older
        # build — cleans up fast with no shell-out.
        #
        # The shell fallback only covers a genuine native miss (e.g. a stale
        # entry on a now-different adapter). It's CAPPED so a pathological run
        # can never turn disconnect into thousands of route.exe spawns
        # (~10 ms each); anything past the cap is left for the next session to
        # re-adopt and clean.
        shell_fallback_budget = 64
        for r in reversed(self.routes):
            try:
                rc = _delete_route_native(r.dest, r.mask, r.gateway, r.if_index, r.metric)
                if rc not in (_NO_ERROR, _ERROR_NOT_FOUND) and shell_fallback_budget > 0:
                    shell_fallback_budget -= 1
                    delete_route(r.dest, r.mask)
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
