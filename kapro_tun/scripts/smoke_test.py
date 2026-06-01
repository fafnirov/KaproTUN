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
    from kapro_tun import main as _main  # noqa: F401


def _import_core() -> None:
    from kapro_tun.core import (  # noqa: F401
        controller, parser, xray_config, storage, paths,
        subscription, geoip_ru, killswitch, i18n, system_proxy,
        ip_probe, dns_options, secrets_store, ipv6_block,
        bandwidth_history, webrtc_block, leak_test, crash_handler,
        hysteria_installer, hysteria_process,
    )


def _import_gui() -> None:
    # GUI modules touch PySide6 at import time — runs under
    # xvfb-style headless mode on the smoke runner.
    from kapro_tun.gui import (  # noqa: F401
        main_window, tray, widgets, onboarding,
        configs_picker, subscription_dialog, sites_dialog,
        world_map, bandwidth_chart, stats_page,
    )


check("kapro_tun.main", _import_main)
check("kapro_tun.core.*", _import_core)
check("kapro_tun.gui.*", _import_gui)


# ---------------------------------------------------------------------------
# Test 2 — parser eats each scheme
# ---------------------------------------------------------------------------

section("Parser — synthetic share URLs")

from kapro_tun.core.parser import parse, ParseError, ProxyConfig

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

from kapro_tun.core.xray_config import build_config


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

