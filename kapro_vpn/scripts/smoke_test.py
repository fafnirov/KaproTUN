"""Pre-release smoke test — gates GH Actions release publishing.

Runs on ubuntu-latest before the platform-specific build matrix. Catches
regressions that would otherwise reach users only after they download
and try to launch.

What we check, in order of "likely to break":

  1. Modules import. The most common regression we ship is "I changed
     X, forgot it's imported in Y at module level on platform Z."
     A simple `import` of every entry point + core module catches that.

  2. Parser eats each share-URL scheme. Synthetic URLs with placeholder
     credentials — no real secrets in the repo. Each must produce a
     ProxyConfig with the expected protocol.

  3. xray-config generation produces JSON-serialisable output for each
     parsed config, with the proxy outbound first (so default routing
     works) and at least one routing rule (so split-routing doesn't
     silently break).

Exit 0 = green light, build matrix runs. Exit 1 = smoke failure, no
release published, the user's `git push v1.x.x` shows red.
"""
from __future__ import annotations

import json
import sys
from typing import Callable


# ---------------------------------------------------------------------------
# Synthetic share URLs — placeholder grammar, no real keys/passwords.
# When you bump these, keep them obviously-fake (UUIDs of all-a's, etc).
# ---------------------------------------------------------------------------

SAMPLE_URLS: list[tuple[str, str]] = [
    (
        "vless",
        "vless://aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@1.2.3.4:443"
        "?type=tcp&security=reality"
        "&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "&sid=01&fp=chrome#Test-VLESS",
    ),
    (
        "trojan",
        "trojan://password@1.2.3.4:443?security=tls&sni=example.com#Test-Trojan",
    ),
    (
        "vmess",
        # base64 of {"add":"1.2.3.4","port":443,"id":"aaaa-bbbb","aid":0,"net":"tcp"}
        "vmess://eyJhZGQiOiIxLjIuMy40IiwicG9ydCI6NDQzLCJpZCI6ImFhYWEtYmJiYi"
        "1jY2NjLWRkZGQtZWVlZWVlZWVlZWVlIiwiYWlkIjowLCJuZXQiOiJ0Y3AifQ==",
    ),
    (
        "shadowsocks",
        # base64 of aes-256-gcm:password
        "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@1.2.3.4:8388#Test-SS",
    ),
    (
        "hysteria2",
        "hysteria2://password@1.2.3.4:443?sni=example.com#Test-HY2",
    ),
]


# ---------------------------------------------------------------------------
# Test harness — tiny custom runner, no pytest dep
# ---------------------------------------------------------------------------

failures: list[str] = []


def section(name: str) -> None:
    print(f"\n=== {name} ===")


