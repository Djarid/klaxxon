#!/usr/bin/env bash
# setup-api.sh — Provision the API LXC (.11) on pve1
#
# Installs Python, deploys Klaxxon FastAPI app, configures systemd service.
# Run from the workstation.
#
# Prerequisites:
#   - pve1 reachable via SSH
#   - Debian 12 template cached on pve1
#   - .env file configured at repo root
#
# Usage: ./deploy/setup-api.sh

set -euo pipefail

# --- Configuration ---
PVE_HOST="pve1"
CTID=111
HOSTNAME="klaxxon-api"
IP="192.168.1.11/24"
GW="192.168.1.1"
TEMPLATE="local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
MEMORY=512
SWAP=256
DISK="local-lvm:4"
CORES=1
TRAEFIK_CTID=100
TRAEFIK_CONF_DIR="/etc/traefik/conf.d"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Validate prerequisites ---
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "ERROR: Missing $REPO_DIR/.env. Copy from .env.template and configure."
    exit 1
fi

echo "=== Setting up Klaxxon API LXC ($HOSTNAME, $IP) ==="

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

# --- Install Python and deploy app ---
echo "Installing Python and deploying app..."
ssh "$PVE_HOST" "sudo pct exec $CTID -- bash -s" <<'LXCEOF'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3

# Create app user FIRST (before any chown operations)
useradd --system --home-dir /opt/klaxxon --create-home --shell /usr/sbin/nologin klaxxon || true

# Create directory structure
mkdir -p /opt/klaxxon/{src,data}
chown -R klaxxon:klaxxon /opt/klaxxon
LXCEOF

# --- Copy application files ---
echo "Deploying application code..."

# Create a tarball of the source, config, requirements, and web SPA
TMPTAR="/tmp/klaxxon-deploy.tar.gz"
tar -czf "$TMPTAR" -C "$REPO_DIR" \
    src/ web/ config.yaml requirements.txt .env 2>/dev/null

# Push tarball to LXC
scp "$TMPTAR" "$PVE_HOST":/tmp/klaxxon-deploy.tar.gz
ssh "$PVE_HOST" "sudo pct push $CTID /tmp/klaxxon-deploy.tar.gz /opt/klaxxon/deploy.tar.gz"
ssh "$PVE_HOST" "rm /tmp/klaxxon-deploy.tar.gz"
rm -f "$TMPTAR"

# Extract and set up venv
ssh "$PVE_HOST" "sudo pct exec $CTID -- bash -s" <<'LXCEOF'
set -euo pipefail

cd /opt/klaxxon
tar -xzf deploy.tar.gz
rm deploy.tar.gz

# Fix .env paths for LXC
sed -i 's|DB_PATH=.*|DB_PATH=/opt/klaxxon/data/meetings.db|' .env

# Point Signal API at comms LXC
sed -i 's|SIGNAL_API_URL=.*|SIGNAL_API_URL=http://192.168.1.10:8082|' .env

# Create venv and install deps
python3 -m venv /opt/klaxxon/.venv
/opt/klaxxon/.venv/bin/pip install --quiet -r requirements.txt

chown -R klaxxon:klaxxon /opt/klaxxon

# Create systemd service
cat > /etc/systemd/system/klaxxon-api.service <<'SVC'
[Unit]
Description=Klaxxon API
After=network.target

[Service]
Type=simple
User=klaxxon
Group=klaxxon
WorkingDirectory=/opt/klaxxon
ExecStart=/opt/klaxxon/.venv/bin/uvicorn src.main:app \
    --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10
EnvironmentFile=/opt/klaxxon/.env

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable klaxxon-api.service
systemctl start klaxxon-api.service

echo ""
echo "Klaxxon API service started."
systemctl status klaxxon-api.service --no-pager || true

LXCEOF

# --- Push Traefik config ---
echo ""
echo "=== Pushing Traefik config ==="
if ssh "$PVE_HOST" "sudo pct status $TRAEFIK_CTID" 2>/dev/null; then
    scp "$SCRIPT_DIR/traefik/klaxxon.yml" "$PVE_HOST":/tmp/klaxxon-traefik.yml && \
    ssh "$PVE_HOST" "sudo pct push $TRAEFIK_CTID /tmp/klaxxon-traefik.yml $TRAEFIK_CONF_DIR/klaxxon.yml && rm -f /tmp/klaxxon-traefik.yml" && \
    ssh "$PVE_HOST" "sudo pct exec $TRAEFIK_CTID -- systemctl reload traefik || true" && \
    echo "  Traefik config pushed and reloaded." || \
    echo "  WARNING: Could not push Traefik config — deploy continues without it."
else
    echo "  WARNING: Traefik LXC ($TRAEFIK_CTID) not found — skipping Traefik config push." || true
fi

echo ""
echo "=== API LXC ($HOSTNAME) provisioned ==="
echo "API running on http://192.168.1.11:8000"
echo "Health check: curl -H 'Authorization: Bearer <token>' http://192.168.1.11:8000/api/health"
