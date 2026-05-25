"""Curated DNS choices exposed in Settings → DNS-сервер.

Why this exists: many subscription providers don't filter ads at the
DNS layer, so even through their VPN you see all the usual tracking
and banners. Picking AdGuard here gives you ad-blocking that follows
you to any server. Picking Cloudflare gives you a clean, fast resolver
in case your ISP serves slow/junk DNS. Picking Quad9 gives you malware-
domain filtering. System is the "leave it alone" default.

When a non-system option is active:

  - xray-core's own DNS block uses the DoH endpoint of the chosen
    service, so domain-based routing rules (e.g. `domain:sber.ru` →
    direct) resolve against that resolver, encrypted.
  - The chosen service's IPs are added to the routing "force direct
    by IP" set so any app that does its own DoH directly to those IPs
    (Yandex.Browser, modern Chrome) doesn't tunnel that traffic — same
    spirit as the existing DNS-leak hardening for common public
    resolvers added in v1.8.0.
  - In TUN mode the chosen service is also pinned on the TUN adapter
    via netsh/ip, AND its IPs go into the OS-level bypass-routes list
    so the DoH-over-port-443 round trip doesn't take a detour through
    the VPN server.

Adding a 5th option: add a new entry to OPTIONS plus translations in
the SettingsPage. xray_config and controller already iterate; no
wiring changes needed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DnsOption:
    key: str                       # stable id stored in settings.json
    label_ru: str                  # short display name (RU)
    label_en: str                  # short display name (EN)
    hint_ru: str                   # one-line description (RU)
    hint_en: str                   # one-line description (EN)
    doh_servers: list[str]         # DoH URLs for xray's dns block, [] for system
    plain_servers: list[str]       # Plain IPv4 for TUN adapter (netsh sets these)
    bypass_ips: list[str]          # IPs that need /32 bypass-routes in TUN mode


# `system` — the leave-it-alone option. No xray dns block, no TUN override,
# no extra bypass routes. Whatever the OS resolved through DHCP wins.
SYSTEM = DnsOption(
    key="system",
    label_ru="Системный",
    label_en="System",
    hint_ru="Использовать DNS как настроен в Windows / провайдером. Без изменений.",
    hint_en="Use whatever DNS Windows/ISP gave you. Don't change anything.",
    doh_servers=[],
    plain_servers=[],
    bypass_ips=[],
)

# AdGuard DNS (94.140.14.14 / 94.140.15.15). Public, free, no logs.
# Blocks ads + trackers via DNS-level domain blocklists. Most popular
# "ad-block via DNS" option among Russian users.
ADGUARD = DnsOption(
    key="adguard",
    label_ru="AdGuard — блокирует рекламу",
    label_en="AdGuard — blocks ads",
    hint_ru="Режет рекламу и трекеры на уровне DNS. Работает на любом сайте, на любом сервере.",
    hint_en="Filters ads and trackers at the DNS level. Works on any site through any server.",
    doh_servers=["https://dns.adguard-dns.com/dns-query"],
    plain_servers=["94.140.14.14", "94.140.15.15"],
    bypass_ips=["94.140.14.14", "94.140.15.15"],
)

# Cloudflare 1.1.1.1 — fastest public resolver in most regions,
# privacy-focused (no logging, no selling). No ad blocking.
CLOUDFLARE = DnsOption(
    key="cloudflare",
    label_ru="Cloudflare 1.1.1.1 — самый быстрый",
    label_en="Cloudflare 1.1.1.1 — fastest",
    hint_ru="Быстрый и приватный DNS. Без блокировок — нужен если ваш провайдер раздаёт медленный или мусорный DNS.",
    hint_en="Fast and private. No filtering — use this if your ISP serves slow or junk DNS.",
    doh_servers=["https://1.1.1.1/dns-query", "https://1.0.0.1/dns-query"],
    plain_servers=["1.1.1.1", "1.0.0.1"],
    bypass_ips=["1.1.1.1", "1.0.0.1"],
)

# Quad9 9.9.9.9 — Swiss-hosted, security-focused. Blocks known malware
# and phishing domains via threat-intel feeds. Doesn't block ads.
QUAD9 = DnsOption(
    key="quad9",
    label_ru="Quad9 — блокирует malware-домены",
    label_en="Quad9 — blocks malware domains",
    hint_ru="Швейцарский, security-focused. Блокирует фишинг и malware, рекламу не трогает.",
    hint_en="Swiss, security-focused. Blocks phishing and malware domains; doesn't touch ads.",
    doh_servers=["https://dns.quad9.net/dns-query"],
    plain_servers=["9.9.9.9", "149.112.112.112"],
    bypass_ips=["9.9.9.9", "149.112.112.112"],
)


# Ordered list — UI iterates in this order. System first (sensible default),
# then most-likely-wanted (AdGuard for the reason this whole feature exists),
# then alternatives.
OPTIONS: list[DnsOption] = [SYSTEM, ADGUARD, CLOUDFLARE, QUAD9]
_BY_KEY: dict[str, DnsOption] = {o.key: o for o in OPTIONS}

DEFAULT_KEY = "system"


def get(key: str) -> DnsOption:
    """Look up by key. Unknown keys fall back to SYSTEM rather than raise —
    if someone hand-edits settings.json with a typo, we don't want the app
    to crash at connect; we want it to act as if they hadn't customised."""
    return _BY_KEY.get(key, SYSTEM)