def check(label: str, fn: Callable[[], None]) -> None:
    try:
        fn()
        print(f"  OK   {label}")
    except Exception as e:
        msg = f"{label}: {type(e).__name__}: {e}"
        failures.append(msg)
        print(f"  FAIL {label}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Test 1 — module imports
# ---------------------------------------------------------------------------

section("Module imports")


def _import_main() -> None:
    from kapro_vpn import main as _main  # noqa: F401


def _import_core() -> None:
    from kapro_vpn.core import (  # noqa: F401
        controller, parser, xray_config, storage, paths,
        subscription, geoip_ru, killswitch, i18n, system_proxy,
        ip_probe, dns_options, secrets_store, ipv6_block,
        bandwidth_history, webrtc_block,
    )


def _import_gui() -> None:
    # GUI modules touch PySide6 at import time — runs under
    # xvfb-style headless mode on the smoke runner.
    from kapro_vpn.gui import (  # noqa: F401
        main_window, tray, widgets, onboarding,
        configs_picker, subscription_dialog, sites_dialog,
        world_map, bandwidth_chart, stats_page,
    )


check("kapro_vpn.main", _import_main)
check("kapro_vpn.core.*", _import_core)
check("kapro_vpn.gui.*", _import_gui)


# ---------------------------------------------------------------------------
# Test 2 — parser eats each scheme
# ---------------------------------------------------------------------------

section("Parser — synthetic share URLs")

from kapro_vpn.core.parser import parse, ParseError, ProxyConfig

parsed: dict[str, ProxyConfig] = {}


def _make_parse_check(label: str, url: str):
    def inner() -> None:
        cfg = parse(url)
        if cfg.protocol != label:
            raise AssertionError(
                f"expected protocol={label}, got {cfg.protocol}"
            )
        if not cfg.outbound.get("server"):
            raise AssertionError("outbound.server is empty")
        parsed[label] = cfg
    return inner


for label, url in SAMPLE_URLS:
    check(label, _make_parse_check(label, url))


# ---------------------------------------------------------------------------
# Test 3 — xray config generation
# ---------------------------------------------------------------------------
# hysteria2 is parsed but Xray-core doesn't run it (raises
# NotImplementedError in proxy_to_xray_outbound). That's expected —
# skip it for the build_config check.

section("xray-core config generation")

from kapro_vpn.core.xray_config import build_config


def _make_xray_check(label: str, cfg: ProxyConfig):
    def inner() -> None:
        full = build_config(cfg, direct_domains=["example.com", "gosuslugi.ru"])
        # Must be JSON-serializable (we write it to disk).
        json.dumps(full, ensure_ascii=False)
        # First outbound must be proxy — xray uses outbounds[0] as default.
        if full["outbounds"][0]["tag"] != "proxy":
            raise AssertionError(
                f"first outbound should be 'proxy', got {full['outbounds'][0]['tag']}"
            )
        # Routing must have at least the api-in rule + geoip:private rule.
        if len(full["routing"]["rules"]) < 2:
            raise AssertionError("routing rules count too low")
    return inner


for label, cfg in parsed.items():
    if label in ("hysteria2", "hy2"):
        continue
    check(label, _make_xray_check(label, cfg))


# ---------------------------------------------------------------------------
# Test 4 — DNS options (v1.9.0)
# ---------------------------------------------------------------------------
# Each option must produce a valid config. "system" should NOT add a dns
# block (xray complains about empty servers). Named options must add the
# block, force IPv4, and include the bypass-by-IP routing rule for that
# service's plain IPs.

section("DNS options")

from kapro_vpn.core import dns_options

_vless_cfg = parsed["vless"]


def _make_dns_check(opt_key: str):
    opt = dns_options.get(opt_key)

    def inner() -> None:
        full = build_config(
            _vless_cfg,
            direct_domains=["example.com"],
            dns_option=opt_key,
        )
        json.dumps(full, ensure_ascii=False)  # must remain serialisable

        if opt_key == "system":
            if "dns" in full:
                raise AssertionError(
                    "system DNS option should NOT emit a dns block "
                    "(xray rejects empty servers)"
                )
            return

        # Named option (adguard/cloudflare/quad9): dns block must exist
        # with the expected DoH endpoints and IPv4-only strategy.
        if "dns" not in full:
            raise AssertionError(f"{opt_key}: dns block missing")
        if full["dns"].get("queryStrategy") != "UseIPv4":
            raise AssertionError(
                f"{opt_key}: queryStrategy must be UseIPv4"
            )
        servers = full["dns"].get("servers", [])
        if servers != opt.doh_servers:
            raise AssertionError(
                f"{opt_key}: dns servers mismatch — got {servers}, "
                f"expected {opt.doh_servers}"
            )

        # And the plain IPs of that service must be in a direct-route rule,
        # otherwise an app doing its own DoH-over-443 directly to those IPs
        # would tunnel through the VPN unnecessarily.
        wanted = {f"{ip}/32" for ip in opt.bypass_ips}
        found_in_direct = False
        for rule in full["routing"]["rules"]:
            if rule.get("outboundTag") != "direct":
                continue
            ips = set(rule.get("ip", []))
            if wanted.issubset(ips):
                found_in_direct = True
                break
        if not found_in_direct:
            raise AssertionError(
                f"{opt_key}: bypass_ips {sorted(wanted)} not found in any "
                f"direct-outbound routing rule"
            )

        # v1.9.1: AdGuard option (and ONLY adguard) must add a block-rule
        # for geosite:category-ads-all. The other named options stay
        # non-filtering — that's their positioning. Catches regression
        # where someone moves the block rule out of the `if adguard` guard.
        has_ad_block = any(
            rule.get("outboundTag") == "block"
            and "geosite:category-ads-all" in rule.get("domain", [])
            for rule in full["routing"]["rules"]
        )
        if opt_key == "adguard" and not has_ad_block:
            raise AssertionError(
                "adguard must add a block-rule for geosite:category-ads-all "
                "(the OS-DNS-only mechanism doesn't catch what browsers do "
                "with their own DoH — this rule is the real ad-block)"
            )
        if opt_key != "adguard" and has_ad_block:
            raise AssertionError(
                f"{opt_key}: should NOT have ad-block rule (only adguard "
                "is positioned as the ad-blocking option)"
            )

    return inner


for opt in dns_options.OPTIONS:
    check(f"dns_option={opt.key}", _make_dns_check(opt.key))


# ---------------------------------------------------------------------------
# Test 5 — IP probe graceful failure (v1.10.0)
# ---------------------------------------------------------------------------
# fetch_public_ip MUST return None (not raise) on any network error, so
# the GUI worker thread can safely swallow the result and hide the IP
# label. Test against a guaranteed-dead SOCKS5 endpoint (an unbound
# high port on localhost) — that simulates the post-connect race where
# the probe fires before xray's SOCKS5 inbound is fully up, or the
# CI runner where there's no network at all.

section("IP probe — graceful failure on bad endpoint")

from kapro_vpn.core import ip_probe as _ip_probe


def _probe_returns_none_on_dead_socks() -> None:
    # 127.0.0.1:1 — well-known "nothing listens here" port. Probe must
    # not raise; must return None within timeout.
    result = _ip_probe.fetch_public_ip(socks_proxy="127.0.0.1:1", timeout=2.0)
    if result is not None:
        raise AssertionError(
            f"expected None on dead SOCKS, got {result!r}"
        )


def _probe_locale_table_has_common_countries() -> None:
    # If someone strips _RU_COUNTRY_NAMES we'd silently fall back to
    # raw ISO codes in UI ("NL" instead of "Нидерланды"). Cheap guard
    # that the table still covers the common VPN-server countries.
    for code in ("NL", "DE", "US", "GB"):
        if code not in _ip_probe._RU_COUNTRY_NAMES:
            raise AssertionError(f"missing RU name for {code}")


check("fetch_public_ip returns None on dead SOCKS",
      _probe_returns_none_on_dead_socks)
check("RU country table covers common VPN locales",
      _probe_locale_table_has_common_countries)


def _probe_restores_getaddrinfo() -> None:
    # v1.10.3: probe monkey-patches socket.getaddrinfo for IPv4-only
    # resolution during the call. If the `finally` doesn't restore the
    # original, every subsequent socket.getaddrinfo in the whole app
    # becomes IPv4-only forever — silent breakage of v6-needing code
    # paths. Regression guard.
    import socket as _socket
    original = _socket.getaddrinfo
    _ip_probe.fetch_public_ip(socks_proxy="127.0.0.1:1", timeout=1.0)
    if _socket.getaddrinfo is not original:
        raise AssertionError(
            "socket.getaddrinfo was not restored after fetch_public_ip"
        )


check("probe restores socket.getaddrinfo after running",
      _probe_restores_getaddrinfo)


# ---------------------------------------------------------------------------
# Test 5.5 — IPv6 leak block (v1.11.0) — basic invariants
# ---------------------------------------------------------------------------
# Doesn't actually shell out to netsh — that needs admin and Windows.
# Just checks the module's public surface is sane and the no-op paths
# don't raise on Linux/macOS where the feature isn't supported yet.

section("IPv6 leak block — module sanity")

from kapro_vpn.core import ipv6_block as _ipv6_block


def _ipv6_block_silent_on_unsupported() -> None:
    # On the CI runner (ubuntu-latest) is_supported() returns False;
    # install/remove/is_active must all be silent no-ops, never raise.
    # On Windows dev box is_supported() is True but install/remove will
    # fail without admin — we don't check rc, we just check no-raise.
    try:
        _ipv6_block.is_supported()
        _ipv6_block.remove()
        _ipv6_block.is_active()
    except Exception as e:
        raise AssertionError(
            f"ipv6_block surface methods must never raise: {type(e).__name__}: {e}"
        )


def _ipv6_block_uses_global_unicast_only() -> None:
    # Regression guard for the design choice: we block 2000::/3 ONLY,
    # not link-local / multicast / loopback. If someone changes the
    # constant to ::/0 or ipv6 (= all v6) they'd break LAN IPv6 devices
    # (NAS, AirPlay, printers on link-local). Fail fast in CI.
    if _ipv6_block._IPV6_GLOBAL_UNICAST != "2000::/3":
        raise AssertionError(
            f"IPv6 block range must be 2000::/3 (global unicast only) — "
            f"got {_ipv6_block._IPV6_GLOBAL_UNICAST!r}. Broader ranges "
            f"would break LAN IPv6."
        )


check("ipv6_block surface no-raise on every platform",
      _ipv6_block_silent_on_unsupported)
check("ipv6_block targets 2000::/3 only (LAN-preserving)",
      _ipv6_block_uses_global_unicast_only)


# ---------------------------------------------------------------------------
# Test 5.7 — WebRTC leak block (v1.16.0)
# ---------------------------------------------------------------------------
# Same surface-shape contract as ipv6_block: every public function must
# return cleanly on every platform (no raises). Plus a port-list sanity:
# we want STUN ports only — NOT random UDP ports that would break DNS
# (53), QUIC (443), VoIP, or anything else.

section("WebRTC leak block — module sanity")

from kapro_vpn.core import webrtc_block as _webrtc_block


def _webrtc_block_silent_on_unsupported() -> None:
    """install/remove/is_active/is_supported must NEVER raise on macOS or
    Linux. The desktop client doesn't ship a non-Windows WebRTC block
    yet, but the call sites still exist and need to noop cleanly.
    """
    try:
        _webrtc_block.is_supported()
        _webrtc_block.remove()  # delete-rule which doesn't exist must be a noop
        _webrtc_block.is_active()
    except Exception as e:
        raise AssertionError(
            f"webrtc_block surface methods must never raise: {type(e).__name__}: {e}"
        ) from e


def _webrtc_block_targets_stun_ports_only() -> None:
    """STUN ports only — DNS (53), QUIC (443), normal UDP services must
    stay reachable. If someone widens the port list to a catch-all
    range like '1-65535', the regression bricks every UDP-using app.
    """
    ports = _webrtc_block._STUN_PORTS
    # Must contain the canonical RFC 5389 STUN port + Google's range.
    for required in ("3478", "5349", "19302"):
        if required not in ports:
            raise AssertionError(
                f"webrtc_block STUN port list missing {required}: {ports!r}"
            )
    # Build the set of every individual port the rule would block.
    # netsh format is comma-separated singles + ranges (M-N), so we
    # need to expand ranges to check coverage. Substring matching
    # (the first cut of this test) false-positives on "53" inside
    # "5349" — explicit parse is correct.
    blocked: set[int] = set()
    for token in ports.split(","):
        token = token.strip()
        if "-" in token:
            lo, hi = token.split("-", 1)
            for p in range(int(lo), int(hi) + 1):
                blocked.add(p)
        else:
            blocked.add(int(token))
    # Common service ports that must NEVER be in the blocked set.
    # If any of these slip in we'd break the OS in painful ways.
    for forbidden in (53, 67, 68, 80, 123, 137, 138, 443, 500, 4500):
        if forbidden in blocked:
            raise AssertionError(
                f"webrtc_block port list includes protected port "
                f"{forbidden} — would break critical UDP service. "
                f"Full blocked set: {sorted(blocked)}"
            )
    # Sanity ceiling: total blocked ports shouldn't be more than ~20
    # — STUN's range is tight, anything wider suggests a typo.
    if len(blocked) > 20:
        raise AssertionError(
            f"webrtc_block now blocks {len(blocked)} ports — STUN range "
            f"shouldn't need more than ~10. Catch-all regression? "
            f"Set: {sorted(blocked)}"
        )


check("webrtc_block surface no-raise on every platform",
      _webrtc_block_silent_on_unsupported)
check("webrtc_block targets STUN ports only (DNS/QUIC safe)",
      _webrtc_block_targets_stun_ports_only)


# ---------------------------------------------------------------------------
# Test 5.6 — Configs-picker search filter (v1.12.0)
# ---------------------------------------------------------------------------
# The matcher is a pure static method on the dialog class — no Qt needed
# to test it. Confirms the match dimensions: name, server IP, port,
# protocol. Regression guards against someone narrowing the haystack
# back to just `cfg.name` (which would break "search by IP block" and
# "search by protocol" — the two cases that justify the feature for
# users with 20+ servers from a subscription).

section("Configs-picker search matcher")

from kapro_vpn.gui.configs_picker import ConfigsPickerDialog as _Picker

# Synthetic config — no real credentials. Mirrors what a typical
# subscription entry looks like.
_test_cfg = ProxyConfig(
    name="🇫🇮 Финляндия WI-FI",
    protocol="vless",
    raw_url="vless://aaaa@1.2.3.4:443?#test",
    outbound={"server": "1.2.3.4", "server_port": 443},
)


def _picker_matcher_finds_by_name() -> None:
    if not _Picker._matches(_test_cfg, "финляндия"):
        raise AssertionError("matcher must find 'финляндия' in cfg.name")


def _picker_matcher_finds_by_ip_prefix() -> None:
    if not _Picker._matches(_test_cfg, "1.2.3"):
        raise AssertionError("matcher must find '1.2.3' in cfg.outbound.server")


def _picker_matcher_finds_by_port() -> None:
    if not _Picker._matches(_test_cfg, "443"):
        raise AssertionError("matcher must find '443' in cfg.outbound.server_port")


def _picker_matcher_finds_by_protocol() -> None:
    if not _Picker._matches(_test_cfg, "vless"):
        raise AssertionError("matcher must find 'vless' in cfg.protocol")


def _picker_matcher_misses_unrelated() -> None:
    if _Picker._matches(_test_cfg, "trojan"):
        raise AssertionError("matcher false-positive on unrelated 'trojan'")


check("picker search: by name (RU substring)",      _picker_matcher_finds_by_name)
check("picker search: by IP block prefix",          _picker_matcher_finds_by_ip_prefix)
check("picker search: by port",                     _picker_matcher_finds_by_port)
check("picker search: by protocol",                 _picker_matcher_finds_by_protocol)
check("picker search: misses unrelated query",      _picker_matcher_misses_unrelated)


# ---------------------------------------------------------------------------
# Test 5.7 — Theme system (v1.13.0)
# ---------------------------------------------------------------------------
# Two pre-built QSS strings + a selector function. Smoke checks both
# sheets render without ValueError (any unresolved {field} in the
# f-string would raise KeyError at build time), that the selector
# returns distinct strings per theme, and that "dark" sheet doesn't
# accidentally have white-text values that'd suggest a light/dark
# mix-up (regression guard against typo in palette wiring).

section("Themes — dark + light")

from kapro_vpn.gui import styles as _styles


def _both_qss_built() -> None:
    if not _styles.DARK_QSS or len(_styles.DARK_QSS) < 1000:
        raise AssertionError("DARK_QSS missing or suspiciously short")
    if not _styles.LIGHT_QSS or len(_styles.LIGHT_QSS) < 1000:
        raise AssertionError("LIGHT_QSS missing or suspiciously short")


def _qss_themes_differ() -> None:
    # If the two sheets are character-identical, the palette wiring is
    # broken (probably LIGHT_PALETTE references DARK_PALETTE constants).
    if _styles.DARK_QSS == _styles.LIGHT_QSS:
        raise AssertionError("DARK_QSS and LIGHT_QSS are identical — wiring broken")


def _selector_picks_explicit_theme() -> None:
    if _styles.get_qss("light") != _styles.LIGHT_QSS:
        raise AssertionError("get_qss('light') didn't return LIGHT_QSS")
    if _styles.get_qss("dark") != _styles.DARK_QSS:
        raise AssertionError("get_qss('dark') didn't return DARK_QSS")


def _palettes_keep_brand_accent() -> None:
    # Amber #f59e0b is the KaproVPN brand color — both themes must
    # use it for ACCENT so the visual identity stays consistent.
    # If someone "rebrands" one of them to a different hue, smoke
    # catches it before users see a confused UI.
    if _styles.DARK_PALETTE.ACCENT.lower() != "#f59e0b":
        raise AssertionError(
            f"DARK accent must be brand amber #f59e0b, got "
            f"{_styles.DARK_PALETTE.ACCENT}"
        )
    if _styles.LIGHT_PALETTE.ACCENT.lower() != "#f59e0b":
        raise AssertionError(
            f"LIGHT accent must be brand amber #f59e0b, got "
            f"{_styles.LIGHT_PALETTE.ACCENT}"
        )


def _backcompat_constants_still_export() -> None:
    # widgets.py and onboarding.py import `styles.ACCENT`, `styles.TEXT_MUTED`
    # directly. The Palette-refactor in v1.13.0 added back-compat aliases
    # so those still work. If someone removes them — instant ImportError
    # at app launch. Guard.
    for name in ("BG", "SURFACE", "BORDER", "TEXT", "TEXT_MUTED",
                 "TEXT_DIM", "ACCENT", "ACCENT_HI", "ACCENT_DIM", "DANGER"):
        if not hasattr(_styles, name):
            raise AssertionError(f"backcompat constant styles.{name} missing")


check("DARK_QSS and LIGHT_QSS both build",         _both_qss_built)
check("DARK and LIGHT sheets are distinct",        _qss_themes_differ)
check("get_qss selector returns correct sheet",    _selector_picks_explicit_theme)
check("both palettes keep brand amber accent",     _palettes_keep_brand_accent)
check("widgets.py backcompat constants exported",  _backcompat_constants_still_export)


# ---------------------------------------------------------------------------
# Test 5.8 — World map widget (v1.14.0)
# ---------------------------------------------------------------------------
# COUNTRY_COORDS coverage check + a couple of invariants. Doesn't try
# to instantiate the widget headless — needs QApplication, and the
# installer-flow section above already sets one up but it's later in
# the file. Module-level checks only.

section("World map — coords + projection sanity")

from kapro_vpn.gui import world_map as _world_map


def _world_map_covers_common_vpn_countries() -> None:
    # Reflection of dns_options.py country names — the typical VPN
    # locations we display in the IP probe. If someone removes one
    # of these from COUNTRY_COORDS, that country's pin silently
    # disappears and the map looks broken to whoever's connected
    # through it. Regression guard.
    required = {"NL", "DE", "FI", "US", "GB", "FR", "RU", "JP", "SG"}
    missing = required - set(_world_map.COUNTRY_COORDS.keys())
    if missing:
        raise AssertionError(
            f"COUNTRY_COORDS missing common VPN locations: {sorted(missing)}"
        )


def _world_map_projection_bounds() -> None:
    # Equirectangular projection must land any (lat, lon) inside [0,w] x [0,h].
    # Smoke checks the extreme corners — if someone breaks the projection
    # math (flipped sign, off-by-180), this catches it immediately.
    for lat, lon, expect_x, expect_y in [
        (90, -180, 0, 0),       # top-left  (north pole, antimeridian west)
        (-90, 180, 100, 50),    # bottom-right (south pole, antimeridian east)
        (0, 0, 50, 25),         # center (Gulf of Guinea)
    ]:
        pt = _world_map._project(lat, lon, 100, 50)
        if abs(pt.x() - expect_x) > 0.5 or abs(pt.y() - expect_y) > 0.5:
            raise AssertionError(
                f"projection broken for ({lat},{lon}): "
                f"got ({pt.x()},{pt.y()}), expected ({expect_x},{expect_y})"
            )


def _world_map_continent_polygons_nonempty() -> None:
    # If the polygon list is empty or malformed, the map renders as
    # pure background — visible regression with no obvious error.
    if not _world_map._CONTINENT_POLYGONS:
        raise AssertionError("no continent polygons defined")
    for i, poly in enumerate(_world_map._CONTINENT_POLYGONS):
        if len(poly) < 3:
            raise AssertionError(
                f"continent #{i} has only {len(poly)} vertices — needs 3+ for a polygon"
            )


check("world map: common VPN countries have coords",  _world_map_covers_common_vpn_countries)
check("world map: equirectangular projection sane",   _world_map_projection_bounds)
check("world map: continent polygons non-trivial",    _world_map_continent_polygons_nonempty)


def _flag_emoji_extracts_country_code() -> None:
    # v1.14.3 fallback for the "probe failed entirely" case. Pulls
    # ISO code from a leading flag emoji in the config name. If this
    # ever breaks, the map+country block disappears whenever AdGuard
    # blocks all our probe endpoints — exactly the regression v1.14.3
    # was meant to fix.
    fn = _world_map.country_code_from_flag
    cases = [
        ("🇳🇱 BMV1+ · VLESS XHTTP",      "NL"),
        ("🇫🇮 Финляндия WI-FI",          "FI"),
        ("🇩🇪 Germany — VLESS",          "DE"),
        ("🇺🇸 USA East",                  "US"),
        # No flag → None
        ("Plain Server Name",            None),
        ("",                              None),
        # Flag emoji of a country NOT in COUNTRY_COORDS — returns None
        # (we don't want a pin pointing nowhere).
        ("🇦🇶 Antarctica",                None),
    ]
    for name, expected in cases:
        got = fn(name)
        if got != expected:
            raise AssertionError(
                f"country_code_from_flag({name!r}) = {got!r}, "
                f"expected {expected!r}"
            )


check("world map: flag-emoji -> ISO code fallback",   _flag_emoji_extracts_country_code)


# ---------------------------------------------------------------------------
# Test 5.9 — Bandwidth history (v1.15.0)
# ---------------------------------------------------------------------------
# round-trip record() → recent_24h() and rolling-window cleanup.
# Uses an isolated temp dir for the db so we don't trash a developer's
# real history when running smoke locally. clear() at the end keeps
# the temp file empty for re-runs.

section("Bandwidth history — sqlite round-trip")

import tempfile as _tempfile
import time as _time
from pathlib import Path as _Path
from kapro_vpn.core import bandwidth_history as _bw
from kapro_vpn.core import paths as _paths

# Redirect the db to a temp file for the duration of this test section.
# bandwidth_history reads paths.app_data_dir() at db-open time, so we
# patch it. Original restored at the end.
_orig_data_dir = _paths.app_data_dir
_smoke_tmpdir = _Path(_tempfile.mkdtemp(prefix="kapro-smoke-bw-"))
_paths.app_data_dir = lambda: _smoke_tmpdir


def _bw_round_trip() -> None:
    _bw.clear()
    now = int(_time.time())
    _bw.record(1024, 4096, ts=now - 60)
    _bw.record(2048, 8192, ts=now - 30)
    rows = _bw.recent_24h()
    if len(rows) != 2:
        raise AssertionError(f"expected 2 rows, got {len(rows)}")
    if rows[0].up_bytes != 1024 or rows[0].down_bytes != 4096:
        raise AssertionError(f"row[0] payload wrong: {rows[0]}")
    if rows[1].up_bytes != 2048 or rows[1].down_bytes != 8192:
        raise AssertionError(f"row[1] payload wrong: {rows[1]}")


def _bw_totals() -> None:
    _bw.clear()
    now = int(_time.time())
    _bw.record(100, 200, ts=now - 60)
    _bw.record(300, 400, ts=now - 30)
    up, down = _bw.totals_24h()
    if up != 400 or down != 600:
        raise AssertionError(f"totals broken: up={up} down={down}, expected 400/600")


def _bw_zero_sample_skipped() -> None:
    # Zero deltas don't get inserted — keeps the db slim when the user
    # is connected but idle.
    _bw.clear()
    _bw.record(0, 0)
    rows = _bw.recent_24h()
    if rows:
        raise AssertionError(f"zero-sample insert should have been skipped, got {rows}")


def _bw_rolling_cleanup() -> None:
    # Records older than 24h must be auto-deleted on next write.
    _bw.clear()
    now = int(_time.time())
    _bw.record(99, 99, ts=now - 25 * 3600)  # 25h old → should get cleaned
    _bw.record(100, 100, ts=now - 60)       # fresh
    rows = _bw.recent_24h()
    if len(rows) != 1:
        raise AssertionError(
            f"rolling cleanup broken: expected 1 row, got {len(rows)}"
        )
    if rows[0].up_bytes != 100:
        raise AssertionError(
            f"wrong row survived cleanup: {rows[0]}"
        )


def _bw_negative_delta_clamped() -> None:
    # If xray restarts mid-session its cumulative counter rolls back,
    # we'd compute a negative delta — the recorder must clamp to 0 to
    # avoid polluting the chart with phantom dips.
    _bw.clear()
    _bw.record(-100, -100)
    rows = _bw.recent_24h()
    if rows:  # negative delta clamped to 0 → falls into zero-skip → no row
        raise AssertionError(
            f"negative delta should clamp+skip, got {rows}"
        )


check("bandwidth: record + recent_24h round-trip",  _bw_round_trip)
check("bandwidth: totals_24h sums correctly",       _bw_totals)
check("bandwidth: zero-byte samples not inserted",  _bw_zero_sample_skipped)
check("bandwidth: rows older than 24h auto-cleaned", _bw_rolling_cleanup)
check("bandwidth: negative deltas clamp to 0",      _bw_negative_delta_clamped)

# Restore — leave the global state clean for downstream sections that
# might depend on paths.app_data_dir() pointing at the real location.
_bw.clear()
_paths.app_data_dir = _orig_data_dir


# ---------------------------------------------------------------------------
# Test 6 — Installer flow transitions
# ---------------------------------------------------------------------------
# Catches regressions like "click does nothing because we addWidget but
# forgot setCurrentWidget" — exactly the v1.8.1 uninstall-button bug.
# We don't need a real display: QT_QPA_PLATFORM=offscreen lets the GUI
# code run headless in CI without an X server.

section("Installer flow transitions")


def _setup_qt_app() -> None:
    """One QApplication for the whole installer-test section."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])


def _make_installer_check(label: str, fn):
    def inner() -> None:
        _setup_qt_app()
        from installer.gui import InstallerWindow, MaintenancePage
        fn(InstallerWindow, MaintenancePage)
    return inner


def _install_mode_starts_on_welcome(InstallerWindow, MaintenancePage):
    from installer.gui import WelcomePage
    w = InstallerWindow(mode="install")
    cur = w.stack.currentWidget()
    if not isinstance(cur, WelcomePage):
        raise AssertionError(
            f"install mode should land on WelcomePage, got {type(cur).__name__}"
        )


def _maintenance_mode_starts_on_maintenance(InstallerWindow, MaintenancePage):
    w = InstallerWindow(mode="maintenance")
    cur = w.stack.currentWidget()
    if not isinstance(cur, MaintenancePage):
        raise AssertionError(
            f"maintenance mode should land on MaintenancePage, got {type(cur).__name__}"
        )


def _uninstall_mode_starts_on_confirm(InstallerWindow, MaintenancePage):
    # Direct --uninstall flow lands on the confirm widget (not the
    # same class as MaintenancePage). We identify by checking the
    # current widget is NOT the maintenance page (which would only
    # exist in maintenance mode anyway).
    w = InstallerWindow(mode="uninstall")
    cur = w.stack.currentWidget()
    if cur is None:
        raise AssertionError("uninstall mode left stack empty")
    # We expect a generic QWidget confirm page. Sanity: it should have
    # at least one Delete button as a child.
    from PySide6.QtWidgets import QPushButton
    btns = [b for b in cur.findChildren(QPushButton) if b.text() == "Удалить"]
    if not btns:
        raise AssertionError(
            "uninstall mode confirm page has no 'Удалить' button"
        )


def _maintenance_uninstall_button_switches_page(InstallerWindow, MaintenancePage):
    # The v1.8.1 bug: this transition silently did nothing. Now we
    # check the stack's current widget actually moves to a NEW page
    # after clicking Uninstall in Maintenance UI.
    w = InstallerWindow(mode="maintenance")
    initial = w.stack.currentWidget()
    # Trigger maintenance → uninstall path the same way the user does.
    w.maintenance.uninstall_clicked.emit()
    after = w.stack.currentWidget()
    if after is initial:
        raise AssertionError(
            "maintenance → uninstall: stack stayed on MaintenancePage "
            "(the v1.8.1 regression — setCurrentWidget was missing)"
        )


def _maintenance_reinstall_button_starts_install(InstallerWindow, MaintenancePage):
    # Unlike Uninstall (which only builds a confirm UI), Reinstall fires
    # the install worker directly — and the worker calls
    # operations.install_everything which downloads xray, writes to
    # %LOCALAPPDATA%, registers an uninstaller in HKCU. On a Linux CI
    # runner that crashes the process and the whole smoke test exits
    # non-zero, blocking the release.
    #
    # Stub install_everything to a no-op so we test the UI transition
    # without doing real work. We also strengthen the assertion: not
    # "InstallingPage exists in stack" but "currentWidget IS
    # InstallingPage" — this is the exact same shape as the v1.8.1
    # regression (addWidget without setCurrentWidget) so we want to
    # catch a Reinstall variant of it too.
    from installer import operations
    from installer.gui import InstallingPage
    original_install = operations.install_everything
    operations.install_everything = lambda **kw: None
    try:
        w = InstallerWindow(mode="maintenance")
        w.maintenance.reinstall_clicked.emit()
        cur = w.stack.currentWidget()
        if not isinstance(cur, InstallingPage):
            raise AssertionError(
                f"maintenance → reinstall should switch stack to "
                f"InstallingPage, got {type(cur).__name__} "
                f"(v1.8.1-shaped regression — setCurrentWidget missing)"
            )
        # Let the (stubbed) worker thread finish so Qt doesn't warn
        # "QThread destroyed while still running" at GC time, which
        # can manifest as a process abort on Linux.
        worker = getattr(w, "_worker", None)
        if worker is not None:
            worker.wait(2000)
    finally:
        operations.install_everything = original_install


check("install mode lands on WelcomePage",
      _make_installer_check("install->welcome", _install_mode_starts_on_welcome))
check("maintenance mode lands on MaintenancePage",
      _make_installer_check("maintenance->page", _maintenance_mode_starts_on_maintenance))
check("uninstall mode lands on confirm page",
      _make_installer_check("uninstall->confirm", _uninstall_mode_starts_on_confirm))
check("Maintenance Uninstall button actually switches page",
      _make_installer_check("regression-1.8.1", _maintenance_uninstall_button_switches_page))
check("Maintenance Reinstall button starts install flow",
      _make_installer_check("maint->install", _maintenance_reinstall_button_starts_install))


# ---------------------------------------------------------------------------
# Test 7 — StatsPage live block (v1.15.2)
# ---------------------------------------------------------------------------
# The live block on the Stats page is fed by MainWindow._poll_traffic at
# 1 Hz via on_live_sample(). on_live_disconnected() resets it when the
# tunnel drops. Both must work headlessly + be idempotent — these are
# the regression shapes:
#   - on_live_sample raising (would crash _poll_traffic mid-poll)
#   - on_live_disconnected thrashing labels when called repeatedly at
#     1 Hz while idle (the _live_connected flag is what prevents this)
#   - sparkline buffer growth bound (deque maxlen=60)

section("StatsPage — live block API")


def _stats_page_live_api() -> None:
    _setup_qt_app()
    from kapro_vpn.gui.stats_page import StatsPage

    page = StatsPage()

    # Default state: disconnected. _live_connected should be False.
    if page._live_connected:
        raise AssertionError(
            "StatsPage should start in disconnected state, but "
            "_live_connected was True"
        )

    # Push one sample → flips to connected and labels update.
    page.on_live_sample(up_bps=1024.0, down_bps=4096.0,
                        up_total=10_000, down_total=40_000)
    if not page._live_connected:
        raise AssertionError(
            "on_live_sample should flip _live_connected to True"
        )
    if page._down_rate_label.text() in ("—", ""):
        raise AssertionError(
            f"down rate label not updated: {page._down_rate_label.text()!r}"
        )

    # Many more samples — sparkline buffer must stay bounded.
    for i in range(120):
        page.on_live_sample(up_bps=float(i), down_bps=float(i * 2),
                            up_total=10_000 + i, down_total=40_000 + i)
    n = len(page.live_sparkline._down)
    if n > 60:
        raise AssertionError(
            f"live sparkline buffer should be capped at 60 (deque maxlen), "
            f"got {n}"
        )

    # Disconnect → reset.
    page.on_live_disconnected()
    if page._live_connected:
        raise AssertionError(
            "on_live_disconnected should clear _live_connected"
        )
    if "—" not in page._down_rate_label.text():
        raise AssertionError(
            f"down rate label should reset to em-dash on disconnect, "
            f"got {page._down_rate_label.text()!r}"
        )
    if len(page.live_sparkline._down) != 0:
        raise AssertionError(
            "live sparkline should be empty after disconnect"
        )

    # Idempotent — calling disconnect again while already idle must not
    # raise and must not touch internal state (the early-return guard).
    page.on_live_disconnected()
    page.on_live_disconnected()


def _stats_page_status_independent_of_data() -> None:
    """v1.15.3 regression: status badge must flip on set_live_connected()
    even when on_live_sample() has not yet been called.

    Reproduces the v1.15.2 user bug — `_poll_traffic` may return early
    on the first second after connect (xray-api stats subprocess slow
    or not ready), so on_live_sample doesn't fire. The status badge
    must still say "● Подключено" because _refresh_home pushed it via
    set_live_connected(True).
    """
    _setup_qt_app()
    from kapro_vpn.gui.stats_page import StatsPage

    page = StatsPage()

    # Simulate _refresh_home tick on a fresh connect — status flips,
    # but no sample yet.
    page.set_live_connected(True)
    if not page._live_connected:
        raise AssertionError(
            "set_live_connected(True) failed to flip _live_connected"
        )
    # Status badge text changed — that's the user-visible thing.
    badge = page._status_label.text()
    if "Подключено" not in badge or "●" not in badge:
        raise AssertionError(
            f"status badge should show '● Подключено' after "
            f"set_live_connected(True), got {badge!r}"
        )
    # Rates show placeholders, not em-dashes — the layout must look
    # alive even before data arrives.
    if page._down_rate_label.text() == "—":
        raise AssertionError(
            f"rate label still '—' after connect — should be a "
            f"'0 Б/с' placeholder, got {page._down_rate_label.text()!r}"
        )
    if page._session_label.text() == "За сессию: —":
        raise AssertionError(
            "session label still '—' after connect — should hint "
            "'считаем…'"
        )

    # Status flip should be idempotent: second call with same value
    # is a no-op (we check by ensuring no exception and state stable).
    page.set_live_connected(True)
    if not page._live_connected:
        raise AssertionError("idempotent set_live_connected(True) lost state")

    # Going back to disconnected must reset both badge and rates.
    page.set_live_connected(False)
    if page._live_connected:
        raise AssertionError(
            "set_live_connected(False) failed to flip _live_connected"
        )
    badge = page._status_label.text()
    if "Не подключено" not in badge or "○" not in badge:
        raise AssertionError(
            f"status badge should show '○ Не подключено' after "
            f"set_live_connected(False), got {badge!r}"
        )


check("StatsPage: live block sample+disconnect cycle", _stats_page_live_api)
check("StatsPage: status flips independently of data (v1.15.3)",
      _stats_page_status_independent_of_data)


# ---------------------------------------------------------------------------
# Test 8 — psutil TUN-iface stats source (v1.15.4)
# ---------------------------------------------------------------------------
# v1.15.4 replaced the unreliable `xray api stats` subprocess with a
# direct psutil read on the named TUN device. Two things to guarantee:
#   - psutil itself is importable (it's a requirement now)
#   - query_tun_iface_stats() returns None for an unknown name and a
#     valid TrafficStats with non-negative byte counters for an existing
#     interface (the loopback is always present on every OS)

section("psutil TUN-iface stats source")


def _psutil_importable() -> None:
    import psutil  # noqa: F401


def _tun_iface_stats_unknown_name() -> None:
    from kapro_vpn.core.xray_stats import query_tun_iface_stats
    s = query_tun_iface_stats("DefinitelyNotARealNIC-123456")
    if s is not None:
        raise AssertionError(
            f"query_tun_iface_stats with bogus name should return None, "
            f"got {s}"
        )


def _tun_iface_stats_real_iface() -> None:
    # Pick whatever interface psutil reports first that has non-zero
    # bytes_recv — that's always present on a CI runner (loopback,
    # primary NIC, etc.). On macOS lo0 is fine; on Linux lo; on Windows
    # the loopback pseudo-interface or the runner NIC.
    import psutil
    counters = psutil.net_io_counters(pernic=True)
    if not counters:
        # Some sandboxed CI environments hide all NICs from psutil —
        # not our bug. Skip rather than fail the build.
        return
    name = next(iter(counters.keys()))
    from kapro_vpn.core.xray_stats import query_tun_iface_stats
    s = query_tun_iface_stats(name)
    if s is None:
        raise AssertionError(
            f"query_tun_iface_stats({name!r}) returned None for a real "
            f"interface — psutil bridge broken"
        )
    if s.uplink_bytes < 0 or s.downlink_bytes < 0:
        raise AssertionError(
            f"negative byte counters: up={s.uplink_bytes} "
            f"down={s.downlink_bytes}"
        )
    if s.timestamp <= 0:
        raise AssertionError(f"timestamp not set: {s.timestamp}")


check("psutil importable",                              _psutil_importable)
check("query_tun_iface_stats: None for unknown iface",  _tun_iface_stats_unknown_name)
check("query_tun_iface_stats: real iface returns data", _tun_iface_stats_real_iface)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

print()
if failures:
    print(f"=== SMOKE TEST FAILED ({len(failures)} issue{'s' if len(failures) != 1 else ''}) ===")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("=== SMOKE TEST PASSED ===")
sys.exit(0)
