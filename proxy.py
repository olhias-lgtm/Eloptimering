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
import http.client
import http.cookiejar
import http.server
import json
import os
import pathlib
import urllib.parse
import urllib.request
import threading
from datetime import date, timedelta

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

class GrowattSession:
    def __init__(self):
        self.lock       = threading.Lock()
        self.plant_id   = None
        self.mix_serial = None
        self.user_id    = None
        self.logged_in  = False
        # Shared cookie jar so session cookie persists across requests
        self._jar     = http.cookiejar.CookieJar()
        self._opener  = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    def _post(self, path: str, params: dict, base: str = None) -> dict:
        url  = (base or GROWATT_BASE) + path
        body = urllib.parse.urlencode(params).encode()
        req  = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "ElstromDashboard/1.0"},
        )
        with self._opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)

    def _post_qs(self, path: str, params: dict, base: str = None) -> dict:
        """POST with params as query string — mirrors requests.post(url, params=...)."""
        url = (base or GROWATT_API) + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, data=b"",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent":   "ElstromDashboard/1.0"},
        )
        with self._opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)

    def login(self):
        username = os.environ.get("GROWATT_USERNAME", "")
        password = os.environ.get("GROWATT_PASSWORD", "")
        if not username or not password:
            raise ValueError("GROWATT_USERNAME / GROWATT_PASSWORD not set in .env")

        hashed = _growatt_hash(password)
        print(f"[Growatt] Logging in as {username} (hash={hashed[:8]}…)")

        # Login on openapi host so session cookie works for API calls there
        resp = self._post("/newTwoLoginAPI.do", {
            "userName": username,
            "password": hashed,
        }, base=GROWATT_API)
        print(f"[Growatt] Login response: {json.dumps(resp)[:400]}")

        back = resp.get("back", {})
        if not back.get("success"):
            raise RuntimeError(f"Login failed: {resp}")

        user = back.get("user") or {}
        self.user_id  = str(user.get("id") or user.get("userId") or "")
        self.logged_in = True

        # Plant list is embedded in the login response — grab it now
        plant_list = back.get("data") or []
        if plant_list:
            self.plant_id = str(plant_list[0].get("plantId") or "")
            print(f"[Growatt] Plant from login: {self.plant_id} ({plant_list[0].get('plantName','')})")

        print(f"[Growatt] Logged in. User ID: {self.user_id}")
        return resp

    def discover(self):
        if not self.logged_in:
            self.login()

        # Try device list endpoint — use the known serial as fallback
        for op in ("getDevicesByPlantList", "getMixList", "getStorageList"):
            try:
                dev_resp = self._post("/newTwoPlantAPI.do", {
                    "op":      op,
                    "plantId": self.plant_id,
                    "currPage": "1",
                }, base=GROWATT_API)
                print(f"[Growatt] {op} resp: {json.dumps(dev_resp)[:400]}")
                dev_list = (
                    dev_resp.get("back", {}).get("data")
                    or dev_resp.get("obj", {}).get("datas")
                    or dev_resp.get("data")
                    or []
                )
                for dev in dev_list:
                    dtype = (dev.get("deviceType") or dev.get("type") or "").lower()
                    sn    = dev.get("deviceSn") or dev.get("sn") or dev.get("mixSn") or ""
                    print(f"[Growatt]   {op} device type={dtype} sn={sn}")
                    if dtype in ("mix", "storage") or sn == "KJN6EXV00L":
                        self.mix_serial = sn
                        break
                if self.mix_serial:
                    break
            except Exception as e:
                print(f"[Growatt] {op} failed: {e}")

        # Hard fallback to known serial from ShinePhone
        if not self.mix_serial:
            self.mix_serial = "KJN6EXV00L"
            print("[Growatt] Using known Mix serial from ShinePhone as fallback")

        print(f"[Growatt] Discovery — plant: {self.plant_id}, mix: {self.mix_serial}")

    def ensure_ready(self):
        with self.lock:
            if not self.logged_in:
                self.login()
            if not self.plant_id:
                self.discover()

    def get_energy(self, target_date: str) -> dict:
        self.ensure_ready()
        # Mirror growattServer.mix_detail: GET with query params on openapi host
        resp = self._post_qs("/newMixApi.do", {
            "op":      "getEnergyProdAndCons_KW",
            "plantId": self.plant_id,
            "mixId":   self.mix_serial,
            "type":    "1",        # 1 = day
            "date":    target_date,
        })
        return resp


SESSION = GrowattSession()

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

        # Energy data (falls back to mock if not logged in)
        if path == "/api/energy":
            target = params.get("date", date.today().isoformat())
            if not SESSION.logged_in:
                data = generate_mock_energy()
                data["mock"] = True
                self.send_json(data)
                return
            try:
                raw = SESSION.get_energy(target)
                self.send_json(raw)
            except Exception as e:
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
