"""
save_prices — GET ?date=YYYY-MM-DD&area=SE3
Fetches spot prices from elprisetjustnu.se for the given date and upserts
each price interval into the spot_prices table (ts, area, sek_per_kwh).
Safe to call repeatedly — upserts on (ts, area).
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from _cron_health import record_run

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_ANON_KEY", "")
ELPRISET_BASE = "https://www.elprisetjustnu.se/api/v1/prices"


def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _fetch_prices(date_str: str, area: str) -> list:
    y, mo, d = date_str.split("-")
    url = f"{ELPRISET_BASE}/{y}/{mo}-{d}_{area}.json"
    req = urllib.request.Request(
        url, headers={"User-Agent": "electricity-dashboard/save_prices"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _upsert_prices(prices: list, area: str):
    """Batch-upsert all price rows for the day."""
    rows = [
        {"ts": p["time_start"], "area": area, "sek_per_kwh": p["SEK_per_kWh"]}
        for p in prices
    ]
    body = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/spot_prices?on_conflict=ts,area",
        data=body, method="POST",
        headers={
            **_sb_headers(),
            "Content-Type": "application/json",
            "Prefer":       "resolution=merge-duplicates,return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=10).read()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))

        tz_cest  = timezone(timedelta(hours=2))
        today    = datetime.now(timezone.utc).astimezone(tz_cest).date()
        tomorrow = (today + timedelta(days=1)).isoformat()

        raw_date = params.get("date", today.isoformat())
        # Accept "tomorrow" as a convenience alias
        date_str = tomorrow if raw_date == "tomorrow" else raw_date
        area     = params.get("area", "SE3")

        try:
            prices = _fetch_prices(date_str, area)
            if not prices:
                record_run("save_prices", ok=False, error="no prices returned")
                self._send({"ok": False, "cron_summary": f"FAILED: no prices returned for {date_str}",
                            "error": "no prices returned"}, 404)
                return
            _upsert_prices(prices, area)
            record_run("save_prices", ok=True)
            self._send({"ok": True,
                        "cron_summary": f"saved {len(prices)} slots for {date_str} ({area})",
                        "date": date_str, "area": area, "slots": len(prices)})
        except Exception as e:
            print(f"[save_prices] {date_str}: {e}")
            record_run("save_prices", ok=False, error=str(e))
            self._send({"ok": False, "cron_summary": f"EXCEPTION: {e}", "error": str(e)}, 500)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
