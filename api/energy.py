import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

_CACHE: dict = {}
_TTL = 300  # 5 minutes for today; past dates cached indefinitely


def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def _fetch_readings(date_str: str) -> list:
    """Fetch all energy_readings rows for a given date (CEST = UTC+2)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        # CEST day boundaries in UTC
        day   = date.fromisoformat(date_str)
        start = datetime(day.year, day.month, day.day, 0, 0, 0,
                         tzinfo=timezone(timedelta(hours=2))).isoformat()
        end   = datetime(day.year, day.month, day.day, 23, 59, 59,
                         tzinfo=timezone(timedelta(hours=2))).isoformat()
        url = (
            f"{SUPABASE_URL}/rest/v1/energy_readings"
            f"?ts=gte.{urllib.parse.quote(start)}"
            f"&ts=lte.{urllib.parse.quote(end)}"
            f"&order=ts.asc"
            f"&select=ts,ppv_kw,load_kw,export_kw,import_kw,charge_kw,discharge_kw"
        )
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[energy] supabase fetch error: {e}")
        return []


def _bucket_readings(rows: list, date_str: str) -> dict:
    """
    Aggregate per-row readings into 5-minute chartData buckets (CEST time labels).
    Each bucket averages power values across all readings that fall in that slot.
    Returns a dict keyed by "HH:MM" covering 00:00–23:55.
    """
    SLOT_MIN = 5
    total_slots = (24 * 60) // SLOT_MIN  # 288

    empty = lambda: {"ppv": 0.0, "load": 0.0, "export": 0.0,
                     "import": 0.0, "charge": 0.0, "discharge": 0.0}

    buckets = {}
    counts  = {}
    for j in range(total_slots):
        label = f"{(j * SLOT_MIN) // 60:02d}:{(j * SLOT_MIN) % 60:02d}"
        buckets[label] = empty()
        counts[label]  = 0

    tz_cest = timezone(timedelta(hours=2))
    for row in rows:
        ts_str = row["ts"]
        # Parse ISO timestamp (may have +00:00 or Z suffix)
        ts_str = ts_str.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(ts_str).astimezone(tz_cest)
        except Exception:
            continue
        # Round down to 5-min slot
        slot_min = (ts.hour * 60 + ts.minute) // SLOT_MIN * SLOT_MIN
        label = f"{slot_min // 60:02d}:{slot_min % 60:02d}"
        if label not in buckets:
            continue
        b = buckets[label]
        b["ppv"]       += float(row.get("ppv_kw")      or 0)
        b["load"]      += float(row.get("load_kw")     or 0)
        b["export"]    += float(row.get("export_kw")   or 0)
        b["import"]    += float(row.get("import_kw")   or 0)
        b["charge"]    += float(row.get("charge_kw")   or 0)
        b["discharge"] += float(row.get("discharge_kw") or 0)
        counts[label]  += 1

    # Average each bucket
    for label in buckets:
        n = counts[label]
        if n > 1:
            b = buckets[label]
            for k in b:
                b[k] = round(b[k] / n, 3)
        elif n == 1:
            b = buckets[label]
            for k in b:
                b[k] = round(b[k], 3)

    # Truncate future slots when viewing today
    today_str = date.today().isoformat()
    if date_str == today_str:
        cest_now = datetime.now(timezone.utc).astimezone(tz_cest)
        cutoff   = cest_now.hour * 60 + cest_now.minute + SLOT_MIN  # grace of one slot
        for label in list(buckets.keys()):
            h, m = map(int, label.split(":"))
            if h * 60 + m > cutoff:
                buckets[label] = empty()

    return buckets


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed   = urllib.parse.urlparse(self.path)
        params   = dict(urllib.parse.parse_qsl(parsed.query))
        date_str = params.get("date", date.today().isoformat())
        today    = date.today().isoformat()
        is_today = (date_str == today)

        # Cache: past dates indefinitely, today for 5 min
        cached = _CACHE.get(date_str)
        if cached:
            age = time.monotonic() - cached["ts"]
            if not is_today or age < _TTL:
                self._send(cached["data"])
                return

        rows = _fetch_readings(date_str)

        if not rows:
            if is_today:
                # No readings yet today — return empty chart (normal early morning)
                empty_cd = _bucket_readings([], date_str)
                result   = {"obj": {"chartData": empty_cd}, "source": "empty"}
                _CACHE[date_str] = {"ts": time.monotonic(), "data": result}
                self._send(result)
            else:
                self._send({"error": f"No data stored for {date_str}"}, 404)
            return

        chart_data = _bucket_readings(rows, date_str)
        result     = {"obj": {"chartData": chart_data}, "source": "supabase"}
        _CACHE[date_str] = {"ts": time.monotonic(), "data": result}
        self._send(result)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
