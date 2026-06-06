"""Shared cron health reporter.

Call record_run(cron_name, ok, error=None) at the end of every cron handler.
Upserts a row in cron_health so the frontend can detect stale data.
Non-fatal — any Supabase error is silently swallowed so it never breaks a cron.
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def record_run(cron_name: str, ok: bool, error: str | None = None) -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "cron_name":   cron_name,
        "last_run_at": now,
        "last_ok":     ok,
        "last_error":  str(error)[:500] if error else None,
    }
    if ok:
        row["last_ok_at"] = now
    try:
        body = json.dumps(row).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/cron_health?on_conflict=cron_name",
            data=body, method="POST",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates,return=minimal",
            },
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[cron_health] record_run failed for {cron_name}: {e}")
