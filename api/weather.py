import json
import urllib.request
from http.server import BaseHTTPRequestHandler

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=59.28&longitude=18.00"
    "&hourly=temperature_2m,cloudcover,windspeed_10m,shortwave_radiation"
    "&daily=sunrise,sunset"
    "&timezone=Europe%2FStockholm&forecast_days=2"
)

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with urllib.request.urlopen(WEATHER_URL, timeout=8) as r:
                data = json.loads(r.read())
            self._send(data)
        except Exception as e:
            self._send({"error": str(e)}, 500)

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
