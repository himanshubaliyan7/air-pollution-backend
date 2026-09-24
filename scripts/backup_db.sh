#!/usr/bin/env bash
# Nightly Postgres backup for a production host: full pg_dump (schema+data,
# custom format) via the running postgres container, rotated locally.
#
# A FULL dump/restore is what TimescaleDB backup/restore actually supports -
# unlike a --data-only dump (which serializes hypertable rows through
# per-chunk tables under _timescaledb_internal and breaks against any target
# whose hypertables weren't created with identical chunk IDs), restoring a
# full dump into a genuinely empty database recreates the TimescaleDB
# catalog/hypertable/chunk structure from scratch consistently. Restore with:
#   docker exec -i docker-postgres-1 pg_restore -U postgres -d airpollution --clean --if-exists /path/to/dump
# against a freshly `CREATE DATABASE`'d (not Alembic-migrated) target.
#
# Run via cron on the server, e.g. daily at 03:00 local:
#   0 3 * * * BACKUP_DIR=$HOME/backups /home/ubuntu/air-pollution-backend/scripts/backup_db.sh >> $HOME/backups/backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
CONTAINER="${POSTGRES_CONTAINER:-docker-postgres-1}"
DB="${POSTGRES_DB:-airpollution}"

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
FILE="${DB}_${STAMP}.dump"

echo "== Dumping ${DB} from ${CONTAINER} =="
sudo docker exec "$CONTAINER" pg_dump -U postgres -Fc -d "$DB" -f "/tmp/${FILE}"
sudo docker cp "${CONTAINER}:/tmp/${FILE}" "${BACKUP_DIR}/${FILE}"
sudo docker exec "$CONTAINER" rm -f "/tmp/${FILE}"

echo "== Rotating backups older than ${KEEP_DAYS} days in ${BACKUP_DIR} =="
find "$BACKUP_DIR" -maxdepth 1 -name "${DB}_*.dump" -mtime "+${KEEP_DAYS}" -print -delete

echo "Backup complete: ${BACKUP_DIR}/${FILE} ($(du -h "${BACKUP_DIR}/${FILE}" | cut -f1))"
