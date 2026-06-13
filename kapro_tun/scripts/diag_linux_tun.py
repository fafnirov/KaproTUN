#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: ФИНАЛ — systemd-resolved на TUN-DNS.
ОТ ROOT:  sudo python3 /tmp/kt.py

UDP/TCP/DNS-hijack уже работают (endpoint_independent_nat). Осталось научить
systemd-resolved (127.0.0.53) ходить за DNS через TUN: resolvectl dns на
интерфейс + default-route + flush. Тогда getaddrinfo/браузер заработают.
"""
import json, os, re, signal, socket, subprocess, tempfile, time

CANDS = ["/root/.local/share/KaproTUN/sing-box/sing-box",
         os.path.expanduser("~/.local/share/KaproTUN/sing-box/sing-box")]
SB = next((p for p in CANDS if os.path.isfile(p)), None)
if not SB:
    print("sing-box не найден"); raise SystemExit(1)
TABLE = "2022"
PRIV = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"]
TUN_DNS = "10.255.0.1"   # любой адрес в TUN; hijack-dns перехватит :53


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def teardown():
    sh("ip rule del priority 9000 2>/dev/null")
    sh("ip rule del priority 8999 2>/dev/null")
    sh(f"ip route flush table {TABLE} 2>/dev/null")
    sh("ip link del KaproTun 2>/dev/null")


sh("sysctl -w net.ipv4.ip_forward=1 >/dev/null")

cfg = {"log": {"level": "error"},
       "dns": {"servers": [{"type": "udp", "server": "192.168.1.1", "tag": "u"}],
               "final": "u", "strategy": "ipv4_only"},
       "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "KaproTun",
                     "address": ["10.255.0.2/30"], "auto_route": False,
                     "stack": "gvisor", "endpoint_independent_nat": True}],
       "outbounds": [{"type": "direct", "tag": "direct"}],
       "route": {"rules": [{"action": "sniff"},
                           {"protocol": "dns", "action": "hijack-dns"},
                           {"ip_cidr": PRIV, "action": "route", "outbound": "direct"}],
                 "final": "direct", "auto_detect_interface": True,
                 "default_mark": 1, "default_domain_resolver": {"server": "u"}}}
fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
open(p, "w").write(json.dumps(cfg))

teardown()
pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
time.sleep(3)
for c in [f"ip route add default dev KaproTun table {TABLE}",
          "ip rule add fwmark 0x1 lookup main priority 8999",
          f"ip rule add from all lookup {TABLE} priority 9000"]:
    sh(c)

# научить systemd-resolved ходить за DNS через TUN
print("настраиваю systemd-resolved на TUN…")
print("  resolvectl dns KaproTun:", sh(f"resolvectl dns KaproTun {TUN_DNS}")[1] or "ok")
print("  default-route:", sh("resolvectl default-route KaproTun yes")[1] or "ok")
print("  flush-caches:", sh("resolvectl flush-caches")[1] or "ok")
time.sleep(1)

print("\n=== ФИНАЛЬНЫЕ ПРОБЫ (как реальные приложения) ===")
for host in ("www.google.com", "api.telegram.org", "www.youtube.com"):
    try:
        ip = socket.getaddrinfo(host, 443, socket.AF_INET)[0][4][0]
        print(f"  getaddrinfo {host:20}: OK -> {ip}")
    except Exception as e:
        print(f"  getaddrinfo {host:20}: FAIL {type(e).__name__}")

import urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for url in ("http://www.gstatic.com/generate_204", "https://api.telegram.org/"):
    try:
        t = time.time()
        r = op.open(urllib.request.Request(url, headers={"User-Agent": "M"}), timeout=8)
        r.read(256)
        print(f"  HTTP {url:38}: OK {getattr(r,'status',None) or r.getcode()} ({time.time()-t:.1f}s)")
    except Exception as e:
        print(f"  HTTP {url:38}: FAIL {type(e).__name__}")

# уборка
print("\nуборка…")
sh("resolvectl revert KaproTun 2>/dev/null")
sh("ip rule del priority 9000 2>/dev/null; ip rule del priority 8999 2>/dev/null")
sh(f"ip route flush table {TABLE} 2>/dev/null")
try:
    pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
except Exception:
    pr.kill()
teardown(); sh("resolvectl flush-caches"); os.unlink(p)
print("\n=== КОНЕЦ — если getaddrinfo/HTTP = OK, схема ПОЛНАЯ ===")
