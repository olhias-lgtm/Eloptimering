import json
from http.server import BaseHTTPRequestHandler
from api._growatt import get_session

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            s    = get_session()
            data = s.get_live()
            self._send(data)
        except Exception as e:
            print(f"[live] {e}")
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
