#!/usr/bin/env python3
"""KaproTUN — Linux/ядро 7.0: поиск рабочего DNS-варианта (TCP уже работает).
ОТ ROOT:  sudo python3 /tmp/kt.py
"""
import json, os, re, signal, socket, subprocess, tempfile, time, threading

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


sh("sysctl -w net.ipv4.ip_forward=1 >/dev/null")
_, rv = sh("resolvectl dns 2>/dev/null")
ips = [x for x in re.findall(r'\b\d+\.\d+\.\d+\.\d+\b', rv) if not x.startswith("127.")]
SYS = ips[0] if ips else "192.168.1.1"
print("системный upstream DNS:", SYS)


def base_in_out():
    return ([{"type": "tun", "tag": "tun-in", "interface_name": "KaproTun",
              "address": ["10.255.0.2/30"], "auto_route": False, "stack": "gvisor"}],
            [{"type": "direct", "tag": "direct"}])


VARIANTS = {
    "A. dns=udp(sys upstream) + hijack": {
        "dns": {"servers": [{"type": "udp", "server": SYS, "tag": "u"}],
                "final": "u", "strategy": "ipv4_only"},
        "rules": [{"action": "sniff"}, {"protocol": "dns", "action": "hijack-dns"},
                  {"ip_cidr": PRIV, "action": "route", "outbound": "direct"}],
        "resolver": "u"},
    "B. dns=local + БЕЗ hijack (systemd сам форвардит)": {
        "dns": {"servers": [{"type": "local", "tag": "l"}], "final": "l",
                "strategy": "ipv4_only"},
        "rules": [{"action": "sniff"},
                  {"ip_cidr": PRIV, "action": "route", "outbound": "direct"}],
        "resolver": "l"},
    "C. dns=udp(8.8.8.8) + hijack": {
        "dns": {"servers": [{"type": "udp", "server": "8.8.8.8", "tag": "g"}],
                "final": "g", "strategy": "ipv4_only"},
        "rules": [{"action": "sniff"}, {"protocol": "dns", "action": "hijack-dns"},
                  {"ip_cidr": PRIV, "action": "route", "outbound": "direct"}],
        "resolver": "g"},
}


def run(name, spec):
    inb, outb = base_in_out()
    cfg = {"log": {"level": "error"}, "dns": spec["dns"], "inbounds": inb,
           "outbounds": outb,
           "route": {"rules": spec["rules"], "final": "direct",
                     "auto_detect_interface": True, "default_mark": 1,
                     "default_domain_resolver": {"server": spec["resolver"]}}}
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
    open(p, "w").write(json.dumps(cfg))
    rc, o = sh(f"{SB} check -c {p}")
    if rc != 0:
        print(f"{name}: check FAIL {o[:90]}"); os.unlink(p); return
    teardown()
    pr = subprocess.Popen([SB, "run", "-c", p], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    for c in [f"ip route add default dev KaproTun table {TABLE}",
              "ip rule add fwmark 0x1 lookup main priority 8999",
              f"ip rule add from all lookup {TABLE} priority 9000"]:
        sh(c)
    time.sleep(0.5)
    # резолв через системный getaddrinfo (как реальные приложения)
    res = []
    for host in ("www.google.com", "api.telegram.org"):
        try:
            socket.getaddrinfo(host, 443, socket.AF_INET); res.append(host.split('.')[1] + "=OK")
        except Exception:
            res.append(host.split('.')[1] + "=FAIL")
    print(f"{name}:  {', '.join(res)}")
    sh("ip rule del priority 9000 2>/dev/null; ip rule del priority 8999 2>/dev/null")
    sh(f"ip route flush table {TABLE} 2>/dev/null")
    try:
        pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
    except Exception:
        pr.kill()
    teardown(); os.unlink(p); time.sleep(1)


print("\n=== DNS-варианты (getaddrinfo через TUN) ===")
for n, s in VARIANTS.items():
    run(n, s)
print("\n=== КОНЕЦ — какой вариант дал OK, тот и берём ===")
