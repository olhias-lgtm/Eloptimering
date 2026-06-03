# EnergySaver — Architecture & Project Model

## Overview

A personal solar/battery home energy dashboard tracking real-time production,
consumption, costs and grid trends. Deployed as a static frontend + serverless
Python API on Vercel Hobby, with Supabase as the persistence layer.

---

## Repository

| Item | Detail |
|------|--------|
| Repo | `github.com/olhias-lgtm/Eloptimering` |
| Branch model | Single `main` branch; every push triggers a Vercel production deployment |
| Versioning | No semantic versioning — Git commit history is the changelog |
| CI/CD | Vercel GitHub integration; build status visible in `vercel.com` dashboard |

---

## Frontend

**Single file:** `index.html`

- Pure HTML + vanilla JS + CSS — no build step, no framework
- Charts: [Chart.js](https://www.chartjs.org/) loaded from CDN
- Fonts: JetBrains Mono (Google Fonts)
- Three sticky tabs (`⚡ Nu`, `📅 Dag`, `📈 Trender`) navigate between sections:
  - **Nu** — live KPI tiles, battery TOU schedule, smart TOU suggestion
  - **Dag** — selected-day power/cost/price charts, date navigation
  - **Trender** — monthly summary, ROI, Swedish national grid production
- Top chrome (header, date bar, weather strip, tab bar) is `position: sticky`
- Live data polled every 60 seconds via `/api/live`; backoff to 10 min on failures
- Configuration stored in DOM inputs (area, kWp, battery kWh)
- No localStorage besides active-tab preference

---

## Serverless API

**Runtime:** Python 3.12 on Vercel Serverless Functions (Hobby plan — max 12 functions)

All files live under `api/`. Routing defined in `vercel.json`:

```
GET /api/<name>  →  api/<name>.py   (handler class)
GET /            →  index.html
```

### Endpoints

| File | Method | Purpose |
|------|--------|---------|
| `live.py` | GET | Latest energy_readings row from Supabase; falls back to direct Growatt call |
| `energy.py` | GET `?date=` | All 5-min readings for a date; 5-min cache for today, indefinite for past |
| `collect.py` | GET | 5-min data collection (called by cron-job.org); also handles autofill gaps |
| `save_summary.py` | POST | Upsert daily cost/earn summary; server-side guard rejects today/future |
| `monthly.py` | GET `?year=&month=` | All daily_summary rows for a month + all-time ROI totals |
| `prices.py` | GET `?date=&area=` | Spot prices for a date from Supabase spot_prices table |
| `save_prices.py` | GET `?date=&area=` | Fetch prices from elprisetjustnu.se → upsert spot_prices |
| `weather.py` | GET | Combined Open-Meteo GTI + met.no cloud correction forecast |
| `solar_model.py` | GET / GET `?action=build` | Per-slot GTI→kW correction model; rebuild from 90 days history |
| `growatt_tou.py` | GET / POST | Read/write Growatt inverter TOU schedule; build smart daily suggestion |
| `grid.py` | GET `?action=fetch` / GET `?days=N` | Fetch Swedish grid production from eSett → Supabase; serve to frontend |
| `status.py` | GET | Growatt session health check (login status, plant/serial IDs) |

### Shared helpers (not deployed as functions)

> Note: Vercel counts `api/*.py` files toward the 12-function limit.
> Helper modules use a `_` prefix (e.g. `_growatt.py`, `_schema.py`) to be excluded.

---

## External Integrations

| Service | Auth | Purpose |
|---------|------|---------|
| **Growatt Cloud API** | Username + password (env vars) | Inverter live data, historical charts, TOU read/write |
| **elprisetjustnu.se** | None (public) | Swedish hourly spot prices (SE1–SE4) |
| **Open-Meteo** | None (public) | GTI solar irradiance forecast + historical data |
| **met.no** | None (public) | Cloud cover correction for solar forecast |
| **eSett Open Data** | None (public) | Swedish national electricity production mix (nuclear/hydro/wind/solar/thermal), 15-min resolution, ~1–2 day lag |
| **cron-job.org** | Shared secret in URL | External 5-min cron trigger for `collect.py` (Vercel Hobby only supports daily crons natively) |

---

## Cron Jobs

Two layers of scheduled tasks:

### Vercel Native Crons (`vercel.json`) — daily, UTC

| Schedule (UTC) | Endpoint | Purpose |
|---------------|----------|---------|
| `0 3 * * *` | `/api/weather` | Pre-cache tomorrow's weather forecast |
| `0 4 * * *` | `/api/solar_model?action=build` | Rebuild solar production model from last 90 days |
| `0 6 * * *` | `/api/save_prices?area=SE3` | Save today's spot prices to Supabase |
| `0 8 * * *` | `/api/grid?action=fetch` | Fetch Swedish grid production for last 9 days from eSett |
| `0 13 * * *` | `/api/save_prices?area=SE3&date=tomorrow` | Save tomorrow's prices as soon as Nord Pool publishes (~12:00 CET) |
| `10 22 * * *` | `/api/growatt_tou?action=build_suggest` | Build tomorrow's smart TOU suggestion (00:10 CEST) |
| `0 20 * * *` | `/api/growatt_tou?action=notify_reset` | Daily TOU notification/reset hook |
| `0 1 * * *` | `/api/collect?action=autofill&days=3` | Fill any data gaps in the last 3 days |

### cron-job.org — every 5 minutes

Calls `/api/collect` every 5 minutes to write live inverter readings to `energy_readings`.
Vercel Hobby does not support sub-daily cron intervals natively.

---

## Persistence — Supabase (PostgreSQL)

**Project:** `ltajsyuwfxoufmogfevj` (EU region)
**Auth model:** Anon key used by both backend (env var) and read operations; Row Level Security enabled on all tables.

### Tables

#### `energy_readings`
5-minute inverter snapshots written by `collect.py`.

| Column | Type | Notes |
|--------|------|-------|
| `ts` | `TIMESTAMPTZ PK` | UTC timestamp of the reading |
| `solar_kw` | `NUMERIC` | PV production |
| `load_kw` | `NUMERIC` | House consumption |
| `import_kw` | `NUMERIC` | Grid import |
| `export_kw` | `NUMERIC` | Grid export |
| `discharge_kw` | `NUMERIC` | Battery discharge |
| `soc_pct` | `NUMERIC` | Battery state of charge (NULL for chart-only rows) |
| `source` | `TEXT` | `'growatt_chart'` or `'growatt_live'` |

#### `daily_summary`
One row per past calendar day, written by the frontend after the day completes.

| Column | Type | Notes |
|--------|------|-------|
| `date` | `DATE PK` | Calendar date (CEST) |
| `solar_kwh` | `NUMERIC` | |
| `export_kwh` | `NUMERIC` | |
| `import_kwh` | `NUMERIC` | |
| `cost_kr` | `NUMERIC` | Variable import cost incl. VAT |
| `earn_kr` | `NUMERIC` | Export revenue |
| `fixed_kr` | `NUMERIC` | Fixed grid tariff share |
| `net_kr` | `NUMERIC` | Net cost (cost − earn − saved) |

Server-side guard in `save_summary.py` refuses writes for today or future dates regardless of client behaviour.

#### `spot_prices`
Hourly Nord Pool spot prices fetched by `save_prices.py`.

| Column | Type | Notes |
|--------|------|-------|
| `ts` | `TIMESTAMPTZ` | UTC hour boundary |
| `area` | `TEXT` | SE1–SE4 |
| `sek_per_kwh` | `NUMERIC` | Excl. VAT |

Primary key: `(ts, area)`

#### `solar_model`
Per-5-min-slot GTI→kW correction ratios, rebuilt nightly.

| Column | Type | Notes |
|--------|------|-------|
| `slot` | `INT PK` | 0–287 (5-min slots in a day) |
| `ratio` | `NUMERIC` | Avg actual kW / avg GTI W/m²; NULL if insufficient data |
| `day_count` | `INT` | Number of training days used |

#### `tou_suggestions`
Smart TOU plan generated nightly for the next day.

| Column | Type | Notes |
|--------|------|-------|
| `date` | `DATE PK` | Target date |
| `segments` | `JSONB` | Array of `{segment_id, mode, start_hour, …}` |
| `reasoning` | `TEXT` | Human-readable explanation |
| `sim_kpis` | `JSONB` | Simulated cost/savings KPIs |

#### `grid_production`
Hourly Swedish national electricity production mix from eSett.

| Column | Type | Notes |
|--------|------|-------|
| `ts` | `TIMESTAMPTZ PK` | UTC hour boundary |
| `nuclear_mw` | `NUMERIC(8,2)` | |
| `hydro_mw` | `NUMERIC(8,2)` | |
| `wind_mw` | `NUMERIC(8,2)` | Onshore + offshore |
| `solar_mw` | `NUMERIC(8,2)` | |
| `thermal_mw` | `NUMERIC(8,2)` | |
| `other_mw` | `NUMERIC(8,2)` | Energy storage + other |
| `total_mw` | `NUMERIC(8,2)` | |

---

## Security

| Concern | Approach |
|---------|----------|
| Growatt credentials | `GROWATT_USER` / `GROWATT_PASS` stored as Vercel environment variables; never exposed to the client |
| Supabase key | `SUPABASE_URL` + `SUPABASE_ANON_KEY` in Vercel env vars; anon key allows only what RLS permits |
| Row Level Security | Enabled on all Supabase tables; public SELECT, INSERT, UPDATE allowed (personal dashboard, no multi-user auth required) |
| TOU write protection | Growatt TOU write endpoint requires a password entered in the UI; validated server-side before any inverter write |
| Data integrity | `save_summary.py` has a server-side date guard (CEST) so partial intraday data can never be persisted to the monthly table even if the client-side guard is bypassed |
| CORS | All API handlers return `Access-Control-Allow-Origin: *`; acceptable for a personal tool with no sensitive write operations exposed without auth |
| Secrets in code | No secrets in source; `.env` excluded from version control |

---

## Solar Forecast Model

Two-stage pipeline:

1. **Physics layer** — Open-Meteo GTI forecast (W/m²) for configured panel tilt/azimuth, corrected by met.no cloud cover
2. **Learned correction layer** — `solar_model` table stores per-slot ratios from 90 days of actual vs. forecast data; rebuilt every night at 04:00 UTC

Used by `growatt_tou.py` suggestion algorithm with a −15% safety haircut.

---

## Smart TOU Suggestion Algorithm

Runs nightly at 22:10 UTC (00:10 CEST) for the following day:

1. Fetch tomorrow's hourly spot prices (elprisetjustnu.se)
2. Fetch tomorrow's hourly solar forecast (Open-Meteo × solar model × 0.85 haircut)
3. Classify each hour into one of three battery modes:
   - **Battery First** — cheap grid hour + low solar (stock up cheaply)
   - **Grid First** — expensive hour + low solar (discharge battery, avoid grid)
   - **Load First** — solar hours or neutral (let solar self-consume)
4. Merge consecutive same-mode runs into TOU segments (max 9, Growatt limit)
5. Upsert result to `tou_suggestions`; user can review and push to inverter in one click

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Single `index.html` with no build step | Zero toolchain; trivially deployable; instant preview in any browser |
| Vercel Hobby + cron-job.org hybrid | Vercel Hobby only allows daily crons; 5-min live data requires external scheduler |
| Supabase anon key (no user auth) | Personal single-user dashboard; full auth would add complexity with no benefit |
| `live.py` reads Supabase instead of Growatt | Eliminates ~4 000 redundant Growatt API calls/day from 60-second frontend polling; reduces account lockout risk |
| eSett over ENTSO-E for Swedish grid data | eSett requires no API token and has a clean JSON REST API; ENTSO-E requires registration and returns XML |
| Hard-coded Swedish nuclear nominal capacity | 6 804 MW (Forsmark 1+2+3 + Ringhals 3+4 + Oskarshamn 3); changes only if reactors open or close permanently |
| 12-function Vercel limit | Drove deletion of `backfill.py` to make room for `grid.py`; helper modules use `_` prefix to avoid counting |
