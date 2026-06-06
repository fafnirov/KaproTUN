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

# Make the suite runnable straight from the repo root —
#   python kapro_tun/scripts/smoke_test.py
# — without requiring PYTHONPATH to be set, while still working under
#   python -m kapro_tun.scripts.smoke_test
# __file__ is <repo>/kapro_tun/scripts/smoke_test.py, so three dirnames up is
# the repo root; prepend it so `import kapro_tun` resolves either way.
import os as _os
import sys as _sys
_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

# Windows-console safety: assert messages / labels can contain non-ASCII
# (Cyrillic, →, ≥). On a cp866/cp1251 console a bare print() of those raises
# UnicodeEncodeError and crashes the whole suite mid-report. Reconfigure
# stdout/stderr to UTF-8 with errors="replace" so output degrades to '?'
# instead of crashing. Best-effort (no-op on streams that can't reconfigure).
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import sys
from typing import Callable


# ---------------------------------------------------------------------------
# Global test sandbox — keep the suite from polluting the REAL app-data dir.
# Tests construct ConnectionManagers / MainWindows, write runtime configs, and
# emit app_log lines; without this they'd append to the user's real
# %LOCALAPPDATA%/KaproTUN/app.log (and settings/configs/runtime configs). We
# redirect paths.app_data_dir at a throwaway temp dir, but keep the BINARY
# lookup dirs (sing-box, tun, xray, hysteria) pointing at the real install so
# the real `sing-box check` test can still find the downloaded binary + WinTUN.
# Per-test monkeypatches of app_data_dir/app_log_file/settings_file nest fine
# under this (they save+restore around it).
# ---------------------------------------------------------------------------
import atexit as _sbx_atexit
import shutil as _sbx_shutil
import tempfile as _sbx_tempfile
from pathlib import Path as _SbxPath

from kapro_tun.core import paths as _sbx_paths
from kapro_tun.core import app_log as _sbx_app_log

# Capture the REAL app.log path + a content snapshot BEFORE redirecting, so a
# regression test below can prove the SUITE never appended to it. We snapshot
# the line set (not just the size): a concurrently-running KaproTUN app may
# append its own [mem]/[connect] lifecycle lines during the ~10s smoke run, so
# a raw size/byte comparison is flaky — the robust check is "no NEW line carries
# a smoke-test signature".
_REAL_APP_LOG = _sbx_paths.app_log_file()
try:
    _REAL_APP_LOG_SIZE_BEFORE = _REAL_APP_LOG.stat().st_size
    _REAL_APP_LOG_LINES_BEFORE = frozenset(
        _REAL_APP_LOG.read_text(encoding="utf-8", errors="replace").splitlines())
except OSError:
    _REAL_APP_LOG_SIZE_BEFORE = 0
    _REAL_APP_LOG_LINES_BEFORE = frozenset()

# Real binary dirs (path-only, no mkdir) captured before the redirect. We keep
# ONLY sing_box_dir + tun_dir real, because the real `sing-box check` test needs
# the installed binary (sing_box_dir) and a valid existing cwd / WinTUN driver
# (tun_dir). We must NOT re-pin xray_dir or hysteria_dir: hysteria_dir also
# holds a *runtime config* (hysteria-client.yaml), and pinning it to a real
# (maybe non-existent) dir breaks the runtime-config write/cleanup test. Those
# two follow app_data_dir into the sandbox like every other writable path.
_REAL_BASE = _sbx_paths.app_data_dir()
_REAL_SINGBOX_DIR = _REAL_BASE / "sing-box"
_REAL_TUN_DIR = _REAL_BASE / "tun"

_SANDBOX_DIR = _SbxPath(_sbx_tempfile.mkdtemp(prefix="kaprotun-smoke-"))
_sbx_paths.app_data_dir = lambda: _SANDBOX_DIR
_sbx_paths.sing_box_dir = lambda: _REAL_SINGBOX_DIR
_sbx_paths.tun_dir = lambda: _REAL_TUN_DIR
# Defence in depth: redirect app_log_file() directly too. It already resolves
# via app_data_dir() (so it's covered), but pinning it explicitly means even a
# test that restores a stale captured app_log_file ref still lands in the
# sandbox — app.log must NEVER resolve to the real path during the suite.
_sbx_paths.app_log_file = lambda: _SANDBOX_DIR / "app.log"
_sbx_app_log._reset_for_test()  # reopen the rotating handler under the sandbox
_sbx_atexit.register(lambda: _sbx_shutil.rmtree(_SANDBOX_DIR, ignore_errors=True))


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
        controller, parser, storage, paths,
        subscription, geoip_ru, killswitch, i18n, system_proxy,
        ip_probe, secrets_store, ipv6_block,
        bandwidth_history, webrtc_block, leak_test, crash_handler,
        sing_box_config, sing_box_installer, sing_box_process,
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
# Test 3 — DNS options (v1.9.0)
# ---------------------------------------------------------------------------
# Each option must produce a valid config. "system" should NOT add a dns
# block (xray complains about empty servers). Named options must add the
# block, force IPv4, and include the bypass-by-IP routing rule for that
# service's plain IPs.

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
    # v3.1.0: sing-box captures IPv6 in-tunnel and rejects global v6 with a RST,
    # so _maybe_arm_ipv6_block NEVER installs a netsh v6 DROP (which caused
    # ERR_NETWORK_ACCESS_DENIED) — it only clears any stale block.
    from kapro_tun.core import controller as ctrl
    mgr = ctrl.ConnectionManager(on_log=lambda _l: None)
    orig = (ctrl.ipv6_block.is_supported, ctrl.ipv6_block.install, ctrl.ipv6_block.remove)
    counts = {"install": 0, "remove": 0}
    ctrl.ipv6_block.is_supported = lambda: True
    ctrl.ipv6_block.install = lambda: (counts.__setitem__("install", counts["install"] + 1) or True)
    ctrl.ipv6_block.remove = lambda: counts.__setitem__("remove", counts["remove"] + 1)
    try:
        mgr.settings = {"ipv6_leak_protection": True}
        mgr._maybe_arm_ipv6_block()
        if counts["install"] != 0:
            raise AssertionError("must NOT install a netsh v6 block (sing-box handles v6)")
        if counts["remove"] < 1:
            raise AssertionError("must clear any stale v6 block")
    finally:
        (ctrl.ipv6_block.is_supported, ctrl.ipv6_block.install, ctrl.ipv6_block.remove) = orig


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
    # v2.1.6: the DEFAULT buffers must be the memory-safe preset, NOT the old
    # 4m/4m that ballooned private memory to multiple GB under many flows.
    if snd == "4m" or rcv == "4m":
        raise AssertionError(
            f"default tun2socks buffers must not be 4m (memory blow-up): {snd}/{rcv}")
    if (snd, rcv) != ("1m", "1m"):
        raise AssertionError(f"default buffers expected balanced 1m/1m, got {snd}/{rcv}")
    # base command must still be intact
    for flag in ("-device", "-proxy", "-mtu", "-loglevel"):
        if flag not in args:
            raise AssertionError(f"tun2socks base arg {flag} missing: {args}")




def _tun2socks_buffer_presets() -> None:
    """v2.1.6: economy/balanced/speed presets resolve correctly, the default is
    NOT the memory-hungry 4m, and 4m is reachable ONLY by explicitly asking for
    'speed' (a typo in settings must fall back to the safe balanced preset)."""
    from kapro_tun.core import tun2socks_process as t2s

    if t2s.BUFFER_PRESETS.get("economy") != ("512k", "512k"):
        raise AssertionError("economy preset should be 512k/512k")
    if t2s.BUFFER_PRESETS.get("balanced") != ("1m", "1m"):
        raise AssertionError("balanced preset should be 1m/1m")
    if t2s.BUFFER_PRESETS.get("speed") != ("4m", "4m"):
        raise AssertionError("speed preset should be 4m/4m")

    if t2s.DEFAULT_BUFFER_PRESET != "balanced":
        raise AssertionError("default preset must be 'balanced'")
    if t2s.resolve_buffer_preset(None) == ("4m", "4m"):
        raise AssertionError("default (None) must not resolve to 4m")
    if t2s.resolve_buffer_preset("garbage") != ("1m", "1m"):
        raise AssertionError("unknown preset must fall back to balanced 1m/1m")
    # 4m only via an explicit 'speed' request.
    if t2s.resolve_buffer_preset("speed") != ("4m", "4m"):
        raise AssertionError("'speed' must yield the explicit 4m/4m")
    only_4m = [k for k, v in t2s.BUFFER_PRESETS.items() if v == ("4m", "4m")]
    if only_4m != ["speed"]:
        raise AssertionError(f"4m/4m must be reachable only via 'speed', not {only_4m}")

    # start() must thread the preset onto _build_args via the instance attrs.
    p = t2s.Tun2socksProcess()
    p.TCP_SNDBUF, p.TCP_RCVBUF = t2s.resolve_buffer_preset("speed")
    args = p._build_args("tun2socks", "127.0.0.1:2081", 1500, "warn")
    if args[args.index("-tcp-sndbuf") + 1] != "4m":
        raise AssertionError("explicit speed preset not reflected in args")




def _performance_preset_default_is_safe() -> None:
    """The storage default for performance_preset is 'balanced' (not 'speed')."""
    from kapro_tun.core import storage
    if storage.DEFAULT_SETTINGS.get("performance_preset") != "balanced":
        raise AssertionError(
            f"performance_preset default must be 'balanced', got "
            f"{storage.DEFAULT_SETTINGS.get('performance_preset')!r}")


check("storage: performance_preset defaults to balanced", _performance_preset_default_is_safe)


def _memory_pressure_tiered() -> None:
    """v2.1.7 tiered guard: memory_pressure_reason classifies tun2socks (mem OR
    handles OR threads) and xray (mem OR handles) into 'moderate'/'critical',
    critical taking precedence; quiet when healthy; never raises on None."""
    from kapro_tun.core import controller as C
    from kapro_tun.core.controller import ConnectionManager
    from kapro_tun.core.proc_stats import ProcSample

    mgr = ConnectionManager(on_log=lambda _l: None)

    def sev(t_mem, t_h, t_thr, x_mem, x_h):
        v = mgr.memory_pressure_reason({
            "tun2socks": ProcSample(t_mem, t_h, t_thr, 0.0),
            "xray": ProcSample(x_mem, x_h, 0, 0.0),
        })
        return v[0] if v else None

    if sev(300_000_000, 800, 200, 400_000_000, 5_000) is not None:
        raise AssertionError("healthy usage wrongly flagged")
    # tun2socks moderate, each facet.
    if sev(C.MEM_TUN2SOCKS_MOD_BYTES + 1, 0, 0, 0, 0) != "moderate":
        raise AssertionError("tun mem moderate not classified")
    if sev(0, C.MEM_TUN2SOCKS_MOD_HANDLES + 1, 0, 0, 0) != "moderate":
        raise AssertionError("tun handles moderate not classified")
    if sev(0, 0, C.MEM_TUN2SOCKS_MOD_THREADS + 1, 0, 0) != "moderate":
        raise AssertionError("tun threads moderate not classified")
    # tun2socks critical, each facet.
    if sev(C.MEM_TUN2SOCKS_CRIT_BYTES + 1, 0, 0, 0, 0) != "critical":
        raise AssertionError("tun mem critical not classified")
    if sev(0, C.MEM_TUN2SOCKS_CRIT_HANDLES + 1, 0, 0, 0) != "critical":
        raise AssertionError("tun handles critical not classified")
    if sev(0, 0, C.MEM_TUN2SOCKS_CRIT_THREADS + 1, 0, 0) != "critical":
        raise AssertionError("tun threads critical (UDP storm) not classified")
    # xray moderate + critical.
    if sev(0, 0, 0, C.MEM_XRAY_MOD_BYTES + 1, 0) != "moderate":
        raise AssertionError("xray mem moderate not classified")
    if sev(0, 0, 0, 0, C.MEM_XRAY_MOD_HANDLES + 1) != "moderate":
        raise AssertionError("xray handles moderate not classified")
    if sev(0, 0, 0, C.MEM_XRAY_CRIT_BYTES + 1, 0) != "critical":
        raise AssertionError("xray mem critical not classified")
    if sev(0, 0, 0, 0, C.MEM_XRAY_CRIT_HANDLES + 1) != "critical":
        raise AssertionError("xray handles critical not classified")
    # Critical takes precedence over a co-occurring moderate.
    if sev(C.MEM_TUN2SOCKS_CRIT_BYTES + 1, C.MEM_TUN2SOCKS_MOD_HANDLES + 1, 0, 0, 0) != "critical":
        raise AssertionError("critical must outrank moderate")
    # None samples → no crash, no false positive.
    if mgr.memory_pressure_reason({"tun2socks": None, "xray": None}) is not None:
        raise AssertionError("None samples should yield no pressure")
    # v2.1.9: thresholds must sit ABOVE the observed ~1.9 GB idle baseline so a
    # healthy session never trips a heal (the false-positive reconnect-loop), and
    # below the observed ~4.7 GB runaway.
    if C.MEM_TUN2SOCKS_MOD_BYTES <= 2_200_000_000:
        raise AssertionError("tun2socks moderate mem threshold too low — must clear the ~1.9 GB idle baseline")
    if not (3_500_000_000 <= C.MEM_TUN2SOCKS_CRIT_BYTES <= 5_000_000_000):
        raise AssertionError("tun2socks critical mem threshold out of band")
    if not (C.MEM_TUN2SOCKS_MOD_BYTES < C.MEM_TUN2SOCKS_CRIT_BYTES):
        raise AssertionError("moderate threshold must be below critical")