from kapro_tun.core import dns_options

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

        # DNS block in xray config: present only when the option has its
        # own servers (system has none). v1.19.1: resolve via the option's
        # PLAIN IPs, NOT its DoH endpoint — DoH-over-tunnel stalls ~11s on
        # some networks; plain UDP/53 over the tunnel returns in ~0.4s.
        if opt.plain_servers:
            if "dns" not in full:
                raise AssertionError(f"{opt_key}: dns block missing")
            if full["dns"].get("queryStrategy") != "UseIPv4":
                raise AssertionError(
                    f"{opt_key}: queryStrategy must be UseIPv4"
                )
            servers = full["dns"].get("servers", [])
            if servers != opt.plain_servers:
                raise AssertionError(
                    f"{opt_key}: dns servers should be PLAIN IPs "
                    f"{opt.plain_servers}, got {servers}"
                )
            if any(str(s).startswith("https://") for s in servers):
                raise AssertionError(
                    f"{opt_key}: dns block must NOT use DoH URLs — they "
                    f"stall over the tunnel (v1.19.1 regression guard)"
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

        # v1.19.1: the upstream-resolver carve-out (resolver IPs :53 →
        # proxy) MUST precede the generic :53 → dns-out hijack. Without it
        # both the resolver's own query and dns-out's forwarded query loop
        # back through dns-out and stall ~11s on every new domain.
        rules = full["routing"]["rules"]
        hijack_idx = next(
            i for i, r in enumerate(rules)
            if r.get("outboundTag") == "dns-out" and r.get("port") == "53"
        )
        carve_idx = next(
            (i for i, r in enumerate(rules)
             if r.get("outboundTag") == "proxy" and r.get("port") == "53"),
            None,
        )
        if carve_idx is None or carve_idx >= hijack_idx:
            raise AssertionError(
                f"{opt_key}: resolver :53 → proxy carve-out must come BEFORE "
                f"the dns-out hijack (breaks the loop that stalled DNS)"
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


# v1.19.0: ad-block decoupled from the AdGuard DNS option (block_ads works
# on any DNS), plus geoip:ru direct routing. The geo TAGS themselves are
# validated against a real xray -test at release time; these guard the
# config-shape (rule present/absent + ordering) against regressions.
def _block_ads_independent_of_dns() -> None:
    def _has_ad_block(full):
        return any(
            r.get("outboundTag") == "block"
            and "geosite:category-ads-all" in r.get("domain", [])
            for r in full["routing"]["rules"]
        )
    on = build_config(_vless_cfg, direct_domains=["example.com"],
                      dns_option="system", block_ads=True)
    json.dumps(on, ensure_ascii=False)
    if not _has_ad_block(on):
        raise AssertionError(
            "block_ads=True must add the geosite:category-ads-all block on "
            "ANY DNS, not just AdGuard"
        )
    # The IP-probe allow-list must precede the block so our own probe to
    # ipinfo.io isn't dropped as a 'tracker'.
    rules = on["routing"]["rules"]
    block_idx = next(i for i, r in enumerate(rules)
                     if "geosite:category-ads-all" in r.get("domain", []))
    allow_idx = next((i for i, r in enumerate(rules)
                      if r.get("outboundTag") == "proxy"
                      and any("ipinfo.io" in d for d in r.get("domain", []))), None)
    if allow_idx is None or allow_idx >= block_idx:
        raise AssertionError("IP-probe allow-list must come before the ad-block rule")
    off = build_config(_vless_cfg, direct_domains=["example.com"],
                       dns_option="system", block_ads=False)
    if _has_ad_block(off):
        raise AssertionError("block_ads=False on System must NOT add an ad-block rule")


def _route_ru_direct_adds_geoip_rule() -> None:
    def _has_ru(full):
        return any(
            r.get("outboundTag") == "direct" and "geoip:ru" in r.get("ip", [])
            for r in full["routing"]["rules"]
        )
    on = build_config(_vless_cfg, direct_domains=["example.com"], route_ru_direct=True)
    json.dumps(on, ensure_ascii=False)
    if not _has_ru(on):
        raise AssertionError("route_ru_direct=True must add geoip:ru -> direct")
    off = build_config(_vless_cfg, direct_domains=["example.com"], route_ru_direct=False)
    if _has_ru(off):
        raise AssertionError("route_ru_direct=False must NOT add a geoip:ru rule")


check("block_ads: geosite ad-block on any DNS + probe allow-list order",
      _block_ads_independent_of_dns)
check("route_ru_direct: geoip:ru -> direct rule toggles",
      _route_ru_direct_adds_geoip_rule)


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

from kapro_tun.core import ip_probe as _ip_probe


def _probe_returns_none_on_dead_socks() -> None:
    # 127.0.0.1:1 — well-known "nothing listens here" port. Probe must
    # not raise; must return None within timeout.
    result = _ip_probe.fetch_public_ip(socks_proxy="127.0.0.1:1", timeout=2.0, retries=0)
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


# v1.19.5: the probe must never surface an IPv6 as "your IP" (it would be
# the user's leaked real address). _looks_ipv4 gates that.
def _probe_rejects_ipv6_results() -> None:
    from kapro_tun.core.ip_probe import _looks_ipv4
    for good in ("77.239.122.15", "1.2.3.4", "255.255.255.255"):
        if not _looks_ipv4(good):
            raise AssertionError(f"_looks_ipv4 rejected a valid IPv4: {good}")
    for bad in ("2a01:ecc0:200:1b63::2", "::1", "fe80::1", "", "garbage",
                "1.2.3", "1.2.3.4.5"):
        if _looks_ipv4(bad):
            raise AssertionError(f"_looks_ipv4 accepted a non-IPv4: {bad!r}")


check("ip-probe rejects IPv6 results (never shows leaked v6 as 'your IP')",
      _probe_rejects_ipv6_results)


def _probe_restores_getaddrinfo() -> None:
    # v1.10.3: probe monkey-patches socket.getaddrinfo for IPv4-only
    # resolution during the call. If the `finally` doesn't restore the
    # original, every subsequent socket.getaddrinfo in the whole app
    # becomes IPv4-only forever — silent breakage of v6-needing code
    # paths. Regression guard.
    import socket as _socket
    original = _socket.getaddrinfo
    _ip_probe.fetch_public_ip(socks_proxy="127.0.0.1:1", timeout=1.0, retries=0)
    if _socket.getaddrinfo is not original:
        raise AssertionError(
            "socket.getaddrinfo was not restored after fetch_public_ip"
        )


check("probe restores socket.getaddrinfo after running",
      _probe_restores_getaddrinfo)


# v1.21.1: in TUN mode the probe can fire before the tunnel's DNS path is
# answering — every endpoint fails on that first pass. It must retry the
# whole pass (not give up), and return as soon as one pass succeeds.
def _ip_probe_retries_then_succeeds() -> None:
    orig_probe = _ip_probe._probe_with_fallback
    state = {"n": 0}
    fake_ip = _ip_probe.PublicIp(ip="1.2.3.4", country_code="NL",
                                 country_name="Нидерланды", city=None)

    def _fail_twice_then_ok(proxies, t, locale, say):
        state["n"] += 1
        return fake_ip if state["n"] >= 3 else None

    _ip_probe._probe_with_fallback = _fail_twice_then_ok
    try:
        # retry_delay=0 keeps the test instant (no real sleep).
        result = _ip_probe.fetch_public_ip(timeout=1.0, retries=2, retry_delay=0)
        if result is None or result.ip != "1.2.3.4":
            raise AssertionError(f"retry should have produced the success, got {result!r}")
        if state["n"] != 3:
            raise AssertionError(f"expected 3 passes (2 fail + 1 ok), got {state['n']}")
        # retries=0 → exactly one pass, no retry loop
        state["n"] = 0
        _ip_probe._probe_with_fallback = lambda *a, **k: (state.update(n=state["n"] + 1) or None)
        r0 = _ip_probe.fetch_public_ip(timeout=1.0, retries=0, retry_delay=0)
        if r0 is not None or state["n"] != 1:
            raise AssertionError(f"retries=0 must do exactly one pass, got n={state['n']} r={r0!r}")
    finally:
        _ip_probe._probe_with_fallback = orig_probe


check("ip-probe retries the pass while tunnel DNS warms up",
      _ip_probe_retries_then_succeeds)


# v1.21.1: leftover bypass routes from a prior session (app killed/crashed
# before restore() ran) must be ADOPTED into the current session so
# disconnect cleans them — otherwise they leak into the routing table and
# can blackhole on a network change. Windows native-API path only.
def _bypass_routes_adopt_leftovers() -> None:
    import sys as _sys
    if _sys.platform != "win32":
        return  # CreateIpForwardEntry path is Windows-only
    from kapro_tun.core import network_routes as _nr
    orig_create = _nr._create_route_native
    orig_delete = _nr.delete_route
    try:
        # BOTH already-exists codes must adopt (track for cleanup) WITHOUT
        # shelling out. Windows returns 183 on some boxes, 5010 on others
        # (the field captures that drove this returned 5010 even for exact
        # dups) — v1.21.1 wrongly delete+recreated on 5010, which is tens of
        # seconds of shell-outs for thousands of geoip CIDRs and flaps a live
        # connection. v1.21.2 adopts both, fast and non-disruptive.
        for label, code in (("ALREADY_EXISTS_183", _nr._ERROR_ALREADY_EXISTS),
                            ("OBJECT_ALREADY_EXISTS_5010", _nr._ERROR_OBJECT_ALREADY_EXISTS)):
            shell = {"n": 0}
            _nr._create_route_native = (lambda c: (lambda *a, **k: c))(code)
            _nr.delete_route = lambda *a, **k: (shell.update(n=shell["n"] + 1) or True)
            sess = _nr.RouteSession()
            added, adopted = sess.add_bypass_cidrs(
                [("8.8.8.8", "255.255.255.255"), ("1.1.1.1", "255.255.255.255")],
                "192.168.1.1", 17, metric=36,
            )
            if (added, adopted) != (0, 2):
                raise AssertionError(f"{label}: expected (0,2), got ({added},{adopted})")
            if len(sess.routes) != 2:
                raise AssertionError(f"{label}: adopted routes must be tracked, got {len(sess.routes)}")
            if shell["n"] != 0:
                raise AssertionError(f"{label}: adopt must NOT shell-delete (got {shell['n']} calls)")
        # Fresh adds report as added, not adopted.
        _nr._create_route_native = lambda *a, **k: _nr._NO_ERROR
        sess2 = _nr.RouteSession()
        a2, ad2 = sess2.add_bypass_cidrs([("9.9.9.9", "255.255.255.255")], "192.168.1.1", 17)
        if (a2, ad2) != (1, 0):
            raise AssertionError(f"fresh add should give (1,0), got ({a2},{ad2})")
    finally:
        _nr._create_route_native = orig_create
        _nr.delete_route = orig_delete


check("bypass routes: adopt leftovers so disconnect cleans them",
      _bypass_routes_adopt_leftovers)


# v2.1.1: TUN egress must bind to the route to the SERVER (Find-NetRoute), not
# "the first 0.0.0.0/0" — fixes multi-NIC (Ethernet + Wi-Fi / virtual adapter)
# "подключено, но трафика нет". The PS shell-out is win32-only; we mock _ps to
# test the parse + validation contract.
def _egress_selection_route_binding() -> None:
    import sys as _sys
    if _sys.platform != "win32":
        return
    from kapro_tun.core import network_routes as _nr
    orig = _nr._ps

    def mock(route_json):
        def _ps(cmd, timeout=10.0):
            # the iface-metric sub-query is a bare Get-NetIPInterface (no
            # Find-NetRoute, no Where-Object) -> return a number.
            if ("InterfaceMetric" in cmd and "Find-NetRoute" not in cmd
                    and "Where-Object" not in cmd):
                return (0, "25\n", "")
            return (0, route_json, "")
        return _ps
    try:
        # a valid server route -> select that interface + gateway
        _nr._ps = mock('{"InterfaceAlias":"Ethernet","InterfaceIndex":21,"NextHop":"192.168.1.1"}')
        e = _nr.find_egress_to("77.239.122.15")
        if e is None or e.index != 21 or e.gateway != "192.168.1.1":
            raise AssertionError(f"valid server route must be selected, got {e}")
        # empty next-hop -> None so the caller falls back
        _nr._ps = mock('{"InterfaceAlias":"X","InterfaceIndex":9,"NextHop":""}')
        if _nr.find_egress_to("1.2.3.4") is not None:
            raise AssertionError("empty next-hop must yield None (fallback)")
        # on-link 0.0.0.0 -> None (never a real public VPN server)
        _nr._ps = mock('{"InterfaceAlias":"X","InterfaceIndex":9,"NextHop":"0.0.0.0"}')
        if _nr.find_egress_to("1.2.3.4") is not None:
            raise AssertionError("on-link 0.0.0.0 next-hop must yield None")
        # multi-NIC fallback: get_default_route_v4 parses the chosen row
        _nr._ps = mock('{"InterfaceAlias":"Ethernet","InterfaceIndex":21,"NextHop":"10.0.0.1"}')
        d = _nr.get_default_route_v4()
        if d is None or d.index != 21 or d.gateway != "10.0.0.1":
            raise AssertionError(f"fallback must parse the default route, got {d}")
    finally:
        _nr._ps = orig


def _egress_injection_and_garbage_guard() -> None:
    """A non-IPv4 remote (injection attempt / hostname / garbage) must be
    rejected BEFORE any shell-out."""
    import sys as _sys
    if _sys.platform != "win32":
        return
    from kapro_tun.core import network_routes as _nr
    orig = _nr._ps
    calls = {"n": 0}

    def _spy(cmd, timeout=10.0):
        calls["n"] += 1
        return (0, "", "")
    try:
        _nr._ps = _spy
        for bad in ("", "1.2.3.4; calc", "evil.example.com", "1.2.3",
                    "999.1.1.1", "::1", "1.2.3.4 || rm"):
            if _nr.find_egress_to(bad) is not None:
                raise AssertionError(f"non-IPv4 {bad!r} must be rejected")
        if calls["n"] != 0:
            raise AssertionError("must not shell out for a non-IPv4 remote")
    finally:
        _nr._ps = orig


check("egress: bind to server route + reject empty/on-link gateway",
      _egress_selection_route_binding)
check("egress: reject non-IPv4 remote before shelling out",
      _egress_injection_and_garbage_guard)


# v2.1.2 — three robustness/cross-platform fixes.
def _unix_find_egress_api_compat() -> None:
    """P1: controller calls network_routes.find_egress_to() cross-platform —
    the Unix module must expose it (it didn't → AttributeError on Linux/macOS).
    Importable + callable + never raises on every OS."""
    from kapro_tun.core import network_routes_unix as nru
    if not hasattr(nru, "find_egress_to"):
        raise AssertionError("network_routes_unix.find_egress_to missing (Unix TUN AttributeError)")
    res = nru.find_egress_to("77.239.122.15")  # must not raise
    if res is not None and not hasattr(res, "gateway"):
        raise AssertionError("find_egress_to must return None or an InterfaceInfo")


def _net_download_bad_content_length() -> None:
    """P2: a non-numeric Content-Length ('abc') must NOT crash the download —
    treated as unknown total (0); streaming + hard cap still work."""
    import tempfile, shutil
    from pathlib import Path
    from kapro_tun.core import net_download as nd
    import requests as _rq

    class _FakeResp:
        def __init__(self, headers, chunks):
            self.headers = headers
            self._chunks = chunks
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=0):
            for c in self._chunks:
                yield c

    orig = _rq.get
    _rq.get = lambda *a, **k: _FakeResp({"Content-Length": "abc"}, [b"hello", b"world"])
    try:
        data = nd.download_to_memory("http://x/f.bin", max_bytes=10_000)
        if data != b"helloworld":
            raise AssertionError(f"download_to_memory body wrong: {data!r}")
        seen = []
        nd.download_to_memory("http://x/f.bin", max_bytes=10_000,
                              progress=lambda d, t: seen.append(t))
        if seen and seen[-1] != 0:
            raise AssertionError(f"non-numeric Content-Length must map to total=0, got {seen}")
        tmp = Path(tempfile.mkdtemp(prefix="kt-nd-"))
        try:
            p = nd.download_to_file("http://x/f.bin", tmp / "out.bin", max_bytes=10_000)
            if p.read_bytes() != b"helloworld":
                raise AssertionError("download_to_file body wrong on non-numeric Content-Length")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        _rq.get = orig


def _tun2socks_mirror_url_host() -> None:
    """P2: tun2socks mirror must use the same working host as the rest of the
    project (kaprovpn.pro/files), not the dead files.kaprovpn.pro subdomain."""
    from kapro_tun.core import tun2socks_installer as ti
    if ti.KAPROTUN_MIRROR_BASE != "https://kaprovpn.pro/files":
        raise AssertionError(f"tun2socks mirror base wrong: {ti.KAPROTUN_MIRROR_BASE!r}")
    url = ti._mirror_url("tun2socks-windows-amd64.zip")
    if not url.startswith("https://kaprovpn.pro/files/") or "files.kaprovpn.pro" in url:
        raise AssertionError(f"tun2socks mirror URL wrong: {url!r}")


check("unix: network_routes_unix.find_egress_to exists + safe", _unix_find_egress_api_compat)
check("net_download: non-numeric Content-Length doesn't crash", _net_download_bad_content_length)
check("tun2socks: mirror URL uses kaprovpn.pro/files", _tun2socks_mirror_url_host)


# v1.21.1: benign broadcast/multicast UDP relay failures (Steam :27036,
# SSDP, mDNS → WSAENOBUFS) are filtered from the user's live Logs page;
# real lines pass through untouched.
def _tun2socks_log_noise_filter() -> None:
    from kapro_tun.core.tun2socks_process import _is_noise_line
    noise = ('{"level":"warn","caller":"tunnel/udp.go:31","msg":"[UDP] dial '
             '10.255.0.255:27036: listen packet: listen udp :0: bind: An '
             'operation on a socket could not be performed because the system '
             'lacked sufficient buffer space or because a queue was full."}')
    if not _is_noise_line(noise):
        raise AssertionError("Steam-broadcast UDP buffer warning should be filtered")
    for keep in (
        '{"level":"info","msg":"tun2socks 2.6.0 started"}',
        '{"level":"error","msg":"[TCP] connection reset by peer"}',
        'INFO[0000] [STACK] tun://KaproTun <-> socks5://127.0.0.1:2081',
    ):
        if _is_noise_line(keep):
            raise AssertionError(f"non-noise line wrongly filtered: {keep!r}")


check("tun2socks log: benign broadcast-UDP noise is filtered, real lines kept",
      _tun2socks_log_noise_filter)


# ---------------------------------------------------------------------------
# Test 5.5 — IPv6 leak block (v1.11.0) — basic invariants
# ---------------------------------------------------------------------------
# Doesn't actually shell out to netsh — that needs admin and Windows.
# Just checks the module's public surface is sane and the no-op paths
# don't raise on Linux/macOS where the feature isn't supported yet.

section("IPv6 leak block — module sanity")

from kapro_tun.core import ipv6_block as _ipv6_block


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


# v1.19.4: diagnosability for "protection ON but IPv6 still leaks" reports.
def _ipv6_block_diagnostics_surface() -> None:
    from kapro_tun.core import ipv6_block
    out = ipv6_block.diagnostics()
    if not isinstance(out, str) or not out.strip():
        raise AssertionError("diagnostics() must return a non-empty string")
    if not isinstance(ipv6_block.probe_ipv6_reachable(timeout=0.5), bool):
        raise AssertionError("probe_ipv6_reachable() must return a bool")
    if not isinstance(ipv6_block.last_install_output(), str):
        raise AssertionError("last_install_output() must return a str")


check("ipv6_block diagnostics/probe surface cleanly",
      _ipv6_block_diagnostics_surface)


# v1.18.1: IPv6-leak protection must be armed in HTTP-proxy mode too, not
# just TUN. Earlier builds only armed it in TUN, so the default HTTP mode
# leaked the real IPv6 on a leak test. These guard against silent reverts.

def _ipv6_arm_gating() -> None:
    from kapro_tun.core import controller as ctrl
    mgr = ctrl.ConnectionManager(on_log=lambda _l: None)
    orig = (ctrl.admin.is_admin, ctrl.ipv6_block.is_supported, ctrl.ipv6_block.install)
    installs = {"n": 0}
    ctrl.ipv6_block.is_supported = lambda: True
    ctrl.ipv6_block.install = lambda: (installs.__setitem__("n", installs["n"] + 1) or True)
    try:
        ctrl.admin.is_admin = lambda: True
        mgr.settings = {"ipv6_leak_protection": True}
        mgr._maybe_arm_ipv6_block()
        if installs["n"] != 1:
            raise AssertionError("ipv6 block not armed when setting on + admin")
        mgr.settings = {"ipv6_leak_protection": False}
        mgr._maybe_arm_ipv6_block()
        if installs["n"] != 1:
            raise AssertionError("ipv6 block armed despite setting off")
        ctrl.admin.is_admin = lambda: False  # can't netsh without admin
        mgr.settings = {"ipv6_leak_protection": True}
        mgr._maybe_arm_ipv6_block()
        if installs["n"] != 1:
            raise AssertionError("ipv6 block 'armed' without admin — impossible")
    finally:
        (ctrl.admin.is_admin, ctrl.ipv6_block.is_supported, ctrl.ipv6_block.install) = orig


def _http_connect_arms_ipv6_block() -> None:
    # The actual leak fix: HTTP-mode connect must call _maybe_arm_ipv6_block.
    # Stub the heavy steps so no real xray/proxy/firewall work happens.
    from kapro_tun.core import controller as ctrl
    from kapro_tun.core.parser import ProxyConfig as PC
    mgr = ctrl.ConnectionManager(on_log=lambda _l: None)
    calls = {"ipv6": 0}
    mgr._maybe_start_hysteria = lambda cfg: None
    mgr._write_and_check = lambda *a, **k: None
    mgr._start_xray = lambda: None
    mgr._maybe_arm_killswitch = lambda: None
    mgr._maybe_arm_webrtc_block = lambda: None
    mgr._maybe_arm_ipv6_block = lambda: calls.__setitem__("ipv6", calls["ipv6"] + 1)
    mgr.settings = dict(mgr.settings)
    mgr.settings["auto_set_system_proxy"] = False  # skip the real registry write
    cfg = PC(name="t", protocol="vless", raw_url="vless://x@127.0.0.1:1",
             outbound={"server": "127.0.0.1", "server_port": 1})
    mgr._connect_http(cfg, [])
    if calls["ipv6"] != 1:
        raise AssertionError(
            "HTTP connect didn't arm IPv6-leak protection — the v6 leak "
            "in the default mode is back"
        )


check("ipv6 arming honors setting + admin gate", _ipv6_arm_gating)
check("HTTP-mode connect arms IPv6-leak protection (v1.18.1)",
      _http_connect_arms_ipv6_block)


# v1.19.2: tun2socks throughput tuning. gVisor's netstack caps the TCP
# receive window below the link's BDP without auto-tuning, so TUN-mode
# throughput sat under the line rate. Guard the flags against silent revert.
def _tun2socks_args_have_throughput_tuning() -> None:
    from kapro_tun.core.tun2socks_process import Tun2socksProcess
    args = Tun2socksProcess()._build_args("tun2socks.exe", "127.0.0.1:2081", 1500, "warn")
    for flag in ("-tcp-auto-tuning", "-tcp-sndbuf", "-tcp-rcvbuf"):
        if flag not in args:
            raise AssertionError(
                f"tun2socks args missing throughput flag {flag} "
                f"(TUN throughput regression): {args}"
            )
    snd = args[args.index("-tcp-sndbuf") + 1]
    rcv = args[args.index("-tcp-rcvbuf") + 1]
    if not snd or not rcv:
        raise AssertionError("tun2socks buffer sizes must be non-empty")
    # base command must still be intact
    for flag in ("-device", "-proxy", "-mtu", "-loglevel"):
        if flag not in args:
            raise AssertionError(f"tun2socks base arg {flag} missing: {args}")


check("tun2socks args carry throughput tuning (auto-tuning + buffers)",
      _tun2socks_args_have_throughput_tuning)


# ---------------------------------------------------------------------------
# Test 5.7 — WebRTC leak block (v1.16.0)
# ---------------------------------------------------------------------------
# Same surface-shape contract as ipv6_block: every public function must
# return cleanly on every platform (no raises). Plus a port-list sanity:
# we want STUN ports only — NOT random UDP ports that would break DNS
# (53), QUIC (443), VoIP, or anything else.

section("WebRTC leak block — module sanity")

from kapro_tun.core import webrtc_block as _webrtc_block


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
    from kapro_tun.gui import window_resize as _wr

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
    from kapro_tun.gui import window_resize as _wr

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


# v2.0.3 — fixed-size window by default (kills the "window resizes/creeps
# erratically" UX bug). Edge handles + size-persistence are gated behind
# allow_window_resize (default OFF); titlebar drag must keep working.
def _window_resize_gate_default_off() -> None:
    from kapro_tun.gui.main_window import MainWindow
    from kapro_tun.core import storage as _st
    if MainWindow._window_resize_allowed({}) is not False:
        raise AssertionError("empty settings must default to non-resizable")
    if MainWindow._window_resize_allowed({"allow_window_resize": False}) is not False:
        raise AssertionError("explicit False must stay non-resizable")
    if MainWindow._window_resize_allowed({"allow_window_resize": True}) is not True:
        raise AssertionError("allow_window_resize=True must enable resize")
    if _st.DEFAULT_SETTINGS.get("allow_window_resize") is not False:
        raise AssertionError("DEFAULT_SETTINGS.allow_window_resize must ship False")


def _window_fixed_and_handleless_by_default() -> None:
    """Build the REAL MainWindow in the default (off) mode and assert it is
    fixed-size with NO resize handles created. Non-fragile: checks geometry
    policy + the handles attribute, not pixels."""
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui import main_window as _mw
    from kapro_tun.core import storage as _st
    orig_load = _st.load_settings
    # Pin the standard preset so the assertion is deterministic regardless of
    # the test machine's screen height (auto would pick compact on short ones).
    _st.load_settings = lambda: {**_st.DEFAULT_SETTINGS,
                                 "allow_window_resize": False,
                                 "window_size_preset": "standard"}
    try:
        w = _mw.MainWindow()
    finally:
        _st.load_settings = orig_load
    try:
        if getattr(w, "_resize_handles", "missing") is not None:
            raise AssertionError("default (fixed) mode must NOT create resize handles")
        if w.minimumSize() != w.maximumSize():
            raise AssertionError("fixed mode must lock min==max (no mouse resize)")
        if (w.width(), w.height()) != (480, 870):
            raise AssertionError(f"fixed mode must open at 480x870, got {w.width()}x{w.height()}")
    finally:
        for attr in ("_poll", "_sub_autorefresh", "_tray_pinger"):
            obj = getattr(w, attr, None)
            if obj is not None and hasattr(obj, "stop"):
                try: obj.stop()
                except Exception: pass
        tray = getattr(w, "tray", None)
        if tray is not None and hasattr(tray, "hide"):
            try: tray.hide()
            except Exception: pass
        w.close()
        w.deleteLater()


def _titlebar_drag_intact() -> None:
    """The titlebar drag-to-move handlers + window-control signals must still
    be present — this fix must not touch titlebar behaviour. No pixels."""
    from PySide6.QtWidgets import QFrame
    from kapro_tun.gui.titlebar import TitleBar
    for name in ("mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent"):
        if getattr(TitleBar, name) is getattr(QFrame, name):
            raise AssertionError(f"TitleBar.{name} drag handler missing (not overridden)")
    for sig in ("minimize_clicked", "close_clicked"):
        if not hasattr(TitleBar, sig):
            raise AssertionError(f"TitleBar.{sig} window-control signal missing")


check("window: resize gate defaults off (fixed window)", _window_resize_gate_default_off)
check("window: fixed-size + no handles by default", _window_fixed_and_handleless_by_default)
check("window: titlebar drag handlers + controls intact", _titlebar_drag_intact)


# v2.1.0 — UI/UX pack: unified state model, typography tokens, readable graph,
# fixed-width traffic legend, standard/compact window presets.
def _connection_state_model() -> None:
    from kapro_tun.gui import connection_state as cs
    if len(cs.ALL_STATES) != 6:
        raise AssertionError("expected six canonical states")
    for s in cs.ALL_STATES:
        sp = cs.spec(s)
        if sp.state != s:
            raise AssertionError(f"spec({s}).state mismatch")
        if sp.circle_state not in ("idle", "connecting", "connected"):
            raise AssertionError(f"{s}: circle_state must be a button visual state")
        if sp.accent not in ("ACCENT", "TEXT_MUTED", "DANGER", "SUCCESS"):
            raise AssertionError(f"{s}: accent must be a palette field name")
        if not sp.label or not sp.glyph:
            raise AssertionError(f"{s}: needs a label + indicator glyph")
    if cs.normalize("idle") != cs.DISCONNECTED:
        raise AssertionError("legacy 'idle' must map to disconnected")
    if cs.normalize("garbage") != cs.DISCONNECTED:
        raise AssertionError("unknown state must fall back to disconnected")
    if not (cs.spec(cs.ERROR).is_error and cs.spec(cs.KILLSWITCH_ACTIVE).is_error):
        raise AssertionError("error + killswitch must be flagged is_error")
    if cs.spec(cs.CONNECTED).is_error:
        raise AssertionError("connected must not be is_error")
    # Every accent the spec references must resolve on the styles module
    # (StatusLabel does getattr(styles, accent)).
    from kapro_tun.gui import styles as _sty
    for s in cs.ALL_STATES:
        if not hasattr(_sty, cs.spec(s).accent):
            raise AssertionError(f"styles missing accent {cs.spec(s).accent}")


def _typography_tokens_in_qss() -> None:
    from kapro_tun.gui import styles
    for tok in ("#title", "#section", "#body", "#secondary", "#caption",
                "#graphDown", "#graphUp", "#graphValue"):
        if f"QLabel{tok}" not in styles.DARK_QSS or f"QLabel{tok}" not in styles.LIGHT_QSS:
            raise AssertionError(f"typography token {tok} missing from QSS")
    if "letter-spacing: 0" not in styles.DARK_QSS:
        raise AssertionError("typography tokens must set letter-spacing: 0")


def _traffic_legend_fixed_width() -> None:
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.widgets import TrafficLegend
    leg = TrafficLegend()
    w0 = leg.down_value.minimumWidth()
    leg.set_values(9.9 * 1024, 38.1 * 1024, 1, 2)
    a = leg.down_value.minimumWidth()
    leg.set_values(1.2 * 1024 * 1024, 999.9 * 1024, 10 ** 9, 10 ** 9)
    b = leg.down_value.minimumWidth()
    if not (w0 == a == b and w0 >= 80):
        raise AssertionError(f"value labels must keep a fixed min width, got {w0}/{a}/{b}")
    leg.deleteLater()


def _sparkline_scale_hysteresis() -> None:
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.sparkline import TrafficSparkline
    sp = TrafficSparkline()
    base = sp._scale
    sp.add_sample(0, 5_000_000)  # one big burst must NOT snap the scale
    if sp._scale >= 5_000_000 * 0.9:
        raise AssertionError("scale should EASE toward a burst, not snap")
    if sp._scale <= base:
        raise AssertionError("scale should grow toward the burst")
    for _ in range(25):
        sp.add_sample(0, 5_000_000)
    if sp._scale <= 5_000_000 * 0.5:
        raise AssertionError("a sustained burst should raise the scale")
    sp.deleteLater()


def _teardown_window(w) -> None:
    for a in ("_poll", "_sub_autorefresh"):
        o = getattr(w, a, None)
        if o is not None and hasattr(o, "stop"):
            try: o.stop()
            except Exception: pass
    t = getattr(w, "tray", None)
    if t is not None and hasattr(t, "hide"):
        try: t.hide()
        except Exception: pass
    w.close()
    w.deleteLater()


def _window_presets() -> None:
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui import main_window as _mw
    from kapro_tun.core import storage as _st
    orig = _st.load_settings

    def build(preset):
        _st.load_settings = lambda: {**_st.DEFAULT_SETTINGS, "window_size_preset": preset}
        try:
            return _mw.MainWindow()
        finally:
            _st.load_settings = orig

    std = build("standard")
    try:
        if (std.width(), std.height()) != (480, 870):
            raise AssertionError(f"standard must be 480x870, got {std.width()}x{std.height()}")
        if std._compact_preset:
            raise AssertionError("standard must not be compact")
    finally:
        _teardown_window(std)

    comp = build("compact")
    try:
        if (comp.width(), comp.height()) != (460, 720):
            raise AssertionError(f"compact must be 460x720, got {comp.width()}x{comp.height()}")
        if not comp._compact_preset:
            raise AssertionError("compact preset flag must be set")
        if comp.home_page.circle.property("compact") != "true":
            raise AssertionError("compact hero circle must carry compact=true")
        for b in ("btn_home", "btn_stats", "btn_settings", "btn_add"):
            if not hasattr(comp.nav, b):
                raise AssertionError(f"compact nav missing {b} (navigation must not break)")
    finally:
        _teardown_window(comp)


check("ui: connection-state model (6 states, normalize, accents)", _connection_state_model)
check("ui: typography tokens present + letter-spacing 0", _typography_tokens_in_qss)
check("ui: traffic legend keeps fixed-width values (no jitter)", _traffic_legend_fixed_width)
check("ui: sparkline Y-scale eases (hysteresis, no snap)", _sparkline_scale_hysteresis)
check("ui: window presets standard 480x870 / compact 460x720", _window_presets)


def _settings_no_overlong_controls() -> None:
    """v2.1.3 regression guard. Offscreen font metrics are unreliable for
    absolute pixel widths (the same checkbox reads ~338px on the real display
    vs ~626px offscreen), so instead of a fragile pixel test we assert the
    STRUCTURAL fix that stops the SettingsPage clipping descriptions on the
    right:

      * no NON-WRAPPING control (QCheckBox / QRadioButton can't word-wrap) has a
        label long enough to force the content wider than the compact 460-px
        viewport. ~54 chars ≈ the 404-px content area at the app font; the old
        60-char DNS-leak label (which clipped the whole page) is over the limit,
        the kept labels (≤46) are well under.
      * the Hysteria2 speed spinboxes are width-capped so their row can't
        balloon the content width (it's now a grid, not one wide QHBoxLayout).
    """
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QCheckBox, QRadioButton
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.core.controller import ConnectionManager
    from kapro_tun.gui.main_window import SettingsPage
    sp = SettingsPage(ConnectionManager(on_log=lambda _l: None))
    LIMIT = 54
    controls = sp.findChildren(QCheckBox) + sp.findChildren(QRadioButton)
    over = [c.text() for c in controls if len(c.text()) > LIMIT]
    if over:
        raise AssertionError(
            "non-wrapping label(s) too long (>%d chars) — would force settings "
            "h-overflow + clip descriptions: %d found" % (LIMIT, len(over)))
    for spin in (sp.hy_down_spin, sp.hy_up_spin):
        if spin.maximumWidth() <= 0 or spin.maximumWidth() > 160:
            raise AssertionError(
                "hy speed spinbox must be width-capped (got %d)" % spin.maximumWidth())
    sp.deleteLater()


check("ui: settings has no over-long non-wrapping labels (no clip)",
      _settings_no_overlong_controls)


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
    from kapro_tun.core import leak_test as _lt
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
    from kapro_tun.core import leak_test as _lt

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


# v1.19.3: the leak test offers a one-click fix when a leak is leaking only
# because its protection toggle is OFF. fixable_protections() drives that.
def _leak_test_fixable_protections() -> None:
    from kapro_tun.core import leak_test as lt
    rep = lt.LeakTestReport()
    rep.ipv6 = lt.IPv6Result(ip="2a01:ecc0::2", ipv6_blocked=False)   # leaking
    rep.webrtc = lt.WebRtcResult(stun_blocked=False)                   # leaking

    # Both leaking + both toggles OFF -> both offered.
    fx = lt.fixable_protections(rep, {"ipv6_leak_protection": False,
                                       "webrtc_leak_protection": False})
    keys = {k for k, _ in fx}
    if keys != {"ipv6_leak_protection", "webrtc_leak_protection"}:
        raise AssertionError(f"expected both fixable, got {keys}")

    # Leaking but protection already ON -> NOT offered (a real toggle flip
    # wouldn't help; e.g. the rule failed to install — different problem).
    fx = lt.fixable_protections(rep, {"ipv6_leak_protection": True,
                                       "webrtc_leak_protection": True})
    if fx:
        raise AssertionError(f"must not offer a fix when protection is ON: {fx}")

    # No leak (blocked) + toggle off -> nothing to fix.
    rep2 = lt.LeakTestReport()
    rep2.ipv6 = lt.IPv6Result(ipv6_blocked=True)
    rep2.webrtc = lt.WebRtcResult(stun_blocked=True)
    if lt.fixable_protections(rep2, {"ipv6_leak_protection": False,
                                     "webrtc_leak_protection": False}):
        raise AssertionError("must not offer a fix when there's no leak")


check("leak_test: fixable_protections offers off-toggle leaks only",
      _leak_test_fixable_protections)


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

from kapro_tun.gui.configs_picker import ConfigsPickerDialog as _Picker

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

from kapro_tun.gui import styles as _styles


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
    # Amber #f59e0b is the KaproTUN brand color — both themes must
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

from kapro_tun.gui import world_map as _world_map


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


# v1.21.0: animated pin (radar pulse + traffic-reactive). Guards the timer
# lifecycle (animate only when pinned+visible -> 0 CPU idle) and the
# throughput->activity mapping.
def _world_map_animation() -> None:
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.world_map import WorldMapWidget
    w = WorldMapWidget()
    if w._anim.isActive():
        raise AssertionError("animation must be idle with no pin")
    w.show()
    w.set_country("NL")
    if not w._anim.isActive():
        raise AssertionError("animation must run when pin is set + visible")
    # throughput → activity_target in [0,1]
    w.set_traffic(0)
    if w._activity_target != 0.0:
        raise AssertionError("idle traffic must give activity 0")
    w.set_traffic(10_000_000)
    if not (0.9 <= w._activity_target <= 1.0):
        raise AssertionError(f"high traffic must saturate near 1.0, got {w._activity_target}")
    w.set_traffic(-5)
    if w._activity_target != 0.0:
        raise AssertionError("negative traffic must clamp to 0")
    # ticks must advance the phase and never raise
    p0 = w._phase
    for _ in range(5):
        w._tick()
    if w._phase == p0:
        raise AssertionError("phase must advance on tick")
    # clearing the pin stops the animation (0 CPU when disconnected)
    w.set_country(None)
    if w._anim.isActive():
        raise AssertionError("animation must stop when the pin is cleared")
    w.deleteLater()


check("world map: pulse animation lifecycle + traffic map", _world_map_animation)


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
from kapro_tun.core import bandwidth_history as _bw
from kapro_tun.core import paths as _paths

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


# --- v1.17.4: stop running app before reinstall/uninstall ------------------
# Reinstall used to crash with PermissionError [Errno 13] because it
# overwrote KaproTUN.exe while the app was still running and holding the
# Windows file lock. stop_running_app() now clears that first.

def _exe_lock_probe_handles_missing_and_unlocked():
    import os as _os
    import tempfile
    from pathlib import Path as _P

    from installer import operations

    # Missing file → not locked, so stop_running_app() no-ops on a fresh
    # install instead of trying to kill a process that isn't there.
    missing = _P(tempfile.gettempdir()) / "kapro-smoke-does-not-exist.exe"
    if missing.exists():
        missing.unlink()
    if operations._exe_is_locked(missing):
        raise AssertionError("_exe_is_locked must be False for a missing file")

    # Existing but unlocked file → not locked, and the probe must not
    # mangle the file (append-mode open + immediate close writes nothing).
    fd, name = tempfile.mkstemp(suffix=".exe")
    _os.write(fd, b"MZ\x00\x00payload")
    _os.close(fd)
    try:
        before = _P(name).read_bytes()
        if operations._exe_is_locked(_P(name)):
            raise AssertionError("_exe_is_locked must be False for an unlocked file")
        if _P(name).read_bytes() != before:
            raise AssertionError("_exe_is_locked must not modify the probed file")
    finally:
        _os.unlink(name)


def _stop_running_app_noops_when_not_installed():
    from installer import operations, paths

    # If KaproTUN isn't actually installed at the per-user path (the CI
    # case, and most dev machines), stop_running_app must return cleanly
    # without raising or shelling out to taskkill.
    if paths.installed_exe_path().exists():
        return  # real install present — skip rather than touch it
    operations.stop_running_app()


def _uninstall_cleanup_leaves_real_proxy_alone():
    # The safety-critical invariant: the uninstall network-cleanup must
    # NEVER disable a real (non-loopback) system proxy — only our own dead
    # local-port entry. Patch the real module so we don't touch the
    # machine's actual proxy settings during the test.
    from installer import operations
    try:
        from kapro_tun.core import system_proxy
    except Exception:
        return  # module not importable here — nothing to assert
    orig_get, orig_dis = system_proxy.get_state, system_proxy.disable_proxy
    calls = {"n": 0}
    system_proxy.get_state = lambda: {"enable": 1, "server": "proxy.corp.example:8080"}
    system_proxy.disable_proxy = lambda: calls.__setitem__("n", calls["n"] + 1)
    try:
        operations._clear_our_system_proxy()
        if calls["n"] != 0:
            raise AssertionError(
                "uninstall cleanup disabled a non-loopback proxy — it must "
                "only clear our own 127.0.0.1:<port> entry"
            )
    finally:
        system_proxy.get_state, system_proxy.disable_proxy = orig_get, orig_dis


check("installer: exe-lock probe handles missing + unlocked files",
      _exe_lock_probe_handles_missing_and_unlocked)
check("installer: stop_running_app no-ops when app not installed",
      _stop_running_app_noops_when_not_installed)
check("installer: uninstall cleanup never touches a real system proxy",
      _uninstall_cleanup_leaves_real_proxy_alone)


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
    from kapro_tun.gui.stats_page import StatsPage

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
    from kapro_tun.gui.stats_page import StatsPage

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
    from kapro_tun.core.xray_stats import query_tun_iface_stats
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
    from kapro_tun.core.xray_stats import query_tun_iface_stats
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

from kapro_tun.core import storage as _storage

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

from kapro_tun.core import secrets_store as _ss

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

from kapro_tun.core import crash_handler as _ch

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
    from kapro_tun import main as _main
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
# Test 14 — subscription: error classification + stub detection (v1.16.14)
# ---------------------------------------------------------------------------
# A 404 must NOT be reported as a REALITY/DPI block, and provider stub
# configs (host 0.0.0.0 / name "App not supported") must be filtered out
# instead of silently imported as dead servers.

section("Subscription — error classify + stub filter")

from kapro_tun.core import subscription as _sub

_STUB_URL = ("vless://aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@0.0.0.0:1"
             "?encryption=none&type=tcp&security=none#App%20not%20supported")
_REAL_URL = SAMPLE_URLS[0][1]  # synthetic vless sample (host 1.2.3.4:443)


def _subscription_ua_is_kaprovpn_prefix() -> None:
    """v1.22.1 regression. Providers (gmailvpn.site & co.) gate their
    subscription endpoint on a User-Agent allowlist matched as a strict
    `KaproVPN/` PREFIX. The v1.22.0 KaproVPN->KaproTUN rebrand changed this
    UA to `KaproTUN/` and silently turned every such provider into a dead
    "App not supported" stub (measured: KaproVPN/ -> 9 real servers,
    KaproTUN/ -> 1 stub). The subscription UA must stay `KaproVPN/`
    regardless of app brand — guard it so a future rename can't regress it.
    """
    ua = _sub.USER_AGENT
    if not ua.startswith("KaproVPN/"):
        raise AssertionError(
            f"subscription User-Agent must start with 'KaproVPN/' (provider "
            f"allowlist prefix) — got {ua!r}")


check("subscription User-Agent keeps the KaproVPN/ allowlist prefix",
      _subscription_ua_is_kaprovpn_prefix)


# ---------------------------------------------------------------------------
# Test 14.5 — v2.0.0 security hardening
# ---------------------------------------------------------------------------
section("Security hardening (v2.0.0)")

import tempfile as _tf
import shutil as _sh
from pathlib import Path as _SecPath


def _https_only_subscription_url() -> None:
    from kapro_tun.core.subscription import is_https_url
    for good in ("https://prov.example/sub/abc", "HTTPS://X/Y", "  https://z  "):
        if not is_https_url(good):
            raise AssertionError(f"https URL wrongly rejected: {good!r}")
    for bad in ("http://prov.example/sub", "ftp://x", "prov.example/sub", "",
                "javascript:alert(1)"):
        if is_https_url(bad):
            raise AssertionError(f"non-https URL wrongly accepted: {bad!r}")


check("subscription URLs: https:// only (UI gate)", _https_only_subscription_url)


def _subscription_secrets_encrypted_and_migrated() -> None:
    """Subscription URL/userinfo move OUT of settings.json into the encrypted
    blob, runtime code still sees them, and legacy plaintext fields migrate."""
    import json as _json
    from kapro_tun.core import storage as _st, paths as _paths, secrets_store as _ss
    tmp = _SecPath(_tf.mkdtemp(prefix="kt-sec-"))
    orig = _paths.app_data_dir
    _paths.app_data_dir = lambda: tmp
    try:
        (tmp / "settings.json").write_text(_json.dumps({
            "mode": "http",
            "subscription_url": "https://prov.example/sub/TOPSECRET",
            "subscription_urls": ["https://prov.example/sub/TOPSECRET"],
            "subscription_userinfo": {"download": 1},
        }), encoding="utf-8")
        s = _st.load_settings()
        if s.get("subscription_url") != "https://prov.example/sub/TOPSECRET":
            raise AssertionError("subscription_url not surfaced into settings dict")
        disk = (tmp / "settings.json").read_text(encoding="utf-8")
        if "TOPSECRET" in disk:
            raise AssertionError("subscription secret still leaks into settings.json")
        for k in ("subscription_url", "subscription_urls", "subscription_userinfo"):
            if k in _json.loads(disk):
                raise AssertionError(f"{k} not stripped from settings.json")
        # Round-trips on a fresh load (from the encrypted blob).
        if _st.load_settings().get("subscription_url") != "https://prov.example/sub/TOPSECRET":
            raise AssertionError("secret did not round-trip via the blob")
        blob = (tmp / "secrets.json").read_bytes()
        if _ss.is_supported():
            if not _ss.looks_encrypted(blob):
                raise AssertionError("secrets.json not encrypted on a keystore-capable platform")
            if b"TOPSECRET" in blob:
                raise AssertionError("plaintext secret visible inside the encrypted blob")
    finally:
        _paths.app_data_dir = orig
        _sh.rmtree(tmp, ignore_errors=True)


check("subscription secrets: encrypted blob + migration off settings.json",
      _subscription_secrets_encrypted_and_migrated)


def _no_silent_plaintext_on_encrypt_failure() -> None:
    """When the platform CAN encrypt but encryption fails, secrets must NOT be
    written in plaintext — raise SecretsError / return False + last_error."""
    from kapro_tun.core import storage as _st, paths as _paths, secrets_store as _ss
    from kapro_tun.core.parser import ProxyConfig as _PC
    tmp = _SecPath(_tf.mkdtemp(prefix="kt-encfail-"))
    o_app, o_sup, o_enc = _paths.app_data_dir, _ss.is_supported, _ss.encrypt
    _paths.app_data_dir = lambda: tmp
    _ss.is_supported = lambda: True

    def _boom(_data):
        raise OSError("DPAPI exploded")

    _ss.encrypt = _boom
    try:
        raised = False
        try:
            _st.save_subscription_secrets({"subscription_url": "https://x/LEAKME"})
        except _st.SecretsError:
            raised = True
        if not raised:
            raise AssertionError("save_subscription_secrets must raise SecretsError on supported-platform encrypt failure")
        blob = tmp / "secrets.json"
        if blob.exists() and b"LEAKME" in blob.read_bytes():
            raise AssertionError("secret written in plaintext despite encryption being supported")
        ok = _st.save_configs([_PC(name="s", protocol="vless", raw_url="vless://x",
                                   outbound={"server": "1.2.3.4"})])
        if ok is not False:
            raise AssertionError("save_configs must return False (not crash) on encrypt failure")
        if not _st.last_error():
            raise AssertionError("last_error() must be set after an encryption failure")
    finally:
        _paths.app_data_dir, _ss.is_supported, _ss.encrypt = o_app, o_sup, o_enc
        _sh.rmtree(tmp, ignore_errors=True)


check("secrets: no silent plaintext fallback when keystore is supported",
      _no_silent_plaintext_on_encrypt_failure)


def _https_subscription_fetch_no_nameerror() -> None:
    """P1 regression (v2.0.2): SubscriptionDialog._on_fetch() with an https://
    URL must reach the fetcher without NameError. The bug called
    `_subscription.is_https_url(url)` but only `is_https_url` was imported, so
    EVERY valid https import crashed. The fetcher is faked so no real network
    thread starts."""
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui import subscription_dialog as _sd

    class _FakeSig:
        def connect(self, *a, **k): pass

    class _FakeFetcher:
        constructed = False
        started = False
        def __init__(self, *a, **k):
            self.succeeded = _FakeSig()
            self.failed = _FakeSig()
            _FakeFetcher.constructed = True
        def start(self):
            _FakeFetcher.started = True

    o_fetch = _sd._SubscriptionFetcher
    o_warn = QMessageBox.warning
    _sd._SubscriptionFetcher = _FakeFetcher
    QMessageBox.warning = lambda *a, **k: 0  # no modal in headless
    try:
        dlg = _sd.SubscriptionDialog()
        dlg.url_edit.setText("https://example.com/api/sub/abc123")
        dlg._on_fetch()  # must NOT raise NameError
        if not _FakeFetcher.constructed:
            raise AssertionError("https URL must pass is_https_url and reach the fetcher")
        if not _FakeFetcher.started:
            raise AssertionError("a valid https URL should start the (faked) fetcher")
        dlg.deleteLater()
    finally:
        _sd._SubscriptionFetcher = o_fetch
        QMessageBox.warning = o_warn


check("subscription: https import reaches fetcher (no NameError regression)",
      _https_subscription_fetch_no_nameerror)


def _secret_migration_deferred_not_lost_on_encrypt_failure() -> None:
    """P2 regression (v2.0.2): if encryption fails during a save, a legacy
    plaintext subscription_url ALREADY on disk must NOT be deleted from
    settings.json (migration deferred, no data loss) — AND a brand-new secret
    that was never on disk must NOT be written to settings.json in plaintext."""
    from kapro_tun.core import storage as _st, paths as _paths, secrets_store as _ss
    import json as _json
    tmp = _SecPath(_tf.mkdtemp(prefix="kt-defer-"))
    o_app, o_sup, o_enc = _paths.app_data_dir, _ss.is_supported, _ss.encrypt
    _paths.app_data_dir = lambda: tmp
    _ss.is_supported = lambda: True

    def _boom(_data):
        raise OSError("DPAPI down")
    _ss.encrypt = _boom
    try:
        # Un-migrated state: a legacy plaintext subscription_url on disk.
        legacy = {"listen_port": 2080, "subscription_url": "https://legacy/KEEPME"}
        _paths.settings_file().write_text(_json.dumps(legacy), encoding="utf-8")
        # Save carries the legacy url AND a NEW secret that was never on disk.
        merged = dict(legacy)
        merged["subscription_urls"] = ["https://new/DONTLEAK"]
        _st.save_settings(merged)

        on_disk = _json.loads(_paths.settings_file().read_text(encoding="utf-8"))
        # 1. legacy plaintext preserved — migration deferred, not lost.
        if on_disk.get("subscription_url") != "https://legacy/KEEPME":
            raise AssertionError(
                f"legacy subscription_url must survive an encrypt failure, got {on_disk.get('subscription_url')!r}")
        # 2. a NEW secret (never on disk) must NOT become plaintext.
        if "subscription_urls" in on_disk:
            raise AssertionError("a new secret must not be written to settings.json in plaintext")
        # 3. nothing leaked into secrets.json either (encrypt failed).
        blob = _paths.secrets_file()
        if blob.exists() and b"KEEPME" in blob.read_bytes():
            raise AssertionError("secret leaked into secrets.json despite encrypt failure")
        # 4. last_error explains it wasn't persisted.
        if "not persisted" not in (_st.last_error() or ""):
            raise AssertionError(f"last_error() must explain the deferral, got {_st.last_error()!r}")
    finally:
        _paths.app_data_dir, _ss.is_supported, _ss.encrypt = o_app, o_sup, o_enc
        _sh.rmtree(tmp, ignore_errors=True)


check("secrets: failed-encrypt migration is deferred, never loses legacy URL",
      _secret_migration_deferred_not_lost_on_encrypt_failure)


def _runtime_config_secure_write_and_cleanup() -> None:
    import os as _os
    from kapro_tun.core import paths as _paths
    tmp = _SecPath(_tf.mkdtemp(prefix="kt-rt-"))
    orig = _paths.app_data_dir
    _paths.app_data_dir = lambda: tmp
    try:
        p = _paths.write_secure_text(_paths.runtime_config_file(), '{"uuid":"x"}')
        if p.read_text(encoding="utf-8") != '{"uuid":"x"}':
            raise AssertionError("write_secure_text content mismatch")
        if _os.name == "posix":
            mode = p.stat().st_mode & 0o777
            if mode != 0o600:
                raise AssertionError(f"runtime config must be 0600, got {oct(mode)}")
        _paths.write_secure_text(_paths.hysteria_config_file(), "auth: secret")
        leftover = _paths.remove_runtime_configs()
        if leftover:
            raise AssertionError(f"cleanup left credential files: {leftover}")
        if _paths.runtime_config_file().exists() or _paths.hysteria_config_file().exists():
            raise AssertionError("runtime configs not removed on cleanup")
        if _paths.remove_runtime_configs():
            raise AssertionError("second cleanup should be a no-op")
    finally:
        _paths.app_data_dir = orig
        _sh.rmtree(tmp, ignore_errors=True)


check("runtime configs: secure write (0600) + cleanup", _runtime_config_secure_write_and_cleanup)


def _killswitch_allows_hysteria_only_for_hy2() -> None:
    import sys as _sys
    if _sys.platform != "win32":
        return  # kill-switch is Windows-only
    from kapro_tun.core import killswitch as _ks
    calls: list = []
    o_add, o_sup, o_rm = _ks._add_rule, _ks.is_supported, _ks.remove
    _ks.is_supported = lambda: True
    _ks._add_rule = lambda name, args: (calls.append((name, list(args))) or True)
    _ks.remove = lambda: None  # don't touch the real firewall during install
    try:
        calls.clear()
        _ks.install(_SecPath("C:/x/xray.exe"))
        names = [c[0] for c in calls]
        if _ks._RULE_ALLOW_HYSTERIA in names:
            raise AssertionError("non-hy2 install must NOT add the hysteria allow rule")
        if _ks._RULE_ALLOW_XRAY not in names:
            raise AssertionError("xray allow rule missing")
        calls.clear()
        _ks.install(_SecPath("C:/x/xray.exe"), _SecPath("C:/x/hysteria.exe"))
        hy = [c for c in calls if c[0] == _ks._RULE_ALLOW_HYSTERIA]
        if not hy:
            raise AssertionError("hy2 install must add the hysteria allow rule")
        if not any("hysteria.exe" in a for a in hy[0][1]):
            raise AssertionError("hysteria allow rule must target hysteria.exe")
        # remove() must delete the hysteria rule name too.
        removed: list = []
        _ks.remove = o_rm
        import subprocess as _sp
        o_run = _sp.run
        _sp.run = lambda cmd, *a, **k: (removed.append(" ".join(map(str, cmd)))
                                        or type("R", (), {"returncode": 0})())
        try:
            _ks.remove()
        finally:
            _sp.run = o_run
        if not any(_ks._RULE_ALLOW_HYSTERIA in r for r in removed):
            raise AssertionError("remove() must delete the hysteria rule too")
    finally:
        _ks._add_rule, _ks.is_supported, _ks.remove = o_add, o_sup, o_rm


check("kill-switch: hysteria.exe allowed only for hy2, removed with the rest",
      _killswitch_allows_hysteria_only_for_hy2)


def _download_size_caps() -> None:
    from kapro_tun.core import net_download as _nd
    import requests as _rq

    class _FakeResp:
        def __init__(self, chunks, declared=None):
            self._chunks = chunks
            self.headers = {} if declared is None else {"Content-Length": str(declared)}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=0):
            for c in self._chunks:
                yield c

    o_get = _rq.get
    try:
        # Declared length over cap → rejected before streaming.
        _rq.get = lambda *a, **k: _FakeResp([b"x"], declared=10_000)
        try:
            _nd.download_to_memory("http://x", max_bytes=1000)
            raise AssertionError("declared over-cap must be rejected")
        except _nd.DownloadTooLarge:
            pass
        # No Content-Length but streamed past cap → aborted mid-stream.
        _rq.get = lambda *a, **k: _FakeResp([b"a" * 600, b"b" * 600])
        try:
            _nd.download_to_memory("http://x", max_bytes=1000)
            raise AssertionError("streamed over-cap must abort")
        except _nd.DownloadTooLarge:
            pass
        # Under cap → returns the bytes intact.
        _rq.get = lambda *a, **k: _FakeResp([b"hello", b"world"], declared=10)
        if _nd.download_to_memory("http://x", max_bytes=1000) != b"helloworld":
            raise AssertionError("under-cap download returned wrong bytes")
    finally:
        _rq.get = o_get


