#!/bin/bash
# Provision a fresh VPS to serve the KaproTUN file mirror.
#
#   scp -r server-setup root@<new-vps>:/tmp/
#   ssh root@<new-vps> 'bash /tmp/server-setup/bootstrap.sh'
#
# Idempotent: safe to re-run. It refuses rather than guesses whenever it
# finds something it did not create.
#
# What it does NOT do: touch DNS. The client has the mirror hostname
# compiled in, so this box must answer for kaprovpn.pro itself — see the
# preflight check below.

set -uo pipefail

DOMAIN="${DOMAIN:-kaprovpn.pro}"
DOCROOT="/var/www/${DOMAIN}/files"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EMAIL="${EMAIL:-}"           # optional: passed to certbot for expiry warnings

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m  %s\n' "$*"; }
die()  { printf '    \033[31mstop\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"

# --- preflight: does this box actually own the domain? --------------------

step "preflight"

command -v apt-get >/dev/null || die "this script assumes Debian/Ubuntu (apt-get); adapt for your distro"

# The whole mirror is worthless if DNS still points at the old server:
# certbot's HTTP-01 challenge would be answered by that box, not this one.
public_ip="$(curl -fsS --max-time 15 https://api.ipify.org 2>/dev/null || echo '')"
resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"

if [ -z "$public_ip" ]; then
    warn "could not determine this machine's public IP — skipping the DNS check"
elif [ -z "$resolved" ]; then
    die "$DOMAIN does not resolve at all — fix DNS before running this"
elif [ "$public_ip" != "$resolved" ]; then
    printf '\n'
    printf '    %s resolves to %s\n' "$DOMAIN" "$resolved"
    printf '    this machine is    %s\n' "$public_ip"
    printf '\n'
    printf '    Point the A record here first and let it propagate. Running\n'
    printf '    certbot now would validate against the OLD server.\n'
    die "DNS still points elsewhere"
else
    ok "$DOMAIN -> $public_ip (this machine)"
fi

# --- packages -------------------------------------------------------------

step "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || die "apt-get update failed"
apt-get install -y -qq nginx certbot python3-certbot-nginx curl openssl \
    || die "package install failed"
ok "nginx, certbot, curl, openssl"

# --- fail-closed default vhost -------------------------------------------

step "default_server (fail closed on unknown hostnames)"
# Without this, an unmatched request on :443 is answered by whichever
# server block nginx happens to load first — including that block's
# certificate. That is exactly how the old box ended up presenting a
# certificate for an unrelated site to KaproTUN clients.
if [ ! -f /etc/nginx/ssl/default.crt ]; then
    mkdir -p /etc/nginx/ssl
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout /etc/nginx/ssl/default.key \
        -out    /etc/nginx/ssl/default.crt \
        -subj "/CN=invalid" >/dev/null 2>&1 \
        || die "could not generate the placeholder certificate"
    chmod 600 /etc/nginx/ssl/default.key
    ok "placeholder certificate generated"
else
    ok "placeholder certificate already present"
fi

cat > /etc/nginx/sites-available/000-default-deny <<'NGINXDENY'
# Refuse unknown hostnames instead of letting them borrow another site's
# certificate. Managed by KaproTUN server-setup/bootstrap.sh.
server {
    listen 80  default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_certificate     /etc/nginx/ssl/default.crt;
    ssl_certificate_key /etc/nginx/ssl/default.key;
    return 444;
}
NGINXDENY
ln -sf /etc/nginx/sites-available/000-default-deny /etc/nginx/sites-enabled/
# Debian ships its own catch-all that would fight ours over default_server.
rm -f /etc/nginx/sites-enabled/default
ok "unknown hostnames now get 444"

# --- the site vhost -------------------------------------------------------

step "vhost for $DOMAIN"
mkdir -p "$DOCROOT"
chown -R www-data:www-data "/var/www/${DOMAIN}"

VHOST="/etc/nginx/sites-available/${DOMAIN}"
if [ -f "$VHOST" ] && ! grep -q "KaproTUN server-setup" "$VHOST"; then
    warn "$VHOST exists and was not written by this script — leaving it alone."
    warn "Add the 'location /files/' block from nginx.conf.example by hand."
else
    # Plain :80 only. certbot --nginx rewrites this into the TLS vhost and
    # adds the redirect; writing :443 ourselves before a certificate exists
    # would just make nginx refuse to start.
    cat > "$VHOST" <<NGINXSITE
# Managed by KaproTUN server-setup/bootstrap.sh
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    root /var/www/${DOMAIN};

    location /files/ {
        alias ${DOCROOT}/;
        autoindex off;
        types {
            application/zip          zip;
            application/gzip         gz;
            application/x-msdownload exe;
        }
        default_type application/octet-stream;

        location ~* \.(zip|gz|exe)\$ {
            expires 7d;
            add_header Cache-Control "public, immutable";
            add_header X-Content-Type-Options "nosniff";
        }
    }
}
NGINXSITE
    ln -sf "$VHOST" /etc/nginx/sites-enabled/
    ok "vhost written, /files/ -> $DOCROOT"
fi

nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx config is invalid"; }
systemctl reload nginx || systemctl start nginx
ok "nginx reloaded"

# --- certificate ----------------------------------------------------------

step "TLS certificate"
if certbot certificates 2>/dev/null | grep -q "Domains:.*${DOMAIN}"; then
    ok "certificate for $DOMAIN already issued"
else
    cb_args=(--nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect)
    if [ -n "$EMAIL" ]; then
        cb_args+=(-m "$EMAIL")
    else
        cb_args+=(--register-unsafely-without-email)
    fi
    certbot "${cb_args[@]}" || die "certbot failed — check DNS and that :80 is reachable from outside"
    ok "certificate issued"
fi
# certbot installs its own renewal timer; make sure it is actually armed.
systemctl enable --now certbot.timer >/dev/null 2>&1 || true

# --- sync script + schedule ----------------------------------------------

step "sync script"
install -m 755 "${SRC_DIR}/sync-binaries.sh" /usr/local/bin/kaprotun-sync \
    || die "could not install the sync script"
ok "/usr/local/bin/kaprotun-sync"

step "daily timer"
install -m 644 "${SRC_DIR}/kaprotun-mirror.service" /etc/systemd/system/
install -m 644 "${SRC_DIR}/kaprotun-mirror.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kaprotun-mirror.timer
ok "$(systemctl list-timers kaprotun-mirror --no-pager --no-legend | head -1)"

step "log retention (7 days)"
cat > "/etc/logrotate.d/${DOMAIN}" <<LOGROTATE
/var/log/nginx/${DOMAIN}.access.log
/var/log/nginx/${DOMAIN}.error.log
{
    daily
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        [ -f /var/run/nginx.pid ] && kill -USR1 \$(cat /var/run/nginx.pid)
    endscript
}
LOGROTATE
ok "7-day rotation configured"

# --- first sync -----------------------------------------------------------

step "first sync (this downloads ~250 MB)"
/usr/local/bin/kaprotun-sync || warn "sync reported errors — see above"

step "done"
printf '\n'
printf '  Verify from a machine that is NOT this one:\n'
printf '      ./verify-mirror.sh\n\n'
printf '  A local curl can pass while the public TLS path is broken, which is\n'
printf '  the failure this whole setup exists to prevent.\n\n'
