import json
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler
from _growatt import get_session

_CACHE: dict = {}
_TTL  = 600  # 10 minutes

import time

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        target = params.get("date", date.today().isoformat())

        cached = _CACHE.get(target)
        if cached and (time.monotonic() - cached["ts"]) < _TTL:
            self._send(cached["data"])
            return

        try:
            s   = get_session()
            raw = s.get_energy(target)
            _CACHE[target] = {"ts": time.monotonic(), "data": raw}
            self._send(raw)
        except Exception as e:
            print(f"[energy] {e}")
            if cached:
                self._send(cached["data"])
            else:
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
