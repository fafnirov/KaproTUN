#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: gvisor vs system стек + счётчики TUN + ping.
ОТ ROOT:  sudo python3 /tmp/kt.py
Цель — понять, доходят ли пакеты ДО TUN и читает ли их sing-box.
"""
import json, os, re, signal, subprocess, tempfile, time, threading

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


def tun_packets():
    rc, o = sh("cat /sys/class/net/KaproTun/statistics/rx_packets 2>/dev/null")
    try: return int(o)
    except Exception: return -1


sh("sysctl -w net.ipv4.ip_forward=1 >/dev/null")


def test(stack):
    print(f"\n{'='*60}\n### СТЕК: {stack}")
    cfg = {"log": {"level": "trace", "timestamp": True},
           "dns": {"servers": [{"type": "local", "tag": "local"}],
                   "final": "local", "strategy": "ipv4_only"},
           "inbounds": [{"type": "tun", "tag": "tun-in",
                         "interface_name": "KaproTun", "address": ["10.255.0.2/30"],
                         "auto_route": False, "stack": stack}],
           "outbounds": [{"type": "direct", "tag": "direct"}],
           "route": {"rules": [{"action": "sniff"},
                               {"protocol": "dns", "action": "hijack-dns"},
                               {"ip_cidr": ["10.0.0.0/8", "172.16.0.0/12",
                                "192.168.0.0/16", "127.0.0.0/8"],
                                "action": "route", "outbound": "direct"}],
                     "final": "direct", "auto_detect_interface": True,
                     "default_mark": 1,
                     "default_domain_resolver": {"server": "local"}}}
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
    open(p, "w").write(json.dumps(cfg))
    teardown()
    pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    logs = []
    threading.Thread(target=lambda: [logs.append(l.rstrip()) for l in pr.stdout],
                     daemon=True).start()
    time.sleep(3)
    if pr.poll() is not None:
        print("упал:", " | ".join(logs)[:300]); os.unlink(p); return
    for c in [f"ip route add default dev KaproTun table {TABLE}",
              "ip rule add fwmark 0x1 lookup main priority 8999",
              f"ip rule add from all lookup {TABLE} priority 9000"]:
        sh(c)
    time.sleep(0.5)
    rx0 = tun_packets()
    mark = len(logs)
    # ПРЯМОЙ ping IP (без DNS) — направляется ли трафик в TUN
    rc_ping, out_ping = sh("ping -c 2 -W 3 8.8.8.8")
    rx1 = tun_packets()
    print(f"ping 8.8.8.8: {'ОТВЕЧАЕТ' if rc_ping==0 else 'НЕТ ОТВЕТА'}")
    print(f"TUN rx_packets: {rx0} -> {rx1}  (дельта {rx1-rx0 if rx0>=0 and rx1>=0 else '?'})")
    rel = [re.sub(r'\x1b\[[0-9;]*m', '', l) for l in logs[mark:]
           if re.search(r'tun|outbound|direct|dns|error|icmp|connection', l, re.I)]
    print(f"trace за пинг ({len(rel)} строк):")
    for l in rel[-12:]:
        print("   ", l[:180])
    sh("ip rule del priority 9000 2>/dev/null; ip rule del priority 8999 2>/dev/null")
    sh(f"ip route flush table {TABLE} 2>/dev/null")
    try:
        pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
    except Exception:
        pr.kill()
    teardown(); os.unlink(p)


for st in ["gvisor", "system"]:
    test(st)
print("\n=== КОНЕЦ — пришли вывод целиком ===")
