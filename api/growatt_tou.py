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
  4. Fetch per-hour seasonal load profile from get_load_profile_by_slot RPC
  5. Storm Watch — fetch 5-day GTI forecast, detect ≥2 consecutive low-solar days;
     if triggered: raise effective SOC_FLOOR to 40% and rec_soc_floor to 40%
  6. LP-equivalent greedy dispatch (_lp_dispatch):
       Enumerate (charge, discharge) hour pairs sorted by spread descending.
       Commit each pair if SoC feasibility holds after assignment.
       Battery First  — committed charge hours (grid-charges battery)
       Grid First     — committed discharge hours (battery → export/load)
       Load First     — all other hours (solar self-consumption default)
  6. Merge consecutive same-mode runs → up to MAX_SEGMENTS TOU segments
  7. Upsert into Supabase tou_suggestions table

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

NOTIFY_TO   = "olhias@gmail.com"
NOTIFY_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "")
NOTIFY_PASS = os.environ.get("NOTIFY_EMAIL_PASS", "")

MODE_NAMES = {0: "Load First", 1: "Battery First", 2: "Grid First"}

# Suggestion tuning
SOLAR_HAIRCUT      = 0.15   # assume 15% less solar than forecast
SOLAR_LOW_KW       = 0.3    # hourly avg kW below which we treat the hour as "dark"
LOW_PRICE_PCTILE   = 0.30   # cheapest 30% of hours → candidate for Battery First
HIGH_PRICE_PCTILE  = 0.70   # most expensive 30% of hours → candidate for Grid First
MAX_SEGMENTS       = 6      # leave headroom below the 9-segment limit

# Storm Watch — pre-charge when consecutive low-solar days are forecast
STORM_LOW_KWH      = 3.0    # daily estimated harvest below this → "low-solar day"
STORM_DAYS         = 2      # number of consecutive low-solar days that triggers watch
STORM_SOC_FLOOR    = 0.40   # raise battery floor to 40 % during storm watch

# Adaptive SoC floor (Gap 4) — scale floor based on tomorrow's expected solar harvest
# Thresholds are estimated kWh for D+1 (12 kWp system, Stockholm).
# Each tuple: (min_kwh_threshold, floor_fraction)  — first matching threshold wins.
# Battery degradation cost (Gap 6) — cost per kWh cycled through the battery.
# Formula: battery_replacement_cost / (expected_cycles × usable_kwh_per_cycle)
# 80 000 kr / (6 000 cycles × 20 kWh) ≈ 0.067 kr/kWh
# The LP will not schedule a cycle unless the price spread exceeds this cost.
DEGRADATION_KR_KWH = 0.067

ADAPTIVE_SOC_THRESHOLDS = [
    (15.0, 0.10),   # good solar day  → floor 10 % (battery will recharge fully)
    ( 8.0, 0.15),   # moderate solar  → floor 15 %
    ( 4.0, 0.25),   # poor solar      → floor 25 %
    ( 0.0, 0.30),   # very poor / none→ floor 30 % (Storm Watch overrides to 40 % if ≥2 days)
]

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
# TOU read cache (Supabase tou_cache — single row, public read, service write)
# Keeps the last-known TOU segments so page loads never touch Growatt.
# Cache is invalidated (refreshed from Growatt) only when:
#   • It doesn't exist yet
#   • It is older than TOU_CACHE_MAX_AGE_MIN
#   • A write or reset succeeds (cache updated immediately with new values)
# ---------------------------------------------------------------------------

TOU_CACHE_MAX_AGE_MIN = 60  # treat cache as stale after 60 minutes


