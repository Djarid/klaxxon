#!/usr/bin/env bash
# setup-web.sh — Provision the web LXC (.12) on pve1
#
# Installs Caddy, deploys the SPA, configures reverse proxy to API LXC.
# Run from the workstation.
#
# Prerequisites:
#   - pve1 reachable via SSH
#   - Debian 12 template cached on pve1
#   - Certs generated (deploy/gen-certs.sh)
#
# Usage: ./deploy/setup-web.sh

set -euo pipefail

# --- Configuration ---
PVE_HOST="pve1"
CTID=112
HOSTNAME="klaxxon-web"
IP="192.168.1.12/24"
GW="192.168.1.1"
TEMPLATE="local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
MEMORY=256
SWAP=128
DISK="local-lvm:2"
CORES=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"

# --- Validate prerequisites ---
for f in ca.crt web.crt web.key; do
    if [ ! -f "$CERT_DIR/$f" ]; then
        echo "ERROR: Missing $CERT_DIR/$f. Run gen-certs.sh first."
        exit 1
    fi
done

echo "=== Setting up Klaxxon web LXC ($HOSTNAME, $IP) ==="

# --- Create LXC on pve1 ---
echo "Creating LXC $CTID on $PVE_HOST..."
ssh "$PVE_HOST" bash -s <<PVEOF
set -euo pipefail

if sudo pct status $CTID &>/dev/null; then
    echo "  LXC $CTID exists, destroying..."
    sudo pct stop $CTID 2>/dev/null || true
    sudo pct destroy $CTID --purge
fi

sudo pct create $CTID $TEMPLATE \
    --hostname $HOSTNAME \
    --memory $MEMORY \
    --swap $SWAP \
    --rootfs $DISK \
    --cores $CORES \
    --net0 name=eth0,bridge=vmbr0,ip=$IP,gw=$GW \
    --unprivileged 1 \
    --features nesting=1 \
    --onboot 1 \
    --start 1

sleep 5
echo "  LXC $CTID created and started."
PVEOF

# --- Copy certs ---
echo "Copying certs..."
ssh "$PVE_HOST" "sudo pct exec $CTID -- mkdir -p /etc/klaxxon/certs"
for f in ca.crt web.crt web.key; do
    ssh "$PVE_HOST" "cat > /tmp/klaxxon_$f" < "$CERT_DIR/$f"
    ssh "$PVE_HOST" "sudo pct push $CTID /tmp/klaxxon_$f /etc/klaxxon/certs/$f"
    ssh "$PVE_HOST" "rm /tmp/klaxxon_$f"
done
ssh "$PVE_HOST" "sudo pct exec $CTID -- bash -c 'chmod 600 /etc/klaxxon/certs/*.key'"

# --- Install Caddy and deploy SPA ---
echo "Installing Caddy and deploying SPA..."
ssh "$PVE_HOST" "sudo pct exec $CTID -- bash -s" <<'LXCEOF'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq curl gnupg debian-keyring debian-archive-keyring apt-transport-https

# Install Caddy from official repo
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
    tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
apt-get update -qq
apt-get install -y -qq caddy

# Create web root
mkdir -p /srv/klaxxon/web
mkdir -p /var/log/klaxxon

# Stop default Caddy service (we'll configure our own)
systemctl stop caddy 2>/dev/null || true

LXCEOF

# --- Copy SPA files ---
echo "Deploying SPA files..."
for f in index.html style.css; do
    ssh "$PVE_HOST" "cat > /tmp/klaxxon_$f" < "$REPO_DIR/web/$f"
    ssh "$PVE_HOST" "sudo pct push $CTID /tmp/klaxxon_$f /srv/klaxxon/web/$f"
    ssh "$PVE_HOST" "rm /tmp/klaxxon_$f"
done

# Copy Caddyfile
ssh "$PVE_HOST" "cat > /tmp/klaxxon_Caddyfile" < "$REPO_DIR/web/Caddyfile"
ssh "$PVE_HOST" "sudo pct push $CTID /tmp/klaxxon_Caddyfile /etc/caddy/Caddyfile"
ssh "$PVE_HOST" "rm /tmp/klaxxon_Caddyfile"

# --- Start Caddy ---
ssh "$PVE_HOST" "sudo pct exec $CTID -- bash -s" <<'LXCEOF'
set -euo pipefail

# Validate Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile || {
    echo "ERROR: Caddyfile validation failed"
    exit 1
}

systemctl start caddy
systemctl enable caddy

echo ""
echo "Caddy started."
systemctl status caddy --no-pager || true

LXCEOF

echo ""
echo "=== Web LXC ($HOSTNAME) provisioned ==="
echo "SPA available at: https://192.168.1.12/"
echo "API proxied through: https://192.168.1.12/api/*"
