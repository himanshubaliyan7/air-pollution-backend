# Project handoff (written 2026-09-22; rewritten 2026-09-25, end of session 5; updated 2026-09-25, end of session 6)

Read this first in a new chat. It is the complete state of the project: goal, what exists, how it was built, what went wrong, and what to do next. Auto-memory (`MEMORY.md`) holds the same facts in shorter form. **No secrets are in this file.**

## 0. START HERE: checks for the next session
The owner was away for 1-2 days after session 6 (from 2026-09-25 ~09:30 UTC). The backend was left running unattended, with nothing pending on it. Run these first, in order, before new work:
1. **Server health**: `ssh air-pollution-backend`; `sudo docker ps` (5 up: postgres, api, caddy, airflow-scheduler, airflow-webserver); `curl -s https://137-23-49-72.sslip.io/api/v1/health`; `df -h /` (39% on 2026-09-25).
2. **DAG runs since 2026-09-25**: for each DAG, `sudo docker exec docker-airflow-scheduler-1 airflow dags list-runs -d <dag> -o plain | head`. Expect hourly successes. Investigate any `failed`: Telegram should have alerted too.
3. **Weekly retrain (Sun 2026-09-27 00:00 UTC)**: first run with the full-year data and the `bfc53b1` fix. Check it succeeded, and how many models it promoted over the 2026-09-25 force-promoted set (`select count(*) from model_runs where is_active and trained_at > '2026-09-26'`).
4. **Old/new model mix gone** (fix `7268c4e`, deployed 2026-09-25 09:02 UTC): no station/pollutant should have more than one `model_id` per horizon at its latest `forecast_made_at` (query in section 6a).
5. **Re-check the verdicts** (open question, section 6 item 2): count `overall_recommendation` over all stations (loop over `/stations`, then `/forecast/{id}/exceedance?pollutant=pm25|no2`). On 2026-09-25 PM2.5 was no-go at 55 of 59 stations with a verdict, partly inflated by the mix in item 4. If near-universal no-go persists, decide with the owner: raise `exceedance_probability_decision_threshold` (config only), use only days 1-3 for the overall verdict, or keep.
6. **Nightly evaluation**: `evaluation_monitoring_dag` should pass. A drift alarm now means real exceedances were missed; it was previewed read-only for the first night and would not have fired.
7. **Backups**: `tail -3 ~/backups/backup.log`, and a new dump for each day.
8. **Current CPCB feed**: `current_aqi_dag` succeeded most hours (data.gov.in published an empty feed for one hour on 2026-09-25; that is upstream).
9. Then ask the owner whether the **domain** is ready: Phase 4 go-live (section 6 item 3).

**Session 7 results (2026-09-27 ~17:45 UTC):**
- Healthy: server (5 containers up, `/health` ok, disk 41%); backups daily through 09-27; `ingestion`, `feature_engineering`, `forecast`, `watchdog` and `station_maintenance` DAGs all succeeded; `evaluation_monitoring_dag` passed every night since the fix.
- Weekly retrain (09-27 00:00 UTC) succeeded: it promoted 1,900 of 2,052 new models. No old/new model mix.
- **CPCB upstream outage since ~2026-09-25 12:30 UTC.** Both CPCB routes stopped at the same time:
  - data.gov.in: `current_aqi_dag` has failed every run since 09-25 13:40 UTC (gateway 502/timeouts, even for a 10-row query). The last CPCB snapshot is from 09-25 12:30 UTC.
  - OpenAQ: 75 CPCB stations have had no new reading since then (OpenAQ returns 500 for their sensors). Only the 7 non-CPCB stations stay fresh.
