# Prompt for Lovable: frontend for the Delhi NCR air-quality forecast API

Attach with this prompt: `docs/openapi.json` (the API contract) and your own design/brand material.

---

You are building the **frontend only** for an existing, already-running backend: an hourly air-quality forecast service for Delhi NCR that helps schools decide whether outdoor practice is safe. Read this whole brief before writing code.

## Ground rules

1. **Do not build or change any backend.** The API exists. The attached `openapi.json` is the single source of truth for every endpoint, parameter and response shape. Generate typed models and one API client module from it. All network calls go through that one module, so the base URL lives in a single place.
2. **Base URL comes from an environment variable** (`VITE_API_BASE_URL`), no trailing slash. All routes are under `/api/v1`. Never hardcode a host. There is no authentication.
3. **Do not invent endpoints, fields, or data.** No mock data in finished screens. If you need something the API does not provide, do not fake or work around it: add an entry to a `BACKEND_REQUESTS.md` in the repo (what you need, which screen needs it, why) and continue with what exists.
4. **Do not compute forecasts, thresholds, AQI categories or recommendations in the frontend.** The server already decides them. Render what it returns.
5. **Design is provided separately by me.** Do not invent a visual identity, palette, typography, logo or imagery. Until I supply material, use plain, unstyled, accessible defaults and keep all presentation in isolated components/tokens so my design can be applied without restructuring. Start each phase below by checking whether design material has been supplied for it.
6. Keep dependencies small. Use a data-fetching/cache library (e.g. TanStack Query), TypeScript, and a router.

## What the data means (must be respected exactly)

- **Pollutants:** `pm25` and `no2`, units µg/m³. The pollutant is a query parameter on most endpoints, default `pm25`.
- **Station ids contain a colon** (e.g. `openaq:8118`). URL-encode them in paths.
- **Most stations have no current forecast, and this is normal.** The upstream sensor feed is delayed for most of the network, so only a handful of stations (currently about 7 of ~83) can be forecast at any time. `GET /stations` returns `has_current_forecast` and `latest_observed_at` for every station, with current-forecast stations sorted first. The UI must make this state clear, and must let a user still browse other stations without ever presenting them as "safe".
- **`overall_recommendation`** (from `/forecast/{station_id}/exceedance`) is one of exactly: `go`, `caution`, `no-go`, `no-data`.
  - **`no-data` means there is no usable forecast. It must never be shown, coloured, worded or grouped as "go", nor as a safe/positive state.** This is the most important rule in the whole app: a school may treat silence as clearance.
  - Handle an unknown future value defensively by treating it as "no usable recommendation".
- **Per-day results** (`days[]`): `date`, `exceedance_flag`, `exceedance_probability` (0–1), `worst_case_value` (µg/m³), `aqi_category` (one of `good`, `satisfactory`, `moderate`, `poor`, `very_poor`, `severe`; display them as human-readable labels). `worst_case_value` is the upper bound of the predicted range, not the expected value.
- **Forecast series** (`/forecast/{station_id}`): each point has `target_time`, `horizon_hours` (24, 48, 72, 96, 120), `point_forecast` (expected value), `quantile_low` / `quantile_high` (lower/upper bound of the predicted range, the 10th and 90th percentiles), `exceedance_probability`, `exceedance_flag`. `forecast_made_at` is when it was generated, and can be `null` with an empty `forecasts` list when there is none.
- **Always show how old the data is.** Show `forecast_made_at` (and `latest_observed_at` for stations) as a clear "as of" time, with relative age. A forecast is only treated as current by the server for a few hours; the UI should never imply it is fresher than it is.
- **Time:** all timestamps are UTC ISO-8601. Display **Indian Standard Time (Asia/Kolkata)**. Per-day results are already bucketed by IST calendar day (`date`).
- **History** (`/forecast/{station_id}/history`, `lookback_days` 1–90): points with `time`, `actual` and `forecast_value`, either of which may be `null`; render gaps as gaps, not zeros.
- **Model health** (`/model-health`) is operator-facing accuracy data (precision, recall, f1, mae, rmse, all nullable) and is not for school users.

## Errors and states (every screen)

Design and build loading, empty, and error states for each screen. Specifically: `404` (unknown station or subscription), `422` (validation, e.g. invalid email; show the field-level message), network failure or timeout (offer retry), and an API that returns an empty list. Never show a blank screen or a raw error payload.

## Backend load (be considerate)

- Call `GET /stations` once per session/page load and cache it (about 5 minutes is fine). Data changes at most hourly.
- Do **not** loop over all stations calling per-station endpoints. Fetch per-station data only for the station the user is viewing.
- Do not poll faster than every 5 minutes. Refetch on window focus is fine.

## Build in these phases

Complete each phase fully, then stop and summarise so I can review before you continue. Each phase depends only on the API and the earlier phases.

**Phase 0: Foundation.** Project setup, env var, typed client generated from `openapi.json`, query/cache setup, routing shell, shared time/IST formatting and "as of" age helpers, a shared recommendation type with a mapping that is exhaustive over `go | caution | no-go | no-data`, and reusable loading/empty/error components. No screens yet.

**Phase 1: Station selection.** List stations from `GET /stations`, distinguishing stations with a current forecast from those without, searchable by name, with the current-data count visible ("N of M stations have a current forecast" and why). Selecting a station and pollutant persists across screens (URL params).

**Phase 2: Go / No-Go view** (the primary product screen). For the selected station and pollutant, call `/forecast/{station_id}/exceedance` and show the overall recommendation prominently plus the per-day breakdown (date, category, probability, worst-case value), with the "as of" age. Must handle all four recommendation values, including a clear, non-alarming, non-"go" `no-data` state.

**Phase 3: Forecast detail.** Chart of `/forecast/{station_id}` (expected value with the low/high range across the horizons) and of `/forecast/{station_id}/history` (actual vs. forecast over a selectable lookback), with accessible tooltips and a table alternative to the chart.

**Phase 4: Alert subscription.** A form that calls `POST /subscriptions` (email, one or more stations, one or more pollutants) and shows the result; and an unsubscribe flow using `DELETE /subscriptions/{subscriber_id}`. Note the API returns `subscriber_id` on subscribe: keep it (e.g. in the confirmation and for the unsubscribe action) since there is no login. Do not collect anything beyond what the schema accepts. Make clear to users that alerts are for stations with forecasts.

**Phase 5: Operator model-health page.** A separate, clearly operator-only page listing `/model-health` results (filterable by station/pollutant), with nullable metrics shown as "n/a". Do not link it from school-facing navigation.

## Definition of done (each phase)

Type-checks with no errors; every state above is handled; no hardcoded host or mock data; no recommendation/threshold logic in the frontend; keyboard-navigable and screen-reader-labelled controls; works at phone width. Summarise what you built, which endpoints it uses, and anything you added to `BACKEND_REQUESTS.md`.
