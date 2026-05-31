"""
Data collection endpoint — called by cron-job.org every 5 minutes.
Fetches live Growatt data and persists it to Supabase energy_readings.
Always returns 200 so the cron service doesn't treat throttling as a failure.
"""
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler
from _growatt import get_session

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def _sb_insert(data: dict):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase env vars not set")
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
    urllib.request.urlopen(req, timeout=8).read()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            s    = get_session()
            data = s.get_live()
            _sb_insert(data)
            print(f"[collect] OK ppv={data.get('ppv_kw')} export={data.get('export_kw')}")
            self._send({"ok": True, "ppv_kw": data.get("ppv_kw")})
        except Exception as e:
            print(f"[collect] error: {e}")
            # Still return 200 so cron-job.org doesn't flag it as failing
            self._send({"ok": False, "error": str(e)})

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
