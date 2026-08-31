# KaproTUN file mirror — server setup

The client fetches its engine and its own updates from
`https://kaprovpn.pro/files/`, falling back to upstream GitHub on any
failure. This directory holds everything needed to run that mirror.

**Why mirror at all?** The fallback is `github.com`, which is exactly what
gets DNS-blocked or throttled for a large share of our users. For them the
mirror is not an optimisation — it is the only working path. A dead mirror
does not look broken from a developer's machine, because the fallback
quietly succeeds there.

```
client (first launch / update)
  ├─ wants sing-box, WinTUN, or KaproTUN-Setup.exe
  ├─ tries  https://kaprovpn.pro/files/<asset>     ← this server
  └─ falls back to GitHub — unreachable for RU users
```

## What is served

Exactly three things, and nothing else:

| Asset | Upstream | Required? |
|---|---|---|
| `sing-box-<pin>-<platform>.zip` / `.tar.gz` (6 platforms) | `SagerNet/sing-box` | **yes** — no engine, no VPN |
| `wintun-<ver>.zip` | `wintun.net` | no — wintun.net isn't blocked in RU |
| `KaproTUN-Setup-v<ver>.exe` | our own release, asset `KaproTUN-Setup.exe` | **yes** — the auto-updater's only RU-reachable path |

The geoip zone file is **not** mirrored: `geoip_ru.py` fetches ipdeny.com
directly and ipdeny is not blocked.

Disk usage: roughly 250 MB with three installers retained.

Versions are **not** pinned in the sync script. It reads
`SINGBOX_PINNED_VERSION` and `WINTUN_FILENAME` straight out of
`kapro_tun/core/sing_box_installer.py` on `main`, so bumping the pin in a
commit is all it takes — the mirror follows on its next run and can never
drift from what the client actually requests.

---

## 0. First: is the mirror even reachable?

Run this from a machine that is **not** the VPS. A local `curl` can
succeed while the public TLS path is broken:

```bash
./verify-mirror.sh
```

It checks the certificate (validity, hostname match, days to expiry) and
every asset the client will ask for. Exit code is non-zero on any problem,
so it can gate a cron job or CI.

### If it reports a hostname mismatch

The server is presenting a certificate issued for a different domain. That
means nginx never matched a `server_name kaprovpn.pro` block and fell
through to whatever `:443` block came first — typically another site on
the same box.

Diagnose:

```bash
sudo nginx -T | grep -n "server_name\|ssl_certificate\|listen 443"
```

Look for: is there still a block with `server_name kaprovpn.pro;`? Is it
`include`d? Does its certificate exist on disk?

Reissue or renew the certificate:

```bash
sudo certbot --nginx -d kaprovpn.pro
sudo certbot certificates          # confirm expiry and the domains covered
sudo nginx -t && sudo systemctl reload nginx
```

Then add the `default_server` block from `nginx.conf.example`. Without it
this failure mode is silent — any unmatched hostname borrows a neighbour's
certificate instead of being refused.

Re-run `./verify-mirror.sh` from outside the box to confirm.

---

## 1. nginx — serve `/files/`

Paste the `location /files/` block from `nginx.conf.example` into the
existing `kaprovpn.pro` `:443` server block, and add the `default_server`
block alongside it. Then:

```bash
sudo mkdir -p /var/www/kaprovpn.pro/files
sudo chown -R www-data:www-data /var/www/kaprovpn.pro/files
sudo nginx -t && sudo systemctl reload nginx
```

## 2. Install the sync script

```bash
sudo install -m 755 sync-binaries.sh /usr/local/bin/kaprotun-sync
sudo /usr/local/bin/kaprotun-sync
```

It stages every download in a temp dir and only then renames each file
into the docroot, so nginx never serves a partially written file. A failed
download leaves the **previous** copy in place — a stale mirror still
serves clients; a truncated one does not. The script exits non-zero if any
required asset failed.

It refuses to mirror a sing-box 1.13.x pin: that line breaks the VLESS
data-path on Windows, and if a bad pin ever lands in a commit the mirror
should not amplify it.

## 3. Schedule it

systemd (preferred — `systemctl list-timers` shows the next run):

```bash
sudo install -m 644 kaprotun-mirror.service /etc/systemd/system/
sudo install -m 644 kaprotun-mirror.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kaprotun-mirror.timer
```

Check on it:

```bash
systemctl list-timers kaprotun-mirror
journalctl -u kaprotun-mirror -n 50
```

Or plain cron, if you'd rather:

```bash
sudo crontab -e
# 04:17 daily, log kept for the last run only
17 4 * * * /usr/local/bin/kaprotun-sync > /var/log/kaprotun-sync.log 2>&1
```

## 4. After every release

The timer picks up a new installer within a day on its own. To publish it
immediately:

```bash
sudo /usr/local/bin/kaprotun-sync && ./verify-mirror.sh
```

Worth doing as the last step of the release ritual — a release whose
installer isn't on the mirror is a release RU users can't update to.

---

## Logs

Access-log retention is deliberately short — see
[nginx-log-rotation.md](nginx-log-rotation.md).
