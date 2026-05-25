# Security & Privacy

KaproVPN is a free, open-source proxy client. We take privacy seriously
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
KaproVPN-owned service except to download required binaries (see below).

You can verify this by:
- Reading the source — every network call comes from `kapro_vpn/core/*.py`
- Running with Wireshark / Process Monitor and watching outbound
- Auditing the GitHub Actions release workflow (`.github/workflows/release.yml`)

## What lives on your machine

All app state is in `%LOCALAPPDATA%\KaproVPN\` on Windows,
`~/Library/Application Support/KaproVPN/` on macOS,
`~/.local/share/KaproVPN/` on Linux:

| File | Content | Protected? |
|---|---|---|
| `configs.json` | Your saved VPN configs (UUIDs, passwords, keys) | **DPAPI-encrypted on Windows** (1.8.0+); plaintext on mac/Linux with default 0600 perms |
| `settings.json` | App preferences, last-used subscription URL | Plaintext (no high-secrecy fields) |
| `sites.json` | Domains routed direct (your custom additions) | Plaintext (just hostnames) |
| `xray-runtime.json` | The xray-core config we generate on connect | Plaintext (regenerated every connect) |
| `xray.log` | Error-level xray output, last ~1 MB | Plaintext (no per-connection logging — see below) |
| `xray/`, `tun/` | xray-core + tun2socks binaries we downloaded | Standard executables |

### What is **NOT** logged

xray-core has an `access_log` feature that writes one line per
connection: timestamp + source + destination IP/host. We **explicitly
disable this** in our generated xray config (`"access": "none"` in
`log:`). Your browsing history is never written to disk by KaproVPN.

If you've been running an older KaproVPN before 1.8.0, check
`%LOCALAPPDATA%\KaproVPN\xray-access.log` and delete it manually — we
never wrote to it, but the path was reserved.

## Network: what does the app reach out to?

In normal operation:

1. **Your VPN provider's endpoint** — wherever your active config points
2. **`api.github.com/repos/fafnirov/KaproVPN/releases/latest`** — checked
   silently 2 seconds after launch, then once a day. To detect new
   versions. Returns a small JSON, no IP/User-Agent of your providers.
3. **`files.kaprovpn.pro`** — our mirror for xray-core / tun2socks /
   WinTUN driver / geoip-CIDR list. Falls back to upstream
   (github.com/XTLS/Xray-core, wintun.net, ipdeny.com) if mirror is
   down. Downloaded once on first launch, cached forever.
4. **Your subscription URL** — fetched once when you import, then every
   12 hours if `subscription_auto_refresh` is on (default). Can be
   disabled in Settings.

That's it. No other outbound calls exist in the codebase.

### What our mirror logs

`files.kaprovpn.pro` runs nginx, which records IP + User-Agent +
filename for each request. Retention is **7 days**, rotated
automatically, never shipped off the VPS. See
`server-setup/nginx-log-rotation.md` for the exact config.

We do not aggregate, correlate, or share these logs.

### The User-Agent we send

When fetching a subscription, the request goes out as:

    User-Agent: KaproVPN/<version> (Windows; +https://github.com/fafnirov/KaproVPN)

This is so subscription providers can identify us and opt-in to
support our client (the same way Happ / Streisand / NekoBox identify
themselves). The User-Agent contains no user-identifying information.

When fetching from our own mirror or GitHub, the default Python
`requests` User-Agent is used — which leaks the urllib3 version.
That's standard for Python apps and we don't try to obfuscate it.

## DNS leak protection

When TUN mode is active, all traffic is routed through the tunnel by
default. We add explicit bypass routes for:

1. **Public DNS resolvers** (Cloudflare 1.1.1.1, Google 8.8.8.8,
   Yandex 77.88.8.8, Quad9 9.9.9.9) — these go out via your real
   interface so the VPN provider doesn't see your DNS queries.
2. **UDP/53 and TCP/53 in general** — xray routing rules force any
   DNS-port traffic to direct outbound even if some app uses a
   resolver we don't have hardcoded.

For HTTP-proxy mode, DNS is handled by your system resolver as
normal — only HTTP/HTTPS traffic is tunneled.

## What we DON'T defend against

We're honest about the limits:

- **Malware running as you.** If a keylogger is already on your
  machine, KaproVPN's DPAPI encryption doesn't help — the same
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
  risk: `python -m PyInstaller KaproVPN.spec`.

## Reporting

Email **fafnirov@protonmail.com** with subject prefix `[KaproVPN
security]`. Include:

- KaproVPN version (from About / `__version__`)
- OS + version
- Reproduction steps OR proof-of-concept
- Severity in your view

We'll respond within 48 hours, fix critical issues within a week,
credit you in the release notes (unless you'd rather stay
anonymous).
