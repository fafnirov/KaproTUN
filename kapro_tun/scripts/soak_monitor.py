"""Read-only soak-test monitor for a stable release (sing-box TUN engine).

Samples the live session every --interval seconds for --minutes, watching for
the failure modes a soak test must rule out:

  * sing-box process death / unexpected restart (pid changes);
  * xray.exe or tun2socks.exe appearing — an engine fallback that must NOT
    happen for a sing-box session the user didn't switch;
  * memory / handle creep on the sing-box process (the v2.x failure class);
  * new app.log anomalies since baseline: a false 'Xray-core упал', a fallback
    to classic_xray_tun2socks, reconnect storms, socket-exhaustion, FATAL/panic,
    or critical memory pressure.

NOTHING is killed, deleted, or modified — pure observation. Prints a per-sample
line and a final PASS / NEEDS-REVIEW summary.

Usage:
    python -m kapro_tun.scripts.soak_monitor --minutes 60 --interval 120
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# app.log lines (already redacted at write) that mean trouble during a soak.
_ANOMALY_MARKERS = (
    ("xray_false_crash", "Xray-core упал"),          # engine-aware watchdog bug
    ("engine_fallback", "engine=classic_xray_tun2socks"),
    ("process_crash", "reason=process_crash"),
    ("socket_exhaustion", "socket-exhaustion"),
    ("mem_critical", "mem-pressure/critical"),
    ("fatal", "FATAL"),
    ("panic", "panic:"),
    ("emergency", "emergency disconnect"),
)


def _helpers():
    """{name: [pids]} for sing-box / xray / tun2socks / hysteria (best-effort)."""
    names = ("sing-box", "xray", "tun2socks", "hysteria")
    found = {n: [] for n in names}
    try:
        import psutil
        for p in psutil.process_iter(["name", "pid"]):
            nm = (p.info.get("name") or "").lower()
            for n in names:
                if nm.startswith(n):
                    found[n].append(p.info["pid"])
    except Exception:
        pass
    return found


def _singbox_stats(pid):
    """(private_MB, handles, threads) for a pid, best-effort."""
    try:
        import psutil
        p = psutil.Process(pid)
        with p.oneshot():
            mem = getattr(p.memory_info(), "private", None)
            if mem is None:
                mem = p.memory_info().rss
            handles = p.num_handles() if hasattr(p, "num_handles") else 0
            threads = p.num_threads()
        return round(mem / (1024 * 1024)), handles, threads
    except Exception:
        return None, None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=120.0)
    args = ap.parse_args()

    from kapro_tun.core import paths, storage

    log = paths.app_log_file()
    try:
        baseline_lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        baseline_lines = []
    seen = len(baseline_lines)

    s = storage.load_settings()
    print("=== KaproTUN soak monitor (read-only) ===")
    print(f"mode={s.get('mode')}  tun_engine={s.get('tun_engine')}  "
          f"kill_switch={s.get('kill_switch')}  dns_leak={s.get('dns_leak_protection')}")
    print(f"duration={args.minutes:g} min, interval={args.interval:g} s, "
          f"app.log baseline={seen} lines\n")

    start = time.monotonic()
    deadline = start + args.minutes * 60.0
    first = _helpers()
    sb_pid0 = first["sing-box"][0] if first["sing-box"] else None

    findings = {k: 0 for k, _ in _ANOMALY_MARKERS}
    sb_seen, sb_deaths, sb_pid_changes = bool(sb_pid0), 0, 0
    legacy_seen = False
    mem_min = mem_max = hnd_min = hnd_max = None
    sample = 0

    while True:
        sample += 1
        h = _helpers()
        sb = h["sing-box"]
        legacy = bool(h["xray"]) or bool(h["tun2socks"])
        legacy_seen = legacy_seen or legacy
        sb_seen = sb_seen or bool(sb)
        if not sb and sb_pid0 is not None:
            sb_deaths += 1
        cur_pid = sb[0] if sb else None
        if cur_pid is not None and sb_pid0 is not None and cur_pid != sb_pid0:
            sb_pid_changes += 1
            sb_pid0 = cur_pid

        mem = hnd = thr = None
        if cur_pid is not None:
            mem, hnd, thr = _singbox_stats(cur_pid)
            if mem is not None:
                mem_min = mem if mem_min is None else min(mem_min, mem)
                mem_max = mem if mem_max is None else max(mem_max, mem)
            if hnd:
                hnd_min = hnd if hnd_min is None else min(hnd_min, hnd)
                hnd_max = hnd if hnd_max is None else max(hnd_max, hnd)

        # New app.log lines since last read → scan for anomalies.
        new_anoms = []
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) >= seen:
                fresh = lines[seen:]
            else:  # rotated — re-scan whole file
                fresh = lines
            seen = len(lines)
            for ln in fresh:
                for key, marker in _ANOMALY_MARKERS:
                    if marker in ln:
                        findings[key] += 1
                        new_anoms.append(f"{key}:{ln[-60:].strip()}")
        except OSError:
            pass

        elapsed = int(time.monotonic() - start)
        legacy_tag = " ⚠LEGACY-PROC" if legacy else ""
        anom_tag = ("  ⚠ " + "; ".join(new_anoms)) if new_anoms else ""
        print(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] sample {sample}: "
              f"sing-box={'pid ' + str(cur_pid) if cur_pid else 'DOWN'}"
              f"{(' mem ' + str(mem) + 'MB' if mem is not None else '')}"
              f"{(' hnd ' + str(hnd) if hnd else '')}"
              f"{(' thr ' + str(thr) if thr else '')}"
              f"{legacy_tag}{anom_tag}", flush=True)

        if time.monotonic() >= deadline:
            break
        time.sleep(max(5.0, min(args.interval, deadline - time.monotonic())))

    # --- summary ---
    print("\n=== SOAK SUMMARY ===")
    print(f"samples={sample}, duration={int(time.monotonic() - start) // 60} min")
    print(f"sing-box: seen={sb_seen}, disappearances={sb_deaths}, "
          f"pid-changes(restarts)={sb_pid_changes}")
    print(f"sing-box mem MB: min={mem_min} max={mem_max}; "
          f"handles: min={hnd_min} max={hnd_max}")
    print(f"legacy xray/tun2socks ever seen: {legacy_seen}")
    print("app.log anomalies since baseline:")
    for k, _ in _ANOMALY_MARKERS:
        print(f"  {k:<18}: {findings[k]}")

    problems = []
    if not sb_seen:
        problems.append("sing-box was never observed running")
    if sb_deaths:
        problems.append(f"sing-box disappeared {sb_deaths}x")
    if sb_pid_changes:
        problems.append(f"sing-box restarted {sb_pid_changes}x")
    if legacy_seen:
        problems.append("xray/tun2socks appeared (engine fallback?)")
    for k in ("xray_false_crash", "engine_fallback", "socket_exhaustion",
              "mem_critical", "panic", "emergency"):
        if findings[k]:
            problems.append(f"app.log: {k} x{findings[k]}")
    if mem_max is not None and mem_max >= 3000:
        problems.append(f"sing-box mem reached {mem_max} MB (>3 GB)")
    if hnd_max is not None and hnd_min is not None and (hnd_max - hnd_min) >= 5000:
        problems.append(f"sing-box handle growth {hnd_min}->{hnd_max}")

    if problems:
        print("\nRESULT: NEEDS-REVIEW")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nRESULT: PASS (no process death, no engine fallback, no log anomalies, "
          "no memory/handle creep)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
