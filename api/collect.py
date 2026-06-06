"""
Data collection endpoint — called by cron-job.org every 5 minutes.
Fetches live Growatt data and persists it to Supabase energy_readings.
Returns 500 on Growatt errors so cron-job.org can detect and alert.

Historical import (manual backfill):
  GET /api/collect?date=YYYY-MM-DD           → dry-run preview (no writes)
  GET /api/collect?date=YYYY-MM-DD&confirm=1 → import + write to Supabase

Automatic gap filling:
  GET /api/collect?action=autofill            → fill gaps in last 2 days
  GET /api/collect?action=autofill&days=N     → fill gaps in last N days (max 7)
  GET /api/collect?action=autofill&dry_run=1  → report gaps without writing

Data retention (weekly cron):
  GET /api/collect?action=retention           → run tiered retention policy
  GET /api/collect?action=retention&dry_run=1 → preview what would be deleted

  Retention tiers:
    0–180 days  : full granularity kept (energy_readings, energy_chart,
                  spot_prices, grid_production, weather_forecast)
    180d – 2yr  : granular rows deleted; daily_summary kept
    2yr+        : daily_summary rolled up → monthly_summary, then deleted
    5yr+        : monthly_summary rolled up → yearly_summary, then deleted
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from _growatt import get_session
from _schema import CHART_FIELD_MAP, CHART_NULL_FIELDS
from _cron_health import record_run

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

# CEST = UTC+2 (Swedish summer time, end of March → end of October)
# CET  = UTC+1 (Swedish winter time)
def _cest_offset(d: date) -> int:
    """Return UTC offset hours for Stockholm on a given date (2 in summer, 1 in winter)."""
    year = d.year
    # DST starts: last Sunday in March at 02:00 local
    # DST ends:   last Sunday in October at 03:00 local
    def last_sunday(y, month):
        # Find last Sunday of given month
        import calendar
        last_day = calendar.monthrange(y, month)[1]
        for day in range(last_day, last_day - 7, -1):
            if date(y, month, day).weekday() == 6:
                return date(y, month, day)
    dst_start = last_sunday(year, 3)   # Last Sunday March
    dst_end   = last_sunday(year, 10)  # Last Sunday October
    if dst_start <= d < dst_end:
        return 2  # CEST
    return 1  # CET


def _chart_to_rows(chart_data: dict, target_date: date, utc_offset_h: int) -> list:
    """
    Convert get_energy() chartData dict → list of energy_readings rows.
    chart_data keys are "HH:MM" strings in local time (CEST/CET).
    Returns list of dicts with 'ts' as UTC ISO string + power fields.
    """
    rows = []
    tz_local = timezone(timedelta(hours=utc_offset_h))
    for label, vals in sorted(chart_data.items()):
        # Parse "HH:MM"
        try:
            h, m = map(int, label.split(":"))
        except Exception:
            continue
        # Growatt labels each slot with the END of the 5-min interval.
        # Subtract 5 min to align with Shinephone (which shows start time).
        local_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            h, m, 0, tzinfo=tz_local,
        ) - timedelta(minutes=5)
        ts_utc = local_dt.astimezone(timezone.utc).isoformat()
        # Map Growatt chart fields → DB columns via schema contract.
        # CHART_FIELD_MAP defines which Growatt key maps to which column,
        # including the critical pacToUser→discharge_kw (NOT import_kw) mapping.
        row: dict = {"ts": ts_utc}
        for growatt_key, db_col in CHART_FIELD_MAP.items():
            row[db_col] = vals.get(growatt_key)
        # Fields unavailable in chart API — explicitly null per schema
        for col in CHART_NULL_FIELDS:
            row[col] = None
        # Skip rows where every measurable power field is exactly 0:
        # Growatt returns 0.0 as a sentinel for "no data recorded", not a real
        # zero reading. Storing these creates a misleading flat-zero line in the
        # chart instead of a proper gap. Real nighttime zeros still have discharge>0.
        power_cols = ("ppv_kw", "load_kw", "export_kw", "discharge_kw")
        if all((row.get(c) or 0) == 0 for c in power_cols):
            continue
        rows.append(row)
    return rows


def _sb_upsert_rows(rows: list):
    """Upsert chart rows into energy_readings, overwriting any existing chart row
    with the same ts.  Safe to use merge-duplicates because:
    - Chart rows have exact-minute timestamps  (HH:MM:00)
    - Live cron rows have second-offset timestamps (HH:MM:30)
    They never share a ts, so merge-duplicates can never overwrite a live row.
    Using merge (not ignore) means a stale all-zero chart row is replaced by
    corrected data on the next backfill/heal cycle."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase env vars not set")
    # Insert in batches of 100
    for i in range(0, len(rows), 100):
        batch = rows[i:i+100]
        body = json.dumps(batch).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/energy_readings?on_conflict=ts",
            data=body,
            method="POST",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates,return=minimal",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except urllib.error.HTTPError as e:
            body_err = e.read().decode(errors="replace")
            raise RuntimeError(f"Supabase upsert HTTP {e.code}: {body_err[:400]}") from e


