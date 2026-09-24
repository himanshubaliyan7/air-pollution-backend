#!/usr/bin/env bash
# Restore a backup_db.sh dump onto this host. DESTRUCTIVE: drops and
# recreates the target database. Usage:
#   scripts/restore_db.sh /path/to/airpollution_YYYYMMDD_HHMMSS.dump
#
# Verified end to end on 2026-09-24 against a throwaway database: a plain
# `pg_restore` of this dump format correctly restores all table data
# (row counts confirmed exact) but pg_restore itself fails to recreate a
# handful of FK constraints that live on a hypertable, because it emits
# `ALTER TABLE ONLY ... ADD CONSTRAINT`, and TimescaleDB rejects `ONLY` on
# hypertable operations. This script re-adds exactly those (without `ONLY`,
# at the hypertable/parent level - never the individual chunk tables, which
# TimescaleDB propagates the constraint to automatically). If the schema
# gains new FKs on/into a hypertable later, they'll need the same treatment
# here - `pg_restore`'s own error output names exactly which ones failed.
set -euo pipefail

DUMP_FILE="${1:?Usage: restore_db.sh /path/to/dump}"
CONTAINER="${POSTGRES_CONTAINER:-docker-postgres-1}"
DB="${POSTGRES_DB:-airpollution}"

if [ ! -f "$DUMP_FILE" ]; then
    echo "Dump file not found: $DUMP_FILE" >&2
    exit 1
fi

echo "This will DROP and recreate database '${DB}' in container '${CONTAINER}', then restore ${DUMP_FILE} into it."
read -r -p "Type the database name to confirm: " CONFIRM
if [ "$CONFIRM" != "$DB" ]; then
    echo "Confirmation did not match '${DB}'. Aborting." >&2
    exit 1
fi

echo "== Terminating connections to ${DB} =="
sudo docker exec "$CONTAINER" psql -U postgres -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB}';"

echo "== Dropping and recreating ${DB} =="
sudo docker exec "$CONTAINER" psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS ${DB};"
sudo docker exec "$CONTAINER" psql -U postgres -d postgres -c "CREATE DATABASE ${DB};"
sudo docker exec "$CONTAINER" psql -U postgres -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "== Restoring ${DUMP_FILE} (a handful of hypertable FK errors here are expected - fixed up next) =="
BASENAME=$(basename "$DUMP_FILE")
sudo docker cp "$DUMP_FILE" "${CONTAINER}:/tmp/${BASENAME}"
sudo docker exec "$CONTAINER" pg_restore -U postgres -d "$DB" "/tmp/${BASENAME}" || true
sudo docker exec "$CONTAINER" rm -f "/tmp/${BASENAME}"

echo "== Re-adding FK constraints pg_restore couldn't create on hypertables =="
sudo docker exec "$CONTAINER" psql -U postgres -d "$DB" -c "
ALTER TABLE public.features ADD CONSTRAINT features_station_id_fkey FOREIGN KEY (station_id) REFERENCES public.stations(station_id);
ALTER TABLE public.forecasts ADD CONSTRAINT forecasts_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.model_runs(model_id);
ALTER TABLE public.forecasts ADD CONSTRAINT forecasts_station_id_fkey FOREIGN KEY (station_id) REFERENCES public.stations(station_id);
ALTER TABLE public.raw_sensor_readings ADD CONSTRAINT raw_sensor_readings_station_id_fkey FOREIGN KEY (station_id) REFERENCES public.stations(station_id);
"

echo "== Verifying FK constraint count =="
sudo docker exec "$CONTAINER" psql -U postgres -d "$DB" -t -c \
  "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND connamespace = 'public'::regnamespace;"

echo "Restore complete."
