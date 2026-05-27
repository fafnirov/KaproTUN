"""Firewall-based IPv6 outbound block — prevents IPv6 leaks in TUN mode.

KaproVPN's TUN mode only tunnels IPv4. On IPv6-enabled hosts (most
Russian residential ISPs hand out public v6 — Beeline, MTS, Rostelecom),
applications resolving AAAA records send their traffic out over IPv6
DIRECTLY through the ISP, bypassing the TUN device entirely. The user's
real IPv6 (and the destinations they visit) are then visible to their
ISP. v1.10.2 user discovered this when our IP probe accidentally
displayed his real Beeline-Moscow IPv6 in the UI; v1.10.3 hid the
symptom from the probe, but the underlying leak in real app traffic
remained.

This module blocks IPv6 leaks at connect-time using a Windows Firewall
rule. Approach:

  Block outbound IPv6 to the global unicast range (2000::/3). That
  single CIDR covers everything routable on the public IPv6 internet.
  We do NOT touch link-local (fe80::/10), multicast (ff00::/8), or
  loopback (::1) — those are link-scope and never leave the LAN
  anyway; blocking them would break LAN devices that have v6 (NAS,
  printers, Apple stuff, etc).

One rule instead of three (vs kill-switch's three) because:
  - We don't need an allow-LAN rule — LAN IPv6 is in fe80:: scope,
    which our 2000::/3 block doesn't touch.
  - We don't need an allow-xray rule — xray talks to the VPN server
    over IPv4 (the server's IP in user's config is IPv4), so the
    block doesn't affect it.

Lifecycle mirrors kill-switch:
  - Installed at TUN-mode connect (if settings.ipv6_leak_protection)
  - Removed on graceful disconnect
  - Orphan rule from crashed-prior-run removed on app startup

Requires admin (TUN mode already does). Silent skip if non-admin.

Future work:
  - macOS via `pfctl` with anchor file
  - Linux via `ip6tables` or nftables
"""
from __future__ import annotations

import subprocess
import sys

# Rule name shared with cleanup logic — anything starting with the
# KaproVPN-ipv6 prefix is fair game for our `remove()`.
_RULE_PREFIX = "KaproVPN-ipv6"
_RULE_BLOCK_GLOBAL = f"{_RULE_PREFIX}-block-global"

# Global unicast IPv6 — 2000::/3 covers all currently-allocated routable
# v6 (2000::–3fff:ffff:ffff:...). Link-local (fe80::/10), unique-local
# (fc00::/7), multicast (ff00::/8), loopback (::1), and the unspecified
# (::) all sit OUTSIDE this range and are not affected — LAN keeps working.
_IPV6_GLOBAL_UNICAST = "2000::/3"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_supported() -> bool:
    """Windows-only — uses `netsh advfirewall`, same as kill-switch.

    macOS/Linux equivalents (pfctl / ip6tables) are future work.
    """
    return sys.platform == "win32"


def install() -> bool:
    """Install the IPv6-block rule. Returns True on success.

    Idempotent: removes any pre-existing rule with our name first so
    a re-install picks up the current parameters (netsh add doesn't
    update, it dupes).
    """
    if not is_supported():
        return False

    # Wipe any leftover from a crashed prior run before adding fresh.
    remove()

    # Single rule: block outbound to global unicast IPv6. Anything in
    # 2000::/3 (≈ all public IPv6 addresses currently in use) gets
    # dropped. link-local / ULA / multicast stay reachable.
    cmd = [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={_RULE_BLOCK_GLOBAL}",
        "dir=out",
        "action=block",
        "enable=yes",
        "profile=any",
        f"remoteip={_IPV6_GLOBAL_UNICAST}",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def remove() -> None:
    """Remove the IPv6-block rule. Idempotent — never raises.

    Best-effort: if the rule doesn't exist, netsh prints a warning
    and returns non-zero, we swallow it. End state: no
    KaproVPN-ipv6-* rules, IPv6 traffic flows normally again.
    """
    if not is_supported():
        return
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule",
             f"name={_RULE_BLOCK_GLOBAL}"],
            capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def is_active() -> bool:
    """True if our IPv6-block rule is currently in the firewall.

    Used at app startup to detect a crashed-prior-run state — if
    KaproVPN exited uncleanly with the rule installed, the next
    launch removes it via cleanup_if_orphan() so the user isn't
    stuck without IPv6 forever.
    """
    if not is_supported():
        return False
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={_RULE_BLOCK_GLOBAL}"],
            capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # netsh returns 1 + "No rules match the specified criteria" when
    # the rule doesn't exist. 0 + rule details when it does.
    return proc.returncode == 0
