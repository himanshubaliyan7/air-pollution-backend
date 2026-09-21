# Prompt for Lovable: frontend for a multi-region air-quality API

Attach with this prompt: `docs/openapi.json` (the API contract) and your own design/brand material.

---

You are building the **frontend only** for an existing, already-running backend that helps schools decide whether outdoor practice is safe. It provides two kinds of information per monitoring station: **what the air quality is right now** (official readings) and **a multi-day outlook** (forecasts with a go / caution / no-go signal). The service covers **one region today and will cover many** (different cities, countries, time zones and air-quality standards). Build for the general case from day one. Read this whole brief before writing code.

## Ground rules

1. **Do not build or change any backend.** The API exists. The attached `openapi.json` is the single source of truth for every endpoint, parameter and response shape. Generate typed models and one API client module from it. All network calls go through that one module, so the base URL lives in a single place.
2. **Base URL comes from an environment variable** (`VITE_API_BASE_URL`), no trailing slash. All routes are under `/api/v1`. Never hardcode a host. There is no authentication.
3. **Do not invent endpoints, fields, or data.** No mock data in finished screens. If you need something the API does not provide, do not fake or work around it: add an entry to a `BACKEND_REQUESTS.md` in the repo (what you need, which screen needs it, why) and continue with what exists.
4. **Do not compute forecasts, thresholds, AQI values, AQI categories, health verdicts or recommendations in the frontend.** The server already decides them. Render what it returns.
5. **Design is provided separately by me.** Do not invent a visual identity, palette, typography, logo or imagery. Until I supply material, use plain, unstyled, accessible defaults and keep all presentation in isolated components/tokens so my design can be applied without restructuring. Start each phase below by checking whether design material has been supplied for it.
6. Keep dependencies small. Use a data-fetching/cache library (e.g. TanStack Query), TypeScript, and a router.

## Nothing region-specific may be hardcoded

The API currently serves a single region, and that must not leak into the code. **Never hardcode** any of the following; read them from the API:

- **Region names, station names or place names.** No city names in code, copy, routes, or defaults.
- **Time zone.** Every displayed time and every day boundary uses the IANA time zone the API gives for the region (`GET /regions`, and the `timezone` field on the exceedance and current-aqi responses). Never assume a fixed offset or a specific zone, and never fall back to the browser's zone for forecast days.
- **AQI standard and category names.** The category ids returned in `aqi_category` / `category` fields, their order (best to worst) and their labels come from `GET /regions`. Different regions use different standards with different numbers of categories, so do not hardcode a category count, names, or order. If the API returns a category id you do not recognise, show a readable version of the id rather than failing.
- **Which level counts as "not recommended".** The region declares it. For current readings the API already tells you (`at_or_above_health_threshold`); do not compare categories yourself.
- **Pollutants.** The set comes from the region's `pollutants` list (for forecasts) and from the `pollutant_id` values in current readings (CPCB publishes PM2.5, PM10, NO2, SO2, CO, OZONE, NH3). Render whatever is returned. Keep units in one place.
- **Language and formatting.** Keep all user-facing text in a single translatable strings module (no string literals scattered through components), and format dates and numbers through the browser's locale APIs, so new regions and languages need no restructuring.

### Regions and stations

- `GET /regions` lists regions; `GET /regions/{region_id}` returns one. Each has `id`, `name`, `country`, `timezone`, `bbox`, `aqi_standard`, `pollutants`, `aqi_categories` (ordered best to worst, each with `id` and `label`) and `health_threshold_category`.
- Every station has a `region_id` (it can be `null`). `GET /stations` accepts an optional `region_id` filter.
- Build the app so the region is part of the app's state and URL from the start (for example `/r/{regionId}/...`), with a region switcher. **With exactly one region, skip the picker and go straight in, but do not remove the region from the data flow**, so adding a second region needs no code change.

## The two kinds of information (do not confuse them)