check("downloads: size cap rejects declared + aborts streamed over-limit",
      _download_size_caps)


def _placeholder_detects_stub() -> None:
    if not _sub.is_placeholder_config(parse(_STUB_URL)):
        raise AssertionError("0.0.0.0 / 'App not supported' not flagged as placeholder")


def _placeholder_passes_real() -> None:
    if _sub.is_placeholder_config(parse(_REAL_URL)):
        raise AssertionError("real server wrongly flagged as placeholder")


def _result_filters_stub() -> None:
    r = _sub.result_from_body(_STUB_URL)
    if r.configs:
        raise AssertionError(f"stub leaked into configs: {r.configs}")
    if len(r.placeholders) != 1:
        raise AssertionError(f"stub not recorded as placeholder: {r.placeholders}")


def _result_keeps_real() -> None:
    r = _sub.result_from_body(_REAL_URL)
    if len(r.configs) != 1 or r.placeholders:
        raise AssertionError(f"real cfg mishandled: cfgs={len(r.configs)} ph={r.placeholders}")


def _classify_404_not_dpi() -> None:
    import requests
    e = requests.exceptions.HTTPError("404 Client Error: Not Found")
    e.response = type("R", (), {"status_code": 404})()
    info = _sub.classify_fetch_error(e)
    if info.category != "not_found":
        raise AssertionError(f"404 misclassified as {info.category}")
    if info.suggest_manual:
        raise AssertionError("404 must NOT suggest manual paste")


