"""
growatt_tou
  GET                    — read current TOU settings via newTlxApi.do?op=getTlxSetData
  GET ?action=suggest    — return latest saved TOU suggestion for tomorrow
  POST                   — write one or more time segments via newTcpsetAPI.do?op=tlxSet
  POST ?action=build_suggest — (re)build and save tomorrow's TOU suggestion (cron at 00:05)

Key findings from reverse-engineering (PyPi_GrowattServer 1.6.0 / HA PR #133319):
  • Read  uses newTlxApi.do?op=getTlxSetData   with body  serialNum=<sn>
  • Write uses newTcpsetAPI.do?op=tlxSet        with body  serialNum=<sn>
    type=time_segment{1-9}, param1..param6, param7..param19="" (all required)
  • Auth  is the standard newTwoLoginAPI.do session (same as all other ops)
  • The "installer password" is a ShinePhone UI concept, not an API parameter

Suggestion algorithm (build_suggest):
  1. Fetch tomorrow's hourly spot prices (elprisetjustnu.se)
  2. Fetch tomorrow's hourly GTI forecast (Open-Meteo) × solar model ratios → kW
  3. Apply SOLAR_HAIRCUT (15%) safety margin
  4. Classify each hour:
       Battery First  — cheap grid hour (< LOW_PRICE_PCTILE) AND low solar
       Grid First     — expensive hour (> HIGH_PRICE_PCTILE) AND low solar
       Load First     — everything else (solar hours + neutral)
  5. Merge consecutive same-mode runs → up to MAX_SEGMENTS TOU segments
  6. Upsert into Supabase tou_suggestions table

POST body (JSON) — single segment:
  { "segment_id": 1, "mode": 1, "start_hour": 0, "start_min": 0,
    "end_hour": 8, "end_min": 0, "enabled": true }

POST body (JSON) — multiple segments at once:
  { "segments": [ { ...same fields... }, ... ] }
"""
import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta, timezone, datetime
from http.server import BaseHTTPRequestHandler

from _growatt import get_session

SERIAL = "KJN6EXV00L"
BASE   = "https://openapi.growatt.com"

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_KEY)

TOU_PASSWORD  = "79"
MAX_FAILURES  = 3
LOCKOUT_HOURS = 24

MODE_NAMES = {0: "Load First", 1: "Battery First", 2: "Grid First"}

# Suggestion tuning
SOLAR_HAIRCUT      = 0.15   # assume 15% less solar than forecast
SOLAR_LOW_KW       = 0.3    # hourly avg kW below which we treat the hour as "dark"
LOW_PRICE_PCTILE   = 0.30   # cheapest 30% of hours → candidate for Battery First
HIGH_PRICE_PCTILE  = 0.70   # most expensive 30% of hours → candidate for Grid First
MAX_SEGMENTS       = 6      # leave headroom below the 9-segment limit

LAT        = 59.28
LON        = 18.00
PANEL_TILT = 45
PANEL_AZ   = -68
AREA       = "SE3"


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _sb_headers(service=False):
    key = SUPABASE_SERVICE if service else SUPABASE_KEY
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }


# ---------------------------------------------------------------------------
# IP lockout (stored in Supabase tou_ip_lockout, service-role only)
# ---------------------------------------------------------------------------

def _get_client_ip(handler_self) -> str:
    forwarded = handler_self.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or handler_self.client_address[0]


def _check_lockout(ip: str) -> dict:
    """Returns {"locked": bool, "fail_count": int, "locked_until": str|None}."""
    url = f"{SUPABASE_URL}/rest/v1/tou_ip_lockout?ip=eq.{urllib.parse.quote(ip)}&limit=1"
    req = urllib.request.Request(url, headers=_sb_headers(service=True))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        if not rows:
            return {"locked": False, "fail_count": 0, "locked_until": None}
        row = rows[0]
        until = row.get("locked_until")
        if until:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < until_dt:
                return {"locked": True, "fail_count": row["fail_count"], "locked_until": until}
        return {"locked": False, "fail_count": row.get("fail_count", 0), "locked_until": None}
    except Exception as e:
        print(f"[lockout check] {e}")
        return {"locked": False, "fail_count": 0, "locked_until": None}


