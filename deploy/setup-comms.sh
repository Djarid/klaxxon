#!/usr/bin/env bash
# setup-comms.sh — Provision the comms LXC (.10) on pve1
#
# Installs signal-cli REST API and optionally Proton Bridge.
# Run from the workstation (SSH into pve1 to create LXC, then SSH into LXC).
#
# Prerequisites:
#   - pve1 reachable via SSH
#   - Debian 12 template cached on pve1
#   - Certs generated (deploy/gen-certs.sh)
#   - signal-cli REST binary built or downloaded
#
# Usage: ./deploy/setup-comms.sh

set -euo pipefail

# --- Configuration ---
PVE_HOST="pve1"
CTID=110
HOSTNAME="klaxxon-comms"
IP="192.168.1.10/24"
GW="192.168.1.1"
TEMPLATE="local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
MEMORY=512
SWAP=256
DISK="local-lvm:4"
CORES=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"
SIGNAL_CLI_REST_VERSION="0.100"  # Update as needed

# --- Validate certs exist ---
for f in ca.crt comms.crt comms.key; do
    if [ ! -f "$CERT_DIR/$f" ]; then
        echo "ERROR: Missing $CERT_DIR/$f. Run gen-certs.sh first."
        exit 1
    fi
done

echo "=== Setting up Klaxxon comms LXC ($HOSTNAME, $IP) ==="

# --- Create LXC on pve1 ---
echo "Creating LXC $CTID on $PVE_HOST..."
ssh "$PVE_HOST" bash -s <<PVEOF
set -euo pipefail

# Destroy existing if present
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

echo "  Waiting for LXC to start..."
sleep 5

echo "  LXC $CTID created and started."
PVEOF

# --- Copy certs to LXC ---
echo "Copying certs to LXC..."
ssh "$PVE_HOST" "sudo pct exec $CTID -- mkdir -p /etc/klaxxon/certs"
for f in ca.crt comms.crt comms.key; do
    ssh "$PVE_HOST" "cat > /tmp/klaxxon_$f" < "$CERT_DIR/$f"
    ssh "$PVE_HOST" "sudo pct push $CTID /tmp/klaxxon_$f /etc/klaxxon/certs/$f"
    ssh "$PVE_HOST" "rm /tmp/klaxxon_$f"
done
ssh "$PVE_HOST" "sudo pct exec $CTID -- bash -c 'chmod 600 /etc/klaxxon/certs/*.key'"

# --- Install signal-cli REST API inside LXC ---
echo "Installing signal-cli REST API..."
ssh "$PVE_HOST" "sudo pct exec $CTID -- bash -s" <<'LXCEOF'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Update and install deps
apt-get update -qq
apt-get install -y -qq curl openjdk-17-jre-headless wget

# Create signal-cli user
useradd --system --home-dir /opt/signal-cli --create-home --shell /usr/sbin/nologin signal-cli || true

# Download signal-cli REST API
SIGNAL_VERSION="0.100"
SIGNAL_URL="https://github.com/bbernhard/signal-cli-rest-api/releases/download/v${SIGNAL_VERSION}/signal-cli-rest-api-${SIGNAL_VERSION}-linux-amd64"

echo "Downloading signal-cli REST API v${SIGNAL_VERSION}..."
wget -q -O /opt/signal-cli/signal-cli-rest-api "$SIGNAL_URL" || {
    echo "WARNING: Download failed. You may need to manually install signal-cli REST API."
    echo "  Place the binary at /opt/signal-cli/signal-cli-rest-api"
}

if [ -f /opt/signal-cli/signal-cli-rest-api ]; then
    chmod +x /opt/signal-cli/signal-cli-rest-api
fi

# Create data directory
mkdir -p /opt/signal-cli/data

# Create systemd service
cat > /etc/systemd/system/signal-cli-rest.service <<'SVC'
[Unit]
Description=signal-cli REST API
After=network.target

[Service]
Type=simple
User=signal-cli
Group=signal-cli
WorkingDirectory=/opt/signal-cli
ExecStart=/opt/signal-cli/signal-cli-rest-api -signal-cli-config /opt/signal-cli/data
Restart=always
RestartSec=10
Environment=PORT=8082

[Install]
WantedBy=multi-user.target
SVC

chown -R signal-cli:signal-cli /opt/signal-cli

systemctl daemon-reload
systemctl enable signal-cli-rest.service
# Don't start yet — needs device linking first
echo ""
echo "signal-cli REST API installed."
echo "Next steps:"
echo "  1. Start the service: systemctl start signal-cli-rest"
echo "  2. Link device via QR: curl http://localhost:8082/v1/qrcodelink?device_name=klaxxon"
echo "  3. Scan QR with Signal app on your phone"

LXCEOF

echo ""
echo "=== Comms LXC ($HOSTNAME) provisioned ==="
echo "SSH into pve1, then: sudo pct enter $CTID"
echo "Link Signal device before proceeding."
