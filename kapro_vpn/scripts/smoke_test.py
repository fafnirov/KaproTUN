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
    )


def _import_gui() -> None:
    # GUI modules touch PySide6 at import time — runs under
    # xvfb-style headless mode on the smoke runner.
    from kapro_vpn.gui import (  # noqa: F401
        main_window, tray, widgets, onboarding,
        configs_picker, subscription_dialog, sites_dialog,
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
