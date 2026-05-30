#!/usr/bin/env python3
"""
proxy.py — Electricity Dashboard Proxy
Hand-rolled Growatt HTTP client (no third-party growattServer library required).
Works on Python 3.9+.

Usage:
  1. Create .env in the same folder:
       GROWATT_USERNAME=your@email.com
       GROWATT_PASSWORD=yourpassword
  2. python3 proxy.py
  3. Open http://localhost:8080 in your browser
"""

import hashlib
import http.cookiejar
import http.server
import json
import os
import pathlib
import time
import urllib.parse
import urllib.request
import threading
from datetime import date, timedelta
import requests as _requests

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

PORT = 8080
GROWATT_BASE = "https://server.growatt.com"
GROWATT_API  = "https://openapi.growatt.com"

# ---------------------------------------------------------------------------
# Growatt MD5 password hashing
# Growatt's quirk: after MD5, replace specific byte pairs with 'c' + pair
# ---------------------------------------------------------------------------

def _growatt_hash(password: str) -> str:
    # At every even index, replace a '0' character with 'c'
    h = hashlib.md5(password.encode("utf-8")).hexdigest()
    h = list(h)
    for i in range(0, len(h), 2):
        if h[i] == "0":
            h[i] = "c"
    return "".join(h)


# ---------------------------------------------------------------------------
# Growatt session — hand-rolled urllib client
# ---------------------------------------------------------------------------

_COOKIE_FILE = pathlib.Path(__file__).parent / ".growatt_cookies"

