# KaproTUN

[![Release](https://img.shields.io/github/v/release/fafnirov/KaproTUN?style=flat-square&color=f59e0b&label=latest)](https://github.com/fafnirov/KaproTUN/releases/latest)
[![License](https://img.shields.io/github/license/fafnirov/KaproTUN?style=flat-square&color=blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](https://www.python.org/)
[![Build](https://img.shields.io/github/actions/workflow/status/fafnirov/KaproTUN/release.yml?style=flat-square&label=build)](https://github.com/fafnirov/KaproTUN/actions/workflows/release.yml)

[English](README.md) · [Русский](README.ru.md)

Desktop VPN client (Windows / macOS / Linux) built on a **sing-box native TUN**
dataplane, with **split routing**: Russian sites and games talk to the internet
directly on your real connection, everything else goes through the tunnel.
Free and open-source forever — GPL v3, no paid tier, no telemetry.

<p align="center">
  <img src="docs/screenshots/main-window.png" alt="KaproTUN main window — dark theme, single-screen layout" width="640">
</p>

---

### ⬇️ Download

Latest stable release — pick the file for your OS:

| OS | File | Notes |
|----|------|-------|
| **Windows 10 / 11 (x64)** | [`KaproTUN-Setup.exe`](https://github.com/fafnirov/KaproTUN/releases/latest) | Per-user install, no admin needed to install |
| **macOS (Apple Silicon)** | [`KaproTUN-macOS-arm64.dmg`](https://github.com/fafnirov/KaproTUN/releases/latest) | Drag into Applications |
| **macOS (Intel)** | [`KaproTUN-macOS-x64.dmg`](https://github.com/fafnirov/KaproTUN/releases/latest) | Drag into Applications |
| **Linux (x64)** | [`KaproTUN-Linux-x64.AppImage`](https://github.com/fafnirov/KaproTUN/releases/latest) | `chmod +x` and run |

A portable `KaproTUN.exe` is also published for Windows if you'd rather not install.

**On Windows and Linux, connecting needs admin/root.** There KaproTUN runs in
TUN mode: it creates a virtual network adapter to tunnel every app system-wide
(browsers, games, Telegram), and creating one requires elevation. The app asks
for it when you connect.

**On macOS no administrator password is needed.** macOS does not let an
unprivileged process create a tunnel interface, so by default the client runs
in proxy mode there: the engine starts as the ordinary user and the client
switches the system proxy on by itself. Browsers and apps that use the system
proxy go through the VPN; games, UDP and programs that ignore the proxy do not.
There is no kill-switch and no WebRTC block in this mode. If you want every app
tunnelled, Settings has a button for the full TUN — macOS then asks for the
password once per launch.

#### ⚠️ Windows SmartScreen warning on first run

When you run `KaproTUN-Setup.exe`, Windows Defender SmartScreen may say
**"Windows protected your PC"** and refuse to launch. This happens because we
don't pay Microsoft $300/year for an EV code-signing certificate — this is a
free OSS project, not a commercial one. To proceed:

1. Click **"More info"** on the SmartScreen dialog
2. Click **"Run anyway"**

You only do this once per release.

#### ⚠️ macOS: "Apple could not verify KaproTUN is free of malware"

This is how macOS greets any app without a paid Apple signature (Developer ID,
$99/year) that was downloaded with a browser. The app is not damaged. Two ways
forward:

**Install from Terminal — no warning, no password.** Open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/fafnirov/KaproTUN/main/packaging/install-macos.sh | bash
```

The script picks the build for your Mac (Intel or Apple Silicon), checks the
file against the SHA-256 that GitHub reports, installs the app and opens it.
There is no warning because macOS only checks files a browser has marked on
download. That also means macOS is not checking the app for you: the checksum
comparison stands in for it. The script is short, and you can
[read it](packaging/install-macos.sh) before running it.

**Or allow it by hand, once.** Drag KaproTUN into Applications, try to open it,
and click "Done" on the warning. Then System Settings → Privacy & Security →
"Open Anyway" at the bottom → confirm with your password or Touch ID. On macOS
13 and 14, right-clicking the app → Open → Open is enough.

---

## What it does

A GUI for proxy protocols (VLESS incl. REALITY, Trojan, VMess, Shadowsocks,
Hysteria2) that runs them as a **system-wide tunnel** — plus routing rules that
decide, per destination and per application, what should *not* go through it.

## Why

A foreign exit IP breaks things you still need: banks, government portals and
marketplaces geofence to Russia, and online games get 100+ ms of extra latency
routed through another continent. Turning the VPN off every time is annoying.
KaproTUN keeps the tunnel on for the open internet while those keep using your
real connection.

## How traffic is routed

Rules are evaluated in order — the first match wins:

| # | Traffic | Goes |
|---|---------|------|
| 1 | Private / LAN / Docker / link-local | **direct** |
| 2 | Games — Steam, Riot (League of Legends, Valorant) and your own app list | **direct** |
| 3 | Blocked or geo-restricted services (YouTube, OpenAI, WhatsApp / Meta) | **tunnel** |
| 4 | Your editable "always direct" domain list (168 defaults) | **direct** |
| 5 | Any Russian IP (`geoip:ru`) | **direct** |
| 6 | Everything else | **tunnel** |

Rule 3 sits above the Russian rules on purpose: those services run edge nodes
whose IPs land in `geoip:ru`, and without it they'd leak onto the real
interface and straight into the block they're being tunnelled to avoid.

## Features

- 🔌 **All major share-URL formats** — `vless://` (incl. REALITY), `trojan://`,
  `vmess://`, `ss://`, `hysteria2://`
- 🎮 **Games bypass the VPN** — matched by *process*, not by domain, so a game
  keeps its real ping while your browser stays tunnelled. Built-in list for
  Steam and Riot; add any `.exe` of your own in Settings.
- 🇷🇺 **Russian sites direct by default** — `geoip:ru` → real IP, everything
  else → tunnel.
- 📥 **Subscription URL import** — paste one URL, get every config from your
  provider. Background auto-refresh every 12 h (additive only, never deletes
  working configs).
- 🛡 **Firewall kill-switch** (Windows, optional) — while it is on, nothing but
  the connection to the VPN server and your local network leaves outside the
  tunnel, including while the client is reconnecting. Split routing is off for
  such a session: Russian sites and games go through the VPN too.
- 🔁 **Self-healing** — auto-reconnect with backoff if the engine dies, and an
  automatic clean reconnect when you roam **Ethernet ↔ Wi-Fi** (the tunnel is
  pinned to the interface it was created on and would otherwise leak or stall).
- 🩺 **Network Diagnostics** — adapters, routes, MTU, the route to your server,
  and real TCP/UDP/proxy tests in one copy-pasteable report. Plus an opt-in
  **Network Debug Mode** with millisecond event logging to catch rare problems.
- 🔒 **Encrypted-on-disk configs** — Windows DPAPI (the mechanism Chrome uses
  for saved passwords). Old plaintext configs auto-upgrade on first launch.
- 🚫 **Leak protection** — IPv6 rejected in-tunnel (no `ERR_NETWORK_ACCESS_DENIED`
  fallout; an unreachable route on Linux), WebRTC/STUN blocked on Windows, DNS
  hijacked into the tunnel's resolver.
- 📡 **Tray quick-connect** — top-3 fastest configs by ping, one click to switch.
- 🌍 **EN / RU localisation**, light/dark themes, live traffic graph, per-config
  ping.
- 🔄 **In-app auto-update** — checks GitHub Releases, downloads, installs.

## Privacy

Short version: **we don't collect anything.** No analytics, no telemetry, no
remote logging. Configs are encrypted on disk on Windows. The runtime log keeps
lifecycle events only and runs every line through a redactor that strips
share-URLs and UUIDs; traffic contents are never written. The optional download
mirror on `kaprovpn.pro/files` keeps nginx access logs for 7 days then deletes
them; the GitHub fallback is always available.

Full details in [SECURITY.md](SECURITY.md), including the responsible-disclosure
address.

## Requirements

| OS | Minimum |
|----|---------|
| Windows | 10 / 11 (x64) |
| macOS | 13+ (Apple Silicon and Intel) |
| Linux | glibc 2.31+ (Ubuntu 20.04+ and equivalents) |

Disk: ~90 MB total (~57 MB app + ~33 MB for the sing-box engine and, on
Windows, the WinTUN driver — both downloaded on first connect).

## Install & run

### Option 1 — Installer (recommended)

Download the right file for your OS from
[Releases](https://github.com/fafnirov/KaproTUN/releases/latest) and run it.

### Option 2 — From source (development / contributing)

```bash
git clone https://github.com/fafnirov/KaproTUN.git
cd KaproTUN
pip install -r requirements.txt
python run.py
```

To build your own installer locally:

```bash
pip install -r requirements-build.txt
pyinstaller KaproTUN.spec          # → dist/KaproTUN.exe (portable, embedded into the installer)
pyinstaller KaproTUN-Setup.spec    # → dist/KaproTUN-Setup.exe (Windows installer)
```

On first connect the app downloads its engine into `%LOCALAPPDATA%\KaproTUN\`
(Windows) or `~/.local/share/KaproTUN/` (macOS / Linux): **sing-box**
(`sing-box/`) and, on Windows, the **WinTUN** driver (`tun/`).

> The sing-box version is **pinned to the 1.12.x line** on purpose. The 1.13
> line regressed the VLESS data-path on Windows — the tunnel establishes and
> the handshake succeeds, but payload bytes never flow, which `sing-box check`
> cannot catch because it's a runtime bug, not a config error. An already
> installed 1.13.x is detected and replaced automatically.

## How it works

You paste a share URL (`vless://…`) or a subscription URL. The app parses it
into a proxy outbound, generates a sing-box config with the routing table
above, and starts one process.

A single `sing-box.exe` owns the TUN device, manages routes (`auto_route` +
`auto_detect_interface`), resolves DNS and dials the upstream proxy itself —
there is **no local SOCKS bridge and no tun2socks**, so the loopback
ephemeral-port exhaustion that wedged the old engine cannot happen, and
`direct` traffic exits the physical NIC (no routing loop).

**DNS** uses the system resolver by default — deliberately, because a DoH
exchange over the physical interface is DPI-throttled on many Russian networks
and would black-hole name resolution entirely. The blocked/geo-restricted
domains from rule 3 are the exception: they resolve through a DoH resolver
*inside the tunnel*, where DPI can't see the query.

**MTU is 1400** — conservative on purpose. A larger TUN MTU let small probes
succeed while bigger TLS/video flows stalled on paths where PMTUD or
fragmentation is filtered; 1400 leaves room for REALITY/VLESS encapsulation
without depending on ICMP feedback.

> **`ping` and `tracert` are not usable for diagnosis while connected.** The
> default TUN stack (gVisor) is a userspace TCP/IP stack and answers ICMP echo
> *locally* — `ping 8.8.8.8` reports <1 ms with TTL=64 and `tracert` shows one
> hop. That's expected stack behaviour, not traffic hijacking: real TCP/UDP
> still traverse the tunnel. Use **Settings → Network diagnostics**, which runs
> actual TCP/UDP tests.

## Project layout

```
kapro_tun/
├── core/
│   ├── parser.py             # share-URL parsers (vless / vmess / trojan / ss / hy2)
│   ├── sing_box_config.py    # generates the sing-box JSON: routing, DNS, TUN, transport gate
│   ├── sing_box_installer.py # downloads sing-box (mirror → GitHub), enforces the 1.12.x pin
│   ├── sing_box_process.py   # sing-box subprocess + log-noise classifier
│   ├── controller.py         # connect/disconnect lifecycle, health verdict, roam detection
│   ├── net_diag.py           # Network Diagnostics snapshot + TCP/UDP probes
│   ├── dns_health.py         # bounded DNS / tunnel-transport probes
│   ├── network_routes.py     # Windows route + interface queries
│   ├── network_routes_unix.py, linux_tun_route.py   # macOS / Linux equivalents
│   ├── killswitch.py, ipv6_block.py, webrtc_block.py  # firewall-based leak protection
│   ├── storage.py, secrets_store.py  # persistent JSON, DPAPI-encrypted on Windows
│   ├── subscription.py       # subscription import + background refresh
│   ├── net_conflicts.py      # detects apps that hijack networking (e.g. Meta Horizon Link)
│   ├── runtime_guard.py      # keeps the app alive on an unhandled slot exception
│   ├── app_log.py            # redacted rotating log + Network Debug Mode
│   ├── i18n.py               # EN/RU translation tables
│   └── paths.py
├── gui/
│   ├── main_window.py        # home / settings / logs, watchdogs, connect flow
│   ├── diagnostics_dialog.py # Network Diagnostics screen
│   ├── bypass_apps_dialog.py # user-defined apps that skip the VPN
│   ├── servers_page.py, add_server_v2.py, settings_v2.py, stats_page.py
│   ├── kit.py, tokens.py, icons_v2.py  # shared widgets, design tokens, icons
│   ├── sites_dialog.py, leak_test_dialog.py, updater_dialog.py
│   ├── tray.py               # system tray with top-3 quick-connect
│   └── widgets.py, styles.py
├── scripts/
│   └── smoke_test.py         # CI gate — parsers, config generation, routing, watchdogs
├── data/
│   └── default_sites.json    # the 168-domain direct list
└── main.py

installer/                    # standalone PyInstaller bundle for KaproTUN-Setup.exe
```

User data (saved configs, edited site list, settings, logs) lives in:
- Windows: `%LOCALAPPDATA%\KaproTUN\`
- macOS: `~/Library/Application Support/KaproTUN/`
- Linux: `~/.local/share/KaproTUN/`

## Contributing

PRs welcome. The most useful directions right now:

- **Native code-signing on macOS** — if you have a paid Apple Developer
  account, wiring codesign + notarytool into the GitHub Actions build would let
  macOS users skip the Gatekeeper prompt.
- **Android client** — lives in its own repo:
  [fafnirov/KaproTUN-Android](https://github.com/fafnirov/KaproTUN-Android)
  (Kotlin + Compose). Shares the RU split-routing list with this repo via
  `kapro_tun/data/default_sites.json`.
- **More languages** — `kapro_tun/core/i18n.py` is dict-based and easy to extend.
- **Linux Wayland support** — works on X11/XWayland; native Wayland needs
  PySide6 platform-plugin tweaks.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[GNU GPL v3](LICENSE). Any derivative work must also be GPL v3 — this is
deliberate so the project cannot be quietly absorbed into a closed-source
product.
