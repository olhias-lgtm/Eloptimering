import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from _growatt import get_session

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

# Throttle inserts: don't write more than once every 3 minutes
_last_insert_ts = 0.0
_MIN_INSERT_GAP = 180  # seconds


def _sb_insert(data: dict):
    """Persist a live reading to energy_readings. Fire-and-forget."""
    global _last_insert_ts
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    now = time.monotonic()
    if now - _last_insert_ts < _MIN_INSERT_GAP:
        return
    try:
        body = json.dumps({
            "ppv_kw":           data.get("ppv_kw"),
            "ppv1_kw":          data.get("ppv1_kw"),
            "ppv2_kw":          data.get("ppv2_kw"),
            "pac_kw":           data.get("pac_kw"),
            "load_kw":          data.get("load_kw"),
            "export_kw":        data.get("export_kw"),
            "import_kw":        data.get("import_kw"),
            "charge_kw":        data.get("charge_kw"),
            "discharge_kw":     data.get("discharge_kw"),
            "epv_today":        data.get("epv_today"),
            "eac_today":        data.get("eac_today"),
            "echarge_today":    data.get("echarge_today"),
            "edischarge_today": data.get("edischarge_today"),
            "eload_today":      data.get("eload_today"),
            "export_today":     data.get("export_today"),
            "import_today":     data.get("import_today"),
        }).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/energy_readings",
            data=body,
            method="POST",
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
        )
        urllib.request.urlopen(req, timeout=5).read()
        _last_insert_ts = now
        print("[live] persisted reading to Supabase")
    except Exception as e:
        print(f"[live] supabase insert error: {e}")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            s    = get_session()
            data = s.get_live()
            _sb_insert(data)
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
