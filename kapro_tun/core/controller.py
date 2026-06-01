"""Connection controller: ties together config generation, Xray-core, tun2socks, and system proxy."""
from __future__ import annotations

import atexit
import socket
import sys
import time
from typing import Callable, Optional

from . import (
    admin, dns_options, geoip_ru, hysteria_installer, hysteria_process,
    ipv6_block, killswitch, paths, storage, webrtc_block,
    system_proxy, xray_config,
)
from .parser import ProxyConfig
from .xray_process import XrayProcess
from .hysteria_process import HysteriaProcess

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
        # Hysteria2 transport (only started for hy2 configs) — runs a local
        # SOCKS5 that xray chains through, since Xray can't dial hy2 itself.
        self.hysteria_process = HysteriaProcess(
            on_log=(lambda l: on_log(f"[hysteria] {l}")) if on_log else None,
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
        try:
            if mode == MODE_TUN:
                self._connect_tun(config, direct_domains)
            else:
                self._connect_http(config, direct_domains)
        except Exception:
            # Any connect failure: make sure the hysteria helper isn't left
            # running. Idempotent — a no-op for non-hy2 configs. (Per-step
            # xray / tun2socks / proxy rollback is handled inside the paths.)
            try:
                self.hysteria_process.stop()
            except Exception:
                pass
            raise

    @staticmethod
    def _is_hysteria(config: ProxyConfig) -> bool:
        return config.raw_url.split("://", 1)[0].lower() in ("hysteria2", "hy2")

    def _maybe_start_hysteria(self, config: ProxyConfig) -> Optional[int]:
        """For hy2 configs: ensure the hysteria binary, start it as a local
        SOCKS5 proxy and wait until it's listening. Returns the SOCKS port
        for xray to chain through, or None for non-hy2 configs.
        """
        if not self._is_hysteria(config):
            return None
        try:
            hysteria_installer.ensure_installed()
        except Exception as e:
            raise ConnectionError(f"Не удалось скачать hysteria-клиент: {e}") from e
        port = hysteria_process.HYSTERIA_SOCKS_PORT
        # Link-speed hints → hysteria's high-throughput brutal CC.
        up = int(self.settings.get("hysteria_up_mbps", 0) or 0)
        down = int(self.settings.get("hysteria_down_mbps", 0) or 0)
        # Auto mode: if we don't have a measurement yet, measure the link
        # NOW. We're early in connect() — before the TUN routes go up — so
        # this hits the RAW link, not the tunnel. Cache the result so later
        # connects are instant; "Перемерить" clears it to re-measure.
        if bool(self.settings.get("hysteria_auto_bandwidth", True)) and (up <= 0 or down <= 0):
            self._log("[*] Замеряю скорость канала для Hysteria2 (разово)…")
            from . import speed_test
            t_start = time.time()
            try:
                m_down, m_up = speed_test.measure_link_speed()
            except Exception:
                m_down, m_up = 0, 0
            # Retry once — but only if the first attempt failed FAST (a
            # transient DNS/connection blip), not after burning the whole
            # measurement window on a genuinely dead-slow link. Bounds the
            # extra connect-time stall to one quick re-attempt.
            if (m_down <= 0 or m_up <= 0) and (time.time() - t_start) < 6.0:
                self._log("[!] Замер сорвался (быстрый сбой) — пробую ещё раз…")
                time.sleep(1.0)
                try:
                    m_down, m_up = speed_test.measure_link_speed()
                except Exception:
                    m_down, m_up = 0, 0
            if m_down > 0 and m_up > 0:
                # Auto-measured -> apply a safety cap before brutal CC. Feeding
                # the FULL measured rate makes a bursty app (Telegram media, a
                # torrent) oversubscribe the link — especially the uplink — and
                # bufferbloat stalls everything. The cap keeps headroom. Manual
                # values (auto off) skip this and are used verbatim.
                down, up = hysteria_process.apply_auto_bandwidth_margin(m_down, m_up)
                self.update_settings(hysteria_down_mbps=down, hysteria_up_mbps=up)
                self._log(
                    f"[*] Замерено: ↓{m_down} / ↑{m_up} Мбит/с; "
                    f"безопасный кап → ↓{down} / ↑{up} Мбит/с — включаю brutal CC"
                )
            else:
                self._log("[!] Не удалось замерить скорость — Hysteria2 на авто (BBR)")
        try:
            cfg_path = hysteria_process.write_client_config(
                config.outbound, port, up_mbps=up, down_mbps=down)
        except Exception as e:
            raise ConnectionError(f"Не удалось записать конфиг hysteria: {e}") from e

        # Start with one automatic retry. The first attempt can FATAL
        # transiently — a cold QUIC handshake, or the link momentarily
        # saturated (e.g. a speedtest running while the user fills in the
        # bandwidth setting) makes hysteria's init handshake to the server
        # time out ("connect error: timeout: no recent network activity").
        # A clean restart then succeeds — this is the "fails the first time,
        # works on the second connect" bug. Do that retry for the user.
        attempts = 2
        last_tail = ""
        for attempt in range(1, attempts + 1):
            if self.hysteria_process.is_running():
                self.hysteria_process.stop()  # never leave a half-dead one
            try:
                self.hysteria_process.start(str(cfg_path))
            except Exception as e:
                raise ConnectionError(f"Не удалось запустить hysteria: {e}") from e
            if self.hysteria_process.wait_until_listening(port, timeout=8.0):
                self._log(f"[*] hysteria поднят, локальный SOCKS на :{port}"
                          + (" (со 2-й попытки)" if attempt > 1 else ""))
                return port
            last_tail = " | ".join(self.hysteria_process.recent_logs()[-5:])
            self.hysteria_process.stop()
            if attempt < attempts:
                self._log(f"[!] hysteria не поднялся (попытка {attempt}/{attempts}) "
                          f"— перезапускаю…")
                time.sleep(1.0)
        raise ConnectionError(
            f"hysteria-клиент не поднял локальный SOCKS-порт за 8 с "
            f"({attempts} попытки). Лог: {last_tail or '(пусто)'}"
        )

    def _install_geoip_ru_bypass(self, session, real, bypass_metric: int) -> None:
        """Pin kernel bypass routes for the whole geoip:ru IP space so RU
        traffic skips the TUN — but ONLY when the user enabled
        `route_ru_direct`.

        When it's OFF we touch ru_cidrs not at all: the user wants RU traffic
        to go THROUGH the VPN like everything else, and installing the bypass
        anyway would silently route the entire RU IP space around the tunnel.
        A partial direct/tunnel split across thousands of RU CIDRs is exactly
        what destabilises apps/CDNs with RU-hosted endpoints (Telegram), so the
        default-off behaviour matters. Cached list lives in
        %LOCALAPPDATA%\\KaproTUN\\geoip-ru.txt (main_window triggers download).
        Extracted from _connect_tun so the gate is testable without a real TUN.
        """
        if not bool(self.settings.get("route_ru_direct", False)):
            self._log("[*] geoip:ru-direct выключен — весь RU-трафик идёт через VPN")
            return
        ru_cidrs = geoip_ru.load_cidrs()
        if not ru_cidrs:
            self._log("[!] CIDR-список не закеширован — прямые RU-сайты с динамическими IP могут не работать")
            return
        self._log(f"[*] Добавляю {len(ru_cidrs)} CIDR'ов из geoip:ru…")
        t0 = time.time()
        added, adopted = session.add_bypass_cidrs(
            ru_cidrs, real.gateway, real.index, metric=bypass_metric)
        self._log(f"[*] geoip:ru за {time.time()-t0:.1f}с: {added} новых"
                  + (f", {adopted} уже было (подхвачены для очистки)" if adopted else "")
                  + " — локальный IP-блок идёт мимо TUN")

    def disconnect(self) -> None:
        # Order matters: stop TUN routing first so traffic stops hitting the
        # tunnel, then tear down processes, then restore system proxy, then
        # finally take down the kill-switch firewall block.
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
        # Stop the hysteria transport after xray (xray was chaining to it).
        # Idempotent — no-op if this wasn't a hy2 session.
        if self.hysteria_process.is_running():
            self.hysteria_process.stop()
        # v2.0.0: both processes that read the runtime configs are down now —
        # delete the on-disk xray/hysteria configs so the server UUID/password
        # doesn't linger at rest between sessions.
        leftover = paths.remove_runtime_configs()
        if leftover:
            self._log("[!] Не удалось удалить runtime-конфиги: "
                      f"{', '.join(leftover)} — они содержат секреты, "
                      "проверь права на папку данных")
        # Kill-switch teardown LAST — until now the firewall block is the
        # safety net if any step above leaves traffic in a weird state.
        # Safe to call even if it wasn't installed (idempotent). v2.0.0: a
        # failed firewall removal can strand the user's connectivity, so it's
        # surfaced to the log instead of swallowed.
        try:
            killswitch.remove()
        except Exception as e:
            self._log(f"[!] Kill-switch: не удалось снять firewall-правила: {e}")
        # Same idempotent teardown for the IPv6-leak block (v1.11.0).
        # Order doesn't matter relative to killswitch — both are
        # independent firewall rules with non-overlapping scopes
        # (kill-switch = all-IP outbound, ipv6_block = global v6 only).
        try:
            ipv6_block.remove()
        except Exception as e:
            self._log(f"[!] IPv6-block: не удалось снять правило: {e}")
        # v1.16.0: webrtc_block lives in the same firewall-rule family.
        # Independent scope from ipv6_block (v6 unicast vs UDP STUN
        # ports), no ordering concerns — both just need to be torn
        # down before we tell the user we're disconnected.
        try:
            webrtc_block.remove()
        except Exception as e:
            self._log(f"[!] WebRTC-block: не удалось снять правило: {e}")
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
        hy_port = self._maybe_start_hysteria(config)
        self._write_and_check(config, direct_domains, host, port,
                              hysteria_socks_port=hy_port)
        self._start_xray()
        # Kill-switch goes up RIGHT AFTER xray starts but BEFORE we
        # touch system proxy — so if proxy-set fails, the firewall is
        # already in place and a partially-broken setup can't leak.
        self._maybe_arm_killswitch()
        # v1.16.0: WebRTC leak protection. Especially critical in
        # HTTP-proxy mode because system proxy only catches TCP —
        # browser WebRTC STUN packets are UDP and would go straight
        # out the real NIC, exposing the real IP to any JavaScript.
        self._maybe_arm_webrtc_block()
        # v1.18.1: IPv6-leak protection in HTTP mode too. System proxy only
        # redirects TCP from proxy-aware apps over IPv4 — the OS keeps full
        # native IPv6, so a leak test (or any app) would expose the real v6
        # address. Same firewall block we use in TUN mode closes it. Needs
        # admin; if the user isn't elevated it skips with a clear warning.
        self._maybe_arm_ipv6_block()
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
                    "Перезапусти KaproTUN от имени администратора "
                    "(правый клик по ярлыку → «Запуск от имени администратора») "
                    "или переключи в Настройках режим на «HTTP-прокси»."
                )
            elif sys.platform == "darwin":
                msg = (
                    "TUN-режиму нужен root для создания utun-интерфейса и "
                    "настройки маршрутов.\n"
                    "Закрой KaproTUN и запусти из терминала:\n"
                    "    sudo /Applications/KaproTUN.app/Contents/MacOS/KaproTUN\n"
                    "Или переключи режим на «HTTP-прокси» — он не требует прав."
                )
            else:
                msg = (
                    "TUN-режиму нужен root для управления маршрутами.\n"
                    "Закрой KaproTUN и запусти через sudo / pkexec:\n"
                    "    pkexec ./KaproTUN-Linux-x64.AppImage\n"
                    "    (или sudo ./KaproTUN-Linux-x64.AppImage)\n"
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
        # hy2: bring up the local hysteria SOCKS before xray so xray can
        # chain to it. It connects to the server via the real route now;
        # the static route added below keeps its QUIC off the TUN once the
        # default route flips to the tunnel.
        hy_port = self._maybe_start_hysteria(config)
        self._write_and_check(config, direct_domains, host, port,
                              hysteria_socks_port=hy_port)
        self._start_xray()
        # Arm kill-switch before tun2socks comes up — same reasoning as
        # _connect_http: firewall block must exist before any traffic
        # is routed, so a mid-setup crash can't leak.
        self._maybe_arm_killswitch()
        # IPv6-leak protection — only relevant in TUN mode (HTTP-mode
        # is browser-only, browsers obey the system HTTP-proxy on v4 +
        # v6 alike, no separate v6 leak path). Same pre-tunnel timing
        # as kill-switch: rule must exist before any traffic flows.
        self._maybe_arm_ipv6_block()
        # v1.16.0: WebRTC leak protection. In TUN mode it's defence-in-
        # depth (STUN UDP is already tunnelled), but cheap and harmless
        # to add — protects against the case where the user accidentally
        # carved out a TUN-bypass route, or against malware running
        # outside the tunnel.
        self._maybe_arm_webrtc_block()

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
                    "Запусти KaproTUN через sudo и попробуй снова."
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
                elif rc == 5010:
                    # Same family as 183 but for a mismatched-proto entry
                    # (typically a /32 left over from a TUN adapter that
                    # died ungracefully). Our auto-recovery already tries
                    # both native + shell delete — if we still hit this
                    # the stale entry is glued in by something we can't
                    # touch from user-space. Reboot is the sure fix;
                    # `route -f` (flush all) usually works too.
                    hint = (" Windows вернул ERROR_OBJECT_ALREADY_EXISTS (5010) — "
                            "висит мёртвая запись от прошлого TUN-адаптера, и наш "
                            "delete-retry её не выгрыз. В админ-PowerShell: "
                            f"`route delete {server_ip}` (или перезагрузка снимет точно).")
                elif rc == 1314:
                    hint = (" Windows вернул ERROR_PRIVILEGE_NOT_HELD (1314). "
                            "Перезапусти KaproTUN от админа.")
                elif rc:
                    hint = f" (Windows rc={rc})"
                raise ConnectionError(
                    f"Не удалось добавить host-route для VPN-сервера ({server_ip})."
                    + hint
                )

            # Bypass DNS resolvers + big Russian service blocks (Yandex/VK
            # ranges) so DoH and CDN traffic doesn't loop through the tunnel.
            # If the user picked a non-system DNS in Settings, splice its IPs
            # in too — otherwise their DoH-over-port-443 would tunnel through
            # the VPN server uselessly (and the provider would see the VPN's
            # IP, not the user's).
            dns_opt = dns_options.get(str(self.settings.get("dns_option", "system")))
            bypass_list: list[tuple[str, str]] = list(_ALWAYS_BYPASS)
            existing_ips = {entry[0] for entry in bypass_list}
            for ip in dns_opt.bypass_ips:
                if ip not in existing_ips:
                    bypass_list.append((ip, "255.255.255.255"))
            added_always, adopted_always = session.add_bypass_cidrs(
                bypass_list, real.gateway, real.index, metric=bypass_metric,
            )
            self._log(f"[*] Always-bypass роуты (DNS + Yandex/VK): {added_always} новых"
                      + (f", {adopted_always} уже было (подхвачены для очистки)"
                         if adopted_always else ""))

            # Default route through TUN. Two /1 routes beat the existing
            # 0.0.0.0/0 by being more specific, so we use those instead of
            # touching the system default. Metric must be >= TUN interface's
            # own metric for the same reason as above.
            tun_metric = network_routes._get_interface_metric_v4(tun.index) + 1
            if not session.add_route("0.0.0.0", "128.0.0.0", TUN_GATEWAY, tun.index, metric=tun_metric):
                raise ConnectionError("Не удалось добавить маршрут 0.0.0.0/1 через TUN.")
            if not session.add_route("128.0.0.0", "128.0.0.0", TUN_GATEWAY, tun.index, metric=tun_metric):
                raise ConnectionError("Не удалось добавить маршрут 128.0.0.0/1 через TUN.")

            # DNS via TUN so resolution doesn't leak to ISP. If the user
            # picked a non-system DNS, use its plain IPv4 servers here so
            # the TUN adapter's resolver matches what xray uses internally;
            # otherwise fall back to the safe Yandex+Cloudflare default.
            tun_dns_servers = dns_opt.plain_servers if dns_opt.plain_servers else TUN_DNS
            session.set_dns(tun.name, tun_dns_servers)

            # v1.16.7 / v1.16.8: silence the physical NIC's DNS to prevent
            # Windows' Smart Multi-Homed Name Resolution from parallel-
            # querying the DHCP-assigned ISP DNS (MGTS / Beeline / etc)
            # alongside our TUN DNS. With physical NIC's DNS cleared,
            # the only DNS Windows can use is TUN's — which routes
            # through xray → hijack → upstream over VPN.
            #
            # Tied to the dns_leak_protection toggle (v1.16.8), not the
            # DNS option. User who turns protection OFF — usually because
            # they need Pi-hole / corporate / locally-pinned DNS to
            # actually answer — keeps the physical NIC's DNS intact.
            #
            # Session tracks the change so disconnect's cleanup restores
            # DHCP-source DNS automatically.
            if bool(self.settings.get("dns_leak_protection", True)):
                session.set_dns(real.name, [])  # empty = clear via address=none
                self._log("[*] DNS на физическом интерфейсе очищен "
                          "(защита от утечек: все запросы пойдут через TUN → "
                          "DoH-upstream через VPN)")

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
                added, adopted = session.add_bypass_routes(all_ips, real.gateway, real.index, metric=bypass_metric)
                self._log(f"[*] Bypass-роуты для direct-доменов: {added} новых"
                          + (f", {adopted} уже было (подхвачены)" if adopted else ""))

            # geoip:ru kernel bypass — gated on route_ru_direct. Extracted to
            # _install_geoip_ru_bypass so the gating is unit-testable without a
            # live TUN session. The curated direct-domain routes above are
            # independent and always applied.
            self._install_geoip_ru_bypass(session, real, bypass_metric)
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
                         host: str, port: int,
                         hysteria_socks_port: Optional[int] = None) -> str:
        dns_option = str(self.settings.get("dns_option", "system"))
        dns_leak_protection = bool(self.settings.get("dns_leak_protection", True))
        block_ads = bool(self.settings.get("block_ads", False))
        route_ru_direct = bool(self.settings.get("route_ru_direct", False))
        try:
            path = xray_config.write_config(
                config, direct_domains, host, port,
                dns_option=dns_option,
                dns_leak_protection=dns_leak_protection,
                block_ads=block_ads,
                route_ru_direct=route_ru_direct,
                hysteria_socks_port=hysteria_socks_port,
            )
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

    def _maybe_arm_killswitch(self) -> None:
        """If user enabled kill-switch in settings, install firewall rules.

        Silent no-op when:
          - Setting is off
          - Not on Windows (other OSes not supported yet)
          - Not running as admin (rule install would fail anyway)

        We DON'T raise on failure — kill-switch is defence-in-depth, the
        connection itself works either way. A `[!] Kill-switch не
        активирован` log line is enough signal.
        """
        if not self.settings.get("kill_switch", False):
            return
        if not killswitch.is_supported():
            self._log("[!] Kill-switch пока работает только на Windows")
            return
        if not admin.is_admin():
            self._log("[!] Kill-switch требует админа — пропускаю")
            return
        xray_exe = paths.xray_exe()
        # Hysteria2 sessions egress via hysteria.exe (xray chains through it),
        # so the kill-switch must allow it too or block-all kills the
        # transport. By the time we arm the switch, hysteria is already up for
        # a hy2 session — so its running state is the signal. Non-hy2 sessions
        # pass None and don't widen the allow-list.
        hy_exe = paths.hysteria_exe() if self.hysteria_process.is_running() else None
        if killswitch.install(xray_exe, hy_exe):
            self._log("[*] Kill-switch активирован (firewall блок весь трафик "
                      + ("мимо xray + hysteria)" if hy_exe else "мимо xray)"))
        else:
            self._log("[!] Не удалось установить firewall-правила kill-switch "
                      "— продолжаю без него")

    def _maybe_arm_ipv6_block(self) -> None:
        """Install the global-unicast IPv6 block if the user enabled
        IPv6-leak protection. Armed in BOTH modes (v1.18.1):

          - TUN mode tunnels only IPv4, so native v6 would bypass the
            tunnel entirely.
          - HTTP-proxy mode only redirects TCP from proxy-aware apps over
            IPv4 — the OS keeps full native IPv6, so a leak test (or any
            app) sees the real v6 address. The same firewall block closes
            both. (Earlier builds left this TUN-only, which is the IPv6
            leak users hit in the default HTTP mode.)

        Needs admin (netsh advfirewall) — same silent-skip conditions as
        _maybe_arm_killswitch. HTTP mode often runs un-elevated, so when we
        can't install we say plainly that v6 may leak and point at TUN /
        running as admin, rather than pretending it's protected. Not
        raising on failure — the v4 path works either way.
        """
        if not self.settings.get("ipv6_leak_protection", True):
            return
        if not ipv6_block.is_supported():
            self._log("[!] IPv6-leak protection пока работает только на Windows")
            return
        if not admin.is_admin():
            self._log("[!] Защита от IPv6-утечек требует прав администратора — "
                      "IPv6 может утекать мимо туннеля. Запусти KaproTUN от "
                      "имени администратора или используй TUN-режим.")
            return
        if ipv6_block.install():
            # Verify it actually took effect. On some systems the netsh add
            # "succeeds" but the IPv6 rule is inert (3rd-party firewall, or
            # IPv6 filtering disabled) — that's the "protection ON but still
            # leaks" report. Better to warn loudly than to leak silently.
            if ipv6_block.probe_ipv6_reachable():
                self._log("[!] IPv6-block правило добавлено, но IPv6 ВСЁ ЕЩЁ "
                          "доступен — правило не сработало в этой системе "
                          "(сторонний firewall / фильтрация IPv6 отключена?). "
                          "Возможна утечка IPv6 — проверь «Тест утечек».")
            else:
                self._log("[*] IPv6-leak protection активирована "
                          "(блок outbound к 2000::/3)")
        else:
            self._log("[!] Не удалось установить IPv6-block firewall-правило "
                      "— v6-трафик может утечь мимо туннеля. netsh: "
                      + (ipv6_block.last_install_output() or "(нет вывода)"))

    def _maybe_arm_webrtc_block(self) -> None:
        """If user enabled WebRTC-leak protection, install the STUN-block
        firewall rule. Both HTTP and TUN modes call this — leak vector
        is identical (browser opens UDP socket to STUN server, server
        echoes back real IP, JavaScript reads it via RTCPeerConnection).

        Same silent-skip conditions as the other firewall arming:
        setting off, non-Windows platform, no admin privileges. Logged
        but never raised — protection is defence-in-depth, the tunnel
        works fine without it.
        """
        if not self.settings.get("webrtc_leak_protection", True):
            return
        if not webrtc_block.is_supported():
            self._log("[!] WebRTC-leak protection пока работает только на Windows")
            return
        if not admin.is_admin():
            # In HTTP-proxy mode we usually aren't admin (don't need it
            # for system_proxy on Windows). Don't spam this — log once
            # at info level so the user knows why protection is off.
            self._log("[!] WebRTC-leak protection требует админа — пропускаю "
                      "(перейди в TUN-режим для админ-прав)")
            return
        if webrtc_block.install():
            self._log("[*] WebRTC-leak protection активирована "
                      "(блок UDP к STUN-портам 3478/5349/19302/19305-19308)")
        else:
            self._log("[!] Не удалось установить WebRTC-block firewall-правило "
                      "— браузер может узнать реальный IP через STUN")

    def _atexit_cleanup(self) -> None:
        try:
            self.disconnect()
        except Exception:
            pass
