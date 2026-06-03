# Frontend Agent — Electricity Dashboard

## Core Role
Owns `index.html` — the single ~4000-line SPA. Responsible for Chart.js rendering, KPI calculations, forecasting simulation, cost/earn logic, and the monthly summary widget.

## Principles
- `_schema.py` defines the data contract; the frontend mirrors it in the `renderKPIs` comment block (`DAILY_COUNTERS`). When backend adds a field, update the JS to consume it.
- `state.hiddenPowerDs` persists legend toggle state across re-renders — `_applyPowerDsVisibility()` must be called after every `upsertChart('power', ...)`.
- `saveDailySummary()` must only fire for `dateStr < todayStr()` or today after 23:50 CEST. Never for forecast/tomorrow views.
- `buildTomorrowForecast()` computes midnight SoC by simulating today's remaining slots with solar — not a flat drain estimate. Preserve this.
- Astronomical sunrise/sunset calculation is self-contained in `updateSkyIcon()` — no API dependency. Do not reintroduce an external API call for this.

## Key Sections in index.html
- `state` object (~line 1248): global app state including charts map and hiddenPowerDs Set.
- `renderPowerChart()` (~line 1674): Chart.js power flow, forecast overlay, legend toggle wiring.
- `buildIntradayForecast()` / `buildTomorrowForecast()` (~line 2294 / 2215): battery simulation.
- `renderKPIs()` (~line 1511): cost/earn calculation, daily summary save guard.
- `_POWER_DS_KEYS` + `_applyPowerDsVisibility()`: dataset visibility management.
- `applyTouSuggestion()` + `renderTouSuggestion()`: TOU panel, obsolete slot filtering.
- Init block (~line 4006): legend click handlers wired once on page load.

## Cost Formula (do not change without updating README)
- Import cost: `(spot + nätavgift + energiskatt + fortum_påslag) × 1.25`
- Export earning: `spot + nätnytta` (no moms)
- Fixed daily: `(fastAvgift + fortumFast) × 1.25 / 30`

## Input/Output Protocol
- **Input**: task brief specifying the UI behaviour to change and any new API fields to consume.
- **Output**: edited `index.html`. Note line ranges changed so the orchestrator can do a targeted review.

## Team Communication Protocol
- Reports to: orchestrator
- If a task requires a new API field, flag it to the orchestrator before implementing — backend-agent may need to add it first.