class GrowattSession:
    def __init__(self):
        self.lock       = threading.Lock()
        self.plant_id   = None
        self.mix_serial = None
        self.user_id    = None
        self.logged_in  = False
        jar = http.cookiejar.LWPCookieJar(str(_COOKIE_FILE))
        if _COOKIE_FILE.exists():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
                print(f"[Growatt] Loaded cookies from {_COOKIE_FILE}")
            except Exception as e:
                print(f"[Growatt] Cookie load failed (will re-login): {e}")
        self._jar = jar
        self._s = _requests.Session()
        self._s.cookies = jar
        self._s.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        })

    def login(self):
        username = os.environ.get("GROWATT_USERNAME", "")
        password = os.environ.get("GROWATT_PASSWORD", "")
        if not username or not password:
            raise ValueError("GROWATT_USERNAME / GROWATT_PASSWORD not set in .env")

        hashed = _growatt_hash(password)
        print(f"[Growatt] Logging in as {username} (hash={hashed[:8]}…)")

        resp = self._s.post(
            GROWATT_API + "/newTwoLoginAPI.do",
            data={"userName": username, "password": hashed},
            timeout=15,
        )
        data = resp.json()
        print(f"[Growatt] Login response: {json.dumps(data)[:400]}")

        back = data.get("back", {})
        if not back.get("success"):
            raise RuntimeError(f"Login failed: {data}")

        user = back.get("user") or {}
        self.user_id   = str(user.get("id") or user.get("userId") or "")
        self.logged_in = True

        plant_list = back.get("data") or []
        if plant_list:
            self.plant_id = str(plant_list[0].get("plantId") or "")
            print(f"[Growatt] Plant from login: {self.plant_id} ({plant_list[0].get('plantName','')})")

        try:
            self._jar.save(ignore_discard=True, ignore_expires=True)
            print(f"[Growatt] Session cookies saved to {_COOKIE_FILE}")
        except Exception as e:
            print(f"[Growatt] Cookie save failed: {e}")

        print(f"[Growatt] Logged in. User ID: {self.user_id}")
        return data

    def discover(self):
        if not self.logged_in:
            self.login()

        for op in ("getDevicesByPlantList", "getTLXList", "getMixList"):
            try:
                dev_resp = self._s.post(
                    GROWATT_API + "/newTwoPlantAPI.do",
                    data={"op": op, "plantId": self.plant_id, "currPage": "1"},
                    timeout=15,
                ).json()
                print(f"[Growatt] {op} resp: {json.dumps(dev_resp)[:400]}")
                dev_list = (
                    dev_resp.get("back", {}).get("data")
                    or dev_resp.get("obj", {}).get("datas")
                    or dev_resp.get("data")
                    or []
                )
                for dev in dev_list:
                    sn = dev.get("deviceSn") or dev.get("sn") or dev.get("tlxSn") or dev.get("mixSn") or ""
                    if sn:
                        self.mix_serial = sn
                        print(f"[Growatt]   {op} → serial={sn}")
                        break
                if self.mix_serial:
                    break
            except Exception as e:
                print(f"[Growatt] {op} failed: {e}")

        if not self.mix_serial:
            self.mix_serial = "KJN6EXV00L"
            print("[Growatt] Using known TLX serial as fallback")

        print(f"[Growatt] Discovery — plant: {self.plant_id}, serial: {self.mix_serial}")

    def ensure_ready(self):
        with self.lock:
            if not self.logged_in:
                self.login()
            if not self.plant_id or not self.mix_serial:
                self.discover()

    def _fetch_energy(self, target_date: str) -> dict:
        resp = self._s.post(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getEnergyProdAndCons_KW"},
            data={
                "date":     target_date,
                "plantId":  self.plant_id,
                "language": "1",
                "id":       self.mix_serial,
                "type":     "1",
            },
            timeout=15,
        )
        data = resp.json()
        return self._normalize_tlx(data)

    def _normalize_tlx(self, data: dict) -> dict:
        """Convert TLX array-based chartData to the time-keyed dict the dashboard expects."""
        obj = data.get("obj", {})
        cd  = obj.get("chartData", {})

        # Field mapping confirmed against ShinePhone daily totals (factor 25 = kWh/slot):
        #   sysOut   / 25 = 60.6 kWh  → solar production
        #   echarge  / 25 = 15.4 kWh  → battery charged (~15.5)
        #   epv3     / 25 = 15.8 kWh  → battery discharged (~14.9)
        #   acCharge / 25 = 30.7 kWh  → closest to load (25.1) + some measurement offset
        #   pacToGrid/ 25 = 32.7 kWh  → closest to export (34.7)
        solar    = cd.get("sysOut")   or []   # solar PV production
        load     = cd.get("acCharge") or []   # load consumption
        pac_grid = cd.get("pacToGrid") or []  # export to grid
        pdis     = cd.get("epv3")     or []   # battery discharge
        pac_user = []                          # grid import ≈ 0, no reliable field

        n = max(len(solar), len(load), len(pac_user), len(pac_grid), len(pdis))
        if n == 0:
            return data

        # TLX returns up to 48 slots/day at 30-min resolution (partial day = fewer slots)
        # Growatt server runs UTC+8; Sweden is CEST (UTC+2) → slots are 6 h ahead of local time
        minutes = 5 if n > 48 else 30
        # Growatt slots appear to be 2 h ahead of CEST (solar at 04:20 CEST appears at slot "06:30").
        # The Growatt day starts at 22:00 CEST the previous evening — exclude those slots.
        # ppv is clamped to 0 outside daylight hours (20:00–04:00 CEST) because sysOut
        # includes battery discharge which must not be shown as solar production.
        CEST_OFFSET_MIN = -6 * 60
        time_cd = {}

        def _v(arr, idx):
            try: return round(float(arr[idx]) / 12.5, 2)
            except: return 0.0

        for i in range(n):
            total_min = (i * minutes + CEST_OFFSET_MIN) % (24 * 60)
            if total_min >= 18 * 60:   # previous CEST evening — skip
                continue
            label = f"{total_min // 60:02d}:{total_min % 60:02d}"
            # Zero out solar outside the Swedish summer daylight window.
            # At night sysOut = pure battery discharge; no field isolates it precisely.
            is_daylight = 4 * 60 <= total_min < 22 * 60
            ppv = max(0.0, round((_v(solar, i) - _v(pdis, i)), 2)) if is_daylight else 0.0
            time_cd[label] = {
                "ppv":       ppv,
                "sysOut":    _v(load, i),
                "pacToUser": _v(pac_user, i),
                "pacToGrid": _v(pac_grid, i),
                "pdischarge":_v(pdis, i),
            }

        obj["chartData"] = time_cd
        data["obj"] = obj
        return data

    def _session_expired(self, data: dict) -> bool:
        # Growatt returns result={"msg":"FAILED","success":false} or redirects to login
        # when the session cookie is no longer valid
        if not isinstance(data, dict):
            return True
        back = data.get("back") or data
        success = back.get("success")
        if success is False:
            return True
        msg = str(back.get("msg", "")).lower()
        return "login" in msg or "session" in msg or "noauth" in msg

    def get_energy(self, target_date: str, _retries: int = 2) -> dict:
        self.ensure_ready()
        for attempt in range(1, _retries + 1):
            data = self._fetch_energy(target_date)
            if not self._session_expired(data):
                return data
            if attempt < _retries:
                print(f"[Growatt] Session expired (attempt {attempt}/{_retries}) — re-logging in")
                if _COOKIE_FILE.exists():
                    _COOKIE_FILE.unlink()
                    print(f"[Growatt] Cleared stale cookie file")
                self._s.cookies.clear()
                with self.lock:
                    self.logged_in = False
                    self.plant_id  = None
            else:
                print(f"[Growatt] Session expired — max retries ({_retries}) reached")
        return data