**1. Air quality right now** (`GET /stations/{station_id}/current-aqi`). Official, near-real-time readings published hourly by the national pollution-control authority. Most stations have these.
- `is_current` says whether the reading is fresh enough to act on. When `false`, `pollutants` is **empty on purpose** and `overall` is `null` (the server refuses to serve an old reading as current); `as_of` (null if the station never had one) says how old the last reading was. Show "no current reading; the last one was <time>", never old numbers.
- `overall` (nullable) is the station's overall AQI: `aqi` (a number), `category` (an id from the region's categories) and `driver` (the pollutant that sets it). It is `null` when too few pollutants are reporting for an overall value to be valid; then show the individual pollutants and say an overall AQI is not available. Never compute your own overall.
- `at_or_above_health_threshold` (nullable) is the server's verdict on whether the overall category is at or worse than the level where outdoor practice is not recommended. `true` means show a clear "outdoor practice not recommended right now" state. `false` means the reading is below that level, which is a statement about current conditions and **not** a forecast or a go signal. `null` means no verdict.
- `pollutants[]`: per pollutant `pollutant_id`, `sub_index_avg`, `sub_index_min`, `sub_index_max` (any may be `null`) and `category`. **These are AQI sub-indices over a rolling window (roughly the last day), not concentrations.** Never label them with a concentration unit such as µg/m³; label them as an index. `sub_index_min` / `sub_index_max` describe the spread over that window.
- `aqi_standard` names the standard (show it), `timezone` is the region's zone.
- **`attribution` is mandatory.** The data is published under a licence that requires attribution. Show the `attribution` text exactly as returned, visibly, on every screen that displays current readings.

**2. Outlook** (`/forecast/{station_id}` and `/forecast/{station_id}/exceedance`). Multi-day forecasts. **Only a small number of stations have one at any time**, because it needs hourly concentration history that most stations do not have. This is normal, not an error.
- `overall_recommendation` is exactly one of `go`, `caution`, `no-go`, `no-data`.
  - **`no-data` means there is no usable forecast. It must never be shown, coloured, worded or grouped as "go", nor as any safe/positive state.** This is the most important rule in the whole app: a school may treat silence as clearance.
  - `no-data` can also be returned together with a partial `days` list, when the forecast run is missing some days. Show those days for information but keep the overall state as no-data; never infer a "go" from an incomplete set.
  - Treat any unknown future value as "no usable recommendation".
