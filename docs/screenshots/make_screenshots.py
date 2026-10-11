"""Render README screenshots offscreen, with demo data only.
Run from the repo root: python docs/screenshots/make_screenshots.py <out-dir> <ru|en>"""
import math
import os
import pathlib
import subprocess
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"
os.environ["QT_SCALE_FACTOR"] = "2"
sys.path.insert(0, os.getcwd())
out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
lang = sys.argv[2] if len(sys.argv) > 2 else "en"
suffix = "" if lang == "en" else "-ru"

from kapro_tun.core import paths

tmp = pathlib.Path(tempfile.mkdtemp(prefix="kaprotun-readme-"))
paths.app_data_dir = lambda: tmp

from kapro_tun.core import firewall_sweep, ipv6_block, killswitch, orphans, webrtc_block


class _NoSubprocess:
    def __getattr__(self, attr):
        return getattr(subprocess, attr)

    @staticmethod
    def run(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")


for mod in (killswitch, webrtc_block, ipv6_block, firewall_sweep):
    mod.subprocess = _NoSubprocess()
orphans._kill = lambda pid: True            # nothing real is ever ended from here
orphans._processes = lambda: iter(())

from kapro_tun.core import autostart, bandwidth_history, i18n, storage
from kapro_tun.core.parser import parse

autostart.is_enabled = lambda: False
autostart.enable = lambda **_k: True
autostart.disable = lambda: True

uid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
vless = (f"vless://{uid}@%s:%d?type=tcp&security=reality"
         "&pbk=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&sid=01&fp=chrome#x")
names = {
    "ru": ["🇳🇱 Нидерланды", "🇩🇪 Германия", "🇺🇸 США — Нью-Йорк", "🇹🇷 Турция", "🇸🇪 Швеция", "🇫🇮 Финляндия"],
    "en": ["🇳🇱 Netherlands", "🇩🇪 Germany", "🇺🇸 USA — New York", "🇹🇷 Turkey", "🇸🇪 Sweden", "🇫🇮 Finland"],
}[lang]
# example.net is reserved for documentation: nothing real is shown or contacted.
links = [vless % ("nl.example.net", 443), vless % ("de.example.net", 8443),
         vless % ("us.example.net", 2053), "trojan://pw@tr.example.net:443?security=tls#x",
         "hysteria2://pw@se.example.net:443?sni=example.net#x", vless % ("fi.example.net", 2083)]
ms = [42, 68, 131, 264, 97, 55]
configs = []
for name, link in zip(names, links):
    c = parse(link)
    c.name = name
    configs.append(c)
pings = dict(zip(names, ms))
storage.save_configs(configs)
storage.update_settings({"language": lang, "theme": "dark", "last_config_name": names[0],
                         "public_ip_probe": True, "subscription_auto_refresh": False})
i18n.init_from_settings(lang)

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

app = QApplication(sys.argv)
from kapro_tun.gui import connection_state as cs
from kapro_tun.gui.main_window import MainWindow
from kapro_tun.gui.styles import get_qss

app.setStyleSheet(get_qss("dark"))
w = MainWindow()
w.show()
QTest.qWait(1200)
if getattr(w, "_tray_pinger", None) is not None:
    for sig in (w._tray_pinger.pinged, w._tray_pinger.finished):
        sig.disconnect()
    w._tray_pinger.requestInterruption()
w.servers_page.set_pinging(False)
for attr in vars(w).values():
    if isinstance(attr, QTimer):
        attr.stop()


def shot(name: str) -> None:
    QTest.qWait(500)
    w.grab().save(str(out / f"{name}{suffix}.png"))
    print("saved", name, flush=True)


country, city = ("Нидерланды", "Амстердам") if lang == "ru" else ("Netherlands", "Amsterdam")
home = w.home_page
w._tray_pings = dict(pings)
w.servers_page.set_pings(pings)
w._refresh_home()
w._goto("home")
home.set_ping(42)
home.set_state(cs.CONNECTED, "00:42:17")
home.set_public_ip("203.0.113.21", country, city, "NL")     # a documentation address
home.set_traffic(86.3 * 1024, 2.4 * 1024 * 1024, 0, 0)
shot("main-window")

w._on_open_servers()
w.servers_page.select(names[0])
shot("servers")

w._goto("settings")
sp = w.settings_page
sp.findChild(QScrollArea).verticalScrollBar().setValue(0)
shot("settings")

w._goto("stats")
now = int(time.time())
for hour in range(24):
    if hour in (3, 4, 5, 13, 14):
        continue
    level = 0.25 + 0.75 * abs(math.sin(hour * 0.9))
    for minute in range(0, 60, 10):
        bandwidth_history.record(int(level * 4e6), int(level * 90e6),
                                 ts=now - (24 - hour) * 3600 + minute * 60 + 30)
stats = w.stats_page
stats.set_live_connected(True)
for i in range(60):
    stats.on_live_sample(86.3 * 1024 * (0.6 + 0.4 * math.sin(i / 5)),
                         2.4 * 1024 * 1024 * (0.55 + 0.45 * math.sin(i / 7 + 1)),
                         int(9.6 * 2**20), int(148.2 * 2**20))
stats.refresh()
shot("statistics")
print("window:", w.width(), "x", w.height(), flush=True)
os._exit(0)
