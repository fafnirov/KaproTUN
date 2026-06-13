#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: проверка ПОЛНОЙ схемы ручной маршрутизации.
ОТ ROOT:  sudo python3 /tmp/kt.py

Схема (замена сломанного auto_route на ядре 7.0):
  • sing-box: auto_route=False + route.default_mark=0x1 (помечает СВОЙ egress);
  • вручную: default dev KaproTun в table 2022 + правило from-all→2022;
  • правило fwmark 0x1 → main (помеченный трафик sing-box идёт мимо TUN, без петли).
Гоняем РЕАЛЬНЫЙ трафик через системный стек (как приложения). direct-outbound:
если google/DNS открываются — схема верна, с proxy-сервером будет так же.
"""
import json, os, re, signal, socket, subprocess, tempfile, time, urllib.request

CANDS = ["/root/.local/share/KaproTUN/sing-box/sing-box",
         os.path.expanduser("~/.local/share/KaproTUN/sing-box/sing-box"),
         "/usr/local/bin/sing-box", "/usr/bin/sing-box"]
SB = next((p for p in CANDS if os.path.isfile(p)), None)
if not SB:
    print("sing-box не найден"); raise SystemExit(1)
MARK = "0x1"
TABLE = "2022"


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def teardown():
    sh(f"ip rule del priority 9000 2>/dev/null")
    sh(f"ip rule del priority 8999 2>/dev/null")
    sh(f"ip route flush table {TABLE} 2>/dev/null")
    sh("ip link del KaproTun 2>/dev/null")


def real_test():
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    out = {}
    for name, url in [("dns(getaddrinfo)", None),
                      ("google", "https://www.google.com/"),
                      ("telegram", "https://api.telegram.org/")]:
        try:
            if url is None:
                socket.getaddrinfo("www.google.com", 443, socket.AF_INET)
                out[name] = "OK"
            else:
                t = time.time()
                r = op.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
                r.read(512)
                out[name] = f"OK {getattr(r,'status',None) or r.getcode()} {time.time()-t:.1f}s"
        except Exception as e:
            out[name] = f"FAIL {type(e).__name__}"
    return out


cfg = {"log": {"level": "error", "timestamp": True},
       "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "KaproTun",
                    "address": ["10.255.0.2/30"], "auto_route": False,
                    "stack": "gvisor"}],
       "outbounds": [{"type": "direct", "tag": "direct"}],
       "route": {"final": "direct", "auto_detect_interface": True,
                 "default_mark": 1}}
fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
open(p, "w").write(json.dumps(cfg))

rc, out = sh(f"{SB} check -c {p}")
if rc != 0:
    print("check FAIL (default_mark не принят?):", out[:200])
    # пробуем без default_mark в route — может это outbound-level routing_mark
    cfg["route"].pop("default_mark", None)
    cfg["outbounds"][0]["routing_mark"] = 1
    open(p, "w").write(json.dumps(cfg))
    rc, out = sh(f"{SB} check -c {p}")
    print("check с outbound.routing_mark:", "OK" if rc == 0 else "FAIL "+out[:150])

teardown()
print("поднимаю TUN (auto_route=False, fwmark)…")
pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True)
time.sleep(3)
if pr.poll() is not None:
    print("TUN не поднялся:", re.sub(r"\x1b\[[0-9;]*m", "", pr.stdout.read())[:300])
    os.unlink(p); raise SystemExit(1)

# ручная маршрутизация
cmds = [
    f"ip route add default dev KaproTun table {TABLE}",
    f"ip rule add fwmark {MARK} lookup main priority 8999",
    f"ip rule add from all lookup {TABLE} priority 9000",
]
print("ставлю маршруты:")
for c in cmds:
    rc, o = sh(c); print(f"  [{'OK' if rc==0 else 'FAIL'}] {c}" + (f" → {o[-70:]}" if rc else ""))

time.sleep(1)
print("\n===== РЕАЛЬНЫЙ ТРАФИК ЧЕРЕЗ TUN =====")
res = real_test()
for k, v in res.items():
    print(f"  {k:18}: {v}")

# уборка
print("\nубираю…")
for c in [f"ip rule del priority 9000", f"ip rule del priority 8999",
          f"ip route flush table {TABLE}"]:
    sh(c)
try:
    pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
except Exception:
    pr.kill()
teardown(); os.unlink(p)

ok = sum(1 for v in res.values() if v.startswith("OK"))
print("\n" + "=" * 60)
if ok >= 2:
    print(f"✓ СХЕМА РАБОТАЕТ ({ok}/3). Реализую Linux-фикс: auto_route=False +")
    print("  fwmark + ручные ip route/rule. На Windows остаётся как есть.")
else:
    print(f"✗ Трафик не пошёл ({ok}/3) — пришли вывод, доработаю схему.")
