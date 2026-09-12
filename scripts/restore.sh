#!/usr/bin/env bash
# Restores a backup created by backup.sh onto a (fresh) database.
#
# ⚠️  This DROPS AND RECREATES data in the target database. Only run this
# against a database you intend to overwrite (e.g. right after restoring
# onto a new Raspberry Pi - see README "Restore process").
#
# Usage (via Docker Compose):
#   docker compose run --rm backup /app/scripts/restore.sh /backups/iphoneflip_20260901T120000Z.sql.gz
#
# Usage (bare metal):
#   PGHOST=localhost PGUSER=iphoneflip PGPASSWORD=... PGDATABASE=iphoneflip \
#     ./scripts/restore.sh /path/to/iphoneflip_20260901T120000Z.sql.gz
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <path-to-backup.sql.gz>" >&2
  exit 1
fi

BACKUP_FILE="$1"
if [ ! -f "$BACKUP_FILE" ]; then
  echo "[restore] Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

echo "[restore] About to restore '${BACKUP_FILE}' into database '${PGDATABASE:-iphoneflip}' on host '${PGHOST:-localhost}'."
read -r -p "This will overwrite existing data. Type 'yes' to continue: " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "[restore] Aborted."
  exit 1
fi

echo "[restore] Dropping and recreating public schema..."
psql -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

echo "[restore] Restoring from backup..."
gunzip -c "$BACKUP_FILE" | psql -v ON_ERROR_STOP=1

echo "[restore] Done. Run 'alembic upgrade head' (the bot container does this automatically on start) to apply any migrations newer than this backup."