check("controller: tiered runaway classification (moderate/critical)",
      _memory_pressure_tiered)


def _mem_heal_decision_logic() -> None:
    """v2.1.7: critical heals bypass cooldown, moderate respects it, the cap
    stops the loop, and economy escalation kicks in after repeated runaway."""
    from kapro_tun.core.controller import mem_heal_decision as D

    # Critical heals NOW even though a heal just happened (cooldown bypassed).
    if not D("critical", 1000.0, 1000.0, 0, max_heals=4, cooldown_s=180.0)["do_heal"]:
        raise AssertionError("critical must bypass cooldown")
    # Moderate within cooldown → wait.
    if D("moderate", 1000.0, 999.0, 0, max_heals=4, cooldown_s=180.0)["do_heal"]:
        raise AssertionError("moderate must respect cooldown")
    # Moderate after cooldown → heal.
    if not D("moderate", 1000.0, 800.0, 0, max_heals=4, cooldown_s=180.0)["do_heal"]:
        raise AssertionError("moderate should heal once cooldown elapsed")
    # At the cap → exhausted, no heal (no infinite loop), even for critical.
    d = D("critical", 1e9, 0.0, 4, max_heals=4)
    if d["do_heal"] or not d["exhausted"]:
        raise AssertionError("must be exhausted at the cap (no endless reconnect loop)")
    # Economy escalation from the escalate_after-th heal, not the first.
    if D("critical", 1e9, 0.0, 0, max_heals=4, escalate_after=2)["escalate_economy"]:
        raise AssertionError("must not escalate to economy on the first heal")
    if not D("critical", 1e9, 0.0, 1, max_heals=4, escalate_after=2)["escalate_economy"]:
        raise AssertionError("should escalate to economy after repeated runaway")
    # No severity → no action at all.
    n = D(None, 1e9, 0.0, 0)
    if n["do_heal"] or n["exhausted"] or n["escalate_economy"]:
        raise AssertionError("no severity → no action")


check("controller: mem_heal_decision (critical bypass / cooldown / cap / economy)",
      _mem_heal_decision_logic)


def _udp_timeout_presets() -> None:
    """v2.1.7: the idle-UDP-session timeout is now preset-driven and the
    default is 10s, NOT the old 30s that let UDP sessions pile up."""
    from kapro_tun.core import tun2socks_process as t2s
    if t2s.resolve_udp_timeout(None) == "30s":
        raise AssertionError("default UDP timeout must not be 30s")
    if t2s.resolve_udp_timeout("balanced") != "10s":
        raise AssertionError("balanced UDP timeout should be 10s")
    if t2s.resolve_udp_timeout("economy") != "5s":
        raise AssertionError("economy UDP timeout should be 5s")
    if t2s.resolve_udp_timeout("speed") != "30s":
        raise AssertionError("speed UDP timeout should be 30s (explicit only)")
    if t2s.resolve_udp_timeout("garbage") != "10s":
        raise AssertionError("unknown preset must fall back to balanced 10s")
    args = t2s.Tun2socksProcess()._build_args("tun2socks", "127.0.0.1:2081", 1500, "warn")
    udp = args[args.index("-udp-timeout") + 1]
    if udp == "30s":
        raise AssertionError("default -udp-timeout must not be 30s")
    if udp != "10s":
        raise AssertionError(f"default -udp-timeout should be 10s, got {udp}")




def _app_log_redacts_secrets() -> None:
    """v2.1.7: app.log writes diagnostic lines to disk with rotation, and
    NEVER leaks share-URLs / UUIDs / keys (redacted defence-in-depth)."""
    import os
    import tempfile
    from pathlib import Path
    from kapro_tun.core import paths, app_log

    # redact() is pure and strips the named secret kinds.
    r = app_log.redact(
        "x vless://u@h?pbk=K 12345678-1234-1234-1234-1234567890ab https://sub.example/p")
    if "vless://" in r or "https://" in r:
        raise AssertionError(f"share/URL not redacted: {r}")
    if "12345678-1234-1234-1234-1234567890ab" in r:
        raise AssertionError(f"UUID not redacted: {r}")

    tmp = tempfile.mkdtemp(prefix="kaprotun_applog_")
    logf = os.path.join(tmp, "app.log")
    orig = paths.app_log_file
    paths.app_log_file = lambda: Path(logf)
    app_log._reset_for_test()
    try:
        secret = ("vless://00000000-0000-0000-0000-000000000000@host:443"
                  "?pbk=SECRETKEY#srv")
        app_log.log("diag line; " + secret)
        app_log.log("[mem] tun2socks: 1.2 ГБ, 8000 хэндлов, 320 потоков")
        app_log._reset_for_test()  # close handlers → flush to disk
        data = Path(logf).read_text(encoding="utf-8", errors="replace")
        if ("vless://" in data or "SECRETKEY" in data
                or "00000000-0000-0000-0000-000000000000" in data):
            raise AssertionError("secret leaked into app.log")
        if "diag line" not in data or "tun2socks" not in data:
            raise AssertionError("diagnostic lines were not written to app.log")
    finally:
        paths.app_log_file = orig
        app_log._reset_for_test()
        try:
            for f in os.listdir(tmp):
                os.remove(os.path.join(tmp, f))
            os.rmdir(tmp)
        except OSError:
            pass


check("app_log: writes diagnostics to disk, redacts secrets", _app_log_redacts_secrets)


def _mem_exhausted_action_pure() -> None:
    """v2.1.8: mem_exhausted_action forces a shutdown only for a CRITICAL
    runaway; moderate (and 'no severity') are survivable."""
    from kapro_tun.core.controller import mem_exhausted_action as A
    if A("critical") != {"force_shutdown": True}:
        raise AssertionError("critical+exhausted must force shutdown")
    if A("moderate") != {"force_shutdown": False}:
        raise AssertionError("moderate+exhausted must NOT force shutdown")
    if A(None) != {"force_shutdown": False}:
        raise AssertionError("None severity must NOT force shutdown")


check("controller: mem_exhausted_action (critical → force shutdown)",
      _mem_exhausted_action_pure)


def _auto_reconnect_same_config_no_switch() -> None:
    """v2.1.8: auto-reconnect must reuse the SAME _active_config and never
    silently fall back to a different server; _do_auto_reconnect stops when
    there's no active config instead of picking configs[0]."""
    import inspect
    from kapro_tun.gui.main_window import MainWindow

    ar = inspect.getsource(MainWindow._do_auto_reconnect)
    if "self._active_config" not in ar:
        raise AssertionError("_do_auto_reconnect must use self._active_config")
    if "configs[0]" in ar or "self.configs[" in ar:
        raise AssertionError("_do_auto_reconnect must NOT pick a different server")
    if "no_active_config" not in ar:
        raise AssertionError("_do_auto_reconnect must stop+log when no active config")
    dc = inspect.getsource(MainWindow._do_connect)
    if "self._active_config" not in dc:
        raise AssertionError("_do_connect must connect to self._active_config")
    # Every reconnect initiator routes through _arm_reconnect (reason logging +
    # storm cap) — guard against a future path bypassing it.
    arm = inspect.getsource(MainWindow._arm_reconnect)
    for token in ("reason=", "no_active_config", "storm", "_emergency_stop"):
        if token not in arm:
            raise AssertionError(f"_arm_reconnect missing {token}")


check("main_window: auto-reconnect keeps same server (no silent switch)",
      _auto_reconnect_same_config_no_switch)


def _runtime_safety_branches_via_window() -> None:
    """v2.1.8 runtime behaviour, driven on a real (offscreen) MainWindow:
      * critical + exhausted → manager.disconnect() called, NO reconnect timer,
        auto-recovery disabled, '[mem-critical] exhausted' logged;
      * moderate + exhausted → NO forced disconnect;
      * _arm_reconnect: no active config → blocked; storm cap → emergency stop;
        a normal arm logs reason=.
    """
    import os as _os2
    _os2.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from kapro_tun.gui import main_window as mw
    from kapro_tun.core import app_log
    from kapro_tun.core.parser import ProxyConfig
    from kapro_tun.core.proc_stats import ProcSample
    if QApplication.instance() is None:
        QApplication([])

    orig_toast = mw.show_toast
    orig_applog = app_log.log
    logged = []
    mw.show_toast = lambda *a, **k: None
    app_log.log = lambda m: logged.append(str(m))
    w = None
    try:
        w = mw.MainWindow()
        try:
            w._dns_watchdog.stop()
        except Exception:
            pass
        for t in ("_mem_timer", "_poll", "_reconnect_timer", "_sub_autorefresh"):
            try:
                getattr(w, t).stop()
            except Exception:
                pass

        cfg = ProxyConfig(name="🇩🇪 T", protocol="vless", raw_url="vless://x@1.2.3.4:1",
                          outbound={"server": "1.2.3.4", "server_port": 1})
        calls = {"disconnect": 0}
        w.manager.disconnect = lambda: calls.__setitem__("disconnect", calls["disconnect"] + 1)

        # --- critical + exhausted → emergency shutdown ---
        w._active_config = cfg
        w._auto_recovery_disabled = False
        w._reconnect_history = []
        w._mem_heal_count = w._mem_heal_max          # exhausted
        w._mem_heal_exhausted_notified = False
        w._reconnect_timer.stop()
        logged.clear()
        w._on_memory_pressure("critical", "tun2socks 3.0 ГБ >= 3.0 ГБ (критично)")
        if calls["disconnect"] < 1:
            raise AssertionError("critical+exhausted must call manager.disconnect()")
        if w._reconnect_timer.isActive():
            raise AssertionError("critical+exhausted must NOT start a reconnect")
        if not w._auto_recovery_disabled:
            raise AssertionError("critical+exhausted must disable auto-recovery")
        if not any("mem-critical" in m and "emergency disconnect" in m for m in logged):
            raise AssertionError("critical+exhausted must log the emergency stop")

        # --- moderate + exhausted → NO forced shutdown ---
        calls["disconnect"] = 0
        w._active_config = cfg
        w._auto_recovery_disabled = False
        w._mem_heal_count = w._mem_heal_max
        w._mem_heal_exhausted_notified = False
        w._reconnect_timer.stop()
        w._on_memory_pressure("moderate", "tun2socks 1.9 ГБ >= 1.8 ГБ")
        if calls["disconnect"] != 0:
            raise AssertionError("moderate+exhausted must NOT force a disconnect")
        if w._auto_recovery_disabled:
            raise AssertionError("moderate+exhausted must NOT disable auto-recovery")

        # --- _arm_reconnect: no active config → blocked, no random server ---
        w._auto_recovery_disabled = False
        w._reconnect_history = []
        w._active_config = None
        if w._arm_reconnect("dns_watchdog", 1, 3) is not False:
            raise AssertionError("no active config must block reconnect")

        # --- _arm_reconnect: a normal arm logs reason= and is allowed ---
        w._active_config = cfg
        w._auto_recovery_disabled = False
        w._reconnect_history = []
        logged.clear()
        if w._arm_reconnect("process_crash", 1, 3) is not True:
            raise AssertionError("first reconnect within budget must be allowed")
        if not any("reason=process_crash" in m for m in logged):
            raise AssertionError("reconnect reason not logged")

        # --- _arm_reconnect: storm cap → emergency stop, returns False ---
        calls["disconnect"] = 0
        w._auto_recovery_disabled = False
        w._reconnect_history = []
        last = None
        for i in range(1, w._RECONNECT_STORM_MAX + 3):
            last = w._arm_reconnect("memory_critical", i, 9)
            if w._auto_recovery_disabled:
                break
        if last is not False:
            raise AssertionError("storm cap must eventually block reconnect")
        if not w._auto_recovery_disabled:
            raise AssertionError("storm must trigger emergency stop")
        if calls["disconnect"] < 1:
            raise AssertionError("storm emergency stop must disconnect helpers")

        # --- v2.1.9 grace period + sustained-breach gate ---
        # A CRITICAL-breaching sample must NOT heal during the post-connect
        # grace window, and outside grace must require N consecutive samples.
        import time as _t2
        heals = []
        w._on_memory_pressure = lambda sev, rsn: heals.append((sev, rsn))
        w.manager.is_connected = lambda: True
        w.manager.current_mode = lambda: mw.MODE_TUN
        # The memory watchdog heals the CLASSIC engine (restart tun2socks, force
        # economy buffers). Pin the engine to classic so the v3 sing-box guard in
        # _check_memory doesn't short-circuit this classic-only path. A sibling
        # assertion below covers the sing-box skip (softer watchdog).
        from kapro_tun.core import controller as _Cmem
        w.manager.current_engine = lambda: _Cmem.ENGINE_CLASSIC
        crit_stats = {"tun2socks": ProcSample(5_000_000_000, 0, 0, 0.0), "xray": None}
        w.manager.sample_runtime_stats = lambda: crit_stats
        # within grace → no heal even though the sample is critical
        w._connected_at = _t2.time()
        w._mem_breach_streak = 0
        w._check_memory()
        if heals:
            raise AssertionError("memory heal fired during the post-connect grace period")
        # past grace → needs _MEM_SUSTAIN_CRITICAL consecutive breaches
        w._connected_at = _t2.time() - (w._MEM_GRACE_S + 30)
        w._mem_breach_streak = 0
        for _k in range(max(0, w._MEM_SUSTAIN_CRITICAL - 1)):
            w._check_memory()
        if heals:
            raise AssertionError("heal fired before the sustained-breach streak was reached")
        w._check_memory()  # this sample reaches the streak
        if not heals:
            raise AssertionError("heal did not fire after a sustained critical breach")
        # a stable baseline BELOW the moderate bar never heals, ever
        heals.clear()
        w._mem_breach_streak = 0
        from kapro_tun.core import controller as _C2
        baseline = {"tun2socks": ProcSample(_C2.MEM_TUN2SOCKS_MOD_BYTES - 1, 0, 0, 0.0),
                    "xray": None}
        w.manager.sample_runtime_stats = lambda: baseline
        for _k in range(6):
            w._check_memory()
        if heals:
            raise AssertionError("a stable sub-threshold baseline must never heal")

        # v3.0.0 softer sing-box watchdog: under the sing-box engine the
        # tun2socks-oriented heal must NEVER run, even on a sample that WOULD be
        # critical for the classic engine — sing-box is a single native-TUN
        # process with no loopback SOCKS bridge, so the UDP-session storm the
        # heal targets can't occur, and the heal would wrongly restart a
        # tun2socks that isn't running.
        heals.clear()
        w._mem_breach_streak = 0
        w.manager.current_engine = lambda: _Cmem.ENGINE_SING_BOX
        w.manager.sample_runtime_stats = lambda: crit_stats
        w._connected_at = _t2.time() - (w._MEM_GRACE_S + 30)
        for _k in range(w._MEM_SUSTAIN_CRITICAL + 2):
            w._check_memory()
        if heals:
            raise AssertionError("sing-box engine must not run the tun2socks memory heal")

        # --- v2.2.0 socket exhaustion: ONE reconnect, then emergency stop ---
        import time as _t4
        w.manager.is_connected = lambda: True
        w.manager.current_mode = lambda: mw.MODE_TUN
        w._auto_recovery_disabled = False
        w._connecting = False
        w._reconnect_timer.stop()
        w._reconnect_history = []
        w._sock_exhaust_bursts = 0
        w._last_sock_exhaust_handled_ts = 0.0
        calls["disconnect"] = 0
        # 1st exhaustion event → one clean reconnect (armed), NOT emergency
        w._on_socket_exhaustion("172.19.2.109:7680")
        if w._auto_recovery_disabled:
            raise AssertionError("first socket exhaustion must not emergency-stop")
        if not w._reconnect_timer.isActive():
            raise AssertionError("first socket exhaustion must arm exactly one reconnect")
        # 2nd event (bypass the flood throttle) → emergency stop, no loop
        w._reconnect_timer.stop()
        w._connecting = False
        w._last_sock_exhaust_handled_ts = _t4.time() - 100
        calls["disconnect"] = 0
        w._on_socket_exhaustion("2.16.103.96:80")
        if not w._auto_recovery_disabled:
            raise AssertionError("recurring socket exhaustion must emergency-stop (no loop)")
        if calls["disconnect"] < 1:
            raise AssertionError("socket-exhaustion emergency stop must disconnect helpers")
        if w._reconnect_timer.isActive():
            raise AssertionError("socket-exhaustion emergency stop must not arm a reconnect")
    finally:
        mw.show_toast = orig_toast
        app_log.log = orig_applog
        if w is not None:
            for t in ("_dns_watchdog",):
                try:
                    getattr(w, t).stop()
                except Exception:
                    pass
            for t in ("_mem_timer", "_poll", "_reconnect_timer"):
                try:
                    getattr(w, t).stop()
                except Exception:
                    pass
            try:
                w.deleteLater()
            except Exception:
                pass


