"""Application entry point."""
from __future__ import annotations

import signal
import socket
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QSplashScreen

from .core import (autostart, firewall_sweep, i18n, ipv6_block, killswitch,
                   storage, system_proxy, tun_recovery, webrtc_block)
from .gui import icons
from .gui.main_window import MainWindow
from .gui.singleton import SingleInstanceGuard
from .gui.styles import DARK_QSS, get_qss  # noqa: F401  (DARK_QSS kept for back-compat)

# Hidden-window flags for subprocess on Windows (no console flash for
# the orphan-killer taskkill calls).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _kill_orphan_helpers() -> None:
    """Force-kill any leftover xray / tun2socks / hysteria / sing-box processes.

    Why this is safe: the single-instance guard runs immediately before
    this — so if we got this far, we are the ONLY KaproTUN running.
    Anything with one of those image names is an orphan from a previous
    crashed/force-closed KaproTUN run, holding file handles or — critically —
    the shared "KaproTun" TUN adapter, which blocks the next download/start.
    Kill them.

    Why this matters in practice: on Windows, an open file handle in a
    running process makes the file un-deletable AND un-overwritable (a fresh
    xray.exe download fails with PermissionError). And BOTH TUN engines name
    their device "KaproTun" — an orphan sing-box.exe / tun2socks.exe still
    owning that adapter makes the next sing-box start die with
    "configure tun interface: Cannot create a file when that file already
    exists." sing-box was missing from this list before v3.0.6.
    """
    # On Unix `pkill` is the closest equivalent. Both calls are
    # idempotent — they exit non-zero when no matching process exists,
    # which we swallow.
    if sys.platform == "win32":
        for name in ("xray.exe", "tun2socks.exe", "hysteria.exe", "sing-box.exe"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", name],
                    capture_output=True, timeout=3, creationflags=_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        for name in ("xray", "tun2socks", "hysteria", "sing-box"):
            try:
                subprocess.run(
                    ["pkill", "-9", "-x", name],
                    capture_output=True, timeout=3,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        return

    # v3.0.9: a force-killed (/F) sing-box can't run its own auto_route cleanup,
    # so a leftover default route via the dead "KaproTun" adapter can survive and
    # black-hole the machine ("Доступ в интернет заблокирован"). If the adapter or
    # its routes linger, delete ONLY routes bound to the KaproTun interface — never
    # the real NIC's default route. No-op when the adapter already vanished with
    # the process (the common case), so it's safe belt-and-suspenders.
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-NetRoute -InterfaceAlias 'KaproTun' -ErrorAction SilentlyContinue"
             " | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue"],
            capture_output=True, timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _clear_stale_system_proxy() -> None:
    """If the registry still says system-proxy points at 127.0.0.1:<our port>
    but nothing's listening there, clear it.

    Happens when a previous HTTP-mode session was killed hard (Task Manager,
    Windows reboot mid-session) before disconnect() could restore the
    registry. Result: every browser/app on the machine tries to send
    traffic through a dead port and gets "connection refused" until the
    user manually clears it via Windows Settings.

    The single-instance guard runs BEFORE this, so if we reach this point
    no other KaproTUN is running — meaning a 127.0.0.1:<port> proxy entry
    is guaranteed stale.
    """
    try:
        state = system_proxy.get_state()
    except Exception:
        return
    if not state or not state.get("enable"):
        return
    server = str(state.get("server", ""))
    if ":" not in server:
        return
    host, _, port_s = server.rpartition(":")
    # Only touch proxies that point at our local listener — leave a real
    # corporate/personal proxy entry alone.
    if host not in ("127.0.0.1", "localhost", "::1"):
        return
    try:
        listen_port = int(storage.load_settings().get("listen_port", 2080))
    except Exception:
        listen_port = 2080
    try:
        if int(port_s) != listen_port:
            return
    except ValueError:
        return
    # Probe — fast TCP connect with tight timeout. If anything responds,
    # leave the registry alone (we'll attach to it).
    try:
        with socket.create_connection((host, listen_port), timeout=0.3):
            return
    except OSError:
        pass  # dead port → fall through to clear
    try:
        system_proxy.disable_proxy()
    except Exception:
        pass


def _run_app() -> int:
    # Let Ctrl+C in the terminal kill the app cleanly
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # --minimized: boot straight to the tray, don't pop the main window
    # (used by the Windows Run-key registration for auto-start on login).
    start_minimized = autostart.MINIMIZED_FLAG in sys.argv

    # v1.22.0 rename: carry over a pre-rename ("KaproVPN") auto-start entry
    # once. The app-data folder migrates lazily on first app_data_dir() call.
    autostart.migrate_legacy()

    app = QApplication(sys.argv)
    # i18n must be initialised BEFORE any window/widget is built, because
    # those build their UI text with tr() at construction time. After
    # QApplication so QLocale.system() works inside detect_system_locale().
    i18n.init_from_settings(storage.load_settings().get("language"))
    app.setApplicationName("KaproTUN")
    app.setOrganizationName("KaproTUN")
    # v1.13.0: theme is now user-selectable (Settings → Тема: Auto/Dark/Light).
    # Auto follows OS via QStyleHints.colorScheme() at this point in startup —
    # QApplication is constructed above, so the style hints are queryable.
    _settings_for_theme = storage.load_settings()
    app.setStyleSheet(get_qss(str(_settings_for_theme.get("theme", "auto"))))
    app.setWindowIcon(icons.app_icon())
    # Don't exit when the user clicks X — we hide to tray instead.
    # Real exit goes through the tray-menu "Выход" item, which calls
    # QApplication.quit() explicitly.
    app.setQuitOnLastWindowClosed(False)

    # Single-instance check — bail early if another KaproTUN is already
    # running (and tell it to show its window). Without this, double-
    # clicking the shortcut while the app is in the tray would spawn a
    # second xray fighting for port 2080.
    guard = SingleInstanceGuard()
    if not guard.acquire():
        # Already running — we've asked the primary to show itself.
        return 0

    # Defensive: a crashed previous run may have left the system proxy
    # pointing at our dead local port. Clear it before any downloads
    # (xray installer, geoip, updater) try to route through nothing.
    _clear_stale_system_proxy()

    # Also defensive: a crashed previous run may have left orphan
    # xray.exe / tun2socks.exe processes holding file handles, which
    # blocks the very next download with PermissionError. Single-
    # instance guard already proved we're the only KaproTUN, so any
    # leftover helper process is by definition orphaned. Kill them.
    _kill_orphan_helpers()

    # Third defensive sweep: if the previous run crashed while
    # kill-switch was active, the firewall rules are still in place
    # — every app on the machine has no internet. Clear them so the
    # user isn't trapped (we'll re-install on their next connect if
    # they still have the setting enabled).
    try:
        if killswitch.is_active():
            killswitch.remove()
    except Exception:
        pass

    # Same defensive sweep for the v1.11.0 IPv6 leak protection rule.
    # If a previous run crashed mid-session, our 2000::/3 outbound block
    # is still active, the user has no IPv6 internet at all. Wipe it on
    # next launch — we'll re-arm at the next TUN-mode connect if the
    # setting's still on.
    try:
        if ipv6_block.is_active():
            ipv6_block.remove()
    except Exception:
        pass

    # v1.16.0: same orphan-rule sweep for the WebRTC STUN-block.
    # Without this, a crash mid-session leaves the user's browser
    # WebRTC permanently broken (Discord-web, Google Meet, etc.)
    # until they manually run `netsh advfirewall firewall delete rule
    # name="KaproTUN-webrtc-block-stun"`. Nobody does that.
    try:
        if webrtc_block.is_active():
            webrtc_block.remove()
    except Exception:
        pass

    splash = None
    if not start_minimized:
        splash = QSplashScreen(icons.splash_pixmap(320), Qt.WindowStaysOnTopHint)
        splash.show()
        splash.showMessage("Запуск…", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
        app.processEvents()

    window = MainWindow()

    # Fourth defensive sweep (v2.1.4): TUN mode clears the physical NIC's DNS
    # for leak protection and only restores it on a clean disconnect. If the
    # previous run was force-killed / crashed / lost power while connected, that
    # interface is still "DNS = none" with no tunnel to carry queries — the
    # machine resolves nothing until the user restarts the adapter by hand.
    # recover() reads the crash journal and restores DHCP DNS on that exact
    # interface. Idempotent and silent when there's nothing to recover. Runs
    # before the optional auto-connect below so we never reconnect on top of a
    # half-broken DNS state.
    try:
        for _action in tun_recovery.recover():
            window.logs_page.append(f"[восстановление] {_action}")
    except Exception:
        pass

    # v3.0.9: brand-prefix firewall sweep. The per-feature removes above only
    # delete their EXACT current rule names — they miss orphans from pre-rename
    # "KaproVPN-*" builds and ad-hoc suffixes (e.g. "KaproVPN-ipv6-block-TEST"),
    # which block 2000::/3 outbound PERMANENTLY and surface in the browser as
    # ERR_NETWORK_ACCESS_DENIED even with the VPN off. Sweep them by prefix.
    try:
        for _name in firewall_sweep.sweep():
            window.logs_page.append(f"[firewall] удалено правило-сирота: {_name}")
    except Exception:
        pass

    # v3.0.9: ANY QApplication.quit() (tray button, OS session-end, installer
    # ping) must tear the tunnel down via the existing disconnect() — not just
    # the tray path — so a quit can't orphan sing-box with the TUN up. Guarded
    # against double-run inside _on_quit_for_real.
    app.aboutToQuit.connect(window._on_quit_for_real)

    # Re-route "show" pings from any future second-launch attempts to
    # the same code path the tray icon uses.
    guard.setParent(window)  # keep the guard alive for the window's lifetime
    guard.show_requested.connect(window._on_show_window)
    # The installer pings this before a reinstall/uninstall so we shut down
    # cleanly (disconnect → restore system proxy + firewall) and release our
    # exe lock, instead of being force-killed mid-session.
    guard.quit_requested.connect(window._on_quit_for_real)

    def reveal() -> None:
        if not start_minimized:
            window.show()
            if splash is not None:
                splash.finish(window)
        # Optional auto-connect on launch — wait a beat after window
        # construction so any first-run installer dialogs finish first.
        if window.manager.settings.get("autoconnect_on_launch", False):
            QTimer.singleShot(800, window.trigger_autoconnect)

    # Tiny delay before swapping splash → window, so the splash is
    # actually visible. Without this, fast machines flash it for 1 frame.
    QTimer.singleShot(600 if not start_minimized else 0, reveal)

    return app.exec()


def main() -> int:
    """Entry point with a startup safety net.

    Any unhandled exception during startup is caught and routed to a
    friendly crash dialog + log (see core.crash_handler) instead of the
    raw PyInstaller traceback popup. Without this, a startup crash is
    effectively unrecoverable for the user: it happens before the in-app
    auto-updater runs, so a broken build can't fix itself.

    KeyboardInterrupt and SystemExit are BaseException, not Exception, so
    they pass through untouched (Ctrl+C and explicit sys.exit still work).
    """
    try:
        return _run_app()
    except Exception as exc:
        from .core import crash_handler
        return crash_handler.handle_startup_crash(exc)


if __name__ == "__main__":
    sys.exit(main())