# ---------------------------------------------------------------------------
# Self-healing: fill recent gaps from chart API after each live insert
# ---------------------------------------------------------------------------

def _heal_recent_gaps(today_str: str) -> int:
    """Check the last 2 hours for missing 5-min slots and fill them from
    Growatt's chart API.  Called after every successful live cron insert so
    gaps are closed within the next 5-minute cycle rather than waiting for
    the nightly autofill.  Safe to run every 5 min — uses ignore-duplicates
    so it never overwrites live rows.  Returns the number of rows upserted."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        now_utc  = datetime.now(timezone.utc)
        tz_cest  = timezone(timedelta(hours=2))
        d        = date.fromisoformat(today_str)

        # Window: up to 2 hours back, but never before today's CEST midnight
        today_midnight_utc = datetime(
            d.year, d.month, d.day, 0, 0, 0, tzinfo=tz_cest
        ).astimezone(timezone.utc)
        window_start = max(now_utc - timedelta(hours=2), today_midnight_utc)

        # Expected number of 5-min slots in the window
        elapsed_min = (now_utc - window_start).total_seconds() / 60
        expected    = max(0, int(elapsed_min / 5))
        if expected == 0:
            return 0

        # Count all rows (live + chart) in the window
        url = (
            f"{SUPABASE_URL}/rest/v1/energy_readings"
            f"?ts=gte.{urllib.parse.quote(window_start.isoformat())}"
            f"&ts=lt.{urllib.parse.quote(now_utc.isoformat())}"
            f"&select=ts&limit=600"
        )
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            actual = len(json.loads(r.read()))

        # Each slot produces at least 1 row (live or chart).  If we have
        # at least as many rows as expected slots there are no gaps.
        if actual >= expected:
            return 0

        # Gaps detected — fetch today's chart data and upsert non-zero rows
        print(f"[heal] {actual} rows vs {expected} expected in last 2 h — fetching chart data")
        s           = get_session()
        result      = s.get_energy(today_str)
        chart_data  = (result.get("obj") or {}).get("chartData") or {}
        if not chart_data:
            print("[heal] chart API returned no data")
            return 0

        rows = _chart_to_rows(chart_data, d, _cest_offset(d))
        if not rows:
            return 0

        _sb_upsert_rows(rows)
        _delete_future_chart_zeros(d)
        print(f"[heal] upserted {len(rows)} chart rows for {today_str}")
        return len(rows)

    except Exception as e:
        print(f"[heal] error: {e}")
        return 0


# ---------------------------------------------------------------------------
# Autofill helpers
# ---------------------------------------------------------------------------

# A day needs filling if fewer than this fraction of expected live slots exist.
_GAP_THRESHOLD = 1.0   # fill any day with even one missing slot


def _count_live_rows(date_str: str) -> int:
    """Count ALL rows (live + chart/backfill) for a CEST calendar date.

    Previously filtered soc_pct IS NOT NULL (live-only), but that caused
    backfilled days to show 0 and autofill to re-trigger forever.
    Any row in the DB means the slot has been filled — count it.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        d = date.fromisoformat(date_str)
        utc_offset_h = _cest_offset(d)
        tz_local = timezone(timedelta(hours=utc_offset_h))
        start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz_local).isoformat()
        end   = (datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz_local)
                 + timedelta(minutes=5)).isoformat()
        url = (
            f"{SUPABASE_URL}/rest/v1/energy_readings"
            f"?ts=gte.{urllib.parse.quote(start)}"
            f"&ts=lte.{urllib.parse.quote(end)}"
            f"&select=ts"
            f"&limit=600"
        )
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            return len(json.loads(r.read()))
    except Exception as e:
        print(f"[autofill] count error for {date_str}: {e}")
        return 0


