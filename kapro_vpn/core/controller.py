"""Connection controller: ties together config generation, Xray-core, tun2socks, and system proxy."""
from __future__ import annotations

import atexit
import socket
import time
from typing import Callable, Optional

from . import admin, geoip_ru, network_routes, storage, system_proxy, tun2socks_process, xray_config
from .parser import ProxyConfig
from .tun2socks_process import Tun2socksProcess
from .xray_process import XrayProcess


class ConnectionError(Exception):
    pass


# Connection modes
MODE_HTTP_PROXY = "http"   # Browser-only, sets Windows system HTTP proxy. No admin needed.
MODE_TUN = "tun"           # System-wide TUN tunnel. Needs admin. Works for all apps incl. Telegram, Steam.


# TUN-side IPs — these live on the virtual interface, not on any real network
TUN_LOCAL_ADDR = "10.255.0.2"
TUN_GATEWAY = "10.255.0.1"
TUN_MASK = "255.255.255.0"
# Yandex DNS first (Russian, fast for ru-sites, supports DoH), Cloudflare fallback.
# These are also explicitly bypassed below so the queries themselves don't loop
# through the tunnel.
TUN_DNS = ["77.88.8.8", "1.1.1.1"]

# Bypass these IPs unconditionally — DNS resolvers, plus the big Russian
# service-provider blocks. Without this, anything that tries to resolve a
# domain via DoH (Yandex Browser does this by default) gets routed through
# our VLESS server, the DoH endpoint sees a Hostkey IP, may rate-limit or
# refuse, and the browser reports ERR_NAME_NOT_RESOLVED.
#
# Each entry: (dest_or_network, mask). For a /32 host-route use 255.255.255.255.
_ALWAYS_BYPASS: list[tuple[str, str]] = [
    # --- Public DNS resolvers ---
    ("77.88.8.8",  "255.255.255.255"),  # Yandex Public DNS (basic)
    ("77.88.8.1",  "255.255.255.255"),  # Yandex Public DNS (basic, secondary)
    ("77.88.8.88", "255.255.255.255"),  # Yandex Safe DNS
    ("77.88.8.7",  "255.255.255.255"),  # Yandex Family DNS
    ("1.1.1.1",    "255.255.255.255"),  # Cloudflare
    ("1.0.0.1",    "255.255.255.255"),  # Cloudflare secondary
    ("8.8.8.8",    "255.255.255.255"),  # Google
    ("8.8.4.4",    "255.255.255.255"),  # Google secondary

    # --- Yandex service blocks (AS13238) — covers DoH, search, maps, mail,
    # disk, music, taxi, eda, yastatic, etc.
    ("5.45.192.0",     "255.255.248.0"),  # /21
    ("5.255.192.0",    "255.255.240.0"),  # /20
    ("77.88.0.0",      "255.255.192.0"),  # /18  (DNS lives in here)
    ("87.250.224.0",   "255.255.224.0"),  # /19
    ("93.158.128.0",   "255.255.128.0"),  # /17
    ("178.154.128.0",  "255.255.128.0"),  # /17
    ("213.180.192.0",  "255.255.224.0"),  # /19

    # --- VK / Mail.ru group (AS47541, AS47764) ---
    ("87.240.128.0",   "255.255.192.0"),  # /18
    ("93.186.224.0",   "255.255.240.0"),  # /20
    ("95.213.192.0",   "255.255.248.0"),  # /21

    # --- yastatic.net / yandexcloud (Yandex CDN, different AS) ---
    ("213.180.193.0",  "255.255.255.0"),  # /24
]


