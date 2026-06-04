# Security & Privacy

KaproTUN is a free, open-source proxy client. We take privacy seriously
because that's the whole point of the tool.

If you find a security vulnerability, please email **fafnirov@protonmail.com**
rather than opening a public GitHub issue. We aim to acknowledge within
48 hours and ship a fix within a week for critical issues.

---

## What we collect

**Nothing.**

There is no telemetry, no analytics (no Google Analytics, no Sentry,
no posthog, no Cloudflare Analytics), no "anonymous usage stats", no
phone-home, no crash reporter. The app does not connect to any
KaproTUN-owned service except to download required binaries (see below).

You can verify this by:
- Reading the source — every network call comes from `kapro_tun/core/*.py`
- Running with Wireshark / Process Monitor and watching outbound
- Auditing the GitHub Actions release workflow (`.github/workflows/release.yml`)

## What lives on your machine

All app state is in `%LOCALAPPDATA%\KaproTUN\` on Windows,
`~/Library/Application Support/KaproTUN/` on macOS,
`~/.local/share/KaproTUN/` on Linux:

| File | Content | Protected? |
|---|---|---|
| `configs.json` | Your saved VPN configs (UUIDs, passwords, keys) | **Encrypted at rest**: DPAPI on Windows (1.8.0+), AES-256-GCM with an OS-keystore key on macOS/Linux (Keychain / Secret Service, 1.16.12+). 0600 perms |
| `secrets.json` | Subscription URLs + last-seen usage info (traffic/expiry) | **Same encryption as configs.json** (2.0.0). A subscription URL is a bearer credential, so it is **no longer kept in settings.json** |
| `settings.json` | App preferences only — **no secrets** | Plaintext, 0600. Subscription fields were moved out into `secrets.json` in 2.0.0 (auto-migrated on first launch) |
| `sites.json` | Domains routed direct (your custom additions) | Plaintext (just hostnames) |
| `xray-runtime.json`, `hysteria-client.yaml`, `sing-box-runtime.json` | The xray / hysteria / sing-box configs we generate on connect — these embed the server UUID / password / auth (the sing-box config is the v3.0.0 native-TUN engine's runtime config) | Written 0600, atomically; **deleted on every disconnect/exit** after the processes stop; never logged (2.0.0; sing-box 3.0.0) |
| `xray.log` | Error-level xray output, last ~1 MB | Plaintext (no per-connection logging — see below) |
| `xray/`, `tun/`, `hysteria/`, `sing-box/` | xray-core + tun2socks + hysteria + sing-box binaries we downloaded | Standard executables |

### Encryption: when plaintext is (and isn't) possible

Encryption is the default everywhere a keystore exists. Plaintext at rest is
used **only** where the platform genuinely has no keystore — e.g. a headless
Linux box with no Secret Service daemon and no `secret-tool` — and there file
permissions (0600) are the protection, the same model as `~/.ssh/config`.

What we do **not** do (changed in 2.0.0): silently fall back to plaintext on a
machine that *can* encrypt. If encryption is supported but fails (a DPAPI API
rejection, an unreachable keychain), we **refuse to write the secret in the
clear** — the value stays in memory only, the failure is recorded
(`storage.last_error()`) and logged, and the app keeps running. You never get
an invisible downgrade from encrypted to plaintext.

### What is **NOT** logged

xray-core has an `access_log` feature that writes one line per
connection: timestamp + source + destination IP/host. We **explicitly
disable this** in our generated xray config (`"access": "none"` in
`log:`). Your browsing history is never written to disk by KaproTUN.

If you've been running an older KaproTUN before 1.8.0, check
`%LOCALAPPDATA%\KaproTUN\xray-access.log` and delete it manually — we
never wrote to it, but the path was reserved.

## Network: what does the app reach out to?

In normal operation:

1. **Your VPN provider's endpoint** — wherever your active config points
2. **`api.github.com/repos/fafnirov/KaproTUN/releases/latest`** — checked
   silently 2 seconds after launch, then once a day. To detect new
   versions. Returns a small JSON, no IP/User-Agent of your providers.
3. **`kaprovpn.pro/files`** — our mirror for xray-core / tun2socks /
   sing-box / WinTUN driver / geoip-CIDR list. Falls back to upstream
   (github.com/XTLS/Xray-core, github.com/SagerNet/sing-box, wintun.net,
   ipdeny.com) if mirror is down. Downloaded once on first launch, cached
   forever.
4. **Your subscription URL** — fetched once when you import, then every
   12 hours if `subscription_auto_refresh` is on (default). Can be
   disabled in Settings.

That's it. No other outbound calls exist in the codebase.

### What our mirror logs

`kaprovpn.pro/files` runs nginx, which records IP + User-Agent +
filename for each request. Retention is **7 days**, rotated
automatically, never shipped off the VPS. See
`server-setup/nginx-log-rotation.md` for the exact config.

We do not aggregate, correlate, or share these logs.

### The User-Agent we send

When fetching a subscription, the request goes out as:

    User-Agent: KaproVPN/<version> (Windows; +https://github.com/fafnirov/KaproTUN)

This is so subscription providers can identify us and opt-in to
support our client (the same way Happ / Streisand / NekoBox identify
themselves). The User-Agent contains no user-identifying information.

The name stays `KaproVPN/` even though the app is now KaproTUN: several
providers whitelist their subscription endpoint on the exact `KaproVPN/`
prefix, and changing it returns a dead "App not supported" stub instead of
real servers. It is a wire compatibility token, not branding.

**Subscriptions are HTTPS-only.** The import UI rejects `http://` links: a
subscription URL is a bearer credential, and over plaintext HTTP both it and
the server list it returns are exposed to anyone on the path. Ask your
provider for an `https://` link.