def _classify_timeout() -> None:
    import requests
    info = _sub.classify_fetch_error(requests.exceptions.ConnectTimeout("timed out"))
    if info.category != "timeout":
        raise AssertionError(f"timeout misclassified as {info.category}")


def _classify_dpi() -> None:
    import requests
    e = requests.exceptions.SSLError("SSLEOFError EOF (unexpected_eof_while_reading)")
    info = _sub.classify_fetch_error(e)
    if info.category != "dpi" or not info.suggest_manual:
        raise AssertionError(f"DPI-shaped misclassified: {info.category}/{info.suggest_manual}")


def _classify_conn() -> None:
    import requests
    e = requests.exceptions.ConnectionError("getaddrinfo failed [Errno 11001]")
    info = _sub.classify_fetch_error(e)
    if info.category != "conn":
        raise AssertionError(f"generic conn misclassified as {info.category}")


check("placeholder: 0.0.0.0 stub detected",     _placeholder_detects_stub)
check("placeholder: real server passes",        _placeholder_passes_real)
check("result_from_body: stub -> placeholders", _result_filters_stub)
check("result_from_body: real -> configs",      _result_keeps_real)
check("classify: 404 = not_found, no manual",   _classify_404_not_dpi)
check("classify: timeout",                      _classify_timeout)
check("classify: DPI-shaped -> dpi",            _classify_dpi)
check("classify: generic conn -> conn",         _classify_conn)


