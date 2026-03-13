#!/usr/bin/env bash
# decommission-web.sh — Stop and destroy the redundant web LXC (112)
#
# Usage: ./deploy/decommission-web.sh

set -euo pipefail

PVE_HOST="pve1"
CTID=112

echo "=== Decommissioning web LXC ($CTID) ==="

if ssh "$PVE_HOST" "sudo pct status $CTID" 2>/dev/null; then
    echo "  Stopping LXC $CTID..."
    ssh "$PVE_HOST" "sudo pct stop $CTID 2>/dev/null || true"
    echo "  Destroying LXC $CTID..."
    ssh "$PVE_HOST" "sudo pct destroy $CTID --purge"
    echo "  LXC $CTID destroyed."
else
    echo "  LXC $CTID does not exist. Nothing to do."
fi

echo ""
echo "=== Web LXC decommissioned ==="
