# Product Requirements — Electricity Dashboard

> Reverse-engineered from the codebase as of June 2026.  
> Property: Sparreholmsvagen 6a. System: Growatt inverter + 20 kWh LiFePO4 battery, 11 kWp solar.

---

## 1. Overview

A single-page web dashboard that gives the household a real-time and historical view of:
- Solar generation, battery state, grid import/export, and house load
- Electricity costs and earnings under the Fortum/Ellevio tariff
- Spot price trends (SE3 or SE4)
- Battery charge/discharge scheduling recommendations (TOU)
- Swedish national grid production mix for context
- Return-on-investment tracking for the solar + battery installation

Hosted on Vercel (Hobby plan). Data is stored in Supabase (`energy_readings`, `daily_summary`, `spot_prices`, `weather_cache`, `tou_cache`, `grid_production`).

---

## 2. Users & Access

- **Single household user** — no authentication, no multi-user support.
- Dashboard is public-read (Supabase RLS allows SELECT for anon).
- Writes are done exclusively by Vercel cron functions (server-side), never directly from the browser.

---

## 3. Tabs & Navigation

| Tab | Label | Purpose |
|-----|-------|---------|
| `nu` | ⚡ Live | Real-time power flow + today's KPIs and charts |
| `dag` | 📅 Dag & TOU | Date-picker view of any day + TOU schedule editor |
| `trender` | 📈 Trender | Monthly summaries, ROI card, Swedish grid production |

A global **date picker** controls which day's data is shown in charts and KPI rows. Defaults to today.

---

## 4. Live Tab (`nu`)

### 4.1 Live Power Flow Diagram
- Animated SVG showing energy flows between: **Solar → Inverter → Battery / Grid / House Load**.
- Direction arrows change based on current power flow (charge vs. discharge, export vs. import).
- Updates every ~30 seconds via `GET /api/live`.
- Fields displayed: solar kW, battery SoC %, battery kW (charge/discharge), grid import/export kW, house load kW.

### 4.2 Live Weather Strip
- Animated sky background (day/night gradient) with rain particles when raining.
- Sky icon (sun/cloud/rain/snow) derived from Open-Meteo WMO weather codes.
- Displays: current temperature, condition icon, cloud cover %, rain probability %, wind speed.
- Updates alongside live data refresh.

### 4.3 Mini Live Strip (historical date view)
- When a non-today date is selected on the Nu tab, the full live flow diagram is hidden.
- A compact chip strip replaces it, showing the last known live values for: Solar, Battery SoC, Battery kW, Spot price, Grid net.
- Same chip format as the Dag/Trender tab mini strips.

### 4.4 KPI Row — Energy (today or selected date)
| KPI | Source |
|-----|--------|
| Solar generation (kWh) | `epv_today` counter from inverter |
| House load (kWh) | `eload_today` counter |
| Export to grid (kWh) | `export_today` counter |
| Grid import (kWh) | Integrated from 5-min `import_kw` slots (counter unreliable) |
| Battery charged (kWh) | `echarge_today` counter |
| Battery discharged (kWh) | `edischarge_today` counter |

### 4.5 KPI Row — Cost & Earnings
- Import cost: `(spot + nätavgift + energiskatt + fortum_påslag) × 1.25 (moms)`
- Export earnings: `spot + nätnytta` (no moms)
- Daily fixed fee: `(fastAvgift + fortumFast) × 1.25 ÷ 30`
- Net day result (kr) = earnings − import cost − fixed fee
- Self-sufficiency % = (solar − export) / load × 100

### 4.6 KPI Row — Price Averages
- Average spot price for today (import hours)
- Average sell price for today (export hours)
- Tomorrow's min/max/average spot price

### 4.7 Power Flow Chart (Chart.js)
- 5-minute resolution stacked line chart for the selected day.
- Datasets: Solar, Load, Export, Import, Battery Charge, Battery Discharge, SoC %.
- SoC shown on a secondary Y-axis (0–100 %).
- Forecast overlay: from current slot to end of day, using today's solar model + battery simulation.
- `spanGaps: true` on SoC lines to bridge chart-row gaps.
- Toggleable legend (persists across date changes).

### 4.8 Effektflöde & Spotpris Chart
- Combined power + spot price chart.
- Spot price line (öre/kWh) on right Y-axis in **fuchsia** (`#e879f9`) — distinct from all power dataset colours.
- Useful for correlating export/import decisions with price.

### 4.9 Price & Running Cost Chart
- Hourly spot price bar chart.
- Running cumulative cost/earnings line.
- Tomorrow's prices shown when available.