- `days[]`: `date` (a calendar day in the response's `timezone`), `exceedance_flag`, `exceedance_probability` (0-1), `worst_case_value` (the **upper bound** of the predicted range, not the expected value), `aqi_category`.
- Forecast series: each point has `target_time`, `horizon_hours`, `point_forecast` (expected value), `quantile_low` / `quantile_high` (10th/90th percentile range), `exceedance_probability`, `exceedance_flag`, in concentration units. Do not hardcode which horizons exist. `forecast_made_at` is when it was generated. `is_current` says whether it is fresh enough to act on: when `false` the `forecasts` list is **empty on purpose**, and `forecast_made_at` (possibly `null`) tells you how old the last one was. Show "no current forecast; the last one was made <time>", never a chart.
- History (`/forecast/{station_id}/history`, `lookback_days` 1-90): `time`, `actual`, `forecast_value`, either may be `null`; render gaps as gaps, not zeros.

**Rules that apply across both**
- **Only the outlook's `overall_recommendation` may use the words go / no-go, and only `at_or_above_health_threshold` may drive an "outdoor practice not recommended" message.** A "good" or "satisfactory" current category is a description of the air, never "safe" or "go".
- **A station with a current reading but no forecast is normal and common.** Show the current reading, and say plainly that no outlook is available for that station. Do not fill the outlook area with anything that looks like a positive result.
- **Always show data age.** Show `as_of` and `forecast_made_at` (and `latest_observed_at` on stations) as clear "as of" times in the region's time zone with relative age.
- **Time:** all API timestamps are UTC ISO-8601. Display in the region's IANA zone.
- **Station ids contain a colon** (e.g. `provider:1234`). URL-encode them in paths.
- **Station list** (`GET /stations`): each station has `has_current_aqi` and `has_current_forecast` (both can be false), and `latest_observed_at`. Stations with a current forecast come first, then those with a current reading. Show how many stations in the region have each, and let users still browse the rest without ever presenting them as "safe". Hardcode no coverage numbers.
- **Model health** (`/model-health`) is operator-facing accuracy data (nullable metrics) and is not for school users.

## Errors and states (every screen)

Design and build loading, empty, and error states for each screen. Specifically: `404` (unknown region, station or subscription), `422` (validation, e.g. invalid email; show the field-level message), network failure or timeout (offer retry), and an API that returns an empty list. Never show a blank screen or a raw error payload.

## Backend load (be considerate)

- Call `GET /regions` and `GET /stations` once per session/page load and cache them (about 5 minutes is fine). Data changes at most hourly.
- Do **not** loop over all stations calling per-station endpoints. Fetch per-station data (`current-aqi`, exceedance, forecast) only for the station the user is viewing.
- Do not poll faster than every 5 minutes. Refetch on window focus is fine.

## Build in these phases

Complete each phase fully, then stop and summarise so I can review before you continue. Each phase depends only on the API and the earlier phases.

**Phase 0: Foundation.** Project setup, env var, typed client generated from `openapi.json`, query/cache setup, routing shell with the region in the URL, a region context (loaded from `/regions`) that supplies time zone, category order/labels, health-threshold category and pollutant list to everything else, the translatable strings module, shared time formatting in the region's zone and "as of" age helpers, a shared recommendation type with a mapping that is exhaustive over `go | caution | no-go | no-data`, a shared attribution component, and reusable loading/empty/error components. No screens yet.

**Phase 1: Region and station selection.** Region switcher (hidden when there is one region). List stations for the selected region from `GET /stations`, distinguishing stations by `has_current_aqi` and `has_current_forecast`, searchable by name, with the coverage counts visible ("N of M stations have a current reading; K have a forecast" and why). The selected region, station and pollutant persist across screens via URL params.

**Phase 2: Station overview (the primary screen).** For the selected station, two clearly separate sections. **Section A, "Air quality right now"** (from `current-aqi`): the overall AQI, its category, the driver pollutant, the `at_or_above_health_threshold` state, the per-pollutant table (sub-index with min/max spread, category), the "as of" age, the standard, and the mandatory attribution; handle `is_current: false`, `overall: null` and no-reading states. **Section B, "Outlook"** (from `/exceedance`): the overall recommendation prominently plus the per-day breakdown (date, category label from the region, probability, worst-case value), with the "as of" age. Must handle all four recommendation values, including a clear, non-alarming, non-"go" `no-data` state, and the common case of a current reading with no outlook.

**Phase 3: Forecast detail.** Chart of `/forecast/{station_id}` (expected value with the low/high range across the returned horizons) and of `/forecast/{station_id}/history` (actual vs. forecast over a selectable lookback), with accessible tooltips and a table alternative to the chart. Do not draw a threshold line from a hardcoded number; if you need the concentration for the region's health-threshold category, log it in `BACKEND_REQUESTS.md`. Only reachable for stations with a current forecast; other stations show the "no current forecast" message.

**Phase 4: Alert subscription. ON HOLD: do not build this phase until I explicitly tell you to.** The subscription flow is being redesigned on the backend (owner-verified email confirmation), so the contract is provisional. When I release it you will receive an updated `openapi.json`. For reference, today: `POST /subscriptions` takes an email, 1-10 station ids that must exist, and pollutants (lower-case ids from the region's list); it returns `409` if the email already has a subscription (it never modifies an existing one) and `422` with a message for unknown stations or invalid input. `DELETE /subscriptions/{subscriber_id}` unsubscribes. Do not build any "change my subscription" feature. Alerts are currently triggered by forecasts only.

**Phase 5: Operator model-health page.** A separate, clearly operator-only page listing `/model-health` results (filterable by station/pollutant), with nullable metrics shown as "n/a". Do not link it from school-facing navigation.

## Definition of done (each phase)

Type-checks with no errors; every state above is handled; no hardcoded host, region, place name, time zone, category name or mock data; no recommendation, threshold or AQI logic in the frontend; attribution shown wherever current readings appear; all text in the strings module; keyboard-navigable and screen-reader-labelled controls; works at phone width. Summarise what you built, which endpoints it uses, and anything you added to `BACKEND_REQUESTS.md`.
