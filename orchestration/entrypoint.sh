#!/usr/bin/env bash
# Writes ~/.cdsapirc from env vars before handing off to Airflow's own
# entrypoint - cdsapi.Client() only reads this file, not env vars directly
# (see ingestion/weather/era5_client.py).
set -euo pipefail

if [ -n "${CDS_API_KEY:-}" ]; then
  cat > "${HOME}/.cdsapirc" <<EOF
url: ${CDS_API_URL:-https://cds.climate.copernicus.eu/api}
key: ${CDS_API_KEY}
EOF
fi

exec /entrypoint "$@"
