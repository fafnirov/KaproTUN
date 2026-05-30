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
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _resolve_with_hard_timeout(host: str, timeout: float = 2.0) -> None:
    """Fire a DNS lookup for `host` with a wallclock cap.

    Why we don't use socket.gethostbyname directly: it has NO timeout
    parameter, and socket.setdefaulttimeout only affects socket I/O,
    not the underlying resolver. When the system DNS chain is
    misconfigured / mid-reconfiguration (e.g. right after toggling
    DNS-leak protection but before reconnecting VPN), gethostbyname
    can hang for minutes per lookup, freezing the probe entirely.
    The user's v1.16.8 report was 10+ lookups × hang = forever.

    On Windows nslookup is always present in PATH; on other platforms
    we fall back to a daemon-thread + Event combo so a single hang
    doesn't block the rest of probe_dns. Either way, the call returns
    in at most `timeout` seconds.

    We don't return the resolved IP — probe_dns only uses the side
    effect of HITTING the resolver, which bash.ws then reports back
    on. Errors (NXDOMAIN, timeout) are intentional and swallowed.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["nslookup", host],
                capture_output=True, timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        return
    # POSIX fallback: daemon thread with a join-timeout. The thread
    # may keep blocking after we move on but it won't keep our caller
    # waiting (daemon flag means process exit kills it).
    import threading
    done = threading.Event()

    def _target() -> None:
        try:
            socket.gethostbyname(host)
        except (socket.gaierror, OSError):
            pass
        finally:
            done.set()

    threading.Thread(target=_target, daemon=True).start()
    done.wait(timeout)

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


def fixable_protections(report: "LeakTestReport", settings: dict) -> list:
    """Leaks the user can fix by flipping a protection toggle that's OFF.

    Returns (settings_key, human_label) pairs for each detected leak whose
    protection setting is currently disabled — so the leak-test dialog can
    offer a one-click "enable" instead of making the user hunt through
    Settings. We only list leaks that turning the setting back ON would
    actually stop (IPv6 / WebRTC firewall blocks); a DNS leak or a leak that
    persists *with* protection on isn't a simple toggle flip, so it's not
    offered here. (v1.19.3 — prompted by a user whose IPv6 leaked because
    ipv6_leak_protection had been switched off.)
    """
    out: list = []
    if not report.ipv6.ipv6_blocked and not settings.get("ipv6_leak_protection", True):
        out.append(("ipv6_leak_protection", "IPv6"))
    if not report.webrtc.stun_blocked and not settings.get("webrtc_leak_protection", True):
        out.append(("webrtc_leak_protection", "WebRTC"))
    return out


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
        # Hard 2-second cap per lookup. v1.16.9 fix: bare gethostbyname
        # could hang for minutes when the DNS chain was mid-reconfig
        # (e.g. after toggling leak-protection but before VPN reconnect),
        # making the whole probe lock the dialog. nslookup-via-subprocess
        # respects timeout properly. We don't care about NXDOMAIN /
        # timeout outcomes — only that bash.ws got asked.
        _resolve_with_hard_timeout(host, timeout=2.0)

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

    # v1.16.10 leak heuristic:
    #
    # A "leak" is when DNS queries reach a resolver the user didn't ask
    # for — typically their ISP. We can't have a perfect classifier here
    # (bash.ws geo metadata is best-effort), so we use two layered
    # checks that catch the obvious cases without flagging clean setups:
    #
    # 1) Whitelist by substring in hostname OR ASN OR ASN-number.
    #    Covers: "cloudflare", "google", "quad9", "adguard", "opendns",
    #    "level3", "ovh", "nextdns", "datacamp" (AdGuard CDN partner),
    #    "edge technology" (AdGuard EU upstream), "fastly", "akamai",
    #    "ovh", "hetzner" (typical VPN/CDN hosters). Plus the actual
    #    AS numbers (AS13335 Cloudflare, AS15169 Google, AS19281 Quad9,
    #    AS207651 AdGuard, AS208398 Edge Technology).
    #
    # 2) If ALL resolvers land in the SAME ASN — that's an anycast/
    #    single-provider DNS network (e.g. AdGuard's whole anycast pool,
    #    all 8 in AS208398). Cannot be a leak: a leak by definition
    #    means one query went to a different provider.
    well_known_vpn_dns_substrings = (
        "cloudflare", "google", "quad9", "adguard",
        "opendns", "level3", "ovh", "nextdns",
        "datacamp",          # AdGuard's CDN partner (84.17.46.* etc.)
        "edge technology",   # AdGuard EU recursive infra (5.45.240.*)
        "fastly", "akamai", "hetzner",
        "as13335", "as15169", "as19281",  # CF / Google / Quad9
        "as207651", "as208398",            # AdGuard / Edge-Technology
    )

    def _resolver_looks_official(entry: dict) -> bool:
        host = (entry.get("hostname") or "").lower()
        asn = (entry.get("asn") or "").lower()
        return any(
            w in host or w in asn
            for w in well_known_vpn_dns_substrings
        )

    leak = False
    if meta:
        # Same-AS short-circuit: all resolvers in one ASN means
        # legit single-provider anycast. Not a leak.
        unique_asns = {
            (entry.get("asn") or "").split(" ")[0]
            for entry in meta
        }
        if len(unique_asns) <= 1:
            leak = False
        else:
            # Mixed ASNs: flag if any resolver doesn't match the
            # whitelist. ISP-DNS in the mix → leak.
            leak = any(not _resolver_looks_official(e) for e in meta)

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
    """Run all four probes in parallel. Total wallclock = max(any probe).

    v1.16.10: parallelised via ThreadPoolExecutor. The probes are
    completely independent network calls — running them sequentially
    just summed up the timeouts (worst-case ~30 s sequential, ~10 s
    parallel since the slowest probe — DNS — is the wallclock floor).

    Order-independence assumptions:
      - IPv4 / IPv6 probes hit different hostnames, don't interact
      - DNS probe uses its own (separate) endpoint and token
      - WebRTC probe is raw UDP to stun.l.google.com — independent
        of any HTTP probe
    Earlier v1.16.4 comment about STUN-going-last "so its firewall log
    isn't confused with v4/v6/DNS" turned out not to matter in practice
    — separate sockets, separate connections, separate log entries.
    """
    from concurrent.futures import ThreadPoolExecutor

    report = LeakTestReport()
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_ipv4 = ex.submit(probe_ipv4, socks_proxy)
        f_ipv6 = ex.submit(probe_ipv6, socks_proxy)
        f_dns = ex.submit(probe_dns)
        f_webrtc = ex.submit(probe_webrtc)
        # .result() with no timeout — each probe already has its own
        # internal cap (requests timeout=, nslookup subprocess timeout=,
        # socket settimeout). The dialog has a 25 s overall watchdog
        # in case any of them somehow leaks past their cap.
        report.ipv4 = f_ipv4.result()
        report.ipv6 = f_ipv6.result()
        report.dns = f_dns.result()
        report.webrtc = f_webrtc.result()
    return report