def _load_tou_cache() -> dict | None:
    """Return cached segments if they exist and are fresh, else None."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        url = f"{SUPABASE_URL}/rest/v1/tou_cache?id=eq.1&select=segments,discharge_pct,soc_floor_pct,saved_at"
        req = urllib.request.Request(url, headers=_sb_headers(service=False))
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        if not rows:
            return None
        row = rows[0]
        saved_at = datetime.fromisoformat(row["saved_at"].replace("Z", "+00:00"))
        age_min  = (datetime.now(timezone.utc) - saved_at).total_seconds() / 60
        if age_min > TOU_CACHE_MAX_AGE_MIN:
            print(f"[tou_cache] stale ({age_min:.0f} min old) — will refresh from Growatt")
            return None
        print(f"[tou_cache] hit ({age_min:.1f} min old)")
        result = {"ok": True, "segments": row["segments"], "source": "cache"}
        if row.get("discharge_pct")  is not None: result["discharge_pct"]  = row["discharge_pct"]
        if row.get("soc_floor_pct")  is not None: result["soc_floor_pct"]  = row["soc_floor_pct"]
        return result
    except Exception as e:
        print(f"[tou_cache] load error: {e}")
        return None


def _save_tou_cache(segments: list,
                    discharge_pct: int | None = None,
                    soc_floor_pct: int | None = None) -> None:
    """Upsert current segments (and optionally discharge_pct / soc_floor_pct) into tou_cache.

    Tries with extra columns first; if Supabase rejects (columns not yet added),
    falls back to saving without them so the segments cache always persists.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE:
        return

    def _do_upsert(payload: dict) -> None:
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/tou_cache",
            data=body, method="POST",
            headers={**_sb_headers(service=True), "Prefer": "resolution=merge-duplicates"},
        )
        urllib.request.urlopen(req, timeout=5).read()

    base = {
        "id":       1,
        "segments": segments,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    extras = {}
    if discharge_pct is not None: extras["discharge_pct"] = discharge_pct
    if soc_floor_pct is not None: extras["soc_floor_pct"] = soc_floor_pct

    try:
        _do_upsert({**base, **extras})
        print(f"[tou_cache] saved {len(segments)} segments"
              + (f" discharge_pct={discharge_pct}" if discharge_pct is not None else "")
              + (f" soc_floor_pct={soc_floor_pct}" if soc_floor_pct is not None else ""))
    except Exception as e:
        if extras:
            # Extra columns may not exist yet — retry with base only
            print(f"[tou_cache] save with extras failed ({e}), retrying without")
            try:
                _do_upsert(base)
                print(f"[tou_cache] saved {len(segments)} segments (no extras)")
            except Exception as e2:
                print(f"[tou_cache] save error: {e2}")
        else:
            print(f"[tou_cache] save error: {e}")


# ---------------------------------------------------------------------------
# Read current TOU settings
# ---------------------------------------------------------------------------

def _read_tou(force_refresh: bool = False) -> dict:
    """Return normalised segment list.

    Serves from the Supabase tou_cache when available and fresh.
    Only calls Growatt when the cache is missing, stale, or force_refresh=True.
    """
    if force_refresh:
        # Honour cooldown even on explicit refresh requests (protects against
        # repeated clicks / debugging loops hitting Growatt too fast)
        if _check_force_refresh_cooldown():
            force_refresh = False  # fall through to cache
        else:
            _record_force_refresh()

    if not force_refresh:
        cached = _load_tou_cache()
        if cached:
            return cached

    # Cache miss or forced refresh — fetch live from Growatt
    print("[GROWATT LIVE CALL] _read_tou (getTlxSetData)")
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

    # Extract power + SoC settings from bean
    discharge_pct = None
    charge_pct    = None
    normal_w      = None
    soc_floor_pct = None
    try:
        dpct = bean.get("discharge_power") or bean.get("disChargePowerCommand")
        cpct = bean.get("charge_power")    or bean.get("chargePowerCommand")
        nw   = bean.get("normalPower")
        sfl  = bean.get("discharge_stop_soc") or bean.get("wdisChargeSOCLowLimit")
        if dpct is not None:
            discharge_pct = int(float(dpct))
        if cpct is not None:
            charge_pct = int(float(cpct))
        if nw is not None:
            normal_w = int(float(nw))
        if sfl is not None:
            soc_floor_pct = int(float(sfl))
    except Exception as e:
        print(f"[tou] power/soc parse error: {e}")

    result = {"ok": True, "raw": data, "segments": segments, "source": "growatt"}
    if discharge_pct  is not None: result["discharge_pct"]  = discharge_pct
    if charge_pct     is not None: result["charge_pct"]     = charge_pct
    if normal_w       is not None: result["normal_w"]       = normal_w
    if soc_floor_pct  is not None: result["soc_floor_pct"]  = soc_floor_pct

    # Persist for future requests
    _save_tou_cache(segments, discharge_pct=discharge_pct, soc_floor_pct=soc_floor_pct)
    return result


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
    print(f"[GROWATT LIVE CALL] _write_segment seg={segment_id} mode={mode} "
          f"{start_hour:02d}:{start_min:02d}–{end_hour:02d}:{end_min:02d} en={enabled}")
    _record_write_op()

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


# ---------------------------------------------------------------------------
# Growatt call-rate guards
# These module-level timestamps survive warm Lambda reuse and prevent
# accidental bursts during debugging or repeated UI interactions.
# ---------------------------------------------------------------------------
import time as _time

WRITE_INTERVAL_SECS       = 1.0   # pause between sequential segment writes
WRITE_OP_COOLDOWN_SECS    = 30    # minimum seconds between separate write operations
FORCE_REFRESH_COOLDOWN_SECS = 120 # minimum seconds between force-refresh reads

_last_write_op_at:      float = 0  # time.monotonic() of last _write_segment call
_last_force_refresh_at: float = 0  # time.monotonic() of last force-refresh


def _check_write_cooldown() -> str | None:
    """Return an error message if a write was attempted too soon, else None."""
    elapsed = _time.monotonic() - _last_write_op_at
    if _last_write_op_at and elapsed < WRITE_OP_COOLDOWN_SECS:
        remaining = int(WRITE_OP_COOLDOWN_SECS - elapsed)
        return f"Vänta {remaining}s innan nästa sparning (skydd mot burst-skrivning)."
    return None


def _record_write_op():
    global _last_write_op_at
    _last_write_op_at = _time.monotonic()


def _check_force_refresh_cooldown() -> bool:
    """Return True if force-refresh should be suppressed (too soon)."""
    elapsed = _time.monotonic() - _last_force_refresh_at
    if _last_force_refresh_at and elapsed < FORCE_REFRESH_COOLDOWN_SECS:
        remaining = int(FORCE_REFRESH_COOLDOWN_SECS - elapsed)
        print(f"[growatt_tou] force-refresh suppressed — cooldown {remaining}s remaining")
        return True
    return False


def _record_force_refresh():
    global _last_force_refresh_at
    _last_force_refresh_at = _time.monotonic()

def _write_discharge_power(percent: int) -> dict:
    """Set battery discharge rate as % of normalPower. Range 1–100."""
    if not 1 <= percent <= 100:
        raise ValueError(f"percent must be 1–100, got {percent}")
    sess = get_session()
    sess.ensure_ready()
    print(f"[GROWATT LIVE CALL] _write_discharge_power {percent}%")
    _record_write_op()
    payload = {
        "serialNum": SERIAL,
        "type":      "discharge_power",
        "param1":    str(percent),
        **{f"param{i}": "" for i in range(2, 20)},
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
    print(f"[growatt_tou] discharge_power={percent}%: {result}")
    return {"success": success, "discharge_pct": percent, "response": result}


def _write_soc_floor(percent: int) -> dict:
    """Set battery discharge stop SoC (floor). Range 5–50 %, typically 10 %."""
    if not 5 <= percent <= 50:
        raise ValueError(f"percent must be 5–50, got {percent}")
    sess = get_session()
    sess.ensure_ready()
    print(f"[GROWATT LIVE CALL] _write_soc_floor {percent}%")
    _record_write_op()
    payload = {
        "serialNum": SERIAL,
        "type":      "discharge_stop_soc",
        "param1":    str(percent),
        **{f"param{i}": "" for i in range(2, 20)},
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
    print(f"[growatt_tou] discharge_stop_soc={percent}%: {result}")
    return {"success": success, "soc_floor_pct": percent, "response": result}


def _write_many(segments: list) -> list:
    import time
    results = []
    for i, seg in enumerate(segments):
        if i > 0:
            time.sleep(WRITE_INTERVAL_SECS)
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
    """Return list of {time_start, SEK_per_kWh} for the given date from elprisetjustnu.

    Falls back to the local Supabase spot_prices table if the external API is unavailable.
    """
    y, mo, day = d.isoformat().split("-")
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{y}/{mo}-{day}_{AREA}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "electricity-dashboard/tou-suggest"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())
            if rows:
                return rows
    except Exception as e:
        print(f"[tou suggest] elprisetjustnu unavailable ({e}), trying Supabase fallback")

    # Fallback: read from our own spot_prices table
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        cest = timezone(timedelta(hours=2 if 3 < d.month < 11 else 1))
        day_start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=cest).isoformat()
        day_end   = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=cest).isoformat()
        sb_url = (f"{SUPABASE_URL}/rest/v1/spot_prices"
                  f"?ts=gte.{urllib.parse.quote(day_start)}"
                  f"&ts=lte.{urllib.parse.quote(day_end)}"
                  f"&area=eq.{AREA}"
                  f"&order=ts.asc&select=ts,sek_per_kwh")
        sb_req = urllib.request.Request(sb_url, headers=_sb_headers())
        with urllib.request.urlopen(sb_req, timeout=8) as r:
            rows = json.loads(r.read())
        # Convert 15-min Supabase rows → hourly elprisetjustnu shape
        # Normalize "+00" → "+00:00" for Python 3.9 fromisoformat compatibility
        from collections import defaultdict
        hour_buckets: dict = defaultdict(list)
        for row in rows:
            ts  = row.get("ts", "").replace("+00:00", "Z").replace("+00", "Z")
            sek = row.get("sek_per_kwh")
            if ts and sek is not None:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    hour_buckets[dt.replace(minute=0, second=0, microsecond=0)].append(float(sek))
                except (ValueError, TypeError):
                    continue
        result = []
        for dt_hour, vals in sorted(hour_buckets.items()):
            result.append({"time_start": dt_hour.isoformat(), "SEK_per_kWh": sum(vals) / len(vals)})
        print(f"[tou suggest] Supabase fallback: {len(result)} hourly price rows for {d}")
        return result
    except Exception as e:
        print(f"[tou suggest] Supabase price fallback error: {e}")
        return []


