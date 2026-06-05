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

# The sing-box TUN resolver (auto_route sets the system DNS to this on the
# KaproTun adapter) vs a known-good public resolver, reached DIRECTLY. The whole
# point of the v3.0.7 fix: lookups via 10.255.0.3 must NOT time out for these.
_TUN_DNS = "10.255.0.3"
_DIRECT_DNS = "1.1.1.1"
_CDN_HOSTS = ("www.youtube.com", "youtubei.googleapis.com", "i.ytimg.com",
              "files.oaiusercontent.com", "yastatic.net")


def _nslookup(host: str, server: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Run `nslookup host server` read-only. Returns (resolved?, one-line note).

    A name is considered resolved if nslookup printed at least one A/AAAA
    'Address' that isn't the server's own address. Never raises."""
    import subprocess
    try:
        cf = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        out = subprocess.run(["nslookup", host, server], capture_output=True,
                             text=True, timeout=timeout, creationflags=cf).stdout
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout:.0f}s"
    except FileNotFoundError:
        return False, "nslookup not found"
    except Exception as e:
        return False, f"error: {e}"
    # Parse the answer section: 'Address(es)' lines AFTER the server block. The
    # first 'Address:' belongs to the server itself, so drop server's own IP.
    addrs: list[str] = []
    for raw in out.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("address:") or low.startswith("addresses:"):
            val = line.split(":", 1)[1].strip()
            if val and val != server and not val.endswith("#53"):
                addrs.append(val)
    # On Windows the server's own address appears once as 'Address: <server>'
    addrs = [a for a in addrs if a != server]
    if addrs:
        return True, ", ".join(addrs[:3])
    if "non-existent" in out.lower() or "nxdomain" in out.lower():
        return False, "NXDOMAIN"
    if "timed out" in out.lower() or "request to" in out.lower() and "timed-out" in out.lower():
        return False, "server timed out"
    return False, "no address returned"


def _dns_servers() -> list[str]:
    """The DNS servers configured on the KaproTun adapter (best-effort, read-only)."""
    import subprocess
    if sys.platform != "win32":
        return []
    try:
        ps = ("Get-DnsClientServerAddress -AddressFamily IPv4 | "
              "Where-Object {$_.InterfaceAlias -like '*KaproTun*'} | "
              "Select-Object -ExpandProperty ServerAddresses")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, text=True, timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


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

    # 4) DNS resolution through the TUN resolver vs a direct public resolver.
    #    This is the v3.0.7 acceptance check: CDN/YouTube/OpenAI names must NOT
    #    time out via 10.255.0.3 while they resolve fine via 1.1.1.1.
    print(f"\n-- DNS resolution: TUN {_TUN_DNS} (KaproTun) vs direct {_DIRECT_DNS} --")
    if sb:
        servers = _dns_servers()
        if servers:
            print(f"  KaproTun adapter DNS servers: {', '.join(servers)}")
        tun_fail = 0
        for host in _CDN_HOSTS:
            ok_t, note_t = _nslookup(host, _TUN_DNS)
            ok_d, note_d = _nslookup(host, _DIRECT_DNS)
            mark = "✓" if ok_t else "✗"
            print(f"  {mark} {host:<28} TUN[{_TUN_DNS}]: "
                  f"{'OK ' + note_t if ok_t else 'FAIL ' + note_t:<34} "
                  f"| direct[{_DIRECT_DNS}]: {'OK' if ok_d else 'FAIL ' + note_d}")
            if not ok_t:
                tun_fail += 1
        if tun_fail:
            print(f"  ⚠ {tun_fail}/{len(_CDN_HOSTS)} CDN names DO NOT resolve via the TUN "
                  f"resolver — YouTube/CDN/OpenAI would hang. (v3.0.7 regression!)")
        else:
            print("  ✓ all CDN/YouTube/OpenAI names resolve via the TUN resolver.")
    else:
        print("  (sing-box not running — skipping TUN-DNS probe)")

    # 5) app.log tail (already redacted at write time) + a crash/watchdog filter.
    print("\n-- app.log (last 20 lines, redacted at write) --")
    crash_lines: list[str] = []
    try:
        log = paths.app_log_file()
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-20:]:
                print("  " + line)
            # Highlight the lines that explain instability across the WHOLE log.
            for line in lines:
                low = line.lower()
                if any(k in low for k in ("process_crash", "dns_watchdog", "reconnect",
                                          "cdn", "youtube", "[process_crash]")):
                    crash_lines.append(line)
        else:
            print("  (no app.log yet)")
    except Exception as e:
        print(f"  <error reading app.log: {e}>")

    if crash_lines:
        print("\n-- app.log: crash / reconnect / DNS-watchdog / CDN lines --")
        for line in crash_lines[-15:]:
            print("  " + line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
