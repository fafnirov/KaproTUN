"""Active leak self-test: DNS, IPv4, IPv6, WebRTC.

Push-button "проверь меня" from the Settings page. Real network probes
done over the live VPN tunnel, results presented as a 4-row pass/fail
table in the UI.

What each probe actually checks:

  IPv4
    Hit a public "what's my IP" JSON endpoint over the SOCKS proxy
    that xray exposes on 127.0.0.1:listen_port+1. Returns the
    exit-IP + country the internet sees. Always succeeds when
    connected — the value tells the user what country their traffic
    appears to come from.

  IPv6
    Same endpoint but on the v6-only hostname. If we DO get an IPv6
    answer it means traffic went out the real ISP via the v6 stack
    (the v1.11 ipv6_block didn't catch it, or the user disabled it).
    "No answer" is the desired result for a clean tunnel.

  DNS
    The canonical bash.ws DNS-leak protocol:
      1) generate a random 10-digit token
      2) resolve N subdomains "{1..10}.{token}.dnsleak.bash.ws" — bash.ws
         is the authoritative resolver for the test zone, so it logs
         every resolver IP that asked
      3) GET https://bash.ws/dnsleak/test/{token}?json — server returns
         the list of resolvers that made queries during step 2
    If the returned list contains the user's ISP resolvers (not their
    VPN's), DNS is leaking. We don't try to classify per-IP — we just
    surface the resolver list, the user can spot "ax.x.beelinetelecom"
    on their own. Optionally we also flag if any resolver IP is in
    the same /24 as the user's real external IP.

  WebRTC / STUN
    Real UDP socket → stun.l.google.com:19302 with a minimal STUN
    Binding Request. If we get a reply, the v1.16 webrtc_block firewall
    rule isn't doing its job (or wasn't installed). If timeout, the
    block works.

Privacy: every probe goes through the active VPN tunnel except the
STUN probe (which intentionally tries to leak — that's the test). No
identifiable user data is sent to any endpoint beyond what these
tests inherently require (random tokens, an IP request).
"""
from __future__ import annotations

import json
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests
except ImportError:  # pragma: no cover — guaranteed by requirements.txt
    requests = None  # type: ignore[assignment]


# ===== Result types ========================================================

@dataclass
class IPv4Result:
    ip: Optional[str] = None
    country: Optional[str] = None
    asn: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.ip is not None


@dataclass
class IPv6Result:
    ip: Optional[str] = None
    error: Optional[str] = None
    # "ok" here MEANS "no v6 leak" — we WANT this to fail.
    # ipv6_blocked = True means our block is doing its job.
    ipv6_blocked: bool = False


@dataclass
class DnsResult:
    resolvers: list[str] = field(default_factory=list)
    # Hostnames/ASNs of those resolvers as reported by bash.ws.
    # bash.ws includes a brief geo-lookup for each one.
    resolvers_meta: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    suspected_leak: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class WebRtcResult:
    stun_blocked: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        # The "good" state here is stun_blocked=True (firewall caught it).
        return self.stun_blocked


@dataclass
class LeakTestReport:
    """Combined output of all four probes."""
    ipv4: IPv4Result = field(default_factory=IPv4Result)
    ipv6: IPv6Result = field(default_factory=IPv6Result)
    dns: DnsResult = field(default_factory=DnsResult)
    webrtc: WebRtcResult = field(default_factory=WebRtcResult)


# ===== Probes ==============================================================

# IPv4 probes — two redundant endpoints because dnsleak'd ISPs sometimes
# NXDOMAIN one (we hit the same issue with the IP-probe in v1.10.4).
_IPV4_ENDPOINTS = (
    "https://api.myip.com",          # returns {"ip", "country", "cc"}
    "https://ipv4.icanhazip.com",    # plain-text IP, no JSON
)


def _socks_proxy_dict(socks_url: str) -> dict:
    """Build a requests-style proxies dict for socks5h://host:port."""
    return {"http": socks_url, "https": socks_url}


