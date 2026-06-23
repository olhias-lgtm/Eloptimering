"""
monthly — GET ?year=YYYY&month=MM  |  GET ?action=roi

Changes vs original:
  • ROI query now uses PostgREST aggregate select (sum on server) instead of
    fetching every row and summing in Python — one small JSON response instead
    of a full table scan transferred over the wire.
  • Both ROI and per-month results are cached in-process:
      - ROI: 5-minute TTL (updates once a day at most, but keep reasonably fresh)
      - Past months: indefinite (data never changes once the month is over)
      - Current month: 5-minute TTL
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

_ROI_CACHE:   dict = {"data": None, "ts": 0.0}
_ROI_TTL      = 5 * 60  # 5 minutes

_MONTH_CACHE: dict = {}  # key: "YYYY-MM" → {"data": [...], "ts": float}
_MONTH_TTL    = 5 * 60  # 5 minutes for the current month; ∞ for past months


def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _fetch_roi_total() -> dict:
    """All-time ROI using server-side aggregation (no full table transfer).
    PostgREST aggregate: ?select=export_earn_kr.sum(),saved_kr.sum(),day.min(),day.max(),day.count()
    Returns a single-element array with the aggregated values.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}

    # Check in-process cache first
    now = time.monotonic()
    if _ROI_CACHE["data"] and now - _ROI_CACHE["ts"] < _ROI_TTL:
        return _ROI_CACHE["data"]

    headers = _sb_headers()
    result: dict = {}

    # Single aggregate query — Postgres does the SUM, not Python
    url = (
        f"{SUPABASE_URL}/rest/v1/daily_summary"
        f"?select=export_earn_kr.sum(),saved_kr.sum(),day.min(),day.max(),day.count()"
    )
    req = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
        row = rows[0] if rows else {}
        earn  = float(row.get("sum") or row.get("export_earn_kr") or 0)
        # PostgREST returns multiple .sum() columns as a list — handle both shapes
        sums = [v for k, v in row.items() if k == "sum"]
        if len(sums) == 2:
            earn  = float(sums[0] or 0)
            saved = float(sums[1] or 0)
        else:
            # Fallback: PostgREST may alias them differently — try named keys
            earn  = float(row.get("export_earn_kr") or 0)
            saved = float(row.get("saved_kr")       or 0)
        result = {
            "total_roi_kr": round(earn + saved, 2),
            "earn_kr":      round(earn,  2),
            "saved_kr":     round(saved, 2),
            "day_count":    int(row.get("count") or 0),
            "first_day":    row.get("min"),
            "last_day":     row.get("max"),
        }
    except Exception as e:
        print(f"[monthly roi] aggregate query failed: {e}")
        return {}

    # Lifetime battery throughput — separate RPC (non-fatal)
    try:
        batt_req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/rpc/get_lifetime_battery_kwh",
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

    _ROI_CACHE["data"] = result
    _ROI_CACHE["ts"]   = now
    return result


def _fetch_month(year: int, month: int) -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []

    cache_key  = f"{year}-{month:02d}"
    today      = date.today()
    is_current = (year == today.year and month == today.month)
    now        = time.monotonic()

    cached = _MONTH_CACHE.get(cache_key)
    if cached:
        age = now - cached["ts"]
        if not is_current or age < _MONTH_TTL:
            return cached["data"]

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
    try:
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        _MONTH_CACHE[cache_key] = {"data": data, "ts": now}
        return data
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

        raw = _fetch_month(year, month)
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
