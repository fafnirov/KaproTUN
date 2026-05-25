"""Connection controller: ties together config generation, Xray-core, tun2socks, and system proxy."""
from __future__ import annotations

import atexit
import socket
import sys
import time
from typing import Callable, Optional

from . import admin, geoip_ru, storage, system_proxy, xray_config
from .parser import ProxyConfig
from .xray_process import XrayProcess

# TUN-mode plumbing: same public API on every OS, but the backend that
# manipulates routes/DNS is platform-specific. The Windows backend uses
# native Win32 ctypes calls; the Unix backend shells out to `ip` / `route`
# / `ifconfig`. Both expose RouteSession + the few helpers controller
# code below calls, so we can keep one code path with no per-OS branches
# in the connect flow.
from . import tun2socks_process
from .tun2socks_process import Tun2socksProcess
if sys.platform == "win32":
    from . import network_routes
else:
    from . import network_routes_unix as network_routes


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
        # WG mode uses an external Windows service — its tunnel name is
        # kept here so disconnect() knows what to tear down. None when
        # the active config isn't WG.
        self._active_wg_tunnel: Optional[str] = None
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
        # WireGuard takes a completely different code path — it doesn't
        # run inside xray at all. The official WireGuard for Windows
        # service does the actual protocol; we just orchestrate it +
        # add bypass routes on top for split-routing.
        if config.protocol in ("wireguard", "wg"):
            self._connect_wireguard(config, direct_domains)
            return
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
        # If we were in WG mode, also uninstall the WireGuard service.
        # Doing this AFTER xray/tun cleanup is harmless — they're
        # unrelated for WG configs anyway.
        if self._active_wg_tunnel is not None:
            from . import wireguard_service
            try:
                wireguard_service.uninstall_tunnel(self._active_wg_tunnel)
            except Exception:
                pass
            self._active_wg_tunnel = None
        self._active = None

    def is_connected(self) -> bool:
        # In TUN mode, the active session is tun_process + xray both up.
        # In HTTP mode, just xray.
        # In WG mode, neither xray nor tun_process — we check the
        # WireGuard Windows service instead.
        if self._active_wg_tunnel is not None:
            from . import wireguard_service
            return wireguard_service.is_tunnel_active(self._active_wg_tunnel)
        return self.process.is_running() or self.tun_process.is_running()

    def active_config(self) -> Optional[ProxyConfig]:
        return self._active if self.is_connected() else None

    def update_settings(self, **changes) -> None:
        self.settings.update(changes)
        storage.save_settings(self.settings)

    def current_mode(self) -> str:
        return self.settings.get("mode", MODE_HTTP_PROXY)

    # --- WireGuard mode (via official WireGuard for Windows service) ------

    def _connect_wireguard(self, config: ProxyConfig,
                           direct_domains: list[str]) -> None:
        """Bring up a WireGuard tunnel via WireGuard.exe /installtunnelservice.

        WG is fundamentally system-wide (it creates a kernel TUN with a
        default route per AllowedIPs), so it bypasses xray entirely and
        ignores the HTTP-proxy vs TUN-mode toggle. Required:

          - Windows (only platform we support WG on for v1.3)
          - Admin (Windows service install needs it)
          - WireGuard for Windows installed (we detect and prompt)

        Split-routing: WG service handles default-via-tunnel, we add
        bypass routes for direct-list domains + geoip:ru on top.
        """
        if sys.platform != "win32":
            raise ConnectionError(
                "WireGuard в KaproVPN пока работает только на Windows.\n"
                "Для mac/Linux используй другой протокол (VLESS/Trojan/VMess) "
                "или подожди следующего релиза."
            )
        if not admin.is_admin():
            raise ConnectionError(
                "WireGuard требует прав администратора (нужно для регистрации "
                "Windows-службы туннеля).\n"
                "Перезапусти KaproVPN от имени администратора и попробуй снова."
            )

        from . import wireguard_service
        from .parser import wg_conf_from_raw_url

        # First-launch: if WireGuard for Windows isn't installed, fetch
        # and install the MSI silently. Takes ~10-15 seconds total.
        # We're already admin (gated above), msiexec /quiet works clean.
        # User just sees the spinner — never sees a third-party installer.
        if not wireguard_service.is_installed():
            self._log("[*] Первый запуск WG — скачиваю и ставлю WireGuard-runtime…")
            try:
                wireguard_service.ensure_installed(
                    progress=lambda done, total: self._log(
                        f"[*] WireGuard MSI: {done // 1024} / "
                        f"{(total or 1) // 1024} KB"
                    ) if total and done % (256 * 1024) < 64 * 1024 else None
                )
            except Exception as e:
                raise ConnectionError(
                    f"Не удалось установить WireGuard-runtime: {e}"
                ) from e
            self._log("[*] WireGuard-runtime установлен")

        # First-launch cleanup: remove any orphan KaproVPN-* tunnels
        # from previous crashed runs. Cheap (one `sc query`).
        try:
            wireguard_service.cleanup_orphan_tunnels()
        except Exception:
            pass

        # Recover the raw .conf text. parser.py base64-encodes it inside
        # raw_url for round-trip storage; here we need the actual INI to
        # hand to WireGuard.exe.
        try:
            conf_text = wg_conf_from_raw_url(config.raw_url)
        except Exception as e:
            raise ConnectionError(
                f"Не удалось декодировать WG-конфиг: {e}"
            ) from e

        # Snapshot the default route BEFORE WG installs its own, so our
        # bypass-route additions know where to point.
        real = network_routes.get_default_route_v4()
        if real is None or not real.gateway or not real.index:
            raise ConnectionError(
                "Не удалось определить текущий шлюз по умолчанию. "
                "Возможно, нет активного интернет-соединения."
            )

        tunnel_name = wireguard_service.sanitize_tunnel_name(config.name)
        self._log(f"[*] Регистрирую WG-туннель как Windows-службу: {tunnel_name}")
        try:
            wireguard_service.install_tunnel(conf_text, tunnel_name)
        except wireguard_service.WireGuardError as e:
            raise ConnectionError(str(e)) from e

        # Sanity-check that wireguard.exe actually created the service.
        # On some Windows configurations (KB-pending updates, broken WG
        # install), /installtunnelservice exits with rc=0 but silently
        # fails to register the service. Detect that here so we don't
        # spend 15 s polling for a status that'll always be empty.
        initial_status = wireguard_service.get_tunnel_status(tunnel_name)
        if not initial_status:
            raise ConnectionError(
                f"wireguard.exe вернул успех, но Windows-служба "
                f"'WireGuardTunnel${tunnel_name}' не появилась в списке. "
                f"Возможно, легаси-установка WireGuard в Program Files "
                f"битая — попробуй удалить её через Параметры → "
                f"Приложения → WireGuard, и переподключись."
            )
        self._log(f"[*] Служба зарегистрирована (статус: {initial_status}), "
                  f"жду пока выйдет в Running…")

        # Service install is async — wait for it to actually be RUNNING
        # before adding bypass routes (the WG interface needs to exist
        # for routes via it to be valid).
        if not wireguard_service.wait_for_tunnel_up(tunnel_name, timeout=15.0):
            last_status = wireguard_service.get_tunnel_status(tunnel_name) or "(unknown)"
            wireguard_service.uninstall_tunnel(tunnel_name)
            raise ConnectionError(
                f"WG-туннель зарегистрирован, но не вышел в Running за 15 секунд.\n"
                f"Последний статус службы: {last_status}.\n\n"
                f"Если 'Stopped' — tunnel.dll стартовал и сразу упал. Скорее "
                f"всего endpoint недоступен с твоей сети (RKN режет UDP к "
                f"WG-серверу) или ключи в .conf не совпадают с серверными.\n"
                f"Если 'StartPending' — служба зависла на инициализации.\n\n"
                f"Полная диагностика: Event Viewer → Application log → "
                f"source 'WireGuard'."
            )
        self._log(f"[*] WG-туннель активен")
        self._active_wg_tunnel = tunnel_name

        # Bypass routes for split-routing — same pattern as TUN mode,
        # just layered on top of WG's own default routes instead of our
        # tun2socks-managed default.
        session = network_routes.RouteSession()
        bypass_metric = real.interface_metric + 1
        try:
            # Direct-domains bypass: resolve, install /32 host-routes
            # via real gateway so kernel never sends them to WG.
            if direct_domains:
                self._log(f"[*] Резолвлю {len(direct_domains)} прямых доменов…")
                domain_ips = network_routes.resolve_domains_parallel(direct_domains)
                all_ips = sorted({ip for ips in domain_ips.values() for ip in ips})
                resolved = sum(1 for ips in domain_ips.values() if ips)
                self._log(
                    f"[*] Резолв: {resolved}/{len(direct_domains)} доменов, "
                    f"{len(all_ips)} уникальных IP"
                )
                added = session.add_bypass_routes(
                    all_ips, real.gateway, real.index, metric=bypass_metric,
                )
                self._log(f"[*] Добавлено {added} bypass-роутов для прямых доменов")

            # geoip:ru — bulk bypass for the whole local CIDR space.
            ru_cidrs = geoip_ru.load_cidrs()
            if ru_cidrs:
                self._log(f"[*] Добавляю {len(ru_cidrs)} CIDR'ов из geoip-списка…")
                t0 = time.time()
                added = session.add_bypass_cidrs(
                    ru_cidrs, real.gateway, real.index, metric=bypass_metric,
                )
                self._log(
                    f"[*] Добавлено {added} CIDR'ов за {time.time()-t0:.1f}с — "
                    f"локальный IP-блок идёт мимо WG"
                )
        except Exception:
            session.restore()
            wireguard_service.uninstall_tunnel(tunnel_name)
            self._active_wg_tunnel = None
            raise

        self._route_session = session
        self._active = config

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
            # Per-OS phrasing — the elevation path differs enough to be
            # worth a tailored hint each time.
            if sys.platform == "win32":
                msg = (
                    "TUN-режим требует прав администратора.\n"
                    "Перезапусти KaproVPN от имени администратора "
                    "(правый клик по ярлыку → «Запуск от имени администратора») "
                    "или переключи в Настройках режим на «HTTP-прокси»."
                )
            elif sys.platform == "darwin":
                msg = (
                    "TUN-режиму нужен root для создания utun-интерфейса и "
                    "настройки маршрутов.\n"
                    "Закрой KaproVPN и запусти из терминала:\n"
                    "    sudo /Applications/KaproVPN.app/Contents/MacOS/KaproVPN\n"
                    "Или переключи режим на «HTTP-прокси» — он не требует прав."
                )
            else:
                msg = (
                    "TUN-режиму нужен root для управления маршрутами.\n"
                    "Закрой KaproVPN и запусти через sudo / pkexec:\n"
                    "    pkexec ./KaproVPN-Linux-x64.AppImage\n"
                    "    (или sudo ./KaproVPN-Linux-x64.AppImage)\n"
                    "Или переключи режим на «HTTP-прокси»."
                )
            raise ConnectionError(msg)

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

        # Step 5: wait for the TUN interface to appear. Per-OS quirk:
        # macOS doesn't let us name our utun device (kernel picks utunN),
        # so we wait for tun2socks to log the assigned name first, then
        # look it up. On Windows + Linux we picked the name and just wait
        # for the OS to register it.
        if sys.platform == "darwin":
            tun_name = self._wait_for_mac_tun_name(timeout=8.0)
            if tun_name is None:
                self.tun_process.stop()
                self.process.stop()
                raise ConnectionError(
                    "tun2socks не сообщил имя utun-интерфейса за 8с. "
                    "Запусти KaproVPN через sudo и попробуй снова."
                )
        else:
            tun_name = tun2socks_process.TUN_DEVICE_NAME
        tun = network_routes.find_interface_by_name(tun_name, timeout=10.0)
        if tun is None:
            self.tun_process.stop()
            self.process.stop()
            if sys.platform == "win32":
                raise ConnectionError(
                    "TUN-интерфейс не появился за 10 секунд. "
                    "Проверь, что wintun.dll лежит рядом с tun2socks.exe и что "
                    "у процесса есть права администратора."
                )
            raise ConnectionError(
                f"TUN-интерфейс «{tun_name}» не появился за 10 секунд. "
                f"Проверь, что у процесса есть root-привилегии."
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
                rc = getattr(session, "last_error_rc", 0)
                hint = ""
                if rc == 87:
                    hint = (" Windows вернул ERROR_INVALID_PARAMETER (87). "
                            "Возможно gateway или метрика не подходят к интерфейсу.")
                elif rc == 160:
                    hint = (" Windows вернул ERROR_BAD_ARGUMENTS (160). "
                            "Метрика маршрута ниже метрики интерфейса.")
                elif rc == 183:
                    hint = (" Windows вернул ERROR_ALREADY_EXISTS (183) и delete-retry "
                            "не сработал. Сделай вручную в админ-PowerShell: "
                            f"`route delete {server_ip}` и подключись снова.")
                elif rc == 1314:
                    hint = (" Windows вернул ERROR_PRIVILEGE_NOT_HELD (1314). "
                            "Перезапусти KaproVPN от админа.")
                elif rc:
                    hint = f" (Windows rc={rc})"
                raise ConnectionError(
                    f"Не удалось добавить host-route для VPN-сервера ({server_ip})."
                    + hint
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
                self._log(f"[*] Добавлено {added} CIDR'ов за {time.time()-t0:.1f}с — локальный IP-блок идёт мимо TUN")
            else:
                self._log("[!] CIDR-список не закеширован — прямые сайты с динамическими IP могут не работать")
        except Exception:
            session.restore()
            self.tun_process.stop()
            self.process.stop()
            raise

        self._route_session = session
        self._active = config

    def _wait_for_mac_tun_name(self, timeout: float = 8.0) -> Optional[str]:
        """macOS-only: poll tun2socks for the kernel-assigned utunN name.

        tun2socks doesn't accept a fixed name on Darwin — it asks the
        kernel for the next free utun slot and announces the result in
        its first INFO log line. We watch that line via the process'
        captured logs.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            name = getattr(self.tun_process, "mac_device_name", lambda: None)()
            if name:
                return name
            time.sleep(0.2)
        return None

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