def probe_ipv4(socks_proxy: Optional[str], timeout: float = 6.0) -> IPv4Result:
    """Ask the internet what our public IPv4 looks like, via VPN.

    socks_proxy: "socks5h://127.0.0.1:2081" (the SOCKS inbound xray
    exposes alongside the HTTP one). Pass None to probe without a
    proxy (used when we want to compare leak vs tunnel — not in v1.16.4
    UI yet but useful for future "compare direct/tunnel" diff view).
    """
    if requests is None:
        return IPv4Result(error="requests library not installed")
    proxies = _socks_proxy_dict(socks_proxy) if socks_proxy else None

    last_error: Optional[str] = None
    for url in _IPV4_ENDPOINTS:
        try:
            r = requests.get(url, proxies=proxies, timeout=timeout)
            if r.status_code != 200:
                last_error = f"{url}: HTTP {r.status_code}"
                continue
            text = r.text.strip()
            if url.endswith("icanhazip.com"):
                # plain-text response, just an IP
                return IPv4Result(ip=text)
            data = json.loads(text)
            return IPv4Result(
                ip=data.get("ip"),
                country=data.get("country"),
                asn=data.get("cc"),
            )
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            last_error = f"{url}: {type(e).__name__}: {e}"
            continue
    return IPv4Result(error=last_error or "all IPv4 endpoints failed")


def probe_ipv6(socks_proxy: Optional[str], timeout: float = 4.0) -> IPv6Result:
    """Test whether IPv6 leaks past the v4-only tunnel.

    We DELIBERATELY probe an IPv6-only endpoint with a SHORT timeout.
    Two possible outcomes:
      - Connection works → got an IPv6 → that IPv6 went out the real
        ISP, NOT the tunnel (xray + tun2socks are v4-only). This IS
        the leak.
      - Connection fails (timeout / refused / no route) → the v6 stack
        couldn't reach the internet. Either the user has no v6 at all,
        or our ipv6_block firewall rule (v1.11) is blocking it. Either
        way: no leak.
    """
    if requests is None:
        return IPv6Result(error="requests library not installed")
    proxies = _socks_proxy_dict(socks_proxy) if socks_proxy else None

    try:
        r = requests.get(
            "https://ipv6.icanhazip.com",
            proxies=proxies,
            timeout=timeout,
        )
        if r.status_code == 200:
            return IPv6Result(ip=r.text.strip(), ipv6_blocked=False)
        return IPv6Result(
            error=f"HTTP {r.status_code}",
            ipv6_blocked=True,  # we couldn't reach v6, that's a clean state
        )
    except requests.exceptions.RequestException:
        # The expected case for a healthy tunnel: no IPv6 connectivity.
        return IPv6Result(ipv6_blocked=True)


def probe_dns(timeout: float = 8.0) -> DnsResult:
    """Run the bash.ws DNS-leak protocol.

    Generates a random 10-digit token, resolves 10 subdomains under
    that token via the system resolver (whatever the OS hands us —
    that's the whole point, we want to SEE which resolvers it uses),
    then asks bash.ws to list the resolver IPs it observed.

    bash.ws is operated by github.com/macvk/dnsleaktest, well-known
    in the privacy community, free, no signup, no logging beyond the
    in-memory test results.
    """
    if requests is None:
        return DnsResult(error="requests library not installed")

    # 10-digit token, URL-safe enough as decimal digits.
    token = "".join(str(secrets.randbelow(10)) for _ in range(10))

    # Step 1: trigger N resolutions of subdomains UNDER bash.ws.
    # bash.ws is the authoritative resolver for its own zone — it
    # logs every recursive resolver IP that comes asking. We use
    # bash.ws (NOT dnsleaktest.com — that's a different site with
    # a different authoritative DNS, was my v1.16.4 typo that
    # silently returned empty resolver lists for everyone).
    # The format must match what bash.ws/dnsleaktest.sh uses:
    #   {i}.{ID}.bash.ws
    for i in range(1, 11):
        host = f"{i}.{token}.bash.ws"
        try:
            socket.gethostbyname(host)
        except socket.gaierror:
            # NXDOMAIN is normal — these subdomains intentionally don't
            # exist. We just need the resolver to try.
            pass
        except OSError:
            pass

    # Step 2: wait for bash.ws to aggregate the resolver hits, then
    # ask. 2 seconds — 1 sec was sometimes too tight under load and
    # we'd get a half-empty list back.
    time.sleep(2.0)

    try:
        r = requests.get(
            f"https://bash.ws/dnsleak/test/{token}?json",
            timeout=timeout,
        )
        if r.status_code != 200:
            return DnsResult(error=f"bash.ws HTTP {r.status_code}")
        data = r.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        return DnsResult(error=f"bash.ws: {type(e).__name__}: {e}")

    # bash.ws returns a JSON array of objects: {ip, country, country_code,
    # asn, type, hostname}. The "type" field marks which entry is the
    # client IP vs the resolvers — we want only the resolvers.
    resolvers: list[str] = []
    meta: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "dns":
            ip = entry.get("ip")
            if ip and ip not in resolvers:
                resolvers.append(ip)
                meta.append(entry)

    # Heuristic leak detection: if MORE than one resolver showed up
    # and any of them looks like a residential ISP (no "cloudflare",
    # "google", "quad9", "adguard" in the hostname/asn), flag as
    # suspected leak. This is a soft signal, the UI will show the
    # list and let the user decide.
    leak = False
    if len(resolvers) > 0:
        well_known_vpn_dns = (
            "cloudflare", "google", "quad9", "adguard",
            "opendns", "level3", "ovh", "nextdns",
        )
        for entry in meta:
            host = (entry.get("hostname") or "").lower()
            asn = (entry.get("asn") or "").lower()
            if not any(w in host or w in asn for w in well_known_vpn_dns):
                leak = True
                break

    return DnsResult(resolvers=resolvers, resolvers_meta=meta, suspected_leak=leak)


