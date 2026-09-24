#!/usr/bin/env bash
# Nightly Postgres backup for a production host: full pg_dump (schema+data,
# custom format) via the running postgres container, rotated locally.
#
# A FULL dump (not --data-only) is what actually works against TimescaleDB:
# --data-only serializes hypertable rows through per-chunk tables under
# _timescaledb_internal, which breaks against any target whose hypertables
# weren't created with identical chunk IDs (hit this migrating data onto the
# OCI instance - see PROJECT_HANDOFF.md). A full dump restores the data
# correctly, but pg_restore still can't recreate 4-5 FK constraints that
# reference or live on a hypertable (`ALTER TABLE ONLY ... ADD CONSTRAINT`
# isn't supported there) - verified by an actual restore-to-a-throwaway-DB
# test on 2026-09-24: all data landed with correct row counts, restore
# logged "errors ignored: 5" for exactly those FK constraints, and manually
# re-running them WITHOUT `ONLY` at the hypertable/parent level (not the
# individual chunk table) fixed all of them cleanly. See restore_db.sh,
# which automates the full procedure and has this fixup built in - don't
# hand-restore from this dump format without it.
#
# Optionally also pushes the dump off-instance via rclone (RCLONE_REMOTE,
# e.g. "oci:air-pollution-backups") so a lost/corrupted VM doesn't take the
# backups with it - set up once with `rclone config` (Oracle Object Storage's
# S3-compatible endpoint needs `provider = Other`, `region = <region>` and
# `force_path_style = true` in the rclone remote - without force_path_style
# it fails auth with a misleading "SignatureDoesNotMatch" error, confirmed
# 2026-09-24).
#
# Run via cron on the server, e.g. daily at 03:00 local:
#   0 3 * * * BACKUP_DIR=$HOME/backups RCLONE_REMOTE=oci:air-pollution-backups /home/ubuntu/air-pollution-backend/scripts/backup_db.sh >> $HOME/backups/backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
CONTAINER="${POSTGRES_CONTAINER:-docker-postgres-1}"
DB="${POSTGRES_DB:-airpollution}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
FILE="${DB}_${STAMP}.dump"

echo "== Dumping ${DB} from ${CONTAINER} =="
sudo docker exec "$CONTAINER" pg_dump -U postgres -Fc -d "$DB" -f "/tmp/${FILE}"
sudo docker cp "${CONTAINER}:/tmp/${FILE}" "${BACKUP_DIR}/${FILE}"
sudo docker exec "$CONTAINER" rm -f "/tmp/${FILE}"

echo "== Rotating backups older than ${KEEP_DAYS} days in ${BACKUP_DIR} =="
find "$BACKUP_DIR" -maxdepth 1 -name "${DB}_*.dump" -mtime "+${KEEP_DAYS}" -print -delete

if [ -n "$RCLONE_REMOTE" ]; then
    echo "== Uploading to ${RCLONE_REMOTE} =="
    rclone copy "${BACKUP_DIR}/${FILE}" "${RCLONE_REMOTE}/"
    echo "== Pruning remote backups older than ${KEEP_DAYS} days =="
    rclone delete "${RCLONE_REMOTE}/" --min-age "${KEEP_DAYS}d"
fi

echo "Backup complete: ${BACKUP_DIR}/${FILE} ($(du -h "${BACKUP_DIR}/${FILE}" | cut -f1))"