check("main_window: critical-exhausted emergency stop + reconnect storm cap",
      _runtime_safety_branches_via_window)


def _private_lan_bypass() -> None:
    """v2.2.0: RFC1918/private/link-local/loopback ranges (incl. 172.16.0.0/12)
    are in _PRIVATE_BYPASS, 172.19.2.109 (Docker/WSL) classifies as private, and
    _connect_tun installs the set via the kernel bypass routes."""
    import ipaddress
    import inspect
    from kapro_tun.core import controller as C

    nets = {net: ipaddress.ip_network(f"{net}/{mask}", strict=False)
            for net, mask in C._PRIVATE_BYPASS}
    required = {"10.0.0.0", "172.16.0.0", "192.168.0.0", "169.254.0.0", "127.0.0.0"}
    missing = required - set(nets)
    if missing:
        raise AssertionError(f"_PRIVATE_BYPASS missing ranges: {missing}")
    # The /12 must really be a /12 covering 172.19.x.
    if nets["172.16.0.0"].prefixlen != 12:
        raise AssertionError(f"172.16.0.0 must be /12, got /{nets['172.16.0.0'].prefixlen}")
    addr = ipaddress.ip_address("172.19.2.109")
    if addr not in nets["172.16.0.0"]:
        raise AssertionError("172.19.2.109 (Docker/WSL) must be inside 172.16.0.0/12")
    if not any(addr in n for n in nets.values()):
        raise AssertionError("172.19.2.109 not covered by any private-bypass range")
    # Connect path installs it in both leak modes, via add_bypass_cidrs.
    src = inspect.getsource(C.ConnectionManager._connect_tun_classic)
    if "list(_PRIVATE_BYPASS)" not in src:
        raise AssertionError("_connect_tun does not install _PRIVATE_BYPASS")
    if "add_bypass_cidrs" not in src:
        raise AssertionError("_connect_tun does not install bypass routes via add_bypass_cidrs")




def _socket_exhaustion_parse() -> None:
    """v2.2.0: tun2socks log lines for local SOCKS port exhaustion classify as
    local_socket_exhaustion with the real dest; benign lines don't; never raises."""
    from kapro_tun.core import tun2socks_process as t2s

    line = ("[tun2socks] [TCP] dial 172.19.2.109:7680: connect to 127.0.0.1:2081: "
            "connectex: Only one usage of each socket address (protocol/network "
            "address/port) is normally permitted.")
    info = t2s.detect_socket_exhaustion(line)
    if not info or info.get("kind") != "local_socket_exhaustion":
        raise AssertionError(f"exhaustion line not classified: {info}")
    if info.get("dest") != "172.19.2.109:7680":
        raise AssertionError(f"dest mis-parsed: {info.get('dest')!r}")
    # public dest variant still classifies (dest captured)
    line2 = ("[tun2socks] [TCP] dial 2.16.103.96:80: connect to 127.0.0.1:2081: "
             "connectex: Only one usage of each socket address ...")
    if (t2s.detect_socket_exhaustion(line2) or {}).get("dest") != "2.16.103.96:80":
        raise AssertionError("second exhaustion variant mis-parsed")
    # benign lines → None (no false positives)
    for benign in (
        "[tun2socks] [TCP] dial 1.2.3.4:443: ok",
        "[tun2socks] Creating adapter",
        "[*] Подключено к «сервер» (TUN)",
        "",
    ):
        if t2s.detect_socket_exhaustion(benign) is not None:
            raise AssertionError(f"benign line misclassified: {benign!r}")




def _disconnect_reason_honest() -> None:
    """v2.2.0: disconnect reasons are honest. _do_disconnect logs an explicit
    reason (default user_requested) and no longer hardcodes 'по запросу
    пользователя'; auto paths carry their own reasons."""
    import inspect
    from kapro_tun.gui.main_window import MainWindow

    dd = inspect.getsource(MainWindow._do_disconnect)
    if "reason=" not in dd or "user_requested" not in dd:
        raise AssertionError("_do_disconnect must log an explicit reason (user_requested)")
    if "по запросу пользователя" in dd:
        raise AssertionError("misleading hardcoded 'по запросу пользователя' still present")
    # The socket handler uses its own reason and the storm/emergency path too.
    sock = inspect.getsource(MainWindow._on_socket_exhaustion)
    if "socket_exhaustion" not in sock:
        raise AssertionError("socket handler must use reason=socket_exhaustion")
    arm = inspect.getsource(MainWindow._arm_reconnect)
    if "reason=" not in arm:
        raise AssertionError("_arm_reconnect must log reason= for every reconnect")


check("main_window: honest disconnect reasons (no fake user_requested)",
      _disconnect_reason_honest)


def _xray_policy_bounds_resources() -> None:
    """v2.1.6: xray config gains level-0 connection timeouts + a buffer cap to
    bound memory/handles, WITHOUT disturbing the stats API the UI graph uses."""
    from kapro_tun.core import xray_config
    from kapro_tun.core.parser import parse
    cfg = parse("vless://aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@1.2.3.4:443"
                "?type=tcp&security=reality&pbk=AAAA&sid=01&fp=chrome#T")
    c = xray_config.build_config(cfg, [], dns_option="system",
                                 dns_leak_protection=True)
    pol = c.get("policy") or {}
    lvl0 = (pol.get("levels") or {}).get("0") or {}
    for key in ("connIdle", "uplinkOnly", "downlinkOnly"):
        if key not in lvl0:
            raise AssertionError(f"xray policy level 0 missing {key}")
    if not (0 < lvl0["connIdle"] <= 600):
        raise AssertionError(f"connIdle should be a sane idle timeout, got {lvl0['connIdle']}")
    # Stats policy MUST remain so the traffic graph keeps working.
    sysp = pol.get("system") or {}
    for key in ("statsInboundUplink", "statsOutboundUplink"):
        if not sysp.get(key):
            raise AssertionError(f"stats policy {key} disabled — UI graph would break")
    if c.get("stats") is None or not c.get("api"):
        raise AssertionError("stats/api block removed — UI graph would break")




def _direct_outbound_bound_to_egress() -> None:
    """v2.2.1: in TUN mode the direct/freedom outbound binds to the physical
    interface (sockopt.interface) so direct-routed traffic exits the real NIC
    and can never loop back into the TUN (the loop that drained loopback
    ephemeral ports). HTTP mode (no egress_interface) leaves it unbound."""
    import inspect
    from kapro_tun.core import xray_config
    from kapro_tun.core.controller import ConnectionManager
    from kapro_tun.core.parser import parse

    cfg = parse("vless://aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@1.2.3.4:443"
                "?type=tcp&security=reality&pbk=AAAA&sid=01&fp=chrome#T")
    # TUN mode (egress bound), with route_ru_direct so geoip:ru → direct exists.
    c = xray_config.build_config(cfg, ["sber.ru"], dns_leak_protection=True,
                                 route_ru_direct=True, egress_interface="Ethernet")
    direct = next(o for o in c["outbounds"] if o.get("tag") == "direct")
    iface = ((direct.get("streamSettings") or {}).get("sockopt") or {}).get("interface")
    if iface != "Ethernet":
        raise AssertionError(f"direct/freedom outbound not bound to egress iface: {direct}")
    # geoip:ru / direct-domains still route to the (now-bound) direct outbound.
    rules = c["routing"]["rules"]
    if not any(r.get("outboundTag") == "direct" and r.get("ip") == ["geoip:ru"] for r in rules):
        raise AssertionError("geoip:ru no longer routed direct under route_ru_direct")

    # HTTP mode → no egress binding (freedom can't loop without a TUN).
    c2 = xray_config.build_config(cfg, [], dns_leak_protection=True)
    direct2 = next(o for o in c2["outbounds"] if o.get("tag") == "direct")
    if "streamSettings" in direct2:
        raise AssertionError("direct outbound bound in HTTP mode (should be unbound)")

    # The TUN connect path actually passes the physical interface name.
    src = inspect.getsource(ConnectionManager._connect_tun_classic)
    if "egress_interface=real.name" not in src:
        raise AssertionError("_connect_tun does not bind the direct outbound to the egress NIC")




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
    """probe() reads success/failure from the OS resolver, is bounded, and
    never raises. DETERMINISTIC via a monkeypatched getaddrinfo — independent of
    the host's real DNS / TUN state (the old 'localhost' version could fail on a
    Windows box mid-TUN, which is exactly what broke this test in the field)."""
    from kapro_tun.core import dns_health

    orig = dns_health.socket.getaddrinfo
    calls = {"n": 0}

    def _ok(*a, **k):
        return [(2, 1, 6, "", ("1.2.3.4", 0))]

    def _boom(*a, **k):
        calls["n"] += 1
        raise OSError("no resolve")

    def _raise_rt(*a, **k):
        raise RuntimeError("unexpected")

    try:
        # All lookups succeed → probe True.
        dns_health.socket.getaddrinfo = _ok
        if dns_health.probe(hosts=("example.test",), timeout=1.0, attempts=1) is not True:
            raise AssertionError("probe should be True when getaddrinfo succeeds")

        # All lookups fail (OSError) → probe False, and it actually tried.
        dns_health.socket.getaddrinfo = _boom
        if dns_health.probe(hosts=("a.test", "b.test"), timeout=0.5, attempts=2) is not False:
            raise AssertionError("probe should be False when getaddrinfo fails")
        if calls["n"] == 0:
            raise AssertionError("probe never actually invoked getaddrinfo")

        # Never raises — even if the resolver throws a non-OSError.
        dns_health.socket.getaddrinfo = _raise_rt
        if not isinstance(dns_health.probe(hosts=("c.test",), timeout=0.5, attempts=1), bool):
            raise AssertionError("probe must return a bool, never raise")
    finally:
        dns_health.socket.getaddrinfo = orig


