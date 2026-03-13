#!/usr/bin/env bash
# redeploy-api.sh — Safe redeploy of the API LXC
#
# 1. Dumps the live DB from LXC 111 to a local backup
# 2. Recreates the API LXC via the deploy script
# 3. Restores the DB backup into the fresh LXC
# 4. Restarts the service
#
# Usage: ./deploy/redeploy-api.sh

set -euo pipefail

PVE_HOST="pve1"
CTID=111
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="$SCRIPT_DIR/.backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/meetings-${TIMESTAMP}.db"
DB_PATH="/opt/klaxxon/data/meetings.db"

mkdir -p "$BACKUP_DIR"

# --- Step 1: Dump live DB ---
echo "=== Step 1: Backing up live database ==="
if ssh "$PVE_HOST" "sudo pct exec $CTID -- test -f $DB_PATH" 2>/dev/null; then
    ssh "$PVE_HOST" "sudo pct pull $CTID $DB_PATH /tmp/klaxxon-backup.db"
    scp "$PVE_HOST":/tmp/klaxxon-backup.db "$BACKUP_FILE"
    ssh "$PVE_HOST" "rm -f /tmp/klaxxon-backup.db"
    echo "  Backed up to: $BACKUP_FILE"
else
    echo "  No existing DB found (first deploy?). Skipping backup."
    BACKUP_FILE=""
fi

# --- Step 2: Recreate LXC via deploy script ---
echo ""
echo "=== Step 2: Running deploy script ==="
"$SCRIPT_DIR/setup-api.sh"

# --- Step 3: Restore DB backup ---
if [ -n "$BACKUP_FILE" ] && [ -f "$BACKUP_FILE" ]; then
    echo ""
    echo "=== Step 3: Restoring database ==="
    scp "$BACKUP_FILE" "$PVE_HOST":/tmp/klaxxon-restore.db
    ssh "$PVE_HOST" "sudo pct push $CTID /tmp/klaxxon-restore.db $DB_PATH"
    ssh "$PVE_HOST" "sudo pct exec $CTID -- chown klaxxon:klaxxon $DB_PATH"
    ssh "$PVE_HOST" "rm -f /tmp/klaxxon-restore.db"
    echo "  Database restored."

    # --- Step 4: Restart service to pick up restored DB ---
    echo ""
    echo "=== Step 4: Restarting service ==="
    ssh "$PVE_HOST" "sudo pct exec $CTID -- systemctl restart klaxxon-api.service"
    sleep 3
    ssh "$PVE_HOST" "sudo pct exec $CTID -- systemctl status klaxxon-api.service --no-pager" || true
    echo "  Service restarted with restored data."
else
    echo ""
    echo "=== Step 3: No backup to restore (fresh deploy) ==="
fi

echo ""
echo "=== Redeploy complete ==="
