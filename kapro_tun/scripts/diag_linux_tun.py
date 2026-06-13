#!/usr/bin/env python3
"""KaproTUN — подбор рабочего TUN-конфига на Linux. Запусти ОТ ROOT:

    sudo python3 diag_linux_tun.py

Изолирует ошибку «add route 0: invalid argument»: перебирает варианты TUN
(с IPv6 / без, gvisor/system, strict_route) с ПУСТЫМ direct-outbound (реальный
сервер НЕ нужен — ошибка возникает на старте TUN, до трафика) и показывает,
какой вариант поднимается без FATAL. Чистый python3 stdlib + бинарь sing-box.
"""
import json, os, subprocess, tempfile, time, signal

# найти бинарь sing-box (стандартные места установки на Linux)
CANDS = [
    os.path.expanduser("~/.local/share/KaproTUN/sing-box/sing-box"),
    os.path.expanduser("~/.local/share/KaproVPN/sing-box/sing-box"),
    "/usr/local/bin/sing-box", "/usr/bin/sing-box",
]
SB = next((p for p in CANDS if os.path.isfile(p)), None)
if not SB:
    print("sing-box не найден. Укажи путь:", CANDS); raise SystemExit(1)
print("sing-box:", SB)
try:
    v = subprocess.run([SB, "version"], capture_output=True, text=True, timeout=8).stdout.splitlines()
    print("версия:", v[0] if v else "?")
except Exception as e:
    print("версия: ?", e)

V4 = "10.255.0.2/30"
V6 = "fdfe:dcba:9876::1/126"

VARIANTS = [
    ("A. v4+v6  gvisor strict=False  (текущий конфиг)", [V4, V6], "gvisor", False),
    ("B. v4only gvisor strict=False", [V4], "gvisor", False),
    ("C. v4only system strict=False", [V4], "system", False),
    ("D. v4only gvisor strict=True ", [V4], "gvisor", True),
    ("E. v4+v6  system strict=False", [V4, V6], "system", False),
    ("F. v4only system strict=True ", [V4], "system", True),
]

def build(address, stack, strict):
    tun = {
        "type": "tun", "tag": "tun-in", "interface_name": "KaproTun",
        "address": address, "mtu": 1400, "auto_route": True,
        "strict_route": strict, "stack": stack, "endpoint_independent_nat": True,
    }
    return {
        "log": {"level": "error", "timestamp": True},
        "inbounds": [tun],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"action": "sniff"}], "final": "direct",
                  "auto_detect_interface": True},
    }

results = []
for name, addr, stack, strict in VARIANTS:
    cfg = build(addr, stack, strict)
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd)
    with open(path, "w") as f:
        json.dump(cfg, f)
    # check сначала (грамматика)
    chk = subprocess.run([SB, "check", "-c", path], capture_output=True, text=True)
    if chk.returncode != 0:
        print(f"{name}: CHECK FAIL: {chk.stderr.strip()[:80]}")
        results.append((name, "check-fail")); os.unlink(path); continue
    # запустить и посмотреть, поднимается ли TUN без FATAL
    proc = subprocess.Popen([SB, "run", "-c", path],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2.5)
    out = ""
    alive = proc.poll() is None
    if not alive:
        out = proc.stdout.read()
    try:
        proc.send_signal(signal.SIGINT); proc.wait(timeout=4)
    except Exception:
        try: proc.kill()
        except Exception: pass
    # подчистить осиротевшие маршруты/интерфейс
    subprocess.run("ip link del KaproTun 2>/dev/null; ip -6 rule del table 2022 2>/dev/null; "
                   "ip rule del table 2022 2>/dev/null", shell=True)
    os.unlink(path)
    low = out.lower()
    if alive:
        verdict = "✓ TUN ПОДНЯЛСЯ"
    elif "add route" in low and "invalid argument" in low:
        verdict = "✗ add route: invalid argument (та самая ошибка)"
    elif "operation not permitted" in low or "not permitted" in low:
        verdict = "✗ нет прав (запусти под sudo)"
    elif "fatal" in low or "error" in low:
        line = next((l for l in out.splitlines() if "FATAL" in l or "ERROR" in l), out[:120])
        import re; verdict = "✗ " + re.sub(r"\x1b\[[0-9;]*m", "", line)[-90:]
    else:
        verdict = "✗ упал без явной причины"
    print(f"{name}: {verdict}")
    results.append((name, verdict))
    time.sleep(1)

print("\n" + "="*70)
ok = [n for n, v in results if v.startswith("✓")]
if ok:
    print("РАБОЧИЕ варианты TUN:")
    for n in ok: print("  ", n)
    print("\n→ берём первый рабочий и применяем его параметры в sing_box_config.py")
else:
    print("Ни один не поднялся — пришли вывод, разберём по тексту ошибок.")
