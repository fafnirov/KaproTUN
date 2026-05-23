# Russian VPN

[English](README.md) · [Русский](README.ru.md)

Desktop proxy client (Windows) with built-in **split routing for Russian sites**.
Built on top of [sing-box](https://github.com/SagerNet/sing-box).

## What it does

A GUI for proxy/VPN connections (Trojan, VLESS, VMess, Shadowsocks, Hysteria2)
with one extra trick: domains in a configurable list — Russian banks, government
services, marketplaces, etc. — bypass the proxy and go directly through your
real IP. Everything else routes through the proxy server.

## Why

When a user in Russia connects through a foreign proxy, services like Sberbank,
gosuslugi.ru, Ozon and many others refuse to work — they geofence to Russian
IPs. Switching the VPN off every time you need to pay a bill is annoying.
This tool keeps the proxy on for the open Internet and lets Russian services
see your real address.

## Features

- Parses share URLs in the standard formats:
  `trojan://`, `vless://` (including REALITY), `vmess://`, `ss://`, `hysteria2://` (`hy2://`)
- Downloads `sing-box.exe` automatically on first launch (~15 MB)
- Editable list of "always direct" domains (108 entries by default — banks, госуслуги, marketplaces, media…)
- Sets the Windows system HTTP proxy on connect and restores it on disconnect / app close
- PySide6 GUI with dark theme
- Live sing-box log panel for troubleshooting

## Requirements

- Windows 10 / 11
- Python 3.10 or newer
- ~20 MB free disk space (for the sing-box binary)

## Install & run

```bash
git clone <repo-url>
cd russian-vpn
pip install -r requirements.txt
python run.py
```

On first launch the app downloads the latest sing-box release into
`%LOCALAPPDATA%\russian-vpn\singbox\`.

## How it works

1. You paste a share URL (e.g. `trojan://…`).
2. The app parses it into a sing-box outbound.
3. A sing-box JSON config is generated with routing rules:
   - domains from your "direct" list → `direct` outbound (your real IP)
   - everything else → `proxy` outbound (the parsed URL)
4. `sing-box.exe` starts as a subprocess and listens on `127.0.0.1:2080`
   (mixed HTTP + SOCKS5 inbound).
5. Windows system proxy is pointed at that port.
6. Any application that respects the system proxy (browsers, Office, most
   desktop apps) now follows the routing rules.

## Limitations

- HTTP/SOCKS-based routing only. Applications that ignore the system proxy
  (some games, P2P clients) are not tunneled. TUN mode is on the roadmap.
- Windows only for now (the registry code in `core/system_proxy.py` is
  Windows-specific; the rest is cross-platform).
- No subscription URL import yet (planned).

## Project layout

```
russian_vpn/
├── core/
│   ├── parser.py            # share-URL parsers
│   ├── singbox_config.py    # generates sing-box JSON
│   ├── singbox_installer.py # downloads sing-box from GitHub releases
│   ├── singbox_process.py   # subprocess management
│   ├── system_proxy.py      # Windows proxy registry
│   ├── storage.py           # persistent JSON (configs / sites / settings)
│   ├── controller.py        # connect/disconnect orchestration
│   └── paths.py             # filesystem paths
├── gui/
│   ├── main_window.py
│   ├── config_dialog.py
│   ├── sites_dialog.py
│   ├── installer_dialog.py
│   └── styles.py            # dark-theme QSS
├── data/
│   └── default_sites.json   # bundled default direct-routing list
└── main.py                  # QApplication entry point
```

User data (saved configs, edited site list, settings, logs) lives in
`%LOCALAPPDATA%\russian-vpn\`.

## Contributing

PRs welcome. A few directions where help is especially useful:

- TUN mode (so games and any app are tunneled, not just HTTP-proxy-aware ones)
- Linux / macOS port
- Subscription URL importer (base64-list URLs)
- System tray icon
- Latency / health-check pings per config

## License

[GNU GPL v3](LICENSE). Any derivative work must also be GPL v3 — this is
deliberate so that the project cannot be quietly absorbed into a closed-source
product.
