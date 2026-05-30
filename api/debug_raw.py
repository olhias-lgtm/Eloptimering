"""Temporary debug endpoint — returns raw Growatt chartData arrays before normalization."""
import json
from datetime import date
from http.server import BaseHTTPRequestHandler
from _growatt import get_session, GROWATT_API

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            s = get_session()
            s.ensure_ready()
            resp = s._s.post(
                GROWATT_API + "/newTlxApi.do",
                params={"op": "getEnergyProdAndCons_KW"},
                data={
                    "date":     date.today().isoformat(),
                    "plantId":  s.plant_id,
                    "language": "1",
                    "id":       s.mix_serial,
                    "type":     "1",
                },
                timeout=15,
            )
            data = resp.json()
            obj = data.get("obj", {})
            cd  = obj.get("chartData", {})
            # Show first few and last few values from each array
            # Return full arrays for all keys so we can compute totals
            full = {}
            for key in cd:
                arr = cd.get(key) or []
                if isinstance(arr, list):
                    full[key] = arr
            self._send({"n_keys": len(cd), "all_keys": list(cd.keys()), "arrays": full})
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
