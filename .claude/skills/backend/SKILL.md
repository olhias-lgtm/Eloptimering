---
name: backend
description: "Work on Python backend files for the electricity dashboard: _growatt.py, _schema.py, and any api/*.py endpoint. Use when adding or modifying data collection logic, Growatt API calls, Supabase writes, or API response shapes. Triggers: 'fix the collect endpoint', 'add a new API field', 'change session handling', 'update _schema.py'. Do NOT use for index.html or Supabase migrations."
---

# Backend Skill — Electricity Dashboard

## Before making changes
Read `_schema.py` — it defines the data contract. Any new DB column belongs there first.

## Session / Growatt rules
- `SESSION_MAX_AGE_HOURS = 0.75` — leave it. Growatt's server TTL is ~1h.
- Both `get_live()` and `get_energy()` use a 2-attempt retry loop with `_session_expired()` detection. New Growatt call methods must follow the same pattern.
- `_looks_like_html(resp)` detects login-page redirects (HTML response instead of JSON).
- `_handle_expiry()`: login first, then `_clear_stored_session()` — never reverse the order.

## Schema contract
When adding a new DB field:
1. Add it to `CHART_FIELD_MAP` (if available from chart API) or `CHART_NULL_FIELDS` (if not).
2. Add it to `DAILY_TOTALS_FIELDS` if it's a reliable inverter counter suitable for KPI totals.
3. Add the column read in `api/energy.py`'s select query.

## Supabase write patterns
- Live rows (collect cron): plain INSERT via `_sb_insert()`, no conflict handling needed (ts is unique per cron fire).
- Chart rows (backfill): `Prefer: resolution=ignore-duplicates` — never overwrites live rows.
- Session row: `Prefer: resolution=merge-duplicates` — single row, always upsert.

## Cron path (collect)
Returns HTTP 200 with `{"ok": false, "error": "..."}` on Growatt errors — cron-job.org treats non-200 as down. Do not change this behaviour.

## Field unit reference
| Source | Unit | Conversion |
|--------|------|-----------|
| `getTlxDetailData` power fields | Watts | ÷ 1000 → kW |
| `getSystemStatus_KW` | kW | none |
| `getEnergyOverview` energy fields | kWh | none |
| Chart API values | raw (÷10 = kW) | already applied in `_normalize_tlx` |
