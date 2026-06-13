#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: проходит ли UDP через схему (TCP уже работает).
ОТ ROOT:  sudo python3 /tmp/kt.py
"""
import json, os, re, signal, socket, struct, subprocess, tempfile, time, threading

CANDS = ["/root/.local/share/KaproTUN/sing-box/sing-box",
         os.path.expanduser("~/.local/share/KaproTUN/sing-box/sing-box")]
SB = next((p for p in CANDS if os.path.isfile(p)), None)
if not SB:
    print("sing-box не найден"); raise SystemExit(1)
TABLE = "2022"
PRIV = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"]


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def teardown():
    sh("ip rule del priority 9000 2>/dev/null")
    sh("ip rule del priority 8999 2>/dev/null")
    sh(f"ip route flush table {TABLE} 2>/dev/null")
    sh("ip link del KaproTun 2>/dev/null")


def dns_query(server, host="google.com", timeout=5):
    """Сырой UDP DNS A-запрос напрямую к server:53 (минуя systemd-resolved)."""
    tid = b"\x12\x34"
    q = tid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for part in host.split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(512)
        ancount = struct.unpack(">H", data[6:8])[0]
        return f"OK ({ancount} записей)"
    except Exception as e:
        return f"FAIL {type(e).__name__}"
    finally:
        s.close()


sh("sysctl -w net.ipv4.ip_forward=1 >/dev/null")

cfg = {"log": {"level": "trace", "timestamp": True},
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
print("check:", "OK" if sh(f"{SB} check -c {p}")[0] == 0 else "FAIL")

teardown()
pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True)
logs = []
threading.Thread(target=lambda: [logs.append(l.rstrip()) for l in pr.stdout],
                 daemon=True).start()
time.sleep(3)
for c in [f"ip route add default dev KaproTun table {TABLE}",
          "ip rule add fwmark 0x1 lookup main priority 8999",
          f"ip rule add from all lookup {TABLE} priority 9000"]:
    sh(c)
time.sleep(1)
mark = len(logs)

print("\n=== ПРОБЫ (с endpoint_independent_nat) ===")
# TCP — контроль (должен работать)
try:
    s = socket.create_connection(("1.1.1.1", 443), timeout=6); s.close()
    print("  TCP 1.1.1.1:443        : OK")
except Exception as e:
    print("  TCP 1.1.1.1:443        : FAIL", type(e).__name__)
# UDP DNS напрямую к разным серверам (минуя systemd-resolved)
print("  UDP DNS @8.8.8.8        :", dns_query("8.8.8.8"))
print("  UDP DNS @1.1.1.1        :", dns_query("1.1.1.1"))
print("  UDP DNS @192.168.1.1    :", dns_query("192.168.1.1"))
# systemd-resolved путь
try:
    socket.getaddrinfo("www.google.com", 443, socket.AF_INET)
    print("  getaddrinfo (systemd)  : OK")
except Exception:
    print("  getaddrinfo (systemd)  : FAIL")

time.sleep(1)
print("\n=== trace (udp/dns/outbound/error) ===")
rel = [re.sub(r'\x1b\[[0-9;]*m', '', l) for l in logs[mark:]
       if re.search(r'udp|dns|outbound|connection|error|exchange|packet', l, re.I)]
for l in rel[-18:]:
    print("   ", l[:190])
if not rel:
    print("   (тишина — UDP не дошёл до sing-box)")

sh("ip rule del priority 9000 2>/dev/null; ip rule del priority 8999 2>/dev/null")
sh(f"ip route flush table {TABLE} 2>/dev/null")
try:
    pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
except Exception:
    pr.kill()
teardown(); os.unlink(p)
print("\n=== КОНЕЦ ===")