def _expected_live_slots(date_str: str) -> int:
    """Expected live rows for a CEST date: full day=288, today=slots up to now."""
    d = date.fromisoformat(date_str)
    utc_offset_h = _cest_offset(d)
    tz_local = timezone(timedelta(hours=utc_offset_h))
    today_local = datetime.now(timezone.utc).astimezone(tz_local).date()
    if d < today_local:
        return 288
    now_local = datetime.now(timezone.utc).astimezone(tz_local)
    return max(1, (now_local.hour * 60 + now_local.minute) // 5)


def _delete_future_chart_zeros(today: "date") -> int:
    """Delete chart rows for today where ts is in the future (fake zeros from mid-day backfill).
    Returns the number of rows deleted."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        # Use current UTC time as the cutoff — anything after now is a future slot
        now_utc = datetime.now(timezone.utc).isoformat()
        url = (
            f"{SUPABASE_URL}/rest/v1/energy_readings"
            f"?ts=gt.{urllib.parse.quote(now_utc)}"
            f"&soc_pct=is.null"
        )
        req = urllib.request.Request(url, method="DELETE", headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer":        "return=representation",
            "Content-Type":  "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            deleted = json.loads(r.read())
            count = len(deleted) if isinstance(deleted, list) else 0
            if count:
                print(f"[autofill] deleted {count} future chart-zero rows for {today}")
            return count
    except Exception as e:
        print(f"[autofill] clean_zeros error: {e}")
        return 0


# ---------------------------------------------------------------------------
# Daily summary recomputation — server-side mirror of frontend renderKPIs()
# ---------------------------------------------------------------------------
# These constants mirror the frontend TARIFF_SCHEDULE and cfg() defaults.
# Update here whenever tariffs change in the dashboard's config panel.
_TARIFF_SCHEDULE = [
    {"from": date(2026, 6, 1),  "fast_avgift": 590.0, "natavg_in": 26.0},
    {"from": date(2000, 1, 1),  "fast_avgift": 390.0, "natavg_in": 26.0},  # baseline
]
_NATNYTTA_HIGH = 5.50     # öre/kWh — weekdays 06:00–22:00, Nov–Mar (high season)
_NATNYTTA_LOW  = 4.12     # öre/kWh — all other times
_ENERGISKATT   = 54.875   # öre/kWh (fixed by law)
_FORTUM_PASLAG = 6.96     # öre/kWh (supplier markup)
_FORTUM_FAST   = 55.20    # kr/month (supplier fixed fee)
_MOMS          = 1.25


def _tariffs_for_date(d: date) -> dict:
    for t in _TARIFF_SCHEDULE:
        if d >= t["from"]:
            return t
    return _TARIFF_SCHEDULE[-1]


def _natnytta_at(dt_local: datetime) -> float:
    """Export credit öre/kWh for a given local datetime.
    Mirrors the frontend natnyttaAt() function exactly."""
    is_weekday     = dt_local.weekday() < 5               # Mon–Fri
    is_high_season = dt_local.month in (11, 12, 1, 2, 3)  # Nov–Mar
    is_daytime     = 6 <= dt_local.hour < 22
    return _NATNYTTA_HIGH if (is_weekday and is_high_season and is_daytime) else _NATNYTTA_LOW


def _recompute_daily_summary(date_str: str, area: str = "SE3") -> dict:
    """Recompute and upsert daily_summary for a completed past day.

    Queries energy_readings + spot_prices from Supabase and applies the
    same cost formula as the frontend's renderKPIs() / calcInterval().
    Only runs for past completed days; skips today and future.

    Returns {"ok": True, "summary": {...}} on success,
            {"skipped": True, "reason": "..."} for non-past days,
            {"error": "..."} on failure.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"error": "Supabase env vars not set"}
    try:
        d = date.fromisoformat(date_str)
    except ValueError as e:
        return {"error": f"invalid date: {e}"}

    today_utc = datetime.now(timezone.utc).date()
    if d >= today_utc:
        return {"skipped": True, "reason": "today or future — day not yet complete"}

    utc_offset_h = _cest_offset(d)
    tz_local     = timezone(timedelta(hours=utc_offset_h))
    day_start    = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz_local)
    day_end      = day_start + timedelta(hours=24, minutes=5)  # include 23:55–00:04 slot

    # ── 1. Energy readings ────────────────────────────────────────────────────
    url = (
        f"{SUPABASE_URL}/rest/v1/energy_readings"
        f"?ts=gte.{urllib.parse.quote(day_start.isoformat())}"
        f"&ts=lt.{urllib.parse.quote(day_end.isoformat())}"
        f"&select=ts,ppv_kw,load_kw,export_kw,import_kw,soc_pct,"
        f"epv_today,eload_today,export_today,echarge_today,edischarge_today"
        f"&order=ts.asc&limit=600"
    )
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        }), timeout=10
    ) as r:
        energy_rows = json.loads(r.read())

    if not energy_rows:
        return {"error": "no energy_readings rows found"}

    # ── 2. Energy totals — counter fields preferred, integrate as fallback ────
    kwh5 = 5 / 60

    # Best live row: highest epv_today among rows where soc_pct IS NOT NULL
    best_live = None
    for row in energy_rows:
        if row.get("soc_pct") is not None and row.get("epv_today") is not None:
            if best_live is None or float(row["epv_today"]) > float(best_live.get("epv_today") or -1):
                best_live = row

    def _counter(row, col):
        v = row.get(col) if row else None
        return round(float(v), 3) if v is not None else None

    solar_kwh  = _counter(best_live, "epv_today")    or round(sum(float(r.get("ppv_kw")    or 0) for r in energy_rows) * kwh5, 3)
    load_kwh   = _counter(best_live, "eload_today")  or round(sum(float(r.get("load_kw")   or 0) for r in energy_rows) * kwh5, 3)
    export_kwh = _counter(best_live, "export_today") or round(sum(float(r.get("export_kw") or 0) for r in energy_rows) * kwh5, 3)
    import_kwh = round(sum(float(r.get("import_kw") or 0) for r in energy_rows) * kwh5, 3)  # always integrate (counter granularity too coarse)

    # ── 3. Spot prices ────────────────────────────────────────────────────────
    price_start = day_start.astimezone(timezone.utc).isoformat()
    price_end   = (day_start + timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    url_p = (
        f"{SUPABASE_URL}/rest/v1/spot_prices"
        f"?ts=gte.{urllib.parse.quote(price_start)}"
        f"&ts=lt.{urllib.parse.quote(price_end)}"
        f"&area=eq.{area}"
        f"&select=ts,sek_per_kwh&order=ts.asc&limit=30"
    )
    with urllib.request.urlopen(
        urllib.request.Request(url_p, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        }), timeout=10
    ) as r:
        price_rows = json.loads(r.read())

    if not price_rows:
        return {"error": "no spot prices found for this date — cannot compute costs"}

    # hour-keyed lookup: "YYYY-MM-DDTHH" (UTC) → öre/kWh
    price_map = {}
    for pr in price_rows:
        ts_dt = datetime.fromisoformat(pr["ts"].replace("Z", "+00:00")).astimezone(timezone.utc)
        price_map[ts_dt.strftime("%Y-%m-%dT%H")] = float(pr["sek_per_kwh"]) * 100  # → öre

    # ── 4. Slot-by-slot cost calculation (mirrors calcInterval exactly) ───────
    tariff      = _tariffs_for_date(d)
    natavg_in   = tariff["natavg_in"]
    fast_avgift = tariff["fast_avgift"]

    total_cost_kr  = 0.0
    total_earn_kr  = 0.0
    total_saved_kr = 0.0
    has_prices     = False

    for row in energy_rows:
        ts_dt = datetime.fromisoformat(
            row["ts"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        spot_ore = price_map.get(ts_dt.strftime("%Y-%m-%dT%H"))
        if spot_ore is None:
            continue
        has_prices = True

        imp_kwh5 = float(row.get("import_kw") or 0) * kwh5
        exp_kwh5 = float(row.get("export_kw") or 0) * kwh5
        lod_kwh5 = float(row.get("load_kw")   or 0) * kwh5

        import_rate_ore = (spot_ore + natavg_in + _ENERGISKATT + _FORTUM_PASLAG) * _MOMS
        export_rate_ore = spot_ore + _natnytta_at(ts_dt.astimezone(tz_local))

        total_cost_kr  += (imp_kwh5 * import_rate_ore) / 100
        total_earn_kr  += (exp_kwh5 * export_rate_ore) / 100
        total_saved_kr += (max(0.0, lod_kwh5 - imp_kwh5) * import_rate_ore) / 100

    if not has_prices:
        return {"error": "no spot prices matched any energy slot"}

    fixed_kr = ((fast_avgift + _FORTUM_FAST) * _MOMS) / 30
    total_cost_kr += fixed_kr
    net_kr = total_earn_kr - total_cost_kr

    # ── 5. Upsert to daily_summary ────────────────────────────────────────────
    payload = {
        "day":            date_str,
        "area":           area,
        "solar_kwh":      round(solar_kwh, 3),
        "export_kwh":     round(export_kwh, 3),
        "import_kwh":     round(import_kwh, 3),
        "import_cost_kr": round(total_cost_kr - fixed_kr, 2),
        "export_earn_kr": round(total_earn_kr, 2),
        "fixed_cost_kr":  round(fixed_kr, 2),
        "net_kr":         round(net_kr, 2),
        "saved_kr":       round(total_saved_kr, 2),
    }
    body = json.dumps(payload).encode()
    urllib.request.urlopen(
        urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/daily_summary?on_conflict=day,area",
            data=body, method="POST",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates,return=minimal",
            },
        ), timeout=8
    ).read()
    print(f"[summary] upserted daily_summary for {date_str}: net={net_kr:.2f} kr")
    return {"ok": True, "summary": payload}


