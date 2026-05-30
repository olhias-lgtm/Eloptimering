import json
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler

ELPRISET_BASE = "https://www.elprisetjustnu.se/api/v1/prices"

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        area   = params.get("area", "SE3")
        ds     = params.get("date", date.today().isoformat())

        def fetch(d):
            y, mo, day = d.split("-")
            url = f"{ELPRISET_BASE}/{y}/{mo}-{day}_{area}.json"
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    return json.loads(r.read())
            except Exception:
                return []

        tomorrow = (date.fromisoformat(ds) + timedelta(days=1)).isoformat()
        today_prices    = fetch(ds)
        tomorrow_prices = fetch(tomorrow)

        self._send({"today": today_prices, "tomorrow": tomorrow_prices})

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
