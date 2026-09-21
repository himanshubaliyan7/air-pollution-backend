# Multi-region plan (architect review, 2026-09-21)

Produced by a read-only architect agent reviewing the code at commit `6b616ac`. **[R]** = verified by reading code/config, **[W]** = from external documentation, **[S]** = suspicion or recollection that must be checked before relying on it. Nothing here has been implemented except the first step (`config/regions.yaml`, `common/regions.py`, `GET /api/v1/regions`).

## Findings that change the plan

1. **OpenAQ budget is spent on sensor-id lookups [R].** `OpenAQSource._sensor_cache` is per instance and `tasks.py` builds a new source every run, so each hourly run repeats one `/locations/{id}` GET per station plus one `/hours` GET per sensor. Persisting sensor ids on the station row roughly halves requests and is the cheapest way to make room for another OpenAQ region.
2. **OpenAQ free tier: 60 requests/min, 2,000/hour per key; repeated violations can bring a ban [W].** Every OpenAQ-backed region and any backfill on the same key share it. `max_active_runs=1` serialises runs of one DAG only.
3. **US data availability is unproven [W].** OpenAQ's AirNow provider showed `datetimeLast = 2024-09-25` in its docs (possibly a stale example). Probe live before choosing OpenAQ for a US region; direct AirNow is a viable alternative.
4. **Units are stored but never normalised [R].** OpenAQ's AirNow provider reports NO2 in ppm; EPA NO2 breakpoints are in ppb. A US region silently breaks NO2 unless conversion happens at the ingestion boundary.
5. **Dead code/config.** `daily_exceedance` in settings.yaml is read by nothing; `models/exceedance.daily_aggregate` (hardcodes Asia/Kolkata) is only used by a test; `Settings.stations_config_path` is unused.
6. **Model feature vectors carry no column list [R].** A region with a different calendar profile could feed a booster a different column layout; equal-count reorders mispredict silently. Store `feature_names` on `model_runs` and reindex at predict time.
7. **Threshold semantics.** CPCB breakpoints are 24-hour averages but the health threshold is compared to hourly values in training, evaluation and prediction. That is an existing modelling choice and must not change for Delhi; the design needs an explicit per-pollutant `target_definition` (hourly-instant vs rolling-24h-mean). EPA PM2.5 breakpoints are 24-hour; EPA NO2 are 1-hour [W].
8. **Frontend gap.** `/regions` exposes only the category id of the health threshold, not a concentration or unit.

## Inventory of Delhi-specific assumptions (B = blocks a second region, P = needs parameterising, C = cosmetic)

