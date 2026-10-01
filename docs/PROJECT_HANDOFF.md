# Project handoff (written 2026-09-22; rewritten 2026-09-25, end of session 5; updated 2026-09-30, end of session 8; updated 2026-10-01, end of session 9)

Read this first in a new chat. It is the complete state of the project: goal, what exists, how it was built, what went wrong, and what to do next. Auto-memory (`MEMORY.md`) holds the same facts in shorter form. **No secrets are in this file.**

## 0. START HERE: session 10
**State at the end of session 9 (2026-10-01 17:20 UTC = 22:50 IST).** Session 9 was long: checks, coverage fixes, a globe that was built and then dropped, and a new dashboard. The details are in section 0a ("Session 9 details").

**What is live:**
- **Backend:** code up to `4b6f1ec` is deployed (station matching, 72 h feature window, `GET /api/v1/overview`). The server repo is pulled to at least `6f0e793`, because the owner ran `train_missing_models.py` from it.
- **Not deployed:** `bee3c03` (matcher prefers the feed station's own operator, `scripts/fix_station_coordinates.py`, `seed_stations.py` keeps coordinates). It needs an image rebuild: owner step 1 below.
- **Forecast coverage (public API, 17:19 UTC):** PM2.5 verdicts at **68 stations** (64 no-go, 4 go; 66 with all 5 days), was 7 at the start of the day. NO2 at **58** (55 go, 2 caution, 1 no-go), was 0. 72 stations have current AQI.
- **Frontend:** air-clear `main` = `a26c652` (PR #7 merged), deployed by the owner. https://air.himanshubaliyan.dev shows the station dashboard with an optional Leaflet map; `/globe` is gone. Verified in Chrome on the live site: dashboard, map with 83 dots, selecting a dot, home page returning to the last station, 7 API calls all 200.
- **Server health (owner, ~13:30 UTC):** 5 containers up; the 03:00 UTC backup completed (146 MB) with the off-site copy.
- Tests: backend 198 pass (the one known failure is still deselected); frontend 72 pass.

**Owner steps still to run:**
1. **Station coordinates** (server; use a :50-:58 or :20-:28 UTC window; no migration). Deploy first: the script needs the new matcher from the image.
   `cd ~/air-pollution-backend && git pull && cd docker && sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate --build api airflow-scheduler airflow-webserver && cd ..`
   `sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/fix_station_coordinates.py` (report)
   `sudo docker exec -i docker-airflow-scheduler-1 python - --apply --allow-grid-change < scripts/fix_station_coordinates.py`
   `sudo docker exec -i docker-airflow-scheduler-1 python - --days 372 --station openaq:5570 < scripts/rebuild_features.py`
   - Expected report: Pusa DPCC, Pusa IMD, North Campus and Sector-1 Noida keep their weather cell; **Aya Nagar moves from cell 28.50_77.25 to 28.50_77.00**, which is why it needs `--allow-grid-change` and the feature rebuild. Its models were trained on the old cell's weather until the Sunday retrain; it cannot forecast before about 2026-10-03 14:00 UTC anyway.
   - MD University Rohtak is left alone: CPCB's coordinates for it are 46 km away and look wrong, ours look right.
2. **Revoke the Cloudflare API token that was pasted into the session-9 chat** (dashboard -> My Profile -> API Tokens -> Roll or Delete). Not confirmed done. Later deploys need a fresh token anyway.
3. Did Telegram show `forecast-coverage-low` "resolved"? Did the 18:00 IST digest arrive with more stations?
4. **Decision B:** what "no-go" means (recommended: graded Poor / Very Poor / Severe).
5. Try the dashboard on a real phone, including tapping a dot on the map. Only a 390 px frame on desktop was checked.

**First checks in session 10:**
1. Coverage: `curl -s https://air-api.himanshubaliyan.dev/api/v1/overview` and count `outlooks[].overall_recommendation`. Compare with 68 / 58 above.
   - From about 2026-10-03 14:00 UTC the five name-matched stations (Pusa x2, Aya Nagar, North Campus, Sector-1 Noida) should get forecasts. North Campus has no PM2.5 model.
   - Alipur, Manesar, Ashok Vihar and Sector 11 Faridabad should have recovered by themselves (they needed 48 h of unbroken CPCB data).
2. Ask the owner which of the steps above were run.
3. **Check the models trained by `train_missing_models.py` before trusting them.** Their holdout is tiny (tens of rows), so their stored metrics mean little. PM2.5 is no-go at 64 of 68 stations, the same share as before the new models; compare the new stations with their neighbours.
4. One NO2 no-go and two NO2 cautions remain. The two no-go verdicts seen at 11:52 UTC were false (one wild upper-quantile model each): plan item D.
5. The weekly retrain runs Sunday 2026-10-04 00:00 UTC. It is the first one to see the rebuilt features: check how many models it promotes.
6. All API paths start with `/api/v1` (`/health` alone returns 404).

**Good next work, in the order I would take it:**
- Owner feedback on the dashboard (phone), then small fixes.
- Decision B, then item C (daily targets, robust holdout) and item D (threshold and the wild-quantile guard). These decide what the dashboard's "Next days" tile says through the winter.
- ~2026-10-03: go-live step 9 (item E).

**Plan (Phase 6 "winter readiness", target ~2026-10-18; details in section 5a):**
- **A. Close P0.**
  - Coverage is 52 PM2.5 stations (session 9). The matching fix is deployed; `train_missing_models.py --apply` should take it past 60.
  - When OpenAQ's relay delivers post-fix NO2, rerun `scripts/cpcb_subindex_study.py`: NO2 should now show ratio ~1.0 (final proof of the unit fix).
  - Watch that CPCB-filled hours don't distort the evaluation (`/model-health`).
- **B. Owner decision (P1 item 4):** what "no-go" means.
  - (a) keep one hour > 91;
  - (b) the 24 h mean >= 91 (CPCB "Poor");
  - (c) **graded verdicts Poor / Very Poor / Severe (recommended)**, so winter days stay distinguishable.
- **C. Build P1 item 5:** per-day targets (daily mean and/or daily max on the IST day, 5 horizons) instead of "one forecast hour per day". Backtest with `scripts/winter_eval.py` before promoting.
  - **Make the holdout robust at the same time:** `train.py` skips a combination when the last 30 days hold no labelled feature row, however much history exists. That left 15 stations without a PM2.5 model for a week. Options: fall back to the last N labelled rows, or train without a holdout and mark the metrics missing.
  - Gap-tolerant features were measured and are not worth it now (section 0).
- **D. P1 item 6:** recalibrate `exceedance_probability_decision_threshold` on winter data.
  - Include the false NO2 no-go verdicts found in session 9: a single wild upper-quantile model (386 ug/m3 where the station never passed 117) pushes the interpolated probability over 0.3. Consider capping a quantile forecast at a multiple of the station's observed maximum, or requiring the classifier to agree.
- **E. P2:**
  - ~Oct 3: go-live step 9 (drop sslip from `CADDY_SITE_ADDRESS` and workers.dev from CORS);
  - Telegram alert when a digest run fails or sends 0 emails;
  - cleanup: worktrees, the `airpollution_{sub,qa,dba,ops}_test` DBs;
  - investigate the pre-existing failing integration test `test_regions_endpoint_and_station_region_and_local_day` (TimescaleDB chunk CheckViolation, test DB only).

- **F. UI overhaul: a station dashboard with an optional map (owner decision 2026-10-01; replaces the globe).**
  - **History:** the owner first asked for a "Google Earth"-style globe. A MapLibre + deck.gl prototype was built, merged (air-clear PR #6) and deployed on 2026-10-01. The owner then rejected it: it worked on desktop, the phone layout had problems, and it was too heavy (about 1.5 MB of JavaScript). **Do not bring the globe back.**
  - **What the owner wants:** something like a modern weather dashboard, with a map the visitor can open and use if they want.
  - **Built 2026-10-01: air-clear PR #7 (`feat/dashboard`), MERGED (`a26c652`) and DEPLOYED the same day; verified on the live site.**
    - The station page (`/r/$regionId/s/$stationId`) is a grid of tiles: right now, next days, pollutants, last 48 hours, nearby stations, map. One column on a phone.
    - The map is Leaflet with OpenStreetMap tiles, loaded only when opened (about 43 kB gzipped). Its view is limited to the region, so no international border is ever in view.
    - The home page returns a visitor to the station they opened last (localStorage).
    - `/globe`, `maplibre-gl` and `deck.gl` are removed (`/globe` returns 404 on the live site).
    - Checked in Chrome at desktop width and at 390 px (in an iframe). Not checked on a real phone.
    - Two signals can disagree on screen ("Satisfactory" right now, "Not recommended" for the next days). Both are correct; watch for owner feedback that it confuses.
    - OpenStreetMap's public tile server is for light use: fine for a demo, a tile provider is needed if traffic grows.
    - Deploying: in `D:\Desktop\air-clear`, `git checkout -- src/routeTree.gen.ts` before every `git pull` (each build rewrites that generated file), then `npm install` and the deploy script.
  - Backend: `GET /api/v1/overview` (`4b6f1ec`, deployed) feeds the nearby list and the map.
  - Ideas kept for later, from the owner's research round: a city overview page (counts per category, best and worst stations), plain guidance text per category in the style of the US EPA school flag program (it would have to come from the API, since the frontend never names a category), a calendar heat map.
  - Five stations sit 1-6.5 km from their real place, because OpenAQ's coordinates are wrong (Pusa x2, Aya Nagar, North Campus, Sector-1 Noida). `scripts/fix_station_coordinates.py` corrects them from the CPCB feed (owner step 3 in section 0). They affect the nearby list and the map.
  - Local run: `DEV_API_PROXY=https://air-api.himanshubaliyan.dev VITE_API_BASE_URL=http://localhost:5199 npx vite dev --port 5199`.
  - `bun.lock` is not updated (no bun on the owner's PC); `package-lock.json` must never be committed. The owner's own clone is `D:\Desktopir-clear`; deploy from there (section 7).

## 0a. Earlier session results (kept for history)
### Session 9 details (2026-10-01)
**Session 9 check results (public API, 2026-10-01 11:52 UTC):**
- **The CPCB fallback works.** PM2.5 verdicts at **52 stations** (48 no-go, 4 go), up from 7. NO2 at **43** (41 go, 2 no-go), up from 0. Most forecasts were made from an hour less than 3 h old. API `/health` ok; 67 stations have current AQI.
- **Below the 60+ target. Causes of the 33 PM2.5 no-data stations:**
  - **15: live CPCB PM2.5 but no PM2.5 model** (e.g. Mandir Marg, Punjabi Bagh, Patparganj, Lodhi Road, Najafgarh). Most of them do have NO2 forecasts. Cause found later the same day: see "What the diagnosis showed" below.
  - **9 entries (7 CPCB stations): the CPCB feed was matched to the wrong station or to none.** FIXED in `037bd38`.
    - Anand Vihar and ITO have two OpenAQ entries each on one point. CPCB data went to the dead duplicate (`openaq:5509`, `openaq:10489`), which has no models; the live entries (`openaq:235`, `openaq:5613`) got nothing.
    - Pusa DPCC, Pusa IMD, Aya Nagar, North Campus and Sector-1 Noida matched nothing: OpenAQ places them 1.2-6.5 km from CPCB's coordinates.
  - **4: model exists, PM2.5 data is fresh, still no forecast** (Ashok Vihar, Alipur, Sector 11 Faridabad, Manesar). Long CPCB outages, see below.
  - 1: Indirapuram has a model for the 24 h horizon only.
  - 4: no CPCB station and no fresh OpenAQ data (MD University Rohtak, New Industrial Town Faridabad, "New Delhi", Ved Vihar-Loni).
- **The two NO2 no-go verdicts are false** (Sanjay Nagar Ghaziabad, NSIT Dwarka). One horizon's upper-quantile model predicts 386 / 292 ug/m3 where the 90-day observed maximum is 117 / 178 and nothing ever exceeded 181. Probability 0.37 / 0.31, just over the 0.3 threshold. Not fixed: it belongs to plan item D.
- **Not checked (auto mode blocks ssh reads of production):** DAG runs, backups, the watchdog's "resolved" message, the 18:00 IST digest. All 5 containers started cleanly at the deploy.

**Built in session 9:**
- `037bd38` **Station matching** (`ingestion/loaders/aqi_snapshot_loader.py::match_station`): among several stations within 250 m the one with the newest OpenAQ reading wins; with none within 250 m, the same site name and operator within 10 km matches (IITM = IMD). Checked against the live feed: 66 matches unchanged, 7 changed, no two feed stations on one of ours.
- `c4c079b` **Feature refresh window 6 h -> 72 h.** The hourly feature run rewrote only the last 6 hours, but readings arrive later than that. Those hours stayed all-NaN in `features`, and training reads only that table, so they dropped out of every retrain since the last manual rebuild.
- `3c03114`, `6f0e793` **Owner-run scripts:** `merge_duplicate_station.py`, `rebuild_features.py`, `coverage_diagnosis.py` (read-only), `train_missing_models.py`.
- `4b6f1ec` **`GET /api/v1/overview`**: every active station's current AQI and both outlooks in one response (4 queries; `Cache-Control: public, max-age=120`). The per-station endpoints' own code produces each part. This is the backend half of plan item F.
- Tests: 196 pass (the one known failure is still deselected).

**Done on the server on 2026-10-01 (owner):**
- Code deployed ~12:45 UTC; `GET /api/v1/overview` answers (83 stations, 181 KB, about 2 s).
- Duplicates merged: `openaq:5509` -> `openaq:235` (808 snapshots moved, 175 CPCB readings) and `openaq:10489` -> `openaq:5613` (810 and 173). Both dead entries are inactive.
- `rebuild_features.py --days 14`: 53,246 rows.
- `coverage_diagnosis.py` run at 12:52 UTC. Result below.

**What the diagnosis showed (PM2.5: 45 complete, 8 with some horizons, 15 without a model, 6 with a model but no fresh forecast, 9 stale):**
- **The 15 stations without a PM2.5 model have plenty of history** (3,700-6,500 complete feature rows) **but only 95-170 in the holdout** (the last 30 days). Their data resumed on 2026-09-21/22 after the long upstream outage. At the 09-25 retrain the holdout therefore held no feature row with a known label for the longer horizons (a 24 h label at best), and `train.py` skips a combination whose holdout is empty. The 8 stations with a `[24]`-only PM2.5 model and the 16 with partial NO2 models are the same effect. Every retrain since then saw all-NaN feature rows (the 6 h window), so nothing healed.
- The feature rebuild has now filled the holdout. **Next step: `scripts/train_missing_models.py`** (below). It trains only combinations without an active model.
- **The "model but no fresh forecast" stations are not short gaps.** Alipur, Manesar, Ashok Vihar and Sector 11 Faridabad had CPCB outages of 16-20 h or came back less than 48 h ago; a forecast needs every lag up to 48 h. They recover by themselves. Measured on the public history: filling gaps of up to 2 h would raise "newest hour usable" from 56 to 63 of 74 stations, but a forecast up to 24 h old is served anyway and the median station already has 22 of 24 usable hours. So gap-tolerant features are not worth a model-input change now.
- The five name-matched stations (Pusa x2, Aya Nagar, North Campus, Sector-1 Noida) still showed 0 CPCB readings at 12:52: the first `current_aqi_dag` run with the new matcher was 13:40 UTC. They need about 48 h of readings before they can forecast.
- NO2 at 8 stations is "no readings": private sensors that only measure PM2.5. Expected.

### Earlier sessions
**Session 8 end state (2026-09-30 ~21:00 UTC):** Phase 4 live in demo mode (frontend https://air.himanshubaliyan.dev, API https://air-api.himanshubaliyan.dev; digest daily 12:30 UTC = 18:00 IST). NO2 unit bug fixed (data, code, models; section 5a). CPCB fallback input deployed (`03be158`, migration 0006; backfill of 9,341 readings since 09-27). OpenAQ's CPCB relay stalled again from 09-29 13:00 UTC.

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
- **API contract**: `docs/openapi.json` (regenerate with `.venv/Scripts/python -m scripts.export_openapi`). `/regions`, `/stations`, `/stations/{id}/current-aqi`, `/forecast/{id}` (+`timezone`), `/forecast/{id}/exceedance`, `/forecast/{id}/history` (+`timezone`), `/attributions`, `/model-health`, `/health`, `/overview` (all stations in one response, since session 9), and **`/subscriptions`** (double opt-in: `POST /subscriptions`, `/confirm`, `/manage`, `/unsubscribe`, `/unsubscribe/one-click`, `/delete`; live since 2026-09-25 but dormant: no SMTP configured, no sign-up UI deployed). `CORS_ALLOWED_ORIGINS` currently allows **only** `https://himanshubaliyan7-air-clear.himanshubaliyan.workers.dev`.
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
6. **Session 7** (2026-09-27): rode out a CPCB relay outage; added the `forecast-coverage-low` watchdog issue; current AQI switched to CPCB's own CAAQMS feed (data.gov.in as fallback).
7. **Session 8** (2026-09-30/10-01):
   - Phase 4 go-live on `himanshubaliyan.dev` in demo mode;
   - planned Phase 6 (section 5a); found OpenAQ's relay is intermittent, and backfilled the 09-25/26 gap;
   - proved CPCB's hourly sub-index = the hourly concentration;
   - found and fixed the NO2 unit bug (data, code, models);
   - built and deployed the CPCB fallback input.
8. **Session 9** (2026-10-01): ran the checks; fixed the CPCB station matching and the 6 h feature refresh window; found why 15 stations had no PM2.5 model and trained the missing models (PM2.5 coverage 7 -> 68, NO2 0 -> 58); built `GET /overview`; built, deployed and then dropped a 3D globe; replaced it with a station dashboard and an optional Leaflet map (live); wrote the coordinate fix (not yet run).

## 4. Key design decisions and why
- Forecast models use hourly ug/m3 history only. The CPCB feed is **AQI sub-indices**. Until 2026-10-01 it powered current conditions only.
  - **Since Phase 6 P0 step 3:** its `Hourly_sub_index` for PM2.5/NO2 is inverted through the CPCB breakpoints and stored as readings with source `CPCB` (`ingestion/loaders/cpcb_reading_loader.py`, written by `current_aqi_dag`), stamped lastupdate - 1.5 h.
  - Validated against OpenAQ (`scripts/cpcb_subindex_study.py`: PM2.5 medAE 1.2 ug/m3, ratio 1.00).
  - `db/readings.py` makes OpenAQ win any hour both hold, for every consumer (features, training targets, evaluation, API history). So CPCB only fills gaps, e.g. while OpenAQ's relay stalls.
  - The Avg/Min/Max fields (24 h statistics) are still never used as model input.
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
- **Don't trust a provider's unit label; check it against an independent source.** OpenAQ labels CPCB NO2 "ppb", but the values are ug/m3. The 2026-09-25 "fix" converted them and overstated NO2 by 1.88x for 5 days. CPCB's own AQI feed exposed it (ratio 0.531 = 1/1.882). Before changing units, compare against a second source.
- **Agree on the look before building an expensive visual feature.** The 3D globe took hours to build and deploy, and the owner rejected it the same day as too heavy. A short round of research and a sketch of three options settled the direction in minutes.
- **Never have the owner paste a deploy token into the chat.** A Cloudflare token ended up in the session transcript and had to be revoked. Deploys run in the owner's own terminal.
- **Matching two station lists by nearest coordinates alone is not enough.** OpenAQ lists some CPCB sites twice (the dead entry can be the nearer one) and misplaces others by kilometres. For 4 days Anand Vihar's current AQI sat on one entry and its forecasts on another. When a new data source is matched to stations, list every station that matched nothing and every point with two candidates, and look at them.
- **A training rule that skips silently needs a visible count.** `train.py` skipped every combination with an empty holdout and only logged a warning; 15 stations had no PM2.5 model for a week and it looked like "no data upstream". `scripts/coverage_diagnosis.py` now names the blocking stage per station; run it whenever coverage is below expectation.
- **A table that training reads must be refreshed over the same window as its inputs.** Ingestion re-read 72 h, features only 6 h, so late readings never became training rows. Nothing failed: the retrain just saw less data each week.
- **Editing long `docker/.env` lines in nano can truncate them to a literal `>`** (took the site down 2026-09-30). After any edit: `grep -n '>' docker/.env | cut -d= -f1` must print nothing.
- **The auto-mode permission check blocks some server actions** (compose rebuilds, mass UPDATEs of production rows, killing processes). The owner runs those; give them exact commands.
- A phase report's own "type check is clean" claim was wrong once (Lovable Phase 5) — always actually run `tsc --noEmit` and the test suite myself before approving, never trust the report alone.
- Spend: launching 7 agents at once hit the owner's monthly limit once. Ask before launching teams.
- GitGuardian flagged a placeholder password once — false positive, fixed, real secrets were never in the repo. The live cloud instance's Postgres/Airflow secrets are freshly generated and were never in git or in `docker/.env` locally.

## 5a. NEXT PHASE: Phase 6 "winter readiness" (planned 2026-09-30, session 8)
Delhi's pollution season starts mid-October (stubble burning) and peaks in November, which is when the service matters. Target: all of P0 and P1 deployed by **~2026-10-18**.

**Status on 2026-09-30 ~15:30 UTC (public API):** current AQI fresh at 67/85 stations (CPCB direct feed works). **Forecasts only at 7/85** (PM2.5: 6 no-go, 1 go; 78 no-data): OpenAQ's CPCB relay has been down since 2026-09-25, 5 days. The one point of failure is now the forecast input.

**P0: forecasts must not depend on one relay** (week of Oct 1)
1. *Cheap check first:* are the CPCB sensors really gone from OpenAQ, or were they re-created under new location/sensor IDs? (A 5-day relay outage could be an ID migration.) If new IDs, remap them: `station_maintenance_dag` / the sensor lookup.
   **RESULT (2026-09-30 ~16:00 UTC): no ID migration.**
   - All 85 of our location IDs still exist in OpenAQ, and no new CPCB locations appeared in the bbox.
   - The relay is **intermittent, not dead**. It came back, backfilled 09-25 .. 09-29 13:00 UTC (for example loc 235: 22-24 h/day), then stalled again. 71 stations have their last OpenAQ hour at 09-29 14:00 UTC.
   - Our ingestion stored data up to 09-29 13:00 for 69 stations. 46 stations got forecasts made at 09-29 13:00 UTC; they expired about 24 h later (the staleness limit), so coverage flickered on and off unnoticed.
   - **Data gap found:** our DB has 0 h on 09-25 and ~1-4 h on 09-26 for most CPCB stations, although OpenAQ now has those hours. The relay backfilled them after they had left `SENSOR_LOOKBACK_HOURS` = 72 (`orchestration/plugins/common/tasks.py`). Fix: one-off `python -m scripts.backfill_history --days 7 --skip-weather` on the server (owner-run). Low impact on forecasts; it matters for evaluation and training completeness.
   - Conclusion: step 2 (a fallback that doesn't depend on OpenAQ) is still needed.
   - Backfill run 2026-09-30 18:08 UTC (owner): 12,467 readings. 09-25/26 PM2.5 is now full at 41 stations, partial at 27, empty at 14 (OpenAQ doesn't have those hours either).
2. **RESULT of `scripts/cpcb_subindex_study.py` (2026-10-01, 09-27..09-29 overlap, ~2,500 pairs per test):**
   - **PM2.5 `Hourly_sub_index` IS the hourly concentration.** Inverted, it matches the OpenAQ hour whose `observed_at` = lastupdate - 1.5 h: medAE 1.2 ug/m3, r 0.95, 100% agreement on > 91. Every other offset and the 24 h mean match much worse. Avg/Min/Max = the 24 h mean/min/max (medAE 0.3-2.3, r 0.97-0.99).
   - Caveat: a monsoon window (1% of hours > 91, none at the 500 cap), so the upper bands are not yet validated on real data.
   - **NO2: suspected unit bug.** r = 0.97-0.99, but CPCB is ~19 ug/m3 lower, and the gap scales with the value. OpenAQ labels the CPCB NO2 sensors (e.g. 12235607, 12234784, 12234793) as **ppb**, and we store value x 1.882 (verified: stored/raw = 1.882 exactly). If CPCB = OpenAQ's raw number, the "ppb" label is wrong: the values are already ug/m3, and the 2026-09-25 conversion (`6693c29`) makes stored NO2 ~1.88x too high at CPCB stations. **CONFIRMED 2026-10-01:** median CPCB/stored ratio **0.531** (IQR 0.512-0.549) in every NO2 comparison, = 1/1.882, while PM2.5 is 1.000 (IQR 0.943-1.046).
     - A cross-year check (old ug/m3-labelled sensors 2016-18 vs the new 'ppb' ones 2025+, same season) was inconclusive: 1 station supports the raw value, 2 the converted one, and they never overlapped in time.
     - Deciding argument: the product promises the CPCB AQI scale, so our NO2 must match what CPCB publishes, and it is 1.88x higher.
   - **NO2 fix plan (awaiting owner go-ahead; do not run piecemeal):**
     a. *Code:* stop converting NO2 labelled 'ppb' from OpenAQ's CPCB-provider sensors (keep the conversion for genuine ppb providers, for multi-region). Add tests.
     b. *Data (owner-run, backup first):* divide by 1.8816 the NO2 rows that came from the 'ppb'-labelled CPCB sensors. Identify them by the sensor id in `source_record_id` (live API rows are `"{sensor_id}:{datetime_from}"`; check the archive-backfill format first). This includes the one-off UPDATE done with `6693c29`.
     c. Recompute NO2 features, retrain and force-promote the NO2 models (55 stations), regenerate forecasts.
     d. Verify: rerun `cpcb_subindex_study.py`; the NO2 ratio should be ~1.0.
     - Why before winter: Delhi winter hourly NO2 of 100-150 ug/m3 is stored as 190-280, above the 181 "poor" line, which gives false NO2 no-go verdicts.
   - **NO2 fix progress (2026-10-01):**
     - a. DONE: code `85ed394` (`ingestion.sources.openaq.declared_unit`) deployed (api, scheduler, webserver rebuilt) with `ingestion_dag` paused.
     - b. DONE: `scripts/fix_no2_units.py --step data` divided 424,408 NO2 rows (75 stations, 75 'ppb' sensors; 0 other NO2 rows) by 1.8816. Probe: 1.882 before, 1.0 after. `ingestion_dag` unpaused.
     - c. DONE (finished 2026-09-30 20:08 UTC): features rebuilt, and **1,312 NO2 models activated** (328 station/horizon combos x 4), 97 combos skipped for too little data (none had an active model), 0 failed, 0 old models left active. Holdout f1 = 0.000 because there are no NO2 exceedances of 181 in the monsoon holdout, which is expected (undefined, not bad). The old active ids are the `ROLLBACK ...` lines in `~/no2_rebuild_20261001.log` on the server.
     - d. TODO: rerun `cpcb_subindex_study.py` once OpenAQ has post-fix NO2 overlapping CPCB snapshots; check NO2 verdicts.
   - A possible bridge: raise `MAX_INPUT_STALENESS_HOURS` 24 -> 36/48 (per 6a, skill loss from 24 h to 48 h is small: 0.32 vs 0.30 recall). But the models only reach 120 h, so an older anchor loses days 4-5; check how the API marks those days before changing it.
2. *CPCB fallback input:* invert the stored `sub_index_hourly` (migration 0005, collected since 2026-09-27) back to ug/m3 with the official CPCB breakpoints, which are piecewise linear and so invertible within each band. Open questions to settle before use:
   - which window `Hourly_sub_index` covers (1 h or a rolling 24 h);
   - integer rounding loss per band;
   - the cap at 500 (PM2.5 > 380 ug/m3 is clipped: it still flags no-go correctly, but it is a biased model input).
   Validate against CPCB's daily bulletin (24 h means) now, and against OpenAQ values once the relay returns.
   - **Step 3 BUILT 2026-10-01 (`03be158`), see section 4.** 181 tests pass. The one failure, `test_station_activity.py::test_regions_endpoint_and_station_region_and_local_day` (TimescaleDB chunk CheckViolation inserting a forecast row in the test DB), fails the same way on the previous commit: pre-existing and test-only, to investigate. Deploy: rebuild with `db-migrate` (migration 0006), then `scripts/backfill_cpcb_readings.py --since 2026-09-27`.
3. If it validates: use it as a model input *only when OpenAQ has nothing fresher*, record the source per row, and keep every staleness rule. Section 4's rule "CPCB feed never feeds the models" changes only for inverted hourly values that pass validation. Success: forecast coverage >= 60 stations without OpenAQ.

**P1: verdict quality before the peak** (weeks of Oct 5 and Oct 12)
4. *Owner decision needed:* what does "no-go" mean?
   - Today it means one forecast hour > 91 ug/m3.
   - CPCB's "Poor" band is a 24 h mean >= 91.
   - In last winter's test, 89% of station-days were bad, so either definition says "no-go" almost every day in Nov.
   - Options: (a) keep hourly; (b) switch to the 24 h mean (matches CPCB); (c) graded verdicts (Poor / Very Poor / Severe) so winter days are still told apart.
5. *Daily verdict redesign (section 4 caveat):* replace "one forecast hour per day" with per-day targets: daily-mean and/or daily-max models, 5 horizons, on the local (IST) day. This is cheaper than hourly horizons (85 x 2 x 120 models). Backtest on the winter window with `scripts/winter_eval.py` before promoting.
6. Re-check the no-go share and probability calibration once coverage is back. Then set `exceedance_probability_decision_threshold` from the winter reliability curve, not the monsoon one.

**P2: operations**
7. Go-live step 9 (~Oct 3): drop sslip from `CADDY_SITE_ADDRESS` and workers.dev from CORS.
8. Alert when a digest run fails or sends 0 emails while there are confirmed subscribers (Telegram).
9. Cleanup: obsolete worktrees, `airpollution_{sub,qa,dba,ops}_test` DBs, air-clear PR #5 (deploy-script fix) merge.

**Later (Phase 7):** multi-region (`docs/multi_region_plan.md`); decide whether `/model-health` stays public; demo mode stays (the project is non-commercial).

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
   - **Progress 2026-09-30 (session 8):** steps 1-5 DONE and verified (`/health` ok and `open: false` on both `air-api.himanshubaliyan.dev` and sslip; CORS allows `https://air.himanshubaliyan.dev`; 85 stations served). Step 6 DONE: PR #4 merged (`25d822b`), deployed with the new API URL (Worker version `c239dab7`), custom domain `air.himanshubaliyan.dev` attached; `/subscribe` 200, CORS preflight 200. Step 7: invited address subscribed, got the confirmation email via Resend, confirmed, got the manage link and a non-invited address got no email (demo mode verified). Step 8 DONE: `alert_digest_dag` unpaused by the owner 2026-09-30; first digest arrived at ~15:14 UTC (20:44 IST) the same day, because with `catchup=False` Airflow still runs the latest missed interval on unpause (expected, one-off); from then on it runs at 12:30 UTC (18:00 IST). **Phase 4 is live in demo mode.** Remaining: step 9 after a few quiet days. Note: `scripts/deploy-cloudflare.sh` fails in Git Bash on Windows (line 22: the apostrophe in `backend's` inside `${VAR:?...}`); run its two commands (`npm run build`, `npx wrangler deploy --config .output/server/wrangler.json`) directly until fixed.
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
- **After any backfill, merge or data repair**, run `scripts/rebuild_features.py --days N` for the affected window: the hourly run only rewrites the last 72 h.
- **Coverage check from any machine** (no ssh): `GET /api/v1/overview` and count `outlooks[].overall_recommendation`. `scripts/coverage_diagnosis.py` (on the server, read-only) names the stage that blocks each station.
- **Times**: the server and Airflow run on UTC; the owner is on IST (UTC+5:30). Give both when telling the owner when to act.
- **Deploying a frontend change**: in the `air-clear` repo, `CLOUDFLARE_API_TOKEN=... VITE_API_BASE_URL=https://137-23-49-72.sslip.io ./scripts/deploy-cloudflare.sh`.
- **Test DB**: `TEST_DATABASE_URL=postgresql+psycopg2://postgres:<pw>@localhost:5433/airpollution_test` (local) or the server's own Postgres on `localhost:5433` there; create with `python -m scripts.setup_test_db`; run `pytest tests`. Never point tests at the real `airpollution` DB (integration tests TRUNCATE tables).
- **Backups**: automatic, daily 03:00 UTC on the server (`scripts/backup_db.sh` via cron), local 14-day rotation + Oracle Object Storage off-instance copy. Restore with `scripts/restore_db.sh /path/to/dump.file` (destructive, asks for confirmation).
- Git Bash mangles `docker exec -w /app` into a Windows path on the owner's machine: use PowerShell for that specific case.
- Commit messages end with the `Co-Authored-By: Claude ...` line of the model in use; commit before starting each new fix, not just at the end of a session.
