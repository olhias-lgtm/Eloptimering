"""
Data contract for energy_readings rows.

Single source of truth for:
  - Which fields exist and what they mean
  - Which rows carry which fields (live vs chart)
  - Which counter fields are reliable enough for daily KPI totals
  - Growatt chart API → DB column mapping (to avoid pacToUser confusion)

Import this in collect.py and energy.py — never duplicate the mapping or
reliability notes in comments scattered across files.
"""
from datetime import datetime

from _tz import STHLM

# ---------------------------------------------------------------------------
# Row types
# ---------------------------------------------------------------------------
# Live rows:  written by collect.py cron every 5 min via get_live().
#             soc_pct IS NOT NULL.  Timestamps are NOT on 5-min boundaries.
# Chart rows: written by collect?date=YYYY-MM-DD (historical backfill).
#             soc_pct IS NULL.  Timestamps ARE on exact 5-min boundaries.
#             A subset of fields is unavailable (see CHART_NULL_FIELDS).
ROW_TYPE_LIVE  = "live"   # soc_pct is not None
ROW_TYPE_CHART = "chart"  # soc_pct is None

# When bucketing into 5-min slots, live rows take priority over chart rows.
# Within the same type, the row closest to the slot boundary wins.
ROW_TYPE_PRIORITY = {ROW_TYPE_LIVE: 1, ROW_TYPE_CHART: 0}

def row_type(row: dict) -> str:
    return ROW_TYPE_LIVE if row.get("soc_pct") is not None else ROW_TYPE_CHART


# ---------------------------------------------------------------------------
# Growatt chart API → DB column mapping
# ---------------------------------------------------------------------------
# Used by collect._chart_to_rows() and backfill._chart_to_rows().
#
# Key insight: pacToUser is battery-discharge-to-loads, NOT grid import.
# Grid import is not available in the chart API at all.
#
# Growatt field   DB column       Notes
CHART_FIELD_MAP = {
    "ppv":         "ppv_kw",       # PV generation (kW)
    "sysOut":      "load_kw",      # Total house load (kW)
    "pacToGrid":   "export_kw",    # Export to grid (kW)
    "pacToUser":   "discharge_kw", # Battery → loads (kW)
    "chargePower": "charge_kw",    # Battery charge (kW)  ← confirmed via API probe
    "userLoad":    "import_kw",    # Import from grid (kW) ← confirmed via API probe
    # soc_pct is not available from the chart API — always NULL in chart rows
}

# Fields that are always NULL in chart rows (not provided by Growatt chart API)
CHART_NULL_FIELDS = {"soc_pct",
                     "epv_today", "eac_today", "echarge_today",
                     "edischarge_today", "eload_today",
                     "export_today", "import_today",
                     "ppv1_kw", "ppv2_kw", "pac_kw"}


# ---------------------------------------------------------------------------
# Daily counter fields
# ---------------------------------------------------------------------------
# Inverter internal Wh counters, read from live rows.
# More accurate than integrating 5-min slot kW values (avoids cron-jitter error).
#
# RELIABLE = safe to use as the primary source for daily KPI totals.
# UNRELIABLE = do NOT use for KPIs; fall back to kwhFromRows() integration.
#
# Counter         DB column         Reliable?  Reason if not
DAILY_COUNTERS = {
    "solar_kwh":  ("epv_today",     True,  None),
    "export_kwh": ("export_today",  True,  None),
    "import_kwh": ("import_today",  False, "0.10 kWh granularity → phantom +0.20 kWh steps"),
    "load_kwh":   ("eload_today",   True,  None),
    "charge_kwh": ("echarge_today", True,  None),
    "dis_kwh":    ("edischarge_today", True, None),
}

# Subset that the API exposes in daily_totals (only reliable ones)
DAILY_TOTALS_FIELDS = {
    kpi: col
    for kpi, (col, reliable, _) in DAILY_COUNTERS.items()
    if reliable
}
# → {"solar_kwh": "epv_today", "export_kwh": "export_today",
#    "load_kwh": "eload_today", "charge_kwh": "echarge_today",
#    "dis_kwh": "edischarge_today"}
#
# NOTE: "import_kwh" intentionally excluded — see granularity note above.


# ---------------------------------------------------------------------------
# Daily counter totals — single source of truth for picking the right row
# ---------------------------------------------------------------------------
# Used by both api/energy.py (frontend daily_totals) and api/collect.py
# (_recompute_daily_summary, the monthly-rollup writer). Do not reimplement
# this selection logic at either call site — see the reset-handling notes
# below, which are easy to get subtly wrong.

def _row_local_date(row: dict) -> str:
    ts_str = row.get("ts", "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str).astimezone(STHLM).date().isoformat()
    except Exception:
        return ""


def daily_counter_totals(rows: list, date_str: str) -> dict | None:
    """Return counter-based daily totals {kpi: value} for date_str, or None
    if the counters aren't usable (see guards below) — callers should fall
    back to integrating the kW columns over the day's rows in that case.

    Only DAILY_TOTALS_FIELDS (the reliable counters) are included.

    The inverter does NOT reset epv_today at local midnight — it carries
    the previous day's final value until production resets sometime after
    dawn. A naive "row with the highest epv_today" pick is wrong on any day
    whose solar total ends up LOWER than the previous day's (e.g. a cloudy
    day following a sunny one): the stale pre-dawn carry-over row would
    outrank the true (lower) max from later in the day, silently reporting
    yesterday's totals as today's.

    We detect the reset (epv_today drops >1 kWh between consecutive live
    rows) and discard the single carry-over row from before it. If no
    reset has happened yet and every live row for the date has ppv_kw=0,
    we're still in the carry-over window — return None rather than a
    stale value.
    """
    anchor_col = DAILY_TOTALS_FIELDS.get("solar_kwh", "epv_today")
    best = None
    prev_val = None
    reset_detected = False
    for row in rows:
        if row_type(row) != "live":
            continue
        # Restrict to the target local date — callers' query windows often
        # extend a few minutes into the next day to catch late cron rows;
        # those belong to tomorrow, not today.
        if _row_local_date(row) != date_str:
            continue
        val = row.get(anchor_col)
        if val is None:
            continue
        fval = float(val)
        if prev_val is not None and fval < prev_val - 1.0:
            best = None
            reset_detected = True
        prev_val = fval
        if best is None or fval > float(best.get(anchor_col) or -1):
            best = row

    if best is None:
        return None

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
