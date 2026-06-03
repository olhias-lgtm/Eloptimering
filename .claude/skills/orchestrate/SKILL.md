---
name: orchestrate
description: "Orchestrates multi-agent feature development on the electricity dashboard. Use when a task touches more than one axis (backend Python, frontend HTML, or infra/Supabase/Vercel), or when you need to coordinate a backend schema change with a frontend UI update. Triggers: 'add a feature', 'fix this across the stack', 'implement X end-to-end', 'this needs a DB change and a UI change'. Do NOT trigger for single-file edits or quick fixes — handle those directly."
---

# Electricity Dashboard — Orchestrator Skill

## Phase 0: Context check
1. Check for `.claude/_workspace/` — prior run artifacts. If present and user requests a partial update, reuse; otherwise rename to `_workspace_prev/`.
2. Confirm axes involved: backend / frontend / infra (can be one, two, or all three).

## Phase 1: Decompose
Write a one-paragraph brief per axis. Each brief must state:
- Which files to touch
- What the expected output is (new field, changed behaviour, migration SQL)
- Any dependency on another axis (e.g. "infra must add column X before backend can read it")

Save briefs to `_workspace/briefs.md`.

## Phase 2: Fan-out
Spawn agents for affected axes. Independent axes run in parallel (`run_in_background: true`).

**If infra changes are needed**: spawn infra-agent first (or at minimum ensure the migration is applied) before spawning backend-agent, since the backend code must compile against the real schema.

Agent definitions are in `.claude/agents/`. Pass the relevant brief as the prompt.

## Phase 3: Fan-in & integration check
Collect results. Run these cross-axis checks:
- If infra added a column: verify backend reads it via `_schema.py`, and frontend consumes it.
- If backend changed an API response field: verify frontend uses the new field name.
- If frontend adds a `saveDailySummary` call: verify the guard `dateStr < todayStr()` is present.

## Phase 4: Commit & deploy
```bash
git add <changed files>
git commit -m "<conventional commit message>"
git push   # GitHub Actions deploys automatically
```
If the change is urgent: `vercel deploy --prod --yes`

## Test scenarios
**Normal flow:** User asks for a new KPI that requires a new DB column + backend endpoint + frontend display.
- infra-agent adds column + migration
- backend-agent adds to `_schema.py` + reads in `api/energy.py`
- frontend-agent adds to `renderKPIs()`
- Integration check: column name consistent across all three files

**Error flow:** infra-agent migration fails (duplicate constraint).
- Report error to user, do not proceed with backend/frontend changes until schema is confirmed.
