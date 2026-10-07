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
    admin, app_log, dns_health, ipv6_block, killswitch, linux_tun_route,
    paths, proc_stats, proxy_session, storage, tun_recovery, webrtc_block,
    xray_stats,
)
from .i18n import tr
from .parser import ProxyConfig
from .sing_box_process import SingBoxProcess, is_benign_noise
from . import sing_box_config, sing_box_installer


class ConnectionError(Exception):
    pass


# Connection modes
MODE_HTTP_PROXY = "http"   # Proxy mode (v3.8.0, macOS): local SOCKS+HTTP listener + system proxy. No admin needed; only proxy-aware apps are carried.
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
        # What actually happened to each firewall protection the user switched
        # on, for this session: "active", "failed", "needs_admin" or
        # "unsupported". A protection that is switched off has no entry. The
        # settings checkbox shows what the user ASKED for; this is the only
        # record of what they GOT, and the UI reads it after connect so a
        # protection that silently failed to arm is reported rather than
        # implied by a ticked box. See inactive_protections().
        self.protection_status: dict[str, str] = {}
        # True while the kill-switch rules are deliberately left in the
        # firewall with no tunnel up: the client is reconnecting on its own, or
        # gave up and is waiting for the user. Only release_killswitch() (user
        # disconnect, setting switched off, quit) clears it.
        self.killswitch_held = False
        # True from a successful install until a confirmed removal. This, not
        # protection_status, decides whether there is anything to hold:
        # protection_status is what the user is told about THIS connect (a
        # re-arm can fail while the previous rules are still in force).
        self._killswitch_armed = False
        self._held_server_ip = ""
        # host -> IP of the last successful lookup, for reconnecting under a
        # held kill-switch when the resolver itself is out of reach.
        self._last_resolved: tuple[str, str] = ("", "")
        # The single engine: sing-box native-TUN process (owns the TUN device,
        # routes + resolves DNS itself — no tun2socks bridge, no xray).
        self.sing_box_process = SingBoxProcess(
            on_log=(lambda l: on_log(f"[sing-box] {l}")) if on_log else None,
        )
        self.settings = storage.load_settings()
        self._active: Optional[ProxyConfig] = None
        # Which TUN engine the live session is using (None when disconnected).
        self._active_engine: Optional[str] = None
        # Mode of the LIVE session (MODE_TUN / MODE_HTTP_PROXY), None when
        # disconnected — see planned_mode() for what the next connect will use.
        self._active_mode: Optional[str] = None
        # Proxy mode only. Per-session secret for sing-box's loopback control
        # API (traffic totals), and whether the system proxy verifiably took
        # effect: True / False, or None when not in a proxy session. False is
        # a state the UI must report — sing-box is up but nothing is using it.
        self._api_secret: str = ""
        self.system_proxy_applied: Optional[bool] = None
        # Roaming detection (v3.4.0): the resolved server IP + the local source
        # IP the OS uses to reach it, snapshotted at connect. sing-box's
        # auto_route pins the tunnel to the interface that was default then; on a
        # full adapter swap (Ethernet↔Wi-Fi) it doesn't re-home, so we watch this
        # fingerprint and clean-reconnect when it changes. See egress_changed().
        self._server_ip: str = ""
        self._egress_fp: Optional[str] = None
        # Baseline for the health check's 'is the tunnel moving bytes?' test.
        self._health_traffic = None
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
            raise ConnectionError(tr("err.already_connected"))
        try:
            if self.planned_mode() == MODE_HTTP_PROXY:
                self._connect_proxy_sing_box(config, direct_domains)
            else:
                self._connect_tun_sing_box(config, direct_domains)
        except sing_box_config.UnsupportedBySingBox as e:
            raise ConnectionError(tr("err.unsupported_server", error=e)) from e

    def disconnect(self, hold_killswitch: bool = False) -> None:
        # sing-box owns the TUN + its routes (auto_route removes them on a clean
        # shutdown) and restores the physical NIC's DNS itself. Stop it, drop the
        # crash-recovery journal (a clean stop has nothing left to undo — its
        # presence on next startup means a session died uncleanly), wipe the
        # credential-bearing runtime config, then take down the firewall rules.
        tun_recovery.clear()
        # Proxy mode: give the system proxy back BEFORE the listener goes away,
        # so no app is left pointing at a port that is about to close. A no-op
        # when there is no proxy session (no journal on disk).
        try:
            proxy_session.end()
        except Exception as e:
            self._log(f"[!] Системный прокси: не удалось вернуть настройки: {e}")
        if self.sing_box_process.is_running():
            self.sing_box_process.stop()
        # Linux: undo the manual routes + resolvectl DNS we laid in place of
        # auto_route. Idempotent no-op on other platforms / if never set up.
        linux_tun_route.teardown()
        leftover = paths.remove_runtime_configs()
        if leftover:
            self._log("[!] Не удалось удалить runtime-конфиги: "
                      f"{', '.join(leftover)} — они содержат секреты, "
                      "проверь права на папку данных")
        # Kill-switch LAST — until now the firewall block is the safety net if
        # any step above leaves traffic in a weird state. `hold_killswitch` is
        # what every automatic teardown passes (engine crash, DNS watchdog,
        # network change): the tunnel is down and about to be rebuilt, which is
        # precisely when the rules must stay. Before v4.0.0 they were removed
        # here on every path, so the kill-switch was off whenever it mattered.
        if hold_killswitch and self._killswitch_in_force():
            self.killswitch_held = True
        else:
            self.release_killswitch()
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
        self.protection_status = {}
        self._active = None
        self._active_engine = None
        self._active_mode = None
        self._api_secret = ""
        self.system_proxy_applied = None
        self._server_ip = ""
        self._egress_fp = None

    def _egress_fingerprint(self) -> Optional[str]:
        """Local source IP the OS would use to reach the VPN server RIGHT NOW.

        Cheap and side-effect-free: a UDP `connect` sends no packet, it only
        makes the kernel resolve the route and bind a source address. No admin,
        no subprocess. In TUN mode the server IP is auto_route-bypassed to the
        PHYSICAL NIC, so this is that NIC's address — it changes when the machine
        roams Ethernet↔Wi-Fi (different adapter → different DHCP lease). Returns
        None when unknown (no server / transient no-route); callers must treat
        None as 'no signal', never as a change."""
        ip = self._server_ip
        if not ip:
            return None
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((ip, 9))          # discard-protocol port; nothing is sent
            return s.getsockname()[0]
        except OSError:
            return None
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    def egress_changed(self) -> bool:
        """True when the physical egress interface changed since connect (e.g.
        Ethernet unplugged → Wi-Fi). Best-effort: not connected, no snapshot, or
        an unresolvable current fingerprint (mid-transition) all read as False,
        so a brief no-route moment during the switch never false-triggers."""
        if not self.is_connected() or not self._egress_fp:
            return False
        now = self._egress_fingerprint()
        if not now:
            return False
        return now != self._egress_fp

    def is_connected(self) -> bool:
        return self.sing_box_process.is_running()

    def active_config(self) -> Optional[ProxyConfig]:
        return self._active if self.is_connected() else None

    def update_settings(self, **changes) -> None:
        # Merge onto what is on disk NOW, not onto the copy loaded at startup:
        # other code writes settings.json too (the device id, subscription
        # stamps), and saving a stale whole dict erased their keys.
        fresh = storage.update_settings(changes, fallback=self.settings)
        # Same dict object, new contents — the GUI holds references to it, and
        # the connect worker reads it from another thread. So never empty it
        # first: for one instant kill_switch would read as off.
        for stale in [k for k in self.settings if k not in fresh]:
            del self.settings[stale]
        self.settings.update(fresh)

    def planned_mode(self) -> str:
        """Mode the NEXT connect will use.

        macOS without root → proxy mode: creating a utun device needs root, and
        asking for an administrator password on every launch is exactly what
        this mode exists to avoid. Launched as root, macOS keeps the full TUN.
        Windows and Linux are TUN-only, as before."""
        if sys.platform == "darwin" and not admin.is_admin():
            return MODE_HTTP_PROXY
        return MODE_TUN

    def current_mode(self) -> str:
        """Mode of the live session, or the planned one when disconnected."""
        return self._active_mode or self.planned_mode()

    def traffic_sample(self):
        """Cumulative byte counters for the live session, or None.

        One accessor for both modes so the traffic graph and the health check
        never need to know which is active: kernel counters off the TUN device,
        or sing-box's own totals in proxy mode, where there is no such device."""
        if self.current_mode() == MODE_HTTP_PROXY:
            if not self._api_secret:
                return None
            return xray_stats.query_clash_totals(
                sing_box_config.PROXY_LISTEN_HOST,
                sing_box_config.CLASH_API_PORT, self._api_secret)
        return xray_stats.query_tun_iface_stats(sing_box_config.TUN_DEVICE_NAME)

    def tun_dns_guarded(self) -> bool:
        """True when the live sing-box TUN owns the system DNS path. sing-box
        hijacks all :53 in BOTH leak modes, so whenever it's up a DNS outage
        means a broken tunnel the runtime watchdog should heal."""
        return self.is_connected()

    # --- runtime health verdict (v3.5.1) ----------------------------------
    # Three states instead of a bool. The old bool made "probe failed" mean
    # "tunnel is dead", and the watchdog answered with a FULL reconnect — a
    # multi-second, whole-network outage. Under load (a game saturating the
    # userspace gvisor stack) the 2.5 s probes fail while the tunnel is
    # perfectly alive, so the cure fired on healthy sessions and looked like a
    # 5-8 s freeze mid-game. Now only a tunnel proven DEAD is reconnected;
    # "slow / probe starved" is DEGRADED and merely logged.
    HEALTH_OK = "ok"
    HEALTH_DEGRADED = "degraded"
    HEALTH_DEAD = "dead"

    def _tun_traffic_moved(self) -> bool:
        """True if the TUN interface moved bytes since the previous call.

        Kernel byte counters on our own TUN device (psutil) — the same source
        the UI graph uses. If packets are still flowing through the tunnel, the
        data path is alive by definition, whatever a timed-out HTTP probe says.
        First call after connect has no baseline and returns False."""
        try:
            sample = self.traffic_sample()
        except Exception:
            return False
        if sample is None:
            return False
        prev, self._health_traffic = self._health_traffic, sample
        if prev is None:
            return False
        return (sample.uplink_bytes > prev.uplink_bytes
                or sample.downlink_bytes > prev.downlink_bytes)

    def tun_runtime_health(self, timeout: float = 5.0) -> str:
        """HEALTH_OK / HEALTH_DEGRADED / HEALTH_DEAD for the live TUN session.

        Order matters. The transport probe (HTTP through the loopback health
        inbound, pinned to outbound=proxy) is the authoritative tunnel test and
        needs no local DNS, so it runs first; a DNS-only failure can then be
        reported as DEGRADED instead of being mistaken for a dead tunnel.

        `timeout` is deliberately generous (5 s, was 2.5): the probe competes
        for CPU with the userspace network stack under load."""
        if not self.tun_dns_guarded():
            return self.HEALTH_OK
        # Cheapest and most definitive signal: the engine itself is gone.
        if not self.sing_box_process.is_running():
            app_log.net("health", verdict="dead", reason="process_not_running")
            return self.HEALTH_DEAD
        try:
            transport_ok = dns_health.singbox_outbound_probe(
                self._singbox_health_proxy_url(), timeout=timeout)
            if transport_ok:
                dns_ok = dns_health.probe(timeout=timeout, attempts=1)
                verdict = self.HEALTH_OK if dns_ok else self.HEALTH_DEGRADED
                app_log.net("health", verdict=verdict, transport="ok",
                            dns=("ok" if dns_ok else "fail"))
                return verdict
            # Transport probe failed — but is the tunnel actually carrying
            # packets? If yes it is busy/starved, NOT dead: reconnecting would
            # be a self-inflicted outage.
            moved = self._tun_traffic_moved()
            verdict = self.HEALTH_DEGRADED if moved else self.HEALTH_DEAD
            app_log.net("health", verdict=verdict, transport="fail",
                        tun_traffic=("moving" if moved else "idle"))
            return verdict
        except Exception as exc:
            self._log(f"[watchdog] health probe failed: {type(exc).__name__}: {exc}")
            app_log.net("health", verdict="degraded", error=type(exc).__name__)
            # An error in OUR probe is not evidence the tunnel died.
            return self.HEALTH_DEGRADED

    def tun_runtime_healthy(self) -> bool:
        """Legacy boolean view of tun_runtime_health() — True only when fully
        OK. Kept for callers/tests that predate the three-state verdict; the
        probe logic lives in ONE place (tun_runtime_health) so the two can
        never drift apart."""
        return self.tun_runtime_health() == self.HEALTH_OK

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
        """Require working system DNS AND a live proxy transport.

        DNS now resolves via the system resolver (v3.1.1), so a passing dns probe
        proves names resolve but says nothing about the VPN. The transport probe
        enters a loopback-only sing-box inbound pinned to outbound=proxy: a
        2xx/3xx there proves the selected VLESS/Trojan/Hysteria2 transport
        actually carries traffic. (No more cdn-cgi/trace egress match — it
        depended on the old custom DoH resolving and false-failed live tunnels.)
        """
        # Gate on REAL elapsed wall-clock, not the sum of `interval` ticks: each
        # iteration's probes can themselves block for seconds (a dead transport
        # times out the http probe ~5 s), so counting `waited += interval` made
        # a nominal 15 s gate actually run ~2 minutes (20 iterations × ~6 s real)
        # before failing — the user stared at "Проверяю…" far too long.
        start = time.time()
        while time.time() - start < deadline:
            dns_ok = dns_health.probe(timeout=1.5, attempts=1)
            transport_ok = (dns_health.singbox_outbound_probe(
                                self._singbox_health_proxy_url(), timeout=2.5)
                            if dns_ok else False)
            if dns_ok and transport_ok:
                return True
            if not self.sing_box_process.is_running():
                return False
            recent = "\n".join(self.sing_box_process.recent_logs()[-30:]).lower()
            if recent.count("crypto_error") >= 3:
                return False
            time.sleep(interval)
        return (dns_health.probe(timeout=1.5, attempts=1)
                and dns_health.singbox_outbound_probe(
                    self._singbox_health_proxy_url(), timeout=2.5))

    def _connect_tun_sing_box(self, config: ProxyConfig, direct_domains: list[str]) -> None:
        """sing-box native-TUN connect (v3.0.0 primary). sing-box owns the TUN
        device, manages routes (auto_route), proxies and resolves DNS itself —
        so there's NO tun2socks process and NO 127.0.0.1 SOCKS bridge. Much less
        to set up than the classic path: no manual route session, no physical-
        DNS clearing (sing-box hijacks :53 to its own tunnelled resolver)."""
        if not admin.is_admin():
            raise ConnectionError(self._tun_admin_message())
        if not sing_box_installer.is_installed():
            raise ConnectionError(tr("err.singbox_not_installed"))

        server_host = str(config.outbound.get("server", "")).strip()
        if not server_host:
            raise ConnectionError(tr("err.no_server_address"))
        if self.killswitch_held:
            # Reconnecting under a held kill-switch, possibly on a different
            # network than the rules were written for: re-spare the current
            # DNS servers first, or the lookup below is blocked by our own rules.
            self._maybe_arm_killswitch(self._held_server_ip)
        try:
            server_ip = socket.gethostbyname(server_host)
        except socket.gaierror as e:
            if not (self.killswitch_held and self._last_resolved[0] == server_host):
                raise ConnectionError(
                    tr("err.resolve_failed", host=server_host, error=e)) from e
            # Under the held kill-switch the resolver may be unreachable (a
            # public DoH resolver, say). The server has not moved since the
            # session that armed the rules: reuse its address.
            server_ip = self._last_resolved[1]
            self._log(f"[*] DNS недоступен под kill-switch — использую прежний "
                      f"адрес сервера {server_ip}")
        self._last_resolved = (server_host, server_ip)

        self._log("[*] Движок: sing-box (нативный TUN, без tun2socks/SOCKS-моста)")
        self._note_security_warnings(config)
        # v3.1.1: DNS is always the system resolver — no dns_option / leak toggle.
        block_ads = bool(self.settings.get("block_ads", False))
        # v3.3.0: RU-direct defaults ON (RU IP → real IP, else → VPN); high_speed
        # (Turbo kernel stack) defaults OFF.
        # (v3.5.0: Steam/Riot games bypass the tunnel, matched by process.)
        plan = self._routing_plan()
        if plan["lockdown"]:
            self._log("[*] Kill-switch включён: на время сессии российские сайты, "
                      "прямые сайты и игры тоже идут через VPN — мимо туннеля "
                      "firewall пропускает только VPN-сервер и локальную сеть")

        # Honest ONE-TIME notice (not on every reconnect) that ad-block is
        # legacy-only — see _note_singbox_adblock_once().
        self._note_singbox_adblock_once()

        # Generate + write the runtime config (may raise UnsupportedBySingBox,
        # which the dispatcher turns into a 'use legacy' message — no process
        # has started yet, so nothing to roll back).
        cfg_path = sing_box_config.write_config(
            config, direct_domains, server_ip=server_ip,
            block_ads=block_ads, on_log=self._log, **plan,
        )
        ok, msg = sing_box_config.check_config(cfg_path)
        if not ok and plan["games_direct"] and not plan["lockdown"]:
            # Safety net (v3.5.0): the games bypass uses process-based rules
            # (process_name / process_path_regex). If the installed engine build
            # doesn't accept them, a rejected config would mean the user simply
            # CAN'T CONNECT — a far worse outcome than routing games through the
            # tunnel. So drop the games rules and try once more instead.
            self._log("[!] Движок отверг конфиг с правилами для игр — "
                      "пересобираю без них (игры пойдут через VPN).")
            app_log.log(f"[games-direct] config rejected, retrying without it: {msg}")
            cfg_path = sing_box_config.write_config(
                config, direct_domains, server_ip=server_ip,
                block_ads=block_ads, on_log=self._log,
                **dict(plan, games_direct=False),
            )
            ok, msg = sing_box_config.check_config(cfg_path)
        if not ok:
            paths.remove_runtime_configs()
            raise ConnectionError(tr("err.singbox_rejected_config", msg=msg))

        # Kill-switch BEFORE the tunnel comes up, written for this server.
        self.protection_status = {}
        self._maybe_arm_killswitch(server_ip)
        if (self.protection_status.get("kill_switch") == "failed"
                and not self._killswitch_armed):
            # Asked for, and not a single rule in the firewall. Connecting
            # anyway would be the opposite of what the setting means; a ticked
            # box plus a passing toast is not consent to run unprotected.
            paths.remove_runtime_configs()
            raise ConnectionError(tr("err.killswitch_not_armed"))
        # The STUN block. This call went missing in v3.1.0 when the legacy
        # engines were cut, and nothing noticed: the setting stayed on by
        # default, the Settings hint kept describing the rule, SECURITY.md kept
        # listing it — and no rule was ever installed. It still matters under
        # a full TUN: split routing sends RU destinations direct, so a page's
        # script could reach a STUN server on an RU address and read back the
        # real IP. Firewall rules filter per socket, so this blocks that path
        # regardless of which interface the route would take.
        self._maybe_arm_webrtc_block()

        try:
            self.sing_box_process.start(cfg_path)
        except Exception as e:
            self._disarm_session_firewall()
            paths.remove_runtime_configs()
            raise ConnectionError(tr("err.singbox_start_failed", error=e)) from e

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
                            tr("err.singbox_restart_failed", error=e)) from e
                if not self._wait_until_running(4.0):
                    tail = "\n".join(self.sing_box_process.recent_logs()[-8:])
                    if tun_busy:
                        raise ConnectionError(
                            tr("err.tun_device_busy")
                            + (tr("err.singbox_log_tail", tail=tail) if tail else ""))
                    raise ConnectionError(
                        tr("err.singbox_died_on_start")
                        + (f":\n{tail}" if tail else "."))
            # Linux (kernel 7.0+): sing-box ran with auto_route OFF because its
            # netlink route-add is rejected by the kernel. Now that the TUN
            # device exists, lay the routing + DNS ourselves via iproute2/
            # resolvectl. No-op on Windows/macOS, where auto_route already did it.
            linux_tun_route.setup()
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
                    tr("err.singbox_no_real_traffic")
                    + (tr("err.singbox_diag_tail", tail=tail) if tail else ""))
            # Confirmed live → switch the log filter to steady-state, so
            # ambiguous network errors become transient noise instead of alarms.
            self.sing_box_process.mark_live()
            self._log("[*] sing-box TUN активен — DNS и реальный трафик через VPN проходят.")
        except Exception:
            self.sing_box_process.stop()
            linux_tun_route.teardown()
            self._disarm_session_firewall()
            paths.remove_runtime_configs()
            raise

        self._active = config
        self._active_engine = ENGINE_SING_BOX
        # A held kill-switch has become this session's ordinary one again.
        self.killswitch_held = False
        # Snapshot the egress fingerprint so the GUI's network-change watchdog
        # can detect an Ethernet↔Wi-Fi roam and clean-reconnect (v3.4.0).
        self._server_ip = server_ip
        self._egress_fp = self._egress_fingerprint()

    def _connect_proxy_sing_box(self, config: ProxyConfig,
                                direct_domains: list[str]) -> None:
        """Proxy-mode connect (v3.8.0): nothing here needs root.

        sing-box runs as the user with a loopback SOCKS+HTTP listener, then the
        system proxy is pointed at it. No TUN device, no routes, no firewall
        rules — which is also why there is no kill-switch and no STUN block in
        this mode: both are firewall rules, and both need privileges we are
        deliberately not asking for."""
        if not sing_box_installer.is_installed():
            raise ConnectionError(tr("err.singbox_not_installed"))
        server_host = str(config.outbound.get("server", "")).strip()
        if not server_host:
            raise ConnectionError(tr("err.no_server_address"))
        try:
            server_ip = socket.gethostbyname(server_host)
        except socket.gaierror as e:
            raise ConnectionError(
                tr("err.resolve_failed", host=server_host, error=e)) from e

        self._log("[*] Движок: sing-box, режим прокси (без TUN, права "
                  "администратора не нужны)")
        self._note_security_warnings(config)
        import secrets as _secrets
        self._api_secret = _secrets.token_hex(16)
        self._active_mode = MODE_HTTP_PROXY
        self.protection_status = {}
        # The user explicitly switched the kill-switch on and it cannot be in
        # force here — report it rather than let the ticked box imply it is.
        # (The STUN block defaults on, so it is covered by the log line below
        # instead of by a warning on every single connect.)
        if self.settings.get("kill_switch", False):
            self.protection_status["kill_switch"] = "unsupported"
        self._log("[*] В режиме прокси нет kill-switch и блокировки WebRTC: "
                  "это правила firewall, им нужны права администратора")

        try:
            cfg_path = sing_box_config.write_proxy_config(
                config, direct_domains, server_ip=server_ip,
                route_ru_direct=bool(self.settings.get("route_ru_direct", True)),
                api_secret=self._api_secret,
            )
            ok, msg = sing_box_config.check_config(cfg_path)
            if not ok:
                raise ConnectionError(tr("err.singbox_rejected_config", msg=msg))
            try:
                self.sing_box_process.start(cfg_path)
            except Exception as e:
                raise ConnectionError(
                    tr("err.singbox_start_failed", error=e)) from e
            if not self._wait_until_running(4.0):
                tail = "\n".join(self.sing_box_process.recent_logs()[-8:])
                raise ConnectionError(
                    tr("err.singbox_died_on_start") + (f":\n{tail}" if tail else "."))
            self._log("[*] Проверяю, что прокси sing-box пропускает трафик…")
            if not self._wait_for_singbox_ready(15.0):
                diag = [l for l in self.sing_box_process.recent_logs()
                        if not is_benign_noise(l, live=False)]
                tail = "\n".join(diag[-6:])
                raise ConnectionError(
                    tr("err.singbox_no_real_traffic")
                    + (tr("err.singbox_diag_tail", tail=tail) if tail else ""))
            self.sing_box_process.mark_live()
        except Exception:
            self.sing_box_process.stop()
            paths.remove_runtime_configs()
            self._active_mode = None
            self._api_secret = ""
            self.protection_status = {}
            raise

        # Only now, with a listener that is proven to carry traffic, touch the
        # system: pointing the proxy at a dead port would cut the user off.
        host, port = (sing_box_config.PROXY_LISTEN_HOST,
                      sing_box_config.PROXY_LISTEN_PORT)
        self.system_proxy_applied = proxy_session.begin(host, port)
        if self.system_proxy_applied:
            self._log(f"[*] Системный прокси включён ({host}:{port}) — браузеры "
                      "и приложения, использующие системный прокси, идут через VPN.")
        else:
            # Not a failed connect: the listener works and can be used by
            # anything pointed at it by hand. But it is NOT what the user
            # expects from pressing Connect, so it is said plainly.
            self._log(f"[!] Не удалось включить системный прокси (macOS не "
                      f"разрешила менять сетевые настройки этой учётной записи). "
                      f"Прокси работает на {host}:{port} — укажи его вручную в "
                      f"Системные настройки → Сеть → Прокси или в браузере.")
            app_log.log("[proxy] system proxy NOT applied; listener is up")

        self._active = config
        self._active_engine = ENGINE_SING_BOX
        self._server_ip = server_ip
        self._egress_fp = self._egress_fingerprint()

    def _disarm_session_firewall(self) -> None:
        """Undo every firewall rule a connect attempt armed. For the failure
        paths: a connect that dies after arming must not leave rules behind
        while the UI reports "disconnected" — that half-state is exactly what
        users can't see. Both rules share one lifecycle, so they are removed
        together; before this, each path listed its own subset and the
        post-start path forgot the STUN rule."""
        # ...except a kill-switch that was already being held when this
        # attempt began: a reconnect that fails is still the tunnel-down case
        # the rules exist for, so they stay until the user releases them.
        if not self.killswitch_held:
            self.release_killswitch()
        try:
            webrtc_block.remove()
        except Exception:
            pass
        self.protection_status = {}

    def _killswitch_in_force(self) -> bool:
        return self._killswitch_armed

    def _killswitch_wanted(self) -> bool:
        """Whether the next TUN connect will arm the kill-switch."""
        return (bool(self.settings.get("kill_switch", False))
                and killswitch.is_supported() and admin.is_admin())

    def release_killswitch(self) -> None:
        """Take the kill-switch down for good: a user disconnect, the setting
        switched off, or quit. A removal that fails leaves the machine without
        internet, so it is said out loud rather than swallowed."""
        self.protection_status.pop("kill_switch", None)
        try:
            # force: when we installed rules ourselves, delete without asking
            # the firewall first whether they exist.
            gone = killswitch.remove(force=self._killswitch_armed) is not False
        except Exception as e:
            gone = False
            app_log.log(f"[protection] kill_switch removal raised: {e}")
        if not gone and not (self._killswitch_armed or self.killswitch_held):
            # Nothing of this session's, yet the firewall would not confirm it
            # is clean (leftovers we may not delete, or netsh not answering).
            # Startup reports real leftovers; don't invent a blocked state here.
            app_log.log("[protection] kill_switch state could not be confirmed clean")
            return
        if not gone:
            # Still blocking. Keep saying so (the home screen shows the held
            # state) and keep the flags, so the next disconnect / untick / quit
            # tries again instead of believing the machine is clean.
            self.killswitch_held = self._killswitch_armed = True
            self._log("[!] Kill-switch: правила firewall не сняты — интернет может "
                      "оставаться заблокированным. Перезапусти KaproTUN от "
                      "администратора: при запуске он убирает свои правила.")
            app_log.log("[protection] kill_switch rules could NOT be removed")
            return
        self.killswitch_held = self._killswitch_armed = False
        self._held_server_ip = ""

    def _routing_plan(self) -> dict:
        """Split-routing switches for the next TUN session. Under the
        kill-switch only the server and the LAN are reachable past the tunnel,
        so `lockdown` makes the config route nothing direct (see killswitch.py)."""
        return {
            "route_ru_direct": bool(self.settings.get("route_ru_direct", True)),
            "high_speed": bool(self.settings.get("high_speed", False)),
            "games_direct": bool(self.settings.get("games_direct", True)),
            "bypass_apps": list(self.settings.get("bypass_apps", []) or []),
            "lockdown": self._killswitch_wanted(),
        }

    def _note_security_warnings(self, config: ProxyConfig) -> None:
        """Say when a server link asks for less security than the user would
        assume (certificate not verified, a pin the engine cannot check)."""
        from .parser import security_warnings
        # The name comes from a subscription: printable characters only.
        name = "".join(ch for ch in str(config.name) if ch.isprintable())[:80]
        for note in security_warnings(config):
            self._log(f"[!] {note}")
            app_log.log(f"[security] {name}: {note}")

    def _tun_admin_message(self) -> str:
        """Per-OS 'TUN needs admin' message."""
        if sys.platform == "win32":
            return tr("err.admin_required_win")
        if sys.platform == "darwin":
            return tr("err.admin_required_mac")
        return tr("err.admin_required_linux")

    def current_engine(self) -> str:
        """Always the sing-box engine (the only one since v3.1.0)."""
        return ENGINE_SING_BOX

    def _maybe_arm_killswitch(self, server_ip: str = "") -> None:
        """Install the kill-switch firewall rules for this server if the user
        enabled it. No-op when off; reported in protection_status when it could
        not be armed (not Windows / not admin / netsh refused). Never raises."""
        if not self.settings.get("kill_switch", False):
            return
        if not killswitch.is_supported():
            self.protection_status["kill_switch"] = "unsupported"
            self._log("[!] Kill-switch пока работает только на Windows")
            return
        if not admin.is_admin():
            self.protection_status["kill_switch"] = "needs_admin"
            self._log("[!] Kill-switch требует админа — пропускаю")
            return
        if killswitch.install(server_ips=[server_ip] if server_ip else [],
                              dns_servers=killswitch.physical_dns_servers()):
            self.protection_status["kill_switch"] = "active"
            self._killswitch_armed = True
            self._held_server_ip = server_ip
            self._log("[*] Kill-switch активирован (firewall блокирует весь "
                      "трафик мимо туннеля, кроме VPN-сервера и локальной сети)")
        elif self.killswitch_held:
            self.protection_status["kill_switch"] = "failed"
            self._log("[!] Kill-switch: не удалось обновить firewall-правила под "
                      "этот сервер — прежняя блокировка остаётся в силе")
            app_log.log("[protection] kill_switch re-arm failed, old rules kept")
        else:
            self.protection_status["kill_switch"] = "failed"
            self._log("[!] Не удалось установить firewall-правила kill-switch "
                      "— продолжаю без него")
            app_log.log("[protection] kill_switch requested but NOT armed")

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
            self.protection_status["webrtc"] = "unsupported"
            self._log("[!] WebRTC-leak protection пока работает только на Windows")
            return
        if not admin.is_admin():
            self.protection_status["webrtc"] = "needs_admin"
            # In HTTP-proxy mode we usually aren't admin (don't need it
            # for system_proxy on Windows). Don't spam this — log once
            # at info level so the user knows why protection is off.
            self._log("[!] WebRTC-leak protection требует админа — пропускаю "
                      "(перейди в TUN-режим для админ-прав)")
            return
        if webrtc_block.install():
            self.protection_status["webrtc"] = "active"
            self._log("[*] WebRTC-leak protection активирована "
                      "(блок UDP к STUN-портам 3478/5349/19302/19305-19309)")
        else:
            self.protection_status["webrtc"] = "failed"
            self._log("[!] Не удалось установить WebRTC-block firewall-правило "
                      "— браузер может узнать реальный IP через STUN")
            app_log.log("[protection] webrtc requested but NOT armed")

    def inactive_protections(self) -> list[tuple[str, str]]:
        """(protection, reason) for every protection the user switched on that
        is NOT actually in force this session. Empty when all is well."""
        return [(name, state) for name, state in self.protection_status.items()
                if state != "active"]

    def _atexit_cleanup(self) -> None:
        try:
            self.disconnect()
        except Exception:
            pass
