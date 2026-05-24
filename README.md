# KaproVPN

[![Release](https://img.shields.io/github/v/release/fafnirov/KaproVPN?style=flat-square&color=f59e0b&label=latest)](https://github.com/fafnirov/KaproVPN/releases/latest)
[![License](https://img.shields.io/github/license/fafnirov/KaproVPN?style=flat-square&color=blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://www.python.org/)
[![Build](https://img.shields.io/github/actions/workflow/status/fafnirov/KaproVPN/release.yml?style=flat-square&label=build)](https://github.com/fafnirov/KaproVPN/actions/workflows/release.yml)

[English](README.md) · [Русский](README.ru.md)

Desktop proxy client (Windows) with built-in **split routing for Russian sites**.
Built on top of [Xray-core](https://github.com/XTLS/Xray-core).

---

### ⬇️ Download

Grab the latest **[`KaproVPN.exe`](https://github.com/fafnirov/KaproVPN/releases/latest)** (~57 MB,
Windows x64, no Python required) and run it. Right-click → "Run as administrator"
for TUN mode (tunnels every app — Telegram, Steam, games — through the proxy).

---

## What it does

A GUI for proxy/VPN connections (Trojan, VLESS with REALITY and XHTTP, VMess,
Shadowsocks) with one extra trick: domains in a configurable list — Russian
banks, government services, marketplaces, etc. — bypass the proxy and go
directly through your real IP. Everything else routes through the proxy server.

## Why

When a user in Russia connects through a foreign proxy, services like Sberbank,
gosuslugi.ru, Ozon and many others refuse to work — they geofence to Russian
IPs. Switching the VPN off every time you need to pay a bill is annoying.
This tool keeps the proxy on for the open Internet and lets Russian services
see your real address.

## Features

- Parses share URLs in the standard formats:
  `trojan://`, `vless://` (including **REALITY** and **XHTTP** transport),
  `vmess://`, `ss://`
- Two connection modes:
  - **HTTP-proxy** (default, no admin) — works for browsers only
  - **TUN** (needs admin) — tunnels all apps system-wide, including Telegram,
    Steam, games. Uses bundled tun2socks + WinTUN driver
- Downloads `xray.exe`, `tun2socks.exe`, `wintun.dll` automatically on first
  use (~30 MB total)
- Editable list of "always direct" domains (108 entries by default — banks,
  госуслуги, marketplaces, media…)
- PySide6 GUI with dark theme, AmneziaVPN-style single-screen layout
- Live Xray-core log panel for troubleshooting

## Requirements

- Windows 10 / 11 (x64)
- ~80 MB total free disk space (~57 MB exe + ~25 MB for Xray + tun2socks + WinTUN)
- Admin rights *only* if you want TUN mode (everything tunneled). HTTP-proxy
  mode works without admin and tunnels just the browser.

## Install & run

### Option 1 — Pre-built binary (recommended)

Download **[`KaproVPN.exe`](https://github.com/fafnirov/KaproVPN/releases/latest)**,
run it. Done.

### Option 2 — From source (for development / contributing)

```bash
git clone https://github.com/fafnirov/KaproVPN.git
cd KaproVPN
pip install -r requirements.txt
python run.py
```

Or, to build your own .exe locally:

```bash
pip install -r requirements-build.txt
pyinstaller KaproVPN.spec
# → dist/KaproVPN.exe
```

On first launch the app downloads the latest Xray-core release into
`%LOCALAPPDATA%\KaproVPN\xray\`. Same path for tun2socks + wintun.dll
(if TUN mode is enabled).

## How it works

1. You paste a share URL (e.g. `vless://…`).
2. The app parses it and generates an Xray-core JSON config with routing rules:
   - domains from your "direct" list → `freedom` outbound (your real IP)
   - everything else → proxy outbound (the parsed URL)
3. `xray.exe` starts as a subprocess and listens on `127.0.0.1:2080` (HTTP)
   and `:2081` (SOCKS5).
4. Windows system proxy is pointed at port 2080.
5. Any application that respects the system proxy (browsers, Office, most
   desktop apps) now follows the routing rules.

## Limitations

- Windows only for now (route + proxy code is Windows-specific; the rest
  is cross-platform).
- No Hysteria2 support yet — Xray-core doesn't speak that protocol. A
  second-engine (sing-box) path is on the roadmap.
- No subscription URL import yet (planned).
- TUN mode does IPv4 only — IPv6 traffic may leak outside the tunnel.

## Project layout

```
kapro_vpn/
├── core/
│   ├── parser.py             # share-URL parsers (vless / vmess / trojan / ss / hy2)
│   ├── xray_config.py        # generates Xray-core JSON with split routing
│   ├── xray_installer.py     # downloads Xray-core from GitHub releases
│   ├── xray_process.py       # Xray subprocess management
│   ├── tun2socks_installer.py # downloads tun2socks + wintun.dll
│   ├── tun2socks_process.py  # tun2socks subprocess management
│   ├── network_routes.py     # Windows route/DNS manipulation for TUN mode
│   ├── admin.py              # UAC elevation helpers
│   ├── system_proxy.py       # Windows HTTP proxy registry (HTTP mode)
│   ├── storage.py            # persistent JSON (configs / sites / settings)
│   ├── controller.py         # connect/disconnect orchestration
│   └── paths.py              # filesystem paths
├── gui/
│   ├── main_window.py     # single-window app with Home / Settings / Logs
│   ├── widgets.py         # CircleConnectButton, ConfigCard, NavBar
│   ├── config_dialog.py
│   ├── configs_picker.py
│   ├── sites_dialog.py
│   ├── installer_dialog.py
│   └── styles.py          # dark-theme QSS with amber accent
├── data/
│   └── default_sites.json # bundled default direct-routing list
└── main.py                # QApplication entry point
```

User data (saved configs, edited site list, settings, logs) lives in
`%LOCALAPPDATA%\KaproVPN\`.

## Contributing

PRs welcome. A few directions where help is especially useful:

- Hysteria2 support via a second engine (sing-box)
- TUN mode (so games and any app are tunneled, not just HTTP-proxy-aware ones)
- Linux / macOS port
- Subscription URL importer (base64-list URLs)
- System tray icon
- Latency / health-check pings per config

## License

[GNU GPL v3](LICENSE). Any derivative work must also be GPL v3 — this is
deliberate so that the project cannot be quietly absorbed into a closed-source
product.
