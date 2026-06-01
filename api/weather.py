"""Weather proxy with server-side cache.

Open-Meteo's Nordic model (most relevant for Stockholm/SE3) publishes new
runs every hour. Caching for 55 minutes means we fetch at most once per
model run regardless of how many browser tabs or Lambda invocations call us.
"""
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=59.28&longitude=18.00"
    "&hourly=temperature_2m,cloudcover,windspeed_10m,shortwave_radiation"
    "&daily=sunrise,sunset"
    "&timezone=Europe%2FStockholm&forecast_days=2"
)

# 55-minute in-process cache — stays within one model-run window.
# Survives warm Lambda reuse; cold starts re-fetch (acceptable, still ≤1/h).
_CACHE_TTL = 55 * 60  # seconds
_cache: dict = {"data": None, "ts": 0.0}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        age = time.monotonic() - _cache["ts"]
        if _cache["data"] and age < _CACHE_TTL:
            self._send(_cache["data"])
            return

        try:
            req = urllib.request.Request(
                WEATHER_URL,
                headers={"User-Agent": "electricity-dashboard/weather"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            _cache["data"] = data
            _cache["ts"]   = time.monotonic()
            self._send(data)
        except Exception as e:
            # Return stale cache on error rather than a broken response
            if _cache["data"]:
                self._send(_cache["data"])
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
