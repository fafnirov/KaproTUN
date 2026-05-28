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
        bandwidth_history, webrtc_block, leak_test, crash_handler,
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
        # v1.16.8: leak protection is a SEPARATE toggle, default True.
        # Pass it explicitly so the check exercises the protected path
        # (the typical user setting); a sibling test below verifies
        # the opt-out OFF path produces the legacy direct :53 rules.
        full = build_config(
            _vless_cfg,
            direct_domains=["example.com"],
            dns_option=opt_key,
            dns_leak_protection=True,
        )
        json.dumps(full, ensure_ascii=False)  # must remain serialisable

        # DNS block in xray config: present only when the option has
        # its own DoH servers (system has none — falls back to a
        # Cloudflare hijack upstream but no top-level dns block).
        if opt.doh_servers:
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

        # With dns_leak_protection=True, ALL :53 (TCP + UDP) must be
        # hijacked to dns-out — independent of which DNS option is
        # selected. System falls back to a Cloudflare upstream.
        hijack_rule = next(
            (r for r in full["routing"]["rules"]
             if r.get("outboundTag") == "dns-out"
             and r.get("port") == "53"),
            None,
        )
        if hijack_rule is None:
            raise AssertionError(
                f"{opt_key} (leak protection ON): missing :53 → dns-out "
                f"hijack rule. Without it, system resolver queries go out "
                f"the VPN exit unmodified and ISP can sniff destinations."
            )
        if "tcp,udp" not in (hijack_rule.get("network") or ""):
            raise AssertionError(
                f"{opt_key}: hijack rule should cover BOTH tcp and udp "
                f":53; got network={hijack_rule.get('network')!r}"
            )

        # dns-out outbound must exist with protocol=dns and a sensible
        # upstream IP. For named options it's their first plain_server;
        # for system it's the Cloudflare fallback (1.1.1.1).
        dns_out = next(
            (o for o in full["outbounds"] if o.get("tag") == "dns-out"),
            None,
        )
        if dns_out is None:
            raise AssertionError(f"{opt_key}: dns-out outbound missing")
        if dns_out.get("protocol") != "dns":
            raise AssertionError(
                f"{opt_key}: dns-out protocol must be 'dns', got "
                f"{dns_out.get('protocol')!r}"
            )
        upstream = dns_out.get("settings", {}).get("address")
        expected_upstreams = opt.plain_servers or ["1.1.1.1", "1.0.0.1"]
        if upstream not in expected_upstreams:
            raise AssertionError(
                f"{opt_key}: dns-out address {upstream!r} not in "
                f"expected upstreams {expected_upstreams}"
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


# v1.16.8: opt-out path. With dns_leak_protection=False the config
# must NOT contain the :53 hijack rule or the dns-out outbound, AND
# must contain the legacy direct :53 rules (so Pi-hole / corp DNS
# users can actually reach their resolver). Independent of DNS option.
def _dns_leak_protection_off_produces_direct_rules() -> None:
    full = build_config(
        _vless_cfg,
        direct_domains=["example.com"],
        dns_option="system",
        dns_leak_protection=False,
    )
    json.dumps(full, ensure_ascii=False)

    # No dns-out outbound when protection is off.
    if any(o.get("tag") == "dns-out" for o in full["outbounds"]):
        raise AssertionError(
            "dns-out outbound should NOT exist when leak protection "
            "is OFF — defeats the user's Pi-hole / corp DNS choice"
        )
    if any(r.get("outboundTag") == "dns-out"
           for r in full["routing"]["rules"]):
        raise AssertionError(
            "no rule should route to dns-out when leak protection is OFF"
        )
    # Legacy direct :53 rules (UDP + TCP) must be present so user's
    # resolver of choice can actually answer.
    direct_53_udp = any(
        r.get("outboundTag") == "direct"
        and r.get("network") == "udp"
        and r.get("port") == "53"
        for r in full["routing"]["rules"]
    )
    direct_53_tcp = any(
        r.get("outboundTag") == "direct"
        and r.get("network") == "tcp"
        and r.get("port") == "53"
        for r in full["routing"]["rules"]
    )
    if not (direct_53_udp and direct_53_tcp):
        raise AssertionError(
            "leak protection OFF must restore the v1.8.0 'direct :53' "
            "rules so Pi-hole / corp DNS users can reach their resolver"
        )


check("dns_leak_protection=False: direct :53 rules restored, no hijack",
      _dns_leak_protection_off_produces_direct_rules)


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
# Test 5.8 — Frameless window resize (v1.16.1)
# ---------------------------------------------------------------------------
# WM_NCHITTEST mapping is the trickiest part of frameless resize:
# wrong border math means dead zones or click-stealing. We can test
# the hit-test geometry without a real Windows MSG by calling the
# pure-Python windows_hit_test() against a dummy widget at known
# screen coordinates.

section("Frameless window resize — hit-test geometry")


def _hit_test_corners_and_edges() -> None:
    """Pure-function hit-test — no QApplication needed."""
    from kapro_vpn.gui import window_resize as _wr

    W, H = 400, 300  # widget dimensions

    # Centre → CLIENT (no resize, Qt handles as normal mouse event).
    if _wr.hit_test_local(200, 150, W, H) != "CLIENT":
        raise AssertionError(
            f"centre should be CLIENT, got "
            f"{_wr.hit_test_local(200, 150, W, H)!r}"
        )
    # Each corner — within the 6 px border in both axes.
    for (x, y, expected) in (
        (0,     0,     "TL"),
        (W - 1, 0,     "TR"),
        (0,     H - 1, "BL"),
        (W - 1, H - 1, "BR"),
    ):
        got = _wr.hit_test_local(x, y, W, H)
        if got != expected:
            raise AssertionError(
                f"corner ({x},{y}) should be {expected!r}, got {got!r}"
            )
    # Mid-edges — within the border on only one axis.
    for (x, y, expected) in (
        (2,     150, "L"),
        (W - 2, 150, "R"),
        (200,   2,   "T"),
        (200,   H - 2, "B"),
    ):
        got = _wr.hit_test_local(x, y, W, H)
        if got != expected:
            raise AssertionError(
                f"mid-edge ({x},{y}) should be {expected!r}, got {got!r}"
            )
    # Just inside the border — off-by-one zone. Click at exactly
    # border-distance from edge should NOT be a resize zone (the
    # open inner interval in the math).
    if _wr.hit_test_local(6, 150, W, H) != "CLIENT":
        raise AssertionError(
            "x=6 (== border width) should be CLIENT — off-by-one regression"
        )


def _resize_handles_install_and_reposition() -> None:
    """Install 8 handles on a real widget, then verify reposition()
    moves them to expected geometry after a resize. Catches regressions
    where someone breaks the corner/edge layout math.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget
    if QApplication.instance() is None:
        QApplication([])
    from kapro_vpn.gui import window_resize as _wr

    w = QWidget()
    w.resize(400, 300)
    handles = _wr.ResizeHandles(w)
    handles.install()
    # 8 handles created and parented.
    if len(handles._handles) != 8:
        raise AssertionError(
            f"expected 8 resize handles, got {len(handles._handles)}"
        )
    # BR corner should be at the bottom-right.
    br = handles._by_key["BR"]
    if br.x() != 400 - _wr.RESIZE_BORDER:
        raise AssertionError(
            f"BR handle X wrong: {br.x()} vs expected "
            f"{400 - _wr.RESIZE_BORDER}"
        )
    if br.y() != 300 - _wr.RESIZE_BORDER:
        raise AssertionError(
            f"BR handle Y wrong: {br.y()} vs expected "
            f"{300 - _wr.RESIZE_BORDER}"
        )
    # Resize widget → handles must follow.
    w.resize(800, 600)
    handles.reposition()
    if br.x() != 800 - _wr.RESIZE_BORDER:
        raise AssertionError(
            f"BR did not follow resize: x={br.x()}, expected "
            f"{800 - _wr.RESIZE_BORDER}"
        )


check("window resize: hit_test_local for 8 zones + client centre",
      _hit_test_corners_and_edges)
check("window resize: 8 handles install and follow resize",
      _resize_handles_install_and_reposition)


# ---------------------------------------------------------------------------
# Test 9 — Leak self-test module (v1.16.4)
# ---------------------------------------------------------------------------
# The leak_test module is the engine behind the new "Проверить утечки"
# button. Most of it makes real network calls (we don't run those in
# CI — they're flaky offline + would hit bash.ws's rate limit on
# repeated CI builds), but we DO want to verify:
#   - The STUN-packet builder produces a valid 20-byte RFC 5389
#     Binding Request header (this is offline-safe).
#   - probe_webrtc() returns stun_blocked=True on a timeout (the
#     desired result when our firewall does its job — we simulate
#     this by pointing the probe at a closed UDP port and a short
#     timeout).
#   - The report dataclasses construct cleanly with default values
#     (the worker creates an empty report on unexpected exception).

section("Leak self-test — module sanity")


def _leak_test_dataclasses_default() -> None:
    from kapro_vpn.core import leak_test as _lt
    report = _lt.LeakTestReport()
    # Each subreport should be present with sane defaults.
    if report.ipv4.ok:
        raise AssertionError("default IPv4Result should not report ok")
    if report.webrtc.ok:
        raise AssertionError(
            "default WebRtcResult should not report ok "
            "(stun_blocked False by default)"
        )
    if report.ipv6.ipv6_blocked:
        raise AssertionError("default IPv6Result should not claim blocked")
    if report.dns.suspected_leak:
        raise AssertionError("default DnsResult shouldn't claim leak")


def _leak_test_stun_packet_shape() -> None:
    """Reconstruct the STUN packet build the same way probe_webrtc does,
    verify it's a valid RFC 5389 Binding Request (20 bytes, magic
    cookie 0x2112A442 at offset 4, message-length zero)."""
    import struct, secrets
    txid = secrets.token_bytes(12)
    packet = struct.pack("!HHI", 0x0001, 0x0000, 0x2112A442) + txid
    if len(packet) != 20:
        raise AssertionError(
            f"STUN packet should be exactly 20 bytes (header only), "
            f"got {len(packet)}"
        )
    # Magic cookie at offset 4.
    if packet[4:8] != b"\x21\x12\xA4\x42":
        raise AssertionError("STUN magic cookie wrong")
    # Message type at offset 0.
    msg_type = struct.unpack("!H", packet[0:2])[0]
    if msg_type != 0x0001:
        raise AssertionError(
            f"STUN message type should be 0x0001 (Binding Request), "
            f"got {msg_type:#x}"
        )


def _leak_test_webrtc_returns_blocked_on_timeout() -> None:
    """Point probe_webrtc at a guaranteed-unreachable address with a
    very short timeout. The desired outcome of the probe — when our
    firewall does its job — is exactly the same shape as "destination
    silently drops": stun_blocked=True. So the probe must return that
    for an unresponsive endpoint."""
    from kapro_vpn.core import leak_test as _lt

    # Patch the STUN address to TEST-NET-3 (RFC 5737 reserved doc
    # range, guaranteed not routable) so the packet times out fast
    # without actually hitting a real STUN server.
    import socket as _socket
    real_sendto = _socket.socket.sendto

    # Easier: monkey-patch probe_webrtc's internal socket calls by
    # replacing the address at send time. Cleanest is to override
    # the STUN host constant via the module if it existed — but it's
    # inlined in the function. So we use a brief monkeypatch on
    # the socket sendto: redirect any sendto to 127.0.0.1:1 (port
    # 1 is reserved, packets dropped).
    def fake_sendto(self, data, address):
        return real_sendto(self, data, ("127.0.0.1", 1))
    _socket.socket.sendto = fake_sendto
    try:
        # Probe with very short timeout — would otherwise take 2 s.
        result = _lt.probe_webrtc(timeout=0.3)
    finally:
        _socket.socket.sendto = real_sendto
    if not result.stun_blocked:
        raise AssertionError(
            "probe_webrtc should report stun_blocked=True on timeout, "
            f"got blocked={result.stun_blocked} error={result.error!r}"
        )


check("leak_test: dataclasses construct with sane defaults",
      _leak_test_dataclasses_default)
check("leak_test: STUN binding request packet is RFC-shaped",
      _leak_test_stun_packet_shape)
check("leak_test: probe_webrtc returns blocked on timeout",
      _leak_test_webrtc_returns_blocked_on_timeout)


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
# Test 11 — corrupted local files don't crash startup (v1.16.11)
# ---------------------------------------------------------------------------
# A stray non-utf8 byte in settings.json / sites.json (partial write, AV
# quarantine restore, disk corruption) used to raise UnicodeDecodeError at
# launch. Because it's a *startup* crash, the in-app auto-updater never got
# a chance to ship the fix — the user was stuck. load_settings / load_sites
# must degrade to defaults instead of raising.

section("Corrupted local files — no startup crash")

from kapro_vpn.core import storage as _storage

_bad_tmpdir = _Path(_tempfile.mkdtemp(prefix="kapro-smoke-corrupt-"))
_bad_settings = _bad_tmpdir / "settings.json"
_bad_sites = _bad_tmpdir / "sites.json"
# 0x9d is an invalid utf-8 start byte — exactly the failure users reported.
_bad_settings.write_bytes(b'{"language":\x9d "ru"}')
_bad_sites.write_bytes(b'{"sites":\x9d ["x"]}')

_orig_settings_file = _paths.settings_file
_orig_sites_file = _paths.sites_file
_paths.settings_file = lambda: _bad_settings
_paths.sites_file = lambda: _bad_sites


def _load_settings_no_crash() -> None:
    s = _storage.load_settings()
    if not isinstance(s, dict) or s.get("listen_port") != 2080:
        raise AssertionError(f"expected DEFAULT_SETTINGS fallback, got {s!r}")


def _load_sites_no_crash() -> None:
    out = _storage.load_sites()
    if out != []:
        raise AssertionError(f"expected [] fallback, got {out!r}")


check("load_settings: corrupt utf-8 -> defaults", _load_settings_no_crash)
check("load_sites: corrupt utf-8 -> []",          _load_sites_no_crash)

_paths.settings_file = _orig_settings_file
_paths.sites_file = _orig_sites_file


# ---------------------------------------------------------------------------
# Test 12 — config encryption: AES-GCM crypto layer (v1.16.12)
# ---------------------------------------------------------------------------
# macOS/Linux at-rest encryption uses AES-256-GCM with a key from the OS
# keystore. The keystore can't be exercised on the headless CI runner, but
# the *crypto* layer is keystore-free and must be correct everywhere:
# round-trip, random nonce, tamper detection, and the magic-prefix dispatch
# in the public encrypt/decrypt/looks_encrypted API.

section("Config encryption — AES-GCM crypto layer")

from kapro_vpn.core import secrets_store as _ss

_KEY = b"\x11" * 32          # fixed 32-byte AES-256 test key
_PLAIN = b'[{"name":"\xd1\x82\xd0\xb5\xd1\x81\xd1\x82","raw_url":"vless://x"}]'


def _aesgcm_round_trip() -> None:
    blob = _ss._encrypt_with_key(_KEY, _PLAIN)
    if _ss._decrypt_with_key(_KEY, blob) != _PLAIN:
        raise AssertionError("AES-GCM round-trip mismatch")


def _aesgcm_random_nonce() -> None:
    # Two encryptions of the same plaintext must differ (random nonce) yet
    # both decrypt back to the original.
    a = _ss._encrypt_with_key(_KEY, _PLAIN)
    b = _ss._encrypt_with_key(_KEY, _PLAIN)
    if a == b:
        raise AssertionError("nonce not random — identical ciphertexts")
    if _ss._decrypt_with_key(_KEY, a) != _PLAIN or _ss._decrypt_with_key(_KEY, b) != _PLAIN:
        raise AssertionError("decrypt failed for one of the variants")


def _aesgcm_tamper_detected() -> None:
    blob = bytearray(_ss._encrypt_with_key(_KEY, _PLAIN))
    blob[-1] ^= 0xFF            # flip a tag byte
    try:
        _ss._decrypt_with_key(_KEY, bytes(blob))
    except Exception:
        return                  # good — GCM caught the tamper
    raise AssertionError("tampered ciphertext decrypted without error")


def _looks_encrypted_dispatch() -> None:
    if not _ss.looks_encrypted(_ss.AESGCM_MAGIC + b"x"):
        raise AssertionError("AESGCM magic not recognised")
    if not _ss.looks_encrypted(_ss.DPAPI_MAGIC + b"x"):
        raise AssertionError("DPAPI magic not recognised")
    if _ss.looks_encrypted(b'[{"name":"plain"}]'):
        raise AssertionError("plaintext misclassified as encrypted")


def _public_decrypt_dispatch() -> None:
    # decrypt() pulls the DEK from the keystore; inject a fixed key so the
    # dispatch path is exercised without a real keystore.
    orig = _ss._get_dek
    _ss._get_dek = lambda: _KEY
    try:
        full = _ss.AESGCM_MAGIC + _ss._encrypt_with_key(_KEY, _PLAIN)
        if _ss.decrypt(full) != _PLAIN:
            raise AssertionError("public decrypt() of AESGCM blob failed")
    finally:
        _ss._get_dek = orig


check("AES-GCM round-trip",            _aesgcm_round_trip)
check("AES-GCM random nonce",          _aesgcm_random_nonce)
check("AES-GCM tamper detected",       _aesgcm_tamper_detected)
check("looks_encrypted dispatch",      _looks_encrypted_dispatch)
check("public decrypt() dispatch",     _public_decrypt_dispatch)


def _dpapi_round_trip() -> None:
    # Windows-only: the multi-backend refactor must not break DPAPI.
    # Skipped on the Linux CI runner.
    if sys.platform != "win32":
        return
    blob = _ss.encrypt(_PLAIN)
    if not blob.startswith(_ss.DPAPI_MAGIC):
        raise AssertionError("Windows encrypt() didn't use DPAPI magic")
    if _ss.decrypt(blob) != _PLAIN:
        raise AssertionError("DPAPI round-trip mismatch")


check("DPAPI round-trip (win32 only)", _dpapi_round_trip)


# ---------------------------------------------------------------------------
# Test 13 — startup reliability: atomic writes + crash handler (v1.16.13)
# ---------------------------------------------------------------------------
# Atomic writes kill the partial-write corruption that caused v1.16.11.
# The crash handler must log + recover without ever raising itself.

section("Startup reliability — atomic write + crash handler")

from kapro_vpn.core import crash_handler as _ch

_rel_tmp = _Path(_tempfile.mkdtemp(prefix="kapro-smoke-rel-"))


def _atomic_round_trip() -> None:
    target = _rel_tmp / "configs.json"
    _storage._atomic_write_bytes(target, b'[{"name":"x"}]')
    if target.read_bytes() != b'[{"name":"x"}]':
        raise AssertionError("atomic write content mismatch")
    # overwrite must also be atomic and leave no .tmp behind
    _storage._atomic_write_bytes(target, b'[]')
    if target.read_bytes() != b'[]':
        raise AssertionError("atomic overwrite mismatch")
    if (_rel_tmp / "configs.json.tmp").exists():
        raise AssertionError("temp file not cleaned up after write")


def _crash_log_written() -> None:
    orig = _paths.logs_dir
    _paths.logs_dir = lambda: _rel_tmp
    try:
        try:
            raise ValueError("smoke-boom")
        except ValueError as e:
            p = _ch.write_crash_log(e)
        if p is None or not p.is_file():
            raise AssertionError("crash log not written")
        text = p.read_text(encoding="utf-8")
        if "ValueError" not in text or "smoke-boom" not in text:
            raise AssertionError("crash log missing traceback content")
    finally:
        _paths.logs_dir = orig


def _quarantine_moves_settings() -> None:
    orig = _paths.settings_file
    sett = _rel_tmp / "settings.json"
    sett.write_bytes(b'{"x":1}')
    _paths.settings_file = lambda: sett
    try:
        if _ch._quarantine_settings() is not True:
            raise AssertionError("quarantine should return True when file exists")
        if sett.exists():
            raise AssertionError("settings.json should have been moved")
        if not list(_rel_tmp.glob("settings.bad-*.json")):
            raise AssertionError("no quarantined settings.bad-* file found")
        if _ch._quarantine_settings() is not False:
            raise AssertionError("quarantine should return False when nothing to move")
    finally:
        _paths.settings_file = orig


def _crash_dialog_builds() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    box, buttons = _ch._build_message_box("Err: x", "traceback…", _rel_tmp / "crash-x.log")
    if set(buttons) != {"reset", "logs", "close"}:
        raise AssertionError(f"unexpected dialog buttons: {set(buttons)}")
    box.deleteLater()


def _main_safe_mode_wiring() -> None:
    # An unhandled startup exception must be caught, logged, and turned
    # into exit code 1 — never propagated as a raw traceback.
    from kapro_vpn import main as _main
    orig_run, orig_dialog, orig_logs = _main._run_app, _ch._show_dialog, _paths.logs_dir
    _main._run_app = lambda: (_ for _ in ()).throw(RuntimeError("smoke-startup-boom"))
    _ch._show_dialog = lambda exc, log: "close"   # don't pop a real dialog
    _paths.logs_dir = lambda: _rel_tmp
    try:
        rc = _main.main()
        if rc != 1:
            raise AssertionError(f"expected exit code 1 from crashed startup, got {rc}")
        if not list(_rel_tmp.glob("crash-*.log")):
            raise AssertionError("startup crash was not logged")
    finally:
        _main._run_app, _ch._show_dialog, _paths.logs_dir = orig_run, orig_dialog, orig_logs


check("atomic write: round-trip + no .tmp leftover", _atomic_round_trip)
check("crash_handler: writes crash log",             _crash_log_written)
check("crash_handler: quarantine settings",          _quarantine_moves_settings)
check("crash_handler: dialog builds (3 buttons)",    _crash_dialog_builds)
check("main(): startup crash -> logged + exit 1",    _main_safe_mode_wiring)


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
