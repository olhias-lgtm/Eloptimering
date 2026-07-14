import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

from _schema import DAILY_TOTALS_FIELDS, ROW_TYPE_PRIORITY, row_type
from _tz import STHLM, local_today, local_day_bounds_utc

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

_CACHE: dict = {}
_TTL = 300  # 5 minutes for today; past dates cached indefinitely


def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


class SupabaseFetchError(Exception):
    """Raised when the Supabase query itself fails (network, HTTP, parse error) —
    distinct from a query that succeeds and simply returns zero rows. Callers
    must not treat this the same as "no data yet" (e.g. early morning before
    any readings have landed)."""


def _fetch_readings(date_str: str) -> list:
    """Fetch all energy_readings rows for a given local (Stockholm) date.
    Raises SupabaseFetchError on any failure — an empty list only ever means
    "the query succeeded and there are genuinely no rows for this date"."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SupabaseFetchError("Supabase env vars not set")
    try:
        # Local day boundaries in UTC
        day = date.fromisoformat(date_str)
        start_utc, end_utc = local_day_bounds_utc(day)
        start = start_utc.isoformat()
        # Extend end by 5 min: the cron fired at 00:00–00:04 local of the next day
        # represents the 23:55 slot of this day (after the -5 min lag correction).
        end   = (end_utc + timedelta(minutes=5)).isoformat()
        url = (
            f"{SUPABASE_URL}/rest/v1/energy_readings"
            f"?ts=gte.{urllib.parse.quote(start)}"
            f"&ts=lte.{urllib.parse.quote(end)}"
            f"&order=ts.asc,ppv_kw.desc.nullslast"
            f"&select=ts,ppv_kw,load_kw,export_kw,import_kw,charge_kw,discharge_kw,"
            f"soc_pct,{','.join(DAILY_TOTALS_FIELDS.values())}"
        )
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except SupabaseFetchError:
        raise
    except Exception as e:
        print(f"[energy] supabase fetch error: {e}")
        raise SupabaseFetchError(str(e)) from e


def _bucket_readings(rows: list, date_str: str) -> dict:
    """
    Aggregate per-row readings into 5-minute chartData buckets (CEST time labels).
    Each bucket averages power values across all readings that fall in that slot.
    Returns a dict keyed by "HH:MM" covering 00:00–23:55.
    """
    SLOT_MIN = 5
    total_slots = (24 * 60) // SLOT_MIN  # 288

    empty = lambda: {"ppv": 0.0, "load": 0.0, "export": 0.0,
                     "import": 0.0, "charge": 0.0, "discharge": 0.0, "soc": None}

    buckets = {}
    counts  = {}
    for j in range(total_slots):
        label = f"{(j * SLOT_MIN) // 60:02d}:{(j * SLOT_MIN) % 60:02d}"
        buckets[label] = empty()
        counts[label]  = 0

    # Per slot: prefer live cron rows (soc_pct not None) over chart-imported rows.
    # Chart rows have import=0 (pac_user empty) and discharge from epv3 (PV string 3,
    # not battery) — both wrong. Live rows carry accurate values from getTlxDetailData.
    # Within same source type, row closest to the 5-min boundary wins.
    priority = {label: (-1, float("inf")) for label in buckets}  # (is_live, offset_s)

    for row in rows:
        ts_str = row["ts"]
        ts_str = ts_str.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(ts_str).astimezone(STHLM)
        except Exception:
            continue
        # Live rows: Growatt API has ~5-min data lag — cron fires at :30 but
        # data reflects inverter state at :25. Shift back to align with Shinephone.
        # Chart rows are already corrected (SQL-shifted -5 min at import time).
        if row.get("soc_pct") is not None:
            ts = ts - timedelta(minutes=5)
        # After shift a row fired at 00:00–00:04 CEST would fall into the
        # previous day's 23:55 slot — drop it so it doesn't corrupt this day.
        if ts.date().isoformat() != date_str:
            continue
        slot_min = (ts.hour * 60 + ts.minute) // SLOT_MIN * SLOT_MIN
        label = f"{slot_min // 60:02d}:{slot_min % 60:02d}"
        if label not in buckets:
            continue

        is_live = ROW_TYPE_PRIORITY[row_type(row)]
        offset  = (ts.minute % SLOT_MIN) * 60 + ts.second
        prev_live, prev_offset = priority[label]
        if is_live < prev_live or (is_live == prev_live and offset >= prev_offset):
            continue

        priority[label] = (is_live, offset)
        b = buckets[label]
        b["ppv"]       = float(row.get("ppv_kw")       or 0)
        b["load"]      = float(row.get("load_kw")      or 0)
        b["export"]    = float(row.get("export_kw")    or 0)
        b["import"]    = float(row.get("import_kw")    or 0)
        b["charge"]    = float(row.get("charge_kw")    or 0)
        b["discharge"] = float(row.get("discharge_kw") or 0)
        soc = row.get("soc_pct")
        b["soc"] = float(soc) if soc is not None else None

    for label in buckets:
        b = buckets[label]
        for k in b:
            if k == "soc":
                b[k] = round(b[k], 1) if b[k] is not None else None
            else:
                b[k] = round(b[k], 3)

    # Interpolate SoC for chart/backfill slots that sit between two known values.
    # A linear interpolation is accurate enough — battery SoC changes smoothly
    # and the surrounding live rows provide reliable anchor points.
    labels_ordered = list(buckets.keys())  # already in 00:00..23:55 order
    soc_values = [buckets[l]["soc"] for l in labels_ordered]
    n = len(labels_ordered)
    for i, label in enumerate(labels_ordered):
        if soc_values[i] is not None:
            continue
        # Find nearest non-null SoC before and after this slot
        before_i = next((j for j in range(i - 1, -1, -1) if soc_values[j] is not None), None)
        after_i  = next((j for j in range(i + 1, n)      if soc_values[j] is not None), None)
        if before_i is not None and after_i is not None:
            span   = after_i - before_i
            weight = (i - before_i) / span
            interp = soc_values[before_i] + weight * (soc_values[after_i] - soc_values[before_i])
            buckets[label]["soc"] = round(interp, 1)
        elif before_i is not None:
            buckets[label]["soc"] = soc_values[before_i]   # trailing gap: hold last known
        # leading gap (no before): leave null — no anchor to extrapolate from

    # Truncate future slots when viewing today (local date — Vercel runs UTC)
    today_str = local_today().isoformat()
    if date_str == today_str:
        local_now = datetime.now(timezone.utc).astimezone(STHLM)
        cutoff   = local_now.hour * 60 + local_now.minute + SLOT_MIN  # grace of one slot
        for label in list(buckets.keys()):
            h, m = map(int, label.split(":"))
            if h * 60 + m > cutoff:
                buckets[label] = empty()

    return buckets


def _row_local_date(row: dict) -> str:
    ts_str = row.get("ts", "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str).astimezone(STHLM).date().isoformat()
    except Exception:
        return ""


def _daily_totals(rows: list, date_str: str) -> dict | None:
    """Return counter-based daily totals from the best live row, or None.

    Uses DAILY_TOTALS_FIELDS from _schema — only reliable counters are included.
    Unreliable counters (e.g. import_today with 0.10 kWh granularity) are
    excluded at the schema level, not here.

    The _fetch_readings window extends 5 minutes past midnight to catch late
    cron rows. We filter to the target local date so that the next-day midnight
    reset (epv_today drops back to 0) does not wipe the correct end-of-day value.

    The inverter resets epv_today at local midnight (within the first 5-10 min).
    The single pre-reset row (00:00–00:05, still carrying yesterday's counter)
    is detected via the >1 kWh drop and discarded.
    """
    anchor_col = DAILY_TOTALS_FIELDS.get("solar_kwh", "epv_today")
    best = None
    prev_val = None
    reset_detected = False
    for row in rows:
        if row_type(row) != "live":
            continue
        # Restrict to the target local date — the query window extends 5 min
        # into the next day; those rows belong to tomorrow, not today.
        if _row_local_date(row) != date_str:
            continue
        val = row.get(anchor_col)
        if val is None:
            continue
        fval = float(val)
        # Counter reset: epv_today drops >1 kWh = inverter started a new day.
        # Discard the single carry-over row from the previous day's counter.
        if prev_val is not None and fval < prev_val - 1.0:
            best = None
            reset_detected = True
        prev_val = fval
        if best is None or fval > float(best.get(anchor_col) or -1):
            best = row

    if best is None:
        return None

    # Pre-dawn guard: if no reset was detected and every live row on this date
    # has ppv_kw=0, the inverter hasn't reset yet (carry-over from yesterday).
    # Return None so the frontend falls back to kwhFromRows (0 solar).
    if not reset_detected:
        live_today = [
            r for r in rows if row_type(r) == "live"
            and _row_local_date(r) == date_str
        ]
        if live_today and all(float(r.get("ppv_kw") or 0) == 0 for r in live_today):
            return None

    def _f(col):
        v = best.get(col)
        return round(float(v), 2) if v is not None else None
    return {kpi: _f(col) for kpi, col in DAILY_TOTALS_FIELDS.items()}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed   = urllib.parse.urlparse(self.path)
        params   = dict(urllib.parse.parse_qsl(parsed.query))
        today    = local_today().isoformat()
        date_str = params.get("date", today)
        is_today = (date_str == today)

        # Cache: past dates indefinitely, today for 5 min
        cached = _CACHE.get(date_str)
        if cached:
            age = time.monotonic() - cached["ts"]
            if not is_today or age < _TTL:
                self._send(cached["data"])
                return

        try:
            rows = _fetch_readings(date_str)
        except SupabaseFetchError as e:
            print(f"[energy] fetch failed for {date_str}: {e}")
            if cached:
                # Serve the last good response rather than a fake "zero solar"
                # chart — a brief Supabase blip should not look like a real
                # zero-production day on the dashboard.
                stale = dict(cached["data"])
                stale["stale"] = True
                stale["source"] = "stale_cache"
                self._send(stale)
                return
            self._send({"error": f"Supabase unavailable: {e}"}, 503)
            return

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
        totals = _daily_totals(rows, date_str)
        if totals:
            result["daily_totals"] = totals
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
