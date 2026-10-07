"""Firewall kill-switch — while it is armed, nothing but the VPN connection
and the local network leaves the machine outside the tunnel.

Why it is built from block rules only
-------------------------------------
Until v4.0.0 this module installed "block ALL outbound" plus "allow
sing-box.exe" and relied on the allow winning. It does not: Windows Firewall
evaluates explicit block rules BEFORE allow rules, whatever their specificity,
so the block-all dropped sing-box's own connection to the server as well. A
block rule cannot say "every program except one", and there is no way to raise
an allow above a block from netsh.

So nothing here is an allow. Each rule blocks a slice of traffic that is
shaped so the flows that must pass never match it:

  v4       every protocol, local address NOT the tunnel's own (exactly
           10.255.0.2, not its subnet: an adapter that merely sits in the
           same /30 is not the tunnel), remote address NOT
           (the VPN server | private/loopback/multicast | the network's
           public DNS servers). An app whose route falls back to the real NIC
           is dropped; the same app through the tunnel has the tunnel's local
           address and is not matched; sing-box reaching the server is not
           matched because of the remote address.
  dns-udp  the network's public DNS servers stay reachable — the engine needs
  dns-tcp  them to resolve, and so does a reconnect — but only on port 53.
  v6       global-unicast IPv6 (and the NAT64 prefixes, which reach the
           IPv4 internet) from anything but the tunnel's own address.

The price: sing-box's `direct` outbound (Russian sites, the direct list, games)
leaves through the real NIC too and cannot be told apart from a leak, so it is
blocked like one. A kill-switch session therefore routes nothing direct — see
sing_box_config.build_config(lockdown=True).

Lifetime
--------
Armed on connect and KEPT while the client reconnects on its own (engine
crash, DNS watchdog, network change): that window is the reason a kill-switch
exists. Only a user disconnect, switching the setting off, or quitting removes
it. If KaproTUN itself is killed the rules stay; main.py clears them on the
next launch.

Re-arming for a new server adds the new generation first and removes the old
one after, so there is never a moment without rules.

Windows only (netsh advfirewall), admin required.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from typing import Iterable, Optional

from .sing_box_config import PRIVATE_CIDRS, TUN_DEVICE_NAME, TUN_INET4, TUN_INET6

# All KaproTUN-managed firewall rules share this prefix so cleanup
# can match-and-delete them safely without touching unrelated rules
# the user (or another VPN) might have added.
_RULE_PREFIX = "KaproTUN-killswitch"

# Two generations, so a re-arm can overlap the old rules with the new ones.
_SLOTS = ("a", "b")
# Order matters: "v4" is added first and deleted last, so a generation that
# exists at all — even half-built or half-removed — always has its v4 rule.
# That is the one is_active() looks for.
_KINDS = ("v4", "v6", "dns-udp", "dns-tcp")

# Names used before v4.0.0 (block-all + per-engine allows). Still deleted and
# still detected, so an upgrade over a crashed old session is cleaned up.
_LEGACY_RULES = tuple(f"{_RULE_PREFIX}-{s}" for s in (
    "block", "allow-lan", "allow-xray", "allow-hysteria", "allow-singbox"))

# IPv6 that reaches the public internet: global unicast, plus the NAT64
# prefixes through which an IPv6-only network reaches IPv4 hosts.
_PUBLIC_IPV6 = "2000::/3,64:ff9b::/96,64:ff9b:1::/48"
# The tunnel's own addresses — the host part of TUN_INET4/6, never the subnet.
_TUN_ADDR4 = str(ipaddress.ip_interface(TUN_INET4).ip)
_TUN_ADDR6 = str(ipaddress.ip_interface(TUN_INET6).ip)
_NOT_PORT_53 = "0-52,54-65535"

# Hidden subprocess on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_supported() -> bool:
    """Kill-switch is currently Windows-only — `netsh advfirewall` is
    the easiest cross-version Windows API. Linux/macOS need iptables/
    pfctl wrappers — separate work, future release.
    """
    return sys.platform == "win32"


def rule_name(slot: str, kind: str) -> str:
    return f"{_RULE_PREFIX}-{slot}-{kind}"


def _slot_rules(slot: str) -> list[str]:
    """One generation's rule names, in the order they must be DELETED."""
    return [rule_name(slot, k) for k in reversed(_KINDS)]


def _all_rule_names() -> list[str]:
    return [n for s in _SLOTS for n in _slot_rules(s)] + list(reversed(_LEGACY_RULES))


def _everything_except(spared: Iterable[str], version: int) -> str:
    """The whole address space minus `spared`, as a netsh address list."""
    nets = [ipaddress.ip_network("0.0.0.0/0" if version == 4 else "::/0")]
    for raw in spared:
        hole = ipaddress.ip_network(raw, strict=False)
        if hole.version != version:
            continue
        kept = []
        for net in nets:
            if hole.subnet_of(net):
                kept.extend(net.address_exclude(hole))
            elif not net.subnet_of(hole):
                kept.append(net)
        nets = kept
    return ",".join(str(n) for n in sorted(nets))


def _public_ipv4(addresses: Iterable[str]) -> list[str]:
    """The addresses a private-range exemption does not already cover."""
    private = [ipaddress.ip_network(c) for c in PRIVATE_CIDRS]
    out: list[str] = []
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(str(raw).strip())
        except ValueError:
            continue
        if ip.version == 4 and not any(ip in n for n in private) and str(ip) not in out:
            out.append(str(ip))
    return out


