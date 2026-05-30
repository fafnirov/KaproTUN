"""Fetch the public IP + country as seen from outside the machine.

Used after a successful connect to confirm in the UI that the tunnel
is actually active ("Ваш IP: 1.2.3.4 (Нидерланды)" — visible proof
that traffic is now egressing from the VPN server, not the user's ISP).

Privacy notes:

  - The probe goes through whatever route the system currently has set
    up. In HTTP-proxy mode we explicitly route it through our local
    SOCKS5 (127.0.0.1:2081) so it sees the VPN server's egress IP, not
    the local IP. In TUN mode all traffic already tunnels, no extra
    routing needed.
  - The endpoint is ipinfo.io — third-party, public, HTTPS. We send
    them an empty GET (no auth, no user identifier). They see "someone
    at IP X asked who they are" — same query a browser would make.
  - We don't log the result anywhere; it's shown in the UI and that's it.
  - User can disable this in Settings (kill switch for any "phone home"-
    looking call) via the `public_ip_probe` setting.

Timeouts kept tight (5s) because if it doesn't return fast, we'd rather
show nothing than make the UI feel sluggish.
"""
from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

import requests

# _force_ipv4 monkeypatches the PROCESS-GLOBAL socket.getaddrinfo. If two
# probes overlap (e.g. a reconnect kicks a second one while the first is in
# flight), the first's `finally` restores the original mid-probe and the
# second silently resolves AAAA again — leaking the user's real IPv6 into
# the "Ваш IP" label. Serialize the patched region so it can never be raced.
_probe_lock = threading.Lock()


def _looks_ipv4(ip: str) -> bool:
    """True only for an IPv4 literal. We reject IPv6 results outright: the
    probe exists to show the VPN's IPv4 egress, and KaproVPN's TUN only
    tunnels IPv4 — any IPv6 we'd get back is the user's REAL leaked address,
    which must never be shown as 'your IP'."""
    return bool(ip) and ":" not in ip and ip.count(".") == 3


@contextmanager
def _force_ipv4():
    """Make socket.getaddrinfo IPv4-only for the duration of the block.

    Why: KaproVPN's TUN mode only tunnels IPv4. On an IPv6-enabled host
    (typical Russian residential ISP — Beeline, MTS — gives clients
    public v6), Python's socket prefers AAAA records when both A and
    AAAA exist. The probe to ipify/ipinfo/ifconfig then resolves via
    AAAA → goes out over IPv6 → bypasses the TUN entirely → returns
    the user's REAL public IPv6 from their ISP, not the VPN server's
    IPv4. v1.10.2 user reported exactly this — UI showed
    `2a00:1370:...` (real Beeline v6) with country "Россия · Moscow".

    Forcing AF_INET in getaddrinfo means only A records are returned,
    only IPv4 connections are attempted. The probe then correctly
    sees the VPN server's egress IPv4.

    Monkey-patching socket.getaddrinfo is normally a red flag, but
    the probe runs in a single worker thread for ~5 seconds with no
    concurrent socket calls — restoration in `finally` is guaranteed.
    """
    # Serialize: the patch is process-global, so overlapping probes must
    # not race each other's setup/teardown (see _probe_lock comment).
    with _probe_lock:
        original = socket.getaddrinfo

        def _v4_only(host, port, family=0, *args, **kwargs):
            return original(host, port, socket.AF_INET, *args, **kwargs)

        socket.getaddrinfo = _v4_only
        try:
            yield
        finally:
            socket.getaddrinfo = original