def _fetch_gti_forecast(d: date) -> tuple:
    """Return ({hour: gti_wm2}, {hour: cloud_cover_pct}) for date d.

    Tries the Supabase weather_forecast table first (gti_adj + cloud_cover columns).
    Falls back to a direct Open-Meteo call if Supabase has no data.
    Cloud cover is used by _build_suggestion to scale the solar haircut per hour
    (Gap 7 — probabilistic forecast uncertainty).
    """
    from datetime import timezone as _tz

    # Try Supabase weather_forecast first (gti_adj = met.no-corrected GTI)
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            cest = _tz(timedelta(hours=2))
            day_start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=cest)
            day_end   = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=cest)
            url = (f"{SUPABASE_URL}/rest/v1/weather_forecast"
                   f"?valid_time=gte.{urllib.parse.quote(day_start.astimezone(_tz.utc).isoformat())}"
                   f"&valid_time=lte.{urllib.parse.quote(day_end.astimezone(_tz.utc).isoformat())}"
                   f"&order=valid_time.asc"
                   f"&select=valid_time,gti_adj,cloud_cover")
            req = urllib.request.Request(url, headers=_sb_headers())
            with urllib.request.urlopen(req, timeout=8) as r:
                rows = json.loads(r.read())
            if rows:
                gti_h   = {}
                cloud_h = {}
                for row in rows:
                    dt_utc  = datetime.fromisoformat(row["valid_time"].replace("Z", "+00:00"))
                    dt_cest = dt_utc.astimezone(cest)
                    h = dt_cest.hour
                    gti_h[h]   = float(row["gti_adj"] or 0)
                    cloud_h[h] = float(row["cloud_cover"] or 0) if row.get("cloud_cover") is not None else 50.0
                print(f"[tou_suggest] GTI+cloud loaded from Supabase for {d} ({len(gti_h)} hours)")
                return gti_h, cloud_h
        except Exception as e:
            print(f"[tou_suggest] Supabase GTI fetch failed, falling back to Open-Meteo: {e}")

    # Fallback: direct Open-Meteo call — fetch GTI + cloudcover in one request
    try:
        print(f"[tou_suggest] Fetching GTI+cloud direct from Open-Meteo for {d}")
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}"
            f"&hourly=global_tilted_irradiance,cloudcover"
            f"&tilt={PANEL_TILT}&azimuth={PANEL_AZ}"
            f"&timezone=Europe%2FStockholm"
            f"&forecast_days=2&past_days=0"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "electricity-dashboard/tou-suggest"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        target  = d.isoformat()
        gti_h   = {}
        cloud_h = {}
        times   = data["hourly"]["time"]
        gtis    = data["hourly"]["global_tilted_irradiance"]
        clouds  = data["hourly"].get("cloudcover", [None] * len(times))
        for t, gv, cv in zip(times, gtis, clouds):
            if t.startswith(target):
                h = int(t[11:13])
                gti_h[h]   = float(gv or 0)
                cloud_h[h] = float(cv or 50.0)
        return gti_h, cloud_h
    except Exception as e:
        print(f"[tou_suggest] Open-Meteo GTI also unavailable: {e}. Returning empty GTI.")
        return {}, {}  # _build_suggestion handles missing GTI gracefully (solar_by_hour → 0)


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
# Battery simulation constants
# ---------------------------------------------------------------------------
BATT_KWH    = 20.0   # usable capacity (4 × 5 kWh APX)
C_RATE_KW   = 12.0   # max charge/discharge (0.6 C × 20 kWh)
SOC_FLOOR   = 0.10   # 10 % minimum SoC
SOC_CEIL    = 1.00   # 100 % maximum SoC
CHARGE_EFF  = 0.95   # round-trip charge efficiency

# Tariff constants used in cost simulation (Swedish SE3 baseline)
NATAVG_IN_ORE   = 26.0   # nätavgift inmatning  öre/kWh
ENERGISKATT_ORE = 54.25  # energiskatt          öre/kWh
FORTUM_ORE      = 4.9    # Fortum påslag        öre/kWh
NATNYTTA_ORE    = 5.50   # nätnytta dagtid      öre/kWh (conservative, high-load rate)
MOMS            = 1.25   # 25 % moms multiplier

# All-in import rate (öre/kWh incl moms) — spot is added per hour
FIXED_IMPORT_ORE = (NATAVG_IN_ORE + ENERGISKATT_ORE + FORTUM_ORE) * MOMS
# Export bonus on top of spot (öre/kWh)
EXPORT_BONUS_ORE = NATNYTTA_ORE * MOMS


