"""Firewall-based WebRTC leak protection — blocks STUN UDP traffic.

The leak: WebRTC (the browser P2P API behind Google Meet, Jitsi,
Discord-web, many web-video features) uses ICE/STUN to discover the
peer's network topology. The client opens a UDP socket to a public
STUN server (stun.l.google.com, etc.) which echoes back the client's
public IP. Any JavaScript on the page — not just video apps, ANY page —
can call `RTCPeerConnection`, harvest the candidates, and learn the
user's real public IP. Even with a perfectly working VPN.

Why a VPN doesn't auto-fix it:

  - HTTP-proxy mode tunnels TCP only. UDP (including STUN) goes
    directly out the physical NIC → real IP exposed.
  - TUN mode tunnels both, so STUN echoes back the VPN-exit IP
    instead of the real one. But "defence in depth" still wants
    the STUN block — protects against a TUN-bypass route the
    user accidentally configured, against malware running outside
    the tunnel, etc.

The block: a single Windows Firewall rule denying outbound UDP to the
known STUN service ports. STUN's well-known ports are tightly
standardised — the spec (RFC 5389) defines 3478 (plain) and 5349
(STUNS / DTLS). Google's web stack also publishes on 19302 and
19305-19309. Blocking these ports is enough to break all browser
WebRTC NAT-traversal without touching DNS (53), HTTPS (443), or
anything else the user actually wants to do.

Limits / known gotchas:

  - Native apps with embedded STUN on custom ports (Discord native
    voice, Zoom client, MS Teams) keep working — they don't use the
    well-known ports. Browser-based Discord/Teams DOES use them
    though, so web-Discord video CAN break. Documented in the
    Settings page warning.
  - We don't block TURN (relay over TLS:443) because that's
    indistinguishable from normal HTTPS traffic — you'd have to
    blacklist every TURN server's IP, which moves constantly.
    TURN doesn't leak the client IP though, only relays packets —
    so it's a different threat model.

Lifecycle mirrors ipv6_block (v1.11):

  - Installed at connect (if settings.webrtc_leak_protection)
  - Removed on graceful disconnect
  - Orphan rule from a crashed prior run cleaned at app startup

Requires admin. Silent skip if non-admin (defence-in-depth, won't
break the connect flow).

Future work:

  - macOS via `pfctl` anchor (block proto udp to any port {3478, 5349,
    19302, 19305:19309})
  - Linux via nftables (similar single-line rule)
"""
from __future__ import annotations

import subprocess
import sys

# Shared rule-name prefix so cleanup logic can sweep anything we left
# behind. Matches the ipv6_block convention.
_RULE_PREFIX = "KaproVPN-webrtc"
_RULE_BLOCK_STUN = f"{_RULE_PREFIX}-block-stun"

# Well-known STUN ports as defined by RFC 5389 + Google's published
# ranges. The netsh `remoteport` field accepts a comma-separated list
# with embedded ranges (M-N). Keep this list small + standardised —
# every entry we add risks breaking a niche-but-legitimate UDP service.
#
# 3478          STUN (RFC 5389) — the canonical STUN port
# 5349          STUNS / DTLS-STUN — same protocol over TLS-secured DTLS
# 19302         Google's public STUN — stun.l.google.com, stun1-4.l...
# 19305-19308   Google's additional STUN range
_STUN_PORTS = "3478,5349,19302,19305-19308"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_supported() -> bool:
    """Windows-only — uses `netsh advfirewall`. Mirrors ipv6_block.

    macOS/Linux equivalents (pfctl / nftables) are future work.
    """
    return sys.platform == "win32"


def install() -> bool:
    """Install the WebRTC STUN-block rule. Returns True on success.

    Idempotent: removes any pre-existing rule with our name first so
    a re-install picks up current parameters (netsh `add` doesn't
    update an existing rule, it creates a duplicate).
    """
    if not is_supported():
        return False

    # Wipe any leftover from a prior crashed run before adding fresh.
    remove()

    # Block outbound UDP to the well-known STUN ports. Protocol=UDP is
    # essential — TCP/3478 (some TURN-over-TCP servers) isn't a WebRTC
    # leak vector and we'd rather leave it alone.
    cmd = [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={_RULE_BLOCK_STUN}",
        "dir=out",
        "action=block",
        "enable=yes",
        "profile=any",
        "protocol=UDP",
        f"remoteport={_STUN_PORTS}",
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
    """Remove the WebRTC STUN-block rule. Idempotent — never raises.

    Best-effort: if the rule isn't present, netsh returns non-zero
    + "No rules match" — we swallow it. End state: no
    KaproVPN-webrtc-* rules, STUN UDP flows normally again, browser
    WebRTC NAT-traversal works.
    """
    if not is_supported():
        return
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule",
             f"name={_RULE_BLOCK_STUN}"],
            capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def is_active() -> bool:
    """True if our STUN-block rule is currently in the firewall.

    Used at app startup to detect a crashed-prior-run state — if
    KaproVPN exited uncleanly with the rule installed, the next launch
    removes it via cleanup so the user isn't stuck without WebRTC
    forever.
    """
    if not is_supported():
        return False
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={_RULE_BLOCK_STUN}"],
            capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # netsh: 0 + rule details when present, 1 + "No rules match" when
    # absent. We only care about the return code.
    return proc.returncode == 0