SESSION = GrowattSession()

# Energy response cache — keyed by date string, refreshed at most every 5 min
_ENERGY_CACHE: dict[str, dict] = {}   # date -> {"ts": float, "data": dict}
_ENERGY_TTL = 600  # seconds

# ---------------------------------------------------------------------------
# Supabase REST helper
# ---------------------------------------------------------------------------

def _supabase_get(path: str, params: dict = None) -> list:
    base = os.environ.get("SUPABASE_URL", "")
    key  = os.environ.get("SUPABASE_ANON_KEY", "")
    if not base or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY not set")
    qs  = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = base + "/rest/v1" + path + qs
    req = urllib.request.Request(url, headers={
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Accept":        "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def fetch_history(days: int = 30, area: str = "SE3") -> list:
    """Return daily aggregates from Supabase for the last `days` days."""
    # Try daily_summary first (fast pre-computed rows)
    try:
        rows = _supabase_get("/daily_summary", {
            "select":  "day,solar_kwh,load_kwh,import_kwh,export_kwh,charge_kwh,discharge_kwh,import_cost_kr,export_earn_kr,net_kr,is_mock",
            "area":    f"eq.{area}",
            "order":   "day.desc",
            "limit":   str(days),
        })
        if rows:
            return rows
    except Exception as e:
        print(f"[History] daily_summary query failed: {e}")

    # Fallback: aggregate energy_intervals by day
    try:
        rows = _supabase_get("/energy_intervals", {
            "select":  "ts,solar_kw,load_kw,import_kw,export_kw,charge_kw,discharge_kw,is_mock",
            "order":   "ts.asc",
            "limit":   str(days * 288 + 10),
        })
        # Group by local date (UTC+2 for SE)
        from collections import defaultdict
        by_day = defaultdict(list)
        for r in rows:
            # ts is ISO string; take the date part after shifting to local
            dt = r["ts"][:10]  # good enough for grouping
            by_day[dt].append(r)

        result = []
        kwh5 = 5 / 60
        for day in sorted(by_day.keys(), reverse=True)[:days]:
            intervals = by_day[day]
            def kwh(field):
                return round(sum(i[field] for i in intervals) * kwh5, 2)
            result.append({
                "day":           day,
                "solar_kwh":     kwh("solar_kw"),
                "load_kwh":      kwh("load_kw"),
                "import_kwh":    kwh("import_kw"),
                "export_kwh":    kwh("export_kw"),
                "charge_kwh":    kwh("charge_kw"),
                "discharge_kwh": kwh("discharge_kw"),
                "is_mock":       any(i["is_mock"] for i in intervals),
            })
        return result
    except Exception as e:
        print(f"[History] interval aggregation failed: {e}")
        return []

# ---------------------------------------------------------------------------
# Weather fetcher — Open-Meteo, Älvsjö 59.28°N 18.00°E
# ---------------------------------------------------------------------------

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=59.28&longitude=18.00"
    "&hourly=temperature_2m,cloudcover,windspeed_10m,shortwave_radiation"
    "&daily=sunrise,sunset"
    "&timezone=Europe%2FStockholm"
    "&forecast_days=2"
)

def fetch_weather() -> dict:
    try:
        req = urllib.request.Request(WEATHER_URL, headers={"User-Agent": "ElstromDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[Weather] Failed: {e}")
        return {}

# ---------------------------------------------------------------------------
# Mock energy data — modelled from ShinePhone screenshot 2026-05-29
# ---------------------------------------------------------------------------

import math

def _bell(t, peak, width, height):
    return height * math.exp(-((t - peak) ** 2) / (2 * width ** 2))

def generate_mock_energy() -> dict:
    chart = {}
    for slot in range(288):  # 5-min intervals
        h = slot * 5 // 60
        m = (slot * 5) % 60
        t = slot * 5 / 60  # decimal hours
        label = f"{h:02d}:{m:02d}"

        # Solar: main bell 06:30-19:00, peak at 10:00, secondary bump at 17:00
        solar = max(0,
            _bell(t, 10.0, 1.6, 9.8) +
            _bell(t, 17.2, 0.4, 2.8) +
            (0.3 if 6.5 < t < 19.0 else 0)
        )
        # Add small random-ish variation using sin
        if solar > 0.1:
            solar *= (0.92 + 0.08 * math.sin(slot * 0.7))
        solar = round(max(0, solar if t > 6.3 and t < 19.2 else 0), 2)

        # Load: flat ~0.85 kW with a morning spike and evening spike
        load = 0.85 + _bell(t, 7.5, 0.3, 0.9) + _bell(t, 19.5, 0.5, 1.2)
        load = round(max(0.4, load), 2)

        # Charging: strong 09:00-11:30, tapers off
        charge = round(max(0, _bell(t, 10.0, 0.8, 8.8)), 2)

        # Discharge: evening 17:00-21:00 and overnight
        discharge = round(max(0,
            _bell(t, 18.0, 0.8, 3.5) +
            (_bell(t, 1.5, 2.5, 1.2) if t < 6.0 else 0)
        ), 2)

        # Export: solar surplus after charging and load
        surplus = solar - load - charge + discharge
        export  = round(max(0, surplus), 2)

        # Import: deficit at night / when surplus is negative
        imp = round(max(0, load - solar - discharge + charge), 2)
        # But zero import while solar is strong
        if solar > 1.0:
            imp = 0.0

        chart[label] = {
            "ppv":         solar,
            "sysOut":      load,
            "pacToUser":   imp,
            "pacToGrid":   export,
            "pdischarge":  discharge,
        }

    return {"obj": {"chartData": chart}, "mock": True}

# ---------------------------------------------------------------------------
# Price fetcher
# ---------------------------------------------------------------------------

def fetch_prices(area: str, target_date: date) -> list:
    url = (
        f"https://www.elprisetjustnu.se/api/v1/prices/"
        f"{target_date.strftime('%Y')}/"
        f"{target_date.strftime('%m')}-{target_date.strftime('%d')}_{area}.json"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ElectricityDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[Prices] Failed: {e}")
        return []

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[HTTP] {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=500):
        self.send_json({"error": msg}, status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        # Serve index.html
        if path in ("/", "/index.html"):
            html_path = pathlib.Path(__file__).parent / "index.html"
            if html_path.exists():
                body = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error_json("index.html not found", 404)
            return

        # Status
        if path == "/api/status":
            self.send_json({
                "logged_in":  SESSION.logged_in,
                "plant_id":   SESSION.plant_id,
                "mix_serial": SESSION.mix_serial,
                "username":   os.environ.get("GROWATT_USERNAME", "(not set)"),
                "env_ok":     bool(os.environ.get("GROWATT_USERNAME")),
            })
            return

        # Discover
        if path == "/api/discover":
            try:
                SESSION.discover()
                self.send_json({
                    "plant_id":   SESSION.plant_id,
                    "mix_serial": SESSION.mix_serial,
                })
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Mock energy data
        if path == "/api/energy/mock":
            self.send_json(generate_mock_energy())
            return

        # Raw TLX chartData arrays for field analysis (no normalization, no cache)
        if path == "/api/energy/raw":
            target = params.get("date", date.today().isoformat())
            dtype  = params.get("type", "1")   # 0=hour, 1=day (default matches main endpoint)
            try:
                SESSION.ensure_ready()
                resp = SESSION._s.post(
                    GROWATT_API + "/newTlxApi.do",
                    params={"op": "getEnergyProdAndCons_KW"},
                    data={
                        "date":     target,
                        "plantId":  SESSION.plant_id,
                        "language": "1",
                        "id":       SESSION.mix_serial,
                        "type":     dtype,
                    },
                    timeout=15,
                )
                print(f"[Raw] status={resp.status_code} body_start={resp.text[:200]!r}")
                raw = resp.json()
                if raw is None:
                    self.send_error_json("Growatt returned null")
                    return
                cd  = raw.get("obj", {}).get("chartData", {})
                # Return field names, lengths, and slot-by-slot table
                fields = {k: v for k, v in cd.items() if isinstance(v, list)}
                n = max((len(v) for v in fields.values()), default=0)
                table = []
                for i in range(n):
                    row = {"slot": i}
                    for k, arr in fields.items():
                        try: row[k] = arr[i]
                        except: row[k] = None
                    table.append(row)
                self.send_json({"fields": list(fields.keys()), "n": n, "table": table})
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Energy data (falls back to mock if not logged in)
        if path == "/api/energy":
            target = params.get("date", date.today().isoformat())
            if not SESSION.logged_in:
                data = generate_mock_energy()
                data["mock"] = True
                self.send_json(data)
                return
            cached = _ENERGY_CACHE.get(target)
            if cached and (time.monotonic() - cached["ts"]) < _ENERGY_TTL:
                self.send_json(cached["data"])
                return
            try:
                raw = SESSION.get_energy(target)
                _ENERGY_CACHE[target] = {"ts": time.monotonic(), "data": raw}
                self.send_json(raw)
            except Exception as e:
                print(f"[Energy] fetch failed: {e}")
                if cached:
                    self.send_json(cached["data"])  # serve stale, no error
                else:
                    self.send_error_json(str(e))
            return

        # Real-time live reading from getTlxDetailData + getSystemStatus_KW
        if path == "/api/live":
            try:
                SESSION.ensure_ready()
                # getTlxDetailData — contains ppv (actual PV power), pac, pself,
                # pacToGridTotal, pacToUserTotal, pacToLocalLoad, edischargeToday, echargeToday
                detail_resp = SESSION._s.get(
                    GROWATT_API + "/newTlxApi.do",
                    params={"op": "getTlxDetailData", "id": SESSION.mix_serial},
                    timeout=10,
                )
                detail = detail_resp.json().get("data", {}) or {}

                # getSystemStatus_KW — contains pdisCharge (kW discharge) and chargePower (kW charge)
                status_resp = SESSION._s.post(
                    GROWATT_API + "/newTlxApi.do",
                    params={"op": "getSystemStatus_KW"},
                    data={"plantId": SESSION.plant_id, "id": SESSION.mix_serial},
                    timeout=10,
                )
                status = status_resp.json().get("obj", {}) or {}

                # getEnergyOverview — provides epvToday (solar PV energy today)
                overview_resp = SESSION._s.post(
                    GROWATT_API + "/newTlxApi.do",
                    params={"op": "getEnergyOverview"},
                    data={"plantId": SESSION.plant_id, "id": SESSION.mix_serial},
                    timeout=10,
                )
                overview = overview_resp.json().get("obj", {}) or {}

                def _w(d, key):
                    """Field is in Watts from getTlxDetailData — convert to kW."""
                    try: return round(float(d.get(key, 0) or 0) / 1000, 3)
                    except: return 0.0

                def _kwh(d, key):
                    """Field is already in kWh."""
                    try: return round(float(d.get(key, 0) or 0), 2)
                    except: return 0.0

                live = {
                    # Power (kW) — detail fields are in Watts
                    "ppv_kw":        _w(detail, "ppv"),           # actual solar PV
                    "ppv1_kw":       _w(detail, "ppv1"),
                    "ppv2_kw":       _w(detail, "ppv2"),
                    "pac_kw":        _w(detail, "pac"),            # total AC output
                    "load_kw":       _w(detail, "pacToLocalLoad"),
                    "export_kw":     _w(detail, "pacToGridTotal"),
                    "import_kw":     _w(detail, "pacToUserTotal"),
                    "self_kw":       _w(detail, "pself"),
                    # system_status fields are already in kW
                    "discharge_kw":  _kwh(status, "pdisCharge"),
                    "charge_kw":     _kwh(status, "chargePower"),
                    # Energy today (kWh)
                    "epv_today":     _kwh(overview, "epvToday"),   # solar PV total today
                    "eac_today":     _kwh(detail, "eacToday"),     # AC output today
                    "echarge_today": _kwh(detail, "echargeToday"),
                    "edischarge_today": _kwh(detail, "edischargeToday"),
                    "eload_today":   _kwh(detail, "elocalLoadToday"),
                    "export_today":  _kwh(detail, "etoGridToday"),
                    "import_today":  _kwh(detail, "etoUserToday"),
                }
                print(f"[Live] ppv={live['ppv_kw']}kW  pac={live['pac_kw']}kW  "
                      f"discharge={live['discharge_kw']}kW  load={live['load_kw']}kW")
                self.send_json(live)
            except Exception as e:
                print(f"[Live] fetch failed: {e}")
                self.send_error_json(str(e))
            return

        # Prices
        if path == "/api/prices":
            area   = params.get("area", "SE3")
            target = params.get("date", date.today().isoformat())
            try:
                d        = date.fromisoformat(target)
                today    = fetch_prices(area, d)
                tomorrow = fetch_prices(area, d + timedelta(days=1))
                self.send_json({"today": today, "tomorrow": tomorrow})
            except Exception as e:
                self.send_error_json(str(e))
            return

        # History (multi-day from Supabase)
        if path == "/api/history":
            area = params.get("area", "SE3")
            days = int(params.get("days", "30"))
            try:
                self.send_json(fetch_history(days, area))
            except Exception as e:
                self.send_error_json(str(e))
            return

        # Weather
        if path == "/api/weather":
            data = fetch_weather()
            if data:
                self.send_json(data)
            else:
                self.send_error_json("Weather fetch failed")
            return

        self.send_error_json(f"Unknown path: {path}", 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Auto-login disabled — call /api/discover manually when ready
    pass

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print(f"✅  Electricity Dashboard running at http://localhost:{PORT}")
    print(f"   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