- Effect: forecasts only for about 7 stations (PM2.5 6 no-go / 1 go; NO2 none). Everything else is correctly `no-data`. The watchdog raises `aqi-feed-stale` every hour, but has no check for forecast coverage or input staleness.
- Item 5 (verdict re-check) can't be done until CPCB data returns.
- The owner chose to leave the system running through the outage; it recovers by itself when CPCB returns.
- New watchdog issue `forecast-coverage-low` (`860631a`): fires when fewer than 30 stations have a current forecast, and the message names the cause (missing upstream inputs vs a failing pipeline). Deployed 2026-09-27 18:55 UTC; its first alert reached Telegram (confirmed by the owner). It repeats every 6 h and sends "resolved" once CPCB returns.
- Domain for the Phase 4 go-live: the owner expected it about 12 h after 2026-09-27 ~18:00 UTC. Go-live should also wait until CPCB data is flowing again.
- **Cause of the outage**: CPCB itself kept publishing; its bulletin for Sep 27 had 40/46 Delhi stations. Only the two relays broke. No announcement or ETA was found from CPCB, OpenAQ or data.gov.in.
- **Current AQI now comes straight from CPCB** (`ingestion/sources/cpcb_caaqms.py`, `https://airquality.cpcb.gov.in/caaqms/rss_feed`, no key), with data.gov.in as the fallback when CPCB fails, is empty, or is more than 6 h old.
  - Each snapshot row now records `source` and CPCB's `sub_index_hourly` (migration 0005).
  - Licence: the owner chose to rely on GODL-India; the risk is recorded in `docs/api_compliance.md`.
  - Deployed 2026-09-27 20:51 UTC (migration 0005 ran). The first run stored 469 readings for 67 matched stations from the CPCB feed. The API then served current AQI for 67 of 85 stations, as of 20:30 UTC (about 20 min old; data.gov.in used to lag 30-90 min).
- **Still open: forecasts at CPCB stations** stay no-data while OpenAQ's CPCB relay is down. Possible later step: convert the stored `sub_index_hourly` back to a concentration as a fallback model input. First find out what window that value covers and compare it with OpenAQ once OpenAQ is back.

## 1. Goal
A production-style service that tells **Delhi NCR schools whether outdoor practice is safe**, built to grow to other cities/countries later. Two signals per monitoring station: (a) **air quality right now**: official CPCB readings; (b) a **multi-day outlook**: hourly PM2.5/NO2 forecasts turned into go / caution / no-go / no-data. Hard product rule: **`no-data` (missing, stale or incomplete forecast) must never read as "go"**: a school could treat silence as clearance. The frontend is built by **Lovable** (now dormant, see section 6); the backend is ours.

