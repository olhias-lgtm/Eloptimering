"""
grid — Swedish national electricity production from eSett Open Data API.

GET ?action=fetch          — fetch last 9 days from eSett, upsert into grid_production
GET ?days=N                — serve last N days from grid_production (default 7, max 30)

Data source: https://api.opendata.esett.com/EXP16/Volumes
  - No API key required
  - 15-min resolution, ~1–2 day settlement lag
  - Fields: nuclear, hydro, wind, windOffshore, solar, thermal, energyStorage, other, total (MW)
  - We average 4×15-min slots → hourly rows before storing

Cron: 0 8 * * *  (08:00 UTC = 10:00 CEST — previous day's settlement is available)
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

ESETT_BASE    = "https://api.opendata.esett.com"
SE_MBAS       = ["10Y1001A1001A44P", "10Y1001A1001A45N",
                 "10Y1001A1001A46L", "10Y1001A1001A47J"]   # SE1–SE4

# Sweden nuclear nominal capacity (MW) — hardcoded; changes only when reactors open/close
NUCLEAR_NOMINAL_MW = 6804.0   # Forsmark 1+2+3 (3210) + Ringhals 3+4 (2194) + Oskarshamn 3 (1400)

# In-process cache (warm Lambda reuse)
_CACHE: dict = {"data": None, "ts": 0.0}
_CACHE_TTL   = 3600   # 60 minutes


def _sb_headers(service: bool = False) -> dict:
    key = SUPABASE_KEY
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }


# ---------------------------------------------------------------------------
# Fetch from eSett
# ---------------------------------------------------------------------------

def _fetch_esett(days: int = 9) -> list[dict]:
    """
    Fetch the last `days` days of 15-min data from eSett for all SE MBAs combined.
    Returns a list of raw row dicts.
    """
    now_utc = datetime.now(timezone.utc)
    start   = now_utc - timedelta(days=days)

    def _fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    qs = (f"start={urllib.parse.quote(_fmt(start))}"
          f"&end={urllib.parse.quote(_fmt(now_utc))}"
          + "".join(f"&mba={m}" for m in SE_MBAS))
    url = f"{ESETT_BASE}/EXP16/Volumes?{qs}"

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[grid] eSett fetch error: {e}")
        return []


def _aggregate_to_hourly(raw_rows: list) -> list:
    """
    Average the 15-min eSett rows into hourly rows keyed on UTC hour boundary.
    Returns list of dicts suitable for grid_production upsert.
    """
    buckets: dict[str, list] = {}
    for row in raw_rows:
        ts_utc_str = row.get("timestampUTC", "")
        if not ts_utc_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_utc_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Truncate to top-of-hour
        hour_dt = dt.replace(minute=0, second=0, microsecond=0)
        key = hour_dt.isoformat()
        buckets.setdefault(key, []).append(row)

    result = []
    for ts_key, rows in sorted(buckets.items()):
        def avg(field):
            vals = [float(r[field]) for r in rows if r.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        result.append({
            "ts":         ts_key,
            "nuclear_mw": avg("nuclear"),
            "hydro_mw":   avg("hydro"),
            "wind_mw":    round(
                (sum(float(r.get("wind") or 0) for r in rows) +
                 sum(float(r.get("windOffshore") or 0) for r in rows)) / len(rows), 2
            ),
            "solar_mw":   avg("solar"),
            "thermal_mw": avg("thermal"),
            "other_mw":   round(
                (sum(float(r.get("energyStorage") or 0) for r in rows) +
                 sum(float(r.get("other") or 0) for r in rows)) / len(rows), 2
            ),
            "total_mw":   avg("total"),
        })
    return result


# ---------------------------------------------------------------------------
# Supabase persistence
# ---------------------------------------------------------------------------

def _upsert_grid(rows: list[dict]) -> int:
    """Upsert hourly rows into grid_production. Returns number of rows sent."""
    if not SUPABASE_URL or not SUPABASE_KEY or not rows:
        return 0
    for i in range(0, len(rows), 100):
        batch = rows[i:i + 100]
        body  = json.dumps(batch).encode()
        req   = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/grid_production?on_conflict=ts",
            data=body, method="POST",
            headers={
                **_sb_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            raise RuntimeError(f"Supabase upsert HTTP {e.code}: {err[:300]}") from e
    return len(rows)


def _load_from_supabase(days: int):
    """Load last `days` days from grid_production. Returns None on error."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        url = (
            f"{SUPABASE_URL}/rest/v1/grid_production"
            f"?ts=gte.{urllib.parse.quote(since)}"
            f"&order=ts.asc"
            f"&select=ts,nuclear_mw,hydro_mw,wind_mw,solar_mw,thermal_mw,other_mw,total_mw"
        )
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[grid] Supabase load error: {e}")
        return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _do_fetch() -> dict:
    """Fetch from eSett → aggregate → upsert to Supabase."""
    raw   = _fetch_esett(days=9)
    if not raw:
        return {"ok": False, "error": "eSett returned no data"}
    hourly = _aggregate_to_hourly(raw)
    written = _upsert_grid(hourly)
    # Invalidate in-process cache so next GET re-reads fresh data
    _CACHE["data"] = None
    _CACHE["ts"]   = 0.0
    print(f"[grid] fetch OK — {len(raw)} raw rows → {written} hourly rows upserted")
    return {"ok": True, "raw_rows": len(raw), "hourly_rows": written}


def _do_serve(days: int) -> list[dict]:
    """Serve grid data from cache or Supabase."""
    # In-process cache hit
    if _CACHE["data"] and (time.monotonic() - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["data"]

    rows = _load_from_supabase(days)
    if rows is None:
        rows = []

    # Normalise field names for frontend
    out = []
    for r in rows:
        out.append({
            "ts":         r["ts"],
            "nuclear":    float(r.get("nuclear_mw") or 0),
            "hydro":      float(r.get("hydro_mw")   or 0),
            "wind":       float(r.get("wind_mw")    or 0),
            "solar":      float(r.get("solar_mw")   or 0),
            "thermal":    float(r.get("thermal_mw") or 0),
            "other":      float(r.get("other_mw")   or 0),
            "total":      float(r.get("total_mw")   or 0),
            "nuclear_nominal_mw": NUCLEAR_NOMINAL_MW,
        })

    _CACHE["data"] = out
    _CACHE["ts"]   = time.monotonic()
    return out


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))
        action = params.get("action", "")

        if action == "fetch":
            try:
                result = _do_fetch()
                self._send(result, 200 if result.get("ok") else 502)
            except Exception as e:
                print(f"[grid] fetch error: {e}")
                self._send({"ok": False, "error": str(e)}, 500)
            return

        # Default: serve data
        try:
            days = min(30, max(1, int(params.get("days", 7))))
        except ValueError:
            days = 7
        try:
            rows = _do_serve(days)
            self._send(rows)
        except Exception as e:
            print(f"[grid] serve error: {e}")
            self._send({"ok": False, "error": str(e)}, 500)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control",  "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