# Map ISO 3166-1 alpha-2 country codes → Russian display names for the
# countries our users typically connect through. Falls back to whatever
# ipinfo.io returns in `country` field for anything not listed.
_RU_COUNTRY_NAMES: dict[str, str] = {
    "NL": "Нидерланды",
    "DE": "Германия",
    "FR": "Франция",
    "GB": "Великобритания",
    "UK": "Великобритания",
    "US": "США",
    "CA": "Канада",
    "FI": "Финляндия",
    "SE": "Швеция",
    "NO": "Норвегия",
    "DK": "Дания",
    "PL": "Польша",
    "CZ": "Чехия",
    "SK": "Словакия",
    "AT": "Австрия",
    "CH": "Швейцария",
    "IT": "Италия",
    "ES": "Испания",
    "PT": "Португалия",
    "BE": "Бельгия",
    "LU": "Люксембург",
    "IE": "Ирландия",
    "LV": "Латвия",
    "LT": "Литва",
    "EE": "Эстония",
    "RO": "Румыния",
    "BG": "Болгария",
    "HU": "Венгрия",
    "RS": "Сербия",
    "MD": "Молдова",
    "UA": "Украина",
    "BY": "Беларусь",
    "KZ": "Казахстан",
    "GE": "Грузия",
    "AM": "Армения",
    "TR": "Турция",
    "IL": "Израиль",
    "AE": "ОАЭ",
    "SG": "Сингапур",
    "JP": "Япония",
    "KR": "Южная Корея",
    "HK": "Гонконг",
    "TW": "Тайвань",
    "AU": "Австралия",
    "NZ": "Новая Зеландия",
    "RU": "Россия",
}


@dataclass(frozen=True)
class PublicIp:
    ip: str
    country_code: str          # "NL", "DE", "US", ...
    country_name: str          # localized — "Нидерланды" / "Netherlands"
    city: Optional[str] = None # may be missing on the free ipinfo.io tier


# Probe-endpoint registry. We try these in order; first one that returns
# a parseable IP wins.
#
# v1.10.4 rearrangement after user logs showed AdGuard DNS NXDOMAIN'ing
# ALL three of our v1.10.3 domain-based endpoints (ipinfo.io, ipify.org,
# ifconfig.co — all classed by AdGuard as "analytics/tracking"). The
# fallback chain was useless when every link in it was blocked.
#
# I briefly considered Cloudflare's 1.1.1.1/cdn-cgi/trace (IP-literal,
# AdGuard-proof), but `_ALWAYS_BYPASS` in controller.py routes 1.1.1.1
# DIRECT at the OS layer (for DNS-leak protection) — any HTTPS to
# 1.1.1.1 would skip the VPN entirely and report the user's REAL IP,
# defeating the whole point.
#
# Picked api.myip.com (a simple JSON IP service, not in mainstream
# adblock lists) as new primary — gives both IP and country in one shot.
# httpbin.org/ip as IP-only fallback (Postman-affiliated testing service,
# popular among devs, unlikely to be flagged as a tracker). Old
# AdGuard-blocked endpoints kept for users on System/Cloudflare/Quad9
# DNS who don't have that blocking layer.
#
# Each entry: (url, handler). Handler takes the `requests.Response`,
# returns (ip, country_code, city) or raises on bad data.
def _handle_api_myip(r) -> tuple[str, str, Optional[str]]:
    """api.myip.com response: {"ip":"1.2.3.4","country":"United States","cc":"US"}"""
    data = r.json()
    ip = str(data.get("ip") or "").strip()
    if not ip:
        raise ValueError("no ip field")
    code = str(data.get("cc") or "").strip().upper()
    return ip, code, None  # endpoint doesn't carry city


def _handle_httpbin(r) -> tuple[str, str, Optional[str]]:
    """httpbin.org/ip response: {"origin": "1.2.3.4"}

    When sitting behind a proxy chain, `origin` can be a comma-separated
    list ("1.2.3.4, 5.6.7.8") — take the first which is the outermost-
    visible IP.
    """
    data = r.json()
    raw = str(data.get("origin") or "").strip()
    if not raw:
        raise ValueError("no origin field")
    ip = raw.split(",", 1)[0].strip()
    return ip, "", None


