# Security & Privacy

KaproTUN is a free, open-source VPN client. We take privacy seriously because
that's the whole point of the tool — and that includes being blunt about what
it does **not** protect.

If you find a security vulnerability, please email **fafnirov@protonmail.com**
rather than opening a public GitHub issue. We aim to acknowledge within
48 hours and ship a fix within a week for critical issues.

---

## Read this first: what leaves the tunnel by default

KaproTUN is a **split-routing** client, not an everything-through-the-tunnel
VPN. Several categories deliberately use your **real connection**, and you
should know which before relying on it:

| Traffic | Default | Setting |
|---|---|---|
| Any Russian IP (`geoip:ru`) | **direct — real IP** | `route_ru_direct` (on) |
| Games — Steam, Riot, and apps you add | **direct — real IP** | `games_direct` (on) |
| Your editable direct-domain list (168 entries) | **direct — real IP** | Settings → Прямые сайты |
| LAN / Docker / link-local | direct | always |
| DNS queries | **your system resolver — ISP-visible** | see below |
| Everything else | tunnel | — |

If you need *everything* through the tunnel, turn off RU-direct and the games
bypass in Settings, and be aware DNS still uses the system resolver.

**Games bypass is enforced at the routing-table level.** Game-server networks
(Riot Direct, Valve/Steam) are excluded from the TUN itself, so those packets
never enter the tunnel at all — that is what makes the latency normal, and it
also means that traffic is not protected by the VPN.

## DNS: what your ISP can see

Since v3.1.1 DNS is **the system resolver by default**, reached over your
physical interface. Practically: your ISP or network operator can see the
domains you look up, the same as with the VPN off.

This is a deliberate reliability trade-off. The previous design sent DNS over
DoH through the tunnel, and on many Russian networks that exchange was
throttled by DPI until name resolution black-holed entirely — a VPN that can't
resolve anything is worse than one whose queries are visible.

Two carve-outs:

- All `:53` traffic is **hijacked** into sing-box's DNS module (`hijack-dns`),
  so applications can't quietly use their own hardcoded resolver.
- Blocked / geo-restricted domains (YouTube, OpenAI, WhatsApp / Meta) resolve
  through **DoH inside the tunnel**, because their answers are tampered with on
  some networks. Only those domains take that path.

The old `dns_leak_protection` setting is accepted for backwards compatibility
but has **no effect**.

## What we collect

**Nothing.**

No telemetry, no analytics (no Google Analytics, no Sentry, no PostHog, no
Cloudflare Analytics), no "anonymous usage stats", no phone-home, no crash
reporter. The app contacts no KaproTUN-owned service except to download the
engine binaries (see below).

You can verify this by:
- Reading the source — every network call is in `kapro_tun/core/*.py`
- Running Wireshark / Process Monitor and watching outbound traffic
- Auditing the release workflow (`.github/workflows/release.yml`)

## What lives on your machine

All app state is in `%LOCALAPPDATA%\KaproTUN\` on Windows,
`~/Library/Application Support/KaproTUN/` on macOS,
`~/.local/share/KaproTUN/` on Linux:

| File | Content | Protected? |
|---|---|---|
| `configs.json` | Saved server configs (UUIDs, passwords, keys) | **Encrypted at rest**: DPAPI on Windows, AES-256-GCM with an OS-keystore key on macOS/Linux (Keychain / Secret Service). 0600 perms |
| `secrets.json` | Subscription URLs + last-seen usage (traffic/expiry) | **Same encryption.** A subscription URL is a bearer credential, so it is never kept in `settings.json` |
| `settings.json` | Preferences only — **no secrets** | Plaintext, 0600 |
| `sites.json` | Your direct-routing domains | Plaintext (hostnames only) |
| `sing-box-runtime.json` | The sing-box config generated on connect — embeds the server UUID / password / keys | Written 0600, atomically; **deleted on every disconnect/exit**; never logged |
| `app.log` (+ `.1`, `.2`) | Lifecycle events, watchdog verdicts, reconnect reasons. ~1 MB × 3 max | Plaintext, but every line passes a redactor that strips share-URLs and UUIDs. **Traffic contents are never written** |
| `logs/runtime-crash-*.log` | Python tracebacks the app survived | Plaintext, no secrets |
| `bandwidth_history.db` | Per-minute byte totals for the Stats page | Local only, no destinations |
| `sing-box/`, `tun/` | The sing-box binary and the WinTUN driver | Standard executables |

Older installs may still contain `xray.log`, `xray/`, `tun2socks`, or
`hysteria/` from the pre-v3.1.0 engine. They are unused and safe to delete.

### Network Debug Mode

Off by default. When enabled (Settings), `app.log` additionally records
millisecond-stamped health-probe verdicts, reconnect causes and network-change
events. It logs event names and technical fields only — never traffic contents
— and still passes the same redactor.

### Encryption: when plaintext is (and isn't) possible

Encryption is the default everywhere a keystore exists. Plaintext at rest is
used **only** where the platform genuinely has none — e.g. a headless Linux box
with no Secret Service daemon — and there file permissions (0600) are the
protection, the same model as `~/.ssh/config`.

What we do **not** do: silently fall back to plaintext on a machine that *can*
encrypt. If encryption is supported but fails, we **refuse to write the secret
in the clear** — it stays in memory, the failure is recorded
(`storage.last_error()`) and logged, and the app keeps running. There is no
invisible downgrade.

## Network: what does the app reach out to?

1. **Your VPN server** — wherever your active config points.
2. **`api.github.com/repos/fafnirov/KaproTUN/releases/latest`** — checked a few
   seconds after launch, then daily, to detect new versions.
3. **`kaprovpn.pro/files`** — our mirror for the sing-box binary, the WinTUN
   driver and the geoip-CIDR list. Falls back to upstream
   (github.com/SagerNet/sing-box, wintun.net, ipdeny.com). Downloaded once and
   cached.
4. **Your subscription URL** — on import, then every 12 hours if
   `subscription_auto_refresh` is on (default). Can be disabled.
5. **A public IP probe** after connect (`public_ip_probe`, on by default) — one
   HTTPS request **through the tunnel** so the UI can show your exit IP. Can be
   disabled in Settings.

That's it. No other outbound calls exist in the codebase.

### What our mirror logs

`kaprovpn.pro/files` runs nginx, which records IP + User-Agent + filename per
request. Retention is **7 days**, rotated automatically, never shipped off the
VPS. We do not aggregate, correlate or share these logs.

### The User-Agent we send

When fetching a subscription:

    User-Agent: KaproVPN/<version> (Windows; +https://github.com/fafnirov/KaproTUN)

It carries no user-identifying information. The name stays `KaproVPN/` even
though the app is now KaproTUN: several providers gate their subscription
endpoint on that exact prefix, and changing it returns a dead "App not
supported" stub instead of real servers. It is a wire-compatibility token, not
branding.

**Minimal-metadata mode** (Settings, off by default) freezes that string to a
constant with no version and no OS, disables the 12-hour refresh heartbeat, and
fetches the subscription through the tunnel so the endpoint sees the VPN exit
IP. It reduces what a subscription endpoint can link to your device; it does
**not** make you anonymous to the VPN provider, whose servers carry your traffic
regardless.

**Subscriptions are HTTPS-only.** The import UI rejects `http://` links: the URL
is a bearer credential, and over plaintext HTTP both it and the server list it
returns are exposed to anyone on the path.

