#!/usr/bin/env bash
# Creates a timestamped, compressed pg_dump backup and prunes old backups
# beyond the configured retention count.
#
# Usage (via Docker Compose, recommended):
#   docker compose run --rm backup
#
# Usage (bare metal / cron on the Pi, if not using Docker for Postgres):
#   PGHOST=localhost PGUSER=iphoneflip PGPASSWORD=... PGDATABASE=iphoneflip \
#     BACKUP_DIR=/mnt/ssd/iphoneflip/backups RETENTION_COUNT=14 ./scripts/backup.sh
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_COUNT="${RETENTION_COUNT:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILENAME="iphoneflip_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[backup] Dumping database '${PGDATABASE:-iphoneflip}' from host '${PGHOST:-localhost}'..."
pg_dump --no-owner --no-privileges | gzip > "${BACKUP_DIR}/${FILENAME}"

SIZE=$(du -h "${BACKUP_DIR}/${FILENAME}" | cut -f1)
echo "[backup] Wrote ${BACKUP_DIR}/${FILENAME} (${SIZE})"

# Retention: keep only the newest RETENTION_COUNT backups.
BACKUP_COUNT=$(ls -1 "${BACKUP_DIR}"/iphoneflip_*.sql.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt "$RETENTION_COUNT" ]; then
  TO_DELETE=$((BACKUP_COUNT - RETENTION_COUNT))
  echo "[backup] Pruning ${TO_DELETE} old backup(s) beyond retention of ${RETENTION_COUNT}..."
  ls -1t "${BACKUP_DIR}"/iphoneflip_*.sql.gz | tail -n "$TO_DELETE" | xargs -r rm -v
fi

echo "[backup] Done. $(ls -1 "${BACKUP_DIR}"/iphoneflip_*.sql.gz | wc -l) backup(s) retained."