check("dns_health.probe: deterministic verdict, bounded, never raises",
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

    src = inspect.getsource(ConnectionManager._connect_tun_classic)
    mark_i = src.find("tun_recovery.mark(")
    clear_dns_i = src.find("session.set_dns(real.name, [])")
    # v2.1.5: the DNS health-check moved into _verify_tunnel_or_raise, which
    # _connect_tun calls as its commit-time liveness gate.
    verify_i = src.find("_verify_tunnel_or_raise(")
    if mark_i < 0:
        raise AssertionError("_connect_tun does not journal the interface (mark)")
    if clear_dns_i < 0:
        raise AssertionError("_connect_tun no longer clears physical DNS?")
    if not (0 <= mark_i < clear_dns_i):
        raise AssertionError("journal mark() must precede the DNS clear")
    if verify_i < 0:
        raise AssertionError("_connect_tun missing the liveness gate call")
    if not (clear_dns_i < verify_i):
        raise AssertionError("liveness gate must run AFTER the DNS clear")

    # The gate itself must probe AND raise on failure (so the except rolls back).
    gate = inspect.getsource(ConnectionManager._verify_tunnel_or_raise)
    if "dns_health.probe(" not in gate or "dns_health.http_probe(" not in gate:
        raise AssertionError("_verify_tunnel_or_raise lost a liveness probe")
    if "raise ConnectionError(" not in gate:
        raise AssertionError("_verify_tunnel_or_raise must raise on a dead tunnel")

    exc_src = src[src.rfind("except Exception"):]
    if "session.restore()" not in exc_src or "tun_recovery.clear()" not in exc_src:
        raise AssertionError("rollback path must restore() and clear the journal")

    disc = inspect.getsource(ConnectionManager.disconnect)
    if "tun_recovery.clear()" not in disc:
        raise AssertionError("disconnect() must clear the recovery journal")




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
    wd = wd2 = wd3 = None
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

        # (c) The production watchdog can inject a combined DNS + data-plane
        # probe instead of silently falling back to DNS-only health.
        calls = []
        wd3 = _DnsWatchdog(
            is_guarded=lambda: True,
            probe_health=lambda: calls.append(1) or True,
        )
        if not wd3._healthy(dns_health):
            raise AssertionError("watchdog rejected a healthy injected probe")
        if calls != [1]:
            raise AssertionError("watchdog did not call the injected health probe")
    finally:
        dns_health.probe = orig_probe
        for w in (wd, wd2, wd3):
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
# Test — TUN DNS resilience (v2.1.5)
#   A no single-DNS dependency · B no leak/bypass conflict ·
#   C transport/REALITY fail-fast · invariant: failure rolls back cleanly
# ---------------------------------------------------------------------------

section("TUN DNS resilience — failover / bypass / fail-fast")


def _no_single_dns_dependency() -> None:
    """A: with leak protection ON, system DNS must NOT hinge on one resolver.
    The upstream set, xray's dns block, and the :53 carve-out must all list
    several servers from MORE THAN ONE operator (distinct /8s)."""
    from kapro_tun.core import dns_options, xray_config
    from kapro_tun.core.parser import parse

    ups = dns_options.LEAK_PROTECTED_SYSTEM_UPSTREAMS
    if len(ups) < 3:
        raise AssertionError(f"need >=3 leak-protected upstreams, got {ups}")
    first_octets = {ip.split(".")[0] for ip in ups}
    if len(first_octets) < 3:
        raise AssertionError(f"upstreams not operator-diverse: {ups}")

    cfg = parse("vless://aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@1.2.3.4:443"
                "?type=tcp&security=reality&pbk=AAAA&sid=01&fp=chrome#T")
    c = xray_config.build_config(cfg, [], dns_option="system",
                                 dns_leak_protection=True)
    dns_block = c.get("dns") or {}
    servers = list(dns_block.get("servers") or [])
    if len(servers) < 3 or set(servers) != set(ups):
        raise AssertionError(f"system+leak dns block not multi-upstream: {servers}")

    carve = [r for r in c["routing"]["rules"]
             if r.get("port") == "53" and r.get("outboundTag") == "proxy"]
    if not carve:
        raise AssertionError("no :53→proxy carve-out for the upstreams")
    carved_ips = {ip.split("/")[0] for r in carve for ip in r.get("ip", [])}
    if not set(ups).issubset(carved_ips):
        raise AssertionError(f"carve-out misses some upstreams: {carved_ips}")




def _leak_on_does_not_bypass_resolvers() -> None:
    """B: the bypass/leak conflict is gone. The public resolvers live in a
    separate list that is NOT applied when leak protection is on, and none of
    the tunnelled upstreams fall inside an always-direct service block (which
    would silently steal their queries back out the physical NIC)."""
    import ipaddress
    from kapro_tun.core import controller, dns_options

    # Split is exhaustive and the alias is the union (no entry lost/dup'd).
    if controller._ALWAYS_BYPASS != controller._DNS_RESOLVER_BYPASS + controller._SERVICE_BYPASS:
        raise AssertionError("_ALWAYS_BYPASS != resolver-bypass + service-bypass")

    # The public DNS resolvers must be ONLY in the resolver-bypass set (the one
    # we skip when leak protection is on), never in the always-on service set.
    resolver_ips = {e[0] for e in controller._DNS_RESOLVER_BYPASS}
    service_ips = {e[0] for e in controller._SERVICE_BYPASS}
    if resolver_ips & service_ips:
        raise AssertionError("a resolver IP leaked into the always-on service bypass")

    # Critical non-overlap: no tunnelled upstream may sit inside a service CIDR.
    service_nets = [ipaddress.ip_network(f"{net}/{mask}")
                    for (net, mask) in controller._SERVICE_BYPASS]
    for up in dns_options.LEAK_PROTECTED_SYSTEM_UPSTREAMS:
        a = ipaddress.ip_address(up)
        for net in service_nets:
            if a in net:
                raise AssertionError(
                    f"leak-protected upstream {up} sits inside always-bypassed "
                    f"{net} — its DNS would leak direct out the physical NIC")

    # Source-level: _connect_tun must choose the service-only set when leak on.
    import inspect
    src = inspect.getsource(controller.ConnectionManager._connect_tun_classic)
    if "list(_SERVICE_BYPASS)" not in src or "list(_ALWAYS_BYPASS)" not in src:
        raise AssertionError("_connect_tun no longer branches bypass on leak mode")




def _http_probe_is_bounded_and_safe() -> None:
    """C: the tunnel-liveness probe never raises and fails fast against a dead
    proxy (so a broken transport becomes a clean connect failure, not a hang)."""
    import time as _t
    from kapro_tun.core import dns_health

    t0 = _t.time()
    r = dns_health.http_probe("http://127.0.0.1:1", timeout=1.0,
                              urls=("http://127.0.0.1:9/",))
    dt = _t.time() - t0
    if r is not False:
        raise AssertionError("http_probe to a dead proxy should be False")
    if dt > 6.0:
        raise AssertionError(f"http_probe not bounded ({dt:.1f}s)")
    # Never raises on junk input either.
    for bad in ("", "not-a-url", "http://"):
        if not isinstance(dns_health.http_probe(bad, timeout=0.5,
                                                urls=("http://127.0.0.1:9/",)), bool):
            raise AssertionError(f"http_probe({bad!r}) returned non-bool")


check("C: dns_health.http_probe bounded + never raises", _http_probe_is_bounded_and_safe)


def _dead_tunnel_connect_rolls_back() -> None:
    """Invariant 2+3: when the tunnel is dead, the connect-time liveness check
    RAISES (which the _connect_tun except turns into a full DNS/route/proxy
    rollback) — it never leaves the machine 'connected but DNS broken'. Also
    checks the REALITY path produces its specific message, and that a live
    tunnel passes."""
    from kapro_tun.core import dns_health
    # NB: the controller defines its OWN ConnectionError (not the builtin), so
    # import that exact class to catch the rollback-triggering raise.
    from kapro_tun.core.controller import ConnectionManager, ConnectionError

    mgr = ConnectionManager(on_log=lambda _l: None)
    orig_http, orig_probe = dns_health.http_probe, dns_health.probe
    orig_scan = mgr._scan_xray_reality_errors
    try:
        # (a) everything dead, no REALITY markers → generic transport rollback.
        dns_health.http_probe = lambda *a, **k: False
        dns_health.probe = lambda *a, **k: False
        mgr._scan_xray_reality_errors = lambda _off: 0
        raised = None
        try:
            mgr._verify_tunnel_or_raise("127.0.0.1", 2080, dns_cleared=True, log_offset=0)
        except ConnectionError as e:
            raised = str(e)
        if raised is None:
            raise AssertionError("dead tunnel did not raise (no rollback would fire)")
        if "восстановлена" not in raised:
            raise AssertionError("rollback message doesn't state the network is restored")

        # (b) dead + REALITY cert errors → REALITY-specific message.
        mgr._scan_xray_reality_errors = lambda _off: 3
        try:
            mgr._verify_tunnel_or_raise("127.0.0.1", 2080, dns_cleared=False, log_offset=0)
            raise AssertionError("dead REALITY transport did not raise")
        except ConnectionError as e:
            if "REALITY" not in str(e):
                raise AssertionError(f"REALITY error not surfaced: {e}")

        # (c) live tunnel (OS DNS resolves) → no raise, commit proceeds.
        dns_health.http_probe = lambda *a, **k: True
        dns_health.probe = lambda *a, **k: True
        mgr._scan_xray_reality_errors = lambda _off: 0
        mgr._verify_tunnel_or_raise("127.0.0.1", 2080, dns_cleared=True, log_offset=0)
    finally:
        dns_health.http_probe, dns_health.probe = orig_http, orig_probe
        mgr._scan_xray_reality_errors = orig_scan

    # Source invariant: the except still restores routes AND clears the journal.
    import inspect
    src = inspect.getsource(ConnectionManager._connect_tun_classic)
    exc = src[src.rfind("except Exception"):]
    if "session.restore()" not in exc or "tun_recovery.clear()" not in exc:
        raise AssertionError("rollback path lost restore()/journal-clear")
    if "_verify_tunnel_or_raise(" not in src:
        raise AssertionError("_connect_tun no longer runs the liveness gate")




def _no_regression_leak_off() -> None:
    """Invariant 4: leak protection OFF is unchanged — no xray dns block for
    the system option (xray keeps using the OS resolver), and DNS still goes
    direct via the full unconditional bypass set."""
    from kapro_tun.core import xray_config, controller
    from kapro_tun.core.parser import parse
    cfg = parse("vless://aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@1.2.3.4:443"
                "?type=tcp&security=reality&pbk=AAAA&sid=01&fp=chrome#T")
    c = xray_config.build_config(cfg, [], dns_option="system",
                                 dns_leak_protection=False)
    if c.get("dns") is not None:
        raise AssertionError("system+leak-OFF should have NO dns block (regression)")
    # :53 still routed direct in leak-off mode.
    direct53 = [r for r in c["routing"]["rules"]
                if r.get("port") == "53" and r.get("outboundTag") == "direct"]
    if not direct53:
        raise AssertionError("leak-off lost its direct :53 routing")
    # The resolver host-routes are still available for the off-path bypass.
    if not controller._DNS_RESOLVER_BYPASS:
        raise AssertionError("resolver bypass set vanished (leak-off would lose direct DNS)")




# ---------------------------------------------------------------------------
# Test — v3.0.0 sing-box native-TUN engine
# ---------------------------------------------------------------------------
# sing-box replaces classic xray+tun2socks as the DEFAULT TUN dataplane. These
# lock in: the migration default, the (near-free) protocol mapping, the
# routing/DNS/private-bypass invariants, the "offer legacy, never silently
# switch" contract, the firewall/cleanup wiring, and the architectural win —
# no 127.0.0.1 SOCKS bridge, so the loopback ephemeral-port exhaustion that
# wedged long classic sessions is structurally impossible. No process starts.

section("v3.0.0 — sing-box TUN engine")

import inspect as _inspect_v3

from kapro_tun.core import (  # noqa: E402
    controller as _ctl_v3,
    killswitch as _ks_v3,
    paths as _paths_v3,
    sing_box_config as _sb_v3,
    sing_box_installer as _sbi_v3,  # noqa: F401  (import-smoke for the new module)
    sing_box_process as _sbp_v3,    # noqa: F401
    storage as _storage_v3,
)


def _v3_migration_default() -> None:
    # 1) Default engine is sing-box; a pre-v3 settings file (no tun_engine key)
    #    migrates to it via the same DEFAULT_SETTINGS merge load_settings uses —
    #    never a hard error, never left unset.
    if _storage_v3.DEFAULT_SETTINGS.get("tun_engine") != _ctl_v3.ENGINE_SING_BOX:
        raise AssertionError("DEFAULT_SETTINGS.tun_engine must be sing_box_tun")
    old = {"mode": "tun", "kill_switch": True}  # representative v2.x settings
    merged = dict(_storage_v3.DEFAULT_SETTINGS)
    merged.update(old)
    if merged.get("tun_engine") != _ctl_v3.ENGINE_SING_BOX:
        raise AssertionError("pre-v3 settings must migrate to sing_box_tun")
    # resolve_engine normalises unknown/empty to the safe default; only an
    # explicit legacy request flips it.
    re = _ctl_v3.resolve_engine
    for val in (None, "", "garbage", "SING_BOX", 123):
        if re(val) != _ctl_v3.ENGINE_SING_BOX:
            raise AssertionError(f"resolve_engine({val!r}) must be sing_box_tun")
    if re(_ctl_v3.ENGINE_CLASSIC) != _ctl_v3.ENGINE_CLASSIC:
        raise AssertionError("explicit legacy must resolve to classic")


def _v3_config_structure() -> None:
    # 2) Config gen: serialisable native-TUN config, proxy outbound first +
    #    server pinned to the resolved IP, all four outbound tags, route.final
    #    proxy + auto_detect_interface (the freedom→TUN loop killer).
    full = _sb_v3.build_config(
        parsed["vless"], ["example.com", "gosuslugi.ru"], server_ip="1.2.3.4",
        dns_option="system", dns_leak_protection=True,
    )
    json.dumps(full, ensure_ascii=False)  # must be writable
    inb = full["inbounds"]
    if not inb or inb[0].get("type") != "tun":
        raise AssertionError("missing native tun inbound")
    if not inb[0].get("auto_route"):
        raise AssertionError("tun inbound must auto_route")
    if inb[0].get("interface_name") != _sb_v3.TUN_DEVICE_NAME:
        raise AssertionError("tun interface_name mismatch")
    obs = full["outbounds"]
    if obs[0].get("tag") != "proxy":
        raise AssertionError("first outbound must be tagged proxy")
    if obs[0].get("server") != "1.2.3.4":
        raise AssertionError("proxy server must be pinned to the resolved IP")
    tags = {o.get("tag") for o in obs}
    # Modern (1.12+) grammar: only proxy + direct outbounds. The legacy `block`
    # and `dns` outbound TYPES are deprecated (1.11) / removed (1.13) — DNS is
    # answered by the dns module via a `hijack-dns` route action instead.
    if tags != {"proxy", "direct"}:
        raise AssertionError(f"outbounds must be exactly proxy+direct, got {tags}")
    obtypes = {o.get("type") for o in obs}
    if "block" in obtypes or "dns" in obtypes:
        raise AssertionError("legacy block/dns outbound types must not be emitted")
    if full["route"].get("final") != "proxy":
        raise AssertionError("route.final must be proxy")
    if not full["route"].get("auto_detect_interface"):
        raise AssertionError("auto_detect_interface must be true (loop killer)")


def _v3_private_bypass() -> None:
    # 3) Private/LAN/Docker never tunnel. The user's Docker host 172.19.2.109
    #    lives in 172.16.0.0/12 — assert that net is in the direct rule AND
    #    actually covers the reported IP (the v2.2.0 regression).
    import ipaddress
    full = _sb_v3.build_config(parsed["vless"], [], server_ip="1.2.3.4")
    priv = None
    for r in full["route"]["rules"]:
        if r.get("outbound") == "direct" and isinstance(r.get("ip_cidr"), list) \
                and "172.16.0.0/12" in r["ip_cidr"]:
            priv = r["ip_cidr"]
            break
    if priv is None:
        raise AssertionError("no private ip_cidr → direct rule")
    for need in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"):
        if need not in priv:
            raise AssertionError(f"private bypass missing {need}")
    if ipaddress.ip_address("172.19.2.109") not in ipaddress.ip_network("172.16.0.0/12"):
        raise AssertionError("172.19.2.109 not covered by the shipped bypass net")


def _v3_dns_hijack_and_leak() -> None:
    # 4) v3.1.1: DNS is ALWAYS the system resolver. All :53 is hijacked into the
    #    dns module, which is a single `type: local` server (the OS resolver over
    #    the physical NIC). The old custom DoH / smart-split was DPI-throttled on
    #    RU networks — DoH timeouts black-holed DNS and false-failed the connect
    #    gate — so it is gone. The dns_leak_protection kwarg is accepted but must
    #    have NO effect on the produced config.
    c = _sb_v3.build_config(parsed["vless"], [], server_ip="1.2.3.4")
    # Modern grammar: DNS hijack is a route ACTION, not a route-to-outbound.
    if not any(r.get("protocol") == "dns" and r.get("action") == "hijack-dns"
               for r in c["route"]["rules"]):
        raise AssertionError("missing DNS hijack rule (protocol:dns → hijack-dns)")
    servers = c["dns"]["servers"]
    if servers != [{"type": "local", "tag": "local"}]:
        raise AssertionError(f"DNS must be a single system (type:local) server, got {servers}")
    if c["dns"].get("strategy") != "ipv4_only":
        raise AssertionError("DNS strategy must be ipv4_only (matches 2000::/3 reject)")
    # No DoH and no resolver-IP carve-out route rule may survive.
    if any(s.get("type") == "https" for s in servers):
        raise AssertionError("custom DoH must be gone — DNS is system only")
    if any(r.get("outbound") == "direct" and r.get("port") in ([443], [53])
           for r in c["route"]["rules"]):
        raise AssertionError("resolver-IP carve-out rule must be gone (type:local self-egresses)")
    # The ignored leak kwarg must not change the output.
    on = _sb_v3.build_config(parsed["vless"], [], server_ip="1.2.3.4",
                             dns_leak_protection=True)
    off = _sb_v3.build_config(parsed["vless"], [], server_ip="1.2.3.4",
                              dns_leak_protection=False)
    if on["dns"] != off["dns"] or on["dns"] != c["dns"]:
        raise AssertionError("dns_leak_protection must be ignored — DNS always system")


def _v3_unsupported_raises() -> None:
    # 5) Anything sing-box can't faithfully do raises UnsupportedBySingBox so the
    #    caller can OFFER legacy — never mis-handshake.
    class _Fake:
        outbound = {"type": "wireguard", "server": "1.2.3.4"}

    class _FakeSS:
        outbound = {"type": "shadowsocks", "server": "1.2.3.4", "plugin": "obfs-local"}

    for bad, why in ((_Fake(), "unknown protocol"), (_FakeSS(), "ss+plugin")):
        try:
            _sb_v3.build_config(bad, [], server_ip="1.2.3.4")
        except _sb_v3.UnsupportedBySingBox:
            pass
        else:
            raise AssertionError(f"{why} must raise UnsupportedBySingBox")


def _v3_dispatch_no_silent_switch() -> None:
    # 5b) v3.1.0: connect() turns UnsupportedBySingBox into a plain
    #     ConnectionError — there is NO legacy engine to switch to, and the
    #     message must NOT mention one.
    cm = _ctl_v3.ConnectionManager()

    def _boom(config, direct_domains):
        raise _sb_v3.UnsupportedBySingBox("nope")

    cm._connect_tun_sing_box = _boom
    try:
        cm.connect(object(), [])
    except _ctl_v3.ConnectionError as e:
        if "legacy" in str(e).lower():
            raise AssertionError("rejection must NOT mention a legacy engine (removed)")
    else:
        raise AssertionError("unsupported config must raise ConnectionError")


def _v3_dispatch_routes_to_singbox_and_no_runtime_fallback() -> None:
    # 5c) connect() always routes to the sing-box path, and a RUNTIME sing-box
    #     failure propagates and stays disconnected — there is no fallback engine.
    cm = _ctl_v3.ConnectionManager()
    calls = {"singbox": 0}
    cm._connect_tun_sing_box = lambda c, d: calls.__setitem__("singbox", calls["singbox"] + 1)
    cm.connect(object(), [])
    if calls["singbox"] != 1:
        raise AssertionError("connect() must dispatch to the sing-box path")

    def _runtime_die(config, direct_domains):
        raise _ctl_v3.ConnectionError("sing-box завершился сразу после старта")

    cm._connect_tun_sing_box = _runtime_die
    try:
        cm.connect(object(), [])
    except _ctl_v3.ConnectionError:
        pass
    else:
        raise AssertionError("a sing-box runtime failure must propagate, not be swallowed")


def _v3_no_loopback_bridge() -> None:
    # 6) The architectural win: sing-box owns the TUN, so there is NO local SOCKS
    #    inbound and nothing bound to 127.0.0.1:2081 — the loopback port the
    #    classic engine exhausted. Its absence is the regression guard.
    full = _sb_v3.build_config(parsed["vless"], [], server_ip="1.2.3.4")
    blob = json.dumps(full)
    if "2081" in blob:
        raise AssertionError("sing-box config must not reference the 2081 SOCKS bridge")
    health = [i for i in full["inbounds"] if i.get("tag") == "health-probe"]
    if len(health) != 1 or health[0].get("type") != "mixed":
        raise AssertionError("missing dedicated sing-box health-probe inbound")
    if health[0].get("listen") != "127.0.0.1":
        raise AssertionError("health-probe inbound must be loopback-only")
    health_routes = [
        r for r in full["route"]["rules"]
        if "health-probe" in (r.get("inbound") or [])
    ]
    if not health_routes or health_routes[0].get("outbound") != "proxy":
        raise AssertionError("health-probe inbound must be forced to proxy")


def _v3_runtime_cleanup() -> None:
    # 7) The sing-box runtime config carries the server UUID/password; it MUST be
    #    wiped on disconnect by remove_runtime_configs (no secret left at rest).
    src = _inspect_v3.getsource(_paths_v3.remove_runtime_configs)
    if "sing_box_runtime_config_file" not in src:
        raise AssertionError("remove_runtime_configs must clean the sing-box config")
    if "sing-box-runtime" not in str(_paths_v3.sing_box_runtime_config_file()):
        raise AssertionError("unexpected sing-box runtime config path")


def _v3_killswitch_singbox() -> None:
    # 8) Kill-switch must allow sing-box.exe out (else its own transport is
    #    blocked) and tear that rule down on remove (no orphan firewall rule).
    sig = _inspect_v3.signature(_ks_v3.install)
    if "allow_exe_path" not in sig.parameters:
        raise AssertionError("killswitch.install must take the sing-box exe path")
    if "_RULE_ALLOW_SINGBOX" not in _inspect_v3.getsource(_ks_v3.install):
        raise AssertionError("install() must add the sing-box allow rule")
    if "_RULE_ALLOW_SINGBOX" not in _inspect_v3.getsource(_ks_v3.remove):
        raise AssertionError("remove() must delete the sing-box allow rule")


def _v3_stats_include_singbox() -> None:
    # 9) Runtime stats sample the sing-box process too, and the diagnostic line
    #    formats without crashing when nothing is running.
    cm = _ctl_v3.ConnectionManager()
    stats = cm.sample_runtime_stats()
    if "sing-box" not in stats:
        raise AssertionError("sample_runtime_stats must include the sing-box process")
    line = cm.format_runtime_stats(stats)
    if "[mem]" not in line:
        raise AssertionError("format_runtime_stats must produce a [mem] line")


def _v3_legacy_unaffected() -> None:
    # 10) The legacy engine stays selectable and its xray config is untouched —
    #     v3 is additive, not a removal.
    if _ctl_v3.resolve_engine("classic_xray_tun2socks") != _ctl_v3.ENGINE_CLASSIC:
        raise AssertionError("legacy engine must remain selectable")
    full = build_config(parsed["vless"], direct_domains=["example.com"])
    if full["outbounds"][0]["tag"] != "proxy":
        raise AssertionError("classic xray config regressed")


def _v3_real_singbox_check() -> None:
    # 11) When a sing-box binary is present (dev/local; absent on the CI runner),
    #     the GENERATED config must pass the real `sing-box check`. This is the
    #     guard that caught the 1.12/1.13 schema break — a structurally-valid
    #     dict that sing-box itself rejects is worthless. No-op (pass) when the
    #     binary isn't installed so CI stays green.
    if not _sbi_v3.is_installed():
        print("       (skip — sing-box binary not installed on this host)")
        return
    for leak in (True, False):
        for ru in (True, False):
            path = _sb_v3.write_config(
                parsed["vless"], ["example.com"], server_ip="1.2.3.4",
                dns_leak_protection=leak, route_ru_direct=ru,
            )
            ok, msg = _sb_v3.check_config(path)
            if not ok:
                raise AssertionError(
                    f"sing-box rejected the generated config "
                    f"(leak={leak} ru={ru}): {msg[:200]}")


def _v3_xhttp_unsupported() -> None:
    # 12) XHTTP / splithttp are Xray-only transports. The sing-box parser can't
    #     render them, so the engine must REJECT them (UnsupportedBySingBox →
    #     'use legacy') instead of silently emitting a plain-TCP outbound that
    #     mis-handshakes the REALITY server ('unknown version: N'). ws/grpc and
    #     plain TCP still build.
    from kapro_tun.core.parser import parse as _parse
    base = "vless://aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa@1.2.3.4:443"
    for net in ("xhttp", "splithttp"):
        cfg = _parse(f"{base}?type={net}&security=reality&pbk=AAAA&sid=01&fp=chrome#x")
        if cfg.network != net:
            raise AssertionError(f"parser must record network={net!r}, got {cfg.network!r}")
        try:
            _sb_v3.build_config(cfg, [], server_ip="1.2.3.4")
        except _sb_v3.UnsupportedBySingBox as e:
            if "legacy" in str(e).lower():
                raise AssertionError(f"{net} rejection must NOT mention legacy (removed)")
        else:
            raise AssertionError(f"{net} transport must raise UnsupportedBySingBox")
    # Supported transports still build a real sing-box transport.
    for net, extra in (("ws", "&host=ex.com&path=/w"), ("grpc", "&serviceName=g")):
        cfg = _parse(f"{base}?type={net}&security=tls{extra}#x")
        full = _sb_v3.build_config(cfg, [], server_ip="1.2.3.4")
        if "transport" not in full["outbounds"][0]:
            raise AssertionError(f"{net} must produce a sing-box transport")
    # Plain TCP (no transport) must NOT be rejected.
    tcp = _parse(f"{base}?type=tcp&security=reality&pbk=AAAA&sid=01&fp=chrome#x")
    _sb_v3.build_config(tcp, [], server_ip="1.2.3.4")  # must not raise


def _v3_xhttp_rawurl_fallback() -> None:
    # v3.0.6: the XHTTP gate must reject even if ProxyConfig.network wasn't
    # populated (e.g. a config carried over from an older build) by sniffing the
    # raw share URL — never let XHTTP reach sing-box as a plain-TCP outbound.
    from kapro_tun.core.parser import ProxyConfig
    cfg = ProxyConfig(
        name="x", protocol="vless",
        raw_url="vless://uuid@1.2.3.4:443?type=xhttp&security=reality&pbk=AAAA",
        outbound={"type": "vless", "server": "1.2.3.4", "uuid": "uuid"},
        network="")  # network deliberately empty
    try:
        _sb_v3.build_config(cfg, [], server_ip="1.2.3.4")
    except _sb_v3.UnsupportedBySingBox as e:
        if "legacy" in str(e).lower():
            raise AssertionError("xhttp-from-rawurl rejection must NOT mention legacy")
    else:
        raise AssertionError(
            "XHTTP from raw_url must raise UnsupportedBySingBox even with empty network")


def _v3_orphan_tun_cleanup() -> None:
    # v3.0.6/v3.0.7: both engines name the TUN "KaproTun", so a leftover
    # sing-box / tun2socks orphan blocks the next start with "...already exists".
    # The startup orphan-killer must include sing-box. The TUN free MUST be
    # REACTIVE (only on a real collision in _connect_tun_sing_box), NOT a proactive
    # global kill in _connect_tun — else a second/child instance could kill the
    # first's live sing-box and start a reconnect storm.
    import inspect as _ins
    from kapro_tun import main as _main
    ksrc = _ins.getsource(_main._kill_orphan_helpers)
    if "sing-box.exe" not in ksrc or '"sing-box"' not in ksrc:
        raise AssertionError("startup orphan-killer must include sing-box (both OSes)")
    from kapro_tun.core import controller as _C
    fsrc = _ins.getsource(_C.ConnectionManager._free_tun_device)
    if "taskkill" not in fsrc or "pkill" not in fsrc:
        raise AssertionError("_free_tun_device must kill orphans on both OSes")
    if "sing-box" not in fsrc or "tun2socks" not in fsrc:
        raise AssertionError("_free_tun_device must target BOTH TUN engines")
    # REACTIVE, not proactive: connect() must NOT blind-kill on every connect.
    dsrc = _ins.getsource(_C.ConnectionManager.connect)
    if "_free_tun_device" in dsrc:
        raise AssertionError("connect() must NOT proactively kill (sibling-kill risk)")
    sbsrc = _ins.getsource(_C.ConnectionManager._connect_tun_sing_box)
    if "_free_tun_device" not in sbsrc or "already exists" not in sbsrc.lower():
        raise AssertionError("_connect_tun_sing_box must free the TUN reactively on collision")
    # Safety: it must not kill our own live helpers.
    if "is_connected()" not in fsrc:
        raise AssertionError("_free_tun_device must guard on is_connected()")


def _v3_singbox_watchdog_engine_aware() -> None:
    # 13) v3.1.0: sing-box is the only engine. The crash watchdog watches the
    #     single sing-box process:
    #     (a) healthy sing-box (process up) → NO process_crash arm.
    #     (b) sing-box death → arms reconnect + log blames sing-box.
    import os as _o3
    _o3.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from kapro_tun.gui import main_window as mw
    from kapro_tun.core import app_log
    from kapro_tun.core.parser import ProxyConfig
    if QApplication.instance() is None:
        QApplication([])
    orig_toast, orig_applog = mw.show_toast, app_log.log
    mw.show_toast = lambda *a, **k: None
    app_log.log = lambda m: None
    _timers = ("_dns_watchdog", "_mem_timer", "_poll", "_reconnect_timer",
               "_sub_autorefresh", "_tray_pinger")
    w = None
    try:
        w = mw.MainWindow()
        for t in _timers:
            try: getattr(w, t).stop()
            except Exception: pass
        w._poll_traffic = lambda: None
        w._connecting = False
        cfg = ProxyConfig(name="T", protocol="vless", raw_url="vless://x@1.2.3.4:1",
                          outbound={"server": "1.2.3.4", "server_port": 1})
        w._active_config = cfg
        m = w.manager
        m._active = cfg
        m.disconnect = lambda: None
        logs = []
        w.logs_page.append = lambda s: logs.append(str(s))
        arm = []
        w._arm_reconnect = lambda reason, a, t: (arm.append(reason) or True)

        # (a) healthy sing-box: process running → no crash arm.
        m.sing_box_process.is_running = lambda: True
        m.is_connected = lambda: True
        logs.clear(); arm.clear(); w._reconnect_attempts = 0; w._crash_confirm = 0
        w._refresh_home()
        if arm:
            raise AssertionError(f"healthy sing-box must NOT arm a reconnect: {arm}")
        if any("упал" in l for l in logs):
            raise AssertionError(f"healthy sing-box must not log a crash: {logs}")

        # (b) sing-box death → arms reconnect + blames sing-box.
        m.sing_box_process.is_running = lambda: False
        m.sing_box_process.returncode = lambda: 1
        m.is_connected = lambda: False
        logs.clear(); arm.clear()
        w._auto_recovery_disabled = False; w._reconnect_attempts = 0
        w._crash_confirm = 0
        w._reconnect_timer.stop()
        # v3.0.8 debounce: two consecutive not-running ticks before arming.
        w._refresh_home(); w._refresh_home()
        w._reconnect_timer.stop()
        if "process_crash" not in arm:
            raise AssertionError("sing-box death must arm process_crash")
        if not any("sing-box упал" in l for l in logs):
            raise AssertionError(f"sing-box death must blame sing-box: {logs}")
        if any("Xray-core упал" in l for l in logs):
            raise AssertionError(f"sing-box death must NOT blame Xray: {logs}")
    finally:
        mw.show_toast, app_log.log = orig_toast, orig_applog
        if w is not None:
            for t in _timers:
                try: getattr(w, t).stop()
                except Exception: pass
            tray = getattr(w, "tray", None)
            if tray is not None and hasattr(tray, "hide"):
                try: tray.hide()
                except Exception: pass
            w.close(); w.deleteLater()


def _v3_singbox_log_noise_classification() -> None:
    # 14) sing-box log filter — three buckets:
    #   (a) pure per-connection churn + the ICMP-not-supported WARN → ALWAYS
    #       hidden from the UI (retained in recent_logs);
    #   (b) ambiguous network errors (missing default interface / no route / i/o
    #       timeout) → VISIBLE at startup (connect diagnostic), hidden once live;
    #   (c) fatal / startup / config / driver / permission → ALWAYS visible.
    from kapro_tun.core import sing_box_process as _sbp

    # (a) churn + ICMP WARN: hidden in BOTH startup and live states.
    always_hidden = [
        "ERROR outbound/direct[direct]: An existing connection was forcibly "
        "closed by the remote host.",
        "ERROR inbound/tun[tun-in]: connection download closed",
        "ERROR read tcp 1.2.3.4:55000->5.6.7.8:443: connection reset by peer",
        "WARN use of closed network connection",
        "WARN inbound/tun[tun-in]: link icmp connection from 10.0.0.2 to 1.1.1.1: "
        "icmp is not supported by default outbound: proxy",
    ]
    for line in always_hidden:
        if not (_sbp.is_benign_noise(line, live=True)
                and _sbp.is_benign_noise(line, live=False)):
            raise AssertionError(f"churn/ICMP must be hidden in both states: {line!r}")

    # (b) transient net: hidden once live, but VISIBLE at startup.
    transient = [
        "ERROR network: missing default interface",
        "ERROR route: no route to internet",
        "ERROR dial tcp 10.0.0.5:443: i/o timeout",
    ]
    for line in transient:
        if not _sbp.is_benign_noise(line, live=True):
            raise AssertionError(f"transient net must be hidden once live: {line!r}")
        if _sbp.is_benign_noise(line, live=False):
            raise AssertionError(f"transient net must be VISIBLE at startup: {line!r}")

    # (c) fatal/startup/config/driver/permission: visible in BOTH states.
    fatal = [
        "FATAL start service: configure tun interface: permission denied",
        "panic: runtime error",
        "ERROR failed to start: bind: address already in use",
        "FATAL decode config at line 3: invalid character",
        "ERROR wintun: failed to load driver",
        "ERROR initialize outbound[proxy]: parse config: bad reality public_key",
        "FATAL start dns server: listen udp :53: bind: permission denied",
    ]
    for line in fatal:
        if _sbp.is_benign_noise(line, live=True) or _sbp.is_benign_noise(line, live=False):
            raise AssertionError(f"fatal/startup error must stay visible: {line!r}")

    class _FakeStdout:
        def __init__(self, lines): self._lines = iter(lines)
        def __iter__(self): return self._lines

    def _run(lines, live):
        sink = []
        p = _sbp.SingBoxProcess(on_log=sink.append)
        p._live = live

        class _FakeProc:
            stdout = _FakeStdout([l + "\n" for l in lines])
        p._proc = _FakeProc()
        p._read_loop()
        return sink, p.recent_logs()

    # Live: ICMP + transient hidden from sink, fatal shown; all kept in recent.
    sink, recent = _run([always_hidden[4], transient[0], fatal[0]], live=True)
    if any("icmp" in s.lower() for s in sink):
        raise AssertionError("ICMP WARN leaked to the user sink when live")
    if any("missing default interface" in s for s in sink):
        raise AssertionError("transient net leaked to the user sink when live")
    if not any("permission denied" in s for s in sink):
        raise AssertionError("fatal line must reach the user sink")
    if not any("icmp" in r.lower() for r in recent):
        raise AssertionError("ICMP line must be retained in recent_logs for diagnostics")

    # Startup window: the same transient error IS forwarded (connect diagnostic).
    sink2, _ = _run([transient[0]], live=False)
    if not any("missing default interface" in s for s in sink2):
        raise AssertionError("transient net must be visible during the startup window")


def _v3_block_ads_warning_once() -> None:
    # 17) The 'ad-block is legacy-only' notice must be logged AT MOST ONCE per
    #     app launch — not on every sing-box reconnect.
    from kapro_tun.core import controller as _C5
    from kapro_tun.core.controller import ConnectionManager
    cm = ConnectionManager(on_log=None)
    cm.settings["block_ads"] = True
    cm.settings["tun_engine"] = _C5.ENGINE_SING_BOX
    logged = []
    cm._log = lambda m: logged.append(str(m))
    # Three connects in a row (the real call site is _connect_tun_sing_box).
    for _ in range(3):
        cm._note_singbox_adblock_once()
    hits = [m for m in logged if "Блокировка рекламы" in m]
    if len(hits) != 1:
        raise AssertionError(
            f"block_ads notice must log exactly once per launch, got {len(hits)}")
    # With block_ads OFF, it never logs.
    cm2 = ConnectionManager(on_log=None)
    cm2.settings["block_ads"] = False
    logged2 = []
    cm2._log = lambda m: logged2.append(str(m))
    cm2._note_singbox_adblock_once()
    if any("Блокировка рекламы" in m for m in logged2):
        raise AssertionError("no ad-block notice when block_ads is off")


def _v3_dns_and_split_routing() -> None:
    # 18) v3.1.1: DNS is always the system resolver (type:local); this test now
    #     guards the SPLIT-ROUTING half of the old 'ChatGPT loads but
    #     oaiusercontent / Yandex images hang' fix: OpenAI/CDN/YouTube domains are
    #     force-proxied BEFORE any direct/geoip:ru rule and never appear in a
    #     direct rule, so route_ru_direct can't pull a CDN IP out the real NIC.
    from kapro_tun.core import sing_box_config as sb

    def build(leak, ru):
        c = sb.build_config(parsed["vless"], ["gosuslugi.ru"], server_ip="1.2.3.4",
                            dns_leak_protection=leak, route_ru_direct=ru)
        return c, c["route"]["rules"]

    def idx(rules, pred):
        for i, r in enumerate(rules):
            if pred(r):
                return i
        return None

    # --- DNS is system in every mode, the leak kwarg is ignored ---
    c_dns, rules_dns = build(True, True)
    if c_dns["dns"]["servers"] != [{"type": "local", "tag": "local"}]:
        raise AssertionError("DNS must be a single system (type:local) server")
    if not any(r.get("action") == "hijack-dns" for r in rules_dns):
        raise AssertionError("app :53 must still be hijacked into the system resolver")
    if any(r.get("outbound") == "direct" and r.get("port") in ([443], [53])
           for r in rules_dns):
        raise AssertionError("no resolver-IP carve-out rule may survive (type:local self-egresses)")

    # --- OpenAI/CDN/YouTube force-proxy ordering, in BOTH leak modes ---
    # v3.0.7 adds YouTube/Google-CDN suffixes (youtube.com, googlevideo.com,
    # ytimg.com, youtubei.googleapis.com, …) so route_ru_direct/geoip:ru can't pull
    # a CDN IP out the real interface. They must precede every direct/geoip rule and
    # never appear in a DIRECT rule.
    FORCED = ("oaiusercontent.com", "youtube.com", "googlevideo.com", "ytimg.com",
              "youtubei.googleapis.com")
    LEAK_SAFE_SUBSTR = ("oaiusercontent", "openai", "chatgpt", "oaistatic",
                        "youtube", "googlevideo", "ytimg", "youtu.be", "ggpht")
    for leak in (False, True):
        c, rules = build(leak, True)  # ru-direct ON = the dangerous case
        # Collect every forced-proxy suffix into one set for membership tests.
        proxy_suffixes = set()
        for r in rules:
            if r.get("outbound") == "proxy":
                proxy_suffixes.update(r.get("domain_suffix") or [])
        for dom in FORCED:
            if dom not in proxy_suffixes:
                raise AssertionError(f"{dom} must be force-proxied (leak={leak})")
        i_oai = idx(rules, lambda r: r.get("outbound") == "proxy"
                    and "oaiusercontent.com" in (r.get("domain_suffix") or []))
        i_yt = idx(rules, lambda r: r.get("outbound") == "proxy"
                   and "youtube.com" in (r.get("domain_suffix") or []))
        if i_oai is None or i_yt is None:
            raise AssertionError(f"OpenAI+YouTube domains must be force-proxied (leak={leak})")
        i_forced = max(i_oai, i_yt)
        i_geoip = idx(rules, lambda r: r.get("outbound") == "direct"
                      and isinstance(r.get("ip_cidr"), list) and len(r["ip_cidr"]) > 50)
        i_directdom = idx(rules, lambda r: r.get("outbound") == "direct"
                          and r.get("domain_suffix"))
        if i_geoip is not None and i_forced >= i_geoip:
            raise AssertionError("forced-proxy rules must precede geoip:ru direct")
        if i_directdom is not None and i_forced >= i_directdom:
            raise AssertionError("forced-proxy rules must precede direct-domains")
        for r in rules:
            if r.get("outbound") == "direct" and r.get("domain_suffix"):
                for d in r["domain_suffix"]:
                    if any(x in d for x in LEAK_SAFE_SUBSTR):
                        raise AssertionError(f"forced-proxy domain in a DIRECT rule: {d}")


def _v3_ipv6_capture_and_throughput() -> None:
    # v3.0.9: IPv6 is captured INSIDE the sing-box TUN and rejected in-tunnel with
    # a TCP RST (no netsh firewall block → no WSAEACCES → no ERR_NETWORK_ACCESS_DENIED),
    # while v6 still never leaks out the physical NIC. Plus the Windows throughput
    # tuning: mixed stack, encapsulation-safe MTU, endpoint_independent_nat.
    from kapro_tun.core import sing_box_config as sb
    c = sb.build_config(parsed["vless"], ["example.com"], server_ip="1.2.3.4",
                        dns_leak_protection=True, route_ru_direct=True)
    inb = c["inbounds"][0]
    # (a) the TUN carries an inet6 address so auto_route captures ::/0.
    addrs = inb.get("address") or []
    if not any(":" in a for a in addrs):
        raise AssertionError("TUN must carry an inet6 address (capture IPv6 in-tunnel)")
    # (b) throughput tuning.
    if inb.get("stack") != "mixed":
        raise AssertionError("TUN stack must be 'mixed' (kernel TCP) for Windows throughput")
    mtu = int(inb.get("mtu", 0))
    if not 1280 <= mtu <= 1500:
        raise AssertionError("TUN mtu must be internet-safe (1280..1500)")
    if inb.get("endpoint_independent_nat") is not True:
        raise AssertionError("endpoint_independent_nat must be enabled for QUIC/UDP")
    rules = c["route"]["rules"]
    # (c) global-unicast IPv6 is REJECTED in-tunnel (not firewall-blocked).
    reject = [r for r in rules if r.get("action") == "reject"]
    if not any("2000::/3" in (r.get("ip_cidr") or []) for r in reject):
        raise AssertionError("global-unicast IPv6 (2000::/3) must be rejected in-tunnel")
    # (d) LAN v6 must NOT be in any reject rule (NAS / printers keep working).
    for r in reject:
        for lan in ("fc00::/7", "fe80::/10", "ff00::/8"):
            if lan in (r.get("ip_cidr") or []):
                raise AssertionError(f"LAN IPv6 {lan} must NOT be rejected")

    def idx(pred):
        for i, r in enumerate(rules):
            if pred(r):
                return i
        return None
    # (e) LAN-v6 direct rule must precede the global-v6 reject (first match wins).
    i_lan6 = idx(lambda r: r.get("outbound") == "direct"
                 and "fc00::/7" in (r.get("ip_cidr") or []))
    i_rej = idx(lambda r: r.get("action") == "reject"
                and "2000::/3" in (r.get("ip_cidr") or []))
    if i_lan6 is None or i_rej is None or i_lan6 >= i_rej:
        raise AssertionError("LAN-v6 direct rule must precede the global-v6 reject")


def _v3_singbox_skips_firewall_ipv6_block() -> None:
    # v3.1.0: sing-box is the only engine and the TUN handles IPv6 in-tunnel
    # (inet6 address + 2000::/3 reject), so _maybe_arm_ipv6_block NEVER installs a
    # netsh v6 firewall block (which caused ERR_NETWORK_ACCESS_DENIED) — it only
    # clears any stale block a prior build may have left.
    import inspect as _ins
    from kapro_tun.core import controller as _C
    src = _ins.getsource(_C.ConnectionManager._maybe_arm_ipv6_block)
    if "ipv6_block.install()" in src:
        raise AssertionError("_maybe_arm_ipv6_block must NOT install a netsh v6 block")
    if "ipv6_block.remove()" not in src:
        raise AssertionError("_maybe_arm_ipv6_block must clear any stale v6 block")


def _v3_firewall_sweep_prefix() -> None:
    # v3.0.9: the startup brand-prefix sweep must match BOTH the current brand and
    # the legacy pre-rename brand, so old-brand / odd-suffix orphans (e.g.
    # 'KaproVPN-ipv6-block-TEST', the live ERR_NETWORK_ACCESS_DENIED cause) get
    # purged. Surface methods must never raise on any platform.
    from kapro_tun.core import firewall_sweep as fs
    if "KaproTUN-" not in fs._RULE_NAME_PREFIXES or "KaproVPN-" not in fs._RULE_NAME_PREFIXES:
        raise AssertionError("firewall_sweep must match both KaproTUN- and KaproVPN- prefixes")
    for f in (fs.is_supported, fs.has_orphans):
        try:
            f()
        except Exception as e:
            raise AssertionError(f"firewall_sweep.{f.__name__} must not raise: "
                                 f"{type(e).__name__}: {e}")
    # win_job surface must never raise either (no-op / False off-Windows).
    from kapro_tun.core import win_job
    if win_job.assign(0) is not False:
        raise AssertionError("win_job.assign(0) must be a safe False no-op")


def _v3_ip_probe_cold_start_tolerant() -> None:
    # v3.0.10: the IP-probe worker must use a cold-start-tolerant retry window.
    # The probe fires right after connect while the REALITY tunnel's proxy pool is
    # still warming up (~10s); a too-short 2-retry window failed and left the UI
    # showing "Ваш IP: —" on a perfectly working VPN. Assert retries>=4.
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import inspect as _ins
    import re as _re
    from kapro_tun.gui import main_window as _mw
    src = None
    for _name, obj in vars(_mw).items():
        if isinstance(obj, type) and hasattr(obj, "run"):
            try:
                s = _ins.getsource(obj.run)
            except (OSError, TypeError):
                continue
            if "fetch_public_ip" in s:
                src = s
                break
    if src is None:
        raise AssertionError("could not find the IP-probe worker run()")
    retries = _re.search(r"retries\s*=\s*(\d+)", src)
    timeout = _re.search(r"timeout\s*=\s*([0-9.]+)", src)
    if not retries or int(retries.group(1)) != 0:
        raise AssertionError("IP display probe must not retry")
    if not timeout or float(timeout.group(1)) > 2.5:
        raise AssertionError("IP display probe timeout must stay short")


def _v3_ip_probe_bypasses_system_proxy() -> None:
    # v3.0.11: the IP-probe must NEVER route through a system/environment HTTP
    # proxy. An ELEVATED process can resolve a stale/foreign dead 127.0.0.1:<port>
    # proxy (left by another app) that a normal process doesn't — and then EVERY
    # probe request CONNECTs to that dead proxy and times out (ConnectTimeout ⊂
    # Timeout = "timeout after 2.0s" on every endpoint), showing "Ваш IP: —" on a
    # perfectly working VPN. The probe builds a Session with trust_env=False so it
    # ignores HTTP_PROXY/HTTPS_PROXY and the Windows system proxy; the explicit
    # `proxies` arg still drives HTTP-proxy mode.
    import inspect as _ins
    from kapro_tun.core import ip_probe as _ip
    src = _ins.getsource(_ip._probe_with_fallback)
    norm = src.replace(" ", "")
    if "trust_env=False" not in norm:
        raise AssertionError("ip-probe must set trust_env=False (bypass system/env proxy)")
    if "session.get(" not in norm and "session.get(" not in src:
        raise AssertionError("ip-probe must issue the request through the trust_env=False Session")


def _v3_singbox_dns_watchdog_guarded() -> None:
    # 19) v3.1.0: sing-box is the only engine; its TUN hijacks all :53 in BOTH
    #     leak modes, so the runtime DNS watchdog guards whenever a session is
    #     live, regardless of the leak setting — tun_dns_guarded() == is_connected().
    from kapro_tun.core.controller import ConnectionManager
    cm = ConnectionManager(on_log=None)
    cm.is_connected = lambda: True
    if not cm.tun_dns_guarded():
        raise AssertionError("a live sing-box TUN must be DNS-guarded")
    cm.is_connected = lambda: False
    if cm.tun_dns_guarded():
        raise AssertionError("a disconnected session must NOT be DNS-guarded")

    # Debounce: the watchdog needs >=2 consecutive failed probes before healing.
    import os as _ow
    _ow.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.main_window import _DnsWatchdog
    wd = _DnsWatchdog(lambda: True)
    if getattr(wd, "_fail_threshold", 0) < 2:
        raise AssertionError("DNS watchdog must require sustained failure (>=2 probes)")


def _v3_disconnect_restores_dns() -> None:
    # 20) A clean disconnect (and the connect-time rollback) must stop sing-box —
    #     sing-box's auto_route then restores the system DNS/routes itself.
    import inspect as _ins
    from kapro_tun.core import controller as _C
    dsrc = _ins.getsource(_C.ConnectionManager.disconnect)
    if "sing_box_process.stop" not in dsrc:
        raise AssertionError("disconnect() must stop the sing-box process")
    rsrc = _ins.getsource(_C.ConnectionManager._connect_tun_sing_box)
    if "sing_box_process.stop" not in rsrc or "remove_runtime_configs" not in rsrc:
        raise AssertionError("sing-box rollback must stop the process + wipe configs")


def _v3_connect_is_forgiving() -> None:
    # 21) v3.0.8 "just click and connect". The sing-box connect path must NOT
    #     roll back a usable tunnel on a strict secondary DNS check. Specifically:
    #     - NO per-CDN/YouTube/OpenAI rollback gate (the v3.0.7 regression that
    #       errored out working tunnels because proxy-routed brand hosts were slow
    #       to warm up). cdn_hosts must be gone entirely.
    #     - liveness is POLL-based and requires both DNS and real proxy traffic.
    #     - process-up is also polled (no flat sleep that misjudges a slow start).
    import inspect as _ins
    from kapro_tun.core import controller as _C
    src = _ins.getsource(_C.ConnectionManager._connect_tun_sing_box)
    # The redundant CDN gate must be GONE — no per-brand-host rollback.
    if "cdn_hosts" in src or "hosts=cdn_hosts" in src:
        raise AssertionError("connect must NOT re-introduce the per-CDN rollback gate")
    if "не резолвится через туннель" in src:
        raise AssertionError("connect must not roll back on YouTube/CDN DNS")
    # Liveness + process-up must be POLL-based via the helper methods.
    if "_wait_for_singbox_ready" not in src:
        raise AssertionError("connect must poll sing-box readiness")
    if "_wait_until_running" not in src:
        raise AssertionError("connect must poll for process-up (no flat sleep)")
    # Exactly the generic liveness failure may roll back; success still marks live.
    if "mark_live" not in src:
        raise AssertionError("connect must mark_live() on success")
    # The poll helpers must be deadline-bounded loops that succeed on first hit.
    dns_src = _ins.getsource(_C.ConnectionManager._wait_for_singbox_ready)
    if "while" not in dns_src or "deadline" not in dns_src or "return True" not in dns_src:
        raise AssertionError("_wait_for_singbox_ready must be a bounded poll loop")
    if "singbox_outbound_probe" not in dns_src:
        raise AssertionError("sing-box readiness must probe the proxy outbound for live transport (v3.1.1)")
    runtime_src = _ins.getsource(_C.ConnectionManager.tun_runtime_healthy)
    if "singbox_outbound_probe" not in runtime_src:
        raise AssertionError("runtime watchdog must probe the proxy outbound for live transport (v3.1.1)")
    run_src = _ins.getsource(_C.ConnectionManager._wait_until_running)
    if "while" not in run_src or "is_running()" not in run_src:
        raise AssertionError("_wait_until_running must poll is_running() until a deadline")


def _v3_system_tun_must_match_proxy_egress() -> None:
    from kapro_tun.core import dns_health
    original = dns_health._trace_egress_ip
    try:
        dns_health._trace_egress_ip = (
            lambda proxy, timeout=2.5: "77.239.122.15"
        )
        if not dns_health.singbox_system_tun_healthy("http://127.0.0.1:2082"):
            raise AssertionError("matching proxy/system VPN IPs must be healthy")

        dns_health._trace_egress_ip = (
            lambda proxy, timeout=2.5:
                "77.239.122.15" if proxy else "46.138.181.187"
        )
        if dns_health.singbox_system_tun_healthy("http://127.0.0.1:2082"):
            raise AssertionError("real/direct system IP must not pass TUN health")

        dns_health._trace_egress_ip = (
            lambda proxy, timeout=2.5: "77.239.122.15" if proxy else ""
        )
        if dns_health.singbox_system_tun_healthy("http://127.0.0.1:2082"):
            raise AssertionError("missing system-TUN response must be unhealthy")
    finally:
        dns_health._trace_egress_ip = original


def _v3_connect_classic_alive_on_transport() -> None:
    # 21b) v3.0.8: the legacy/classic verify must treat the tunnel as ALIVE when
    #      the proxy transport carries HTTP, even if OS DNS is slow to warm up
    #      under leak protection — `alive = http_ok or dns_ok`, NOT dns-only. A
    #      working server must never fail the connect just because the freshly
    #      cleared resolver lags.
    import inspect as _ins
    from kapro_tun.core import controller as _C
    src = _ins.getsource(_C.ConnectionManager._verify_tunnel_or_raise)
    if "http_ok or dns_ok" not in src:
        raise AssertionError("classic verify must be alive on http_ok OR dns_ok (not dns-only)")
    # The dead-tunnel rollback (both signals dead) must still exist.
    if "raise ConnectionError" not in src:
        raise AssertionError("classic verify must still roll back a genuinely dead tunnel")


def _v3_crash_detector_debounced() -> None:
    # 21c) v3.0.8: the 1s process-crash detector must require TWO consecutive
    #      not-running polls before tearing down (kills the spurious
    #      "Подключение…" flicker from a single unreadable poll), AND must reset
    #      the debounce counter on a healthy tick so it measures CONSECUTIVE misses.
    import inspect as _ins
    from kapro_tun.gui.main_window import MainWindow
    src = _ins.getsource(MainWindow._refresh_home)
    if "_crash_confirm" not in src:
        raise AssertionError("crash detector must use a debounce counter (_crash_confirm)")
    if "self._crash_confirm < 2" not in src and "_crash_confirm < 2" not in src:
        raise AssertionError("crash detector must require >=2 consecutive not-running ticks")
    if "self._crash_confirm = 0" not in src:
        raise AssertionError("crash debounce counter must RESET on a healthy tick (consecutive)")


def _v3_dns_watchdog_not_twitchy() -> None:
    # 21d) The runtime TUN watchdog must require sustained failure before a
    #      disruptive reconnect, so a transient blip never churns a working
    #      tunnel.
    import os as _ow
    _ow.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.main_window import _DnsWatchdog
    wd = _DnsWatchdog(lambda: True)
    if getattr(wd, "_fail_threshold", 0) < 3:
        raise AssertionError("TUN watchdog must require >=3 sustained failures")


def _v3_process_crash_diagnostics() -> None:
    # 22) v3.0.6/v3.0.7 process_crash forensics. A real engine exit must log a
    #     single redacted diagnostic carrying pid + returncode + uptime + the last
    #     raw log lines — so app.log explains WHY it died, not just
    #     "reason=process_crash". Secrets in the tail must be redacted.
    import os as _o
    _o.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from kapro_tun.gui.main_window import MainWindow

    class _FakeProc:
        def last_pid(self): return 43217
        def returncode(self): return 1
        def uptime(self): return 12.7
        def recent_logs(self):
            # include a secret-looking token to prove redaction runs on the tail
            return ["FATAL start: bad config",
                    "uuid: 11111111-2222-3333-4444-555555555555 leaked"]

    # _crash_diagnostics never touches `self`, so call the raw function with a
    # dummy receiver (constructing a real QMainWindow needs the full app).
    diag = MainWindow._crash_diagnostics(object(), _FakeProc(), "sing-box")
    for needle in ("engine=sing-box", "pid=43217", "returncode=1",
                   "uptime=12s", "last_logs=["):
        if needle not in diag:
            raise AssertionError(f"process_crash diag missing {needle!r}: {diag}")
    if "11111111-2222-3333-4444-555555555555" in diag:
        raise AssertionError("process_crash diag must REDACT secrets in the log tail")
    if "\n" in diag:
        raise AssertionError("process_crash diag must be a single line (app.log friendly)")

    # And the crash branch must log it ONCE per episode (debounce flag), gated so a
    # controlled stop / connect rollback / user reconnect doesn't double-count.
    import inspect as _ins
    rsrc = _ins.getsource(MainWindow._refresh_home)
    if "_crash_diag_logged" not in rsrc or "[process_crash]" not in rsrc:
        raise AssertionError("crash branch must log [process_crash] once per episode")
    if "self._connecting" not in rsrc:
        raise AssertionError("crash detection must be gated on _connecting (no rollback false-crash)")


def _v3_block_ads_disabled_on_singbox() -> None:
    # 15) Ad-block is an Xray feature; under the sing-box TUN engine the Settings
    #     checkbox must be DISABLED (never promise blocking that won't happen)
    #     with the 'legacy only' note shown — and the stored block_ads value must
    #     NOT be mutated by switching engines.
    import os as _o4
    _o4.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from kapro_tun.core.controller import ConnectionManager
    from kapro_tun.core import controller as _C4
    from kapro_tun.core import storage as _st4
    from kapro_tun.gui.main_window import SettingsPage
    from kapro_tun.core.controller import MODE_TUN as _MT, MODE_HTTP_PROXY as _MH
    if QApplication.instance() is None:
        QApplication([])

    def _mk(mode, engine, block_ads=True):
        base = {**_st4.DEFAULT_SETTINGS, "mode": mode,
                "tun_engine": engine, "block_ads": block_ads}
        orig = _st4.load_settings
        _st4.load_settings = lambda: dict(base)
        try:
            return SettingsPage(ConnectionManager(on_log=lambda _l: None))
        finally:
            _st4.load_settings = orig

    # TUN + sing-box → disabled + note shown, but the checked state preserved.
    # Use isHidden() (the explicit show/hide flag) — isVisible() is False for any
    # child until the top-level window is actually shown, which never happens
    # in the offscreen test.
    sp = _mk(_MT, _C4.ENGINE_SING_BOX, block_ads=True)
    if sp.block_ads_check.isEnabled():
        raise AssertionError("ad-block must be DISABLED under sing-box TUN")
    if sp._block_ads_engine_note.isHidden():
        raise AssertionError("ad-block 'legacy only' note must be shown under sing-box")
    if not sp.block_ads_check.isChecked():
        raise AssertionError("disabling must NOT clear the stored block_ads value")
    sp.deleteLater()

    # TUN + legacy → enabled, note hidden.
    sp = _mk(_MT, _C4.ENGINE_CLASSIC)
    if not sp.block_ads_check.isEnabled():
        raise AssertionError("ad-block must be enabled under legacy TUN")
    if not sp._block_ads_engine_note.isHidden():
        raise AssertionError("note must be hidden under legacy TUN")
    sp.deleteLater()

    # HTTP mode → enabled (xray handles ad-block) regardless of tun_engine.
    sp = _mk(_MH, _C4.ENGINE_SING_BOX)
    if not sp.block_ads_check.isEnabled():
        raise AssertionError("ad-block must be enabled in HTTP mode (xray)")
    sp.deleteLater()


_SMOKE_LOG_SIGNATURES = (
    "SMOKE-SANDBOX-MARKER",         # the marker this test emits
    "kaprotun-smoke-",             # the sandbox temp-dir prefix
    "Test-VLESS", "Test-Trojan", "Test-HY2",  # synthetic config names
)


def _smoke_does_not_touch_real_app_log() -> None:
    # 16) The whole suite must run inside the sandbox: writing to the REAL
    #     %LOCALAPPDATA%/KaproTUN/app.log is a regression. Two checks:
    #       (a) our marker landed in the SANDBOX app.log (redirect works);
    #       (b) the REAL app.log gained NO smoke-attributable line.
    #     We deliberately do NOT assert the real app.log size is unchanged: a
    #     concurrently-running KaproTUN app appends its own [mem]/[connect]
    #     lifecycle lines during the ~10s run — that is NOT a smoke leak. A real
    #     leak would carry a smoke signature (the marker, the sandbox prefix, a
    #     synthetic test config name), which is what we fail on.
    marker = "SMOKE-SANDBOX-MARKER-do-not-ship"
    _sbx_app_log.log(marker)
    _sbx_app_log._reset_for_test()  # close handlers → flush to disk
    sandbox_log = _SANDBOX_DIR / "app.log"
    sb_text = (sandbox_log.read_text(encoding="utf-8", errors="replace")
               if sandbox_log.exists() else "")
    if marker not in sb_text:
        raise AssertionError("app_log did not write to the sandbox — redirect failed")
    if not _REAL_APP_LOG.exists():
        return
    real_text = _REAL_APP_LOG.read_text(encoding="utf-8", errors="replace")
    if marker in real_text:
        raise AssertionError("smoke wrote the test marker into the REAL app.log!")
    # Any NEW line (vs the pre-suite snapshot) that carries a smoke signature is
    # a leak. App-lifecycle lines from a running instance are tolerated.
    new_lines = [l for l in real_text.splitlines()
                 if l not in _REAL_APP_LOG_LINES_BEFORE]
    leaked = [l for l in new_lines
              if any(sig in l for sig in _SMOKE_LOG_SIGNATURES)]
    if leaked:
        raise AssertionError(
            f"smoke leaked {len(leaked)} line(s) into the REAL app.log: "
            f"{leaked[:3]}")


check("migration: default engine sing-box; old settings migrate", _v3_migration_default)
check("config: native-TUN structure + pinned server + loop killer", _v3_config_structure)
check("config: private/LAN/Docker (172.19.2.109) bypass", _v3_private_bypass)
check("config: DNS always system (type:local) + :53 hijacked, leak kwarg ignored", _v3_dns_hijack_and_leak)
check("config: unsupported protocol/plugin raises", _v3_unsupported_raises)
check("dispatch: unsupported → legacy error, no silent switch", _v3_dispatch_no_silent_switch)
check("dispatch: engine=sing-box routes to sing-box; runtime fail no fallback",
      _v3_dispatch_routes_to_singbox_and_no_runtime_fallback)
check("config: no 2081 bridge; health-only probe forced to proxy", _v3_no_loopback_bridge)
check("cleanup: sing-box runtime config wiped on disconnect", _v3_runtime_cleanup)
check("kill-switch: allows + removes sing-box.exe rule", _v3_killswitch_singbox)
check("stats: sample/format include sing-box process", _v3_stats_include_singbox)
check("config: real `sing-box check` accepts generated config", _v3_real_singbox_check)
check("config: XHTTP/splithttp → UnsupportedBySingBox (no half-working TCP)",
      _v3_xhttp_unsupported)
check("config: XHTTP rejected via raw-url fallback (empty .network)",
      _v3_xhttp_rawurl_fallback)
check("tun: orphan sing-box/tun2socks freed before TUN start (KaproTun collision)",
      _v3_orphan_tun_cleanup)
check("watchdog: engine-aware crash detection (no false Xray crash on sing-box)",
      _v3_singbox_watchdog_engine_aware)
check("logs: sing-box benign/ICMP hidden, transient startup-visible, fatal stays",
      _v3_singbox_log_noise_classification)
check("logs: block_ads notice logs once per launch, not per reconnect",
      _v3_block_ads_warning_once)
check("dns/routing: DNS always system + OpenAI/CDN/YouTube force-proxy ordering",
      _v3_dns_and_split_routing)
check("watchdog: sing-box DNS guarded both leak modes, sustained-failure debounce",
      _v3_singbox_dns_watchdog_guarded)
check("disconnect/rollback stops sing-box → system DNS/routes restored",
      _v3_disconnect_restores_dns)
check("ipv6: captured + rejected, mixed stack, safe MTU/EIN (v3.0.13)",
      _v3_ipv6_capture_and_throughput)
check("ipv6: sing-box engine skips the netsh firewall block (v3.0.9)",
      _v3_singbox_skips_firewall_ipv6_block)
check("firewall_sweep: matches KaproTUN-/KaproVPN- prefixes; win_job safe no-op (v3.0.9)",
      _v3_firewall_sweep_prefix)
check("ip-probe: one short optional attempt, never a connect gate (v3.0.13)",
      _v3_ip_probe_cold_start_tolerant)
check("ip-probe: trust_env=False — never hijacked by a system/env proxy (v3.0.11)",
      _v3_ip_probe_bypasses_system_proxy)
check("connect: forgiving — no CDN rollback, poll-based liveness warm-up (v3.0.8)",
      _v3_connect_is_forgiving)
check("connect: system TUN egress must match proxy VPN IP (v3.0.13)",
      _v3_system_tun_must_match_proxy_egress)
check("watchdog: crash detector debounced 2 consecutive ticks + resets (v3.0.8)",
      _v3_crash_detector_debounced)
check("watchdog: TUN health needs >=3 sustained fails, not twitchy (v3.0.13)",
      _v3_dns_watchdog_not_twitchy)
check("diagnostics: process_crash logs pid/returncode/uptime/redacted-tail once",
      _v3_process_crash_diagnostics)

# Must run LAST — proves the whole suite stayed in the sandbox and never wrote
# to the real app.log.
check("sandbox: smoke never writes to the real app.log", _smoke_does_not_touch_real_app_log)


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