def _sb_count(table: str, filter_qs: str) -> int:
    """COUNT rows matching a filter via PostgREST HEAD + Prefer:count=exact.
    Returns the integer from the Content-Range header (no response body)."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filter_qs}"
    req = urllib.request.Request(url, method="HEAD", headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer":        "count=exact",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        # Content-Range: 0-N/TOTAL  or  */TOTAL
        cr = resp.headers.get("Content-Range", "*/0")
        return int(cr.split("/")[-1])
    except Exception as e:
        print(f"[retention] COUNT {table} error: {e}")
        return 0


def _sb_delete(table: str, filter_qs: str) -> int:
    """DELETE rows from a Supabase table matching a PostgREST filter query string.
    Uses return=minimal (no response body) to avoid hitting the 10 MB limit on
    large deletes. Row count is obtained beforehand via _sb_count()."""
    count = _sb_count(table, filter_qs)
    if count == 0:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filter_qs}"
    req = urllib.request.Request(url, method="DELETE", headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer":        "return=minimal",
    })
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return count
    except Exception as e:
        print(f"[retention] DELETE {table} error: {e}")
        return 0


def _sb_query(table: str, select: str, filter_qs: str) -> list:
    """SELECT rows from a Supabase table."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?select={select}&{filter_qs}"
    req = urllib.request.Request(url, headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        print(f"[retention] SELECT {table} error: {e}")
        return []


def _sb_upsert(table: str, rows: list, on_conflict: str) -> None:
    """Upsert rows into a Supabase table."""
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(rows).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        f"resolution=merge-duplicates,return=minimal",
    })
    urllib.request.urlopen(req, timeout=15).read()


