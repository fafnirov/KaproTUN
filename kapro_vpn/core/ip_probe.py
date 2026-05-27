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

from dataclasses import dataclass
from typing import Callable, Optional

import requests


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
# a parseable IP wins. Multiple endpoints needed because v1.10.1 user
# discovered AdGuard DNS classifies `ipinfo.io` as a tracking domain
# and NXDOMAINs it — our own ad-block feature killed our own probe.
#
# Order matters: ipinfo.io first because it gives full geo (country +
# city) in one shot. Fallbacks are IP-only or country-only — better
# than no info at all, and crucially they're "boring IP services"
# rather than "analytics endpoints" so they're rarely in blocklists.
#
# Each entry: (url, parser_callable). Parser takes the parsed JSON,
# returns (ip, country_code, city) tuple or raises on bad data.
def _parse_ipinfo(data: dict) -> tuple[str, str, Optional[str]]:
    ip = str(data.get("ip") or "").strip()
    if not ip:
        raise ValueError("no ip field")
    code = str(data.get("country") or "").strip().upper()
    city = str(data.get("city") or "").strip() or None
    return ip, code, city


def _parse_ipify(data: dict) -> tuple[str, str, Optional[str]]:
    # IP-only service. We get the IP, but country is empty — UI will
    # show just "Ваш IP: X.X.X.X" without the country suffix. Still
    # better than the label staying hidden.
    ip = str(data.get("ip") or "").strip()
    if not ip:
        raise ValueError("no ip field")
    return ip, "", None


def _parse_ifconfig_co(data: dict) -> tuple[str, str, Optional[str]]:
    ip = str(data.get("ip") or "").strip()
    if not ip:
        raise ValueError("no ip field")
    # ifconfig.co returns full country name ("Netherlands") in `country`
    # and 2-letter code in `country_iso`. We want the code for our
    # localization table lookup.
    code = str(data.get("country_iso") or "").strip().upper()
    city = str(data.get("city") or "").strip() or None
    return ip, code, city


_PROBE_ENDPOINTS: list[tuple[str, callable]] = [
    ("https://ipinfo.io/json",         _parse_ipinfo),
    ("https://api.ipify.org?format=json", _parse_ipify),
    ("https://ifconfig.co/json",       _parse_ifconfig_co),
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

    last_error = "no endpoints tried"
    for url, parser in _PROBE_ENDPOINTS:
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
            data = r.json()
        except ValueError:
            last_error = f"{host}: non-JSON response"
            _say(f"[ip-probe] {host}: non-JSON response, trying next")
            continue

        try:
            ip, code, city = parser(data)
        except Exception as e:
            last_error = f"{host}: parser failed: {e}"
            _say(f"[ip-probe] {host}: parser failed: {e}, trying next")
            continue

        # Success. country_code may be empty (ipify is IP-only) — that's
        # fine, the UI just shows the IP without country suffix.
        name = _country_display(code, fallback=code, locale=locale)
        _say(f"[ip-probe] OK via {host}: {ip} {code or '(no country)'} {city or ''}".rstrip())
        return PublicIp(ip=ip, country_code=code, country_name=name, city=city)

    _say(f"[ip-probe] all endpoints failed. Last error: {last_error}")
    return None
