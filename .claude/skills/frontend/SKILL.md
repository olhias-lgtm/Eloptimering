---
name: frontend
description: "Work on index.html for the electricity dashboard SPA. Use when changing charts, KPIs, forecasting simulation, cost calculations, TOU UI, monthly summary, or live data display. Triggers: 'update the chart', 'fix the KPI', 'change the forecast', 'add a UI element', 'fix the monthly table'. Do NOT use for Python backend files or Supabase migrations."
---

# Frontend Skill — Electricity Dashboard

## Architecture orientation
`index.html` is a single ~4000-line file. Key anchor points:

| What | ~Line | Notes |
|------|-------|-------|
| `state` object | 1248 | Global app state. `hiddenPowerDs` Set persists legend toggles. |
| `renderPowerChart()` | 1674 | Chart.js power flow. Call `_applyPowerDsVisibility()` after every `upsertChart('power',...)`. |
| `_POWER_DS_KEYS` | 1815 | Maps legend `data-ds` → dataset label prefixes. Update when adding a dataset. |
| `renderKPIs()` | 1511 | Cost/earn calc + `saveDailySummary` guard. |
| `buildIntradayForecast()` | 2294 | Today's SoC simulation from nowSlot → 287. |
| `buildTomorrowForecast()` | 2215 | Tomorrow simulation. Midnight SoC from today's solar sim — not a flat drain. |
| `renderTouSuggestion()` | 3916 | TOU table with obsolete slot greying. |
| `applyTouSuggestion()` | 4000 | Filters out passed slots before writing to editor. |
| Init block | 4006 | Legend click handlers. Wire new one-time event listeners here. |

## Invariants — do not break these
- `_applyPowerDsVisibility()` must be called after every power chart re-render.
- `saveDailySummary()` guard: `isDayComplete = dateStr < todayStr() || (dateStr === today && nowCEST >= 23:50)`.
- `updateSkyIcon()` uses a self-contained astronomical calculation — no external API.
- `spanGaps: true` on SoC datasets (actual + forecast) — required because chart rows have `soc_pct = null`.
- Forecast SoC line (`overlayByTime`) shows only for slots AFTER `lastActualTime`.

## Data contract (mirrors _schema.py)
```javascript
// Daily totals from API
const dt = rows.daily_totals;
solar = dt?.solar_kwh  ?? kwhFromRows(rows, 'solar_kw');
load  = dt?.load_kwh   ?? kwhFromRows(rows, 'load_kw');
exp   = dt?.export_kwh ?? kwhFromRows(rows, 'export_kw');
dis   = dt?.dis_kwh    ?? kwhFromRows(rows, 'discharge_kw');
chg   = dt?.charge_kwh ?? kwhFromRows(rows, 'charge_kw');
imp   =                   kwhFromRows(rows, 'import_kw');  // no counter
```

## Cost formula
- Import: `(spot + nätavgift + energiskatt + fortumPåslag) × moms`
- Export: `spot + nätnytta` (no moms)
- Fixed daily: `(fastAvgift + fortumFast) × moms / 30`

## Chart.js patterns
- `upsertChart(canvasId, key, config)` — destroys previous chart before creating new one.
- `chart.update({ duration: 300, easing: 'easeInOutQuart' })` for animated axis rescaling.
- Y-axis autoscales to visible datasets when `setDatasetVisibility()` is used — no explicit max needed.