def _handle_ipinfo(r) -> tuple[str, str, Optional[str]]:
    data = r.json()
    ip = str(data.get("ip") or "").strip()
    if not ip:
        raise ValueError("no ip field")
    code = str(data.get("country") or "").strip().upper()
    city = str(data.get("city") or "").strip() or None
    return ip, code, city


def _handle_ipify(r) -> tuple[str, str, Optional[str]]:
    # IP-only service. We get the IP, but country is empty — UI will
    # show just "Ваш IP: X.X.X.X" without the country suffix.
    data = r.json()
    ip = str(data.get("ip") or "").strip()
    if not ip:
        raise ValueError("no ip field")
    return ip, "", None


def _handle_ifconfig_co(r) -> tuple[str, str, Optional[str]]:
    data = r.json()
    ip = str(data.get("ip") or "").strip()
    if not ip:
        raise ValueError("no ip field")
    code = str(data.get("country_iso") or "").strip().upper()
    city = str(data.get("city") or "").strip() or None
    return ip, code, city


_PROBE_ENDPOINTS: list[tuple[str, callable]] = [
    # NEW (v1.10.4): not-typically-blocked endpoints first. Both give IP;
    # api.myip.com adds country code in one shot.
    ("https://api.myip.com",                  _handle_api_myip),
    ("https://httpbin.org/ip",                _handle_httpbin),
    # Old fallbacks. AdGuard typically blocks all three of these but
    # they work fine for users on System/Cloudflare/Quad9 DNS where
    # there's no domain-level adblock layer.
    ("https://ipinfo.io/json",                _handle_ipinfo),
    ("https://api.ipify.org?format=json",     _handle_ipify),
    ("https://ifconfig.co/json",              _handle_ifconfig_co),
]


def _country_display(code: str, fallback: str, locale: str) -> str:
    """Map ISO code → display name. Russian table when locale=='ru',
    otherwise return whatever ipinfo.io gave us in `country` (English).
    """
    code = (code or "").upper()
    if locale == "ru" and code in _RU_COUNTRY_NAMES:
        return _RU_COUNTRY_NAMES[code]
    return fallback or code or ""


def fetch_public_ip(
    socks_proxy: Optional[str] = None,
    timeout: float = 5.0,
    locale: str = "ru",
    debug: Optional[Callable[[str], None]] = None,
) -> Optional[PublicIp]:
    """Return the public IP + country as seen by ipinfo.io, or None on
    any failure (timeout, network error, malformed response, etc.).

    socks_proxy: if set (e.g. "127.0.0.1:2081"), route the probe through
    this SOCKS5 — used in HTTP-proxy mode so we see the VPN server's IP
    instead of the local one. In TUN mode pass None — the system route
    table already sends everything through the tunnel.

    debug: optional callback(str) — when provided, gets one line per
    significant step (start / endpoint chosen / response status / error
    type). Off by default so we don't spam the Logs page on every
    connect. v1.10.1 wires this to the in-app log so silent probe
    failures stop being invisible.

    Failure is silent (None return): the UI showing "Ваш IP: ..." is a
    nice-to-have, not a hard requirement. We never raise here; the worst
    case is the label stays empty and the user falls back to their old
    habit of checking ipleak.net manually.
    """
    def _say(msg: str) -> None:
        if debug:
            try:
                debug(msg)
            except Exception:
                pass  # debug callback misbehaving must not break the probe

    proxies: Optional[dict[str, str]] = None
    if socks_proxy:
        # requests' socks support comes from PySocks. In a PyInstaller
        # bundle PySocks isn't auto-detected (urllib3 imports it
        # dynamically only when a socks:// URL is actually used) — the
        # spec file's hiddenimports=['socks'] makes sure it ships.
        # socks5h:// means resolve the hostname on the proxy side too —
        # ipinfo.io shouldn't leak via local DNS while we're testing
        # what's behind the tunnel.
        proxies = {
            "http":  f"socks5h://{socks_proxy}",
            "https": f"socks5h://{socks_proxy}",
        }
        _say(f"[ip-probe] starting via SOCKS5 {socks_proxy} (HTTP mode)")
    else:
        _say("[ip-probe] starting direct (TUN mode — kernel routes through tunnel)")

    # Try each endpoint in order until one succeeds. Per-endpoint
    # timeout is shorter than the total budget so a single dead
    # endpoint doesn't eat the whole probe window.
    per_endpoint_timeout = max(2.0, timeout / len(_PROBE_ENDPOINTS))

    return _probe_with_fallback(
        proxies, per_endpoint_timeout, locale, _say,
    )


