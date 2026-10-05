# Air quality for outdoor practice — backend

[![tests](https://github.com/himanshubaliyan7/air-pollution-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/himanshubaliyan7/air-pollution-backend/actions/workflows/ci.yml)

A service that tells schools in Delhi NCR whether outdoor practice is a good idea, for
each of about 80 monitoring stations: the official air-quality reading right now, and a
graded outlook (go / caution / not recommended / no data) for the next five days.

- **Live dashboard:** https://air.himanshubaliyan.dev ([how it works](https://air.himanshubaliyan.dev/about))
- **Live API:** https://air-api.himanshubaliyan.dev/api/v1/overview ([OpenAPI document](docs/openapi.json))
- **Frontend repository:** [air-clear](https://github.com/himanshubaliyan7/air-clear) (TypeScript, React, TanStack Start)

This repository holds everything behind the dashboard: ingestion, the feature and
forecast pipeline, evaluation, the API, alerting and the deployment.

## Architecture

```mermaid
flowchart LR
    subgraph Sources
        OAQ[OpenAQ<br/>hourly PM2.5 / NO2]
        CPCB[CPCB CAAQMS feed<br/>official AQI]
        ERA[ERA5 + Open-Meteo<br/>weather]
    end

    subgraph Airflow["Airflow (9 DAGs)"]
        ING[ingestion<br/>hourly]
        FEAT[features<br/>hourly]
        FC[forecast<br/>hourly]
        EVAL[evaluation<br/>nightly]
        RT[retrain / refit<br/>weekly]
        WD[watchdog<br/>hourly]
        DIG[email digest<br/>daily]
    end

    DB[(PostgreSQL<br/>+ TimescaleDB)]
    API[FastAPI]
    WEB[Dashboard<br/>Cloudflare Workers]
    TG[Telegram +<br/>healthchecks.io]
    MAIL[Subscribers]

    OAQ --> ING
    CPCB --> ING
    ERA --> ING
    ING --> DB
    DB --> FEAT --> DB
    DB --> FC --> DB
    DB --> EVAL --> DB
    DB --> RT --> DB
    DB --> API --> WEB
    WD --> TG
    EVAL --> TG
    DB --> DIG --> MAIL
```

One Oracle Cloud VM (ARM, Always Free) runs the whole stack with Docker Compose behind
Caddy (automatic TLS). It is reboot-tested: every container comes back by itself.

| Part | Where | What it does |
| --- | --- | --- |
| Ingestion | `ingestion/` | OpenAQ, the CPCB feed (data.gov.in as fallback), ERA5 and Open-Meteo; units converted at the boundary; idempotent loaders |
| Features | `features/` | Lags, rolling means and weather per station and hour |
| Forecast | `models/` | The served daily rule (`level.py`), the LightGBM family kept for comparison, grading (`daily.py`, `outlook.py`) |
| Evaluation | `models/evaluation.py` | Nightly scoring of past forecasts against measurements; drift check |
| API | `api/` | FastAPI, read-only plus double opt-in email subscriptions |
| Orchestration | `orchestration/` | Airflow DAGs, watchdog, alert callbacks |
| Alerting | `alerting/` | Daily digest email, signed unsubscribe links, data retention |
| Storage | `db/` | SQLAlchemy models, 7 Alembic migrations, hypertables |
| Deployment | `docker/` | Compose files, Caddyfile, production overrides |
| Operations | `scripts/` | Backups and restore, backfills, backtests, repair and diagnosis scripts |

## How a verdict is made

1. The expected mean of each of the next five days is the station's mean over the last
   24 hours times a ratio. The ratios (10 %, 50 % and 90 % quantiles of *day mean / last
   24-hour mean*, per day ahead) are fitted on a year of history from all stations
   together and refitted weekly.
2. A day gets the worst CPCB category its mean reaches with a probability of at least 0.4.
3. The verdict follows from the category: **go** below Poor, **caution** for Poor,
   **not recommended** from Very Poor.
4. A missing, old or incomplete forecast is **no data**. The API never returns "go"
   without a current forecast behind it, and tests hold that rule in place.

## What the backtests say

Two held-out periods of last winter (onset: 15 Oct to 30 Nov 2025; late winter: 15 Jan
to 22 Mar 2026), scored by `scripts/daily_verdict_backtest.py`:

| | Onset | Late winter |
| --- | --- | --- |
| Grade exactly right, tomorrow | 61 % | 50 % |
| Grade exactly right, five days ahead | 52 % | 38 % |
| Real not-recommended days called "go" | 2–5 % | 7–16 % |

**The served forecast is deliberately not a machine-learning model.** A LightGBM model
per station, a pooled model over all stations, and a pooled model given the target day's
*actual* weather were each backtested against "the next days look like the last 24
hours". None beat it by more than a few points, and the per-station models were worse.
The rule is as accurate, better calibrated (8 / 47 / 91 % of days fell below its 10 / 50 /
90 % quantiles) and cannot learn a sensor fault or a calendar date. Its known limit: it
follows the air about a day behind, so it cannot foresee a sudden change.

## Problems found in production, and the fixes

- **Sensor faults were training labels.** A reading of 2,938,322 µg/m³ was in the data and
  the API served a forecast of 8,243. Readings are now checked against a plausible range
  on read, for every consumer (`db/readings.py`).
- **Calendar features memorised last year.** With one year of history, "is it stubble
  season" identifies last year's dates; forecasts tripled overnight on 1 October. Found
  with a block-holdout backtest that hides a whole season.
- **A unit label was wrong upstream.** NO2 was labelled ppb but was already in µg/m³, so a
  conversion overstated it by 1.88×. An independent source (CPCB's own index) exposed it.
- **A relay outage took out the forecast input for five days.** The official index feed is
  now inverted back to concentrations as a fallback input, validated against the primary
  source (median error 1.2 µg/m³).
- **Two station lists did not match by coordinates alone.** Duplicate and misplaced
  entries put a station's current reading on one record and its forecast on another.
- **Silent skips.** A training rule skipped stations without logging a count, and 15
  stations had no model for a week. `scripts/coverage_diagnosis.py` now names the stage
  that blocks each station.
- **CORS never worked.** Compose only passes a variable a service lists explicitly; the
  setting was in `.env` but reached no container.

The full record, including what did not work, is in [docs/PROJECT_HANDOFF.md](docs/PROJECT_HANDOFF.md).

## Operations

- **Monitoring:** an hourly watchdog checks data freshness and forecast coverage and
  alerts on Telegram once per issue, with a recovery message; a dead-man's-switch ping
  catches the case where nothing runs at all.
- **Backups:** nightly `pg_dump` with 14-day rotation and an off-site copy; the restore
  was tested end to end (TimescaleDB needs more than a plain `pg_restore`).
- **Rollback:** both forecast families live side by side in the database; one config
  line (`forecast.target` in `config/settings.yaml`) switches which one is served.
- **Compliance:** licences and attribution for each data source are in
  [docs/api_compliance.md](docs/api_compliance.md).

## Run it locally

```sh
cp docker/env/.env.example docker/.env     # then fill in the keys
make up                                    # Postgres, migrations, Airflow, API
curl http://localhost:8000/api/v1/health
```

Tests need a database whose name ends in `_test`, because the integration tests truncate
every table:

```sh
pip install -e ".[ingestion,models,api,alerting,dev]" httpx
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d postgres
export TEST_DATABASE_URL=postgresql+psycopg2://USER:PASSWORD@localhost:5433/airpollution_test
python -m scripts.setup_test_db
pytest
```

233 tests (unit and integration) run on every push.

## Stack

Python 3.11, Apache Airflow 2.9, PostgreSQL 15 + TimescaleDB, FastAPI, SQLAlchemy +
Alembic, pandas, LightGBM, scikit-learn, Docker Compose, Caddy, GitHub Actions.

A non-commercial portfolio project. It is a decision aid, not medical advice.
