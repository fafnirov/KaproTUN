"""A check the packaged application runs on itself.

    KaproTUN --selftest [folder]        what a user can safely run
    KaproTUN --selftest-e2e [folder]    plus a real connection (for the build)

Why it exists. The Windows build is used every day by the person who makes
it; the macOS and Linux builds were for a long time only ever built. The test
suite runs the source on Linux — it never ran the packaged application, and
some things break only there: v4.1.5 was a macOS build whose own TLS had no
certificate authorities, so no server could be connected to, and nothing
short of running the built app on a Mac could have shown it. The release
workflow now does exactly that, on every platform it builds for.

What it checks:

  tls      an HTTPS request by the standard library verifies its certificate
  window   the main window can be created and drawn (a screenshot is saved)
  engine   (e2e) the pinned engine downloads, verifies and runs
  connect  (e2e) a real proxy-mode session through a server started here on
           the loopback: the traffic check passes, the system proxy is
           pointed at the client and given back exactly as it was

Everything happens in a temporary data folder: the user's servers, settings
and engine are not read or touched. The e2e part changes one thing on the
machine — the system proxy, for the length of the session — and only runs
where the client would use proxy mode (macOS without root); on a TUN platform
it is skipped, never forced: a self-test must not take the network down.

The result goes to stdout and to <folder>/selftest.json; the exit code is 0
only if nothing failed.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path

OK, FAIL, SKIP = "ok", "FAIL", "skip"
_LOOPBACK_PORT = 18388


class Report:
    def __init__(self, folder: Path):
        self.folder = folder
        self.items: list[dict] = []

    def add(self, stage: str, status: str, detail: str = "", **data) -> None:
        self.items.append(dict(stage=stage, status=status, detail=detail, **data))
        print(f"[{status}] {stage}: {detail}", flush=True)

    def failed(self) -> bool:
        return any(i["status"] == FAIL for i in self.items)

    def save(self) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / "selftest.json").write_text(
                json.dumps(self.items, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"[warn] the report could not be saved: {e}", flush=True)


def _stage(report: Report, name: str, fn) -> None:
    """Run one check; whatever it raises is that check's failure, not ours."""
    try:
        status, detail, data = fn()
    except Exception as e:
        last = traceback.format_exc(limit=4).strip().splitlines()[-1]
        report.add(name, FAIL, f"{type(e).__name__}: {e}", trace=last)
        return
    report.add(name, status, detail, **(data or {}))


def _check_environment():
    from . import __version__
    from .core import admin
    frozen = bool(getattr(sys, "frozen", False))
    return OK, (f"KaproTUN {__version__}, {sys.platform} {platform.machine()}, "
                f"{'packaged' if frozen else 'from source'}, "
                f"{'administrator' if admin.is_admin() else 'ordinary user'}"), \
        {"version": __version__, "platform": sys.platform, "machine": platform.machine(),
         "frozen": frozen}


def _check_tls():
    """The request the connect path's traffic check makes, minus the tunnel."""
    import ssl
    import urllib.request
    from .core import tls_trust
    how = tls_trust.ensure_ca_bundle()
    paths = ssl.get_default_verify_paths()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request("https://www.gstatic.com/generate_204",
                                 headers={"User-Agent": "KaproTUN-selftest"})
    with opener.open(req, timeout=15) as resp:
        code = resp.status
    return (OK if 200 <= code < 400 else FAIL,
            f"HTTPS verified (HTTP {code}); certificate authorities from: {how}",
            {"trust": how, "cafile": paths.cafile, "capath": paths.capath})


