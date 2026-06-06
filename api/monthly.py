"""
monthly — GET ?year=YYYY&month=MM
Returns all daily_summary rows for the requested calendar month.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def _fetch_roi_total() -> dict:
    """All-time sum of export_earn_kr + saved_kr from daily_summary,
    plus lifetime battery throughput from energy_readings via RPC."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    url = (
        f"{SUPABASE_URL}/rest/v1/daily_summary"
        f"?select=export_earn_kr,saved_kr,day"
        f"&order=day.asc"
    )
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
        total_earn  = sum(float(r.get("export_earn_kr") or 0) for r in rows)
        total_saved = sum(float(r.get("saved_kr")       or 0) for r in rows)
        result = {
            "total_roi_kr": round(total_earn + total_saved, 2),
            "earn_kr":      round(total_earn,  2),
            "saved_kr":     round(total_saved, 2),
            "day_count":    len(rows),
            "first_day":    rows[0]["day"]  if rows else None,
            "last_day":     rows[-1]["day"] if rows else None,
        }
    except Exception as e:
        print(f"[monthly roi] {e}")
        return {}

    # Lifetime battery throughput — separate RPC query (non-fatal if unavailable)
    try:
        batt_url = f"{SUPABASE_URL}/rest/v1/rpc/get_lifetime_battery_kwh"
        batt_req = urllib.request.Request(
            batt_url,
            data=b"{}",
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(batt_req, timeout=10) as r:
            batt = json.loads(r.read())
        if batt:
            b = batt[0] if isinstance(batt, list) else batt
            result["batt_charge_kwh"]    = float(b.get("total_charge_kwh")    or 0)
            result["batt_discharge_kwh"] = float(b.get("total_discharge_kwh") or 0)
            result["batt_day_count"]     = int(b.get("day_count")             or 0)
    except Exception as e:
        print(f"[monthly roi battery] {e}")

    return result


def _fetch_month(year: int, month: int) -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    first = f"{year}-{month:02d}-01"
    last  = f"{year}-{month:02d}-{last_day:02d}"
    url = (
        f"{SUPABASE_URL}/rest/v1/daily_summary"
        f"?day=gte.{first}"
        f"&day=lte.{last}"
        f"&order=day.asc"
        f"&select=day,solar_kwh,export_kwh,import_kwh,import_cost_kr,export_earn_kr,fixed_cost_kr,net_kr,saved_kr"
    )
    req = urllib.request.Request(url, headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[monthly] fetch error: {e}")
        return []


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query
        ))
        today = date.today()
        try:
            year  = int(params.get("year",  today.year))
            month = int(params.get("month", today.month))
        except ValueError:
            self._send({"error": "invalid year/month"}, 400)
            return
        if params.get("action") == "roi":
            self._send(_fetch_roi_total())
            return

        raw  = _fetch_month(year, month)
        # Normalise column names to what the frontend expects
        rows = [
            {
                "date":       r.get("day"),
                "solar_kwh":  r.get("solar_kwh"),
                "export_kwh": r.get("export_kwh"),
                "import_kwh": r.get("import_kwh"),
                "cost_kr":    r.get("import_cost_kr"),
                "earn_kr":    r.get("export_earn_kr"),
                "fixed_kr":   r.get("fixed_cost_kr"),
                "net_kr":     r.get("net_kr"),
                "saved_kr":   r.get("saved_kr"),
            }
            for r in raw
        ]
        self._send(rows)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
