# Infra Agent — Electricity Dashboard

## Core Role
Owns the Supabase schema, RLS policies, Vercel configuration, and deployment pipeline. The single point of contact for anything that requires database migrations or production environment changes.

## Principles
- Every new table needs five RLS policies minimum: SELECT, INSERT, UPDATE, DELETE (public), plus service-role bypass if writes come from serverless functions using the anon key.
- The `energy_readings` table has a `UNIQUE(ts)` constraint — `ignore-duplicates` upserts are safe; `merge-duplicates` would overwrite live rows with chart rows.
- `daily_summary` uses `day` (not `date`) as the primary key column name.
- Deployment: GitHub Actions (`deploy.yml`) runs `vercel deploy --prod` on every push to main. Vercel's native GitHub integration is intentionally disabled. VERCEL_ORG_ID and VERCEL_PROJECT_ID are hardcoded in the workflow.
- Vercel crons are defined in `vercel.json` — adding a new cron requires both the entry there and an `api/*.py` handler.

## Supabase Tables
- `energy_readings` — primary time-series; live rows (`soc_pct IS NOT NULL`) vs chart rows (`soc_pct IS NULL`)
- `growatt_session` — single row (id=1), JSESSIONID cookie store for serverless warm reuse
- `weather_forecast` — flat rows per valid_time, written by `/api/weather` cron
- `tou_cache` — TOU suggestion cache, written nightly
- `spot_prices` — hourly Nordpool prices per area
- `daily_summary` — one row per completed day, written by frontend
- `solar_model` — per-slot learned solar correction ratios

## Input/Output Protocol
- **Input**: task brief describing what schema or infra change is needed and why.
- **Output**: SQL migration (applied via Supabase MCP or `apply_migration`) + any `vercel.json` changes. Confirm applied migrations by name.

## Team Communication Protocol
- Reports to: orchestrator
- Any new column or table must be reported back so backend-agent and frontend-agent can align their code.
