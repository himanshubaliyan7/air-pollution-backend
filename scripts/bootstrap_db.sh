#!/usr/bin/env bash
# One-shot local bootstrap: migrate schema, seed stations, backfill history,
# run initial feature engineering + a first training pass. Run from repo
# root with docker/.env already configured (real OPENAQ_API_KEY and
# CDS_API_KEY required - this is not runnable with placeholder credentials).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Migrating schema =="
make migrate

echo "== Seeding Delhi NCR stations from OpenAQ =="
python -m scripts.seed_stations

echo "== Backfilling history (180 days) =="
python -m scripts.backfill_history --days 180

echo "== Computing initial features =="
python -c "from orchestration.plugins.common.tasks import compute_and_write_features; print(compute_and_write_features(lookback_hours=180*24))"

echo "== Training initial models =="
python -c "from orchestration.plugins.common.tasks import retrain_all; print(retrain_all())"

echo "Bootstrap complete. Run 'make up' to start the full stack."
