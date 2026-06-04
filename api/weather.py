"""Combined weather forecast: Open-Meteo GTI × met.no cloud correction.

Why two sources
---------------
Open-Meteo provides Global Tilted Irradiance (GTI) already adjusted for panel
tilt/azimuth — the best freely available solar irradiance estimate.  But its
cloud model is global NWP.

Met.no (Norwegian Met Institute) runs a high-resolution NWP model specifically
tuned for Scandinavia, updated every hour.  Crucially it provides *separate*
low / medium / high cloud fractions, which matter for solar:
  low clouds  (stratus/fog)  → ~85% irradiance reduction
  medium clouds (altocumulus) → ~50% reduction
  high clouds  (cirrus)       → ~10% reduction

We use Open-Meteo's GTI as the base clear-sky-anchored estimate, then scale it
by the ratio of met.no's height-weighted cloud transmission vs Open-Meteo's
implied transmission.  The result is stored in Supabase weather_forecast so
the TOU suggestion engine can read it without making any extra API calls.

Update cadence
--------------
Met.no sets Expires ~30 min ahead; we cache for 55 min so one Lambda reuse
within a model run shares the fetch.  Cold starts re-fetch if Supabase data
is >55 min old.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LAT = 59.28
LON = 18.00
PANEL_TILT = 45
PANEL_AZ   = -68   # degrees from south, negative = east of south

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SVC = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                or os.environ.get("SUPABASE_ANON_KEY", ""))
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

CACHE_TTL = 55 * 60   # seconds — one model-run window

# Cloud-height transmission coefficients (fraction of irradiance that passes)
# Low clouds are optically thick; high cirrus is nearly transparent.
TRANS_LO  = 0.15   # low cloud layer transmits 15%
TRANS_MID = 0.50   # medium cloud layer transmits 50%
TRANS_HI  = 0.90   # high cloud layer transmits 90%

# In-process cache (survives warm Lambda reuse)
_cache: dict = {"data": None, "ts": 0.0}

OM_URL = (
    f"https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    f"&hourly=temperature_2m,cloudcover,windspeed_10m,shortwave_radiation"
    f",global_tilted_irradiance"
    f"&tilt={PANEL_TILT}&azimuth={PANEL_AZ}"
    f"&daily=sunrise,sunset"
    f"&timezone=Europe%2FStockholm&forecast_days=3"
)

METNO_URL = (
    f"https://api.met.no/weatherapi/locationforecast/2.0/complete"
    f"?lat={LAT}&lon={LON}"
)

HEADERS_OM    = {"User-Agent": "electricity-dashboard/weather"}
HEADERS_METNO = {"User-Agent": "electricity-dashboard/1.0 https://github.com/electricity-dashboard"}


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch(url: str, headers: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _fetch_om() -> dict:
    return _fetch(OM_URL, HEADERS_OM)


def _fetch_metno() -> dict:
    return _fetch(METNO_URL, HEADERS_METNO)


# ---------------------------------------------------------------------------
# GTI adjustment
# ---------------------------------------------------------------------------

def _cloud_transmission(lo: float, mid: float, hi: float) -> float:
    """
    Height-weighted cloud transmission (0–1) given % cloud fractions.

    We model three independent semi-transparent layers.  Total transmission
    is the product of each layer's fractional contribution:
      T = (lo_frac × TRANS_LO + clear_lo)
        × (mid_frac × TRANS_MID + clear_mid)
        × (hi_frac × TRANS_HI + clear_hi)
    where clear_x = 1 - frac_x (the clear portion of that layer passes 100%).
    """
    lo_f  = lo  / 100
    mid_f = mid / 100
    hi_f  = hi  / 100
    t_lo  = lo_f  * TRANS_LO  + (1 - lo_f)
    t_mid = mid_f * TRANS_MID + (1 - mid_f)
    t_hi  = hi_f  * TRANS_HI  + (1 - hi_f)
    return t_lo * t_mid * t_hi


def _adjust_gti(gti_om: float, om_cloud_pct: float,
                lo: float, mid: float, hi: float) -> float:
    """
    Scale Open-Meteo GTI using met.no's height-weighted cloud transmission.

    Open-Meteo's GTI already encodes their cloud model.  We infer what
    clear-sky GTI they assumed, then reapply met.no's transmission instead.

    Clear-sky estimate: GTI_clear ≈ GTI_om / T_om
    Adjusted:          GTI_adj   = GTI_clear × T_metno
    """
    if gti_om <= 0:
        return 0.0
    # Open-Meteo implied transmission (simple linear cloud model)
    t_om   = max(1 - om_cloud_pct / 100, 0.05)   # floor at 5% to avoid div/0
    # Met.no height-weighted transmission
    t_metno = _cloud_transmission(lo, mid, hi)
    # Reconstruct clear-sky and reapply
    gti_clear = gti_om / t_om
    gti_adj   = gti_clear * t_metno
    # Clamp: can't be negative or implausibly more than 2× base
    return round(max(0.0, min(gti_adj, gti_om * 2.0)), 2)


# ---------------------------------------------------------------------------
# Build combined forecast
# ---------------------------------------------------------------------------

def _build_combined(om: dict, metno: dict) -> dict:
    """
    Merge Open-Meteo and met.no into a unified hourly dict keyed by ISO hour.
    Returns the combined response structure and a list of rows for Supabase.
    """
    # Index met.no by truncated hour (UTC ISO string "YYYY-MM-DDTHH:00:00Z")
    metno_by_hour: dict[str, dict] = {}
    for entry in metno.get("properties", {}).get("timeseries", []):
        t    = entry["time"]                     # "2026-06-01T21:00:00Z"
        hour = t[:13] + ":00:00Z"               # normalise to full hour
        d    = entry["data"]["instant"]["details"]
        metno_by_hour[hour] = {
            "cloud":    d.get("cloud_area_fraction", 0),
            "cloud_lo": d.get("cloud_area_fraction_low", 0),
            "cloud_mi": d.get("cloud_area_fraction_medium", 0),
            "cloud_hi": d.get("cloud_area_fraction_high", 0),
            "temp":     d.get("air_temperature", None),
            "wind":     d.get("wind_speed", None),
        }

    # Build OM hourly arrays (Open-Meteo returns Stockholm local time strings)
    om_h   = om["hourly"]
    times  = om_h["time"]                        # "2026-06-01T00:00"
    n      = len(times)

    def _arr(key):
        a = om_h.get(key, [])
        return a + [None] * (n - len(a))

    om_cloud = _arr("cloudcover")
    om_gti   = _arr("global_tilted_irradiance")
    om_sw    = _arr("shortwave_radiation")
    om_temp  = _arr("temperature_2m")
    om_wind  = _arr("windspeed_10m")

    # Output arrays (same indexing as OM times — local Stockholm)
    out_cloud     = []
    out_cloud_lo  = []
    out_cloud_mid = []
    out_cloud_hi  = []
    out_gti_om    = []
    out_gti_adj   = []
    out_sw        = []
    out_temp      = []
    out_wind      = []
    sb_rows       = []

    for i, t_local in enumerate(times):
        # Convert local "YYYY-MM-DDTHH:MM" → UTC ISO for met.no lookup
        # Stockholm is UTC+2 in summer; just try both offsets
        try:
            dt_local = datetime.fromisoformat(t_local)
            dt_utc   = dt_local - timedelta(hours=2)   # CEST offset
            utc_key  = dt_utc.strftime("%Y-%m-%dT%H:00:00Z")
        except Exception:
            utc_key  = ""

        mn = metno_by_hour.get(utc_key, {})

        gti_om_v   = float(om_gti[i]   or 0)
        sw_v       = float(om_sw[i]    or 0)
        om_cld_v   = float(om_cloud[i] or 0)
        temp_v     = om_temp[i] if mn.get("temp") is None else mn["temp"]
        wind_v     = om_wind[i] if mn.get("wind") is None else mn["wind"]
        cloud_v    = mn.get("cloud",    om_cld_v)
        cloud_lo_v = mn.get("cloud_lo", 0)
        cloud_mi_v = mn.get("cloud_mi", 0)
        cloud_hi_v = mn.get("cloud_hi", 0)

        if mn:
            gti_adj_v = _adjust_gti(gti_om_v, om_cld_v,
                                    cloud_lo_v, cloud_mi_v, cloud_hi_v)
        else:
            gti_adj_v = gti_om_v   # no met.no data for this hour — use OM as-is

        out_cloud.append(cloud_v)
        out_cloud_lo.append(cloud_lo_v)
        out_cloud_mid.append(cloud_mi_v)
        out_cloud_hi.append(cloud_hi_v)
        out_gti_om.append(gti_om_v)
        out_gti_adj.append(gti_adj_v)
        out_sw.append(sw_v)
        out_temp.append(temp_v)
        out_wind.append(wind_v)

        # Row for Supabase (store UTC time)
        if utc_key:
            sb_rows.append({
                "valid_time":    utc_key,
                "temp_c":        round(float(temp_v), 2) if temp_v is not None else None,
                "wind_ms":       round(float(wind_v), 2) if wind_v is not None else None,
                "cloud_pct":     round(cloud_v, 1),
                "cloud_lo_pct":  round(cloud_lo_v, 1),
                "cloud_mid_pct": round(cloud_mi_v, 1),
                "cloud_hi_pct":  round(cloud_hi_v, 1),
                "cloud_om_pct":  round(om_cld_v, 1),
                "gti_om":        round(gti_om_v, 2),
                "gti_adj":       round(gti_adj_v, 2),
                "shortwave_wm2": round(sw_v, 2),
            })

    combined = {
        "hourly": {
            "time":                   times,
            "temperature_2m":         out_temp,
            "cloudcover":             out_cloud,       # met.no total (replaces OM)
            "cloud_lo":               out_cloud_lo,
            "cloud_mid":              out_cloud_mid,
            "cloud_hi":               out_cloud_hi,
            "windspeed_10m":          out_wind,
            "shortwave_radiation":    out_sw,
            "global_tilted_irradiance": out_gti_om,   # raw OM GTI
            "gti_adjusted":           out_gti_adj,     # met.no-corrected GTI
        },
        "daily":  om.get("daily", {}),
        "source": "open-meteo+met.no",
    }
    return combined, sb_rows


# ---------------------------------------------------------------------------
# Supabase persistence
# ---------------------------------------------------------------------------

def _sb_headers(service: bool = False) -> dict:
    key = SUPABASE_SVC if service else SUPABASE_KEY
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }


def _load_from_supabase() -> list | None:
    """Return rows for the next 48h if the most recent is <55 min old."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        now_utc   = datetime.now(timezone.utc)
        from_time = now_utc.strftime("%Y-%m-%dT%H:00:00Z")
        cutoff    = (now_utc - timedelta(minutes=55)).isoformat()

        # Check freshness of most recent row near now
        url = (f"{SUPABASE_URL}/rest/v1/weather_forecast"
               f"?valid_time=gte.{from_time}"
               f"&order=valid_time.asc"
               f"&limit=48"
               f"&select=*")
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        if not rows:
            return None
        # Check how old the data is
        saved = datetime.fromisoformat(rows[0]["saved_at"].replace("Z", "+00:00"))
        age_min = (now_utc - saved).total_seconds() / 60
        if age_min > 55:
            return None
        return rows
    except Exception as e:
        print(f"[weather] Supabase load error: {e}")
        return None


