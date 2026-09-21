# External API compliance register

**Rule: read a provider's terms and rate limits BEFORE integrating it, record them here, and give the client a request budget below the limit.** Added after OpenAQ suspended our account (2026-09-21) for repeated rate-limit violations. Never work around a suspension or limit with extra accounts or keys.

Legend: [V] verified from the provider's own page on the date shown, [U] could not be verified (say so; do not assume).

## OpenAQ (api.openaq.org) - SUSPENDED
- Limits [V 2026-09-21]: free tier 60 requests/minute and 2,000/hour; repeated violations can bring a temporary or permanent ban; headers x-ratelimit-used/limit/remaining/reset; 429 on excess.
- Terms [V]: one API key per person; multiple accounts to over-consume or abuse are a violation; no unreasonable bandwidth; do not leave requests running when data is no longer needed; suspension can follow. No documented reinstatement process - contact the team (dev@openaq.org). Bulk/historical data: AWS Open Data archive, not the API.
- Our use: hourly ingestion, ~200 requests/run (one /locations lookup per station + one /hours per sensor). Bursts and overlapping runs caused 207 rate-limit responses on 20-21 Sep.
- Status: account suspended; ingestion_dag and station_maintenance_dag paused. Do not resume without their reply.

## Open-Meteo (weather forecast to fill ERA5's ~5-day gap)
- Limits [V 2026-09-22]: free tier 600/minute, 5,000/hour, 10,000/day.
- Terms [V]: free API is NON-COMMERCIAL ONLY (private/non-profit sites without ads or subscriptions are fine; commercial use needs a paid plan); data under CC-BY 4.0, so ATTRIBUTION IS REQUIRED.
- Our use: one small request per hour.
- OPEN ITEMS: (1) the owner must confirm the service is non-commercial (no ads/subscriptions/paid tier); (2) attribution is not shown anywhere yet.

## Copernicus Climate Data Store (ERA5 / ERA5T)
- Terms [V via search 2026-09-22, not the licence page itself - it returned 404]: the "Licence to use Copernicus Products" was replaced by CC-BY on 2 July 2025; reuse, redistribution and commercial use are allowed with clear, visible attribution, e.g. "Generated using Copernicus Climate Change Service information [year]".
- CDS API request limits: [U] not read; requests queue server-side. We make ~1-2 small requests/hour (see ingestion_dag; the recent-window request slides back to the newest available hour).
- OPEN ITEM: attribution notice not shown anywhere yet.

## data.gov.in - CPCB real-time AQI feed
- Licence [V via the API's own metadata and the portal footer]: Government Open Data License - India (GODL-India); commercial and non-commercial use allowed with attribution. The API returns the exact attribution text (common/aqi.py ATTRIBUTION) and the frontend must show it.
- API terms/rate limits: [U] the portal's terms page is a JavaScript shell whose API-specific rules could not be read; the API returns X-Ratelimit headers of -1 (no published limit). Our use is light: ~7 paged requests/hour with a personal key.
- Key handling: personal key in docker/.env only; never commit or paste it.

## Adding a new provider (checklist)
1. Read the terms and rate-limit pages; record limits, key policy, commercial-use, attribution, caching/redistribution rules here with [V]/[U] and the date.
2. Confirm our expected request volume is well under the limits (budget <= 50% of the tightest limit).
3. Pace requests from the provider's rate-limit headers; single active run per provider; fail loudly on 401/403.
4. Add any required attribution to the API response so every client shows it.