def _do_retention(dry_run: bool = False) -> tuple[int, dict]:
    """
    Tiered data retention:
      0–180 days    : full granularity (energy_readings, energy_chart, spot_prices,
                      grid_production, weather_forecast)
      180d – 2yr    : granular rows deleted; daily_summary kept
      2yr+          : daily_summary rolled up → monthly_summary, then deleted
      (future)      : monthly_summary → yearly_summary rollup at 5yr+
    """
    now_utc  = datetime.now(timezone.utc)
    cutoff_granular  = (now_utc - timedelta(days=180)).date().isoformat()
    cutoff_daily     = (now_utc - timedelta(days=730)).date().isoformat()   # ~2 years
    cutoff_monthly   = (now_utc - timedelta(days=1825)).date().isoformat()  # ~5 years

    report: dict = {"dry_run": dry_run}

    # ── 1. Roll up daily_summary rows older than 2 years → monthly_summary ───
    old_daily = _sb_query(
        "daily_summary",
        "day,area,solar_kwh,load_kwh,import_kwh,export_kwh,charge_kwh,"
        "discharge_kwh,import_cost_kr,export_earn_kr,fixed_cost_kr,net_kr,saved_kr",
        f"day=lt.{cutoff_daily}&order=day.asc",
    )
    # Aggregate by (year_month, area)
    monthly_accum: dict = {}
    for r in old_daily:
        key = (r["day"][:7], r.get("area") or "SE3")  # 'YYYY-MM'
        if key not in monthly_accum:
            monthly_accum[key] = {
                "year_month": key[0], "area": key[1],
                "solar_kwh": 0, "load_kwh": 0, "import_kwh": 0, "export_kwh": 0,
                "charge_kwh": 0, "discharge_kwh": 0, "import_cost_kr": 0,
                "export_earn_kr": 0, "fixed_cost_kr": 0, "net_kr": 0,
                "saved_kr": 0, "day_count": 0,
            }
        acc = monthly_accum[key]
        for col in ("solar_kwh", "load_kwh", "import_kwh", "export_kwh",
                    "charge_kwh", "discharge_kwh", "import_cost_kr",
                    "export_earn_kr", "fixed_cost_kr", "net_kr", "saved_kr"):
            acc[col] = round((acc[col] or 0) + float(r.get(col) or 0), 4)
        acc["day_count"] += 1

    monthly_rows = list(monthly_accum.values())
    report["daily_to_monthly_rows"] = len(monthly_rows)
    report["daily_deleted_candidates"] = len(old_daily)

    if not dry_run and monthly_rows:
        _sb_upsert("monthly_summary", monthly_rows, "year_month,area")
        print(f"[retention] upserted {len(monthly_rows)} monthly_summary rows")

    if not dry_run and old_daily:
        n = _sb_delete("daily_summary", f"day=lt.{cutoff_daily}")
        print(f"[retention] deleted {n} daily_summary rows older than {cutoff_daily}")
        report["daily_deleted"] = n

    # ── 2. Roll up monthly_summary rows older than 5 years → yearly_summary ──
    old_monthly = _sb_query(
        "monthly_summary",
        "year_month,area,solar_kwh,load_kwh,import_kwh,export_kwh,charge_kwh,"
        "discharge_kwh,import_cost_kr,export_earn_kr,fixed_cost_kr,net_kr,saved_kr,day_count",
        f"year_month=lt.{cutoff_monthly[:7]}&order=year_month.asc",
    )
    yearly_accum: dict = {}
    for r in old_monthly:
        key = (r["year_month"][:4], r.get("area") or "SE3")  # 'YYYY'
        if key not in yearly_accum:
            yearly_accum[key] = {
                "year": key[0], "area": key[1],
                "solar_kwh": 0, "load_kwh": 0, "import_kwh": 0, "export_kwh": 0,
                "charge_kwh": 0, "discharge_kwh": 0, "import_cost_kr": 0,
                "export_earn_kr": 0, "fixed_cost_kr": 0, "net_kr": 0,
                "saved_kr": 0, "month_count": 0,
            }
        acc = yearly_accum[key]
        for col in ("solar_kwh", "load_kwh", "import_kwh", "export_kwh",
                    "charge_kwh", "discharge_kwh", "import_cost_kr",
                    "export_earn_kr", "fixed_cost_kr", "net_kr", "saved_kr"):
            acc[col] = round((acc[col] or 0) + float(r.get(col) or 0), 4)
        acc["month_count"] += int(r.get("day_count") or 1)

    yearly_rows = list(yearly_accum.values())
    report["monthly_to_yearly_rows"] = len(yearly_rows)
    report["monthly_deleted_candidates"] = len(old_monthly)

    if not dry_run and yearly_rows:
        _sb_upsert("yearly_summary", yearly_rows, "year,area")
        print(f"[retention] upserted {len(yearly_rows)} yearly_summary rows")

    if not dry_run and old_monthly:
        n = _sb_delete("monthly_summary", f"year_month=lt.{cutoff_monthly[:7]}")
        print(f"[retention] deleted {n} monthly_summary rows older than {cutoff_monthly[:7]}")
        report["monthly_deleted"] = n

    # ── 3. Delete granular tables older than 180 days ─────────────────────────
    tables_granular = [
        ("energy_readings",  f"ts=lt.{cutoff_granular}T00:00:00Z"),
        ("energy_chart",     f"date=lt.{cutoff_granular}"),
        ("grid_production",  f"ts=lt.{cutoff_granular}T00:00:00Z"),
        # Spot prices: keep 90 days (shorter than granular cutoff; never touch last 7d)
        ("spot_prices",      f"ts=lt.{(now_utc - timedelta(days=90)).date().isoformat()}T00:00:00Z"),
        # TOU suggestions: keep 90 days (useful for reviewing recent decisions)
        ("tou_suggestions",  f"for_date=lt.{(now_utc - timedelta(days=90)).date().isoformat()}"),
    ]
    # Weather forecast: delete rows with valid_time in the past (>24h ago)
    past_weather = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tables_granular.append(("weather_forecast", f"valid_time=lt.{past_weather}"))

    for table, filt in tables_granular:
        n_before = _sb_count(table, filt)
        report[f"{table}_candidates"] = n_before
        if not dry_run:
            n = _sb_delete(table, filt)
            report[f"{table}_deleted"] = n
            print(f"[retention] deleted {n} rows from {table} (filter: {filt})")

    report["ok"] = True
    return 200, report


