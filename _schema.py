"""
Data contract for energy_readings rows.

Single source of truth for:
  - Which fields exist and what they mean
  - Which rows carry which fields (live vs chart)
  - Which counter fields are reliable enough for daily KPI totals
  - Growatt chart API → DB column mapping (to avoid pacToUser confusion)

Import this in collect.py, energy.py, backfill.py — never duplicate the
mapping or reliability notes in comments scattered across files.
"""

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