## 2. What exists NOW (2026-09-25)
- **Backend repo**: `D:\Desktop\New_Project`, branch `main`, private GitHub `https://github.com/himanshubaliyan7/air-pollution-backend`. `ingestion/` (OpenAQ, ERA5, Open-Meteo, CPCB CAAQMS feed + data.gov.in fallback), `features/`, `models/` (LightGBM per station/pollutant/horizon), `api/` (FastAPI), `orchestration/` (Airflow 2.9.3 DAGs), `alerting/`, `db/` (SQLAlchemy 1.4-style + Alembic, Postgres/TimescaleDB), `config/`, `docker/` (compose + prod override + Caddyfile), `scripts/` (backup/restore/setup, backfills, backtests), `tests/` (168, all green), `docs/`.
- **LIVE deployment: Oracle Cloud (Always Free), not the owner's PC.** `https://137-23-49-72.sslip.io`. Instance `air-pollution-backend`, `ap-mumbai-1`, `VM.Standard.A1.Flex` (ARM, 4 OCPU/24GB), reserved public IP `137.23.49.72`, Ubuntu 24.04 Minimal aarch64. SSH: `ssh air-pollution-backend` (alias in `~/.ssh/config` on the owner's machine, also usable from any Claude session with that config present) or `ssh ubuntu@137.23.49.72` — the owner's own key (`~/.ssh/id_ed25519`) is authorized directly on the server, independent of any Claude session. Stack: `postgres` (TimescaleDB), `airflow-scheduler`/`airflow-webserver`, `api`, `caddy` (TLS + reverse proxy, HTTP/1.1+2 only — HTTP/3 deliberately disabled). All containers `restart: unless-stopped`/`on-failure` and **reboot-tested** to come back on their own.
- **DAGs: 8 running on schedule** (UTC): `ingestion_dag` :10, `feature_engineering_dag` :30, `current_aqi_dag` :40, `forecast_dag` :45, `watchdog_dag` :55 (Telegram + healthchecks.io, both live), `evaluation_monitoring_dag` daily 00:00, `retraining_dag` weekly (Sun 00:00), `station_maintenance_dag` daily 02:30 (station activity + subscriber-data retention purge). **`alert_digest_dag` (daily 12:30 UTC = 18:00 IST) is deployed but PAUSED** until the Phase 4 go-live.
- **Data (2026-09-25, session 6)**: 85 active stations, ~70 with a current CPCB reading, **73 with a current forecast** (was 6). History now starts **2025-10-01** (last winter backfilled from the OpenAQ S3 archive), ERA5 weather from 2025-09-25. **NO2 is stored in ug/m3** (was ppb until 2026-09-25). **All live models retrained 2026-09-25 on the last 365 days (winter included)** and force-promoted; PM2.5 models for 66 stations, NO2 for 55.
- **API contract**: `docs/openapi.json` (regenerate with `.venv/Scripts/python -m scripts.export_openapi`). `/regions`, `/stations`, `/stations/{id}/current-aqi`, `/forecast/{id}` (+`timezone`), `/forecast/{id}/exceedance`, `/forecast/{id}/history` (+`timezone`), `/attributions`, `/model-health`, `/health`, and **`/subscriptions`** (double opt-in: `POST /subscriptions`, `/confirm`, `/manage`, `/unsubscribe`, `/unsubscribe/one-click`, `/delete`; live since 2026-09-25 but dormant: no SMTP configured, no sign-up UI deployed). `CORS_ALLOWED_ORIGINS` currently allows **only** `https://himanshubaliyan7-air-clear.himanshubaliyan.workers.dev`.
- **Frontend repo**: `https://github.com/himanshubaliyan7/air-clear` (TanStack Start/Query, TS). **Phases 0-3 and 5 built, reviewed, live.** **Phase 4 (daily-email pages) built and tested, in PR #4, OPEN**; merge and deploy only at go-live (section 6 item 3). Two deployments exist:
  - **Cloudflare Workers — the live one**: `https://himanshubaliyan7-air-clear.himanshubaliyan.workers.dev`. Redeploy after any frontend change with `CLOUDFLARE_API_TOKEN=... VITE_API_BASE_URL=https://137-23-49-72.sslip.io ./scripts/deploy-cloudflare.sh` (script is in the repo; needs a fresh Cloudflare API token each time — see section 6).
  - **Lovable — dormant, not deleted**: `https://air-wise-globe.lovable.app`. Still published but CORS-blocked (owner's Lovable credits are exhausted; project kept intact for other future work). Add its origin back to `CORS_ALLOWED_ORIGINS` if it's ever used again.
- Brief for Lovable phases: `docs/lovable_frontend_prompt.md`. Compliance register: `docs/api_compliance.md`. Multi-region plan: `docs/multi_region_plan.md`.
- **Branches/worktrees**: backend PR #1 (Phase 4) was MERGED 2026-09-25; its worktree `.claude/worktrees/phase4` can be removed. The four session-3 agent worktrees under `.claude/worktrees/agent-*` are obsolete: the subscription draft was ported into Phase 4, and the rest was never merged.
- **Local Windows stack: STOPPED** (`docker compose down`, no `-v` — volumes kept on disk as an extra local copy, not actively used). Bring back with `docker compose up -d` in `docker/` if ever needed.

## 3. How it was built (chronology, condensed)
1. **Sessions 1-2**: built all phases end to end, went live on real data.
2. **Session 3**: real-time forecasting fixes, security reviews, `/regions`, agent-team work (partly cut off by spend limit).
3. **Session 4**: recovered a 3-day PC-sleep outage; OpenAQ suspension lifted, new key verified; Lovable Phase 2 reviewed.
4. **Session 5** (this one, 2026-09-24/25) — the big one: migrated the entire backend off the owner's PC onto Oracle Cloud (provisioning, Docker hardening, data migration, cutover, reboot-tested); set up backups (local + off-instance) and Telegram/healthchecks.io alerting, both verified end to end; reviewed and shipped Lovable Phases 3 and 5 (one real bug found and fixed in each — see section 5); found and fixed a real CORS-passthrough bug that meant CORS restriction had never actually worked in *any* deployment; deployed the frontend a second time to Cloudflare Workers after Lovable's credits ran out, found and fixed an HTTP/3 firewall gap and the expected CORS gap; left the Lovable site dormant rather than torn down.
5. **Session 6** (2026-09-25):
   - fixed a false nightly drift alarm, and verified the CPCB breakpoints against the official PDF (which also fixed the AQI-category gap bug);
   - traced the forecast-coverage gap to the 6 h staleness limit and raised it to 24 h (coverage 7 -> 73 stations);
   - backtested the models and fixed 36 models whose files were never migrated from the PC;
   - backfilled last winter (OpenAQ S3 archive + ERA5) and found and fixed NO2 being stored in ppb;
   - ran a winter test and retrained and force-promoted all models;
   - built Phase 4: double opt-in + daily digest + retention (backend PR #1, merged and deployed dormant) and the four frontend pages (air-clear PR #4, open). Verified end to end locally (MailHog + Chrome).
   Details are in sections 5, 6 and 6a.

## 4. Key design decisions and why
- Forecast models use hourly ug/m3 history only. The CPCB feed is **AQI sub-indices**, so it powers current conditions and is **never** fed to the models.
- Forecasts anchor on each station's newest observed hour, skipped if older than `MAX_INPUT_STALENESS_HOURS` = **24** (was 6; CPCB-via-OpenAQ data arrives ~12 h late). Current conditions (CPCB snapshots) have their own stricter `MAX_CURRENT_READING_AGE_HOURS` = 6. Stale/missing/incomplete => `no-data`, never `go`.
- **No-go rule is hourly** (owner decision 2026-09-25): a day is flagged when its forecast hour is likely above 91 ug/m3 PM2.5, stricter than CPCB's 24 h-average category. Caveat: each "day" is ONE forecast hour (anchor hour + N x 24 h, ~21:30 IST for CPCB stations), not every hour of the day - a model-design limitation to fix later (e.g. hourly horizons or daily-max models).
- Units are converted to ug/m3 at the ingestion boundary (`ingestion/units.py`); unconvertible units are dropped, not guessed.
- Frontend never computes recommendations, thresholds or units; the API supplies everything. Region-agnostic from day one.
- API Docker image is minimal (no `requests`): API modules must not import ingestion code.

## 5. Incidents and lessons (do not repeat)
- Images bake code: after any code change, `docker compose build` then `up -d --force-recreate`; `stop/start` does not re-read `docker/.env`.
- **OpenAQ suspended the account once** (rate-limit violations from overlapping runs) — resolved, new key working, `max_active_runs=1` + quota-header pacing prevents a repeat. Never create a second account/key to evade a suspension.
- **The owner's PC went dark for hours-to-days at a time** (sleep/reboot) with no one noticing — the actual reason for the Oracle Cloud migration. Resolved by moving to a real always-on server with `restart: unless-stopped` and a dead-man's-switch ping (healthchecks.io).
- **TimescaleDB dump/restore is NOT plain `pg_dump`/`pg_restore`.** A `--data-only` dump serializes hypertable rows through per-chunk tables under `_timescaledb_internal`, which won't exist by the same name/ID on an independently-created target — data silently drops or the target's Timescale catalog gets corrupted. A *full* dump into a genuinely empty (non-pre-migrated) database mostly works, but `pg_restore` still can't recreate FK constraints that live on a hypertable (`ALTER TABLE ONLY` unsupported there) — re-add those manually after restore, without `ONLY`, at the parent/hypertable level. `scripts/backup_db.sh`/`restore_db.sh` implement and this was actually verified end to end, not assumed.
- **Docker Compose only passes an env var into a container if that service's `environment:` block explicitly lists it** — having it in `docker/.env` is not enough. `CORS_ALLOWED_ORIGINS` and `DASHBOARD_URL` were missing from `docker-compose.yml` for a long time; CORS restriction silently did nothing in any deployment until this was found and fixed.
- **A CORS failure and an HTTP/3-over-a-closed-UDP-port failure look identical in a browser** — both surface as a generic `TypeError: Failed to fetch`, and curl won't reproduce either (it doesn't attempt QUIC, and a same-origin/no-origin curl request skips CORS entirely). Diagnose with `wrangler tail`/server logs (rules out server-side exceptions) and a curl request with an explicit `-H "Origin: ..."` header (rules out the server rejecting it outright) before assuming either cause.
- **Don't rely on implicit `COPY` ownership across Docker/BuildKit versions** — worked on an old local Docker Desktop, failed ("Permission denied") on a newer engine on the cloud server. Create dirs as root + explicit `chown` instead.
- **Undefined metrics must not be scored as 0.** `evaluation_monitoring_dag` failed every night after go-live with a false drift alarm ("20/20 below recall floor"): windows with no real exceedances got recall 0.0 via `zero_division=0`. Fixed in `c323309`: undefined precision/recall/F1 are now NULL and the drift check ignores them; the 28 existing rows were corrected in the live DB; a manual run passed (2026-09-25). Expect NULL recall on `/model-health` until the pollution season produces real exceedances.
- **CPCB breakpoints verified against the official source (2026-09-25)** (cpcb.gov.in "About National Air Quality Index" PDF): all PM2.5/NO2 bands match. The check found that `get_aqi_category` labelled values *between* CPCB's integer ranges (90.5, 120.5 ug/m3) as "good" - 22 live forecasts were affected (per-day label only, not the verdict). Fixed in `af7c3d6`, deployed, and every live forecast day was re-checked against the official bands with 0 mismatches.
- **Session 6 bugs (2026-09-25), all fixed and tested - don't reintroduce:** NO2 stored in ppb but compared with ug/m3 thresholds (`6693c29`); current-conditions freshness shared the forecast staleness constant, so raising it would have served day-old CPCB readings as "now" (`2adf3cf`); None-F1 crashed `promote_if_better` (weekly retrain) (`bfc53b1`); after a retrain, forecast rows from old and new models coexisted at an unchanged anchor and the outlook took the worst of both (`7268c4e`); the S3 archive stamps periods at their END and some stations publish 15-minute data (`12dd226`, verified value-for-value against API rows).
- **Forking worker processes must not inherit a pooled DB connection** ("lost synchronization with server"): `get_engine().dispose()` before `mp.Pool` and in a worker initializer. The first forced-retrain attempt crashed on this (no models written).
- **The auto-mode permission check blocks some server actions** (compose rebuilds, mass UPDATEs of production rows, killing processes). The owner runs those; give them exact commands.
- A phase report's own "type check is clean" claim was wrong once (Lovable Phase 5) — always actually run `tsc --noEmit` and the test suite myself before approving, never trust the report alone.
- Spend: launching 7 agents at once hit the owner's monthly limit once. Ask before launching teams.
- GitGuardian flagged a placeholder password once — false positive, fixed, real secrets were never in the repo. The live cloud instance's Postgres/Airflow secrets are freshly generated and were never in git or in `docker/.env` locally.

## 6. Next steps, in priority order
1. **Section 0 checks** first.
2. **New models' verdicts** (open question). After the 2026-09-25 retrain, PM2.5 was no-go at 55 of 59 stations with a verdict (26 before).
   - Partly inflated by the old/new row mix, fixed in `7268c4e` and deployed.
   - The rest is real: 51-60 stations had an hourly reading > 91 ug/m3 in the previous 36 h.
   - The winter-trained models also lean high: median forecast ~75 vs ~50 observed, day 5 ~169. On their Aug 26-Sep 25 holdout they flagged about as many hours as exceeded, but hourly recall/precision there was only 0.11-0.20.
   - Options: keep; raise the decision threshold (config); limit the overall verdict to days 1-3. Rollback: `~/active_models_before_retrain_20260925.csv` on the server lists the previous 1,960 active model ids.
3. **Phase 4 go-live on `himanshubaliyan.dev`, in DEMO MODE (owner decisions 2026-09-28).**
   - The domain is the owner's portfolio domain (DNS on Cloudflare). Projects live on subdomains and are never commercial.
   - Demo mode (`f613fcc`): only addresses in `SUBSCRIPTION_ALLOWED_EMAILS` can subscribe or get the digest. Others get the same 202, and nothing is stored or sent. `GET /subscriptions/availability` drives the frontend notice (PR #4, `32c3e21`).
   - Names:
     - frontend `air.himanshubaliyan.dev` (Workers custom domain);
     - API `air-api.himanshubaliyan.dev` (A record to `137.23.49.72`, **DNS only / grey cloud**: Caddy gets its own certificate, and Cloudflare's free certificate would not cover a deeper name);
     - mail from `alerts@air.himanshubaliyan.dev`.
   - Mail provider: any SMTP service with STARTTLS on 587 works (mailer: `smtplib` + `starttls` + login). Suggested: Resend (`smtp.resend.com`, user `resend`, password = API key); verify the domain `air.himanshubaliyan.dev` there and add its DKIM/SPF/MX records in Cloudflare as DNS only. Also add a DMARC record: `_dmarc.air` TXT `v=DMARC1; p=none`.
   - Steps (owner-run unless noted):
     1. Mail provider account, domain verified, SMTP key created.
     2. Cloudflare DNS: `air-api` A record `137.23.49.72`, proxy OFF.
     3. Server `docker/.env`:
        - `CADDY_SITE_ADDRESS=air-api.himanshubaliyan.dev, 137-23-49-72.sslip.io` (keep sslip during the switch);
        - `CORS_ALLOWED_ORIGINS=https://air.himanshubaliyan.dev,https://himanshubaliyan7-air-clear.himanshubaliyan.workers.dev`;
        - `FRONTEND_BASE_URL` and `DASHBOARD_URL` = `https://air.himanshubaliyan.dev`;
        - `PUBLIC_API_BASE_URL=https://air-api.himanshubaliyan.dev`;
        - `SMTP_HOST/PORT/USER/PASSWORD`, `ALERT_FROM_ADDRESS=alerts@air.himanshubaliyan.dev`;
        - `SUBSCRIPTION_TOKEN_SECRET` (generate once on the server, never rotate casually);
        - `SUBSCRIPTION_ALLOWED_EMAILS=<invited addresses>`.
     4. `git pull`, then recreate `api airflow-scheduler airflow-webserver caddy` (caddy for the new address; no migration).
     5. Check: `/health` on the new name; `/subscriptions/availability` says `open: false`.
     6. Merge air-clear PR #4, deploy with `VITE_API_BASE_URL=https://air-api.himanshubaliyan.dev`, then add the custom domain `air.himanshubaliyan.dev` to the Worker (Workers & Pages -> the worker -> Settings -> Domains & Routes).
     7. End-to-end test: an invited address subscribes and confirms, and gets a manage link; a non-invited address gets nothing.
     8. Unpause `alert_digest_dag` (12:30 UTC = 18:00 IST daily).
     9. Later: drop sslip from `CADDY_SITE_ADDRESS` and workers.dev from CORS.
   - **Progress 2026-09-30 (session 8):** steps 1-5 DONE and verified (`/health` ok and `open: false` on both `air-api.himanshubaliyan.dev` and sslip; CORS allows `https://air.himanshubaliyan.dev`; 85 stations served). Step 6 DONE: PR #4 merged (`25d822b`), deployed with the new API URL (Worker version `c239dab7`), custom domain `air.himanshubaliyan.dev` attached; `/subscribe` 200, CORS preflight 200. Step 7: invited address subscribed, got the confirmation email via Resend, confirmed, got the manage link (the non-invited check was not reported back). Step 8 DONE: `alert_digest_dag` unpaused by the owner 2026-09-30; first digest at the next 12:30 UTC (18:00 IST), to be confirmed. **Phase 4 is live in demo mode.** Remaining: step 9 after a few quiet days. Note: `scripts/deploy-cloudflare.sh` fails in Git Bash on Windows (line 22: the apostrophe in `backend's` inside `${VAR:?...}`); run its two commands (`npm run build`, `npx wrangler deploy --config .output/server/wrangler.json`) directly until fixed.
   - **Incident during step 3:** editing `docker/.env` in nano cut three long lines at the screen edge, leaving a literal `>` (`DATABASE_URL` ended `.../airpol>`). `db-migrate` then failed, so `api` and `caddy` never started: the site was down for roughly 15-30 min. Fixed by restoring the DB URLs from `docker/.env.backup-20260930` and rewriting CORS with `echo >>`. **Lesson:** after any `.env` edit run `grep -n '>' docker/.env | cut -d= -f1` (must print nothing) before recreating containers; prefer `sed`/`echo >>` over nano for long lines.
4. **Each "day" is one forecast hour** (section 4). Design a proper daily verdict before the peak season.
5. **If 429s from OpenAQ reappear**: persist sensor IDs to a `Station` DB column. Not needed today.
6. **Cloudflare API token**: never persisted; generate a fresh one per frontend deploy.
7. Later:
   - multi-region plan;
   - decide whether `/model-health` stays public;
   - remove obsolete worktrees and the `airpollution_{sub,qa,dba,ops}_test` databases;
   - tear down the dormant Lovable project and the local stack (optional).

## 6a. Forecast coverage + model backtest (2026-09-25)
- **Why most stations are `no-data`**: 68 of 85 stations DO have OpenAQ PM2.5 data, but it arrives about 12 h late (CPCB via OpenAQ), and `MAX_INPUT_STALENESS_HOURS` = 6 rejects it. Only 7 stations are under 6 h.
- **Backtest** (`scripts/backtest_models.py`, read-only; test window 2026-08-22..09-24, 70 stations, monsoon so few exceedances). The production rule (P(PM2.5 > 91) >= 0.3) has hourly recall 0.32 / precision 0.18 at 24 h, falling to about 0.2 at 72-120 h. It beats persistence (0.14 / 0.14) at every horizon. Per local day (any hour > 91): recall 0.16-0.31, precision 0.26-0.47. MAE is similar to persistence at 24-48 h and better at 96-120 h. A 24 h rolling mean beats it at 24-72 h.
- **Staleness costs little**: rule recall is 0.32 at 24 h vs 0.30 at 48 h, so a 12-24 h older anchor loses little skill.
- **The 0.07 recall stored in `model_runs.metrics` is misleading**: it is an unweighted average of per-station recalls, with 0.0 for stations whose holdout had no exceedances (same `zero_division` issue as the drift alarm). Pooled recall is about 0.3.
- **Threshold semantics**: CPCB's "Poor" band is a 24 h AVERAGE >= 91, but the flag fires on any HOURLY value > 91. Only 1-7 station-days in the test window had a 24 h mean > 91, vs 55-81 with an hourly spike.
- **36 active models (8 PM2.5 station-horizons at 4 stations, plus 12 classifiers) point to `D:\...` artifact paths from PC training.** FIXED 2026-09-25: the 36 files were copied into the `docker_model_artifacts` volume and their paths updated. The old paths are backed up in `~/model_paths_backup_20260925.csv` on the server. All 36 load, and `predict.forecast` works for the 4 affected stations. The 1,924 inactive rows still have `D:` paths, but they are never loaded.
- **Model-mix check query** (section 0, item 4): for each station/pollutant, take its latest `forecast_made_at` within the last 24 h and count distinct `model_id` among its 24 h-horizon rows; every count must be 1. On 2026-09-25 (before `7268c4e`) it was >1 for 43 of 61 PM2.5 stations.
- **Winter test** (`scripts/winter_eval.py`, test window 2026-01-15..03-23, 89% of station-days bad): daily no-go recall for the old Mar-Sep models fell from 0.78 (day 1) to 0.28 (day 5); models trained with winter data held 0.95-0.97 at every horizon with precision 0.90-0.92 (near the 0.89 base rate - the gain is not missing bad days). Hourly precision ~0.5 vs 0.7 for persistence; MAE worse at 3-5 days (models lean high). On that evidence all models were retrained on 365 days and force-promoted (not the usual holdout-F1 comparison, which would have judged them on a monsoon month).

## 7. Operating notes
- **Server access**: `ssh air-pollution-backend` (or `ssh ubuntu@137.23.49.72`) — owner's own key works independently of any Claude session. Repo is cloned at `~/air-pollution-backend` on the server; `git config core.fileMode false` is set there (a pulled script's `+x` bit otherwise blocks the next `git pull`). Secrets live only in the server's `docker/.env` (never committed, never printed). A read-only GitHub deploy key is registered on the backend repo for the server to `git pull` with.
- **Deploying a backend code change**: commit and push locally. Then on the server: `git pull && cd docker && sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate --build <services>`. Python code is baked into the images, so rebuild every service whose code changed: usually `api airflow-scheduler airflow-webserver`. **Add `db-migrate` whenever a new Alembic migration ships**: it has its own image, and running the old image skips the migration. Avoid :10-:17 (ingestion), :30, :40 and :45 past the hour, so running tasks aren't killed. `dashboard` and `mailhog` are deliberately left out of production.
- **Running one-off Python on the server**: `scripts/` is not in the images. Pipe a file in with `sudo docker exec -i docker-airflow-scheduler-1 python - < /tmp/x.py`. `/app` is importable, and args go after `python -`. For long jobs, wrap in `nohup sh -c "..." > ~/log 2>&1 &`. A `docker exec -i` inside a `bash -s` heredoc swallows the rest of the script's stdin: give it `< /dev/null` or its own input.
- **Local testing**: start Docker Desktop, then `docker compose up -d postgres` in `docker/` (test DB on localhost:5433, password in `docker/.env`). Afterwards, `docker compose down` (no `-v`) and quit Docker Desktop. For email tests, `docker compose up -d mailhog` (UI http://127.0.0.1:8025). Never use a bare `git stash` in this repo, because worktrees share the stash: use a WIP commit or a named stash applied by SHA.
- **Times**: the server and Airflow run on UTC; the owner is on IST (UTC+5:30). Give both when telling the owner when to act.
- **Deploying a frontend change**: in the `air-clear` repo, `CLOUDFLARE_API_TOKEN=... VITE_API_BASE_URL=https://137-23-49-72.sslip.io ./scripts/deploy-cloudflare.sh`.
- **Test DB**: `TEST_DATABASE_URL=postgresql+psycopg2://postgres:<pw>@localhost:5433/airpollution_test` (local) or the server's own Postgres on `localhost:5433` there; create with `python -m scripts.setup_test_db`; run `pytest tests`. Never point tests at the real `airpollution` DB (integration tests TRUNCATE tables).
- **Backups**: automatic, daily 03:00 UTC on the server (`scripts/backup_db.sh` via cron), local 14-day rotation + Oracle Object Storage off-instance copy. Restore with `scripts/restore_db.sh /path/to/dump.file` (destructive, asks for confirmation).
- Git Bash mangles `docker exec -w /app` into a Windows path on the owner's machine: use PowerShell for that specific case.
- Commit messages end `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`; commit before starting each new fix, not just at the end of a session.