def _record_failure(ip: str) -> int:
    """Increment fail count; lock if >= MAX_FAILURES. Returns new fail_count."""
    import urllib.parse as _up
    state = _check_lockout(ip)
    new_count = state["fail_count"] + 1
    locked_until = None
    if new_count >= MAX_FAILURES:
        from datetime import timedelta
        locked_until = (datetime.now(timezone.utc) + timedelta(hours=LOCKOUT_HOURS)).isoformat()
    row = {
        "ip":           ip,
        "fail_count":   new_count,
        "locked_until": locked_until,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps([row]).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/tou_ip_lockout?on_conflict=ip",
        data=body, method="POST",
        headers={**_sb_headers(service=True), "Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[lockout record] {e}")
    return new_count


def _clear_failures(ip: str):
    """Reset fail count on successful auth."""
    row = {"ip": ip, "fail_count": 0, "locked_until": None,
           "last_attempt": datetime.now(timezone.utc).isoformat()}
    body = json.dumps([row]).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/tou_ip_lockout?on_conflict=ip",
        data=body, method="POST",
        headers={**_sb_headers(service=True), "Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"[lockout clear] {e}")


# ---------------------------------------------------------------------------
# Read current TOU settings
# ---------------------------------------------------------------------------

def _read_tou() -> dict:
    """Return raw getTlxSetData response + normalised segment list if parseable."""
    sess = get_session()
    sess.ensure_ready()
    r = sess._s.post(
        BASE + "/newTlxApi.do",
        params={"op": "getTlxSetData"},
        data={"serialNum": SERIAL},
        timeout=10,
    )
    if not r.text.strip():
        return {"ok": False, "note": "empty response — device may not support getTlxSetData via this account"}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "raw": r.text[:500]}

    obj  = data.get("obj") or {}
    bean = obj.get("tlxSetBean") or obj
    segments = []
    for i in range(1, 10):
        start = (bean.get(f"forcedTimeStart{i}")
                 or bean.get(f"startTime{i}")
                 or bean.get(f"time{i}Start"))
        stop  = (bean.get(f"forcedTimeStop{i}")
                 or bean.get(f"endTime{i}")
                 or bean.get(f"time{i}Stop"))
        mode  = (bean.get(f"time{i}Mode")
                 or bean.get(f"segment{i}Mode"))
        en    = (bean.get(f"forcedStopSwitch{i}")
                 or bean.get(f"segmentEnable{i}")
                 or bean.get(f"time{i}Enable"))
        if start or stop or mode is not None:
            segments.append({
                "segment_id":  i,
                "start":       start,
                "stop":        stop,
                "mode":        int(mode) if mode is not None else None,
                "mode_name":   MODE_NAMES.get(int(mode)) if mode is not None else None,
                "enabled":     bool(int(en)) if en is not None else None,
            })

    return {"ok": True, "raw": data, "segments": segments}


# ---------------------------------------------------------------------------
# Write segments
# ---------------------------------------------------------------------------

def _write_segment(segment_id: int, mode: int, start_hour: int, start_min: int,
                   end_hour: int, end_min: int, enabled: bool) -> dict:
    if not 1 <= segment_id <= 9:
        raise ValueError(f"segment_id must be 1-9, got {segment_id}")
    if mode not in (0, 1, 2):
        raise ValueError(f"mode must be 0/1/2 (Load/Battery/Grid First), got {mode}")

    sess = get_session()
    sess.ensure_ready()

    payload = {
        "serialNum": SERIAL,
        "type":      f"time_segment{segment_id}",
        "param1":    str(mode),
        "param2":    str(start_hour),
        "param3":    str(start_min),
        "param4":    str(end_hour),
        "param5":    str(end_min),
        "param6":    "1" if enabled else "0",
        **{f"param{i}": "" for i in range(7, 20)},
    }

    r = sess._s.post(
        BASE + "/newTcpsetAPI.do",
        params={"op": "tlxSet"},
        data=payload,
        timeout=15,
    )
    try:
        result = r.json()
    except Exception:
        result = {"_status": r.status_code, "_raw": r.text[:300]}

    success = result.get("success") is True or result.get("msg") == "200"
    print(f"[growatt_tou] seg {segment_id} ({start_hour:02d}:{start_min:02d}–"
          f"{end_hour:02d}:{end_min:02d} mode={mode} en={enabled}): {result}")
    return {"segment_id": segment_id, "success": success, "response": result}


def _write_many(segments: list) -> list:
    results = []
    for seg in segments:
        try:
            res = _write_segment(
                segment_id = int(seg["segment_id"]),
                mode       = int(seg["mode"]),
                start_hour = int(seg.get("start_hour", 0)),
                start_min  = int(seg.get("start_min",  0)),
                end_hour   = int(seg.get("end_hour",   0)),
                end_min    = int(seg.get("end_min",    0)),
                enabled    = bool(seg.get("enabled", True)),
            )
        except Exception as e:
            res = {"segment_id": seg.get("segment_id"), "success": False, "error": str(e)}
        results.append(res)
    return results


# ---------------------------------------------------------------------------
# Suggestion: data fetchers
# ---------------------------------------------------------------------------

def _fetch_prices_for_date(d: date) -> list:
    """Return list of {time_start, SEK_per_kWh} for the given date from elprisetjustnu."""
    y, mo, day = d.isoformat().split("-")
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{y}/{mo}-{day}_{AREA}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "electricity-dashboard/tou-suggest"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _fetch_gti_forecast(d: date) -> dict:
    """Return {hour: gti_wm2} for tomorrow from Open-Meteo forecast."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=global_tilted_irradiance"
        f"&tilt={PANEL_TILT}&azimuth={PANEL_AZ}"
        f"&timezone=Europe%2FStockholm"
        f"&forecast_days=2&past_days=0"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "electricity-dashboard/tou-suggest"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read())
    target = d.isoformat()
    result = {}
    for t, v in zip(data["hourly"]["time"], data["hourly"]["global_tilted_irradiance"]):
        if t.startswith(target) and v is not None:
            hour = int(t[11:13])
            result[hour] = float(v)
    return result


def _fetch_solar_model() -> dict:
    """Return {slot: ratio} from Supabase solar_model table. slot = 0-min/5."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}
    url = f"{SUPABASE_URL}/rest/v1/solar_model?select=slot,ratio&order=slot.asc"
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
        return {int(row["slot"]): row["ratio"] for row in rows if row["ratio"] is not None}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Suggestion: optimiser
# ---------------------------------------------------------------------------

def _build_suggestion(for_date: date) -> dict:
    """
    Compute an optimised TOU schedule for for_date.
    Returns {"ok": True, "for_date": ..., "segments": [...], "reasoning": "..."}
    """
    prices_raw = _fetch_prices_for_date(for_date)
    if not prices_raw:
        return {"ok": False, "error": f"No spot prices available for {for_date}"}

    # Build hourly price map  (öre/kWh incl 25% moms — elprisetjustnu returns SEK_per_kWh)
    # Keep in SEK/kWh for comparison
    price_by_hour = {}
    for row in prices_raw:
        t = row.get("time_start", "")
        if len(t) >= 13:
            h = int(t[11:13])
            price_by_hour[h] = float(row.get("SEK_per_kWh", 0))

    if not price_by_hour:
        return {"ok": False, "error": "Could not parse prices"}

    # Solar forecast with model correction + haircut
    gti = _fetch_gti_forecast(for_date)
    model = _fetch_solar_model()

    # Convert GTI → kW per hour using per-slot ratios
    # slot = hour * 12 (top-of-hour slot); if no ratio use simple panel_kWp estimate
    PANEL_KWP = 12.0
    solar_by_hour = {}
    for h in range(24):
        slot = h * 12
        gti_val = gti.get(h, 0.0)
        if slot in model:
            kw = model[slot] * gti_val  # ratio is kW / (W/m²)
        elif gti_val > 0:
            kw = (gti_val / 1000) * PANEL_KWP * 0.85  # simple physics fallback
        else:
            kw = 0.0
        solar_by_hour[h] = kw * (1 - SOLAR_HAIRCUT)

    # Percentile thresholds
    prices_sorted = sorted(price_by_hour.values())
    n = len(prices_sorted)
    low_thresh  = prices_sorted[int(n * LOW_PRICE_PCTILE)]
    high_thresh = prices_sorted[int(n * HIGH_PRICE_PCTILE)]

    # Classify each hour
    # 0=Load First, 1=Battery First, 2=Grid First
    hour_mode = {}
    for h in range(24):
        price = price_by_hour.get(h)
        solar = solar_by_hour.get(h, 0.0)
        is_dark = solar < SOLAR_LOW_KW

        if price is not None and price <= low_thresh and is_dark:
            hour_mode[h] = 1  # Battery First: cheap + dark → charge from grid
        elif price is not None and price >= high_thresh and is_dark:
            hour_mode[h] = 2  # Grid First: expensive + dark → discharge battery
        else:
            hour_mode[h] = 0  # Load First: solar hours or neutral

    # Merge consecutive same-mode hours into runs, skip Load First runs
    # (Load First is the inverter default; we only need segments for modes 1 and 2)
    runs = []
    cur_mode = hour_mode[0]
    cur_start = 0
    for h in range(1, 25):
        mode = hour_mode.get(h, hour_mode[23])  # h=24 sentinel
        if mode != cur_mode or h == 24:
            if cur_mode != 0:  # skip Load First
                runs.append({"mode": cur_mode, "start": cur_start, "end": h})
            cur_mode = mode
            cur_start = h

    # Limit to MAX_SEGMENTS
    runs = runs[:MAX_SEGMENTS]

    # Build segment list
    segments = []
    for i, run in enumerate(runs, 1):
        sh, sm = run["start"], 0
        eh, em = run["end"] % 24, 0
        segments.append({
            "segment_id": i,
            "mode":       run["mode"],
            "mode_name":  MODE_NAMES[run["mode"]],
            "start_hour": sh, "start_min": sm,
            "end_hour":   eh, "end_min":   em,
            "enabled":    True,
            "start":      f"{sh:02d}:00",
            "stop":       f"{eh:02d}:00",
        })

    # Human-readable reasoning
    cheap_hours  = sorted(h for h, m in hour_mode.items() if m == 1)
    exp_hours    = sorted(h for h, m in hour_mode.items() if m == 2)
    solar_peak   = max(solar_by_hour.items(), key=lambda x: x[1])
    reasoning = (
        f"Priser {for_date}: min {min(prices_sorted):.2f} – max {max(prices_sorted):.2f} SEK/kWh. "
        f"Laddning (Battery First) timmar: {cheap_hours}. "
        f"Urladdning (Grid First) timmar: {exp_hours}. "
        f"Solpeak (−15%): {solar_peak[1]:.1f} kW kl {solar_peak[0]:02d}:00. "
        f"Solproduktion antagen 15% lägre än prognos."
    )

    return {
        "ok":         True,
        "for_date":   for_date.isoformat(),
        "segments":   segments,
        "reasoning":  reasoning,
        "price_range": {"min": round(min(prices_sorted), 4),
                        "max": round(max(prices_sorted), 4),
                        "low_thresh":  round(low_thresh, 4),
                        "high_thresh": round(high_thresh, 4)},
        "solar_peak_kw": round(solar_peak[1], 2),
    }


# ---------------------------------------------------------------------------
# Suggestion: Supabase persistence
# ---------------------------------------------------------------------------

def _save_suggestion(result: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    row = {
        "for_date":  result["for_date"],
        "segments":  result["segments"],
        "reasoning": result.get("reasoning", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps([row]).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/tou_suggestions?on_conflict=for_date",
        data=body, method="POST",
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def _load_suggestion(for_date: date) -> dict:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"ok": False, "error": "no supabase config"}
    url = (f"{SUPABASE_URL}/rest/v1/tou_suggestions"
           f"?for_date=eq.{for_date.isoformat()}&limit=1")
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read())
        if rows:
            return {"ok": True, **rows[0]}
        return {"ok": False, "error": "no suggestion saved yet for this date"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))
        action = params.get("action", "")

        if action == "suggest":
            # Return today's saved suggestion
            self._send(_load_suggestion(date.today()))
            return

        try:
            self._send(_read_tou())
        except Exception as e:
            print(f"[growatt_tou GET] {e}")
            self._send({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))
        action = params.get("action", "")

        if action == "build_suggest":
            # Cron entry point — no password required (internal)
            try:
                result = _build_suggestion(date.today())
                if result.get("ok"):
                    _save_suggestion(result)
                self._send(result)
            except Exception as e:
                print(f"[growatt_tou build_suggest] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length)) if length else {}

            # --- Authentication ---
            ip = _get_client_ip(self)
            lockout = _check_lockout(ip)
            if lockout["locked"]:
                self._send({
                    "ok": False,
                    "error": "locked",
                    "message": f"För många misslyckade försök. Åtkomst blockerad i {LOCKOUT_HOURS}h.",
                }, 403)
                return

            provided_pwd = body.get("pwd", "")
            if provided_pwd != TOU_PASSWORD:
                fails = _record_failure(ip)
                remaining = max(0, MAX_FAILURES - fails)
                self._send({
                    "ok":        False,
                    "error":     "wrong_password",
                    "remaining": remaining,
                    "message":   f"Fel lösenord. {remaining} försök kvar." if remaining else
                                 f"Fel lösenord. IP låst i {LOCKOUT_HOURS}h.",
                }, 401)
                return

            # Password correct — clear any previous failures
            _clear_failures(ip)

            # --- Write ---
            # Multiple segments
            if "segments" in body:
                results = _write_many(body["segments"])
                self._send({"ok": True, "results": results})
                return

            # Single segment
            res = _write_segment(
                segment_id = int(body["segment_id"]),
                mode       = int(body["mode"]),
                start_hour = int(body.get("start_hour", 0)),
                start_min  = int(body.get("start_min",  0)),
                end_hour   = int(body.get("end_hour",   0)),
                end_min    = int(body.get("end_min",    0)),
                enabled    = bool(body.get("enabled", True)),
            )
            self._send({"ok": True, **res})

        except Exception as e:
            print(f"[growatt_tou POST] {e}")
            self._send({"ok": False, "error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

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
