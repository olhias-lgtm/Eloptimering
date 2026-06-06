"""
prices — GET ?date=YYYY-MM-DD&area=SE3

Returns {today: [...], tomorrow: [...]} spot price arrays compatible with
the elprisetjustnu.se format {time_start, SEK_per_kWh}.

Primary source: elprisetjustnu.se (live, ~0 latency).
Fallback:       Supabase spot_prices table (populated by save_prices cron).

If the external API is down or returns no data for a date, we serve whatever
is already stored in Supabase — so the dashboard keeps working as long as the
last successful cron ran (typically within the last 24 h).
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

ELPRISET_BASE = "https://www.elprisetjustnu.se/api/v1/prices"
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_ANON_KEY", "")


def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _fetch_live(date_str: str, area: str) -> list:
    """Fetch from elprisetjustnu.se. Returns [] on any failure."""
    y, mo, d = date_str.split("-")
    url = f"{ELPRISET_BASE}/{y}/{mo}-{d}_{area}.json"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "electricity-dashboard/prices"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[prices] live fetch failed for {date_str}: {e}")
        return []


def _fetch_stored(date_str: str, area: str) -> list:
    """Read from Supabase spot_prices. Returns [] if unavailable."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    # Query the full UTC day that covers the requested CEST date.
    # spot_prices.ts is stored as the original time_start ISO string.
    # Filter by ts starting with the date string to match CEST-local rows.
    try:
        # Use gte/lt on the date prefix — works for both UTC and +02:00 offsets
        d_next = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
        url = (
            f"{SUPABASE_URL}/rest/v1/spot_prices"
            f"?area=eq.{area}"
            f"&ts=gte.{date_str}T00:00:00"
            f"&ts=lt.{d_next}T00:00:00"
            f"&order=ts.asc"
            f"&select=ts,sek_per_kwh"
        )
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read())
        if not rows:
            return []
        # Re-shape to match elprisetjustnu.se format expected by the frontend
        return [{"time_start": row["ts"], "SEK_per_kWh": float(row["sek_per_kwh"])}
                for row in rows]
    except Exception as e:
        print(f"[prices] Supabase fallback failed for {date_str}: {e}")
        return []


def _get_prices(date_str: str, area: str) -> tuple[list, str]:
    """Return (prices, source) — tries live first, falls back to stored."""
    prices = _fetch_live(date_str, area)
    if prices:
        return prices, "live"
    prices = _fetch_stored(date_str, area)
    if prices:
        print(f"[prices] serving {date_str} from Supabase fallback ({len(prices)} slots)")
        return prices, "supabase"
    return [], "none"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        area   = params.get("area", "SE3")
        ds     = params.get("date", date.today().isoformat())

        tomorrow = (date.fromisoformat(ds) + timedelta(days=1)).isoformat()

        today_prices,    src_today    = _get_prices(ds,       area)
        tomorrow_prices, src_tomorrow = _get_prices(tomorrow, area)

        self._send({
            "today":    today_prices,
            "tomorrow": tomorrow_prices,
            "_sources": {"today": src_today, "tomorrow": src_tomorrow},
        })

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