class ConnectionManager:
    """Single source of truth for the connect/disconnect lifecycle."""

    def __init__(self, on_log: Optional[Callable[[str], None]] = None):
        self._on_log = on_log
        self.process = XrayProcess(on_log=on_log)
        self.tun_process = Tun2socksProcess(
            on_log=(lambda l: on_log(f"[tun2socks] {l}")) if on_log else None,
        )
        self.settings = storage.load_settings()
        self._saved_proxy_state: Optional[dict] = None
        self._route_session: Optional[network_routes.RouteSession] = None
        self._active: Optional[ProxyConfig] = None
        # Belt-and-braces: if Python exits uncleanly with TUN routes active,
        # the user's network is broken until reboot. Best-effort cleanup here.
        atexit.register(self._atexit_cleanup)

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    # --- public API -------------------------------------------------------

    def connect(self, config: ProxyConfig, direct_domains: list[str]) -> None:
        if self.is_connected():
            raise ConnectionError("Уже подключено. Сначала отключись.")
        mode = self.settings.get("mode", MODE_HTTP_PROXY)
        if mode == MODE_TUN:
            self._connect_tun(config, direct_domains)
        else:
            self._connect_http(config, direct_domains)

    def disconnect(self) -> None:
        # Order matters: stop TUN routing first so traffic stops hitting the
        # tunnel, then tear down processes, then restore system proxy.
        if self._route_session is not None:
            try:
                self._route_session.restore()
            finally:
                self._route_session = None
        if self.tun_process.is_running():
            self.tun_process.stop()
        if self._saved_proxy_state is not None:
            try:
                system_proxy.restore(self._saved_proxy_state)
            finally:
                self._saved_proxy_state = None
        self.process.stop()
        self._active = None

    def is_connected(self) -> bool:
        # In TUN mode, the active session is tun_process + xray both up.
        # In HTTP mode, just xray.
        return self.process.is_running() or self.tun_process.is_running()

    def active_config(self) -> Optional[ProxyConfig]:
        return self._active if self.is_connected() else None

    def update_settings(self, **changes) -> None:
        self.settings.update(changes)
        storage.save_settings(self.settings)

    def current_mode(self) -> str:
        return self.settings.get("mode", MODE_HTTP_PROXY)

    # --- HTTP-proxy mode (browsers only) ----------------------------------

    def _connect_http(self, config: ProxyConfig, direct_domains: list[str]) -> None:
        host = str(self.settings.get("listen_host", "127.0.0.1"))
        port = int(self.settings.get("listen_port", 2080))
        self._write_and_check(config, direct_domains, host, port)
        self._start_xray()
        if self.settings.get("auto_set_system_proxy", True):
            self._saved_proxy_state = system_proxy.get_state()
            try:
                system_proxy.set_proxy(host, port)
            except Exception as e:
                self.process.stop()
                self._saved_proxy_state = None
                raise ConnectionError(
                    f"Xray запустился, но не удалось поставить системный прокси: {e}"
                ) from e
        self._active = config

    # --- TUN mode (system-wide) -------------------------------------------

    def _connect_tun(self, config: ProxyConfig, direct_domains: list[str]) -> None:
        if not admin.is_admin():
            raise ConnectionError(
                "TUN-режим требует прав администратора.\n"
                "Перезапусти KaproVPN от имени администратора "
                "(правый клик по ярлыку → «Запуск от имени администратора») "
                "или переключи в Настройках режим на «HTTP-прокси»."
            )

        # Step 1: snapshot the real default gateway BEFORE we mess with routes.
        real = network_routes.get_default_route_v4()
        if real is None or not real.gateway or not real.index:
            raise ConnectionError(
                "Не удалось определить текущий шлюз по умолчанию. "
                "Возможно, нет активного интернет-соединения."
            )

        # Step 2: resolve the VPN-server hostname to an IP — we need a static
        # route to it so xray's outbound to the server doesn't loop back into
        # the TUN we're about to create.
        server_host = str(config.outbound.get("server", "")).strip()
        if not server_host:
            raise ConnectionError("В конфиге нет адреса сервера.")
        try:
            server_ip = socket.gethostbyname(server_host)
        except socket.gaierror as e:
            raise ConnectionError(f"Не удалось резолвнуть VPN-сервер «{server_host}»: {e}") from e

        # Step 3: write + validate + start xray (its SOCKS5 inbound is what
        # tun2socks forwards into).
        host = "127.0.0.1"
        port = int(self.settings.get("listen_port", 2080))
        self._write_and_check(config, direct_domains, host, port)
        self._start_xray()

        # Step 4: launch tun2socks — it creates the TUN device and forwards
        # all packets to xray's SOCKS5 inbound at port+1.
        try:
            self.tun_process.start(socks_addr=f"{host}:{port + 1}")
        except Exception as e:
            self.process.stop()
            raise ConnectionError(f"Не удалось запустить tun2socks: {e}") from e

        # Step 5: wait for the TUN interface to show up in Windows.
        tun = network_routes.find_interface_by_name(
            tun2socks_process.TUN_DEVICE_NAME, timeout=10.0,
        )
        if tun is None:
            self.tun_process.stop()
            self.process.stop()
            raise ConnectionError(
                "TUN-интерфейс не появился за 10 секунд. "
                "Проверь, что wintun.dll лежит рядом с tun2socks.exe и что "
                "у процесса есть права администратора."
            )

        # Step 6: assign an IP to the TUN, set its DNS, install routes.
        # If anything below fails, restore() in the except block unwinds
        # every change we made.
        session = network_routes.RouteSession()
        try:
            network_routes.configure_tun_interface(tun, TUN_LOCAL_ADDR, TUN_MASK)

            # CreateIpForwardEntry needs m1 >= interface_metric (it's the
            # STORED metric, not the route increment). Bumping by +1 keeps
            # us above the adapter's base and still less than anything
            # competing on the same /n.
            bypass_metric = real.interface_metric + 1

            # Pin server IP through the real gateway (loop prevention).
            if not session.add_route(server_ip, "255.255.255.255",
                                     real.gateway, real.index, metric=bypass_metric):
                raise ConnectionError(
                    f"Не удалось добавить host-route для VPN-сервера ({server_ip})."
                )

            # Bypass DNS resolvers + big Russian service blocks (Yandex/VK
            # ranges) so DoH and CDN traffic doesn't loop through the tunnel.
            added_always = session.add_bypass_cidrs(
                _ALWAYS_BYPASS, real.gateway, real.index, metric=bypass_metric,
            )
            self._log(f"[*] Добавлено {added_always} always-bypass роутов "
                      f"(DNS-серверы + Yandex/VK CIDR'ы)")

            # Default route through TUN. Two /1 routes beat the existing
            # 0.0.0.0/0 by being more specific, so we use those instead of
            # touching the system default. Metric must be >= TUN interface's
            # own metric for the same reason as above.
            tun_metric = network_routes._get_interface_metric_v4(tun.index) + 1
            if not session.add_route("0.0.0.0", "128.0.0.0", TUN_GATEWAY, tun.index, metric=tun_metric):
                raise ConnectionError("Не удалось добавить маршрут 0.0.0.0/1 через TUN.")
            if not session.add_route("128.0.0.0", "128.0.0.0", TUN_GATEWAY, tun.index, metric=tun_metric):
                raise ConnectionError("Не удалось добавить маршрут 128.0.0.0/1 через TUN.")

            # DNS via TUN so resolution doesn't leak to ISP.
            session.set_dns(tun.name, TUN_DNS)

            # Split-routing in TUN mode: xray's freedom outbound can't be
            # trusted alone because its outgoing packets still hit the kernel
            # routing table, which currently sends everything to TUN — that
            # means freedom -> TCP -> kernel -> TUN -> tun2socks -> xray ->
            # freedom -> ... infinite loop, manifesting as a connection
            # timeout to the user. The fix is the same trick AmneziaVPN uses:
            # resolve every direct-list domain and pin /32 bypass routes for
            # the resulting IPs via the real gateway. The kernel then dodges
            # the TUN entirely for that traffic.
            if direct_domains:
                self._log(f"[*] Резолвлю {len(direct_domains)} доменов из списка direct…")
                domain_ips = network_routes.resolve_domains_parallel(direct_domains)
                all_ips = [ip for ips in domain_ips.values() for ip in ips]
                resolved = sum(1 for ips in domain_ips.values() if ips)
                failed = len(direct_domains) - resolved
                self._log(
                    f"[*] Резолв: {resolved}/{len(direct_domains)} доменов, "
                    f"{len(set(all_ips))} уникальных IP"
                    + (f" (не резолвнулось: {failed})" if failed else "")
                )
                added = session.add_bypass_routes(all_ips, real.gateway, real.index, metric=bypass_metric)
                self._log(f"[*] Добавлено {added} bypass-роутов для direct-доменов")

            # geoip:ru — bypass the entire Russian IP space so any RU-hosted
            # resource (CDN sub-domains we didn't pre-resolve, third-party
            # widgets, statics, even minor sites) skips the tunnel.
            # Cached list lives in %LOCALAPPDATA%\KaproVPN\geoip-ru.txt;
            # main_window's connect path triggers download if missing.
            ru_cidrs = geoip_ru.load_cidrs()
            if ru_cidrs:
                self._log(f"[*] Добавляю {len(ru_cidrs)} CIDR'ов из geoip:ru…")
                t0 = time.time()
                added = session.add_bypass_cidrs(ru_cidrs, real.gateway, real.index, metric=bypass_metric)
                self._log(f"[*] Добавлено {added} CIDR'ов за {time.time()-t0:.1f}с — российский IP-космос идёт мимо TUN")
            else:
                self._log("[!] geoip:ru не закеширован — российские сайты с динамическими IP могут не работать")
        except Exception:
            session.restore()
            self.tun_process.stop()
            self.process.stop()
            raise

        self._route_session = session
        self._active = config

    # --- helpers ----------------------------------------------------------

    def _write_and_check(self, config: ProxyConfig, direct_domains: list[str],
                         host: str, port: int) -> str:
        try:
            path = xray_config.write_config(config, direct_domains, host, port)
        except (ValueError, NotImplementedError) as e:
            raise ConnectionError(f"Конфиг не поддерживается: {e}") from e
        ok, msg = XrayProcess.check_config(path)
        if not ok:
            raise ConnectionError(f"Xray отверг конфиг:\n{msg}")
        return path

    def _start_xray(self) -> None:
        try:
            self.process.start(str(xray_config.paths.runtime_config_file()))
        except Exception as e:
            raise ConnectionError(f"Не удалось запустить Xray: {e}") from e

    def _atexit_cleanup(self) -> None:
        try:
            self.disconnect()
        except Exception:
            pass
