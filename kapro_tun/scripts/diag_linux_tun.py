#!/usr/bin/env python3
"""KaproTUN — Linux TUN-диагностика v2. ОТ ROOT:  sudo python3 /tmp/kt.py

Все варианты auto_route падают с «add route: invalid argument» → проблема не в
IPv6/stack, а в самом auto_route на этой системе. Этот скрипт: (1) печатает
сетевой контекст; (2) проверяет, поднимается ли TUN БЕЗ auto_route; (3) пробует
обходные варианты (split route_address, auto_redirect); (4) даёт ПОЛНЫЙ
trace-лог проблемного конфига, чтобы увидеть точную причину netlink-EINVAL.
"""
import json, os, subprocess, tempfile, time, signal, re

CANDS = [os.path.expanduser("~/.local/share/KaproTUN/sing-box/sing-box"),
         "/root/.local/share/KaproTUN/sing-box/sing-box",
         "/usr/local/bin/sing-box", "/usr/bin/sing-box"]
SB = next((p for p in CANDS if os.path.isfile(p)), None)
if not SB:
    print("sing-box не найден:", CANDS); raise SystemExit(1)
print("sing-box:", SB)


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=8).stdout.strip()
    except Exception as e:
        return f"<err {e}>"


print("\n===== СЕТЕВОЙ КОНТЕКСТ =====")
print("-- ip route (v4) --\n" + sh("ip route show"))
print("-- ip -6 route --\n" + (sh("ip -6 route show") or "(пусто — IPv6 нет)"))
print("-- ip rule --\n" + sh("ip rule show"))
print("-- интерфейсы --\n" + sh("ip -br link show"))
print("-- ip_forward --", sh("cat /proc/sys/net/ipv4/ip_forward"))

V4 = "10.255.0.2/30"
V6 = "fdfe:dcba:9876::1/126"


def run_cfg(tun, level="error", wait=2.5):
    cfg = {"log": {"level": level, "timestamp": True},
           "inbounds": [dict(tun, type="tun", tag="tun-in",
                             interface_name="KaproTun")],
           "outbounds": [{"type": "direct", "tag": "direct"}],
           "route": {"rules": [{"action": "sniff"}], "final": "direct",
                     "auto_detect_interface": True}}
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
    open(p, "w").write(json.dumps(cfg))
    chk = subprocess.run([SB, "check", "-c", p], capture_output=True, text=True)
    if chk.returncode != 0:
        os.unlink(p); return False, "CHECK FAIL: " + chk.stderr.strip()[:140]
    pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    time.sleep(wait)
    alive = pr.poll() is None
    out = "" if alive else pr.stdout.read()
    try:
        pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
    except Exception:
        try: pr.kill()
        except Exception: pass
    sh("ip link del KaproTun 2>/dev/null; ip rule del table 2022 2>/dev/null; "
       "ip -6 rule del table 2022 2>/dev/null")
    os.unlink(p)
    return alive, re.sub(r"\x1b\[[0-9;]*m", "", out)


def short(out):
    return " | ".join(l for l in out.splitlines() if l.strip())[:320]


print("\n===== ТЕСТЫ =====")

alive, out = run_cfg({"address": [V4], "mtu": 1400, "auto_route": False,
                      "stack": "gvisor"})
print(f"\n[1] TUN БЕЗ auto_route (gvisor): {'OK ПОДНЯЛСЯ' if alive else 'FAIL'}")
if not alive:
    print("    " + short(out))

alive, out = run_cfg({"address": [V4], "mtu": 1400, "auto_route": True,
                      "route_address": ["0.0.0.0/1", "128.0.0.0/1"],
                      "stack": "gvisor"})
print(f"\n[2] auto_route + split route_address: {'OK ПОДНЯЛСЯ' if alive else 'FAIL'}")
if not alive:
    print("    " + short(out))

alive, out = run_cfg({"address": [V4], "mtu": 1400, "auto_route": True,
                      "auto_redirect": True, "stack": "system"})
print(f"\n[3] auto_route + auto_redirect (system): {'OK ПОДНЯЛСЯ' if alive else 'FAIL'}")
if not alive:
    print("    " + short(out))

print("\n[4] ПОЛНЫЙ trace-лог текущего конфига (v4+v6 gvisor auto_route):")
alive, out = run_cfg({"address": [V4, V6], "mtu": 1400, "auto_route": True,
                      "strict_route": False, "stack": "gvisor",
                      "endpoint_independent_nat": True}, level="trace", wait=3)
for l in out.splitlines():
    if l.strip():
        print("    " + l[:200])

print("\n===== КОНЕЦ =====")
