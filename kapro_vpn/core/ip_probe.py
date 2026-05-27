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
from typing import Optional

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
) -> Optional[PublicIp]:
    """Return the public IP + country as seen by ipinfo.io, or None on
    any failure (timeout, network error, malformed response, etc.).

    socks_proxy: if set (e.g. "127.0.0.1:2081"), route the probe through
    this SOCKS5 — used in HTTP-proxy mode so we see the VPN server's IP
    instead of the local one. In TUN mode pass None — the system route
    table already sends everything through the tunnel.

    Failure is silent: the UI showing "Ваш IP: ..." is a nice-to-have,
    not a hard requirement. We never raise here; the worst case is the
    label stays empty and the user falls back to their old habit of
    checking ipleak.net manually.
    """
    proxies: Optional[dict[str, str]] = None
    if socks_proxy:
        # requests' socks support comes from PySocks (already a transitive
        # dep of xray-installer's mirror downloads). socks5h:// means
        # resolve the hostname on the proxy side too — ipinfo.io shouldn't
        # leak via local DNS while we're testing what's behind the tunnel.
        proxies = {
            "http":  f"socks5h://{socks_proxy}",
            "https": f"socks5h://{socks_proxy}",
        }

    try:
        r = requests.get(
            "https://ipinfo.io/json",
            timeout=timeout,
            proxies=proxies,
            headers={"User-Agent": "KaproVPN/ip-probe"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    ip = str(data.get("ip") or "").strip()
    if not ip:
        return None

    code = str(data.get("country") or "").strip().upper()
    city = str(data.get("city") or "").strip() or None
    name = _country_display(code, fallback=code, locale=locale)

    return PublicIp(ip=ip, country_code=code, country_name=name, city=city)
