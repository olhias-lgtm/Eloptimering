import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler
from _growatt import get_session

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def _fetch_cron_health() -> dict:
    """Return {cron_name: {last_run_at, last_ok_at, last_ok}} for all tracked crons."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}
    try:
        url = (f"{SUPABASE_URL}/rest/v1/cron_health"
               f"?select=cron_name,last_run_at,last_ok_at,last_ok,last_error")
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        return {row["cron_name"]: row for row in rows}
    except Exception as e:
        print(f"[status] cron_health fetch failed: {e}")
        return {}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        s = get_session()
        cron_health = _fetch_cron_health()
        body = json.dumps({
            "logged_in":   s.logged_in,
            "plant_id":    s.plant_id,
            "mix_serial":  s.mix_serial,
            "username":    os.environ.get("GROWATT_USER", ""),
            "env_ok":      bool(os.environ.get("GROWATT_USER") and os.environ.get("GROWATT_PASS")),
            "cron_health": cron_health,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
