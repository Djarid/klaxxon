#!/usr/bin/env bash
# setup-api.sh — Provision the API LXC (.11) on pve1
#
# Installs Python, deploys Klaxxon FastAPI app, configures systemd service.
# Run from the workstation.
#
# Prerequisites:
#   - pve1 reachable via SSH
#   - Debian 12 template cached on pve1
#   - Certs generated (deploy/gen-certs.sh)
#   - .env file configured at repo root
#
# Usage: ./deploy/setup-api.sh

set -euo pipefail

# --- Configuration ---
PVE_HOST="pve1"
CTID=111
HOSTNAME="klaxxon-api"
IP="10.10.10.11/24"
GW="10.10.10.1"
TEMPLATE="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
MEMORY=512
SWAP=256
DISK="local-lvm:4"
CORES=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"

# --- Validate prerequisites ---
for f in ca.crt api.crt api.key; do
    if [ ! -f "$CERT_DIR/$f" ]; then
        echo "ERROR: Missing $CERT_DIR/$f. Run gen-certs.sh first."
        exit 1
    fi
done

if [ ! -f "$REPO_DIR/.env" ]; then
    echo "ERROR: Missing $REPO_DIR/.env. Copy from .env.template and configure."
    exit 1
fi

echo "=== Setting up Klaxxon API LXC ($HOSTNAME, $IP) ==="

# --- Create LXC on pve1 ---
echo "Creating LXC $CTID on $PVE_HOST..."
ssh root@"$PVE_HOST" bash -s <<PVEOF
set -euo pipefail

if pct status $CTID &>/dev/null; then
    echo "  LXC $CTID exists, destroying..."
    pct stop $CTID 2>/dev/null || true
    pct destroy $CTID --purge
fi

pct create $CTID $TEMPLATE \
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
ssh root@"$PVE_HOST" "pct exec $CTID -- mkdir -p /etc/klaxxon/certs"
for f in ca.crt api.crt api.key; do
    ssh root@"$PVE_HOST" "cat > /tmp/klaxxon_$f" < "$CERT_DIR/$f"
    ssh root@"$PVE_HOST" "pct push $CTID /tmp/klaxxon_$f /etc/klaxxon/certs/$f"
    ssh root@"$PVE_HOST" "rm /tmp/klaxxon_$f"
done
ssh root@"$PVE_HOST" "pct exec $CTID -- chmod 600 /etc/klaxxon/certs/*.key"

# --- Install Python and deploy app ---
echo "Installing Python and deploying app..."
ssh root@"$PVE_HOST" "pct exec $CTID -- bash -s" <<'LXCEOF'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

# Create app user
useradd --system --home-dir /opt/klaxxon --create-home --shell /usr/sbin/nologin klaxxon || true

# Create directory structure
mkdir -p /opt/klaxxon/{src,data}
chown -R klaxxon:klaxxon /opt/klaxxon
LXCEOF

# --- Copy application files ---
echo "Deploying application code..."

# Create a tarball of the source, config, and requirements
TMPTAR="/tmp/klaxxon-deploy.tar.gz"
tar -czf "$TMPTAR" -C "$REPO_DIR" \
    src/ config.yaml requirements.txt .env 2>/dev/null

# Push tarball to LXC
scp "$TMPTAR" root@"$PVE_HOST":/tmp/klaxxon-deploy.tar.gz
ssh root@"$PVE_HOST" "pct push $CTID /tmp/klaxxon-deploy.tar.gz /opt/klaxxon/deploy.tar.gz"
ssh root@"$PVE_HOST" "rm /tmp/klaxxon-deploy.tar.gz"
rm -f "$TMPTAR"

# Extract and set up venv
ssh root@"$PVE_HOST" "pct exec $CTID -- bash -s" <<'LXCEOF'
set -euo pipefail

cd /opt/klaxxon
tar -xzf deploy.tar.gz
rm deploy.tar.gz

# Fix .env paths for LXC
sed -i 's|DB_PATH=.*|DB_PATH=/opt/klaxxon/data/meetings.db|' .env
sed -i 's|TLS_CERT_PATH=.*|TLS_CERT_PATH=/etc/klaxxon/certs/api.crt|' .env
sed -i 's|TLS_KEY_PATH=.*|TLS_KEY_PATH=/etc/klaxxon/certs/api.key|' .env

# Point Signal API at comms LXC
sed -i 's|SIGNAL_API_URL=.*|SIGNAL_API_URL=http://10.10.10.10:8082|' .env

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
    --host 0.0.0.0 --port 8443 \
    --ssl-certfile /etc/klaxxon/certs/api.crt \
    --ssl-keyfile /etc/klaxxon/certs/api.key
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

echo ""
echo "=== API LXC ($HOSTNAME) provisioned ==="
echo "API running on https://$IP:8443"
echo "Health check: curl -k -H 'Authorization: Bearer <token>' https://10.10.10.11:8443/api/health"