def _probe_with_fallback(
    proxies: Optional[dict],
    per_endpoint_timeout: float,
    locale: str,
    _say: Callable[[str], None],
) -> Optional[PublicIp]:
    """The actual probe loop, factored out so the IPv4-force context
    manager wraps it cleanly without nested indentation.
    """
    last_error = "no endpoints tried"

    # Force IPv4 for the entire probe — see _force_ipv4 docstring.
    # Without this, on IPv6-enabled hosts the probe leaks the user's
    # real IPv6 (TUN tunnels IPv4 only). v1.10.2 user saw their real
    # Beeline-Moscow v6 instead of the VPN's v4 — that's the bug
    # this context manager fixes.
    with _force_ipv4():
        for url, handler in _PROBE_ENDPOINTS:
            host = url.split("//", 1)[-1].split("/", 1)[0]
            try:
                r = requests.get(
                    url,
                    timeout=per_endpoint_timeout,
                    proxies=proxies,
                    headers={"User-Agent": "KaproVPN/ip-probe"},
                )
            except requests.exceptions.Timeout:
                last_error = f"{host}: timeout after {per_endpoint_timeout:.1f}s"
                _say(f"[ip-probe] {host}: timeout, trying next endpoint")
                continue
            except requests.exceptions.ConnectionError as e:
                # AdGuard DNS NXDOMAIN'ing the endpoint shows up here as
                # NameResolutionError → ConnectionError. PySocks-missing
                # on bundled .exe also lands here.
                last_error = f"{host}: connection failed: {e}"
                _say(f"[ip-probe] {host}: connection failed (likely blocked/no-DNS), trying next")
                continue
            except Exception as e:
                last_error = f"{host}: {type(e).__name__}: {e}"
                _say(f"[ip-probe] {host}: {type(e).__name__}, trying next")
                continue

            if r.status_code != 200:
                last_error = f"{host}: HTTP {r.status_code}"
                _say(f"[ip-probe] {host}: HTTP {r.status_code}, trying next")
                continue

            try:
                ip, code, city = handler(r)
            except Exception as e:
                last_error = f"{host}: handler failed: {type(e).__name__}: {e}"
                _say(f"[ip-probe] {host}: handler failed: {type(e).__name__}: {e}, trying next")
                continue

            # Belt-and-suspenders: never surface an IPv6 as "your IP". If
            # one slipped through (monkeypatch raced, or HTTP-mode socks5h
            # resolved AAAA remotely), it's the user's leaked real address —
            # skip it and try the next endpoint for the v4 egress.
            if not _looks_ipv4(ip):
                last_error = f"{host}: got non-IPv4 {ip!r} (leak?) — skipping"
                _say(f"[ip-probe] {host}: got non-IPv4 {ip!r}, skipping (would be a leak)")
                continue

            # Success. country_code may be empty (ipify is IP-only) — that's
            # fine, the UI just shows the IP without country suffix.
            name = _country_display(code, fallback=code, locale=locale)
            _say(f"[ip-probe] OK via {host}: {ip} {code or '(no country)'} {city or ''}".rstrip())
            return PublicIp(ip=ip, country_code=code, country_name=name, city=city)

    _say(f"[ip-probe] all endpoints failed. Last error: {last_error}")
    return None