# ===== WebRTC / STUN probe ================================================

def probe_webrtc(timeout: float = 2.0) -> WebRtcResult:
    """Try a real STUN Binding Request to stun.l.google.com:19302.

    Builds a minimal RFC 5389 Binding Request (20-byte header, no
    attributes) and sends it over a UDP socket. If the server's
    response comes back, our webrtc_block firewall rule (v1.16.0)
    didn't catch it — STUN is leakable. If we time out, the block
    works.

    19302 is one of the ports on our block list, so a correctly-
    armed firewall rejects this packet on the way out. We don't
    need to PARSE the response (it'd contain our real external
    IP) — just receiving anything tells us the block failed.
    """
    # STUN Binding Request header (RFC 5389 §6):
    #   message type:   0x0001 (Binding Request)
    #   message length: 0x0000 (no attributes)
    #   magic cookie:   0x2112A442
    #   transaction id: 12 random bytes
    txid = secrets.token_bytes(12)
    packet = struct.pack("!HHI", 0x0001, 0x0000, 0x2112A442) + txid

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.sendto(packet, ("stun.l.google.com", 19302))
        except OSError as e:
            # Firewall rejected the SEND (some configs do this) — that
            # also counts as "blocked".
            return WebRtcResult(stun_blocked=True, error=str(e))
        try:
            data, _ = sock.recvfrom(1024)
        except socket.timeout:
            # Timeout = firewall ate the packet (or STUN server is
            # ghosted). Either way the user's IP didn't escape.
            return WebRtcResult(stun_blocked=True)
        except OSError as e:
            return WebRtcResult(stun_blocked=True, error=str(e))
        # We got a STUN response. Sanity-check it's actually STUN
        # (magic cookie at offset 4) before declaring a leak.
        if len(data) >= 20 and data[4:8] == b"\x21\x12\xA4\x42":
            return WebRtcResult(stun_blocked=False)
        return WebRtcResult(
            stun_blocked=False,
            error="non-STUN response",
        )
    finally:
        sock.close()


# ===== Orchestrator ========================================================

def run_full_leak_test(socks_proxy: Optional[str]) -> LeakTestReport:
    """Run all four probes sequentially. Total wallclock ~10-15 s.

    Order matters slightly — STUN UDP test goes LAST so its (potential)
    firewall log isn't confused with the v4/v6/DNS tests. Other than
    that, the probes are independent.
    """
    report = LeakTestReport()
    report.ipv4 = probe_ipv4(socks_proxy)
    report.ipv6 = probe_ipv6(socks_proxy)
    report.dns = probe_dns()
    report.webrtc = probe_webrtc()
    return report
