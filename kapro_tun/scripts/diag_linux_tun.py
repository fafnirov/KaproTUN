#!/usr/bin/env python3
"""KaproTUN — Linux: поиск версии sing-box, где auto_route РАБОТАЕТ. ОТ ROOT:
    sudo python3 /tmp/kt.py

TUN-устройство поднимается, но netlink отвергает любой маршрут под 1.12.9
(«add route 0: invalid argument»). Качаем несколько версий sing-box и проверяем
auto_route минимальным конфигом — найдём ту, что работает на этом ядре.
"""
import io, json, os, re, signal, subprocess, tarfile, tempfile, time, urllib.request

print("ядро:", subprocess.run("uname -r", shell=True, capture_output=True,
                              text=True).stdout.strip())
print("дистрибутив:", subprocess.run("lsb_release -ds 2>/dev/null || cat /etc/os-release | head -1",
      shell=True, capture_output=True, text=True).stdout.strip())

VERSIONS = ["1.11.15", "1.12.0", "1.12.4", "1.12.9", "1.13.12"]


def download(ver):
    url = (f"https://github.com/SagerNet/sing-box/releases/download/"
           f"v{ver}/sing-box-{ver}-linux-amd64.tar.gz")
    data = urllib.request.urlopen(url, timeout=90).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        m = next(x for x in tf.getmembers() if x.name.endswith("/sing-box"))
        path = f"/tmp/sb-{ver}"
        open(path, "wb").write(tf.extractfile(m).read())
        os.chmod(path, 0o755)
    return path


def cleanup():
    subprocess.run("ip link del KaproTun 2>/dev/null; "
                   "ip rule del table 2022 2>/dev/null; "
                   "ip -6 rule del table 2022 2>/dev/null", shell=True)


def test_autoroute(sb):
    cfg = {"log": {"level": "error", "timestamp": True},
           "inbounds": [{"type": "tun", "tag": "t", "interface_name": "KaproTun",
                        "address": ["10.255.0.2/30"], "auto_route": True,
                        "stack": "gvisor"}],
           "outbounds": [{"type": "direct", "tag": "direct"}],
           "route": {"final": "direct", "auto_detect_interface": True}}
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
    open(p, "w").write(json.dumps(cfg))
    pr = subprocess.Popen([sb, "run", "-c", p], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    time.sleep(2.5)
    alive = pr.poll() is None
    out = "" if alive else pr.stdout.read()
    try:
        pr.send_signal(signal.SIGINT); pr.wait(timeout=4)
    except Exception:
        try: pr.kill()
        except Exception: pass
    cleanup(); os.unlink(p)
    return alive, re.sub(r"\x1b\[[0-9;]*m", "", out)


print("\n===== ВЕРСИИ sing-box: РАБОТАЕТ ЛИ auto_route =====")
working = []
for ver in VERSIONS:
    try:
        sb = download(ver)
    except Exception as e:
        print(f"sing-box {ver:8}: скачать не удалось — {type(e).__name__}: {e}")
        continue
    alive, out = test_autoroute(sb)
    if alive:
        print(f"sing-box {ver:8}: ✓ auto_route РАБОТАЕТ")
        working.append(ver)
    else:
        err = next((l for l in out.splitlines() if "FATAL" in l or "add route" in l),
                   out[:100])
        print(f"sing-box {ver:8}: ✗ {err.strip()[-90:]}")
    try: os.unlink(sb)
    except Exception: pass
    time.sleep(1)

print("\n" + "=" * 60)
print("РАБОЧИЕ версии:", ", ".join(working) if working else "НЕТ НИ ОДНОЙ")
if working:
    print(f"→ на Linux пинимся на {working[0]} (или ближайшую рабочую)")
else:
    print("→ проблема не в версии — копаем ядро/netlink, пришли вывод")