# ---------------------------------------------------------------------------
# Test 15 — Subscription-Userinfo: remaining traffic / expiry (v1.16.15)
# ---------------------------------------------------------------------------

section("Subscription-Userinfo — parse + summary")


def _userinfo_parse_full() -> None:
    info = _sub.parse_userinfo("upload=100; download=200; total=1000; expire=4102444800")
    if info is None:
        raise AssertionError("full header parsed to None")
    if (info.upload, info.download, info.total, info.expire) != (100, 200, 1000, 4102444800):
        raise AssertionError(f"fields wrong: {info}")
    if info.used != 300 or info.remaining != 700:
        raise AssertionError(f"used/remaining wrong: {info.used}/{info.remaining}")


def _userinfo_parse_edge() -> None:
    if _sub.parse_userinfo("") is not None:
        raise AssertionError("empty header should parse to None")
    if _sub.parse_userinfo("garbage-no-fields") is not None:
        raise AssertionError("fieldless header should parse to None")
    partial = _sub.parse_userinfo("total=2048")
    if partial is None or partial.total != 2048 or partial.upload != 0:
        raise AssertionError(f"partial header wrong: {partial}")


def _userinfo_summary_limited() -> None:
    s = _sub.SubscriptionInfo(upload=100, download=200, total=1000,
                              expire=4102444800).summary()
    if "осталось" not in s or "до " not in s:
        raise AssertionError(f"limited summary missing parts: {s!r}")