def _do_autofill(days: int, dry_run: bool) -> tuple[int, dict]:
    utc_offset_h = _cest_offset(datetime.now(timezone.utc).date())
    tz_local = timezone(timedelta(hours=utc_offset_h))
    today_local = datetime.now(timezone.utc).astimezone(tz_local).date()

    results = []
    filled_dates = []

    for i in range(1, min(days, 7) + 1):   # start at 1 = yesterday; skip today
        target = today_local - timedelta(days=i)
        date_str = target.isoformat()
        live = _count_live_rows(date_str)
        expected = _expected_live_slots(date_str)
        needs = live < expected * _GAP_THRESHOLD
        entry = {"date": date_str, "live_rows": live, "expected": expected,
                 "missing": max(0, expected - live), "needs_fill": needs}

        if needs:
            print(f"[autofill] {date_str}: {live}/{expected} live rows — {'dry run' if dry_run else 'filling'}")
            if not dry_run:
                status, resp = _do_historical(date_str, confirm=True)
                entry["fill_status"]  = status
                entry["fill_written"] = resp.get("written", 0)
                entry["fill_error"]   = resp.get("error") if status != 200 else None
                if status == 200:
                    filled_dates.append(date_str)
                    # Recompute daily_summary now that energy_readings are fresh.
                    # Autofill only runs for yesterday and earlier, so all dates here
                    # are completed past days. Non-fatal on error.
                    try:
                        entry["daily_summary"] = _recompute_daily_summary(date_str)
                    except Exception as e:
                        entry["daily_summary"] = {"error": str(e)}
        else:
            print(f"[autofill] {date_str}: {live}/{expected} live rows — OK")

        results.append(entry)

    # Clean up fake-zero chart rows for today: future slots stored by a previous
    # mid-day backfill. A chart row with ts > now cannot have real data, so it is
    # safe to delete unconditionally (live rows have soc_pct IS NOT NULL → kept).
    zeros_deleted = 0
    if not dry_run:
        zeros_deleted = _delete_future_chart_zeros(today_local)

    # Refresh materialised battery stats after any writes
    batt_refreshed = False
    if not dry_run and (filled_dates or zeros_deleted):
        try:
            url = f"{SUPABASE_URL}/rest/v1/rpc/refresh_battery_stats"
            req = urllib.request.Request(
                url, data=b"{}", method="POST",
                headers={
                    "apikey":        SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type":  "application/json",
                },
            )
            urllib.request.urlopen(req, timeout=10).read()
            batt_refreshed = True
            print("[autofill] battery_stats refreshed")
        except Exception as e:
            print(f"[autofill] battery_stats refresh failed (non-fatal): {e}")

    return 200, {
        "ok":              True,
        "dry_run":         dry_run,
        "days_checked":    len(results),
        "filled":          filled_dates,
        "zeros_deleted":   zeros_deleted,
        "batt_refreshed":  batt_refreshed,
        "results":         results,
    }