def _pump(app, seconds: float) -> None:
    """Let the event loop run for a while (QtTest is not in the bundle)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _check_window(folder: Path):
    """Create the real main window and draw it once."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .core import i18n, storage
    from .gui.main_window import MainWindow
    from .gui.styles import get_qss

    i18n.init_from_settings(storage.load_settings().get("language"))
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(get_qss("dark"))
    window = MainWindow()
    window.show()
    _pump(app, 1.5)
    for attr in vars(window).values():
        if isinstance(attr, QTimer):
            attr.stop()
    shot = window.grab()
    target = folder / "window.png"
    folder.mkdir(parents=True, exist_ok=True)
    saved = shot.save(str(target))
    size = f"{window.width()}x{window.height()}"
    home_ok = window.home_page.isVisible()
    return (OK if saved and home_ok and not shot.isNull() else FAIL,
            f"main window {size} on platform '{app.platformName()}', screenshot "
            f"{'saved' if saved else 'NOT saved'}",
            {"size": size, "qpa": app.platformName(), "screenshot": str(target) if saved else ""})


def _check_splash(folder: Path):
    """Draw the start-up splash: every step, and what a failed start looks like."""
    from PySide6.QtWidgets import QApplication
    from . import __version__
    from .core import i18n, storage
    from .gui import boot
    from .gui.styles import get_qss

    i18n.init_from_settings(storage.load_settings().get("language"))
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(get_qss("dark"))
    splash = boot.SplashWindow(__version__)
    splash.show()
    for k in range(1, boot.STEP_COUNT + 1):
        splash.set_step(k)
        _pump(app, 0.15)
    shot = splash.grab()
    folder.mkdir(parents=True, exist_ok=True)
    saved = shot.save(str(folder / "splash.png"))
    # A splash that drew has the mark, text and the bar on it; a blank one is
    # a single colour.
    from PySide6.QtCore import Qt
    small = shot.toImage().scaled(96, 96, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    drawn = len({small.pixel(x, y) for x in range(96) for y in range(96)}) >= 8
    splash.show_error("RuntimeError: self-test")
    _pump(app, 0.15)
    said = splash.failed() and splash.error.isVisible()
    splash.hide()
    splash.deleteLater()
    size = f"{splash.width()}x{splash.height()}"
    return (OK if saved and said and drawn else FAIL,
            f"splash {size}, {boot.STEP_COUNT} steps{'' if drawn else ', BLANK'}, "
            f"a failed start is shown: {'yes' if said else 'NO'}",
            {"size": size, "screenshot": str(folder / 'splash.png') if saved else ""})


def _check_engine():
    import subprocess
    from .core import paths, sing_box_installer
    sing_box_installer.download_and_install()
    if not sing_box_installer.is_installed():
        return FAIL, "the engine was downloaded but does not match the pinned build", {}
    with sing_box_installer.hold_engine() as guard:
        exe = guard.real_path(paths.sing_box_exe().name)
        out = subprocess.run([str(exe), "version"], capture_output=True, text=True, timeout=20)
    first = (out.stdout or out.stderr).strip().splitlines()[:1]
    want = sing_box_installer.SINGBOX_PINNED_VERSION.lstrip("v")
    ok = out.returncode == 0 and bool(first) and want in first[0]
    return (OK if ok else FAIL, f"{first[0] if first else 'no output'} (pinned {want})",
            {"version_line": first[0] if first else ""})


def _wait_port(port: int, seconds: float) -> bool:
    import socket
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _check_connect(folder: Path):
    """A whole proxy-mode session against a server of our own on the loopback."""
    import base64
    import secrets
    import subprocess
    from .core import controller, dns_health, paths, sing_box_config, sing_box_installer
    from .core import system_proxy
    from .core.parser import parse

    log: list[str] = []
    manager = controller.ConnectionManager(on_log=log.append)
    if manager.planned_mode() != controller.MODE_HTTP_PROXY:
        return SKIP, ("this platform connects through a TUN device; a self-test does not "
                      "create one (it would take over the machine's routing)"), {}

    method, password = "chacha20-ietf-poly1305", secrets.token_urlsafe(18)
    server_config = folder / "selftest-server.json"
    folder.mkdir(parents=True, exist_ok=True)
    server_config.write_text(json.dumps({
        "log": {"level": "warn"},
        "inbounds": [{"type": "shadowsocks", "listen": "127.0.0.1",
                      "listen_port": _LOOPBACK_PORT, "method": method, "password": password}],
        "outbounds": [{"type": "direct"}],
    }), encoding="utf-8")

    data: dict = {}
    server = None
    guard = sing_box_installer.hold_engine()
    try:
        server = subprocess.Popen(
            [str(guard.real_path(paths.sing_box_exe().name)), "run", "-c", str(server_config)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if not _wait_port(_LOOPBACK_PORT, 15):
            return FAIL, "the loopback test server did not start", {}
        userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
        config = parse(f"ss://{userinfo}@127.0.0.1:{_LOOPBACK_PORT}#selftest")

        before = system_proxy.get_state()
        try:
            manager.connect(config, [])
        except controller.ConnectionError as e:
            data["log"] = log[-12:]
            return FAIL, f"connect failed: {' '.join(str(e).split())[:400]}", data
        try:
            listener = (f"http://{sing_box_config.PROXY_LISTEN_HOST}:"
                        f"{sing_box_config.PROXY_LISTEN_PORT}")
            data["traffic_through_listener"] = dns_health.http_probe(
                listener, timeout=10, urls=("https://www.gstatic.com/generate_204",))
            data["probe_error"] = dns_health.last_http_error()
            data["system_proxy_applied"] = bool(manager.system_proxy_applied)
            data["system_proxy_points_at_us"] = system_proxy.mac_proxy_points_at(
                sing_box_config.PROXY_LISTEN_HOST, sing_box_config.PROXY_LISTEN_PORT)
        finally:
            manager.disconnect()
        after = system_proxy.get_state()
        # Exactly: down to the address a switched-off slot remembers.
        data["system_proxy_restored"] = (after == before)
        if after != before:
            data["proxy_before"], data["proxy_after"] = before, after
        data["refusal"] = system_proxy.last_refusal()
        data["still_points_at_us"] = system_proxy.mac_proxy_points_at(
            sing_box_config.PROXY_LISTEN_HOST, sing_box_config.PROXY_LISTEN_PORT)
        data["services"] = sorted((before.get("services") or {}))
        data["log"] = log[-12:]
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        guard.close()
        try:
            server_config.unlink()
        except OSError:
            pass

    problems = []
    if not data["traffic_through_listener"]:
        problems.append(f"no traffic through the client's listener ({data['probe_error']})")
    if data["system_proxy_applied"] != data["system_proxy_points_at_us"]:
        problems.append("the client's idea of the system proxy differs from the system's")
    if not data["system_proxy_restored"] or data["still_points_at_us"]:
        problems.append("the system proxy was not given back as it was")
    if problems:
        return FAIL, "; ".join(problems), data
    proxy = ("system proxy switched on and given back exactly as it was"
             if data["system_proxy_applied"]
             else f"macOS refused the system proxy change ({data['refusal'] or 'no reason given'}), "
                  "and the client said so")
    return OK, f"connected through the loopback server, traffic passes; {proxy}", data


def run(argv: list[str]) -> int:
    e2e = "--selftest-e2e" in argv
    rest = [a for a in argv if not a.startswith("--")]
    folder = Path(rest[0]).expanduser().resolve() if rest else \
        Path(tempfile.gettempdir()) / "kaprotun-selftest"

    # A data folder of its own: the user's servers, settings and engine are
    # neither read nor changed, and nothing here can start a real session.
    from .core import paths
    data_dir = Path(tempfile.mkdtemp(prefix="kaprotun-selftest-data-"))
    paths.app_data_dir = lambda: data_dir

    report = Report(folder)
    _stage(report, "environment", _check_environment)
    _stage(report, "tls", _check_tls)
    if e2e:
        _stage(report, "engine", _check_engine)
        _stage(report, "connect", lambda: _check_connect(folder))
    _stage(report, "splash", lambda: _check_splash(folder))
    _stage(report, "window", lambda: _check_window(folder))
    report.save()
    print(f"selftest: {'FAILED' if report.failed() else 'passed'}; report in {folder}", flush=True)
    return 1 if report.failed() else 0