### 4.10 CSV Export
- Button exports today's 5-min KPI data as a `.csv` file.
- Includes: time, solar, load, import, export, charge, discharge, soc, spot price, running cost.

---

## 5. Dag & TOU Tab (`dag`)

### 5.1 Date Navigation
- Date picker selects any historical day.
- All charts and KPIs re-render for the selected date.
- Mini live strip (chip row) shows current live values regardless of selected date.

### 5.2 TOU Battery Schedule Panel
- Displays current Time-of-Use charge/discharge schedule uploaded to inverter.
- Table with columns: Start, End, Action (charge/discharge/self-use), SoC floor, power %.
- Past slots greyed out.
- **Edit mode**: user can modify schedule directly in the table.
- "Återställ" button resets to default schedule.
- "Uppdatera" button writes edited schedule back to inverter via Growatt API.

### 5.3 TOU Suggestion Panel
- AI-generated optimal schedule suggestion based on tomorrow's spot prices.
- Computed nightly by `api/growatt_tou.py` (`build_suggest` action, cron 22:10 UTC).
- Displays suggested charge windows, discharge windows, SoC floor, and discharge power %.
- "Tillämpa förslag" button applies suggestion to the live schedule.
- Obsolete slots (start time in the past) are greyed out and excluded when applying.

### 5.4 Discharge Power & SoC Floor Controls
- Slider/input to set battery max discharge power (% of rated 12 kW).
- Slider/input to set minimum SoC floor (battery never discharges below this %).
- Synced with TOU suggestion panel.

---

## 6. Trender Tab (`trender`)

### 6.1 Monthly Summary Table
- One row per day for the current month.
- Columns: Date, Solar kWh, Load kWh, Import kWh, Export kWh, Net cost (kr).
- Totals row at bottom.
- Data from `daily_summary` table (written by `api/save_summary.py` nightly or on-demand).

### 6.2 ROI Card
- Tracks cumulative return on investment for the solar + battery system.
- Shows: total savings to date, projected annual savings, payback years remaining.
- Input: installation cost, monthly savings derived from daily_summary.

### 6.3 Swedish Grid Production Widget
- National electricity production mix for the last 7 days (hourly resolution).
- Source: **eSett Open Data API** (`EXP16/Volumes`) — no API key required.
- Stored in Supabase `grid_production` table; fetched daily at 08:00 UTC by `api/grid.py`.
- **KPI chips**: Nuclear (MW + % of 6 804 MW nominal), Hydro, Wind, Solar, Thermal, Total.
  - Nuclear chip colour: green >70 % capacity, amber 40–70 %, red <40 %.
- **Stacked area chart**: 7 days, datasets for Nuclear / Hydro / Wind / Solar / Thermal+Other.
- Subsampled to one point per 2 hours (~84 points) for readability.
- Dashed reference line at 6 804 MW (Swedish nuclear nominal capacity).

---

## 7. Configuration Panel

All tariff and system parameters are editable in-browser (no redeploy required):

| Parameter | Default | Notes |
|-----------|---------|-------|
| Price area | SE3 | SE3 or SE4 |
| Solar kWp | 11.0 kWp | Used in solar model |
| Battery capacity | 20.0 kWh | Used in SoC simulation |
| Fast avgift (Ellevio) | 390 kr/mån | Grid subscription fee |
| Nätavgift import | 26.0 öre/kWh | Grid use fee |
| Nätnytta high/low | 5.50 / 4.12 öre/kWh | Export credit |
| Energiskatt | 54.875 öre/kWh | Fixed by Swedish law |
| Fortum påslag | 6.96 öre/kWh | Supplier markup |
| Fortum fast | 55.20 kr/mån | Supplier fixed fee |
| Panel tilt | 45° | Solar model geometry |
| Panel azimuth | −68° | South-southwest |
| Battery efficiency | 95 % | Round-trip efficiency |
| Starting SoC | 50 % | Default for forecast |

Settings persist in `localStorage`.

---

## 8. Data Collection & Gap-Filling

### 8.1 Live Cron (every 5 minutes, always-on)
- `api/collect.py` calls Growatt `getTlxDetailData` for the current inverter state.
- Inserts one row into `energy_readings` with `soc_pct NOT NULL` (live row).
- After each insert, runs `_heal_recent_gaps`: checks the last 2 hours for missing 5-min slots; if gaps found, fetches today's chart data from Growatt and upserts non-zero rows.
- Gap rows are **chart rows** (`soc_pct = NULL`); live rows take priority in display bucketing.

