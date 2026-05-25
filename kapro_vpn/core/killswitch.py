"""Firewall-based kill-switch — blocks all outbound traffic when the
tunnel is down so packets can't leak via the real interface.

How it works on Windows:

  At connect-time (when settings.kill_switch is True), we install
  three Windows Firewall rules using netsh advfirewall:

    1. KaproVPN-killswitch-block   Block ALL outbound traffic.
    2. KaproVPN-killswitch-allow-lan  Allow outbound to RFC1918 +
       loopback + link-local. So local network printers, NAS, router
       UI, AirPlay etc. keep working.
    3. KaproVPN-killswitch-allow-xray  Allow outbound from xray.exe
       specifically — that's the only process whose traffic SHOULD
       reach the public internet (it'll wrap everything in proxy/
       VLESS/Trojan/etc and send to the VPN server).

  Windows Firewall rule precedence: "block" wins over "allow" of the
  same scope, but more-specific allows win over broad blocks. So:
    - Random app tries to reach 1.2.3.4:443 → matches Block-all → DROP.
    - Same app instead asks xray (via SOCKS or system proxy) → xray
      then makes the outbound → matches Allow-xray-program → PASS.
    - Random app tries to reach 192.168.1.5 → matches Allow-lan → PASS.

If xray crashes mid-session, the rules STAY in place. Result:
  - Browser timeout instead of silent leak to real ISP
  - User notices, opens KaproVPN, reconnects → xray restarts,
    everything works again

Cleanup paths:
  - Graceful disconnect: rules removed
  - Crash/kill of KaproVPN: rules stay, but main.py's startup cleanup
    removes them on next launch (same pattern as orphan-killer)

Requires admin. The controller gates on admin.is_admin() before
activating kill-switch — without elevation, we silently skip
(non-admin users get a settings hint instead).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

# All KaproVPN-managed firewall rules share this prefix so cleanup
# can match-and-delete them safely without touching unrelated rules
# the user (or another VPN) might have added.
_RULE_PREFIX = "KaproVPN-killswitch"

_RULE_BLOCK_ALL = f"{_RULE_PREFIX}-block"
_RULE_ALLOW_LAN = f"{_RULE_PREFIX}-allow-lan"
_RULE_ALLOW_XRAY = f"{_RULE_PREFIX}-allow-xray"

# Private networks + loopback + link-local — these stay reachable
# under kill-switch so the user's LAN doesn't die alongside their VPN.
_LAN_RANGES = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16"

# Hidden subprocess on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_supported() -> bool:
    """Kill-switch is currently Windows-only — `netsh advfirewall` is
    the easiest cross-version Windows API. Linux/macOS need iptables/
    pfctl wrappers — separate work, future release.
    """
    return sys.platform == "win32"


def install(xray_exe_path: Path) -> bool:
    """Install the three firewall rules. Returns True on success.

    Idempotent: if rules already exist (e.g. from a crashed prior run),
    we remove-then-add to make sure parameters are current.
    """
    if not is_supported():
        return False

    # Tear down any leftover rules first so we don't end up with
    # duplicate entries (netsh adds, doesn't update).
    remove()

    # 1. Block all outbound. profile=any covers Domain/Private/Public.
    if not _add_rule(_RULE_BLOCK_ALL, [
        "dir=out", "action=block", "enable=yes",
        "profile=any",
    ]):
        return False

    # 2. Allow LAN outbound — survives the block because more-specific
    # rules win in netsh. Without this, RDP into the box, file sharing,
    # printer access, etc. would all die when kill-switch activates.
    if not _add_rule(_RULE_ALLOW_LAN, [
        "dir=out", "action=allow", "enable=yes",
        "profile=any",
        f"remoteip={_LAN_RANGES}",
    ]):
        remove()
        return False

    # 3. Allow xray.exe outbound — this is the ONLY process whose
    # traffic should reach the public internet. Everything else
    # (browser, Telegram, etc.) goes THROUGH xray via local SOCKS/HTTP,
    # so the firewall sees their outbound as "xray.exe → 1.2.3.4" and
    # passes it through.
    if not _add_rule(_RULE_ALLOW_XRAY, [
        "dir=out", "action=allow", "enable=yes",
        "profile=any",
        f"program={xray_exe_path}",
    ]):
        remove()
        return False

    return True


def remove() -> None:
    """Remove all KaproVPN-managed kill-switch rules.

    Best-effort — if rules don't exist, netsh prints a warning and
    returns non-zero, which we swallow. End state: no KaproVPN-*
    firewall rules present, traffic flows normally.
    """
    if not is_supported():
        return
    for name in (_RULE_BLOCK_ALL, _RULE_ALLOW_LAN, _RULE_ALLOW_XRAY):
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule",
                 f"name={name}"],
                capture_output=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def is_active() -> bool:
    """True if any of our kill-switch rules currently exist in the
    firewall. Used at startup to detect a crashed-prior-run state.
    """
    if not is_supported():
        return False
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={_RULE_BLOCK_ALL}"],
            capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # netsh returns 1 + "No rules match the specified criteria" when
    # the rule doesn't exist. 0 + rule details when it does.
    return proc.returncode == 0


def _add_rule(name: str, args: list[str]) -> bool:
    """Wrap `netsh advfirewall firewall add rule name=... <args>`."""
    cmd = [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={name}", *args,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
