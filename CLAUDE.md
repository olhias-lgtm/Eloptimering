# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Harness: Electricity Dashboard

**Goal:** Coordinate backend, frontend, and infra changes without breaking the data contract between layers.

**Trigger:** For tasks that touch more than one axis (Python + HTML, or schema + code), use the `orchestrate` skill. Single-file fixes can be handled directly.

**Agents:** `.claude/agents/` — `orchestrator`, `backend-agent`, `frontend-agent`, `infra-agent`
**Skills:** `.claude/skills/` — `orchestrate`, `backend`, `frontend`, `infra`

**Change history:**
| Date | Change | Target | Reason |
|------|--------|--------|--------|
| 2026-06-03 | Initial harness setup | All | First configuration |

## What this project is

Real-time electricity cost & earnings dashboard for a Swedish household with Growatt solar + battery inverter. Combines Growatt API data, Nordpool/Fortum spot prices, and Ellevio grid tariffs.

## Running locally

```bash
python3 proxy.py          # serves index.html at http://localhost:8080
```

Credentials go in `.env` (copy from `.env.example`):
```
GROWATT_USERNAME=...
GROWATT_PASSWORD=...
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
```

## Deployment

Production runs on **Vercel** (Python 3.12 serverless). GitHub Actions deploys on every push to `main` — Vercel's own GitHub integration is intentionally bypassed because it silently stalls.

```bash
vercel deploy --prod --yes    # force-deploy manually if needed
vercel env pull .env.local    # pull production env vars locally
```

## Architecture

### Two runtimes, one frontend

**Local dev**: `proxy.py` serves `index.html` and proxies Growatt API calls directly. It has its own `GrowattSession` class and mock data generator — mostly a legacy artifact.

**Production**: `index.html` (static) + `api/*.py` (Vercel serverless functions). The frontend calls `/api/*` endpoints directly. No build step.

### Data flow

```
cron-job.org (every 5 min)
  → GET /api/collect
  → _growatt.py (GrowattSession singleton, cookies in Supabase growatt_session table)
  → Supabase energy_readings table

Vercel crons (vercel.json)
  → /api/weather       @ 03:00 UTC  → Supabase weather_forecast
  → /api/solar_model   @ 04:00 UTC  → Supabase solar_model
  → /api/save_prices   @ 06:00+13:00 UTC → Supabase spot_prices
  → /api/growatt_tou   @ 22:10 UTC  → build TOU suggestion, push to Growatt

Browser
  → /api/live    (live inverter reading, polled every 30s)
  → /api/energy  (bucketed 5-min chart data from energy_readings)
  → /api/prices  (spot prices from Supabase or elprisetjustnu.se)
  → /api/weather (cached Open-Meteo forecast)
  → /api/monthly (daily_summary aggregates)
```

### Key shared modules

**`_growatt.py`** — module-level `GrowattSession` singleton shared across all serverless invocations via warm Lambda reuse. Persists JSESSIONID cookies to Supabase `growatt_session` (row id=1). `SESSION_MAX_AGE_HOURS = 0.75` (proactively re-logins before Growatt's ~1h server-side TTL). `get_live()` and `get_energy()` both have 2-attempt retry with session-expiry detection.

**`_schema.py`** — single source of truth for `energy_readings` column mapping:
- `CHART_FIELD_MAP`: Growatt chart API keys → DB columns (critical: `pacToUser` → `discharge_kw`, NOT import)
- `CHART_NULL_FIELDS`: fields unavailable from chart API (always NULL in chart rows)
- `DAILY_TOTALS_FIELDS`: reliable inverter counters for daily KPI totals
- `row_type(row)`: `"live"` if `soc_pct IS NOT NULL`, else `"chart"`

### Supabase schema notes

**`energy_readings`** — primary time-series table. Two row types:
- **Live rows**: written by `/api/collect` cron; `soc_pct IS NOT NULL`; timestamps NOT on 5-min boundaries
- **Chart rows**: written by `/api/collect?date=YYYY-MM-DD&confirm=1` backfill; `soc_pct IS NULL`; timestamps on exact 5-min boundaries
- Unique constraint on `ts` — `ignore-duplicates` upsert works correctly
- Live rows always win over chart rows in bucketing (see `energy.py` `_bucket_readings`)

**`daily_summary`** — one row per completed past day, written by the frontend `renderKPIs()` only when `dateStr < today` (or today after 23:50 CEST). Never written for forecast/tomorrow views.

### Frontend (`index.html`)

Single ~4000-line file. Key sections:
- `state` object holds `energyData`, `charts`, `hiddenPowerDs` (legend toggle state), etc.
- `renderPowerChart()` — Chart.js power flow chart with intraday forecast overlay
- `buildIntradayForecast()` — today's remaining SoC/power simulation from current slot to 23:55
- `buildTomorrowForecast()` — tomorrow's full-day simulation; starting SoC computed by running today's solar simulation to midnight (not a flat drain estimate)
- `renderKPIs()` — cost/earn calculation using `calcInterval()` per 5-min slot
- `_applyPowerDsVisibility()` — applies `state.hiddenPowerDs` Set to Chart.js after re-renders
- `_POWER_DS_KEYS` — maps legend `data-ds` keys to dataset label prefixes

**Cost formula (Swedish tariff):**
- Import cost: `(spot + nätavgift + energiskatt + fortum_påslag) × 1.25 moms`
- Export earning: `spot + nätnytta` (no moms on income)
- Fixed daily: `(fastAvgift + fortumFast) × 1.25 / 30`

### Growatt API quirks

- Password hash: MD5, then replace `'0'` → `'c'` at every even index
- `getTlxDetailData` fields are in **Watts** (divide by 1000 for kW)
- `getSystemStatus_KW` fields are already in **kW**
- Chart API `pacToUser` = battery→loads, NOT grid import (grid import unavailable in chart API)
- Chart slots are in local time (CEST/CET), labelled with slot END time — subtract 5 min to align with Shinephone

### TOU (Time-of-Use battery schedule)

`/api/growatt_tou.py` manages Growatt's 9-segment daily TOU schedule. The suggestion engine runs nightly, generates optimal charge/discharge windows based on tomorrow's spot prices and solar forecast, and stores them in Supabase `tou_cache`. The frontend renders the suggestion with obsolete slots (start time already passed today) greyed out and excluded from the "apply" action.

## Backfilling data gaps

```bash
# Preview (dry run)
curl "https://electricity-dashboard-phi.vercel.app/api/collect?date=YYYY-MM-DD"

# Write to Supabase
curl "https://electricity-dashboard-phi.vercel.app/api/collect?date=YYYY-MM-DD&confirm=1"
```

Chart rows use `ignore-duplicates` so they never overwrite live rows. The `UNIQUE(ts)` constraint prevents duplicate chart rows from repeated backfills.
