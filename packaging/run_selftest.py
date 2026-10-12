"""Run the application that was just built and let it check itself.

    python packaging/run_selftest.py <report-folder>

Used by the release workflow after the build, on every platform. It starts
the PACKAGED application (not the source) with --selftest-e2e, prints the
report and passes on the result: a build whose application cannot verify
TLS, cannot fetch and run its engine, cannot draw its window or — where the
platform uses proxy mode — cannot carry a connection end to end, is not
released.

On macOS it also starts the app the way a user does (from the .app bundle,
no arguments) and checks that it is still running a little later.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 420


def _command(dist: Path) -> tuple[list[str], dict]:
    env = dict(os.environ)
    if sys.platform == "win32":
        return [str(dist / "KaproTUN.exe")], env
    if sys.platform == "darwin":
        return [str(dist / "KaproTUN.app" / "Contents" / "MacOS" / "KaproTUN")], env
    # Linux runners have no display; the AppImage is unpacked rather than
    # mounted so that the check does not depend on FUSE being set up.
    env["QT_QPA_PLATFORM"] = "offscreen"
    return [str(dist / "KaproTUN-Linux-x64.AppImage"), "--appimage-extract-and-run"], env


def _seed_proxy_settings() -> None:
    """macOS: give the runner proxy settings of its "own" before the check —
    a bypass list, and a web proxy that is switched off but remembers an
    address. A machine with nothing set would make "the system proxy was
    given back exactly as it was" true of almost any restore."""
    listed = subprocess.run(["networksetup", "-listallnetworkservices"],
                            capture_output=True, text=True).stdout.splitlines()[1:]
    for service in (line.strip() for line in listed):
        if not service or service.startswith("*"):
            continue
        for command in (["-setwebproxy", service, "proxy.corp.example", "3128"],
                        ["-setwebproxystate", service, "off"],
                        ["-setproxybypassdomains", service, "*.corp.example", "10.0.0.0/8"]):
            subprocess.run(["networksetup", *command], capture_output=True, text=True)
        print(f"seeded proxy settings on '{service}'", flush=True)


def _launch_like_a_user(dist: Path, out: Path) -> bool:
    """macOS: open the bundle, wait, see that the app is still there."""
    app = dist / "KaproTUN.app"
    binary = str(app / "Contents" / "MacOS" / "KaproTUN")
    subprocess.run(["open", "-n", str(app)], check=False)
    time.sleep(25)
    alive = subprocess.run(["pgrep", "-f", binary], capture_output=True, text=True)
    running = alive.returncode == 0
    print(f"[{'ok' if running else 'FAIL'}] launch: the app started from its bundle is "
          f"{'still running after 25 s' if running else 'NOT running after 25 s'}", flush=True)
    # A picture of the screen, if the runner lets us take one (not required).
    subprocess.run(["screencapture", "-x", str(out / "desktop.png")], check=False,
                   capture_output=True)
    subprocess.run(["pkill", "-f", binary], check=False)
    return running


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "selftest-out").resolve()
    out.mkdir(parents=True, exist_ok=True)
    dist = Path("dist").resolve()
    command, env = _command(dist)
    if sys.platform == "darwin":
        _seed_proxy_settings()
    print("running:", " ".join(command), "--selftest-e2e", out, flush=True)
    started = time.time()
    try:
        done = subprocess.run(command + ["--selftest-e2e", str(out)], env=env,
                              capture_output=True, text=True, timeout=TIMEOUT_S)
        code, text = done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as e:
        code = 124
        text = f"{e.stdout or ''}{e.stderr or ''}\n[FAIL] the self-test did not finish in {TIMEOUT_S} s"
    print(text.strip(), flush=True)
    print(f"exit code {code} after {time.time() - started:.0f} s", flush=True)

    report = out / "selftest.json"
    if report.is_file():
        # A windowed Windows build has no console: the report file is how
        # its result is read.
        for item in json.loads(report.read_text(encoding="utf-8")):
            extra = {k: v for k, v in item.items() if k not in ("stage", "status", "detail")}
            print(f"  {item['status']:>4}  {item['stage']}: {item['detail']}", flush=True)
            if extra:
                print("        " + json.dumps(extra, ensure_ascii=False)[:1500], flush=True)
    else:
        print("[FAIL] no report was written", flush=True)
        code = code or 1

    if sys.platform == "darwin" and not _launch_like_a_user(dist, out):
        code = code or 1
    return code


if __name__ == "__main__":
    sys.exit(main())