def _sb_insert(data: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase env vars not set")
    body = json.dumps({
        "ppv_kw":           data.get("ppv_kw"),
        "ppv1_kw":          data.get("ppv1_kw"),
        "ppv2_kw":          data.get("ppv2_kw"),
        "pac_kw":           data.get("pac_kw"),
        "load_kw":          data.get("load_kw"),
        "export_kw":        data.get("export_kw"),
        "import_kw":        data.get("import_kw"),
        "charge_kw":        data.get("charge_kw"),
        "discharge_kw":     data.get("discharge_kw"),
        "epv_today":        data.get("epv_today"),
        "eac_today":        data.get("eac_today"),
        "echarge_today":    data.get("echarge_today"),
        "edischarge_today": data.get("edischarge_today"),
        "eload_today":      data.get("eload_today"),
        "export_today":     data.get("export_today"),
        "import_today":     data.get("import_today"),
        "soc_pct":          data.get("soc_pct"),
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/energy_readings",
        data=body,
        method="POST",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Supabase insert HTTP {e.code}: {err_body}") from e


def _do_historical(date_str: str, confirm: bool) -> tuple[int, dict]:
    """
    Validate and optionally import a historical day's chart data.
    Returns (http_status, response_dict).
    """
    # ── 1. Validate date format ────────────────────────────────────────────────
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        return 400, {"ok": False, "error": "date must be YYYY-MM-DD"}

    try:
        target = date.fromisoformat(date_str)
    except ValueError as e:
        return 400, {"ok": False, "error": f"invalid date: {e}"}

    today_utc = datetime.now(timezone.utc).date()

    # ── 2. Validate range ──────────────────────────────────────────────────────
    if target > today_utc:
        return 400, {"ok": False, "error": "date cannot be in the future"}

    if (today_utc - target).days > 365:
        return 400, {"ok": False, "error": "date too far in the past (max 365 days)"}

    # ── 3. Determine UTC offset for that date ─────────────────────────────────
    utc_offset_h = _cest_offset(target)
    tz_name = "CEST (UTC+2)" if utc_offset_h == 2 else "CET (UTC+1)"

    # ── 4. Fetch from Growatt ──────────────────────────────────────────────────
    s = get_session()
    result = s.get_energy(date_str)

    chart_data = (result.get("obj") or {}).get("chartData") or {}
    if not chart_data:
        return 502, {"ok": False, "error": "Growatt returned empty chartData"}

    # ── 5. Convert to rows ─────────────────────────────────────────────────────
    rows = _chart_to_rows(chart_data, target, utc_offset_h)

    if not rows:
        return 502, {"ok": False, "error": "No rows produced from chartData"}

    # ── 6. Basic sanity checks ─────────────────────────────────────────────────
    non_zero_solar = sum(1 for r in rows if (r.get("ppv_kw") or 0) > 0)
    max_ppv        = max((r.get("ppv_kw") or 0) for r in rows)
    ts_values      = [r["ts"] for r in rows]
    ts_start       = min(ts_values)
    ts_end         = max(ts_values)

    # Expect 00:00 → 23:55 in local time = UTC range depends on offset
    # We want at least 200 slots (out of 288 5-min slots) to consider it complete
    slot_count = len(rows)
    warnings = []
    if slot_count < 200:
        warnings.append(f"Only {slot_count} slots (expected ~288)")
    if max_ppv > 50:
        warnings.append(f"Suspiciously high ppv peak: {max_ppv} kW")

    preview = {
        "valid":          True,
        "date":           date_str,
        "timezone":       tz_name,
        "utc_offset_h":   utc_offset_h,
        "slot_count":     slot_count,
        "non_zero_solar": non_zero_solar,
        "max_ppv_kw":     max_ppv,
        "ts_start_utc":   ts_start,
        "ts_end_utc":     ts_end,
        "sample":         rows[:3],
        "warnings":       warnings,
    }

    if not confirm:
        preview["dry_run"] = True
        preview["note"]    = "Add &confirm=1 to write to Supabase"
        return 200, preview

    # ── 7. Write ───────────────────────────────────────────────────────────────
    rows_before = _count_live_rows(date_str)
    _sb_upsert_rows(rows)
    rows_after  = _count_live_rows(date_str)
    actual_new  = max(0, rows_after - rows_before)

    # If backfilling today, delete future chart zeros that Growatt returns for
    # not-yet-recorded slots (these would otherwise sit in the DB as stale zeros).
    zeros_deleted = 0
    today_utc = datetime.now(timezone.utc).date()
    if target == today_utc:
        zeros_deleted = _delete_future_chart_zeros(today_utc)

    preview["dry_run"]       = False
    preview["attempted"]     = slot_count
    preview["new_rows"]      = actual_new
    preview["skipped"]       = slot_count - actual_new  # ignored as duplicates
    preview["zeros_deleted"] = zeros_deleted
    preview["note"]          = "Rows upserted to energy_readings."

    # Recompute daily_summary for completed past days (not today — partial data).
    # Errors here are non-fatal; backfill already succeeded.
    if target < today_utc:
        try:
            preview["daily_summary"] = _recompute_daily_summary(date_str)
        except Exception as e:
            preview["daily_summary"] = {"error": str(e)}

    return 200, preview


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed  = urlparse(self.path)
        params  = parse_qs(parsed.query)
        date_str = (params.get("date") or [None])[0]

        action = (params.get("action") or [None])[0]

        if action == "retention":
            dry_run = (params.get("dry_run") or ["0"])[0] in ("1", "true", "yes")
            try:
                status, resp = _do_retention(dry_run)
                record_run("collect_retention", ok=(status == 200),
                           error=resp.get("error") if status != 200 else None)
            except Exception as e:
                print(f"[retention] error: {e}")
                status, resp = 500, {"ok": False, "error": str(e)}
                record_run("collect_retention", ok=False, error=str(e))
            self._send(resp, status=status)
            return

        if action == "autofill":
            # Automatic gap detection + chart backfill
            dry_run = (params.get("dry_run") or ["0"])[0] in ("1", "true", "yes")
            try:
                days = min(7, max(1, int((params.get("days") or ["2"])[0])))
            except ValueError:
                days = 2
            try:
                status, resp = _do_autofill(days, dry_run)
                if not dry_run:
                    record_run("collect_autofill", ok=(status == 200),
                               error=resp.get("error") if status != 200 else None)
            except Exception as e:
                print(f"[autofill] error: {e}")
                status, resp = 500, {"ok": False, "error": str(e)}
                record_run("collect_autofill", ok=False, error=str(e))
            self._send(resp, status=status)
            return

        if date_str is not None:
            # Historical import branch
            confirm = (params.get("confirm") or ["0"])[0] in ("1", "true", "yes")
            try:
                status, resp = _do_historical(date_str, confirm)
            except Exception as e:
                print(f"[collect] historical error: {e}")
                status, resp = 500, {"ok": False, "error": str(e)}
            self._send(resp, status=status)
            return

        # ── Live collection (normal cron path) ────────────────────────────────
        try:
            s    = get_session()
            data = s.get_live()
            _sb_insert(data)
            print(f"[collect] OK ppv={data.get('ppv_kw')} export={data.get('export_kw')}")

            # Self-heal: fill any gaps in the last 2 hours from the chart API
            today_str = (
                datetime.now(timezone.utc)
                .astimezone(timezone(timedelta(hours=2)))
                .date().isoformat()
            )
            _heal_recent_gaps(today_str)

            self._send({"ok": True, "ppv_kw": data.get("ppv_kw")})
        except Exception as e:
            print(f"[collect] error: {e}")
            # Return 500 so cron-job.org can detect and alert on Growatt failures.
            # (Previously 200 to avoid alerts — but silent failures are worse.)
            self._send({"ok": False, "error": str(e)}, status=500)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