def _userinfo_summary_unlimited() -> None:
    s = _sub.SubscriptionInfo(total=0, download=500).summary()
    if "осталось" in s or "использовано" not in s:
        raise AssertionError(f"unlimited summary wrong: {s!r}")


def _userinfo_summary_expired() -> None:
    s = _sub.SubscriptionInfo(total=1000, expire=1).summary()
    if "истекла" not in s:
        raise AssertionError(f"expired summary wrong: {s!r}")


def _userinfo_roundtrip() -> None:
    x = _sub.SubscriptionInfo(upload=1, download=2, total=3, expire=4)
    y = _sub.SubscriptionInfo.from_dict(x.to_dict())
    if (y.upload, y.download, y.total, y.expire) != (1, 2, 3, 4):
        raise AssertionError(f"round-trip mismatch: {y}")


check("userinfo: parse full header",   _userinfo_parse_full)
check("userinfo: parse empty/partial", _userinfo_parse_edge)
check("userinfo: summary (limited)",   _userinfo_summary_limited)
check("userinfo: summary (unlimited)", _userinfo_summary_unlimited)
check("userinfo: summary (expired)",   _userinfo_summary_expired)
check("userinfo: to_dict/from_dict",   _userinfo_roundtrip)


# ---------------------------------------------------------------------------
# Test 16 — Hysteria2 transport: installer asset + client config + xray chain
# ---------------------------------------------------------------------------
# Xray can't dial hy2, so the hysteria client runs as a local SOCKS5 and
# xray chains through it. E2E "does it connect" needs a real hy2 server;
# here we verify the pure asset/config logic that gets us there.

section("Hysteria2 — asset + client config + xray chain")

from kapro_tun.core import hysteria_installer as _hyi, hysteria_process as _hyp

_HY2_URL = ("hysteria2://mypassword@1.2.3.4:443"
            "?sni=example.com&insecure=1&obfs=salamander&obfs-password=xyz#hy2-test")


def _hy_asset_name() -> None:
    name = _hyi._asset_name()
    known = {"hysteria-windows-amd64.exe", "hysteria-windows-arm64.exe",
             "hysteria-darwin-amd64", "hysteria-darwin-arm64",
             "hysteria-linux-amd64", "hysteria-linux-arm64"}
    if name not in known:
        raise AssertionError(f"unexpected asset name: {name}")
    if sys.platform == "win32" and not name.startswith("hysteria-windows"):
        raise AssertionError(f"win32 asset wrong: {name}")


def _hy_client_config() -> None:
    c = _hyp.build_client_config(parse(_HY2_URL).outbound, 2089)
    if c["server"] != "1.2.3.4:443":
        raise AssertionError(f"server wrong: {c['server']}")
    if c["auth"] != "mypassword":
        raise AssertionError(f"auth wrong: {c['auth']}")
    if c["socks5"]["listen"] != "127.0.0.1:2089":
        raise AssertionError(f"socks5 listen wrong: {c['socks5']}")
    if c.get("tls", {}).get("sni") != "example.com" or not c["tls"].get("insecure"):
        raise AssertionError(f"tls wrong: {c.get('tls')}")
    obfs = c.get("obfs", {})
    if obfs.get("type") != "salamander" or obfs.get("salamander", {}).get("password") != "xyz":
        raise AssertionError(f"obfs wrong: {obfs}")


def _hy_xray_chain() -> None:
    from kapro_tun.core.xray_config import build_config
    full = build_config(parse(_HY2_URL), direct_domains=["example.com"],
                        hysteria_socks_port=2089)
    ob = full["outbounds"][0]
    if ob.get("protocol") != "socks" or ob.get("tag") != "proxy":
        raise AssertionError(f"hy2 proxy outbound not socks: {ob}")
    srv = ob["settings"]["servers"][0]
    if srv["address"] != "127.0.0.1" or srv["port"] != 2089:
        raise AssertionError(f"socks chain target wrong: {srv}")


def _hy_no_port_raises() -> None:
    from kapro_tun.core.xray_config import build_config
    try:
        build_config(parse(_HY2_URL), direct_domains=["example.com"])
    except NotImplementedError:
        return
    raise AssertionError("hy2 without socks port should raise NotImplementedError")


def _hy_bandwidth_config() -> None:
    # v1.19.6: link-speed hints -> hysteria brutal CC bandwidth block.
    ob = parse(_HY2_URL).outbound
    # 0/0 (default) -> no bandwidth block (BBR, safe).
    if "bandwidth" in _hyp.build_client_config(ob, 2089, up_mbps=0, down_mbps=0):
        raise AssertionError("bandwidth must be omitted at 0/0 (BBR default)")
    # both set -> 'N mbps' strings.
    bw = _hyp.build_client_config(ob, 2089, up_mbps=20, down_mbps=200).get("bandwidth")
    if bw != {"up": "20 mbps", "down": "200 mbps"}:
        raise AssertionError(f"bandwidth wrong: {bw}")
    # only one set -> still omitted (brutal CC needs both up AND down).
    if "bandwidth" in _hyp.build_client_config(ob, 2089, up_mbps=0, down_mbps=200):
        raise AssertionError("bandwidth needs BOTH up and down — omit if one is 0")


def _hy_start_auto_retries() -> None:
    # v1.19.7: a transient first-attempt FATAL (cold QUIC handshake / link
    # busy from a speedtest) must be retried automatically instead of
    # surfacing the "fails first, works on second connect" error.
    from kapro_tun.core import controller as ctrl
    mgr = ctrl.ConnectionManager(on_log=lambda _l: None)
    cfg = parse(_HY2_URL)
    state = {"starts": 0, "alive": False, "waits": 0}

    class _FakeHy:
        def is_running(self): return state["alive"]
        def start(self, path): state["starts"] += 1; state["alive"] = True
        def wait_until_listening(self, port, timeout=8.0):
            state["waits"] += 1
            if state["waits"] == 1:           # attempt 1: simulate FATAL exit
                state["alive"] = False
                return False
            return True                       # attempt 2: comes up
        def stop(self): state["alive"] = False
        def recent_logs(self): return ["FATAL ... timeout: no recent network activity"]

    orig = (ctrl.hysteria_installer.ensure_installed,
            ctrl.hysteria_process.write_client_config, ctrl.time.sleep)
    ctrl.hysteria_installer.ensure_installed = lambda *a, **k: None
    ctrl.hysteria_process.write_client_config = lambda *a, **k: "fake.yaml"
    ctrl.time.sleep = lambda *_a, **_k: None
    mgr.hysteria_process = _FakeHy()
    try:
        port = mgr._maybe_start_hysteria(cfg)
    finally:
        (ctrl.hysteria_installer.ensure_installed,
         ctrl.hysteria_process.write_client_config, ctrl.time.sleep) = orig
    if port != ctrl.hysteria_process.HYSTERIA_SOCKS_PORT:
        raise AssertionError(f"expected the hy SOCKS port back, got {port}")
    if state["starts"] < 2:
        raise AssertionError(f"transient failure must auto-retry (>=2 starts), got {state['starts']}")


check("hysteria: asset name per platform",   _hy_asset_name)
check("hysteria: client config mapping",     _hy_client_config)
check("hysteria: xray socks-chain",          _hy_xray_chain)
check("hysteria: no port -> NotImplemented", _hy_no_port_raises)
check("hysteria: bandwidth brutal-CC config", _hy_bandwidth_config)
check("hysteria: start auto-retries transient fail", _hy_start_auto_retries)


def _speed_test_surface() -> None:
    # v1.20.0: link-speed probe math + never-raise on failure.
    from kapro_tun.core import speed_test as st
    if st._mbps(12_500_000, 1.0) != 100:        # 12.5 MB in 1 s == 100 Mbps
        raise AssertionError(f"_mbps wrong: {st._mbps(12_500_000, 1.0)}")
    if st._mbps(0, 1.0) != 0 or st._mbps(100, 0) != 0:
        raise AssertionError("_mbps must be 0 for zero bytes or zero time")
    if st._mbps(10 ** 13, 1.0) > st._MAX_MBPS:
        raise AssertionError("_mbps must clamp to the max ceiling")
    # Point at a dead local port so it fails fast → (0, 0), never raises.
    orig = (st._DOWN_URL, st._UP_URL)
    st._DOWN_URL = "http://127.0.0.1:9/__down?bytes={n}"
    st._UP_URL = "http://127.0.0.1:9/__up"
    try:
        res = st.measure_link_speed(down_bytes=1000, up_bytes=1000, timeout=1.0)
    finally:
        st._DOWN_URL, st._UP_URL = orig
    if not (isinstance(res, tuple) and len(res) == 2 and all(isinstance(x, int) for x in res)):
        raise AssertionError(f"measure_link_speed must return (int, int): {res!r}")
    if res != (0, 0):
        raise AssertionError(f"dead host must give (0, 0), got {res!r}")


