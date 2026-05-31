import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler
from _growatt import get_session

_CACHE: dict = {}
_TTL  = 600  # 10 minutes

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def _sb_get(date_str: str):
    """Fetch chart data for a past date from Supabase. Returns dict or None."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        url = f"{SUPABASE_URL}/rest/v1/energy_chart?date=eq.{date_str}&select=chart_data"
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
            if rows:
                return rows[0]["chart_data"]
    except Exception as e:
        print(f"[energy] supabase read error: {e}")
    return None


def _sb_upsert(date_str: str, data: dict):
    """Save/overwrite chart data for a date in Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        body = json.dumps({"date": date_str, "chart_data": data}).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/energy_chart",
            data=body,
            method="POST",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[energy] supabase write error: {e}")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        target = params.get("date", date.today().isoformat())
        today  = date.today().isoformat()
        is_today = (target == today)

        # In-memory cache (valid for current Lambda instance)
        cached = _CACHE.get(target)
        if cached and (time.monotonic() - cached["ts"]) < _TTL:
            self._send(cached["data"])
            return

        # Past date → serve from Supabase only (Growatt ignores date param)
        if not is_today:
            sb = _sb_get(target)
            if sb:
                _CACHE[target] = {"ts": time.monotonic(), "data": sb}
                self._send(sb)
            else:
                self._send({"error": f"No data stored for {target}"}, 404)
            return

        # Today → fetch from Growatt, persist to Supabase
        try:
            s   = get_session()
            raw = s.get_energy(target)
            _CACHE[target] = {"ts": time.monotonic(), "data": raw}
            # Persist today's snapshot so it becomes available as history tomorrow
            obj = raw.get("obj", {})
            if obj.get("chartData"):
                _sb_upsert(target, raw)
            self._send(raw)
        except Exception as e:
            print(f"[energy] {e}")
            if cached:
                self._send(cached["data"])
            else:
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
