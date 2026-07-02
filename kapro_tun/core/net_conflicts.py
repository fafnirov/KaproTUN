"""Detect third-party apps that hijack Windows networking (virtual adapters,
packet filters, route/DNS managers) and can stop the VPN TUN from carrying
traffic.

The failure they cause is nasty because it looks like OUR bug: sing-box comes
up, the status shows "connected", but no site loads and no public IP resolves —
the packets get intercepted by the other app's network layer before they ever
reach WinTUN. A userspace VPN can't reliably override another app's network
driver, so the honest help is to DETECT the culprit and tell the user to close
it, instead of them (and us) hunting a phantom VPN bug for hours.

Motivating report: Meta Horizon Link (the Meta Quest / VR PC app) broke all
internet while KaproTUN was connected.

Detection is best-effort and process-based (psutil): match known network-owning
apps by process-name substring. Never raises; returns [] when it can't tell.
"""
from __future__ import annotations

from typing import List

# Display name → lowercase process-name substrings that identify the app family.
# Keep substrings specific enough not to false-positive on unrelated processes.
_KNOWN_CONFLICTS: dict[str, tuple[str, ...]] = {
    "Meta Horizon Link / Oculus (VR)": (
        "metahorizon", "oculusclient", "ovrserver", "ovrservicelauncher",
        "ovrredir",
    ),
}


def detect_running_conflicts() -> List[str]:
    """Display names of known network-hijacking apps currently running.

    Best-effort: returns [] if psutil is unavailable or nothing matches, and
    never raises (called from the connect path, must not break a connect)."""
    try:
        import psutil
    except Exception:
        return []
    found: set[str] = set()
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except Exception:
                continue
            if not name:
                continue
            for display, needles in _KNOWN_CONFLICTS.items():
                if any(n in name for n in needles):
                    found.add(display)
    except Exception:
        pass
    return sorted(found)
