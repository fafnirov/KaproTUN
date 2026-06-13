#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: отладка полной схемы (DNS + fwmark + trace).
ОТ ROOT:  sudo python3 /tmp/kt.py
"""
import json, os, re, signal, socket, subprocess, tempfile, time, urllib.request

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


# включаем форвардинг (вдруг нужен для gvisor-стека)
print("ip_forward было:", sh("cat /proc/sys/net/ipv4/ip_forward")[1])
sh("sysctl -w net.ipv4.ip_forward=1")

# ПОЛНЫЙ конфиг: DNS hijack→local, sniff, private→direct, default_mark, final=direct
cfg = {
    "log": {"level": "trace", "timestamp": True},
    "dns": {"servers": [{"type": "local", "tag": "local"}], "final": "local",
            "strategy": "ipv4_only"},
    "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "KaproTun",
                  "address": ["10.255.0.2/30"], "auto_route": False,
                  "stack": "gvisor"}],
    "outbounds": [{"type": "direct", "tag": "direct"}],
    "route": {"rules": [
        {"action": "sniff"},
        {"protocol": "dns", "action": "hijack-dns"},
        {"ip_cidr": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                     "127.0.0.0/8"], "action": "route", "outbound": "direct"},
    ], "final": "direct", "auto_detect_interface": True,
        "default_mark": 1, "default_domain_resolver": {"server": "local"}},
}
fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
open(p, "w").write(json.dumps(cfg))
rc, out = sh(f"{SB} check -c {p}")
print("check:", "OK" if rc == 0 else "FAIL " + out[:200])

teardown()
print("поднимаю TUN…")
pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True)
logs = []
import threading
threading.Thread(target=lambda: [logs.append(l.rstrip()) for l in pr.stdout],
                 daemon=True).start()
time.sleep(3)
if pr.poll() is not None:
    print("упал:", " | ".join(logs)[:400]); os.unlink(p); raise SystemExit(1)

for c in [f"ip route add default dev KaproTun table {TABLE}",
          "ip rule add fwmark 0x1 lookup main priority 8999",
          f"ip rule add from all lookup {TABLE} priority 9000"]:
    rc, o = sh(c); print(f"  [{'OK' if rc==0 else 'FAIL'}] {c}")

mark = len(logs)
time.sleep(1)
print("\n=== пробы реального трафика ===")
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    socket.getaddrinfo("www.google.com", 443, socket.AF_INET)
    print("  getaddrinfo: OK")
except Exception as e:
    print("  getaddrinfo: FAIL", type(e).__name__)
try:
    r = op.open(urllib.request.Request("http://www.gstatic.com/generate_204",
                headers={"User-Agent": "M"}), timeout=8)
    print("  http gstatic:", getattr(r, "status", None) or r.getcode())
except Exception as e:
    print("  http gstatic: FAIL", type(e).__name__, str(e)[:60])

time.sleep(1)
print("\n=== trace sing-box за время проб (dns/outbound/direct/error) ===")
rel = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in logs[mark:]
       if re.search(r"dns|outbound|direct|inbound/tun|error|fatal|mark|loop", l, re.I)]
for l in rel[-25:]:
    print("   ", l[:200])
if not rel:
    print("   (тишина — трафик не дошёл до sing-box; значит застрял в маршрутизации)")

# уборка
sh("ip rule del priority 9000 2>/dev/null; ip rule del priority 8999 2>/dev/null")
sh(f"ip route flush table {TABLE} 2>/dev/null")
try:
    pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
except Exception:
    pr.kill()
teardown(); os.unlink(p)
print("\n=== КОНЕЦ — пришли вывод целиком ===")
