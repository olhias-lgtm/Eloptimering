"""
solar_model
  GET                        — return current per-slot correction model from Supabase
  GET ?action=build          — rebuild the model from 90 days of historical data
  GET ?action=solcast_fetch  — pull latest forecast from Solcast API → upsert solcast_forecast
  GET ?action=solcast_read   — serve upcoming solcast_forecast rows (default 2 days)

Build logic: fetches actual solar per 5-min slot via a server-side RPC, fetches
90-day historical GTI from Open-Meteo, computes ratio = avg_actual_kw / avg_gti_wm2
per slot, upserts into solar_model table.

Only slots with avg_gti > GTI_MIN W/m² and day_count >= MIN_DAYS get a ratio;
the rest get ratio=NULL so the frontend falls back to the physics model.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_ANON_KEY", "")
SOLCAST_API_KEY   = os.environ.get("SOLCAST_API_KEY", "")
SOLCAST_SITE_UUID = os.environ.get("SOLCAST_SITE_UUID", "")

LAT        = 59.28
LON        = 18.00
PANEL_TILT = 45
PANEL_AZ   = -68
LOOKBACK   = 90
GTI_MIN    = 50.0
MIN_DAYS   = 5

_CACHE          = None
_CACHE_TTL      = 3600

_LOAD_CACHE       = None
_LOAD_CACHE_TTL   = 6 * 3600  # 6 hours — load patterns change slowly

_SOLCAST_CACHE    = None
_SOLCAST_CACHE_TTL = 30 * 60  # 30 minutes


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _fetch_model() -> list:
    global _CACHE
    now = time.time()
    if _CACHE and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/solar_model?order=slot.asc&select=slot,ratio,day_count"
    req = urllib.request.Request(url, headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    _CACHE = {"ts": now, "data": data}
    return data


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _fetch_solar_actuals() -> list:
    url = f"{SUPABASE_URL}/rest/v1/rpc/get_solar_actuals_by_slot?lookback_days={LOOKBACK}"
    req = urllib.request.Request(url, headers={**_sb_headers(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _fetch_historical_gti() -> dict:
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=global_tilted_irradiance"
        f"&tilt={PANEL_TILT}&azimuth={PANEL_AZ}"
        f"&timezone=Europe%2FStockholm"
        f"&past_days={LOOKBACK}&forecast_days=0"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "electricity-dashboard/solar-model"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    hourly_buckets = defaultdict(list)
    for t, v in zip(data["hourly"]["time"], data["hourly"]["global_tilted_irradiance"]):
        if v is not None and v > 0:
            hourly_buckets[int(t[11:13])].append(float(v))
    return {h: sum(vs) / len(vs) for h, vs in hourly_buckets.items()}


def _upsert_model(rows: list):
    body = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/solar_model?on_conflict=slot",
        data=body, method="POST",
        headers={
            **_sb_headers(),
            "Content-Type": "application/json",
            "Prefer":       "resolution=merge-duplicates,return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=15).read()


def _build_model() -> dict:
    actuals        = _fetch_solar_actuals()
    avg_gti        = _fetch_historical_gti()
    actual_by_slot = {int(r["slot"]): r for r in actuals}
    now_str        = datetime.now(timezone.utc).isoformat()

    upsert_rows = []
    built = 0
    for slot in range(288):
        hour    = slot // 12
        gti_avg = avg_gti.get(hour, 0.0)
        actual  = actual_by_slot.get(slot)
        if actual and gti_avg >= GTI_MIN and int(actual["day_count"]) >= MIN_DAYS:
            ratio = float(actual["avg_solar_kw"]) / gti_avg
            upsert_rows.append({"slot": slot, "ratio": round(ratio, 8),
                                 "day_count": int(actual["day_count"]), "updated_at": now_str})
            built += 1
        else:
            upsert_rows.append({"slot": slot, "ratio": None,
                                 "day_count": int(actual["day_count"]) if actual else 0,
                                 "updated_at": now_str})

    _upsert_model(upsert_rows)
    global _CACHE
    _CACHE = None   # invalidate read cache after rebuild
    print(f"[solar_model] built {built}/288 slots")
    return {"ok": True, "slots_built": built, "total_slots": 288, "lookback_days": LOOKBACK}


# ---------------------------------------------------------------------------
# Load profile model
# ---------------------------------------------------------------------------

def _fetch_load_profile(lookback: int = 90) -> list:
    """Return per-slot, per-month average load_kw from the last `lookback` days.

    Calls the get_load_profile_by_slot Supabase RPC which groups energy_readings
    by (slot, month) and returns avg_load_kw + day_count per combination.
    Results are cached for 6 hours — load patterns change slowly.

    Returns a list of {slot, month, avg_load_kw, day_count} dicts.
    """
    global _LOAD_CACHE
    now = time.time()
    if _LOAD_CACHE and now - _LOAD_CACHE["ts"] < _LOAD_CACHE_TTL:
        return _LOAD_CACHE["data"]
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = (
        f"{SUPABASE_URL}/rest/v1/rpc/get_load_profile_by_slot"
        f"?lookback_days={lookback}"
    )
    req = urllib.request.Request(url, headers={
        **_sb_headers(), "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    _LOAD_CACHE = {"ts": now, "data": data}
    print(f"[load_model] fetched {len(data)} slot×month entries")
    return data


# ---------------------------------------------------------------------------
# Horizon / shade profile analysis
# ---------------------------------------------------------------------------

def _sun_position(dt_utc: datetime, lat_deg: float, lon_deg: float) -> tuple:
    """Return (azimuth_deg, elevation_deg) for a UTC datetime and location.

    Azimuth is measured clockwise from north (0=N, 90=E, 180=S, 270=W).
    Uses a compact NOAA low-precision model accurate to ~0.5° for our purposes.
    """
    jd  = 2440587.5 + dt_utc.timestamp() / 86400.0
    n   = jd - 2451545.0

    L   = (280.460 + 0.9856474 * n) % 360
    g   = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)

    sin_dec = math.sin(eps) * math.sin(lam)
    dec     = math.asin(max(-1.0, min(1.0, sin_dec)))
    ra      = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))

    gmst_h  = (18.697374558 + 24.06570982441908 * n) % 24
    lha     = math.radians((gmst_h * 15 - math.degrees(ra) + lon_deg) % 360)

    lat     = math.radians(lat_deg)
    sin_el  = (math.sin(lat) * math.sin(dec)
               + math.cos(lat) * math.cos(dec) * math.cos(lha))
    el      = math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))

    denom   = math.cos(math.radians(el)) * math.cos(lat) + 1e-9
    cos_az  = (math.sin(dec) - math.sin(math.radians(el)) * math.sin(lat)) / denom
    az      = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    if math.sin(lha) > 0:
        az = 360.0 - az

    return az, el


def _horizon_analysis() -> dict:
    """Estimate site horizon profile from historical shading patterns.

    Method
    ------
    1. Fetch 90 days of hourly GTI from Open-Meteo (clear-sky reference).
    2. Fetch all energy_readings rows with ppv_kw > 0 from Supabase.
    3. For each reading: if GTI > 300 W/m² (strong sun) but ppv_kw < 15% of
       expected → the sun was blocked at that azimuth/elevation → shade event.
    4. For each 5° azimuth bin: horizon elevation = max blocked elevation seen.

    Confidence
    ----------
    Requires data from many different days to separate shading from clouds.
    Returns a 'confidence' field: 'low' < 14 days, 'medium' 14–60, 'high' > 60.
    The horizon profile is still returned at low confidence — useful as a starting
    point, but treat it as indicative until more data is collected.

    Output format
    -------------
    Returns {horizon, blocked_observations, data_days, confidence, readings_analyzed}
    horizon: [{azimuth, elevation}] for azimuths 0–355 in 5° steps — paste into
    Solcast toolkit → Site → Advanced Settings → Horizon Profile.
    """
    CAPACITY_KWP  = 11.7
    LOSS_FACTOR   = 0.89
    GTI_MIN_CLEAR = 300    # W/m² — decent direct sun, not just diffuse
    SHADE_RATIO   = 0.15   # actual < 15% of expected → shaded
    EL_MIN        = 4.0    # ignore sun below 4° (atmospheric effects, not obstacles)
    LOOKBACK      = 90

    # 1. Historical GTI from Open-Meteo
    gti_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=global_tilted_irradiance"
        f"&tilt={PANEL_TILT}&azimuth={PANEL_AZ}"
        f"&timezone=UTC&past_days={LOOKBACK}&forecast_days=0"
    )
    req = urllib.request.Request(gti_url, headers={"User-Agent": "electricity-dashboard/horizon"})
    with urllib.request.urlopen(req, timeout=20) as r:
        gti_data = json.loads(r.read())

    # "YYYY-MM-DDTHH" → gti W/m²
    gti_by_hour: dict = {}
    for t, v in zip(gti_data["hourly"]["time"], gti_data["hourly"]["global_tilted_irradiance"]):
        gti_by_hour[t[:13]] = float(v or 0)

    # 2. Fetch actual solar readings (ppv_kw) from Supabase — all available rows
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sb_url = (
        f"{SUPABASE_URL}/rest/v1/energy_readings"
        f"?ts=gte.{cutoff}"
        f"&ppv_kw=gt.0"
        f"&select=ts,ppv_kw"
        f"&order=ts.asc"
        f"&limit=50000"
    )
    req = urllib.request.Request(sb_url, headers={
        **_sb_headers(), "Prefer": "count=none",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        readings = json.loads(r.read())

    print(f"[horizon] {len(readings)} solar readings fetched")

    # Count distinct data days
    data_days = len({row["ts"][:10] for row in readings})

    # 3. Identify shading events
    az_blocked: dict = defaultdict(list)  # azimuth_bin → [elevation]
    blocked_count = 0

    for row in readings:
        hour_key  = row["ts"][:13]   # "YYYY-MM-DDTHH"
        gti       = gti_by_hour.get(hour_key, 0.0)
        if gti < GTI_MIN_CLEAR:
            continue  # not a clear-sky slot — can't distinguish shade from clouds

        expected_kw = (gti / 1000.0) * CAPACITY_KWP * LOSS_FACTOR
        actual_kw   = float(row["ppv_kw"])
        if actual_kw >= expected_kw * SHADE_RATIO:
            continue  # producing enough — not shaded

        try:
            dt = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except Exception:
            continue

        az, el = _sun_position(dt, LAT, LON)
        if el < EL_MIN:
            continue  # sun too low — atmospheric effects, not a real obstacle

        az_bin = round(az / 5) * 5 % 360
        az_blocked[az_bin].append(el)
        blocked_count += 1

    print(f"[horizon] {blocked_count} shade events across {len(az_blocked)} azimuth bins")

    # 4. Build horizon profile — for each 5° azimuth, max blocked elevation
    horizon = []
    for az_bin in range(0, 360, 5):
        if az_bin in az_blocked:
            max_el = round(max(az_blocked[az_bin]), 1)
        else:
            max_el = 0.0
        horizon.append({"azimuth": az_bin, "elevation": max_el})

    if data_days >= 60:
        confidence = "high"
    elif data_days >= 14:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "ok":                    True,
        "horizon":               horizon,
        "blocked_observations":  blocked_count,
        "azimuth_bins_with_shading": len(az_blocked),
        "data_days":             data_days,
        "confidence":            confidence,
        "readings_analyzed":     len(readings),
        "note": (
            f"Based on {data_days} days of data. "
            + ("Confidence is LOW — results are indicative only. Re-run after 30+ clear-day observations."
               if confidence == "low" else
               "Paste the 'horizon' array into Solcast toolkit → Site → Advanced Settings → Horizon Profile.")
        ),
    }


# ---------------------------------------------------------------------------
# Solcast
# ---------------------------------------------------------------------------

def _solcast_fetch() -> dict:
    """Fetch latest Solcast rooftop forecast and upsert into solcast_forecast table."""
    if not SOLCAST_API_KEY or not SOLCAST_SITE_UUID:
        raise RuntimeError("SOLCAST_API_KEY or SOLCAST_SITE_UUID not configured")
    url = (
        f"https://api.solcast.com.au/rooftop_sites/{SOLCAST_SITE_UUID}/forecasts"
        f"?format=json&hours=72&api_key={SOLCAST_API_KEY}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "electricity-dashboard/solcast"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    forecasts = data.get("forecasts", [])
    if not forecasts:
        return {"ok": True, "upserted": 0}

    rows = [
        {
            "period_end":    f["period_end"],
            "pv_estimate":   f.get("pv_estimate"),
            "pv_estimate10": f.get("pv_estimate10"),
            "pv_estimate90": f.get("pv_estimate90"),
        }
        for f in forecasts
    ]
    body = json.dumps(rows).encode()
    upsert_req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/solcast_forecast?on_conflict=period_end",
        data=body, method="POST",
        headers={
            **_sb_headers(),
            "Content-Type": "application/json",
            "Prefer":       "resolution=merge-duplicates,return=minimal",
        },
    )
    urllib.request.urlopen(upsert_req, timeout=15).read()
    global _SOLCAST_CACHE
    _SOLCAST_CACHE = None  # invalidate read cache
    print(f"[solcast] upserted {len(rows)} rows")
    return {"ok": True, "upserted": len(rows)}


def _solcast_read(days: int = 2) -> list:
    """Return upcoming Solcast forecast rows from Supabase (cached 30 min)."""
    global _SOLCAST_CACHE
    now = time.time()
    if _SOLCAST_CACHE and now - _SOLCAST_CACHE["ts"] < _SOLCAST_CACHE_TTL:
        return _SOLCAST_CACHE["data"]
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    limit = days * 48 + 10  # 30-min slots
    url = (
        f"{SUPABASE_URL}/rest/v1/solcast_forecast"
        f"?period_end=gte.{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&order=period_end.asc&limit={limit}"
        f"&select=period_end,pv_estimate,pv_estimate10,pv_estimate90"
    )
    req = urllib.request.Request(url, headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    _SOLCAST_CACHE = {"ts": now, "data": data}
    return data


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))
        if params.get("type") == "load":
            # Load profile model: per-slot, per-month average house load
            try:
                self._send(_fetch_load_profile())
            except Exception as e:
                print(f"[load_model] {e}")
                self._send([], 200)
        elif params.get("action") == "horizon_analysis":
            if not SUPABASE_URL or not SUPABASE_KEY:
                self._send({"error": "missing Supabase env vars"}, 500)
                return
            try:
                self._send(_horizon_analysis())
            except Exception as e:
                print(f"[horizon_analysis] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
        elif params.get("action") == "solcast_fetch":
            try:
                self._send(_solcast_fetch())
            except Exception as e:
                print(f"[solcast fetch] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
        elif params.get("action") == "solcast_read":
            try:
                days = int(params.get("days", 2))
                self._send(_solcast_read(days))
            except Exception as e:
                print(f"[solcast read] {e}")
                self._send([], 200)
        elif params.get("action") == "build":
            if not SUPABASE_URL or not SUPABASE_KEY:
                self._send({"error": "missing Supabase env vars"}, 500)
                return
            try:
                self._send(_build_model())
            except Exception as e:
                print(f"[solar_model build] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
        else:
            try:
                self._send(_fetch_model())
            except Exception as e:
                print(f"[solar_model] {e}")
                self._send([], 200)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