def _hy_auto_measures_when_empty() -> None:
    # v1.20.0: auto mode with no cached value measures the link and feeds
    # the result into the hysteria config (brutal CC), then caches it.
    from kapro_tun.core import controller as ctrl
    from kapro_tun.core import speed_test as st
    mgr = ctrl.ConnectionManager(on_log=lambda _l: None)
    cfg = parse(_HY2_URL)
    mgr.settings = dict(mgr.settings)
    mgr.settings.update(hysteria_auto_bandwidth=True,
                        hysteria_down_mbps=0, hysteria_up_mbps=0)
    captured = {}

    class _Hy:
        def is_running(self): return False
        def start(self, p): pass
        def wait_until_listening(self, port, timeout=8.0): return True
        def stop(self): pass
        def recent_logs(self): return []
    mgr.hysteria_process = _Hy()

    def _fake_write(outbound, port, up_mbps=0, down_mbps=0):
        captured["up"], captured["down"] = up_mbps, down_mbps
        return "fake.yaml"

    orig = (ctrl.hysteria_installer.ensure_installed,
            ctrl.hysteria_process.write_client_config,
            st.measure_link_speed, ctrl.storage.save_settings)
    ctrl.hysteria_installer.ensure_installed = lambda *a, **k: None
    ctrl.hysteria_process.write_client_config = _fake_write
    st.measure_link_speed = lambda *a, **k: (100, 20)
    ctrl.storage.save_settings = lambda s: None
    try:
        mgr._maybe_start_hysteria(cfg)
    finally:
        (ctrl.hysteria_installer.ensure_installed,
         ctrl.hysteria_process.write_client_config,
         st.measure_link_speed, ctrl.storage.save_settings) = orig
    # v2.0.1: auto-measured speed gets a SAFE CAP before brutal CC
    # (down *0.75, up *0.50) so a bursty app (Telegram) can't saturate the
    # uplink and stall the whole tunnel. 100/20 measured -> 75/10 applied.
    if captured.get("down") != 75 or captured.get("up") != 10:
        raise AssertionError(f"auto bw must be capped 100/20 -> 75/10: {captured}")
    if mgr.settings.get("hysteria_down_mbps") != 75 or mgr.settings.get("hysteria_up_mbps") != 10:
        raise AssertionError(f"capped bw must be cached: {mgr.settings.get('hysteria_down_mbps')}/{mgr.settings.get('hysteria_up_mbps')}")


def _hy_auto_bandwidth_margin() -> None:
    # v2.0.1: safe cap on AUTO-measured bandwidth (down*0.75, up*0.50). A
    # failed measurement (0) passes through as 0 (-> BBR); a tiny positive
    # value floors at 1 so a valid measurement never collapses to "BBR".
    from kapro_tun.core import hysteria_process as _hp
    if _hp.apply_auto_bandwidth_margin(100, 20) != (75, 10):
        raise AssertionError(f"100/20 -> {_hp.apply_auto_bandwidth_margin(100, 20)}, want (75,10)")
    if _hp.apply_auto_bandwidth_margin(0, 0) != (0, 0):
        raise AssertionError("0/0 must pass through (BBR fallback)")
    if _hp.apply_auto_bandwidth_margin(1, 1) != (1, 1):
        raise AssertionError("positive measurement must floor at 1, not collapse to 0")
    if not (_hp.AUTO_BW_DOWN_FACTOR > _hp.AUTO_BW_UP_FACTOR):
        raise AssertionError("uplink must be capped harder than downlink")


def _geoip_ru_gated_on_route_ru_direct() -> None:
    # v2.0.1: the TUN geoip:ru kernel bypass must fire ONLY when
    # route_ru_direct is on — a forced RU split otherwise destabilises
    # Telegram/CDN. Test the extracted gate with fakes (no real TUN / admin).
    from kapro_tun.core import controller as ctrl, geoip_ru as geo

    class _FakeSession:
        def __init__(self): self.cidr_calls = 0
        def add_bypass_cidrs(self, *a, **k):
            self.cidr_calls += 1
            return (0, 0)

    class _FakeReal:
        gateway = "192.168.1.1"
        index = 17

    class _FakeSelf:
        def __init__(self, ru): self.settings = {"route_ru_direct": ru}
        def _log(self, *_a): pass

    load_calls = {"n": 0}
    orig = geo.load_cidrs
    geo.load_cidrs = lambda: (load_calls.__setitem__("n", load_calls["n"] + 1)
                              or [("1.2.3.0", "255.255.255.0")])
    try:
        s_off = _FakeSession()
        ctrl.ConnectionManager._install_geoip_ru_bypass(_FakeSelf(False), s_off, _FakeReal(), 26)
        if load_calls["n"] != 0 or s_off.cidr_calls != 0:
            raise AssertionError(f"OFF must not load/add RU cidrs: load={load_calls['n']} add={s_off.cidr_calls}")
        s_on = _FakeSession()
        ctrl.ConnectionManager._install_geoip_ru_bypass(_FakeSelf(True), s_on, _FakeReal(), 26)
        if load_calls["n"] != 1 or s_on.cidr_calls != 1:
            raise AssertionError(f"ON must load+add RU cidrs: load={load_calls['n']} add={s_on.cidr_calls}")
    finally:
        geo.load_cidrs = orig


def _tun2socks_udp_timeout_arg() -> None:
    # v2.0.1: explicit -udp-timeout so idle UDP sessions are reaped under a
    # Telegram/UDP storm instead of piling up in the netstack.
    from kapro_tun.core.tun2socks_process import Tun2socksProcess
    args = Tun2socksProcess()._build_args("tun2socks", "127.0.0.1:2081", 1500, "warn")
    if "-udp-timeout" not in args:
        raise AssertionError("tun2socks args must include -udp-timeout")
    val = args[args.index("-udp-timeout") + 1]
    if not val.endswith("s"):
        raise AssertionError(f"-udp-timeout should be a duration like '30s', got {val!r}")


check("speed_test: probe math + never-raises", _speed_test_surface)
check("hysteria: auto-measures link speed when empty", _hy_auto_measures_when_empty)
check("hysteria: auto-bandwidth safe cap (down*0.75/up*0.50)", _hy_auto_bandwidth_margin)
check("controller: geoip:ru TUN bypass gated on route_ru_direct", _geoip_ru_gated_on_route_ru_direct)
check("tun2socks: -udp-timeout reaps idle UDP (storm guard)", _tun2socks_udp_timeout_arg)


# ---------------------------------------------------------------------------
# Test 17 — auto-updater: mirror-first download sources (v1.16.17)
# ---------------------------------------------------------------------------
# github.com is frequently DNS-blocked in RU (getaddrinfo failed), which
# made auto-update dead. The updater must try our mirror BEFORE GitHub.

section("Auto-updater — mirror-first download sources")

from kapro_tun.gui.updater_dialog import _setup_sources


def _updater_sources_order() -> None:
    srcs = _setup_sources("1.2.3")
    if len(srcs) != 2:
        raise AssertionError(f"expected 2 sources, got {srcs}")
    if "kaprovpn.pro/files" not in srcs[0]:
        raise AssertionError(f"mirror must be first: {srcs}")
    if "github.com" not in srcs[1]:
        raise AssertionError(f"github must be the fallback: {srcs}")
    if "1.2.3" not in srcs[0] or "1.2.3" not in srcs[1]:
        raise AssertionError(f"version missing from a source: {srcs}")
    if not srcs[0].endswith("KaproTUN-Setup-v1.2.3.exe"):
        raise AssertionError(f"mirror filename wrong: {srcs[0]}")


check("updater: mirror-first source order", _updater_sources_order)


# ---------------------------------------------------------------------------
# Test 18 — configs picker: sort + colour-coded rows (UX 2.0 / 1.17.0)
# ---------------------------------------------------------------------------

section("Configs picker — sort + rows")


def _picker_sort_and_rows() -> None:
    import os as _os2
    _os2.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.configs_picker import (
        ConfigsPickerDialog, _SORT_SPEED, _SORT_NAME, _SORT_PROTO,
    )
    from kapro_tun.core.parser import ProxyConfig as PC
    cfgs = [
        PC(name="🇩🇪 Германия", protocol="vless", raw_url="vless://x@127.0.0.1:1",
           outbound={"server": "127.0.0.1", "server_port": 1}),
        PC(name="🇳🇱 Нидерланды", protocol="trojan", raw_url="trojan://x@127.0.0.1:1",
           outbound={"server": "127.0.0.1", "server_port": 1}),
        PC(name="🇫🇷 Франция", protocol="hysteria2", raw_url="hysteria2://x@127.0.0.1:1",
           outbound={"server": "127.0.0.1", "server_port": 1}),
    ]
    dlg = ConfigsPickerDialog(cfgs, current_name="🇩🇪 Германия")
    if dlg._pinger is not None:
        dlg._pinger.wait(3000)  # let the (instant, localhost-refused) pinger finish
    dlg._pings = {"🇩🇪 Германия": 50, "🇳🇱 Нидерланды": 200, "🇫🇷 Франция": -1}

    dlg._sort_mode = _SORT_SPEED
    if [c.name for c in dlg._sorted_configs()] != ["🇩🇪 Германия", "🇳🇱 Нидерланды", "🇫🇷 Франция"]:
        raise AssertionError("speed sort wrong (reachable asc, UDP last)")

    dlg._sort_mode = _SORT_NAME  # flag stripped -> Германия < Нидерланды < Франция
    if [c.name for c in dlg._sorted_configs()] != ["🇩🇪 Германия", "🇳🇱 Нидерланды", "🇫🇷 Франция"]:
        raise AssertionError("name sort wrong (flag-emoji not stripped?)")

    dlg._sort_mode = _SORT_PROTO
    protos = [c.protocol for c in dlg._sorted_configs()]
    if protos != sorted(protos):
        raise AssertionError(f"proto sort not ordered: {protos}")

    # rows + pill styling must build without raising
    if dlg._make_row(cfgs[0]) is None:
        raise AssertionError("row widget is None")
    dlg.deleteLater()


check("picker: sort speed/name/proto + row build", _picker_sort_and_rows)


def _picker_subs_refresh_merge_and_url_list() -> None:
    # v1.18.0: "🔄 Обновить" re-fetches all saved subscriptions and merges.
    # Verify (a) the saved-URL list migrates from the legacy single URL and
    # de-dupes, and (b) the merge adds new servers, refreshes existing ones
    # by name (no duplicates), and never deletes.
    import os as _os3
    _os3.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.core import storage
    from kapro_tun.core.parser import ProxyConfig as PC
    from kapro_tun.gui.configs_picker import ConfigsPickerDialog

    orig = (storage.save_configs, storage.save_settings,
            storage.load_settings, QMessageBox.information)
    saved = {"configs": None}
    storage.save_configs = lambda cfgs: saved.__setitem__("configs", list(cfgs))
    storage.save_settings = lambda s: None
    QMessageBox.information = lambda *a, **k: None  # no modal hang offscreen
    try:
        existing = [
            PC(name="🇩🇪 Германия", protocol="vless", raw_url="vless://x@127.0.0.1:1",
               outbound={"server": "127.0.0.1", "server_port": 1}),
            PC(name="🇳🇱 Нидерланды", protocol="trojan", raw_url="trojan://x@127.0.0.1:2",
               outbound={"server": "127.0.0.1", "server_port": 2}),
        ]
        dlg = ConfigsPickerDialog(existing, current_name="🇩🇪 Германия")
        if dlg._pinger is not None:
            dlg._pinger.wait(3000)

        # (a) URL-list: de-dupe preserving order …
        storage.load_settings = lambda: {
            "subscription_urls": ["u1", "u2", "u1"], "subscription_url": "x"}
        if dlg._all_subscription_urls() != ["u1", "u2"]:
            raise AssertionError("subscription_urls not deduped/ordered")
        # … and migrate from the legacy single URL when the list is empty.
        storage.load_settings = lambda: {
            "subscription_urls": [], "subscription_url": "legacy"}
        if dlg._all_subscription_urls() != ["legacy"]:
            raise AssertionError("legacy single-URL migration failed")

        # (b) merge: one same-name update + one brand-new server.
        updated = PC(name="🇩🇪 Германия", protocol="vless", raw_url="vless://x@127.0.0.1:443",
                     outbound={"server": "127.0.0.1", "server_port": 443})
        brand_new = PC(name="🇫🇷 Франция", protocol="vless", raw_url="vless://x@127.0.0.1:3",
                       outbound={"server": "127.0.0.1", "server_port": 3})
        dlg._on_subs_refreshed({
            "configs": [updated, brand_new], "userinfo": None,
            "ok": 1, "errors": [], "total": 1,
        })
        if dlg._pinger is not None:
            dlg._pinger.wait(3000)
        names = [c.name for c in dlg._configs]
        if names.count("🇩🇪 Германия") != 1:
            raise AssertionError("update-by-name created a duplicate")
        de = next(c for c in dlg._configs if c.name == "🇩🇪 Германия")
        if de.outbound.get("server_port") != 443:
            raise AssertionError("existing server not refreshed on merge")
        if "🇫🇷 Франция" not in names:
            raise AssertionError("new server not added on merge")
        if "🇳🇱 Нидерланды" not in names:
            raise AssertionError("merge deleted a server it must keep")
        if saved["configs"] is None:
            raise AssertionError("merge didn't persist via save_configs")
        dlg.deleteLater()
    finally:
        (storage.save_configs, storage.save_settings,
         storage.load_settings, QMessageBox.information) = orig