### 8.2 Nightly Autofill (01:00 UTC = 03:00 CEST)
- `api/collect?action=autofill&days=3` backfills the last 3 days of chart data.
- Safety net to catch any days where the self-healing failed (e.g. Vercel cold-start gaps).

### 8.3 Historical Backfill (on demand)
- `GET /api/collect?date=YYYY-MM-DD` fetches a full day's chart data from Growatt.
- Upserts with `merge-duplicates` so stale zero rows get overwritten.
- Deletes future zero sentinel rows for today after backfill.

### 8.4 Zero Sentinel Filtering
- Growatt returns `0.0` as a sentinel for "no data recorded" (inverter off / night).
- Chart rows where all power columns are zero are dropped at import time — never stored.
- Condition: `ppv_kw`, `load_kw`, `export_kw`, `discharge_kw` all zero or null → skip row.

### 8.5 Row Deduplication
- Chart rows have `:00` second timestamps; live rows have `:30` second timestamps → they never collide.
- `merge-duplicates` is safe for all upserts; allows corrected chart rows to overwrite earlier bad data.

---

## 9. SoC Continuity (Chart Display)

Live rows carry `soc_pct`; chart/backfill rows do not.  
At display time (`api/energy.py → _bucket_readings`), SoC is interpolated:

- **Between two known values**: linear interpolation.
- **Trailing gap** (no future anchor): hold last known value.
- **Leading gap** (no past anchor): leave null (no guessing before first reading).

This ensures the SoC line is continuous and smooth across chart-data gaps.

---

## 10. Solar Generation Forecast

### 10.1 Layer 1 — Physics Model (baseline)

- Open-Meteo API provides hourly **Global Tilted Irradiance** (GTI, W/m²) for the exact panel geometry: tilt 45°, azimuth −68° (south-southwest), lat 59.28°N.
- `gtiToSolarProfile()` converts hourly GTI → 288 five-minute kW slots using:
  - Performance Ratio (PR = 0.82) accounting for system losses
  - Solar position geometry (declination, hour angle, altitude, azimuth) to zero out slots where the sun is behind the panel face
  - Empirical afternoon scale factor (0.65× from 17:00) correcting for known late-day overestimate at this azimuth
  - Hard horizon cutoff at 21:10 (buildings/terrain obstruction)
- Intraday forecast: runs from `nowSlot` to end of day, anchored to current real SoC.
- Tomorrow forecast: full-day simulation using a midnight SoC projected by running today's remaining slots through the physics model.
- 5-day outlook: same model applied to D+1 through D+5 using Open-Meteo's 7-day GTI forecast; displayed as a bar chart on the Trender tab (colour-coded by cloud cover).

### 10.2 Layer 2 — Learned Per-Slot Corrections (ML overlay)

A lightweight empirical correction layer refines the physics model using the system's own historical production data. This is **not** a general ML model — it is a per-slot ratio table trained exclusively on this installation's actual output.

**How it works:**

1. **Data collection** (`api/solar_model.py?action=build`, cron 04:00 UTC daily):
   - Fetches 90 days of actual `ppv_kw` readings from `energy_readings` via a Supabase RPC (`get_solar_actuals_by_slot`) that returns `avg_solar_kw` and `day_count` per 5-min slot.
   - Fetches 90-day historical GTI from Open-Meteo (`past_days=90`) for the same panel geometry.
   - Averages GTI per hour across the 90-day window.

2. **Ratio computation:**
   ```
   ratio[slot] = avg_actual_kw[slot] / avg_gti_wm2[hour_of_slot]
   ```
   Only slots meeting quality thresholds are assigned a ratio:
   - `avg_gti ≥ 50 W/m²` (daytime signal, not noise)
   - `day_count ≥ 5` (enough samples for a stable average)
   - Slots failing either threshold get `ratio = NULL` → falls back to pure physics model.

3. **Application** (`buildIntradayForecast` in `index.html`):
   - After the physics model runs for future slots, the frontend fetches `/api/solar_model`.
   - For each forecast slot where `ratio IS NOT NULL` and `gti > 50 W/m²`, the model overrides the physics estimate:
     ```
     solar_kw[slot] = gti_today[slot] × ratio[slot]
     ```
   - This captures site-specific effects the physics model can't know: shading patterns, soiling, inverter clipping, actual panel orientation imprecision.

**Storage:** `solar_model` table in Supabase — 288 rows (one per 5-min slot), columns: `slot`, `ratio`, `day_count`, `updated_at`.

