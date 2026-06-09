"""
save_summary — called by the frontend after computing daily cost/earn.
POST JSON {date, solar_kwh, load_kwh, export_kwh, import_kwh, cost_kr, earn_kr, fixed_kr, net_kr}
Upserts to daily_summary via Supabase REST.
Always returns 200.

Server-side guard: refuses writes for today (CEST) or future dates.
Only completed past days are allowed, so partial intraday data can never
pollute the monthly table even if the client-side guard is bypassed
(e.g. stale browser cache, browser timezone mismatch).
"""
import json
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def _is_past_day(date_str: str) -> bool:
    """Return True only if date_str is strictly before today in CEST (Stockholm)."""
    try:
        target = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return False
    tz_cest = timezone(timedelta(hours=2))  # close enough; DST handled below
    # Use Europe/Stockholm via a fixed +2/+1 offset based on month (approximate)
    # For precision: DST in effect March last Sunday → October last Sunday
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset = 2 if 3 < month < 11 or (month == 3 and now_utc.day >= 25) or (month == 10 and now_utc.day < 25) else 1
    today_local = now_utc.astimezone(timezone(timedelta(hours=offset))).date()
    return target < today_local


def _upsert(payload: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase env vars not set")
    body = json.dumps({
        "day":            payload.get("date"),
        "area":           payload.get("area", "SE3"),
        "solar_kwh":      payload.get("solar_kwh"),
        "load_kwh":       payload.get("load_kwh"),
        "export_kwh":     payload.get("export_kwh"),
        "import_kwh":     payload.get("import_kwh"),
        "import_cost_kr": payload.get("cost_kr"),
        "export_earn_kr": payload.get("earn_kr"),
        "fixed_cost_kr":  payload.get("fixed_kr"),
        "net_kr":         payload.get("net_kr"),
        "saved_kr":       payload.get("saved_kr"),
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/daily_summary?on_conflict=day,area",
        data=body,
        method="POST",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates,return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=8).read()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length  = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length)) if length else {}
            date_str = payload.get("date", "")
            if not _is_past_day(date_str):
                # Refuse writes for today or future — partial data must never enter monthly table
                self._send({"ok": False, "skipped": True, "reason": f"{date_str} is not a completed past day"})
                return
            _upsert(payload)
            self._send({"ok": True})
        except Exception as e:
            print(f"[save_summary] error: {e}")
            self._send({"ok": False, "error": str(e)})

    # Allow OPTIONS preflight
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