def build_rules(slot: str, *, server_ips: Iterable[str],
                dns_servers: Iterable[str] = ()) -> list[tuple[str, list[str]]]:
    """(name, netsh arguments) for one generation of rules. Pure — no I/O."""
    servers = _public_ipv4(server_ips)
    resolvers = [d for d in _public_ipv4(dns_servers) if d not in servers]
    common = ["dir=out", "action=block", "enable=yes", "profile=any"]
    off_tunnel4 = _everything_except([_TUN_ADDR4], 4)
    rules = [
        (rule_name(slot, "v4"), common + [
            f"localip={off_tunnel4}",
            "remoteip=" + _everything_except([*PRIVATE_CIDRS, *servers, *resolvers], 4),
        ]),
        (rule_name(slot, "v6"), common + [
            "localip=" + _everything_except([_TUN_ADDR6], 6),
            f"remoteip={_PUBLIC_IPV6}",
        ]),
    ]
    if resolvers:
        for kind, proto in (("dns-udp", "UDP"), ("dns-tcp", "TCP")):
            rules.append((rule_name(slot, kind), common + [
                f"protocol={proto}",
                f"localip={off_tunnel4}",
                "remoteip=" + ",".join(resolvers),
                f"remoteport={_NOT_PORT_53}",
            ]))
    return rules


def install(*, server_ips: Iterable[str], dns_servers: Iterable[str] = ()) -> bool:
    """Arm the kill-switch for this server. True only if the rules are in the
    firewall afterwards (read back, not assumed from netsh's exit code).

    If a generation is already in force it stays until the new one exists; if
    the new one cannot be installed the old one is left alone — a failed re-arm
    must not open the machine up.
    """
    if not is_supported():
        return False
    # "Could not tell" counts as in force: never build on top of a slot that
    # may still hold rules.
    in_force = [s for s in _SLOTS if _rule_exists(rule_name(s, "v4")) is not False]
    new = next((s for s in _SLOTS if s not in in_force), _SLOTS[-1])
    if new in in_force:
        # Both generations present: a re-arm was interrupted. The spare one is
        # rebuilt; the other keeps blocking meanwhile.
        _delete_rules(_slot_rules(new))
        in_force.remove(new)

    rules = build_rules(new, server_ips=server_ips, dns_servers=dns_servers)
    ok = all(_add_rule(name, args) for name, args in rules)
    ok = ok and all(_rule_exists(name) is True for name, _ in rules)
    if not ok:
        _delete_rules(_slot_rules(new))
        return False
    _delete_rules([n for s in in_force for n in _slot_rules(s)])
    _delete_rules(reversed(_LEGACY_RULES))
    return True


def remove(force: bool = False) -> bool:
    """Remove every kill-switch rule, current and legacy. True when none is
    left — the caller tells the user if the machine may still be blocked.

    `force` is for a caller that KNOWS rules were installed: it deletes without
    asking first, so a lookup that fails cannot talk us out of removing them."""
    if not is_supported():
        return True
    # Every disconnect comes through here, kill-switch or not, so the common
    # case (nothing armed) costs three lookups rather than a dozen deletes.
    if not force and not is_active():
        return True
    _delete_rules(_all_rule_names())
    return not is_active()


def is_active() -> bool:
    """True if any kill-switch rule exists in the firewall — ours or one left
    by a pre-4.0 build. Used at startup to detect a crashed prior run."""
    if not is_supported():
        return False
    # A lookup that could not run (netsh timed out) is not a "no": answering
    # False there would report a blocked machine as clean.
    return any(_rule_exists(name) is not False for name in
               [rule_name(s, "v4") for s in _SLOTS] + [_LEGACY_RULES[0]])


def physical_dns_servers() -> list[str]:
    """IPv4 DNS servers of every interface except our own TUN. The kill-switch
    keeps them reachable (port 53 only), otherwise neither the engine nor a
    reconnect could resolve anything once the rules are up. [] when it cannot
    be read — the LAN ranges, where most resolvers live, are spared anyway."""
    if not is_supported():
        return []
    try:
        from . import network_routes
        rc, out, _ = network_routes._ps(
            "Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction "
            f"SilentlyContinue | Where-Object {{ $_.InterfaceAlias -ne '{TUN_DEVICE_NAME}' }} "
            "| ForEach-Object { $_.ServerAddresses }",
            timeout=8.0,
        )
    except Exception:
        return []
    if rc != 0:
        return []
    tun = ipaddress.ip_network(TUN_INET4, strict=False)
    found: list[str] = []
    for raw in re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", out or ""):
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip not in tun and not ip.is_loopback and not ip.is_unspecified \
                and str(ip) not in found:
            found.append(str(ip))
    return found


def _netsh(*args: str) -> Optional[bool]:
    """True / False by netsh's exit code; None when netsh could not be run or
    did not answer — "unknown", which callers must not read as either."""
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", *args],
            capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def _add_rule(name: str, args: list[str]) -> bool:
    """Wrap `netsh advfirewall firewall add rule name=... <args>`."""
    return _netsh("add", "rule", f"name={name}", *args) is True


def _rule_exists(name: str) -> Optional[bool]:
    # netsh returns 1 + "No rules match the specified criteria" when
    # the rule doesn't exist. 0 + rule details when it does.
    return _netsh("show", "rule", f"name={name}")


def _delete_rules(names: Iterable[str]) -> None:
    # Deleting a rule that isn't there is netsh's non-zero "no match": fine.
    for name in names:
        _netsh("delete", "rule", f"name={name}")
