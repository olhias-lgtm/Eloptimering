"""
Data collection endpoint — called by cron-job.org every 5 minutes.
Fetches live Growatt data and persists it to Supabase energy_readings.
Always returns 200 so the cron service doesn't treat throttling as a failure.

Historical import:
  GET /api/collect?date=YYYY-MM-DD           → dry-run preview (no writes)
  GET /api/collect?date=YYYY-MM-DD&confirm=1 → import + write to Supabase
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from _growatt import get_session
from _schema import CHART_FIELD_MAP, CHART_NULL_FIELDS

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
        row: dict = {"ts": ts_utc, "import_kw": 0}  # import not in chart API
        for growatt_key, db_col in CHART_FIELD_MAP.items():
            row[db_col] = vals.get(growatt_key)
        # Fields unavailable in chart API — explicitly null per schema
        for col in CHART_NULL_FIELDS:
            row[col] = None
        rows.append(row)
    return rows


def _sb_upsert_rows(rows: list):
    """Insert rows into energy_readings, skipping any that already exist (by ts).
    Uses ignore-duplicates so live cron rows (with soc_pct, counters, etc.) are
    never overwritten by chart rows that have soc_pct=NULL."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase env vars not set")
    # Insert in batches of 100
    for i in range(0, len(rows), 100):
        batch = rows[i:i+100]
        body = json.dumps(batch).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/energy_readings",
            data=body,
            method="POST",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=ignore-duplicates,return=minimal",
            },
        )
        urllib.request.urlopen(req, timeout=15).read()


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
    _sb_upsert_rows(rows)
    preview["dry_run"] = False
    preview["written"] = slot_count
    preview["note"]    = "Rows upserted to energy_readings. Run /api/backfill to recompute daily_summary."
    return 200, preview


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed  = urlparse(self.path)
        params  = parse_qs(parsed.query)
        date_str = (params.get("date") or [None])[0]

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
            self._send({"ok": True, "ppv_kw": data.get("ppv_kw")})
        except Exception as e:
            print(f"[collect] error: {e}")
            # Still return 200 so cron-job.org doesn't flag it as failing
            self._send({"ok": False, "error": str(e)})

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
