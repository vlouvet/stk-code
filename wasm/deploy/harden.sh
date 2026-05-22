#!/bin/bash
# stk.linuxcolorado.com security hardening pass.
# MUST run as root (invoke via: sudo bash harden.sh).
# Idempotent.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

STAGING=/tmp/stk-deploy-staging
BAK=/root/stk-harden-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p "$BAK"

echo "=== Backup current configs to $BAK ==="
cp -a /etc/nginx/sites-available/stk.linuxcolorado.com "$BAK/" || true
cp -a /etc/systemd/system/stk-wsproxy.service "$BAK/" || true
cp -a /etc/ssh/sshd_config "$BAK/" || true
cp -a /home/vlouvet/stk-wasm/wsproxy_prod.py "$BAK/" || true

echo "=== 1. wsproxy_prod.py (hardened) ==="
install -o vlouvet -g vlouvet -m 644 "$STAGING/wsproxy_prod.py" /home/vlouvet/stk-wasm/wsproxy_prod.py

echo "=== 2. stk-wsproxy.service (sandbox + IPAddressDeny + caps) ==="
install -m 644 "$STAGING/stk-wsproxy.service" /etc/systemd/system/stk-wsproxy.service

echo "=== 3. nginx limits + vhost + scanner 444s ==="
install -m 644 "$STAGING/stk-limits.conf" /etc/nginx/conf.d/stk-limits.conf
install -m 644 "$STAGING/stk.linuxcolorado.com.conf" /etc/nginx/sites-available/stk.linuxcolorado.com
nginx -t

echo "=== 4. fail2ban (sshd + nginx scanners + 429 abuse) ==="
if ! command -v fail2ban-client >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban
fi
install -m 644 "$STAGING/jail.local" /etc/fail2ban/jail.local
install -m 644 "$STAGING/filter-noscript.conf" /etc/fail2ban/filter.d/stk-nginx-noscript.conf
install -m 644 "$STAGING/filter-429.conf"      /etc/fail2ban/filter.d/stk-nginx-429.conf

echo "=== 5. ufw (allow 22/80/443, deny rest) ==="
if ! command -v ufw >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y ufw
fi
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp  comment "ssh"   >/dev/null
ufw allow 80/tcp  comment "http"  >/dev/null
ufw allow 443/tcp comment "https" >/dev/null
ufw --force enable >/dev/null

echo "=== 6. SSH: disable password auth (key-only) ==="
mkdir -p /etc/ssh/sshd_config.d
if ! grep -qsE "^[[:space:]]*PasswordAuthentication[[:space:]]+no" /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
    cat > /etc/ssh/sshd_config.d/99-stk-harden.conf <<EOF
# Hardening: keys only (added $(date -I) by stk harden.sh)
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
fi
sshd -t

echo "=== 7. unattended-upgrades sanity ==="
systemctl enable --now unattended-upgrades >/dev/null 2>&1

echo "=== Reload everything ==="
systemctl daemon-reload
systemctl restart stk-wsproxy
systemctl restart nginx
systemctl restart fail2ban
systemctl reload sshd 2>/dev/null || systemctl restart ssh

echo
echo "=== Status ==="
systemctl is-active stk-wsproxy nginx fail2ban ssh unattended-upgrades || true
echo
ufw status numbered | head -12
echo
fail2ban-client status

echo
echo "Done. Backups at $BAK"
echo "Open a NEW SSH session to verify SSH still works before closing this one."
