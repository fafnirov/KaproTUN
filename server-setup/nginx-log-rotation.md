# Mirror access-log retention — keep it short

KaproVPN's file mirror (files.kaprovpn.pro) is logged by nginx like any
website. Each line: IP + timestamp + User-Agent + downloaded URL.

For a privacy-respecting project, we keep this log retention as short
as practical:

  - **7 days max** on disk
  - Logs are NOT shipped anywhere off the VPS
  - No log aggregation, no Cloudflare Analytics, no GA, nothing
  - When the user comes back to support and asks "I had trouble on
    May 15th, what happened?" — we genuinely can't tell. That's fine.

## Setup on the VPS (one-time)

Debian/Ubuntu ships `logrotate` by default. Confirm:

```bash
which logrotate
# /usr/sbin/logrotate
```

Then drop in this config — replaces the default nginx rotation
(usually 14 days) with our shorter policy:

```bash
sudo tee /etc/logrotate.d/files.kaprovpn.pro <<'EOF'
/var/log/nginx/files.kaprovpn.pro.access.log
/var/log/nginx/files.kaprovpn.pro.error.log
{
    daily
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        # Make nginx re-open log files after rotation. Without this it
        # keeps writing to the old (now compressed) file's inode.
        [ -f /var/run/nginx.pid ] && kill -USR1 $(cat /var/run/nginx.pid)
    endscript
}
EOF
```

Test the rule (dry run, no actual rotation):

```bash
sudo logrotate -d /etc/logrotate.d/files.kaprovpn.pro
```

Force one rotation now to verify it works:

```bash
sudo logrotate -f /etc/logrotate.d/files.kaprovpn.pro
ls -la /var/log/nginx/files.kaprovpn.pro.access.log*
# Should show .log + .log.1.gz after first run
```

logrotate runs daily via `/etc/cron.daily/logrotate`. Already wired
up — nothing else to do.

## Verifying retention is working a week later

```bash
ls -la /var/log/nginx/files.kaprovpn.pro.access.log*
# Should show .log (current) + .log.1.gz through .log.7.gz max.
# .log.8 should NOT exist — logrotate deleted it on day 8.
```

## Why not zero retention?

We need *something* short-term for debugging — "user reports the
download was slow yesterday" needs at least 24-48 hours of logs.
7 days is the comfortable minimum for sysadmin work without being
a long-term tracking liability.
