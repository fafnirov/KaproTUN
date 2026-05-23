# KaproVPN

[English](README.md) · [Русский](README.ru.md)

Desktop proxy client (Windows) with built-in **split routing for Russian sites**.
Built on top of [Xray-core](https://github.com/XTLS/Xray-core).

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
- Downloads `xray.exe` automatically on first launch (~20 MB)
- Editable list of "always direct" domains (108 entries by default — banks,
  госуслуги, marketplaces, media…)
- Sets the Windows system HTTP proxy on connect and restores it on disconnect
  / app close
- PySide6 GUI with dark theme, AmneziaVPN-style single-screen layout
- Live Xray-core log panel for troubleshooting

## Requirements

- Windows 10 / 11
- Python 3.10 or newer
- ~25 MB free disk space (for the Xray-core binary + geo data)

## Install & run

```bash
git clone https://github.com/fafnirov/KaproVPN.git
cd KaproVPN
pip install -r requirements.txt
python run.py
```

On first launch the app downloads the latest Xray-core release into
`%LOCALAPPDATA%\KaproVPN\xray\`.

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

- HTTP/SOCKS-based routing only. Applications that ignore the system proxy
  (some games, P2P clients) are not tunneled. TUN mode is on the roadmap.
- Windows only for now (the registry code in `core/system_proxy.py` is
  Windows-specific; the rest is cross-platform).
- No Hysteria2 support yet — Xray-core doesn't speak that protocol. A
  second-engine (sing-box) path is on the roadmap.
- No subscription URL import yet (planned).

## Project layout

```
kapro_vpn/
├── core/
│   ├── parser.py          # share-URL parsers (vless / vmess / trojan / ss / hy2)
│   ├── xray_config.py     # generates Xray-core JSON with split routing
│   ├── xray_installer.py  # downloads Xray-core from GitHub releases
│   ├── xray_process.py    # subprocess management
│   ├── system_proxy.py    # Windows proxy registry
│   ├── storage.py         # persistent JSON (configs / sites / settings)
│   ├── controller.py      # connect/disconnect orchestration
│   └── paths.py           # filesystem paths
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