**Limitations & design choices:**
- Corrections apply to the **intraday** forecast only; the tomorrow forecast currently uses the pure physics model (the ratio table could be applied there too as a future improvement).
- The ratio is a 90-day rolling average — it adapts to seasonal changes in shading and panel soiling within 1–3 months.
- Does not model weather-type-specific corrections (e.g., a different ratio on overcast vs. clear days). A future enhancement could stratify by cloud cover band.
- Forecast is shown as a lighter overlay on the power flow and running-cost charts. Forecast slots are visually distinguished from actual data slots.

### 10.3 Battery Simulation

- `simulateBatteryDay()` runs a full 288-slot simulation: solar surplus → charge battery → export remainder; deficit → discharge battery → import remainder.
- Respects battery floor (10 % SoC minimum), capacity (`batteryKwh`), and round-trip efficiency (`batteryEff`).
- Used for both intraday (from `nowSlot`, anchored to real SoC) and tomorrow (from projected midnight SoC) forecasts.
- Load profile for simulation: yesterday's actual `load_kw` values; falls back to flat 1 kW if unavailable.

---

## 11. Spot Prices

- Fetched from Nordpool/Elpriset API by `api/save_prices.py`.
- Today's prices fetched at 06:00 UTC; tomorrow's at 13:00 UTC (after Nordpool publishes at ~12:45).
- Stored in Supabase `spot_prices` table.
- Served to frontend by `api/prices.py`.

---

## 12. Growatt API Field Mapping

Confirmed mapping between Growatt `getEnergyProdAndCons_KW` fields and DB columns:

| Growatt field | DB column | Meaning |
|---------------|-----------|---------|
| `ppv` | `ppv_kw` | PV generation (kW) |
| `sysOut` | `load_kw` | Total house load (kW) |
| `pacToGrid` | `export_kw` | Export to grid (kW) |
| `pacToUser` | `discharge_kw` | Battery → loads (kW) |
| `chargePower` | `charge_kw` | Battery charge (kW) |
| `userLoad` | `import_kw` | Grid import (kW) |

`soc_pct` is **not available** from the chart API — always NULL in chart rows, interpolated at display time.

---

## 13. Scheduled Cron Jobs (Vercel)

| Schedule (UTC) | Endpoint | Purpose |
|----------------|----------|---------|
| `*/5 * * * *` | `/api/collect` | Live data collection (implicit, no cron entry — Vercel cron limit) |
| `0 1 * * *` | `/api/collect?action=autofill&days=3` | Nightly chart backfill |
| `0 3 * * *` | `/api/weather` | Daily weather cache refresh |
| `0 4 * * *` | `/api/solar_model?action=build` | Rebuild solar generation model |
| `0 6 * * *` | `/api/save_prices?area=SE3` | Fetch today's spot prices |
| `0 8 * * *` | `/api/grid?action=fetch` | Fetch Swedish grid production |
| `0 13 * * *` | `/api/save_prices?area=SE3&date=tomorrow` | Fetch tomorrow's spot prices |
| `0 20 * * *` | `/api/growatt_tou?action=notify_reset` | Reset TOU notification state |
| `10 22 * * *` | `/api/growatt_tou?action=build_suggest` | Build TOU schedule suggestion |

---

## 14. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Gap fill latency | ≤ 5 minutes (self-healing after next cron tick) |
| Historical chart load | < 2 s (Supabase query + bucket aggregation) |
| Today's energy API cache TTL | 5 minutes |
| Past dates cache TTL | Indefinite (immutable once day is complete) |
| Vercel function limit | 12 (Hobby plan) — `backfill.py` deleted to stay within limit |
| No authentication required | Dashboard is public-read |
| No secrets in frontend | All Growatt/Supabase credentials are server-side only |
| Timezone | All CEST display (UTC+2), all DB storage in UTC |

---

## 15. Known Constraints & Design Decisions

- **Growatt account lockout**: Hard-pause code was removed after a June 2026 lockout. `_HARD_PAUSED_UNTIL = None` in `_growatt.py` must stay `None`.
- **import_today counter excluded from KPIs**: Growatt reports this with 0.10 kWh granularity, causing phantom +0.20 kWh steps. Import kWh is always integrated from 5-min slot data instead.
- **charge_kw from API, fallback to energy balance**: Frontend reads `v.charge` directly; energy balance formula `(solar + import − load − export + discharge)` is only used as fallback for old rows pre-dating the `charge_kw` field.
- **pacToUser = discharge, not import**: Common point of confusion — the Growatt field named `pacToUser` means "power flowing from battery to user loads", not "power from grid to user". Grid import is `userLoad`.
