#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: TCP к IP + DNS через явный upstream (не local).
ОТ ROOT:  sudo python3 /tmp/kt.py

ICMP (ping) уже проходит через TUN. Проверяем TCP к IP (без DNS) и DNS через
udp-резолвер вместо local (local зацикливается через systemd-resolved).
"""
import json, os, re, signal, socket, subprocess, tempfile, time, threading, urllib.request

CANDS = ["/root/.local/share/KaproTUN/sing-box/sing-box",
         os.path.expanduser("~/.local/share/KaproTUN/sing-box/sing-box")]
SB = next((p for p in CANDS if os.path.isfile(p)), None)
if not SB:
    print("sing-box не найден"); raise SystemExit(1)
TABLE = "2022"


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def teardown():
    sh("ip rule del priority 9000 2>/dev/null")
    sh("ip rule del priority 8999 2>/dev/null")
    sh(f"ip route flush table {TABLE} 2>/dev/null")
    sh("ip link del KaproTun 2>/dev/null")


sh("sysctl -w net.ipv4.ip_forward=1 >/dev/null")
print("системный DNS (resolvectl):")
print(" ", sh("resolvectl dns 2>/dev/null | head -3")[1] or "(нет resolvectl)")

# DNS через ЯВНЫЙ udp-upstream (не local) — обходит петлю systemd-resolved
cfg = {"log": {"level": "trace", "timestamp": True},
       "dns": {"servers": [{"type": "udp", "server": "1.1.1.1", "tag": "up"}],
               "final": "up", "strategy": "ipv4_only"},
       "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "KaproTun",
                     "address": ["10.255.0.2/30"], "auto_route": False,
                     "stack": "gvisor"}],
       "outbounds": [{"type": "direct", "tag": "direct"}],
       "route": {"rules": [{"action": "sniff"},
                           {"protocol": "dns", "action": "hijack-dns"},
                           {"ip_cidr": ["10.0.0.0/8", "172.16.0.0/12",
                            "192.168.0.0/16", "127.0.0.0/8"],
                            "action": "route", "outbound": "direct"}],
                 "final": "direct", "auto_detect_interface": True,
                 "default_mark": 1, "default_domain_resolver": {"server": "up"}}}
fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
open(p, "w").write(json.dumps(cfg))
rc, o = sh(f"{SB} check -c {p}")
print("check:", "OK" if rc == 0 else "FAIL " + o[:160])

teardown()
pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True)
logs = []
threading.Thread(target=lambda: [logs.append(l.rstrip()) for l in pr.stdout],
                 daemon=True).start()
time.sleep(3)
if pr.poll() is not None:
    print("упал:", " | ".join(logs)[:300]); os.unlink(p); raise SystemExit(1)
for c in [f"ip route add default dev KaproTun table {TABLE}",
          "ip rule add fwmark 0x1 lookup main priority 8999",
          f"ip rule add from all lookup {TABLE} priority 9000"]:
    sh(c)
time.sleep(1)
mark = len(logs)

print("\n=== ПРОБЫ ===")
# 1) TCP к IP (без DNS)
try:
    t = time.time(); s = socket.create_connection(("1.1.1.1", 443), timeout=6); s.close()
    print(f"  TCP 1.1.1.1:443 (без DNS): OK ({time.time()-t:.1f}s)")
except Exception as e:
    print(f"  TCP 1.1.1.1:443: FAIL {type(e).__name__}: {e}")
# 2) DNS резолв через схему
try:
    ip = socket.getaddrinfo("www.google.com", 443, socket.AF_INET)[0][4][0]
    print(f"  getaddrinfo www.google.com: OK -> {ip}")
except Exception as e:
    print(f"  getaddrinfo: FAIL {type(e).__name__}")
# 3) HTTP по имени
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for url in ["http://www.gstatic.com/generate_204", "https://api.telegram.org/"]:
    try:
        t = time.time(); r = op.open(urllib.request.Request(url, headers={"User-Agent": "M"}), timeout=8)
        r.read(256); print(f"  {url}: OK {getattr(r,'status',None) or r.getcode()} ({time.time()-t:.1f}s)")
    except Exception as e:
        print(f"  {url}: FAIL {type(e).__name__}")

time.sleep(1)
print("\n=== trace (dns/outbound/connection/error) ===")
rel = [re.sub(r'\x1b\[[0-9;]*m', '', l) for l in logs[mark:]
       if re.search(r'dns|outbound|connection|error|fatal|exchange', l, re.I)]
for l in rel[-18:]:
    print("   ", l[:190])
if not rel:
    print("   (тишина)")

sh("ip rule del priority 9000 2>/dev/null; ip rule del priority 8999 2>/dev/null")
sh(f"ip route flush table {TABLE} 2>/dev/null")
try:
    pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
except Exception:
    pr.kill()
teardown(); os.unlink(p)
print("\n=== КОНЕЦ ===")
