"""Connection controller: ties together config generation, Xray-core, tun2socks, and system proxy."""
from __future__ import annotations

import atexit
import socket
import sys
import time
from typing import Callable, Optional

from . import (
    admin, app_log, dns_options, dns_health, geoip_ru, hysteria_installer,
    hysteria_process, ipv6_block, killswitch, paths, proc_stats, storage,
    tun_recovery, webrtc_block, system_proxy, xray_config,
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
from .sing_box_process import SingBoxProcess, is_benign_noise
from . import sing_box_config, sing_box_installer
if sys.platform == "win32":
    from . import network_routes
else:
    from . import network_routes_unix as network_routes


class ConnectionError(Exception):
    pass


# Connection modes
MODE_HTTP_PROXY = "http"   # Browser-only, sets Windows system HTTP proxy. No admin needed.
MODE_TUN = "tun"           # System-wide TUN tunnel. Needs admin. Works for all apps incl. Telegram, Steam.

# TUN dataplane engines (v3.0.0)
ENGINE_SING_BOX = "sing_box_tun"            # primary: native TUN, no tun2socks bridge
ENGINE_CLASSIC = "classic_xray_tun2socks"   # legacy fallback: xray + tun2socks


def resolve_engine(value) -> str:
    """Normalise the tun_engine setting → a known engine id. Unknown/empty →
    the sing-box default (this is also the migration for old settings that
    predate the engine choice)."""
    return ENGINE_CLASSIC if str(value or "") == ENGINE_CLASSIC else ENGINE_SING_BOX


class UnsupportedBySingBox(sing_box_config.UnsupportedBySingBox):
    """Re-export so callers can catch it from the controller namespace."""


# TUN-side IPs — these live on the virtual interface, not on any real network
TUN_LOCAL_ADDR = "10.255.0.2"
TUN_GATEWAY = "10.255.0.1"
TUN_MASK = "255.255.255.0"
# TUN-adapter resolvers when leak protection is OFF — DNS goes DIRECT, so a
# Russian-fast resolver first (Yandex), Cloudflare as fallback. These are also
# in _DNS_RESOLVER_BYPASS so the queries leave via the physical NIC. When leak
# protection is ON we DON'T use this list (see _LEAK_PROTECTED_TUN_DNS): DNS
# must tunnel, so we use diverse upstreams that are NOT bypassed.
TUN_DNS = ["77.88.8.8", "1.1.1.1"]

# TUN-adapter resolvers when leak protection is ON — DNS rides the tunnel, so
# these MUST be servers we deliberately route through it (via xray's :53
# carve-out), NOT bypassed. Three different operators (Cloudflare/Google/Quad9)
# for failover; same list xray's dns block + carve-out use, so the OS and xray
# agree. None sit inside the always-bypassed RU service blocks below.
_LEAK_PROTECTED_TUN_DNS = list(dns_options.LEAK_PROTECTED_SYSTEM_UPSTREAMS)

# Public DNS-resolver host routes. Pinning these to the physical NIC means DNS
# queries to them go DIRECT — correct ONLY when leak protection is OFF. With
# leak protection ON these MUST NOT be installed: a /32 here would send the
# OS's plaintext UDP/53 query straight out the physical NIC (an ISP-visible DNS
# leak that defeats the whole feature) and would also steal the queries away
# from the tunnelled carve-out. So this set is now applied conditionally.
# Each entry: (dest_or_network, mask). For a /32 host-route use 255.255.255.255.
_DNS_RESOLVER_BYPASS: list[tuple[str, str]] = [
    ("77.88.8.8",  "255.255.255.255"),  # Yandex Public DNS (basic)
    ("77.88.8.1",  "255.255.255.255"),  # Yandex Public DNS (basic, secondary)
    ("77.88.8.88", "255.255.255.255"),  # Yandex Safe DNS
    ("77.88.8.7",  "255.255.255.255"),  # Yandex Family DNS
    ("1.1.1.1",    "255.255.255.255"),  # Cloudflare
    ("1.0.0.1",    "255.255.255.255"),  # Cloudflare secondary
    ("8.8.8.8",    "255.255.255.255"),  # Google
    ("8.8.4.4",    "255.255.255.255"),  # Google secondary
]

# Big Russian service-provider blocks (Yandex / VK / Mail.ru / CDN). Routing
# these direct keeps RU services reachable from a Russian IP and off the
# tunnel. Applied in BOTH leak modes — none of these ranges contain the
# leak-protected upstreams (1.1.1.1 / 8.8.8.8 / 9.9.9.9), so they don't clash
# with tunnelled DNS. (Note: 77.88.0.0/18 DOES contain Yandex DNS, which is why
# the leak-protected resolver set above deliberately avoids Yandex IPs.)
_SERVICE_BYPASS: list[tuple[str, str]] = [
    # --- Yandex service blocks (AS13238) — DoH, search, maps, mail, disk,
    # music, taxi, eda, yastatic, etc.
    ("5.45.192.0",     "255.255.248.0"),  # /21
    ("5.255.192.0",    "255.255.240.0"),  # /20
    ("77.88.0.0",      "255.255.192.0"),  # /18  (Yandex DNS lives in here)
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

# Back-compat alias — the full unconditional set (used only in the
# leak-protection-OFF path, where direct DNS is intended).
_ALWAYS_BYPASS: list[tuple[str, str]] = _DNS_RESOLVER_BYPASS + _SERVICE_BYPASS

# Private / LAN / Docker / link-local / loopback ranges that must ALWAYS stay
# off the TUN (v2.2.0), in EITHER leak mode. Routed direct via the physical
# gateway so they never fall into the 0.0.0.0/1 TUN catch-all.
#
# Why this matters: traffic to an otherwise-unrouted private dest — e.g. a
# Windows Delivery-Optimization peer on a Docker/WSL subnet like 172.19.2.109,
# or any RFC1918 host with no on-link route — would hit the TUN catch-all, and
# tun2socks opens a FRESH 127.0.0.1:<socks> socket per flow → ephemeral-port
# exhaustion ("Only one usage of each socket address") + handle blow-up, which
# the memory watchdog then mistook for a leak and reconnect-looped.
#
# Safety: each /8../16 is LESS specific than a real on-link subnet route (your
# 192.168.x /24, Docker's 172.19.x /16, the TUN's own 10.255.0.0/24), so
# genuine LAN/Docker/TUN traffic keeps using its own adapter; only unrouted
# private dests get sent to the physical gw, which drops them instead of
# flooding the loopback. The VPN-server host-route (/32) is separate and
# unaffected.
_PRIVATE_BYPASS: list[tuple[str, str]] = [
    ("10.0.0.0",    "255.0.0.0"),     # RFC1918 /8
    ("172.16.0.0",  "255.240.0.0"),   # RFC1918 /12  (Docker/WSL 172.19.x lives here)
    ("192.168.0.0", "255.255.0.0"),   # RFC1918 /16
    ("169.254.0.0", "255.255.0.0"),   # link-local /16
    ("127.0.0.0",   "255.0.0.0"),     # loopback /8
]


# Runaway-resource guard thresholds (v2.1.7 — two tiers).
#
# MODERATE: above healthy use; heal on a cooldown so a slow climb doesn't
#   thrash the connection.
# CRITICAL: near the levels that wedge the machine (the reported 4.7 GB / 38k
#   handles / 900 threads); heal IMMEDIATELY, bypassing the cooldown.
#
# tun2socks triggers on memory OR handles OR threads (each a facet of the
# UDP/session storm). xray triggers on memory OR a HIGH handle count (its 66k
# in the report); its threads aren't a reliable fault signal so they're logged
# but not gated on.
# v2.1.9: raised ABOVE the real idle baseline. Live data showed tun2socks
# sitting at ~1.9 GB "private bytes" seconds after a fresh connect — that's a
# baseline (Go/gVisor reserves address space Windows counts as private), NOT a
# runaway, so the old 1.8 GB moderate bar fired on every healthy session and
# the client reconnect-looped. Bars now sit between that baseline and the
# observed runaway (tun2socks ~4.7 GB / ~38k handles / ~900 threads, xray
# ~2.3 GB / ~66k handles). The GUI watchdog ALSO requires a post-connect grace
# period + a sustained breach, so a stable baseline can never trip a heal.
MEM_TUN2SOCKS_MOD_BYTES = 3_200_000_000       # ~3.0 GiB (idle ~1.9 GB; safe gap)
MEM_TUN2SOCKS_MOD_HANDLES = 20_000
MEM_TUN2SOCKS_MOD_THREADS = 700
MEM_TUN2SOCKS_CRIT_BYTES = 4_300_000_000      # ~4.0 GiB (runaway ~4.7 GB)
MEM_TUN2SOCKS_CRIT_HANDLES = 33_000
MEM_TUN2SOCKS_CRIT_THREADS = 1_000

MEM_XRAY_MOD_BYTES = 3_200_000_000            # xray rarely trips on bytes…
MEM_XRAY_MOD_HANDLES = 45_000                 # …handles are its real signal
MEM_XRAY_CRIT_BYTES = 4_300_000_000
MEM_XRAY_CRIT_HANDLES = 60_000                # runaway ~66k → critical

# Back-compat aliases (kept so any external reference still resolves).
MEM_HEAL_TUN2SOCKS_BYTES = MEM_TUN2SOCKS_MOD_BYTES
MEM_HEAL_TUN2SOCKS_HANDLES = MEM_TUN2SOCKS_MOD_HANDLES
MEM_HEAL_XRAY_BYTES = MEM_XRAY_MOD_BYTES


def mem_heal_decision(severity: Optional[str], now: float, last_heal_ts: float,
                      heal_count: int, *, max_heals: int = 4,
                      cooldown_s: float = 180.0, escalate_after: int = 2) -> dict:
    """Pure decision for the memory watchdog — no I/O, fully unit-testable.

    Returns {do_heal, exhausted, escalate_economy}:
      * critical severity heals IMMEDIATELY (ignores cooldown);
      * moderate heals only once the cooldown since last_heal_ts elapsed;
      * once heal_count reaches max_heals we stop (exhausted) — no endless loop;
      * from the escalate_after-th heal onward we ask the caller to drop the
        performance preset to 'economy' so the next reconnect uses the smallest
        buffers + shortest UDP timeout.
    """
    if not severity:
        return {"do_heal": False, "exhausted": False, "escalate_economy": False}
    if heal_count >= max_heals:
        return {"do_heal": False, "exhausted": True, "escalate_economy": False}
    if severity == "critical":
        do_heal = True
    elif severity == "moderate":
        do_heal = (now - last_heal_ts) >= cooldown_s
    else:
        do_heal = False
    escalate = do_heal and (heal_count + 1) >= escalate_after
    return {"do_heal": do_heal, "exhausted": False, "escalate_economy": escalate}


def mem_exhausted_action(severity: Optional[str]) -> dict:
    """What to do once the heal budget is spent. A CRITICAL runaway must NOT be
    left running — a tun2socks/xray sitting at 3-4 GB can wedge or crash the
    whole client — so force a clean emergency shutdown of the helpers. A
    moderate runaway is survivable, so we leave it and ask the user to act.
    Pure + testable."""
    return {"force_shutdown": severity == "critical"}


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
        # v3.0.0 primary TUN engine — single native-TUN process, no bridge.
        self.sing_box_process = SingBoxProcess(
            on_log=(lambda l: on_log(f"[sing-box] {l}")) if on_log else None,
        )
        self.settings = storage.load_settings()
        self._saved_proxy_state: Optional[dict] = None
        self._route_session: Optional[network_routes.RouteSession] = None
        self._active: Optional[ProxyConfig] = None
        # Which TUN engine the live session is using (None when disconnected).
        self._active_engine: Optional[str] = None
        # Once-per-app-launch guard so the "ad-block is legacy-only" notice
        # isn't logged on every sing-box reconnect.
        self._singbox_adblock_noted = False
        # Belt-and-braces: if Python exits uncleanly with TUN routes active,
        # the user's network is broken until reboot. Best-effort cleanup here.
        atexit.register(self._atexit_cleanup)

    def _log(self, msg: str) -> None:
        # Mirror every controller diagnostic to the on-disk app.log (redacted),
        # so a hang/crash leaves a trail beyond the in-memory Logs page.
        app_log.log(msg)
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
        # Clean disconnect: restore() above already put the physical NIC's DNS
        # back, so the crash-recovery journal has nothing left to undo. Drop it
        # — its presence on next startup must mean "a session died uncleanly".
        tun_recovery.clear()
        if self.tun_process.is_running():
            self.tun_process.stop()
        # sing-box engine: stop the single native-TUN process. It removes the
        # routes it added (auto_route) on clean shutdown; remove_runtime_configs
        # below deletes its credential-bearing runtime config.
        if self.sing_box_process.is_running():
            self.sing_box_process.stop()
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
        self._active_engine = None

    def is_connected(self) -> bool:
        # Classic TUN: tun_process + xray. HTTP: xray only. sing-box TUN:
        # the single sing-box process.
        return (self.process.is_running()
                or self.tun_process.is_running()
                or self.sing_box_process.is_running())

    def active_config(self) -> Optional[ProxyConfig]:
        return self._active if self.is_connected() else None

    def update_settings(self, **changes) -> None:
        self.settings.update(changes)
        storage.save_settings(self.settings)

    def current_mode(self) -> str:
        return self.settings.get("mode", MODE_HTTP_PROXY)

    def tun_dns_guarded(self) -> bool:
        """True when a live TUN session owns the system's DNS path — i.e. the
        case where a DNS outage means a broken tunnel rather than a normal
        app-level hiccup, so the runtime watchdog should heal it.

        sing-box owns the system DNS in BOTH leak modes: its TUN hijacks all
        :53, so even with leak protection OFF a failing resolver means the
        TUN/DNS path is broken (the v3.0.5 'files.oaiusercontent.com / Yandex
        images hang' class) — guard it regardless of the leak setting. The
        classic engine only clears the physical resolver under leak protection,
        so there it stays gated on dns_leak_protection.
        """
        if not (self.is_connected() and self.current_mode() == MODE_TUN):
            return False
        if self.current_engine() == ENGINE_SING_BOX:
            return True
        return bool(self.settings.get("dns_leak_protection", True))

    # --- runtime memory watchdog (v2.1.6) ---------------------------------

    def sample_runtime_stats(self) -> dict:
        """Best-effort {name: ProcSample|None} for the helper processes. Cheap
        (a psutil read per pid); never raises. Includes the sing-box process so
        the [mem] diagnostic line covers the v3 engine too — but note the
        memory-pressure heal (see memory_pressure_reason) intentionally does NOT
        act on sing-box: it's a single native-TUN process without the loopback
        UDP-session storm the tun2socks heal was built for."""
        return {
            "tun2socks": proc_stats.sample(self.tun_process.pid()),
            "xray": proc_stats.sample(self.process.pid()),
            "sing-box": proc_stats.sample(self.sing_box_process.pid()),
        }

    def format_runtime_stats(self, stats: Optional[dict] = None) -> str:
        """One diagnostic line: memory, handles, threads, pid, uptime per
        helper process + the active preset. ASCII-only numbers so it's safe to
        write anywhere (app.log, Logs page, console). Only processes that are
        actually running (non-None sample) are shown, so a sing-box session
        doesn't print 'tun2socks: н/д' noise and vice-versa."""
        if stats is None:
            stats = self.sample_runtime_stats()
        preset = str(self.settings.get("performance_preset", "balanced"))
        pids = {
            "tun2socks": self.tun_process.pid(),
            "xray": self.process.pid(),
            "sing-box": self.sing_box_process.pid(),
        }
        now = time.time()
        parts = []
        names = [n for n in ("tun2socks", "xray", "sing-box")
                 if stats.get(n) is not None] or ["tun2socks", "xray"]
        for name in names:
            s = stats.get(name)
            if s is None:
                parts.append(f"{name}: н/д")
                continue
            seg = f"{name}: {proc_stats.human_bytes(s.private_bytes)}"
            if s.handles:
                seg += f", {s.handles} хэндлов"
            if s.threads:
                seg += f", {s.threads} потоков"
            if pids.get(name):
                seg += f", pid {pids[name]}"
            if s.create_time:
                up = max(0, int(now - s.create_time))
                seg += f", up {up // 60}м{up % 60:02d}с"
            parts.append(seg)
        return f"[mem] preset={preset}; " + "; ".join(parts)

    def memory_pressure_reason(self, stats: Optional[dict] = None):
        """Classify runaway. Returns (severity, reason) with severity in
        {'critical','moderate'}, or None when healthy. CRITICAL is checked
        first (immediate heal); each helper trips on memory OR handles OR (for
        tun2socks) threads — different facets of the UDP/session storm. Pure
        threshold logic, unit-testable with synthetic ProcSamples."""
        if stats is None:
            stats = self.sample_runtime_stats()
        t = stats.get("tun2socks")
        x = stats.get("xray")
        hb = proc_stats.human_bytes

        # --- CRITICAL (heal immediately, bypass cooldown) ---
        if t is not None:
            if t.private_bytes >= MEM_TUN2SOCKS_CRIT_BYTES:
                return ("critical", f"tun2socks {hb(t.private_bytes)} >= {hb(MEM_TUN2SOCKS_CRIT_BYTES)} (критично)")
            if t.handles >= MEM_TUN2SOCKS_CRIT_HANDLES:
                return ("critical", f"tun2socks {t.handles} хэндлов >= {MEM_TUN2SOCKS_CRIT_HANDLES} (критично)")
            if t.threads >= MEM_TUN2SOCKS_CRIT_THREADS:
                return ("critical", f"tun2socks {t.threads} потоков >= {MEM_TUN2SOCKS_CRIT_THREADS} (критично, UDP-шторм)")
        if x is not None:
            if x.private_bytes >= MEM_XRAY_CRIT_BYTES:
                return ("critical", f"xray {hb(x.private_bytes)} >= {hb(MEM_XRAY_CRIT_BYTES)} (критично)")
            if x.handles >= MEM_XRAY_CRIT_HANDLES:
                return ("critical", f"xray {x.handles} хэндлов >= {MEM_XRAY_CRIT_HANDLES} (критично)")

        # --- MODERATE (heal on cooldown) ---
        if t is not None:
            if t.private_bytes >= MEM_TUN2SOCKS_MOD_BYTES:
                return ("moderate", f"tun2socks {hb(t.private_bytes)} >= {hb(MEM_TUN2SOCKS_MOD_BYTES)}")
            if t.handles >= MEM_TUN2SOCKS_MOD_HANDLES:
                return ("moderate", f"tun2socks {t.handles} хэндлов >= {MEM_TUN2SOCKS_MOD_HANDLES}")
            if t.threads >= MEM_TUN2SOCKS_MOD_THREADS:
                return ("moderate", f"tun2socks {t.threads} потоков >= {MEM_TUN2SOCKS_MOD_THREADS}")
        if x is not None:
            if x.private_bytes >= MEM_XRAY_MOD_BYTES:
                return ("moderate", f"xray {hb(x.private_bytes)} >= {hb(MEM_XRAY_MOD_BYTES)}")
            if x.handles >= MEM_XRAY_MOD_HANDLES:
                return ("moderate", f"xray {x.handles} хэндлов >= {MEM_XRAY_MOD_HANDLES}")
        return None

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

    def _free_tun_device(self) -> None:
        """Force-kill orphan TUN helpers (sing-box / tun2socks) left by a
        crashed/force-closed prior run, so the shared "KaproTun" adapter is free.

        BOTH engines name the device "KaproTun"; a leftover sing-box.exe OR
        tun2socks.exe still owning that adapter makes the next start die with
        "configure tun interface: Cannot create a file when that file already
        exists." Called from the TUN connect path, where connect() has already
        verified THIS controller has no live session — so anything matching is an
        orphan. Best-effort; never raises.
        """
        if self.is_connected():
            return  # never touch our own live helpers
        import subprocess
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        killed = False
        targets = (("sing-box.exe", "tun2socks.exe") if sys.platform == "win32"
                   else ("sing-box", "tun2socks"))
        for name in targets:
            try:
                if sys.platform == "win32":
                    r = subprocess.run(["taskkill", "/F", "/IM", name],
                                       capture_output=True, timeout=3,
                                       creationflags=no_window)
                else:
                    r = subprocess.run(["pkill", "-9", "-x", name],
                                       capture_output=True, timeout=3)
                if r.returncode == 0:
                    killed = True
            except (OSError, subprocess.SubprocessError):
                pass
        if killed:
            # The OS removes the WinTUN/utun adapter when the holder exits, but
            # async — give it a moment so the next create() doesn't race it.
            self._log("[*] Освобождаю TUN-устройство «KaproTun» от орфанных "
                      "процессов прошлого запуска…")
            time.sleep(1.0)

    def _connect_tun(self, config: ProxyConfig, direct_domains: list[str]) -> None:
        """Dispatch to the selected TUN engine (v3.0.0). sing-box is the
        default; classic xray+tun2socks is the legacy fallback. An unsupported
        config raises a clear error suggesting legacy — NEVER a silent switch.

        NOTE: we deliberately do NOT proactively kill orphan TUN helpers here.
        The startup orphan-killer (main._kill_orphan_helpers, runs once when we
        are the only instance per the single-instance guard) handles
        cross-session leftovers; a same-session collision is handled REACTIVELY
        in _connect_tun_sing_box (kill + retry only when the start actually fails
        with "...already exists"). A blind global kill on every connect could,
        if two instances ever co-existed, let the second kill the first's live
        sing-box and start a reconnect storm — exactly what we must avoid (v3.0.7).
        """
        engine = resolve_engine(self.settings.get("tun_engine"))
        if engine == ENGINE_SING_BOX:
            try:
                self._connect_tun_sing_box(config, direct_domains)
            except sing_box_config.UnsupportedBySingBox as e:
                raise ConnectionError(
                    f"{e}\n\nЭтот конфиг пока не поддержан движком sing-box. "
                    f"Переключи движок на «Legacy (Xray + tun2socks)» в "
                    f"Настройках и подключись снова."
                ) from e
            return
        self._connect_tun_classic(config, direct_domains)

    def _connect_tun_classic(self, config: ProxyConfig, direct_domains: list[str]) -> None:
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

        # Step 1: resolve the VPN-server hostname to an IP FIRST. We need it
        # both for the loop-prevention host-route below AND to pick the egress
        # interface bound to the actual path to the server.
        server_host = str(config.outbound.get("server", "")).strip()
        if not server_host:
            raise ConnectionError("В конфиге нет адреса сервера.")
        try:
            server_ip = socket.gethostbyname(server_host)
        except socket.gaierror as e:
            raise ConnectionError(f"Не удалось резолвнуть VPN-сервер «{server_host}»: {e}") from e

        # Step 2: snapshot the real egress (interface + gateway) BEFORE we add
        # TUN routes — bound to the route the OS uses TO THE SERVER, not just
        # "the first 0.0.0.0/0". On multi-NIC boxes (Ethernet + Wi-Fi with
        # different gateways) or with a virtual adapter holding a stale default
        # route, the latter picks the wrong interface and the tunnel blackholes
        # ("подключено, но трафика нет"). find_egress_to() asks Windows which
        # path actually reaches the server; get_default_route_v4() is the
        # effective-metric fallback if that lookup is unavailable.
        real = (network_routes.find_egress_to(server_ip)
                or network_routes.get_default_route_v4())
        if real is None or not real.gateway or not real.index:
            raise ConnectionError(
                "Не удалось определить шлюз до VPN-сервера. "
                "Возможно, нет активного интернет-соединения."
            )
        self._log(f"[*] Egress к серверу: {real.name} (gw {real.gateway})")

        # Step 3: write + validate + start xray (its SOCKS5 inbound is what
        # tun2socks forwards into).
        host = "127.0.0.1"
        port = int(self.settings.get("listen_port", 2080))
        # hy2: bring up the local hysteria SOCKS before xray so xray can
        # chain to it. It connects to the server via the real route now;
        # the static route added below keeps its QUIC off the TUN once the
        # default route flips to the tunnel.
        hy_port = self._maybe_start_hysteria(config)
        # TUN mode: bind xray's `direct`/freedom outbound to the physical NIC
        # (real.name) so direct traffic exits the real interface and can never
        # loop back into the TUN. No-op in HTTP mode. v2.2.1.
        self._write_and_check(config, direct_domains, host, port,
                              hysteria_socks_port=hy_port,
                              egress_interface=real.name)
        # Remember where xray's error log ends right now, so the connect-time
        # liveness check can scan ONLY this session's lines for REALITY/transport
        # failures (the log is appended across runs). v2.1.5.
        log_offset = self._xray_log_size()
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
            self.tun_process.start(
                socks_addr=f"{host}:{port + 1}",
                buffer_preset=str(self.settings.get("performance_preset", "balanced")),
            )
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

            # Bypass routes — what stays OFF the tunnel and goes direct.
            #
            # v2.1.5 fixes a real conflict: the public DNS-resolver host-routes
            # used to be installed UNCONDITIONALLY. With leak protection ON that
            # meant the OS's :53 query to e.g. 1.1.1.1 left in plaintext via the
            # physical NIC — an ISP-visible DNS leak — AND it stole the query
            # from the tunnelled carve-out. So:
            #   leak ON  -> only the RU service blocks go direct; the resolvers
            #               are NOT bypassed (DNS rides the tunnel, no leak).
            #   leak OFF -> legacy: resolvers + service blocks + the chosen
            #               option's IPs all go direct (DNS is meant to be
            #               direct in this mode — no behaviour change).
            dns_opt = dns_options.get(str(self.settings.get("dns_option", "system")))
            leak = bool(self.settings.get("dns_leak_protection", True))
            # Private / LAN / Docker / link-local ALWAYS bypass the TUN (v2.2.0),
            # in either leak mode — this is what keeps 172.19.x-style private
            # dests out of the loopback flood that caused socket exhaustion.
            bypass_list: list[tuple[str, str]] = list(_PRIVATE_BYPASS)
            if leak:
                bypass_list += list(_SERVICE_BYPASS)
                self._log("[*] Защита DNS включена: публичные резолверы НЕ "
                          "байпасятся — DNS идёт в туннель (без утечки на ISP).")
            else:
                bypass_list += list(_ALWAYS_BYPASS)
                existing_ips = {entry[0] for entry in bypass_list}
                for ip in dns_opt.bypass_ips:
                    if ip not in existing_ips:
                        bypass_list.append((ip, "255.255.255.255"))
            added_always, adopted_always = session.add_bypass_cidrs(
                bypass_list, real.gateway, real.index, metric=bypass_metric,
            )
            self._log(f"[*] Bypass-роуты (приватные/LAN + "
                      f"{'сервисы РФ' if leak else 'DNS + сервисы РФ'}): "
                      f"{added_always} новых"
                      + (f", {adopted_always} уже было (подхвачены для очистки)"
                         if adopted_always else ""))

            # Direct-list bypass routes — resolve the curated direct domains and
            # pin /32 routes for their IPs via the real gateway, so that traffic
            # dodges the TUN (the same AmneziaVPN trick; also breaks the
            # freedom->kernel->TUN->xray->freedom loop). v2.1.5: do this NOW,
            # while the default route is still the physical NIC, so resolution
            # uses the real (direct) DNS path — NOT after the /1 TUN routes flip
            # the default, which (with leak protection's resolvers no longer
            # bypassed) would force this resolution through the tunnel and make
            # it fail whenever the tunnel is slow to warm up.
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

            # Default route through TUN. Two /1 routes beat the existing
            # 0.0.0.0/0 by being more specific, so we use those instead of
            # touching the system default. Metric must be >= TUN interface's
            # own metric for the same reason as above. AFTER this the default
            # egress is the tunnel, so anything resolved/added above had to
            # happen first.
            tun_metric = network_routes._get_interface_metric_v4(tun.index) + 1
            if not session.add_route("0.0.0.0", "128.0.0.0", TUN_GATEWAY, tun.index, metric=tun_metric):
                raise ConnectionError("Не удалось добавить маршрут 0.0.0.0/1 через TUN.")
            if not session.add_route("128.0.0.0", "128.0.0.0", TUN_GATEWAY, tun.index, metric=tun_metric):
                raise ConnectionError("Не удалось добавить маршрут 128.0.0.0/1 через TUN.")

            # DNS on the TUN adapter. Match what xray uses internally:
            #   - named option  -> its plain IPv4 servers (both leak modes)
            #   - system + leak -> the diverse tunnelled upstreams (3 operators,
            #     failover; NOT bypassed, so they ride the tunnel)
            #   - system + no   -> the legacy Yandex+Cloudflare direct default
            # Listing several servers lets the OS resolver itself fail over
            # between them, so one provider being unreachable doesn't kill
            # resolution — the core of the "no single-DNS dependency" fix.
            if dns_opt.plain_servers:
                tun_dns_servers = list(dns_opt.plain_servers)
            elif leak:
                tun_dns_servers = list(_LEAK_PROTECTED_TUN_DNS)
            else:
                tun_dns_servers = list(TUN_DNS)
            session.set_dns(tun.name, tun_dns_servers)
            self._log("[*] DNS на TUN-адаптере: " + ", ".join(tun_dns_servers)
                      + (" (через туннель, с failover)" if leak else " (прямой)"))

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
            dns_cleared = leak  # same dns_leak_protection axis, computed above
            if dns_cleared:
                # Journal the interface BEFORE we clear its DNS, so a crash
                # while connected can be undone on the next startup (recover()
                # restores DHCP). Best-effort: a failed journal write still
                # lets the connect proceed (the in-session health-check below
                # and disconnect's restore() remain the primary safety nets).
                if not tun_recovery.mark(real.name, real.index):
                    self._log("[!] Не удалось записать журнал восстановления TUN "
                              "(восстановление после аварийного выхода может не "
                              "сработать) — продолжаю.")
                session.set_dns(real.name, [])  # empty = clear via address=none
                self._log(f"[*] DNS на физическом интерфейсе «{real.name}» "
                          f"(ifIndex {real.index}) очищен — все запросы пойдут "
                          f"через TUN → DoH-upstream через VPN.")

            # (Direct-list bypass routes are installed earlier — before the /1
            # TUN routes — so resolution happens over the still-direct path.)

            # geoip:ru kernel bypass — gated on route_ru_direct. Extracted to
            # _install_geoip_ru_bypass so the gating is unit-testable without a
            # live TUN session. The curated direct-domain routes above are
            # independent and always applied.
            self._install_geoip_ru_bypass(session, real, bypass_metric)

            # Liveness gate (v2.1.4 → strengthened v2.1.5). Starting the
            # processes is NOT proof the tunnel carries traffic: REALITY can be
            # failing its handshake (the "received real certificate" errors),
            # the server can be down, or the upstream DNS unreachable. With the
            # physical DNS cleared, that lands the user in the worst state —
            # "connected" but nothing resolves. Verify the tunnel is REALLY
            # alive before committing; on failure, fall through to the except
            # below (restores DNS/routes/proxy, stops the processes, clears the
            # journal) and surface a specific, actionable error instead of a
            # silently-broken connection. Runs in BOTH leak modes now.
            self._verify_tunnel_or_raise(host, port, dns_cleared, log_offset)
        except Exception:
            session.restore()
            # DNS/routes are back; the recovery journal would otherwise make the
            # next startup think this run crashed mid-session, so drop it now.
            tun_recovery.clear()
            self.tun_process.stop()
            self.process.stop()
            raise

        self._route_session = session
        self._active = config
        self._active_engine = ENGINE_CLASSIC

    def _note_singbox_adblock_once(self) -> None:
        """Log the 'ad-block is legacy-only' notice AT MOST ONCE per app launch.

        geosite ad-block is an Xray feature and can't run on sing-box. The
        Settings checkbox is disabled under sing-box, but if `block_ads` was left
        True from a legacy session we surface it once — never on every reconnect
        (which spammed the Logs page / app.log). Idempotent within a launch."""
        if bool(self.settings.get("block_ads")) and not self._singbox_adblock_noted:
            self._singbox_adblock_noted = True
            self._log("[sing-box] Блокировка рекламы (block_ads) работает только в "
                      "legacy-движке / HTTP-режиме — на sing-box она неактивна.")

    def _connect_tun_sing_box(self, config: ProxyConfig, direct_domains: list[str]) -> None:
        """sing-box native-TUN connect (v3.0.0 primary). sing-box owns the TUN
        device, manages routes (auto_route), proxies and resolves DNS itself —
        so there's NO tun2socks process and NO 127.0.0.1 SOCKS bridge. Much less
        to set up than the classic path: no manual route session, no physical-
        DNS clearing (sing-box hijacks :53 to its own tunnelled resolver)."""
        if not admin.is_admin():
            raise ConnectionError(self._tun_admin_message())
        if not sing_box_installer.is_installed():
            raise ConnectionError(
                "Движок sing-box ещё не установлен. Дай приложению докачать "
                "его (Настройки → переподключись) или выбери legacy-движок.")

        server_host = str(config.outbound.get("server", "")).strip()
        if not server_host:
            raise ConnectionError("В конфиге нет адреса сервера.")
        try:
            server_ip = socket.gethostbyname(server_host)
        except socket.gaierror as e:
            raise ConnectionError(
                f"Не удалось резолвнуть VPN-сервер «{server_host}»: {e}") from e

        self._log("[*] Движок: sing-box (нативный TUN, без tun2socks/SOCKS-моста)")
        dns_option = str(self.settings.get("dns_option", "system"))
        dns_leak = bool(self.settings.get("dns_leak_protection", True))
        block_ads = bool(self.settings.get("block_ads", False))
        route_ru_direct = bool(self.settings.get("route_ru_direct", False))

        # Honest ONE-TIME notice (not on every reconnect) that ad-block is
        # legacy-only — see _note_singbox_adblock_once().
        self._note_singbox_adblock_once()

        # Generate + write the runtime config (may raise UnsupportedBySingBox,
        # which the dispatcher turns into a 'use legacy' message — no process
        # has started yet, so nothing to roll back).
        cfg_path = sing_box_config.write_config(
            config, direct_domains, server_ip=server_ip,
            dns_option=dns_option, dns_leak_protection=dns_leak,
            block_ads=block_ads, route_ru_direct=route_ru_direct,
            on_log=self._log,
        )
        ok, msg = sing_box_config.check_config(cfg_path)
        if not ok:
            paths.remove_runtime_configs()
            raise ConnectionError(f"sing-box отверг конфиг:\n{msg}")

        # Kill-switch BEFORE the tunnel comes up, allowing sing-box.exe out.
        self._maybe_arm_killswitch(sing_box=True)

        try:
            self.sing_box_process.start(cfg_path)
        except Exception as e:
            try:
                killswitch.remove()
            except Exception:
                pass
            paths.remove_runtime_configs()
            raise ConnectionError(f"Не удалось запустить sing-box: {e}") from e

        try:
            # Did it die immediately (bad config / driver / TUN collision)?
            time.sleep(1.5)
            if not self.sing_box_process.is_running():
                tail = "\n".join(self.sing_box_process.recent_logs()[-8:])
                low = tail.lower()
                tun_busy = ("already exists" in low or "configure tun" in low
                            or "create tun" in low)
                if tun_busy:
                    # An orphan helper is still holding "KaproTun". Free it
                    # REACTIVELY (only here, on a real collision — not on every
                    # connect) and retry ONCE.
                    self._log("[*] TUN «KaproTun» занят орфаном — освобождаю и "
                              "пробую ещё раз…")
                    self._free_tun_device()
                    try:
                        self.sing_box_process.start(cfg_path)
                    except Exception as e:
                        raise ConnectionError(
                            f"Не удалось перезапустить sing-box: {e}") from e
                    time.sleep(1.5)
                if not self.sing_box_process.is_running():
                    tail = "\n".join(self.sing_box_process.recent_logs()[-8:])
                    if tun_busy:
                        raise ConnectionError(
                            "sing-box не смог создать TUN-устройство «KaproTun» — "
                            "оно занято другим процессом. Полностью закрой "
                            "KaproTUN и запусти снова (при старте оно вычищает "
                            "орфанов), затем подключись."
                            + (f"\n\nЛог sing-box:\n{tail}" if tail else ""))
                    raise ConnectionError(
                        "sing-box завершился сразу после старта"
                        + (f":\n{tail}" if tail else "."))
            # Liveness: OS resolution now travels through the sing-box TUN →
            # its tunnelled resolver. If it answers, the tunnel carries traffic.
            self._log("[*] Проверяю, что туннель sing-box живой…")
            if not dns_health.probe(timeout=2.0, attempts=4):
                # Surface the startup-relevant sing-box lines (missing default
                # interface / no route / config) as a connect diagnostic — these
                # are hidden once live, but at startup they explain the failure.
                diag = [l for l in self.sing_box_process.recent_logs()
                        if not is_benign_noise(l, live=False)]
                tail = "\n".join(diag[-6:])
                raise ConnectionError(
                    "sing-box поднялся, но DNS через туннель не отвечает. "
                    "Откатываю. Проверь сервер/подписку, либо переключи движок "
                    "на legacy (Xray + tun2socks)."
                    + (f"\n\nДиагностика sing-box:\n{tail}" if tail else ""))
            # REAL gate (not a warning): resolve actual CDN / YouTube / OpenAI
            # hosts through the SAME system resolver (10.255.0.3 = the sing-box
            # TUN). These are the domains that hang when DNS-over-UDP-through-
            # proxy times out — the v3.0.7 bug. If they don't answer in a
            # generous bounded window, the tunnel's DNS is broken and the session
            # is effectively useless, so roll back HONESTLY rather than claim
            # "DNS проходят". The DoH leak-on DNS (v3.0.7) makes this pass
            # normally; it only trips when the DNS path is genuinely degraded.
            cdn_hosts = ("www.youtube.com", "files.oaiusercontent.com",
                         "yastatic.net")
            if not dns_health.probe(timeout=2.5, attempts=2, hosts=cdn_hosts):
                raise ConnectionError(
                    "sing-box поднялся, но DNS для YouTube / CDN / OpenAI не "
                    "резолвится через туннель — системный DNS на TUN-адаптере не "
                    "отвечает на эти домены, сайты/видео зависали бы на загрузке. "
                    "Откатываю. Попробуй другой сервер, переключи защиту от "
                    "DNS-утечек, либо движок на legacy (Xray + tun2socks).")
            # Confirmed fully live → switch the log filter to steady-state, so
            # ambiguous network errors become transient noise instead of alarms.
            self.sing_box_process.mark_live()
            self._log("[*] sing-box TUN активен — трафик и DNS (вкл. YouTube/CDN) "
                      "проходят.")
        except Exception:
            self.sing_box_process.stop()
            try:
                killswitch.remove()
            except Exception:
                pass
            paths.remove_runtime_configs()
            raise

        self._active = config
        self._active_engine = ENGINE_SING_BOX

    def _tun_admin_message(self) -> str:
        """Per-OS 'TUN needs admin' message (shared by both engines)."""
        if sys.platform == "win32":
            return (
                "TUN-режим требует прав администратора.\n"
                "Перезапусти KaproTUN от имени администратора "
                "(правый клик по ярлыку → «Запуск от имени администратора») "
                "или переключи в Настройках режим на «HTTP-прокси».")
        if sys.platform == "darwin":
            return (
                "TUN-режиму нужен root для создания utun-интерфейса.\n"
                "Запусти из терминала через sudo, или переключи режим на "
                "«HTTP-прокси».")
        return (
            "TUN-режиму нужен root для управления маршрутами.\n"
            "Запусти через sudo / pkexec, или переключи режим на «HTTP-прокси».")

    def current_engine(self) -> str:
        """The engine of the live session, or the configured default when idle.
        Used by the UI to show 'TUN · sing-box' vs 'TUN · legacy'."""
        if self._active_engine:
            return self._active_engine
        return resolve_engine(self.settings.get("tun_engine"))

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
                         hysteria_socks_port: Optional[int] = None,
                         egress_interface: Optional[str] = None) -> str:
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
                egress_interface=egress_interface,
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

    # --- connect-time liveness (v2.1.5) ----------------------------------

    def _xray_log_size(self) -> int:
        """Current byte-size of xray's error log (0 if absent). Captured before
        start so the REALITY scan reads only this session's lines."""
        try:
            return int(paths.log_file().stat().st_size)
        except Exception:
            return 0

    def _scan_xray_reality_errors(self, offset: int) -> int:
        """Count REALITY 'received real certificate' failures logged since
        `offset`. A working REALITY transport never logs this; a burst means
        the handshake is failing (stale pbk/sid/sni, server changed, or active
        MITM) — the tunnel can't carry traffic. Reads only bytes appended after
        `offset` (tolerant of truncation). Never raises."""
        try:
            p = paths.log_file()
            size = int(p.stat().st_size)
            start = offset if (isinstance(offset, int) and 0 <= offset <= size) else 0
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(start)
                text = fh.read()
        except Exception:
            return 0
        return text.lower().count("received real certificate")

    def _verify_tunnel_or_raise(self, host: str, port: int,
                                dns_cleared: bool, log_offset: int) -> None:
        """Confirm the tunnel actually carries traffic; raise (→ rollback) if
        not, with a message that names the real cause.

        Two independent signals:
          * http_probe — an HTTP request through xray's local inbound, i.e.
            xray-side DNS + the proxy transport. Works in BOTH leak modes and
            is the primary "is REALITY alive" test.
          * dns probe — OS-level resolution. Only meaningful when leak
            protection cleared the physical DNS (then it proves the full
            OS→TUN→xray→tunnel path the user's apps depend on).

        Alive criterion: leak ON → OS resolution must work (apps need it);
        leak OFF → the tunnel transport must work. On failure we scan xray's
        log for REALITY errors to distinguish a broken obfuscated transport
        from a plain dead server / dead DNS, and raise the matching message.
        """
        self._log("[*] Проверяю, что туннель реально живой (не только процессы)…")
        proxy_url = f"http://{host}:{port}"
        http_ok = dns_health.http_probe(proxy_url, timeout=2.5)
        dns_ok = dns_health.probe(timeout=1.5, attempts=2) if dns_cleared else None

        alive = bool(dns_ok) if dns_cleared else http_ok
        if alive:
            self._log("[*] Туннель живой — трафик проходит"
                      + (", DNS резолвится." if dns_cleared else "."))
            return

        reality = self._scan_xray_reality_errors(log_offset)
        if reality:
            raise ConnectionError(
                "Транспорт REALITY не проходит рукопожатие — сервер отдаёт "
                f"настоящий TLS-сертификат вместо маскировки ({reality} таких "
                "ошибок в логе). Обычно это значит, что параметры (pbk/sid/sni) "
                "устарели, сервер сменили или соединение перехватывают. "
                "Подключение отменено, сеть восстановлена — обнови "
                "подписку/конфиг и попробуй снова."
            )
        if not http_ok:
            raise ConnectionError(
                "Туннель поднялся, но трафик через него не проходит (проверка "
                "соединения и DNS не ответили). Сервер недоступен или "
                "блокируется. Подключение отменено, сеть восстановлена — "
                "проверь сервер/подписку и попробуй снова."
            )
        raise ConnectionError(
            "Туннель работает, но системный DNS через TUN не поднялся — "
            "резолвинг не отвечает. Подключение отменено, сеть восстановлена. "
            "Если повторяется — попробуй другой сервер или временно отключи "
            "«Защиту от DNS-утечек»."
        )

    def _maybe_arm_killswitch(self, sing_box: bool = False) -> None:
        """If user enabled kill-switch in settings, install firewall rules.

        `sing_box=True` (the sing-box TUN engine) also allows sing-box.exe out —
        in that mode sing-box, not xray, reaches the VPN server.

        Silent no-op when:
          - Setting is off
          - Not on Windows (other OSes not supported yet)
          - Not running as admin (rule install would fail anyway)

        We DON'T raise on failure — kill-switch is defence-in-depth, the
        connection itself works either way.
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
        sb_exe = paths.sing_box_exe() if sing_box else None
        if killswitch.install(xray_exe, hy_exe, sing_box_exe_path=sb_exe):
            who = "sing-box" if sing_box else ("xray + hysteria" if hy_exe else "xray")
            self._log(f"[*] Kill-switch активирован (firewall блокирует весь "
                      f"трафик мимо {who})")
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
