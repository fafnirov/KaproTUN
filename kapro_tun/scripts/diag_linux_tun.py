#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: проверка ручной маршрутизации в обход auto_route.
ОТ ROOT:  sudo python3 /tmp/kt.py

sing-box (все версии) не может добавить маршрут через netlink на ядре 7.0
(«add route 0: invalid argument»), но TUN-устройство поднимается. Проверяем,
можно ли проложить маршруты вручную утилитой `ip` (iproute2) — если да, делаем
Linux-фикс: auto_route=False + ручные маршруты.
"""
import json, os, re, signal, subprocess, tempfile, time

CANDS = ["/root/.local/share/KaproTUN/sing-box/sing-box",
         os.path.expanduser("~/.local/share/KaproTUN/sing-box/sing-box"),
         "/usr/local/bin/sing-box", "/usr/bin/sing-box"]
SB = next((p for p in CANDS if os.path.isfile(p)), None)
if not SB:
    print("sing-box не найден"); raise SystemExit(1)


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def cleanup():
    sh("ip link del KaproTun 2>/dev/null")
    sh("ip rule del table 2022 2>/dev/null; ip -6 rule del table 2022 2>/dev/null")


# минимальный TUN БЕЗ auto_route — sing-box только создаёт устройство
cfg = {"log": {"level": "error", "timestamp": True},
       "inbounds": [{"type": "tun", "tag": "t", "interface_name": "KaproTun",
                    "address": ["10.255.0.2/30"], "auto_route": False,
                    "stack": "gvisor"}],
       "outbounds": [{"type": "direct", "tag": "direct"}],
       "route": {"final": "direct", "auto_detect_interface": True}}
fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
open(p, "w").write(json.dumps(cfg))

cleanup()
print("поднимаю TUN (auto_route=False)…")
pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True)
time.sleep(3)
if pr.poll() is not None:
    out = re.sub(r"\x1b\[[0-9;]*m", "", pr.stdout.read())
    print("TUN не поднялся:", out[:300]); os.unlink(p); raise SystemExit(1)

print("TUN жив. Состояние интерфейса:")
print(sh("ip addr show KaproTun")[1])

# default-шлюз физического NIC (для exclude-маршрута к VPN-серверу)
gw = sh("ip route show default")[1]
print("\nдефолт:", gw)

print("\n===== РУЧНЫЕ МАРШРУТЫ ЧЕРЕЗ iproute2 =====")
TESTS = [
    "ip link set KaproTun up",
    "ip route add 10.99.0.0/16 dev KaproTun",          # тестовая подсеть
    "ip route add 0.0.0.0/1 dev KaproTun",             # split-default половина 1
    "ip route add 128.0.0.0/1 dev KaproTun",           # split-default половина 2
    "ip route add default dev KaproTun table 2022",    # policy-route в свою таблицу
    "ip rule add from all lookup 2022 priority 9000",  # правило на таблицу 2022
]
ok = 0
for cmd in TESTS:
    rc, out = sh(cmd)
    mark = "OK" if rc == 0 else "FAIL"
    if rc == 0: ok += 1
    print(f"  [{mark}] {cmd}" + ("" if rc == 0 else f"   → {out[-80:]}"))

print("\nтаблица маршрутов после ручного добавления:")
print(sh("ip route show | head -8")[1])
print("таблица 2022:")
print(sh("ip route show table 2022 2>/dev/null")[1] or "(пусто)")

# уборка
print("\nубираю тестовые маршруты/правила…")
sh("ip rule del priority 9000 2>/dev/null")
sh("ip route flush table 2022 2>/dev/null")
sh("ip route del 0.0.0.0/1 dev KaproTun 2>/dev/null")
sh("ip route del 128.0.0.0/1 dev KaproTun 2>/dev/null")
sh("ip route del 10.99.0.0/16 dev KaproTun 2>/dev/null")
try:
    pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
except Exception:
    pr.kill()
cleanup(); os.unlink(p)

print("\n" + "=" * 60)
if ok >= 4:
    print(f"✓ Ручная маршрутизация РАБОТАЕТ ({ok}/{len(TESTS)}). Linux-фикс возможен:")
    print("  auto_route=False + прокладка маршрутов через iproute2.")
else:
    print(f"✗ Ручная маршрутизация тоже не идёт ({ok}/{len(TESTS)}).")
    print("  Тогда остаётся HTTP-proxy режим (без TUN) или ждать фикс sing-box под ядро 7.0.")
