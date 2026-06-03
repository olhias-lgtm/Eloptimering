"""
Autofill endpoint — detects and fills gaps in energy_readings for recent days.

Called by Vercel cron every 2 hours and by cron-job.org for external monitoring.

Logic per day:
  1. Count live rows (soc_pct IS NOT NULL) for that CEST day.
  2. Compute expected live rows up to now (for today) or full day (past days).
  3. If live coverage < GAP_THRESHOLD, fetch chart data from Growatt and
     upsert chart rows for the missing slots (ignore-duplicates keeps live rows safe).

GET /api/autofill               → check + fill last 2 days
GET /api/autofill?days=N        → check + fill last N days (max 7)
GET /api/autofill?dry_run=1     → report gaps without writing
"""
import json
import os
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

# Reuse collect helpers (same package directory)
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from api.collect import _do_historical, _cest_offset

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

# A day is considered "gappy" if fewer than this fraction of expected
# live slots are present.  0.85 = tolerate up to 15% missing.
GAP_THRESHOLD = 0.85

# Minimum gap to trigger autofill (minutes). Avoids reacting to single missed crons.
MIN_GAP_MINUTES = 15


def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def _count_live_rows(date_str: str) -> int:
    """Count live rows (soc_pct IS NOT NULL) for a CEST date."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    d = date.fromisoformat(date_str)
    utc_offset_h = _cest_offset(d)
    tz_local = timezone(timedelta(hours=utc_offset_h))
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz_local).isoformat()
    end   = (datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz_local)
             + timedelta(minutes=5)).isoformat()
    url = (
        f"{SUPABASE_URL}/rest/v1/energy_readings"
        f"?ts=gte.{urllib.parse.quote(start)}"
        f"&ts=lte.{urllib.parse.quote(end)}"
        f"&soc_pct=not.is.null"
        f"&select=ts"
        f"&limit=300"
    )
    try:
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read())
        return len(rows)
    except Exception as e:
        print(f"[autofill] count_live_rows error for {date_str}: {e}")
        return 0


def _expected_live_slots(date_str: str) -> int:
    """
    Expected number of live cron rows for a given CEST date.
    For today: slots from 00:00 to now.
    For past dates: full 288 slots (cron fires ~every 5 min = ~288/day).
    """
    d = date.fromisoformat(date_str)
    utc_offset_h = _cest_offset(d)
    tz_local = timezone(timedelta(hours=utc_offset_h))
    today_local = datetime.now(timezone.utc).astimezone(tz_local).date()

    if d < today_local:
        return 288
    # Today: slots up to now
    now_local = datetime.now(timezone.utc).astimezone(tz_local)
    elapsed_min = now_local.hour * 60 + now_local.minute
    return max(1, elapsed_min // 5)


def _needs_fill(date_str: str) -> tuple[bool, int, int]:
    """Returns (needs_fill, live_count, expected_count)."""
    live = _count_live_rows(date_str)
    expected = _expected_live_slots(date_str)
    needs = live < expected * GAP_THRESHOLD
    return needs, live, expected


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))

        dry_run = params.get("dry_run", "0") in ("1", "true", "yes")
        try:
            days = min(7, max(1, int(params.get("days", "2"))))
        except ValueError:
            days = 2

        utc_offset_h = _cest_offset(datetime.now(timezone.utc).date())
        tz_local = timezone(timedelta(hours=utc_offset_h))
        today_local = datetime.now(timezone.utc).astimezone(tz_local).date()

        results = []
        filled_dates = []

        for i in range(days):
            target = today_local - timedelta(days=i)
            date_str = target.isoformat()

            needs, live, expected = _needs_fill(date_str)
            missing = expected - live
            entry = {
                "date":          date_str,
                "live_rows":     live,
                "expected":      expected,
                "missing":       missing,
                "needs_fill":    needs,
            }

            if needs:
                print(f"[autofill] {date_str}: {live}/{expected} live rows, gap={missing} slots — {'dry run' if dry_run else 'filling'}")
                if not dry_run:
                    status, resp = _do_historical(date_str, confirm=True)
                    entry["fill_status"]  = status
                    entry["fill_written"] = resp.get("written", 0)
                    entry["fill_error"]   = resp.get("error") if status != 200 else None
                    if status == 200:
                        filled_dates.append(date_str)
                else:
                    entry["dry_run"] = True
            else:
                print(f"[autofill] {date_str}: {live}/{expected} live rows — OK, no fill needed")

            results.append(entry)

        body = json.dumps({
            "ok":           True,
            "dry_run":      dry_run,
            "days_checked": days,
            "filled":       filled_dates,
            "results":      results,
        }).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