When fetching from our own mirror or GitHub, the default Python
`requests` User-Agent is used — which leaks the urllib3 version.
That's standard for Python apps and we don't try to obfuscate it.

## DNS leak protection

When TUN mode is active, all traffic is routed through the tunnel by
default, and **DNS leak protection** decides where name resolution goes. The
mechanism differs by TUN engine, but the user-visible contract is the same:
with leak-protection ON, queries ride the tunnel (the ISP/provider can't see
them); with it OFF, they go direct (ISP-visible) — an explicit opt-out.

- **sing-box (default engine).** A route rule hijacks all `:53` traffic
  (`action: hijack-dns`) to sing-box's own DNS module. With leak-protection
  ON the upstream resolver is reached **through the proxy** (`detour: proxy`);
  with it OFF it's reached directly. Private / LAN / Docker ranges are pinned
  to `direct`, so local resolvers and devices keep working.
- **legacy (Xray + tun2socks).** xray routing rules force any DNS-port
  traffic (UDP/53, TCP/53) to the appropriate outbound, plus explicit kernel
  bypass routes for public resolvers (Cloudflare 1.1.1.1, Google 8.8.8.8,
  Yandex 77.88.8.8, Quad9 9.9.9.9) so they leave via the real interface when
  leak protection is off.

For HTTP-proxy mode, DNS is handled by your system resolver as
normal — only HTTP/HTTPS traffic is tunneled.

## Coverage: HTTP-proxy mode vs TUN

KaproTUN has two modes and they protect different things — be clear which
you're in:

- **HTTP-proxy mode (default, no admin).** Only apps that honour the system
  HTTP proxy — browsers, basically — are tunneled, and only their HTTP/HTTPS
  (TCP) traffic. Everything else — Telegram, games, other system and all UDP
  traffic — goes out over your **real IP**. This is **not** a whole-system
  VPN; it's a deliberate trade-off for zero admin rights and instant-on. The
  UI labels the mode "только браузер" and says so at the toggle.
- **TUN mode (needs admin / sudo).** Creates a system TUN device and routes
  the whole machine's IPv4 through the tunnel — every app, like a traditional
  VPN. Pick this for full-system protection. The default engine is **sing-box
  native TUN** (one process owns the TUN, no `127.0.0.1:2081` SOCKS bridge);
  a **legacy Xray + tun2socks** engine is a manual fallback. Either way the
  whole-system routing + leak protection above applies.

The IPv6- and WebRTC-leak blocks are armed in **both** modes — a browser in
HTTP mode can otherwise leak the real address over native IPv6 or WebRTC STUN.

**RU-direct is opt-in.** Routing the entire Russian IP range *outside* the
tunnel (`route_ru_direct`) is **off by default**. When off, RU traffic goes
through the VPN like everything else; the kernel bypass for `geoip:ru` is
installed only when you turn the option on (2.0.0 fixed a gap where TUN
bypassed RU regardless of the setting). The curated direct-domain list is
separate and always applied.

## Kill-switch

Optional (Settings → kill-switch), **Windows-only** today, needs admin. When
on, it installs Windows Firewall rules that block ALL outbound except: your
LAN (so printers / NAS / router UI keep working), `xray.exe`, for Hysteria2
sessions — `hysteria.exe` (the process that actually egresses for hy2; 2.0.0
closed a gap where it was blocked and hy2 wouldn't connect under the
kill-switch), and — for the v3.0.0 sing-box TUN engine — `sing-box.exe` (the
single native-TUN process that egresses to the server in that mode). If the
tunnel process dies, traffic stops rather than
silently falling back to your ISP. All KaproTUN firewall rules are removed on
disconnect and swept on the next launch if the app crashed.

## Downloads

Binaries and the installer are fetched over HTTPS from our mirror
(`kaprovpn.pro/files`) with a GitHub fallback, and every download is
**size-capped** (2.0.0): a response that declares — or streams — more than the
per-asset ceiling is rejected, so a hostile or broken mirror can't fill your
disk or RAM. We do not yet verify a SHA-256 / signature of the downloaded
binaries; that is tracked for a future release (see the limits below).

## What we DON'T defend against

We're honest about the limits:

- **Malware running as you.** If a keylogger is already on your
  machine, KaproTUN's DPAPI encryption doesn't help — the same
  Windows account that decrypts configs can be impersonated by
  anything you run.
- **State-actor adversary with disk access + your Windows password.**
  DPAPI keys are derived from your Windows credentials. Someone with
  both can decrypt configs.json offline.
- **Process memory.** xray-core (and our Python process) hold your
  VPN keys in RAM while connected. A privileged debugger or RAM
  acquisition tool can extract them.
- **TLS fingerprinting by your VPN provider.** Standard for any
  proxy client.
- **Your VPN provider's logging policy.** That's between you and
  them, not us.
- **Reproducible builds.** We don't currently produce signed SLSA
  attestations. Trust in our .exe rests on (a) GitHub Actions logs
  being public, (b) every commit being signed in git, (c) the code
  being open. A determined supply-chain attacker who compromises my
  GitHub account could ship a backdoored binary — and you wouldn't
  catch it just from the binary. Building from source removes this
  risk: `python -m PyInstaller KaproTUN.spec`.

## Reporting

Email **fafnirov@protonmail.com** with subject prefix `[KaproTUN
security]`. Include:

- KaproTUN version (from About / `__version__`)
- OS + version
- Reproduction steps OR proof-of-concept
- Severity in your view

We'll respond within 48 hours, fix critical issues within a week,
credit you in the release notes (unless you'd rather stay
anonymous).