| Where | Assumption | Class |
|---|---|---|
| ingestion/config.py | `DELHI_NCR_BBOX`, country ISO (duplicates regions.yaml); one global `ACTIVE_SOURCE`, registry has OpenAQ only | B |
| tasks.py, scripts | Source constructed with the OpenAQ key only; `location_last_data_times` is OpenAQ-only and not on the `SensorSource` ABC; ingestion sends every station to the one active source | B |
| ingestion/weather/*, tasks.py | `DELHI_NCR_AREA` default and passed explicitly; grid function named `delhi_ncr_grid_points` (logic generic; area derivable from bbox) | B |
| common/config.py, models/exceedance.py, predict.py, train.py, tasks.py, api, dashboard | One global thresholds path/loader (`Region.thresholds()` exists but only `/regions` uses it) | B |
| common/constants.py, common/config.py, features/time_features.py, notifier.py | `IST_OFFSET_HOURS`, `IST`, `to_ist()`; `add_cyclical` hardcodes Asia/Kolkata | B |
| settings.yaml, features/calendar | Diwali dates and stubble window produce `is_diwali_window`/`is_stubble_season`; calendar config is global | B |
| lightgbm_pipeline.py, build_features.py | Feature column set fixed by code, not recorded per model | B once profiles differ |
| orchestration/dags/*.py | All DAGs global, coupled by minute offsets (:10/:30/:45); drift check spans ALL stations with one `MIN_RECALL` | B |
| common/regions.py, config/regions.yaml | Located via `thresholds_config_path.parent`; no sensor/weather/calendar/staleness/status fields; a listed region is advertised even with no data | B |
| scripts/seed_stations.py, backfill_history.py, train_all_parallel.py, bootstrap_db.sh | Delhi constants, no `--region` flag | B |
| openaq.py | `city`/`state` hardcoded "Delhi" for every station; floor-to-hour applied unconditionally (a CPCB quirk) | B (data quality) / P |
| db/models.py | No `region_id` column; region derived per request by bbox (fragile with overlapping bboxes) | P |
| constants.py, db/models.py | `Pollutant` = pm25, no2 mirrored in six PG enum types (O3 would need six `ALTER TYPE`s) | P |
| common/constants.py, freshness.py, forecasts.py | `MAX_INPUT_STALENESS_HOURS=6` global, justified by the CPCB lag | P |
| alerting | Hardcoded "ug/m3", English copy, IST date, one global `dashboard_url` | P |
| api/routers/regions.py | Category list from PM2.5 breakpoints only | P |
| ERA5 cadence | Hourly task always asks for the last 48h (past the ~5-day edge), slides back to the same slice each run; wasteful with N regions | P |
| dashboard, docs, fixtures | Delhi/CPCB copy | C |

## Target design (summary)

- **Region config** stays in git-reviewed YAML (already mounted into every container), with optional keys defaulting to today's Delhi behaviour: `sensor` (source, query, credentials env, staleness, timestamp quirk), `weather` (ERA5 area derived from bbox, cadence), `aqi` (standard, thresholds file, per-pollutant `target_definition`), `calendar_profile` (`delhi`/`us`/`none`), `pollutants`, `alerts`, `status: draft|active` (only active regions are listed).
- **Station -> region** via a stored `stations.region_id` (backfilled by bbox once); models/forecasts/evaluations inherit region through `station_id` (no denormalisation onto hypertables). Add nullable `model_runs.feature_names`.
- **Thresholds**: region-scoped loader; versioned by `effective_from` (EPA PM2.5 breakpoints changed in 2024 [W]); normalise units to canonical µg/m³ at ingestion.
- **Calendar features**: registry of profiles; `none` returns zero columns (cyclical features already give seasonality); cyclical/calendar use the region time zone.
- **Weather**: derive the ERA5 area from the bbox; ERA5 daily per region, Open-Meteo hourly (note its free tier is non-commercial [W]).
- **SensorSource v2**: capabilities (interval, timestamp semantics, latency, rate limit, units), optional last-seen, a `FetchResult` reporting requests used and failed ids, persisted sensor ids, credential factory, exactly one sensor source per region.
- **DAGs**: generate per-region DAGs from regions.yaml (a factory), keep Delhi's existing ids unchanged, use Airflow Pools (`openaq_api`=1 slot, `cds_api`, `training`), per-region drift check. Later: Airflow Datasets instead of minute offsets.
- **API**: additive only — region `status`, per-pollutant `unit`, health-threshold concentrations, staleness, station/current-forecast counts; `unit` next to `worst_case_value`.

## Phased plan

| Phase | Work | Effort | Touches live Delhi? |
|---|---|---|---|
| 0 | Safety net: golden tests (feature vectors, forecasts, API snapshots, config-equivalence, DAG ids) | S-M | No |
| 1 | Behaviour-identical region plumbing (region-scoped thresholds/timezone/calendar/staleness/ERA5 area; keep legacy shims) | M | Code deploy only |
| 2 | Additive migration: `stations.region_id`, optional `regions` table, `model_runs.feature_names` | S | Additive |
| 3 | Source abstraction v2 (capabilities, units, factory, FetchResult, persisted sensor ids) | M | Yes (ingestion) |
| 4 | DAG fan-out (factory, pools, per-region drift, ERA5 daily) keeping Delhi ids | M | Stage carefully |
| 5 | Region-2 pilot on a separate DB/branch (source, thresholds, backfill, train, evaluate) | L | No |
| 6 | API/frontend additive fields, status gating, openapi + brief update | S-M | Additive |
| 7 | Later: Datasets scheduling, `us` calendar, alert locale, rolling-24h targets | M each | Optional |

Regression strategy: feature-vector golden (exact columns and values), golden forecasts from current artifacts, API snapshots (additive fields only), config-equivalence tests (region resolves to today's bbox/area/calendar/thresholds/+5:30), DAG id/schedule tests.

## Illustrative walkthrough: one US region via AirNow

AirNow's hourly bounding-box endpoint can supply PM2.5 and NO2 (previous hour typically available 10-30 minutes past the hour; 500 requests/hour/key) [W]; exact parameter names must be read from docs.airnowapi.org with a key before coding [S]. Historical training data for a new region is an open question (OpenAQ S3 archive, EPA AQS). Files to add: `ingestion/sources/airnow.py`, `ingestion/units.py`, `config/thresholds_epa.yaml`, `features/calendar/profiles.py`, a region-id migration, a DAG factory. Files to change: regions.yaml, common/regions.py, config.py, freshness/staleness plumbing, ingestion config/base/openaq, weather grid/clients, time/calendar features, model train/predict/registry, exceedance, tasks, scripts (`--region`), alerting, API, compose env.

## Questions for the owner

1. Which second region, and which sensor source (AirNow direct, OpenAQ, both)? Is the deployment commercial (Open-Meteo licence)?
2. For US schools, is the threshold "Unhealthy for Sensitive Groups" (PM2.5 >= 35.5)? Compare hourly forecasts to 24-hour breakpoints as Delhi does today, or use a rolling-24h target?
3. Where does historical training data for a new region come from, and how many months before going live?
4. One OpenAQ key shared across regions or a separate one for backfills?
5. Is the Streamlit dashboard being retired in favour of the Lovable frontend?
6. Should alerts be localised per region (language, units, dashboard URL)?
7. NO2 in scope where few stations measure it? O3 wanted for US schools?

## Would not do yet

One DB per region or per-region schemas; a single global model across regions; per-region horizons/quantiles/hyperparameters; renaming Delhi's DAG ids, artifact paths or station ids; a runtime region-admin UI; new pollutants; NowCast/AQI computation; fixing the "one hour per day" API day decision or the evaluation columns that count hours as days inside this effort.
