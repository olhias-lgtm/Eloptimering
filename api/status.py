import json
import os
from http.server import BaseHTTPRequestHandler
from api._growatt import get_session

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        s = get_session()
        body = json.dumps({
            "logged_in":  s.logged_in,
            "plant_id":   s.plant_id,
            "mix_serial": s.mix_serial,
            "username":   os.environ.get("GROWATT_USER", ""),
            "env_ok":     bool(os.environ.get("GROWATT_USER") and os.environ.get("GROWATT_PASS")),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