check("picker: subscription refresh merge + URL-list migration",
      _picker_subs_refresh_merge_and_url_list)


# ---------------------------------------------------------------------------
# Test — TUN reliability hardening (v2.1.4)
#   D1 startup recovery · D2 DNS health-check rollback · D3 watchdog · D4 logs
# ---------------------------------------------------------------------------

section("TUN reliability — recovery / DNS health / watchdog")


def _dns_health_probe_contract() -> None:
    """probe() is bounded, never raises, and reads success/failure correctly.

    Deterministic offline: 'localhost' always resolves (loopback / hosts file);
    a '.invalid' TLD (RFC 6761) never resolves — no network dependency either
    way."""
    from kapro_tun.core import dns_health

    if dns_health.probe(hosts=("localhost",), timeout=2.0, attempts=1) is not True:
        raise AssertionError("probe() should resolve localhost → True")

    bad = dns_health.probe(
        hosts=("nonexistent-kaprotun-xyz.invalid",), timeout=1.0, attempts=1)
    if bad is not False:
        raise AssertionError("probe() should return False for an .invalid host")

    # Must never raise, whatever it's handed.
    for kw in ({}, {"hosts": ("localhost",)}, {"timeout": 0.1, "attempts": 1}):
        r = dns_health.probe(**kw)
        if not isinstance(r, bool):
            raise AssertionError(f"probe({kw}) returned non-bool {type(r)}")


check("dns_health.probe: bounded, never raises, correct verdict",
      _dns_health_probe_contract)


def _tun_recovery_journal_lifecycle() -> None:
    """D1: mark→has_pending→recover restores DNS + deletes journal, and is
    idempotent. Covers clean (no journal), crashed (journal present), and
    corrupt-journal cases — plus the index→name fallback."""
    import os
    import sys as _sys
    import json as _json
    import types
    import tempfile
    from kapro_tun.core import paths, tun_recovery

    import kapro_tun.core as _core_pkg
    tmpdir = tempfile.mkdtemp(prefix="kaprotun_rec_")
    journal = os.path.join(tmpdir, "tun-session.json")
    orig_path_fn = paths.tun_recovery_file
    orig_nr = _sys.modules.get("kapro_tun.core.network_routes")
    orig_attr = getattr(_core_pkg, "network_routes", None)

    # Fake the route backend so the test is platform-independent (the real one
    # is Windows-only) and so we can observe which restore path was taken.
    calls = {"by_index": [], "by_name": []}
    fake_nr = types.ModuleType("kapro_tun.core.network_routes")
    fake_nr.reset_dns_by_index = lambda idx: (calls["by_index"].append(idx) or True)
    fake_nr.reset_dns = lambda name: calls["by_name"].append(name)

    from pathlib import Path
    paths.tun_recovery_file = lambda: Path(journal)
    # recover() does `from . import network_routes` — on Windows the real module
    # is already imported, so the binding comes from the PACKAGE ATTRIBUTE, not
    # sys.modules. Patch both so the fake is picked up on every platform.
    _sys.modules["kapro_tun.core.network_routes"] = fake_nr
    _core_pkg.network_routes = fake_nr
    try:
        # (0) Clean machine: no journal → recover is silent and idempotent.
        tun_recovery.clear()
        if tun_recovery.has_pending():
            raise AssertionError("has_pending() true after clear()")
        if tun_recovery.recover() != []:
            raise AssertionError("recover() not silent when no journal present")

        # (1) Mark a session, then crash (we just don't call clear()).
        if not tun_recovery.mark("Ethernet 2", 17):
            raise AssertionError("mark() returned False")
        if not tun_recovery.has_pending():
            raise AssertionError("has_pending() false right after mark()")
        with open(journal, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        if (data.get("iface_name") != "Ethernet 2"
                or data.get("iface_index") != 17
                or data.get("dns_cleared") is not True):
            raise AssertionError(f"journal payload wrong: {data}")

        # (2) Next startup recovers: restores DNS by INDEX and deletes journal.
        actions = tun_recovery.recover()
        if not actions:
            raise AssertionError("recover() produced no actions for a live journal")
        if calls["by_index"] != [17]:
            raise AssertionError(f"DNS not restored by index: {calls['by_index']}")
        if tun_recovery.has_pending():
            raise AssertionError("journal not deleted after recover()")

        # (3) Idempotent: a second recover() does nothing.
        if tun_recovery.recover() != []:
            raise AssertionError("recover() not idempotent (second call acted)")

        # (4) Fallback to name when no usable index was journalled.
        calls["by_index"].clear(); calls["by_name"].clear()
        tun_recovery.mark("Беспроводная сеть", None)
        tun_recovery.recover()
        if calls["by_name"] != ["Беспроводная сеть"]:
            raise AssertionError(f"name-fallback restore not used: {calls}")

        # (5) Corrupt journal is cleaned up, not left to trip every startup.
        with open(journal, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        if not tun_recovery.has_pending():
            raise AssertionError("corrupt journal should still count as pending")
        acts = tun_recovery.recover()
        if tun_recovery.has_pending():
            raise AssertionError("corrupt journal not removed by recover()")
        if not any("повреждён" in a for a in acts):
            raise AssertionError(f"corrupt-journal note missing: {acts}")
    finally:
        paths.tun_recovery_file = orig_path_fn
        if orig_nr is not None:
            _sys.modules["kapro_tun.core.network_routes"] = orig_nr
        else:
            _sys.modules.pop("kapro_tun.core.network_routes", None)
        if orig_attr is not None:
            _core_pkg.network_routes = orig_attr
        else:
            try:
                delattr(_core_pkg, "network_routes")
            except AttributeError:
                pass
        try:
            if os.path.exists(journal):
                os.remove(journal)
            os.rmdir(tmpdir)
        except OSError:
            pass


check("tun_recovery: journal lifecycle, idempotent, corrupt-safe",
      _tun_recovery_journal_lifecycle)


def _connect_tun_has_dns_rollback_wiring() -> None:
    """D2: _connect_tun journals the interface BEFORE clearing its DNS, then
    health-checks the TUN DNS path and rolls back (clears journal too) on
    failure. Verified at source level — the live path needs admin + a real
    TUN, so we assert the safety wiring is present and correctly ordered."""
    import inspect
    from kapro_tun.core import controller
    from kapro_tun.core.controller import ConnectionManager

    # Imports actually wired up.
    if not hasattr(controller, "dns_health") or not hasattr(controller, "tun_recovery"):
        raise AssertionError("controller missing dns_health / tun_recovery imports")

    src = inspect.getsource(ConnectionManager._connect_tun)
    mark_i = src.find("tun_recovery.mark(")
    clear_dns_i = src.find("session.set_dns(real.name, [])")
    probe_i = src.find("dns_health.probe(")
    raise_i = src.find("raise ConnectionError(")
    if mark_i < 0:
        raise AssertionError("_connect_tun does not journal the interface (mark)")
    if clear_dns_i < 0:
        raise AssertionError("_connect_tun no longer clears physical DNS?")
    if not (0 <= mark_i < clear_dns_i):
        raise AssertionError("journal mark() must precede the DNS clear")
    if probe_i < 0 or raise_i < 0:
        raise AssertionError("_connect_tun missing DNS health-check + rollback raise")
    if not (clear_dns_i < probe_i):
        raise AssertionError("health-check must run AFTER the DNS clear")

    exc_src = src[src.find("except Exception"):]
    if "session.restore()" not in exc_src or "tun_recovery.clear()" not in exc_src:
        raise AssertionError("rollback path must restore() and clear the journal")

    disc = inspect.getsource(ConnectionManager.disconnect)
    if "tun_recovery.clear()" not in disc:
        raise AssertionError("disconnect() must clear the recovery journal")


check("controller: DNS health-check + crash-journal rollback wiring",
      _connect_tun_has_dns_rollback_wiring)


def _tun_dns_guarded_gate() -> None:
    """D3 gate: a fresh (disconnected) manager is NOT guarded, so the watchdog
    stays idle in HTTP mode / when disconnected / with leak protection off."""
    from kapro_tun.core.controller import ConnectionManager, MODE_TUN
    mgr = ConnectionManager(on_log=lambda _l: None)
    if mgr.tun_dns_guarded() is not False:
        raise AssertionError("disconnected manager reported tun_dns_guarded() True")
    # Even if settings say TUN + leak-protection, a disconnected manager is not
    # guarded (no live session holding DNS hostage).
    mgr.settings["mode"] = MODE_TUN
    mgr.settings["dns_leak_protection"] = True
    if mgr.tun_dns_guarded() is not False:
        raise AssertionError("guarded True with no live connection")


check("controller.tun_dns_guarded(): false unless a live TUN session",
      _tun_dns_guarded_gate)


def _watchdog_threshold_and_gating() -> None:
    """D3: _DnsWatchdog emits `unhealthy` only after >=2 consecutive failed
    probes AND only while guarded. Driven with interval 0 and a stubbed probe
    so it's deterministic and sub-second; threads are always stopped."""
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.main_window import _DnsWatchdog
    from kapro_tun.core import dns_health

    orig_probe = dns_health.probe
    dns_health.probe = lambda **k: False   # pretend DNS is dead
    wd = wd2 = None
    try:
        # (a) Guarded + failing → emits (sustained-outage heal trigger).
        emits = []
        wd = _DnsWatchdog(is_guarded=lambda: True)
        wd._interval_s = 0
        if wd._fail_threshold < 2:
            raise AssertionError("watchdog should require >=2 fails before healing")

        def _on_emit():
            emits.append(1)
            wd._stop = True
        wd.unhealthy.connect(_on_emit, Qt.DirectConnection)
        wd.start()
        wd.wait(3000)
        if wd.isRunning():
            wd.stop()
        if not emits:
            raise AssertionError("watchdog never emitted on a sustained DNS outage")

        # (b) NOT guarded → never emits, even with the probe failing.
        emits2 = []
        wd2 = _DnsWatchdog(is_guarded=lambda: False)
        wd2._interval_s = 0
        wd2.unhealthy.connect(lambda: emits2.append(1), Qt.DirectConnection)
        wd2.start()
        wd2.wait(150)        # let it spin through many guard-skips
        wd2.stop()
        if emits2:
            raise AssertionError("watchdog emitted while not in guarded TUN mode")
    finally:
        dns_health.probe = orig_probe
        for w in (wd, wd2):
            try:
                if w is not None and w.isRunning():
                    w.stop()
            except Exception:
                pass


check("watchdog: emits only on sustained failure while guarded",
      _watchdog_threshold_and_gating)


def _ps_forces_utf8_output() -> None:
    """D4: the PowerShell wrapper prepends a UTF-8 OutputEncoding line so
    Cyrillic interface names survive instead of arriving as mojibake."""
    import sys as _sys
    if _sys.platform != "win32":
        return   # network_routes is win32-only (ctypes.windll at import time)
    import inspect
    from kapro_tun.core import network_routes as nr
    src = inspect.getsource(nr._ps)
    if "OutputEncoding" not in src or "UTF8" not in src:
        raise AssertionError("_ps() does not force UTF-8 console output encoding")
    if not hasattr(nr, "reset_dns_by_index"):
        raise AssertionError("network_routes missing reset_dns_by_index helper")


check("network_routes._ps: forces UTF-8 (fixes garbled iface names)",
      _ps_forces_utf8_output)


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