def _fetch_avg_load_kw() -> float:
    """Return average hourly load kW from the last 30 days of daily_summary.
    Used as a flat fallback when the per-hour load model is unavailable."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0.6  # safe fallback: ~14 kWh/day
    since = (date.today() - timedelta(days=30)).isoformat()
    url = (f"{SUPABASE_URL}/rest/v1/daily_summary"
           f"?day=gte.{since}&select=solar_kwh,export_kwh,import_kwh&order=day.desc&limit=30")
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read())
        if not rows:
            return 0.6
        daily_loads = [
            float(r.get("solar_kwh") or 0)
            - float(r.get("export_kwh") or 0)
            + float(r.get("import_kwh") or 0)
            for r in rows
        ]
        avg_daily_kwh = sum(daily_loads) / len(daily_loads)
        return max(0.3, avg_daily_kwh / 24)
    except Exception as e:
        print(f"[tou sim] load forecast error: {e}")
        return 0.6


def _fetch_load_profile_for_date(for_date: date) -> dict:
    """Return {hour: avg_load_kw} for the given date using the seasonal load model.

    Calls the get_load_profile_by_slot Supabase RPC (same one used by the
    frontend) which returns per-slot, per-month averages from 90 days of
    energy_readings.  Averages 12 slots → 1 hour.

    Fallback chain:
      1. Model for target month (any day_count)
      2. Nearest calendar month with data (seasonal proxy)
      3. Flat _fetch_avg_load_kw() value
    """
    flat = _fetch_avg_load_kw()
    fallback = {h: flat for h in range(24)}

    if not SUPABASE_URL or not SUPABASE_KEY:
        return fallback
    try:
        url = f"{SUPABASE_URL}/rest/v1/rpc/get_load_profile_by_slot?lookback_days=90"
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read())

        if not rows:
            return fallback

        target_month = for_date.month

        # Build {month: {slot: avg_load_kw}}
        by_month: dict = {}
        for row in rows:
            m = int(row["month"])
            s = int(row["slot"])
            if m not in by_month:
                by_month[m] = {}
            by_month[m][s] = float(row["avg_load_kw"])

        # Pick the best available month — target first, then nearest by calendar distance
        months_available = sorted(
            by_month.keys(),
            key=lambda m: min(abs(m - target_month), 12 - abs(m - target_month))
        )
        slot_map = by_month.get(target_month) or by_month.get(months_available[0], {})

        if len(slot_map) < 144:   # need at least half the day covered
            return fallback

        # Average 12 slots per hour
        result = {}
        for h in range(24):
            vals = [slot_map[s] for s in range(h * 12, (h + 1) * 12) if s in slot_map]
            result[h] = (sum(vals) / len(vals)) if vals else flat

        print(f"[tou lp] load profile for month {target_month}: "
              f"min {min(result.values()):.2f} kW, max {max(result.values()):.2f} kW, "
              f"avg {sum(result.values())/24:.2f} kW")
        return result

    except Exception as e:
        print(f"[tou lp] load profile fetch error: {e}")
        return fallback


def _fetch_soc_start() -> float:
    """Return current battery SoC (0.0–1.0) from Growatt, default 0.5."""
    try:
        sess = get_session()
        sess.ensure_ready()
        r = sess._s.post(
            BASE + "/newTlxApi.do",
            params={"op": "getTlxLastData"},
            data={"serialNum": SERIAL},
            timeout=8,
        )
        data = r.json()
        obj  = data.get("obj") or {}
        # Field name varies by firmware; try common variants
        soc_raw = (obj.get("soc") or obj.get("bmsSoc") or
                   obj.get("batteryCapacity") or obj.get("capacity"))
        if soc_raw is not None:
            soc = float(soc_raw)
            return soc / 100 if soc > 1 else soc
    except Exception as e:
        print(f"[tou sim] SoC fetch error: {e}")
    return 0.5  # default to 50 %


def _fetch_solar_forecast_days(n_days: int = 5) -> list:
    """Return list of estimated solar kWh for D+1 … D+n_days.

    Tries the Supabase weather_forecast table (gti_adj column) first, then
    falls back to a direct Open-Meteo call.  Converts hourly GTI to kWh using
    the same solar model ratios as _build_suggestion.

    Returns a list of floats, index 0 = D+1, index 1 = D+2, …
    On any error returns an empty list (caller must handle gracefully).
    """
    from datetime import timezone as _tz
    today   = date.today()
    PANEL_KWP = 12.0

    # Load model ratios once (used to convert GTI → kW)
    model = _fetch_solar_model()

    def _gti_hours_to_kwh(hourly: dict) -> float:
        """Sum hourly GTI dict → estimated kWh using model ratios."""
        total = 0.0
        for h, gti_val in hourly.items():
            if gti_val <= 0:
                continue
            slot = h * 12
            if slot in model:
                kw = model[slot] * gti_val
            else:
                kw = (gti_val / 1000) * PANEL_KWP * 0.85
            total += kw * (1 - SOLAR_HAIRCUT)
        return total

    # --- Try Supabase weather_forecast ---
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            cest = _tz(timedelta(hours=2))
            start_dt = datetime(today.year, today.month, today.day,
                                0, 0, 0, tzinfo=cest) + timedelta(days=1)
            end_dt   = start_dt + timedelta(days=n_days)
            url = (f"{SUPABASE_URL}/rest/v1/weather_forecast"
                   f"?valid_time=gte.{urllib.parse.quote(start_dt.astimezone(_tz.utc).isoformat())}"
                   f"&valid_time=lt.{urllib.parse.quote(end_dt.astimezone(_tz.utc).isoformat())}"
                   f"&order=valid_time.asc"
                   f"&select=valid_time,gti_adj")
            req = urllib.request.Request(url, headers=_sb_headers())
            with urllib.request.urlopen(req, timeout=8) as r:
                rows = json.loads(r.read())

            if rows:
                # Bucket into days (CEST local date)
                from collections import defaultdict as _dd
                by_day: dict = _dd(dict)
                for row in rows:
                    dt_utc  = datetime.fromisoformat(row["valid_time"].replace("Z", "+00:00"))
                    dt_cest = dt_utc.astimezone(cest)
                    d_key   = dt_cest.date()
                    by_day[d_key][dt_cest.hour] = float(row["gti_adj"] or 0)

                result = []
                for offset in range(1, n_days + 1):
                    d_key  = today + timedelta(days=offset)
                    hourly = by_day.get(d_key, {})
                    result.append(_gti_hours_to_kwh(hourly))

                print(f"[storm_watch] solar forecast D+1…D+{n_days}: "
                      f"{[round(v,1) for v in result]} kWh (from Supabase)")
                return result
        except Exception as e:
            print(f"[storm_watch] Supabase forecast fetch failed: {e}, trying Open-Meteo")

    # --- Fallback: Open-Meteo ---
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}"
            f"&hourly=global_tilted_irradiance"
            f"&tilt={PANEL_TILT}&azimuth={PANEL_AZ}"
            f"&timezone=Europe%2FStockholm"
            f"&forecast_days={n_days + 1}&past_days=0"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "electricity-dashboard/storm-watch"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())

        from collections import defaultdict as _dd
        by_day: dict = _dd(dict)
        for t, v in zip(data["hourly"]["time"], data["hourly"]["global_tilted_irradiance"]):
            d_str = t[:10]
            h     = int(t[11:13])
            by_day[d_str][h] = float(v or 0)

        result = []
        for offset in range(1, n_days + 1):
            d_key  = (today + timedelta(days=offset)).isoformat()
            hourly = by_day.get(d_key, {})
            result.append(_gti_hours_to_kwh(hourly))

        print(f"[storm_watch] solar forecast D+1…D+{n_days}: "
              f"{[round(v,1) for v in result]} kWh (from Open-Meteo)")
        return result

    except Exception as e:
        print(f"[storm_watch] Open-Meteo forecast also failed: {e}")
        return []


def _check_storm_watch(daily_kwh: list) -> tuple:
    """Detect consecutive low-solar days in the forecast.

    daily_kwh : list where index 0 = D+1, index 1 = D+2, …

    Returns (triggered: bool, low_days: list[int], note: str).
    'low_days' contains the 1-based day offsets that are below STORM_LOW_KWH.
    'triggered' is True only when STORM_DAYS or more consecutive days starting
    at D+1 are low (we care about the near-term window, not isolated future dips).
    """
    if not daily_kwh:
        return False, [], ""

    low_days = [i + 1 for i, kwh in enumerate(daily_kwh) if kwh < STORM_LOW_KWH]

    # Check for STORM_DAYS consecutive low days starting at D+1
    triggered = False
    run = 0
    for offset in range(1, len(daily_kwh) + 1):
        if offset in low_days:
            run += 1
            if run >= STORM_DAYS:
                triggered = True
                break
        else:
            run = 0

    if triggered:
        kwh_strs = [f"D+{d}: {daily_kwh[d-1]:.1f} kWh" for d in low_days[:STORM_DAYS]]
        note = (f"⛈ Storm Watch: {STORM_DAYS} dagar låg solinstrålning: "
                f"{', '.join(kwh_strs)}. "
                f"SoC-golv höjt till {int(STORM_SOC_FLOOR*100)}% — förladdar inför molnigt väder.")
    else:
        note = ""

    return triggered, low_days, note


def _lp_dispatch(price_h: dict, solar_h: dict,
                 load_h: dict, soc_start: float,
                 soc_floor: float = SOC_FLOOR) -> dict:
    """Optimal 24-hour battery dispatch — LP-equivalent greedy algorithm.

    For a battery with linear costs and convex SoC constraints the optimal
    solution has the structure of a min-cost flow problem, solvable in
    O(H²) by greedy pair-matching.  No external LP solver required.

    Algorithm
    ---------
    1. Simulate a Load-First baseline to get the natural SoC trajectory.
    2. Enumerate every profitable (charge_hour c, discharge_hour d) pair where
       d > c, both hours are "dark" (solar < SOLAR_LOW_KW), and the spread
       exp_rate[d]×eff − imp_rate[c] > 0.
    3. Sort pairs by spread descending; greedily commit each pair if:
       a. Neither hour has already been assigned.
       b. After the assignment, the simulated SoC trajectory still shows
          meaningful charge at c and meaningful discharge at d.
       Committed pairs update the running SoC reference for subsequent checks.

    The greedy is LP-optimal here because:
    • Each decision variable (hour assignment) appears in exactly one
      constraint set (SoC balance), which has total-unimodularity.
    • We process highest-value pairs first, so no later swap can improve
      the objective without breaking a higher-value commitment.

    Returns
    -------
    {hour: 0|1|2}  —  0 = Load First, 1 = Battery First, 2 = Grid First
    """
    floor_kwh = soc_floor * BATT_KWH
    ceil_kwh  = BATT_KWH
    MIN_KWH   = 0.3   # minimum useful dispatch per hour (kWh)

    # All-in rates (kr/kWh)
    imp_rate = {h: (price_h.get(h, 0.0) * 100 + FIXED_IMPORT_ORE) / 100 for h in range(24)}
    exp_rate = {h: (price_h.get(h, 0.0) * 100 + EXPORT_BONUS_ORE) / 100 for h in range(24)}

    def _sim(modes: dict) -> list:
        """Simulate SoC kWh after each of the 24 hours for the given mode map."""
        soc   = soc_start * BATT_KWH
        track = []
        for h in range(24):
            s  = solar_h.get(h, 0.0)
            ld = load_h.get(h, 0.5)
            m  = modes.get(h, 0)

            if m == 1:   # Battery First — charge from grid first, then add solar surplus
                headroom = max(0.0, (ceil_kwh - soc) / CHARGE_EFF)
                grid_chg = min(C_RATE_KW, headroom)
                soc = min(ceil_kwh, soc + grid_chg * CHARGE_EFF)
                # Solar surplus after load still contributes
                solar_net = max(0.0, s - ld)
                extra = min(solar_net, max(0.0, (ceil_kwh - soc) / CHARGE_EFF))
                soc = min(ceil_kwh, soc + extra * CHARGE_EFF)

            elif m == 2: # Grid First — discharge battery to serve load + export surplus
                dis = min(C_RATE_KW, max(0.0, soc - floor_kwh))
                soc = max(floor_kwh, soc - dis)

            else:        # Load First — solar first, balance from/to battery
                net = s - ld
                if net > 0:
                    chg = min(net, C_RATE_KW, max(0.0, (ceil_kwh - soc) / CHARGE_EFF))
                    soc = min(ceil_kwh, soc + chg * CHARGE_EFF)
                else:
                    dis = min(-net, C_RATE_KW, max(0.0, soc - floor_kwh))
                    soc = max(floor_kwh, soc - dis)

            track.append(soc)
        return track

    # Dark hours: solar < SOLAR_LOW_KW — only schedule Battery/Grid First here.
    # Solar hours are best left as Load First (solar self-consumption is free).
    dark = {h for h in range(24) if solar_h.get(h, 0.0) < SOLAR_LOW_KW}

    # Baseline SoC trajectory (all Load First)
    modes    = {h: 0 for h in range(24)}
    cur_soc  = _sim(modes)

    # Enumerate profitable (charge, discharge) pairs
    pairs = []
    for ch in dark:
        for dh in dark:
            if dh <= ch:
                continue
            spread = exp_rate[dh] * CHARGE_EFF - imp_rate[ch]
            if spread > 0.01 + DEGRADATION_KR_KWH:
                pairs.append((spread, ch, dh))
    pairs.sort(reverse=True)   # highest spread first

    committed_charge    : set = set()
    committed_discharge : set = set()

    for spread, ch, dh in pairs:
        # Each hour can only be assigned one mode
        if ch in committed_charge or ch in committed_discharge:
            continue
        if dh in committed_charge or dh in committed_discharge:
            continue

        # Tentatively assign
        modes[ch] = 1
        modes[dh] = 2
        trial = _sim(modes)

        # Verify that the simulation actually charged meaningfully at ch
        soc_pre_ch  = (soc_start * BATT_KWH) if ch == 0 else trial[ch - 1]
        charged     = trial[ch] - soc_pre_ch

        # Verify that the simulation actually discharged meaningfully at dh
        soc_pre_dh  = (soc_start * BATT_KWH) if dh == 0 else trial[dh - 1]
        discharged  = soc_pre_dh - trial[dh]

        if charged < MIN_KWH * CHARGE_EFF or discharged < MIN_KWH:
            # Infeasible or trivial — revert
            modes[ch] = 0
            modes[dh] = 0
            continue

        committed_charge.add(ch)
        committed_discharge.add(dh)
        cur_soc = trial   # update running SoC reference for next iteration

    # Negative export rate hours: spot + nätnytta < 0 means exporting actually costs money.
    # Force Battery First for any such hour that isn't already assigned, so solar surplus
    # fills the battery rather than being exported at a loss.
    neg_export_hours = []
    for h in range(24):
        if exp_rate[h] >= 0:
            continue
        neg_export_hours.append(h)
        if h in committed_charge or h in committed_discharge:
            continue
        modes[h] = 1   # Battery First — maximise storage, minimise export
        committed_charge.add(h)

    n_charge    = len(committed_charge)
    n_discharge = len(committed_discharge)
    if neg_export_hours:
        print(f"[tou lp] negative export rate hours (spot+nätnytta<0): {neg_export_hours} "
              f"→ forced Battery First")
    print(f"[tou lp] scheduled {n_charge} Battery First + {n_discharge} Grid First hours "
          f"from {len(pairs)} candidate pairs")
    result = dict(modes)
    result['_neg_export_hours'] = neg_export_hours
    return result


def _simulate_battery(solar_h: dict, load_h,
                      mode_h: dict, price_h: dict,
                      soc_start: float) -> dict:
    """Hourly battery simulation. Returns daily totals only.

    solar_h   : {hour: kW}         — forecast solar (already haircut-adjusted)
    load_h    : float | {hour: kW} — per-hour load or flat average
    mode_h    : {hour: int}        — 0=Load First, 1=Battery First, 2=Grid First
    price_h   : {hour: SEK/kWh}    — spot price
    soc_start : float 0–1          — SoC at start of day
    """
    soc_kwh     = soc_start * BATT_KWH
    soc_floor_k = SOC_FLOOR * BATT_KWH
    soc_ceil_k  = SOC_CEIL  * BATT_KWH

    total_import_kwh = 0.0
    total_export_kwh = 0.0
    total_cost_kr    = 0.0
    total_earn_kr    = 0.0
    total_saved_kr   = 0.0

    for h in range(24):
        solar  = solar_h.get(h, 0.0)
        load   = load_h[h] if isinstance(load_h, dict) else load_h
        mode   = mode_h.get(h, 0)
        spot   = price_h.get(h)        # SEK/kWh, may be None

        import_kwh = 0.0
        export_kwh = 0.0

        if mode == 1:  # Battery First — force charge from grid
            headroom   = min(C_RATE_KW, (soc_ceil_k - soc_kwh) / CHARGE_EFF)
            solar_self = min(solar, load)
            solar_exc  = max(0.0, solar - load)
            chg_solar  = min(solar_exc, headroom)
            chg_grid   = max(0.0, headroom - chg_solar)
            soc_kwh   += (chg_solar + chg_grid) * CHARGE_EFF
            import_kwh = max(0.0, load - solar_self) + chg_grid
            export_kwh = max(0.0, solar_exc - chg_solar)

        elif mode == 2:  # Grid First — discharge battery
            avail      = min(C_RATE_KW, soc_kwh - soc_floor_k)
            net        = solar + avail - load
            export_kwh = max(0.0, net)
            import_kwh = max(0.0, -net)
            soc_kwh   -= avail

        else:  # Load First — normal operation
            net = solar - load
            if net >= 0:
                chg        = min(net, C_RATE_KW, (soc_ceil_k - soc_kwh) / CHARGE_EFF)
                soc_kwh   += chg * CHARGE_EFF
                export_kwh = max(0.0, net - chg)
                import_kwh = 0.0
            else:
                dis        = min(-net, C_RATE_KW, soc_kwh - soc_floor_k)
                soc_kwh   -= dis
                import_kwh = max(0.0, -net - dis)
                export_kwh = 0.0

        soc_kwh = max(soc_floor_k, min(soc_ceil_k, soc_kwh))

        total_import_kwh += import_kwh
        total_export_kwh += export_kwh

        if spot is not None:
            import_rate_ore = spot * 100 + FIXED_IMPORT_ORE
            export_rate_ore = spot * 100 + EXPORT_BONUS_ORE
            total_cost_kr  += import_kwh * import_rate_ore / 100
            # When export rate is negative (spot + nätnytta < 0), model as curtailment:
            # earn = 0 rather than negative. The Battery First mode above already minimises
            # export during these hours; any remaining surplus is treated as wasted kWh.
            if export_rate_ore > 0:
                total_earn_kr += export_kwh * export_rate_ore / 100
            # Savings = self-consumed kWh valued at full import rate
            self_kwh        = max(0.0, load - import_kwh)
            total_saved_kr += self_kwh * import_rate_ore / 100

    net_kr = total_earn_kr - total_cost_kr
    return {
        "import_kwh":  round(total_import_kwh, 3),
        "export_kwh":  round(total_export_kwh, 3),
        "cost_kr":     round(total_cost_kr,    2),
        "earn_kr":     round(total_earn_kr,    2),
        "saved_kr":    round(total_saved_kr,   2),
        "net_kr":      round(net_kr,           2),
        "soc_start":   round(soc_start,        3),
    }


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
    # Keep in SEK/kWh for comparison.
    # IMPORTANT: elprisetjustnu time_start is UTC (ends in "Z"). The Growatt inverter
    # uses local time (CEST = UTC+2 in summer, CET = UTC+1 in winter). We must
    # convert to local time before extracting the hour or every segment ends up
    # 2 hours early.
    _month = for_date.month
    _utc_off = 2 if 3 < _month < 11 else 1   # CEST Apr–Oct, CET Nov–Mar (approx)
    _tz_local = timezone(timedelta(hours=_utc_off))

    price_by_hour = {}
    for row in prices_raw:
        t = row.get("time_start", "")
        try:
            dt_utc = datetime.fromisoformat(t.replace("Z", "+00:00"))
            h = dt_utc.astimezone(_tz_local).hour
        except (ValueError, TypeError):
            continue
        price_by_hour[h] = float(row.get("SEK_per_kWh", 0))

    if not price_by_hour:
        return {"ok": False, "error": "Could not parse prices"}

    # Solar forecast with model correction + per-hour uncertainty haircut (Gap 7)
    gti, cloud_by_hour = _fetch_gti_forecast(for_date)
    model = _fetch_solar_model()

    # Convert GTI → kW per hour using per-slot ratios.
    # Haircut scales with cloud cover: low cloud → 10% (forecast reliable),
    # high cloud → 30% (GTI model less accurate under broken/overcast sky).
    # Formula: haircut = 0.10 + cloud_fraction * 0.20  (range 10–30%)
    PANEL_KWP = 12.0
    solar_by_hour = {}
    for h in range(24):
        slot    = h * 12
        gti_val = gti.get(h, 0.0)
        cloud   = cloud_by_hour.get(h, 50.0)          # default 50% if unknown
        haircut = 0.10 + (cloud / 100.0) * 0.20       # 10%–30%
        if slot in model:
            kw = model[slot] * gti_val
        elif gti_val > 0:
            kw = (gti_val / 1000) * PANEL_KWP * 0.85
        else:
            kw = 0.0
        solar_by_hour[h] = kw * (1 - haircut)

    avg_haircut = sum(
        0.10 + (cloud_by_hour.get(h, 50.0) / 100.0) * 0.20 for h in range(24)
    ) / 24

    prices_sorted = sorted(price_by_hour.values())

    # Current battery SoC — needed by LP for feasibility checks
    soc_start = _fetch_soc_start()

    # Per-hour load profile for tomorrow (seasonal monthly averages)
    load_by_hour = _fetch_load_profile_for_date(for_date)

    # Multi-day solar forecast — used by both Adaptive SoC floor (Gap 4) and Storm Watch (Gap 3)
    daily_solar_kwh = _fetch_solar_forecast_days(5)

    # Gap 4 — Adaptive SoC floor: scale floor based on D+1 (= for_date) expected harvest.
    # If solar tomorrow is poor the battery won't recharge during the day, so we must
    # not let it drain too low overnight going into that day.
    d1_kwh = daily_solar_kwh[0] if daily_solar_kwh else None
    if d1_kwh is not None:
        adaptive_floor = SOC_FLOOR   # default
        for kwh_thresh, floor_frac in ADAPTIVE_SOC_THRESHOLDS:
            if d1_kwh >= kwh_thresh:
                adaptive_floor = floor_frac
                break
    else:
        adaptive_floor = SOC_FLOOR   # no forecast data — stay conservative

    # Gap 3 — Storm Watch: hard override to 40 % when ≥2 consecutive low-solar days ahead
    storm_triggered, storm_low_days, storm_note = _check_storm_watch(daily_solar_kwh)

    # Final effective floor: adaptive (gap 4) ≥ base, storm watch (gap 3) overrides further
    effective_soc_floor = adaptive_floor
    if storm_triggered:
        effective_soc_floor = max(effective_soc_floor, STORM_SOC_FLOOR)

    print(f"[tou lp] SOC floor: adaptive={adaptive_floor:.0%}"
          + (f" → storm_watch={STORM_SOC_FLOOR:.0%}" if storm_triggered else "")
          + (f" | D+1 forecast={d1_kwh:.1f} kWh" if d1_kwh is not None else ""))

    # LP-equivalent optimal dispatch
    # Replaces the former percentile-threshold heuristic with a SoC-aware
    # greedy pair-matching algorithm that:
    #   • Uses per-hour load (seasonal) instead of a flat average
    #   • Validates SoC feasibility before committing each charge/discharge hour
    #   • Only schedules pairs where the spread covers the full import cost
    #   • Raises SOC_FLOOR via adaptive floor (Gap 4) and Storm Watch (Gap 3)
    hour_mode = _lp_dispatch(price_by_hour, solar_by_hour, load_by_hour, soc_start,
                             soc_floor=effective_soc_floor)

    # Extract negative export metadata before passing modes to simulator
    neg_export_hours = hour_mode.pop('_neg_export_hours', [])

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

    # Clip segments that start before "now" when building for today in CEST.
    # A segment can't be applied retroactively, so trim the start to the next
    # 15-min boundary and drop any segment whose window has already closed.
    tz_cest = timezone(timedelta(hours=2))
    today_cest = datetime.now(tz_cest).date()
    if for_date == today_cest:
        now_cest  = datetime.now(tz_cest)
        now_min   = now_cest.hour * 60 + now_cest.minute
        clip_min  = ((now_min // 15) + 1) * 15   # round up to next 15-min slot
        clip_h, clip_m = divmod(clip_min, 60)
        clipped = []
        for seg in segments:
            seg_end_min   = seg["end_hour"] * 60 + seg["end_min"]
            seg_start_min = seg["start_hour"] * 60 + seg["start_min"]
            if seg_end_min <= clip_min:
                continue  # entire window already passed
            if seg_start_min < clip_min:
                seg = dict(seg)
                seg["start_hour"] = clip_h
                seg["start_min"]  = clip_m
                seg["start"]      = f"{clip_h:02d}:{clip_m:02d}"
            clipped.append(seg)
        segments = clipped

    # Count Grid First hours (used by both SOC floor and discharge power recommendations)
    grid_first_hours = sum(1 for m in hour_mode.values() if m == 2)

    # Recommend SOC floor — three-layer calculation:
    #   1. Base: from price spread / Grid First hours
    #   2. Gap 4 adaptive: raise if D+1 solar is poor (battery won't recharge during the day)
    #   3. Gap 3 Storm Watch: hard override to 40% for ≥2 consecutive low-solar days
    price_spread = max(prices_sorted) - min(prices_sorted)
    if grid_first_hours > 0 and price_spread >= 0.30:
        rec_soc_floor = 10   # strong arbitrage — worth draining fully
    elif grid_first_hours > 0:
        rec_soc_floor = 15   # mild arbitrage — keep small reserve
    else:
        rec_soc_floor = 20   # no Grid First — keep comfortable reserve
    # Gap 4: adaptive floor based on tomorrow's solar (never lower than base)
    adaptive_floor_pct = int(adaptive_floor * 100)
    rec_soc_floor = max(rec_soc_floor, adaptive_floor_pct)
    # Gap 3: Storm Watch override
    if storm_triggered:
        rec_soc_floor = max(rec_soc_floor, int(STORM_SOC_FLOOR * 100))

    # Recommend discharge power % — drain usable battery within the Grid First window
    # Formula: target_kw = usable_kwh / grid_first_hours; pct = target_kw / normal_kw
    # Capped 50–100 %, rounded to nearest 5 %
    NORMAL_KW = BATT_KWH * 0.6   # nominal inverter/battery max = C_RATE_KW
    USABLE_KWH = BATT_KWH * (SOC_CEIL - SOC_FLOOR)   # 18 kWh
    if grid_first_hours > 0:
        target_kw   = USABLE_KWH / grid_first_hours
        raw_pct     = (target_kw / NORMAL_KW) * 100
        # Round to nearest 5 %, clamp 50–100
        rec_pct     = int(min(100, max(50, round(raw_pct / 5) * 5)))
    else:
        rec_pct = 100   # no Grid First → doesn't matter, keep at max

    # Human-readable reasoning
    cheap_hours  = sorted(h for h, m in hour_mode.items() if m == 1)
    exp_hours    = sorted(h for h, m in hour_mode.items() if m == 2)
    solar_peak   = max(solar_by_hour.items(), key=lambda x: x[1])
    avg_load_kw = sum(load_by_hour.values()) / 24
    adaptive_note = ""
    if d1_kwh is not None and not storm_triggered:
        # Only show adaptive note when Storm Watch isn't already dominating the message
        if adaptive_floor_pct > int(SOC_FLOOR * 100):
            adaptive_note = (f"  🌥 Adaptivt SoC-golv: {adaptive_floor_pct}% "
                             f"(prognos D+1: {d1_kwh:.1f} kWh sol).")

    reasoning = (
        f"LP-optimering {for_date}: priser {min(prices_sorted):.2f}–{max(prices_sorted):.2f} SEK/kWh. "
        f"Laddning (Battery First): {cheap_hours}. "
        f"Urladdning (Grid First): {exp_hours}. "
        f"Solpeak (−{int(avg_haircut*100)}% molnavdrag): {solar_peak[1]:.1f} kW kl {solar_peak[0]:02d}:00. "
        f"Medelbelastning: {avg_load_kw:.2f} kW (säsongsmodell). "
        f"SoC vid start: {soc_start*100:.0f}%."
        + (f"  {storm_note}" if storm_note else "")
        + adaptive_note
    )

    # Battery simulation — daily KPI totals using per-hour load
    sim_kpis = _simulate_battery(solar_by_hour, load_by_hour, hour_mode,
                                 price_by_hour, soc_start)

    return {
        "ok":               True,
        "for_date":         for_date.isoformat(),
        "segments":         segments,
        "discharge_pct":    rec_pct,
        "soc_floor_pct":    rec_soc_floor,
        "reasoning":        reasoning,
        "price_range": {"min": round(min(prices_sorted), 4),
                        "max": round(max(prices_sorted), 4)},
        "solar_peak_kw": round(solar_peak[1], 2),
        "sim_kpis":      sim_kpis,
        "storm_watch": {
            "triggered":    storm_triggered,
            "low_days":     storm_low_days,
            "daily_kwh":    [round(v, 1) for v in daily_solar_kwh],
            "note":         storm_note,
        },
        "adaptive_soc": {
            "d1_kwh":       round(d1_kwh, 1) if d1_kwh is not None else None,
            "floor_pct":    adaptive_floor_pct,
        },
        "neg_export": {
            "hours":        neg_export_hours,
            "count":        len(neg_export_hours),
        },
    }


# ---------------------------------------------------------------------------
# Suggestion: Supabase persistence
# ---------------------------------------------------------------------------

def _save_suggestion(result: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    # Merge discharge_pct + soc_floor_pct into sim_kpis (no schema change needed)
    sim_kpis = dict(result.get("sim_kpis") or {})
    if result.get("discharge_pct")  is not None: sim_kpis["discharge_pct"]  = result["discharge_pct"]
    if result.get("soc_floor_pct")  is not None: sim_kpis["soc_floor_pct"]  = result["soc_floor_pct"]
    row = {
        "for_date":  result["for_date"],
        "segments":  result["segments"],
        "reasoning": result.get("reasoning", ""),
        "sim_kpis":  sim_kpis,
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
            row = rows[0]
            result = {"ok": True, **row}
            # Hoist discharge_pct + soc_floor_pct out of sim_kpis for easy frontend access
            sk = row.get("sim_kpis") or {}
            if isinstance(sk, dict):
                if "discharge_pct" in sk: result["discharge_pct"] = sk["discharge_pct"]
                if "soc_floor_pct" in sk: result["soc_floor_pct"] = sk["soc_floor_pct"]
            return result
        return {"ok": False, "error": "no suggestion saved yet for this date"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Reset to default (disable all segments → inverter falls back to Load First)
# ---------------------------------------------------------------------------

def _reset_to_default() -> list:
    """Write all 9 segments as disabled. Returns _write_many results."""
    segs = [
        {
            "segment_id": i,
            "mode":       0,   # Load First
            "start_hour": 0, "start_min": 0,
            "end_hour":   0, "end_min":   0,
            "enabled":    False,
        }
        for i in range(1, 10)
    ]
    return _write_many(segs)


# ---------------------------------------------------------------------------
# Notify: send reminder e-mail if any TOU segment is currently active
# ---------------------------------------------------------------------------

def _send_email(subject: str, body: str):
    import smtplib
    from email.mime.text import MIMEText
    if not NOTIFY_FROM or not NOTIFY_PASS:
        print("[notify] NOTIFY_EMAIL_FROM / NOTIFY_EMAIL_PASS not set — skipping email")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = NOTIFY_FROM
    msg["To"]      = NOTIFY_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
        s.login(NOTIFY_FROM, NOTIFY_PASS)
        s.sendmail(NOTIFY_FROM, [NOTIFY_TO], msg.as_string())
    print(f"[notify] email sent to {NOTIFY_TO}: {subject}")


def _notify_if_active() -> dict:
    """Read current TOU (cache preferred), send reminder email if any segment is enabled."""
    tou = _read_tou()  # uses cache if fresh — avoids a Growatt call for the nightly check
    if not tou.get("ok"):
        return {"ok": False, "error": "Could not read TOU from inverter"}

    active = [s for s in tou.get("segments", []) if s.get("enabled")]
    if not active:
        return {"ok": True, "email_sent": False, "reason": "no active segments"}

    desc_lines = []
    for s in active:
        desc_lines.append(
            f"  Segment {s['segment_id']}: {s.get('start','?')}–{s.get('stop','?')} "
            f"({s.get('mode_name', s.get('mode', '?'))})"
        )
    body = (
        f"Det finns {len(active)} aktiva TOU-segment på växelriktaren (MID 12KTL3-XH):\n\n"
        + "\n".join(desc_lines)
        + "\n\nKom ihåg att återställa till standardläge (Load First) om det inte längre behövs."
        + "\n\nhttps://electricity-dashboard-phi.vercel.app"
    )
    try:
        _send_email("remember to reset TOU", body)
        return {"ok": True, "email_sent": True, "active_segments": len(active)}
    except Exception as e:
        print(f"[notify] email error: {e}")
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

        if action == "build_suggest":
            # Vercel crons send GET — build TOU suggestion.
            # Optional ?date=YYYY-MM-DD overrides the default (tomorrow UTC).
            try:
                date_param = params.get("date", "")
                if date_param:
                    for_date = date.fromisoformat(date_param)
                else:
                    for_date = date.today() + timedelta(days=1)
                result = _build_suggestion(for_date)
                if result.get("ok"):
                    _save_suggestion(result)
                self._send(result)
            except Exception as e:
                print(f"[growatt_tou build_suggest GET] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if action == "notify_reset":
            # Vercel crons send GET — send reminder email if TOU segments are active
            try:
                self._send(_notify_if_active())
            except Exception as e:
                print(f"[growatt_tou notify_reset GET] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
            return

        # ?action=refresh forces a live read from Growatt, bypassing the cache
        force = (action == "refresh")
        try:
            self._send(_read_tou(force_refresh=force))
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
                tomorrow = date.today() + timedelta(days=1)
                result = _build_suggestion(tomorrow)
                if result.get("ok"):
                    _save_suggestion(result)
                self._send(result)
            except Exception as e:
                print(f"[growatt_tou build_suggest] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
            return

        if action == "notify_reset":
            # Cron entry point — no password required (internal, read-only + email)
            try:
                self._send(_notify_if_active())
            except Exception as e:
                print(f"[growatt_tou notify_reset] {e}")
                self._send({"ok": False, "error": str(e)}, 500)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length)) if length else {}

            # --- Write-burst cooldown ---
            cooldown_msg = _check_write_cooldown()
            if cooldown_msg:
                self._send({"ok": False, "error": "cooldown", "message": cooldown_msg}, 429)
                return

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

            # --- Set SOC floor ---
            if action == "set_soc_floor":
                pct = int(body.get("pct", 10))
                res = _write_soc_floor(pct)
                if res.get("success"):
                    try:
                        cached = _load_tou_cache()
                        segs = cached["segments"] if cached else []
                        dpct = cached.get("discharge_pct") if cached else None
                        _save_tou_cache(segs, discharge_pct=dpct, soc_floor_pct=pct)
                    except Exception:
                        pass
                self._send({"ok": res.get("success", False), **res})
                return

            # --- Set discharge power ---
            if action == "set_discharge_power":
                pct = int(body.get("pct", 100))
                res = _write_discharge_power(pct)
                if res.get("success"):
                    # Update cache with new discharge_pct without changing segments
                    try:
                        cached = _load_tou_cache()
                        segs = cached["segments"] if cached else []
                        _save_tou_cache(segs, discharge_pct=pct)
                    except Exception:
                        pass
                self._send({"ok": res.get("success", False), **res})
                return

            # --- Reset to default ---
            if action == "reset":
                results = _reset_to_default()
                all_ok  = all(r.get("success") for r in results)
                if all_ok:
                    # Brief pause then refresh cache to confirm default state landed
                    try:
                        import time; time.sleep(WRITE_INTERVAL_SECS)
                        _read_tou(force_refresh=True)
                    except Exception:
                        pass
                self._send({"ok": all_ok, "results": results})
                return

            # --- Write ---
            # Multiple segments
            if "segments" in body:
                results = _write_many(body["segments"])
                # Brief pause then refresh cache after bulk write
                try:
                    import time; time.sleep(WRITE_INTERVAL_SECS)
                    _read_tou(force_refresh=True)
                except Exception:
                    pass
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
            if res.get("success"):
                # Brief pause then refresh cache to confirm write landed
                try:
                    import time; time.sleep(WRITE_INTERVAL_SECS)
                    _read_tou(force_refresh=True)
                except Exception:
                    pass
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
