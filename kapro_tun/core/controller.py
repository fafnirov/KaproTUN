"""Connection controller: drives the sing-box native-TUN dataplane lifecycle.

v3.1.0 removed the legacy Xray + tun2socks engine and HTTP-proxy mode — sing-box
is now the single engine and TUN the single mode."""
from __future__ import annotations

import atexit
import socket
import sys
import time
from typing import Callable, Optional

from . import (
    admin, app_log, dns_options, dns_health, ipv6_block, killswitch,
    paths, proc_stats, storage, tun_recovery, webrtc_block,
)
from .parser import ProxyConfig
from .sing_box_process import SingBoxProcess, is_benign_noise
from . import sing_box_config, sing_box_installer


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
        # The single engine: sing-box native-TUN process (owns the TUN device,
        # routes + resolves DNS itself — no tun2socks bridge, no xray).
        self.sing_box_process = SingBoxProcess(
            on_log=(lambda l: on_log(f"[sing-box] {l}")) if on_log else None,
        )
        self.settings = storage.load_settings()
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
        try:
            self._connect_tun_sing_box(config, direct_domains)
        except sing_box_config.UnsupportedBySingBox as e:
            raise ConnectionError(
                f"{e}\n\nЭтот тип сервера (например, XHTTP/splithttp) пока не "
                f"поддерживается. Используй сервер на VLESS/Trojan/Shadowsocks/"
                f"Hysteria2."
            ) from e

    def disconnect(self) -> None:
        # sing-box owns the TUN + its routes (auto_route removes them on a clean
        # shutdown) and restores the physical NIC's DNS itself. Stop it, drop the
        # crash-recovery journal (a clean stop has nothing left to undo — its
        # presence on next startup means a session died uncleanly), wipe the
        # credential-bearing runtime config, then take down the firewall rules.
        tun_recovery.clear()
        if self.sing_box_process.is_running():
            self.sing_box_process.stop()
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
        return self.sing_box_process.is_running()

    def active_config(self) -> Optional[ProxyConfig]:
        return self._active if self.is_connected() else None

    def update_settings(self, **changes) -> None:
        self.settings.update(changes)
        storage.save_settings(self.settings)

    def current_mode(self) -> str:
        # v3.1.0: TUN is the only mode (HTTP-proxy mode was removed with Xray).
        return MODE_TUN

    def tun_dns_guarded(self) -> bool:
        """True when the live sing-box TUN owns the system DNS path. sing-box
        hijacks all :53 in BOTH leak modes, so whenever it's up a DNS outage
        means a broken tunnel the runtime watchdog should heal."""
        return self.is_connected()

    def tun_runtime_healthy(self) -> bool:
        """Real data-plane health: DNS resolves AND the sing-box outbound carries
        traffic whose public egress IP matches the system TUN's. A direct secure
        resolver can stay healthy while the VPN outbound is dead, so DNS alone is
        not enough."""
        if not self.tun_dns_guarded():
            return True
        try:
            if not dns_health.probe(timeout=2.5, attempts=2):
                return False
            return dns_health.singbox_system_tun_healthy(
                self._singbox_health_proxy_url(), timeout=2.5)
        except Exception as exc:
            self._log(f"[watchdog] health probe failed: {type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _singbox_health_proxy_url() -> str:
        return (f"http://{sing_box_config.HEALTH_PROXY_HOST}:"
                f"{sing_box_config.HEALTH_PROXY_PORT}")

    # --- runtime memory watchdog (v2.1.6) ---------------------------------

    def sample_runtime_stats(self) -> dict:
        """Best-effort {name: ProcSample|None} for the sing-box process. Cheap
        (one psutil read); never raises."""
        return {"sing-box": proc_stats.sample(self.sing_box_process.pid())}

    def format_runtime_stats(self, stats: Optional[dict] = None) -> str:
        """One diagnostic line: memory, handles, threads, pid, uptime for the
        sing-box process. ASCII-only numbers, safe to write anywhere."""
        if stats is None:
            stats = self.sample_runtime_stats()
        s = stats.get("sing-box")
        if s is None:
            return "[mem] sing-box: н/д"
        seg = f"sing-box: {proc_stats.human_bytes(s.private_bytes)}"
        if s.handles:
            seg += f", {s.handles} хэндлов"
        if s.threads:
            seg += f", {s.threads} потоков"
        pid = self.sing_box_process.pid()
        if pid:
            seg += f", pid {pid}"
        if s.create_time:
            up = max(0, int(time.time() - s.create_time))
            seg += f", up {up // 60}м{up % 60:02d}с"
        return "[mem] " + seg

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

    def _wait_until_running(self, deadline: float = 4.0,
                            interval: float = 0.25) -> bool:
        """Poll the sing-box process until it reports running, up to `deadline`s.
        Returns True as soon as it's alive (fast path on a healthy box), False if
        it's still not running at the deadline (a genuine instant death). Replaces
        a coarse fixed sleep that misjudged a slow WinTUN init as a crash."""
        waited = 0.0
        while waited < deadline:
            if self.sing_box_process.is_running():
                return True
            time.sleep(interval)
            waited += interval
        return self.sing_box_process.is_running()

    def _wait_for_singbox_ready(self, deadline: float = 15.0,
                                interval: float = 0.75) -> bool:
        """Require both working DNS and real application traffic through TUN.

        DNS is independent direct DoH in v3.0.13, so it cannot prove that the
        selected VLESS/Trojan/Hysteria2 transport works. The second probe enters
        a loopback-only sing-box inbound that is explicitly routed to proxy.
        """
        waited = 0.0
        while waited < deadline:
            dns_ok = dns_health.probe(timeout=1.5, attempts=1)
            transport_ok = (dns_health.singbox_system_tun_healthy(
                                self._singbox_health_proxy_url(), timeout=2.0)
                            if dns_ok else False)
            if dns_ok and transport_ok:
                return True
            if not self.sing_box_process.is_running():
                return False
            recent = "\n".join(self.sing_box_process.recent_logs()[-30:]).lower()
            if recent.count("crypto_error") >= 3:
                return False
            time.sleep(interval)
            waited += interval
        return (dns_health.probe(timeout=1.5, attempts=1)
                and dns_health.singbox_system_tun_healthy(
                    self._singbox_health_proxy_url(), timeout=2.0))

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
        self._maybe_arm_killswitch()

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
            # Poll for a few seconds rather than a flat sleep — a slightly-slow
            # WinTUN init on a busy machine must NOT be misjudged as an instant
            # crash (that errored out tunnels that were merely slow to come up).
            if not self._wait_until_running(4.0):
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
                if not self._wait_until_running(4.0):
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
            # The process being alive and DNS resolving are not enough: encrypted
            # DNS is intentionally independent of the VPN transport. Require a
            # real HTTP request whose route falls through final=proxy.
            self._log("[*] Проверяю, что туннель sing-box живой…")
            if not self._wait_for_singbox_ready(15.0):
                # Surface the startup-relevant sing-box lines (missing default
                # interface / no route / config) as a connect diagnostic.
                diag = [l for l in self.sing_box_process.recent_logs()
                        if not is_benign_noise(l, live=False)]
                tail = "\n".join(diag[-6:])
                raise ConnectionError(
                    "sing-box поднялся, но VPN-сервер не пропускает реальный "
                    "трафик. DNS проверяется отдельно и может работать даже при "
                    "мёртвом транспорте. Подключение отменено. Попробуй другой "
                    "сервер из подписки."
                    + (f"\n\nДиагностика sing-box:\n{tail}" if tail else ""))
            # Confirmed live → switch the log filter to steady-state, so
            # ambiguous network errors become transient noise instead of alarms.
            self.sing_box_process.mark_live()
            self._log("[*] sing-box TUN активен — DNS и реальный трафик через VPN проходят.")
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
        """Per-OS 'TUN needs admin' message."""
        if sys.platform == "win32":
            return (
                "KaproTUN требует прав администратора.\n"
                "Перезапусти от имени администратора "
                "(правый клик по ярлыку → «Запуск от имени администратора»).")
        if sys.platform == "darwin":
            return (
                "KaproTUN нужен root для создания utun-интерфейса.\n"
                "Запусти из терминала через sudo.")
        return (
            "KaproTUN нужен root для управления маршрутами.\n"
            "Запусти через sudo / pkexec.")

    def current_engine(self) -> str:
        """Always the sing-box engine (the only one since v3.1.0)."""
        return ENGINE_SING_BOX

    def _maybe_arm_killswitch(self) -> None:
        """Install the kill-switch firewall rules (block ALL outbound except LAN
        + sing-box.exe) if the user enabled it. Silent no-op when off / not
        Windows / not admin. Never raises — defence-in-depth, the tunnel works
        either way."""
        if not self.settings.get("kill_switch", False):
            return
        if not killswitch.is_supported():
            self._log("[!] Kill-switch пока работает только на Windows")
            return
        if not admin.is_admin():
            self._log("[!] Kill-switch требует админа — пропускаю")
            return
        if killswitch.install(paths.sing_box_exe()):
            self._log("[*] Kill-switch активирован (firewall блокирует весь "
                      "трафик мимо sing-box)")
        else:
            self._log("[!] Не удалось установить firewall-правила kill-switch "
                      "— продолжаю без него")

    def _maybe_arm_ipv6_block(self) -> None:
        """No-op firewall-wise for the sing-box TUN: the tunnel itself captures
        IPv6 (inet6 address + auto_route) and REJECTS global-unicast v6 in-tunnel
        with a TCP RST, so a netsh v6 DROP is both redundant AND the cause of
        ERR_NETWORK_ACCESS_DENIED. Just clear any stale firewall block a prior
        build left behind. (The sing-box connect path no longer calls this; kept
        callable for safety.)"""
        try:
            ipv6_block.remove()
        except Exception:
            pass

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
