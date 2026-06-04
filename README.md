# KaproVPN

[![Release](https://img.shields.io/github/v/release/fafnirov/KaproVPN?style=flat-square&color=f59e0b&label=latest)](https://github.com/fafnirov/KaproVPN/releases/latest)
[![License](https://img.shields.io/github/license/fafnirov/KaproVPN?style=flat-square&color=blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://www.python.org/)
[![Build](https://img.shields.io/github/actions/workflow/status/fafnirov/KaproVPN/release.yml?style=flat-square&label=build)](https://github.com/fafnirov/KaproVPN/actions/workflows/release.yml)

[English](README.md) · [Русский](README.ru.md)

Cross-platform proxy client (Windows / macOS / Linux) with **split routing
via a customisable direct-list**, built on top of
[Xray-core](https://github.com/XTLS/Xray-core).
Free and open-source forever — GPL v3, no paid tier, no telemetry.

<p align="center">
  <img src="docs/screenshots/main-window.png" alt="KaproVPN main window — dark theme, single-screen layout" width="640">
</p>

---

### ⬇️ Download

Latest stable release — pick the file for your OS:

| OS | File | Notes |
|----|------|-------|
| **Windows 10 / 11 (x64)** | [`KaproVPN-Setup.exe`](https://github.com/fafnirov/KaproVPN/releases/latest) | Per-user install, no admin needed |
| **macOS (Apple Silicon)** | [`KaproVPN-macOS-arm64.dmg`](https://github.com/fafnirov/KaproVPN/releases/latest) | Drag into Applications |
| **Linux (x64)** | [`KaproVPN-Linux-x64.AppImage`](https://github.com/fafnirov/KaproVPN/releases/latest) | `chmod +x` and run |

TUN mode (tunnel every app system-wide, including Telegram / Steam / games)
needs admin/root rights. HTTP-proxy mode works without admin and tunnels
browser traffic.

#### ⚠️ Windows SmartScreen warning on first run

When you run `KaproVPN-Setup.exe`, Windows Defender SmartScreen may say
**"Windows protected your PC"** and refuse to launch. This happens because
we don't pay Microsoft $300/year for an EV code-signing certificate —
this is a free OSS project, not a commercial one. To proceed:

1. Click **"More info"** on the SmartScreen dialog
2. Click **"Run anyway"**

You only do this once per release. macOS may show a similar
**"unidentified developer"** prompt — right-click the `.dmg` → **Open** →
**Open** to bypass (one-time).

---

## What it does

GUI for proxy/VPN connections (Trojan, VLESS with REALITY and XHTTP, VMess,
Shadowsocks, Hysteria2) with one extra trick: domains in a configurable
list **bypass the proxy** and connect directly through your real IP.
Everything else routes through the proxy server.

## Why

When you connect through a foreign proxy, some services refuse to work
because they geofence to a specific country (banks, government portals,
marketplaces). Switching the VPN off every time you need to use one is
annoying. KaproVPN keeps the proxy on for the open Internet and lets
the sites in your direct list see your real address.

## Features

- 🔌 **All major share-URL formats** — `vless://` (incl. REALITY & XHTTP),
  `trojan://`, `vmess://`, `ss://`, `hysteria2://`
- 📥 **Subscription URL import** — paste a single URL, get all configs
  from your provider. Background auto-refresh every 12 h (additive-only,
  never deletes working configs).
- 🛡 **Real firewall kill-switch** — if the tunnel dies, Windows Firewall
  blocks all outbound except the active engine's process: `sing-box.exe` for
  the sing-box TUN engine, or `xray.exe` (+ `hysteria.exe` for Hysteria2) for
  the legacy engine and HTTP mode. No silent leak of your real IP.
- 🔁 **Auto-reconnect** — transparently retries up to 3 times with
  backoff if the active engine crashes mid-session.
- 🔒 **Encrypted-on-disk configs** — Windows DPAPI (same mechanism Chrome
  uses for saved passwords). Old plaintext configs auto-upgrade on first
  launch.
- 🌐 **Two connection modes** —
  - **HTTP-proxy** (no admin) — browser + system-proxy-aware apps, via Xray
  - **TUN** (admin/root) — tunnels every app, including games and Telegram.
    The default engine is **sing-box native TUN**: a single process owns the
    TUN device and routes itself — no local SOCKS bridge (`127.0.0.1:2081`),
    no separate tun2socks. A **legacy Xray + tun2socks** engine stays available
    as a manual fallback in Settings → Движок TUN.
- ✏️ **Editable "always direct" domain list** — 108 sensible defaults
  (banks, government portals, marketplaces, media).
- 📡 **Tray quick-connect** — top-3 fastest configs by ping in the tray
  menu, one click to switch.
- 🌍 **EN / RU localisation** — auto-detected from system locale,
  switchable in Settings.
- 📊 **Live traffic stats + per-config ping** in the UI.
- 🔄 **In-app auto-update** — checks GitHub Releases, downloads, installs.

## Privacy

Short version: **we don't collect anything.** No analytics, no telemetry,
no remote logging. Configs are encrypted on disk on Windows. Xray's
access-log is explicitly disabled in our generated config (no per-domain
log on your disk). The optional download mirror on `kaprovpn.pro/files`
keeps nginx access-logs for 7 days then deletes them; the GitHub
fallback is always available.

Full details in [SECURITY.md](SECURITY.md) including the responsible
disclosure address.

## Requirements

| OS | Minimum |
|----|---------|
| Windows | 10 / 11 (x64) |
| macOS | 12+ (Apple Silicon) |
| Linux | glibc 2.31+ (Ubuntu 20.04+ and equivalents) |

Disk: ~95 MB total (~57 MB app + ~35 MB for sing-box + Xray + tun2socks +
WinTUN, downloaded on first connect — sing-box for the default TUN engine,
Xray for HTTP mode + the legacy engine).

## Install & run

### Option 1 — Installer (recommended)

Download the right file for your OS from
[Releases](https://github.com/fafnirov/KaproVPN/releases/latest) and run it.

### Option 2 — From source (for development / contributing)

```bash
git clone https://github.com/fafnirov/KaproVPN.git
cd KaproVPN
pip install -r requirements.txt
python run.py
```

To build your own installer locally:

```bash
pip install -r requirements-build.txt
pyinstaller KaproVPN.spec          # → dist/KaproVPN.exe (portable, embedded into installer)
pyinstaller KaproVPN-Setup.spec    # → dist/KaproVPN-Setup.exe (Windows installer)
```

On first launch / first connect, the app downloads its binaries into
`%LOCALAPPDATA%\KaproVPN\` (Windows) or `~/.local/share/KaproVPN/`
(macOS / Linux): **sing-box** (`sing-box/`, the default TUN engine),
**Xray-core** (`xray/`, HTTP mode + legacy engine), and on Windows
**tun2socks + wintun.dll** (`tun/`, for the legacy engine and shared WinTUN
driver).

## How it works

1. You paste a share URL (e.g. `vless://…`) or a subscription URL. The app
   parses it into a proxy outbound and applies your split-routing rules:
   - domains from your "direct" list → direct (your real IP)
   - private / LAN / Docker ranges → always direct, never tunnelled
   - everything else → proxy outbound (the parsed URL)
   - DNS goes through the tunnel when leak-protection is on, else direct

Then, depending on mode/engine:

- **HTTP-proxy mode** — `xray.exe` runs and listens on `127.0.0.1:2080`
  (HTTP) / `:2081` (SOCKS5); the OS HTTP-proxy is pointed at port 2080.
- **TUN mode · sing-box (default)** — a single `sing-box.exe` owns the TUN
  device, manages routes (`auto_route` + `auto_detect_interface`), resolves
  DNS, and dials the upstream proxy itself. **No `127.0.0.1:2081` SOCKS
  bridge and no tun2socks** — so the loopback ephemeral-port exhaustion that
  could wedge long classic sessions can't happen, and `direct` traffic exits
  the physical NIC (no route loop).
- **TUN mode · legacy (manual fallback)** — `tun2socks.exe` creates a virtual
  adapter and forwards every packet through `127.0.0.1:2081` into `xray.exe`,
  which routes by rule. Selectable in Settings for configs sing-box can't yet
  reproduce (e.g. XHTTP transport).

If the active engine dies unexpectedly, auto-reconnect retries the **same**
engine. With the firewall kill-switch on, traffic stays blocked until
reconnect or explicit disconnect — no silent leak.

## Project layout

```
kapro_vpn/
├── core/
│   ├── parser.py             # share-URL parsers (vless / vmess / trojan / ss / hy2)
│   ├── xray_config.py        # generates Xray-core JSON with split routing + DNS-leak hardening
│   ├── xray_installer.py     # downloads Xray-core from GitHub releases (with mirror fallback)
│   ├── xray_process.py       # Xray subprocess + log rotation
│   ├── sing_box_config.py    # generates sing-box JSON (default TUN engine) + transport gate
│   ├── sing_box_installer.py # downloads sing-box from GitHub releases (with mirror fallback)
│   ├── sing_box_process.py   # sing-box subprocess + per-connection log-noise classifier
│   ├── tun2socks_installer.py  # legacy engine
│   ├── tun2socks_process.py    # legacy engine
│   ├── network_routes.py     # Windows route/DNS manipulation for the legacy TUN engine
│   ├── network_routes_unix.py # macOS/Linux equivalent
│   ├── admin.py              # UAC / sudo helpers
│   ├── system_proxy.py       # OS HTTP-proxy controller (3 platforms)
│   ├── storage.py            # persistent JSON, transparently routed through DPAPI on Win
│   ├── secrets_store.py      # Windows DPAPI wrapper (Chrome-style on-disk encryption)
│   ├── killswitch.py         # Windows Firewall rules for the real kill-switch
│   ├── controller.py         # connect/disconnect orchestration + auto-reconnect
│   ├── subscription.py       # subscription-URL import + 12 h background refresh
│   ├── i18n.py               # EN/RU translation tables
│   └── paths.py
├── gui/
│   ├── main_window.py
│   ├── tray.py               # system tray with top-3 quick-connect
│   ├── onboarding.py         # first-launch 3-card welcome
│   ├── subscription_dialog.py
│   ├── sites_dialog.py
│   ├── configs_picker.py
│   ├── widgets.py
│   └── styles.py
├── scripts/
│   └── smoke_test.py         # CI gate — imports + parser + xray-config + installer-flow
├── data/
│   └── default_sites.json
└── main.py

installer/                    # standalone PyInstaller bundle for KaproVPN-Setup.exe
├── gui.py                    # Welcome / Maintenance (Reinstall+Uninstall) / Installing pages
├── operations.py             # download + copy + shortcuts + Programs & Features
├── paths.py
└── main.py
```

User data (saved configs, edited site list, settings, logs) lives in:
- Windows: `%LOCALAPPDATA%\KaproVPN\`
- macOS: `~/Library/Application Support/KaproVPN/`
- Linux: `~/.local/share/KaproVPN/`

## Contributing

PRs welcome. The most useful directions right now:

- **Native code-signing on macOS** — if you have a paid Apple Developer
  account, a CONTRIBUTING patch that wires up codesigning + notarytool in
  the GitHub Actions build would let macOS users skip the
  "unidentified developer" Gatekeeper prompt.
- **Android client** — moved to its own repo:
  [fafnirov/KaproVPN-Android](https://github.com/fafnirov/KaproVPN-Android)
  (Kotlin + Compose, v0.1.0 shipped). Shares the RU split-routing list with
  this repo via `kapro_vpn/data/default_sites.json`.
- **IPv6 in TUN mode** — currently IPv4-only; IPv6 traffic can leak
  outside the tunnel.
- **More languages** — `kapro_vpn/core/i18n.py` is dict-based, easy to add.
- **Linux Wayland support** — works on X11/XWayland; native Wayland needs
  PySide6 platform-plugin tweaks.

## Roadmap

- Crash-report opt-in (user-initiated log upload, no auto-collect)
- Public-IP/country indicator after connect (so you see proof the tunnel
  is up)
- macOS Keychain / Linux libsecret equivalent of DPAPI for configs

## License

[GNU GPL v3](LICENSE). Any derivative work must also be GPL v3 — this
is deliberate so the project cannot be quietly absorbed into a closed-
source product.