## Leak protection

- **IPv6.** The TUN carries an IPv6 address, so `::/0` is captured into the
  tunnel; global-unicast v6 (`2000::/3`) is then **rejected in-tunnel** with a
  clean RST/ICMP, so Happy Eyeballs falls back to IPv4 instantly and v6 never
  egresses your real interface. LAN/ULA/link-local v6 stays direct.
- **WebRTC/STUN.** Firewall rules block the common STUN ports so a browser
  can't reveal the real address through WebRTC.
- **QUIC.** On the default userspace stack, tunnelled QUIC (UDP/443) is
  rejected so browsers fall back to TCP, which the tunnel carries reliably.

## Kill-switch

Optional (Settings), **Windows-only**, needs admin. When on, Windows Firewall
blocks all outbound except your LAN (printers / NAS / router UI) and
**`sing-box.exe`** — the only process that reaches the public internet. If the
tunnel process dies, traffic stops instead of silently falling back to your ISP.
All KaproTUN firewall rules are removed on disconnect, and swept on the next
launch if the app crashed.

Note the interaction with split routing: destinations excluded from the tunnel
(games, RU IPs) are carried by `sing-box.exe` too, so they keep working under
the kill-switch — by design.

## Downloads

Binaries are fetched over HTTPS from our mirror with a GitHub fallback, and
every download is **size-capped**: a response that declares — or streams — more
than the per-asset ceiling is rejected, so a hostile or broken mirror can't fill
your disk or RAM. We do **not** yet verify a SHA-256 or signature of downloaded
binaries; that is tracked for a future release.

The sing-box engine is **pinned to the 1.12.x line**. The 1.13 line regressed
the VLESS data path on Windows (tunnel establishes, payload never flows), so an
installed 1.13.x is detected and replaced.

## What we DON'T defend against

We're honest about the limits:

- **Split routing is not a bug.** RU IPs, games and your direct-domain list
  leave the tunnel by default (see the first table). If your threat model needs
  everything tunnelled, change those settings.
- **DNS is ISP-visible by default** (see above).
- **Malware running as you.** If a keylogger is already on your machine, DPAPI
  encryption doesn't help — the account that decrypts configs can be
  impersonated by anything you run.
- **An adversary with disk access + your Windows password.** DPAPI keys derive
  from your Windows credentials; both together decrypt `configs.json` offline.
- **Process memory.** sing-box and our Python process hold keys in RAM while
  connected. A privileged debugger or RAM acquisition tool can extract them.
- **TLS fingerprinting by your VPN provider.** Standard for any proxy client.
- **Your VPN provider's logging policy.** That's between you and them.
- **Third-party network drivers.** Software that installs its own network
  filter (VR link tools, some antivirus) can intercept traffic before it reaches
  our TUN. The client detects known offenders and warns, but cannot override
  them from userspace.
- **Reproducible builds.** We don't produce signed SLSA attestations. Trust in
  our `.exe` rests on (a) public GitHub Actions logs, (b) signed commits,
  (c) open code. A supply-chain attacker who compromised the GitHub account
  could ship a backdoored binary. Building from source removes this risk:
  `pyinstaller KaproTUN.spec`.

## Reporting

Email **fafnirov@protonmail.com** with subject prefix `[KaproTUN security]`.
Include:

- KaproTUN version (About / `__version__`)
- OS + version
- Reproduction steps OR proof-of-concept
- Severity in your view

We'll respond within 48 hours, fix critical issues within a week, and credit you
in the release notes (unless you'd rather stay anonymous).
