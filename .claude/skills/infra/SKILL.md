---
name: infra
description: "Work on Supabase schema, RLS policies, Vercel configuration, and deployment for the electricity dashboard. Use when adding DB tables/columns, writing migrations, fixing RLS policies, changing cron schedules, or managing environment variables. Triggers: 'add a table', 'write a migration', 'fix RLS', 'add a cron', 'deploy', 'change vercel.json'. Do NOT use for Python or JavaScript code changes."
---

# Infra Skill — Electricity Dashboard

## Supabase
**Project ID:** `yiqmtsczgshfltcutuhq`
**MCP tool:** `mcp__829a98f6-34c6-4ba8-89d4-a27ed1558428__apply_migration` for DDL, `execute_sql` for DML/queries.

### RLS checklist for every new table
```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read"   ON <table> FOR SELECT USING (true);
CREATE POLICY "public insert" ON <table> FOR INSERT WITH CHECK (true);
CREATE POLICY "public update" ON <table> FOR UPDATE USING (true);
CREATE POLICY "public delete" ON <table> FOR DELETE USING (true);
```
Missing any of these causes silent write failures from the anon key — the most common infra bug.

### energy_readings constraints
- `UNIQUE(ts)` — present. `ignore-duplicates` upserts work correctly.
- Live rows: `soc_pct IS NOT NULL`. Chart rows: `soc_pct IS NULL`.
- Never add `merge-duplicates` to the backfill path — it would overwrite live rows.

### daily_summary
- Primary key column is `day` (DATE), not `date`.
- Written by the frontend only when `dateStr < today` (completed days). Never for forecast rows.

## Vercel
**Deployment:** GitHub Actions (`deploy.yml`) on every push to main.
```bash
vercel deploy --prod --yes   # manual override
vercel env pull              # sync env to local
```
Hardcoded in workflow: `VERCEL_ORG_ID=team_Sfc7y9iKuMifBSMrJoPsrIC1`, `VERCEL_PROJECT_ID=prj_U8jMeCOh2zNnedzqbvVPenA7YQVL`.

### Adding a Vercel cron
1. Add entry to `vercel.json` `crons` array (schedule in UTC).
2. Ensure the matching `api/<name>.py` handler exists with a `class handler(BaseHTTPRequestHandler)`.

### Env vars
All sensitive values live in Vercel (not in repo). To add one:
```bash
vercel env add <NAME> production
```
Reference in Python: `os.environ.get("<NAME>", "")`.

## Migration naming
Use `snake_case` descriptive names: `add_unique_constraint_energy_readings_ts`, `fix_missing_rls_policies`, etc. Supabase tracks these and won't re-run them.
