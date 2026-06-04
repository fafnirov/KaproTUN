"""Read-only diagnostics snapshot for a stable-release soak test.

Prints, without changing anything:
  * which KaproTUN helper processes are running (sing-box / xray / tun2socks /
    hysteria) — the key check for "is the right engine live?";
  * the configured TUN engine + mode from settings.json;
  * whether any runtime config (with secrets) is lingering on disk — there
    should be NONE while disconnected;
  * the tail of app.log (already redacted at write time).

NOTHING is killed, deleted, or modified. Safe to run at any point during a
soak test. Usage:

    python -m kapro_tun.scripts.collect_diagnostics

On Windows the process check uses psutil if available, else `tasklist`.
"""
from __future__ import annotations

import os
import sys

# Make it runnable straight from the repo root (mirror smoke_test.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HELPERS = ("sing-box", "xray", "tun2socks", "hysteria")


def _running_helpers() -> dict[str, list[int]]:
    """Map helper-name → list of PIDs currently running (best-effort)."""
    found: dict[str, list[int]] = {h: [] for h in _HELPERS}
    try:
        import psutil  # type: ignore
        for proc in psutil.process_iter(["name", "pid"]):
            name = (proc.info.get("name") or "").lower()
            for h in _HELPERS:
                if name.startswith(h):
                    found[h].append(proc.info["pid"])
        return found
    except Exception:
        pass
    # Fallback: tasklist on Windows, ps on POSIX.
    try:
        import subprocess
        if sys.platform == "win32":
            out = subprocess.run(["tasklist"], capture_output=True, text=True,
                                  timeout=10).stdout.lower()
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm"], capture_output=True,
                                 text=True, timeout=10).stdout.lower()
        for h in _HELPERS:
            if h in out:
                found[h].append(-1)  # present (PID unknown via this path)
    except Exception:
        pass
    return found


def main() -> int:
    from kapro_tun.core import paths, storage

    print("=== KaproTUN diagnostics (read-only) ===\n")

    # 1) Engine + mode from settings.
    try:
        s = storage.load_settings()
        print(f"mode         : {s.get('mode')}")
        print(f"tun_engine   : {s.get('tun_engine')}")
        print(f"kill_switch  : {s.get('kill_switch')}")
        print(f"dns_leak_prot: {s.get('dns_leak_protection')}")
    except Exception as e:
        print(f"settings     : <error: {e}>")

    # 2) Running helper processes.
    print("\n-- running helper processes --")
    helpers = _running_helpers()
    for h in _HELPERS:
        pids = helpers.get(h) or []
        state = "running " + (f"(pid {pids})" if pids and pids != [-1] else "") if pids else "—"
        print(f"  {h:<10}: {state}")
    sb = bool(helpers.get("sing-box"))
    legacy = bool(helpers.get("xray")) or bool(helpers.get("tun2socks"))
    if sb and legacy:
        print("  ⚠ BOTH sing-box AND xray/tun2socks are running — unexpected for a "
              "single sing-box session.")
    elif sb:
        print("  ✓ sing-box session: sing-box running, no xray/tun2socks (expected).")
    elif legacy:
        print("  legacy session (or HTTP mode): xray/tun2socks running.")

    # 3) Runtime config leftovers (should be NONE while disconnected).
    print("\n-- runtime config files (should be absent while disconnected) --")
    for fn in (paths.sing_box_runtime_config_file, paths.runtime_config_file,
               paths.hysteria_config_file):
        try:
            p = fn()
            print(f"  {p.name:<24}: {'PRESENT ⚠' if p.exists() else 'absent ✓'}")
        except Exception as e:
            print(f"  <{fn.__name__}>: error {e}")

    # 4) app.log tail (already redacted at write time).
    print("\n-- app.log (last 20 lines, redacted at write) --")
    try:
        log = paths.app_log_file()
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-20:]:
                print("  " + line)
        else:
            print("  (no app.log yet)")
    except Exception as e:
        print(f"  <error reading app.log: {e}>")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