def _load_stale_from_supabase(from_date: str) -> dict | None:
    """Read all weather columns from Supabase with no freshness check.
    Used when Open-Meteo is unavailable and the in-process cache is cold.
    Returns the same combined-format dict as _rows_to_combined().
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from datetime import date as _date
        d = _date.fromisoformat(from_date)
        from_utc = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                    - timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00Z")
        url = (f"{SUPABASE_URL}/rest/v1/weather_forecast"
               f"?valid_time=gte.{from_utc}"
               f"&order=valid_time.asc"
               f"&limit=72"
               f"&select=*")
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=6) as r:
            rows = json.loads(r.read())
        if not rows:
            return None
        print(f"[weather] stale fallback: {len(rows)} rows from Supabase")
        return _rows_to_combined(rows)
    except Exception as e:
        print(f"[weather] stale fallback error: {e}")
        return None


def _load_gti_fallback(from_date: str) -> dict | None:
    """Read GTI from Supabase with no freshness check — best-available fallback.

    from_date: 'YYYY-MM-DD' (Stockholm local date; we fetch from midnight UTC-ish)
    Returns a dict compatible with extractHourlyGTI in the frontend:
      { "hourly": { "time": [...], "global_tilted_irradiance": [...] } }
    Times are Stockholm local 'YYYY-MM-DDTHH:MM'.
    Uses gti_adj (met.no-corrected) in global_tilted_irradiance for best quality.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        # Fetch from the day before from_date (UTC) to cover midnight-CEST wrap
        from datetime import date as _date
        d = _date.fromisoformat(from_date)
        from_utc = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                    - timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00Z")
        url = (f"{SUPABASE_URL}/rest/v1/weather_forecast"
               f"?valid_time=gte.{from_utc}"
               f"&order=valid_time.asc"
               f"&limit=72"          # up to 3 days
               f"&select=valid_time,gti_adj,gti_om")
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=6) as r:
            rows = json.loads(r.read())
        if not rows:
            return None
        tz_sthlm = timezone(timedelta(hours=2))
        times, gti = [], []
        for row in rows:
            dt_utc   = datetime.fromisoformat(row["valid_time"].replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(tz_sthlm)
            times.append(dt_local.strftime("%Y-%m-%dT%H:%M"))
            # Prefer met.no-corrected GTI; fall back to raw OM
            val = row.get("gti_adj") if row.get("gti_adj") is not None else row.get("gti_om")
            gti.append(float(val) if val is not None else 0.0)
        print(f"[weather] GTI fallback from Supabase: {len(rows)} rows from {from_date}")
        return {"hourly": {"time": times, "global_tilted_irradiance": gti},
                "source": "supabase_fallback"}
    except Exception as e:
        print(f"[weather] GTI fallback error: {e}")
        return None


def _save_to_supabase(rows: list) -> None:
    if not SUPABASE_URL or not SUPABASE_SVC or not rows:
        return
    try:
        # Upsert in batches of 50 to stay within request size limits
        for i in range(0, len(rows), 50):
            batch = rows[i:i+50]
            body  = json.dumps(batch).encode()
            req   = urllib.request.Request(
                f"{SUPABASE_URL}/rest/v1/weather_forecast",
                data=body, method="POST",
                headers={**_sb_headers(service=True),
                         "Prefer": "resolution=merge-duplicates"},
            )
            urllib.request.urlopen(req, timeout=8).read()
        print(f"[weather] saved {len(rows)} rows to Supabase")
    except Exception as e:
        print(f"[weather] Supabase save error: {e}")


def _rows_to_combined(rows: list) -> dict:
    """Reconstruct combined-format response from Supabase rows."""
    tz_stockholm = timezone(timedelta(hours=2))

    times       = []
    temp        = []
    cloud       = []
    cloud_lo    = []
    cloud_mid   = []
    cloud_hi    = []
    wind        = []
    sw          = []
    gti_om_arr  = []
    gti_adj_arr = []

    for r in rows:
        # Convert UTC valid_time → Stockholm local (YYYY-MM-DDTHH:MM)
        dt_utc   = datetime.fromisoformat(r["valid_time"].replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(tz_stockholm)
        times.append(dt_local.strftime("%Y-%m-%dT%H:%M"))
        temp.append(r.get("temp_c"))
        cloud.append(r.get("cloud_pct"))
        cloud_lo.append(r.get("cloud_lo_pct"))
        cloud_mid.append(r.get("cloud_mid_pct"))
        cloud_hi.append(r.get("cloud_hi_pct"))
        wind.append(r.get("wind_ms"))
        sw.append(r.get("shortwave_wm2"))
        gti_om_arr.append(r.get("gti_om"))
        gti_adj_arr.append(r.get("gti_adj"))

    # Derive sunrise/sunset from shortwave radiation crossings
    # (first hour where sw goes above / drops below 10 W/m²)
    sunrise_str = None
    sunset_str  = None
    for i in range(len(sw) - 1):
        prev = float(sw[i]   or 0)
        nxt  = float(sw[i+1] or 0)
        if not sunrise_str and prev <= 10 and nxt > 10:
            sunrise_str = times[i + 1]   # local Stockholm "YYYY-MM-DDTHH:MM"
        if sunrise_str and not sunset_str and prev > 10 and nxt <= 10:
            sunset_str = times[i]

    return {
        "hourly": {
            "time":                     times,
            "temperature_2m":           temp,
            "cloudcover":               cloud,
            "cloud_lo":                 cloud_lo,
            "cloud_mid":                cloud_mid,
            "cloud_hi":                 cloud_hi,
            "windspeed_10m":            wind,
            "shortwave_radiation":      sw,
            "global_tilted_irradiance": gti_om_arr,
            "gti_adjusted":             gti_adj_arr,
        },
        "daily": {
            "sunrise": [sunrise_str] if sunrise_str else [],
            "sunset":  [sunset_str]  if sunset_str  else [],
        },
        "source": "supabase",
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        import urllib.parse as _up
        params = dict(_up.parse_qsl(_up.urlparse(self.path).query))

        # ?action=gti&date=YYYY-MM-DD — serve cached GTI as Open-Meteo-compatible
        # response with no freshness gate.  Used as a fallback by the frontend
        # when Open-Meteo is unavailable.
        if params.get("action") == "gti":
            date_str = params.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            data = _load_gti_fallback(date_str)
            if data:
                self._send(data)
            else:
                self._send({"error": "no GTI data in cache"}, 503)
            return

        # 1. In-process cache (warm Lambda)
        age = time.monotonic() - _cache["ts"]
        if _cache["data"] and age < CACHE_TTL:
            self._send(_cache["data"])
            return

        # 2. Supabase cache (cross cold-starts)
        sb_rows = _load_from_supabase()
        if sb_rows:
            data = _rows_to_combined(sb_rows)
            _cache["data"] = data
            _cache["ts"]   = time.monotonic()
            self._send(data)
            return

        # 3. Fetch fresh from both APIs
        try:
            om    = _fetch_om()
            metno = _fetch_metno()
            data, sb_rows = _build_combined(om, metno)
            _cache["data"] = data
            _cache["ts"]   = time.monotonic()
            # Persist (fire-and-forget errors)
            try:
                _save_to_supabase(sb_rows)
            except Exception as e:
                print(f"[weather] save error (non-fatal): {e}")
            self._send(data)
        except Exception as e:
            print(f"[weather] fetch error: {e}")
            # Serve stale cache if available (in-process first, then Supabase)
            if _cache["data"]:
                self._send(_cache["data"])
                return
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            stale = _load_stale_from_supabase(today_str)
            if stale:
                self._send(stale)
            else:
                self._send({"error": str(e)}, 500)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
