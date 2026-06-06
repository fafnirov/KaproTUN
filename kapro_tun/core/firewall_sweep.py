"""Brand-prefix firewall sweep — remove ANY leftover KaproTUN/KaproVPN rule.

The per-feature modules (killswitch.py, ipv6_block.py, webrtc_block.py) tear down
their rules ONLY by exact current name (e.g. "KaproTUN-ipv6-block-global"). That
misses two classes of orphan:

  * pre-v1.22.0 builds named their rules "KaproVPN-*" (the rename to "KaproTUN"
    changed every prefix), and
  * ad-hoc/test suffixes like "KaproVPN-ipv6-block-TEST".

Such an orphan (a real one found in the field: "KaproVPN-ipv6-block-TEST" blocking
2000::/3 outbound) persists across disconnects AND app restarts and silently
breaks IPv6 even with the VPN off — surfacing in the browser as
ERR_NETWORK_ACCESS_DENIED on dual-stack sites. This module sweeps every leftover
by brand PREFIX so old-brand / odd-suffix rules can't linger.

Windows-only; a silent no-op everywhere else. Enumeration uses PowerShell
`Get-NetFirewallRule` (object DisplayName) NOT netsh text parsing: netsh
`show rule` DATA is localized, so scraping a "Rule Name:" label is locale-fragile
(it fails outright on a Russian-locale box).
"""
from __future__ import annotations

import subprocess
import sys

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Every firewall rule this app has ever created starts with one of these brand+
# dash prefixes. Current brand: "KaproTUN-" (killswitch-/ipv6-/webrtc- submodules
# append their own segment). Legacy pre-v1.22.0 brand: "KaproVPN-". The trailing
# dash keeps us from matching an unrelated user rule like "KaproTUNnel".
_RULE_NAME_PREFIXES = ("KaproTUN-", "KaproVPN-")


def is_supported() -> bool:
    return sys.platform == "win32"


def _list_managed_rule_names() -> list[str]:
    """All firewall rule DisplayNames starting with one of our brand prefixes."""
    if not is_supported():
        return []
    conds = " -or ".join(
        "$_.DisplayName -like '{}*'".format(p) for p in _RULE_NAME_PREFIXES)
    ps = ("Get-NetFirewallRule | Where-Object { " + conds + " } "
          "| ForEach-Object { $_.DisplayName }")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    seen: dict[str, None] = {}
    for line in (proc.stdout or "").splitlines():
        name = line.strip()
        # Re-validate the prefix client-side (defence-in-depth) and de-dup.
        if name and name.startswith(_RULE_NAME_PREFIXES):
            seen.setdefault(name, None)
    return list(seen)


def sweep() -> list[str]:
    """Delete every leftover KaproTUN/KaproVPN-prefixed firewall rule.

    Best-effort, idempotent, never raises. Returns the names it removed (for
    logging). Safe at every startup: per-session install() re-adds the correct
    rules LATER at connect-time, so a healthy session loses nothing.
    """
    removed: list[str] = []
    for name in _list_managed_rule_names():
        try:
            proc = subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule",
                 "name={}".format(name)],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW,
            )
            if proc.returncode == 0:
                removed.append(name)
        except (OSError, subprocess.SubprocessError):
            pass
    return removed


def has_orphans() -> bool:
    """True if any KaproTUN/KaproVPN-prefixed firewall rule currently exists."""
    return bool(_list_managed_rule_names())
