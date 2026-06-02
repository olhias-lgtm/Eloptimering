"""
Live data endpoint — returns the most recent energy_readings row from Supabase.

Previously called get_live() directly on Growatt (3 API calls per request).
Now reads the row the collect.py cron already writes every 5 minutes.
This eliminates ~4,000+ redundant Growatt API calls per day from frontend polling.

Falls back to a direct Growatt call only if Supabase has no data at all
(e.g. first-ever startup before the cron has run once).
"""
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

# Treat Supabase data as stale if the newest row is older than this.
# collect.py cron runs every 5 min; 10 min gives one missed cron before fallback.
MAX_AGE_MINUTES = 10


def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def _latest_from_supabase() -> dict | None:
    """Return the most recent energy_readings row, or None if absent/stale."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        url = (f"{SUPABASE_URL}/rest/v1/energy_readings"
               f"?soc_pct=not.is.null&order=ts.desc&limit=1&select=*")
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        if not rows:
            return None
        row = rows[0]
        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        if age_min > MAX_AGE_MINUTES:
            print(f"[live] Supabase row is {age_min:.1f} min old — falling back to Growatt")
            return None
        row["_source"] = "supabase"
        row["_age_min"] = round(age_min, 1)
        return row
    except Exception as e:
        print(f"[live] Supabase read error: {e}")
        return None


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Primary path: read from Supabase (no Growatt calls)
        data = _latest_from_supabase()
        if data:
            self._send(data)
            return

        # Fallback: direct Growatt call (only when Supabase has nothing fresh)
        try:
            from _growatt import get_session
            s    = get_session()
            data = s.get_live()
            data["_source"] = "growatt_fallback"
            self._send(data)
        except Exception as e:
            print(f"[live] Growatt fallback error: {e}")
            self._send({"error": str(e)}, 500)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
