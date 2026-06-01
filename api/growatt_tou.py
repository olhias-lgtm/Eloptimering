"""
growatt_tou
  GET  — read current TOU settings via newTlxApi.do?op=getTlxSetData
  POST — write one or more time segments via newTcpsetAPI.do?op=tlxSet

Key findings from reverse-engineering (PyPi_GrowattServer 1.6.0 / HA PR #133319):
  • Read  uses newTlxApi.do?op=getTlxSetData   with body  serialNum=<sn>
  • Write uses newTcpsetAPI.do?op=tlxSet        with body  serialNum=<sn>
    type=time_segment{1-9}, param1..param6, param7..param19="" (all required)
  • Auth  is the standard newTwoLoginAPI.do session (same as all other ops)
  • The "installer password" is a ShinePhone UI concept, not an API parameter

POST body (JSON) — single segment:
  {
    "segment_id": 1,        // 1–9
    "mode":       1,        // 0=Load First  1=Battery First  2=Grid First
    "start_hour": 0,
    "start_min":  0,
    "end_hour":   8,
    "end_min":    0,
    "enabled":    true
  }

POST body (JSON) — multiple segments at once:
  { "segments": [ { ...same fields... }, ... ] }
"""
import json
from http.server import BaseHTTPRequestHandler

from _growatt import get_session

SERIAL = "KJN6EXV00L"
BASE   = "https://openapi.growatt.com"

MODE_NAMES = {0: "Load First", 1: "Battery First", 2: "Grid First"}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _read_tou() -> dict:
    """Return raw getTlxSetData response + normalised segment list if parseable."""
    sess = get_session()
    sess.ensure_ready()
    r = sess._s.post(
        BASE + "/newTlxApi.do",
        params={"op": "getTlxSetData"},
        data={"serialNum": SERIAL},
        timeout=10,
    )
    if not r.text.strip():
        return {"ok": False, "note": "empty response — device may not support getTlxSetData via this account"}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "raw": r.text[:500]}

    # Normalise into a usable segment list if the obj/tlxSetBean is present
    obj  = data.get("obj") or {}
    bean = obj.get("tlxSetBean") or obj  # location varies by firmware
    segments = []
    for i in range(1, 10):
        # Field names seen in community data: forcedTimeStart{N}, time{N}Mode, etc.
        # Also try compact variants used by some firmware versions.
        start = (bean.get(f"forcedTimeStart{i}")
                 or bean.get(f"startTime{i}")
                 or bean.get(f"time{i}Start"))
        stop  = (bean.get(f"forcedTimeStop{i}")
                 or bean.get(f"endTime{i}")
                 or bean.get(f"time{i}Stop"))
        mode  = (bean.get(f"time{i}Mode")
                 or bean.get(f"segment{i}Mode"))
        en    = (bean.get(f"forcedStopSwitch{i}")
                 or bean.get(f"segmentEnable{i}")
                 or bean.get(f"time{i}Enable"))
        if start or stop or mode is not None:
            segments.append({
                "segment_id":  i,
                "start":       start,
                "stop":        stop,
                "mode":        int(mode) if mode is not None else None,
                "mode_name":   MODE_NAMES.get(int(mode)) if mode is not None else None,
                "enabled":     bool(int(en)) if en is not None else None,
            })

    return {"ok": True, "raw": data, "segments": segments}


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def _write_segment(segment_id: int, mode: int, start_hour: int, start_min: int,
                   end_hour: int, end_min: int, enabled: bool) -> dict:
    if not 1 <= segment_id <= 9:
        raise ValueError(f"segment_id must be 1-9, got {segment_id}")
    if mode not in (0, 1, 2):
        raise ValueError(f"mode must be 0/1/2 (Load/Battery/Grid First), got {mode}")

    sess = get_session()
    sess.ensure_ready()

    payload = {
        "serialNum": SERIAL,
        "type":      f"time_segment{segment_id}",
        "param1":    str(mode),
        "param2":    str(start_hour),
        "param3":    str(start_min),
        "param4":    str(end_hour),
        "param5":    str(end_min),
        "param6":    "1" if enabled else "0",
        # param7–param19 must be present as empty strings (API contract)
        **{f"param{i}": "" for i in range(7, 20)},
    }

    r = sess._s.post(
        BASE + "/newTcpsetAPI.do",
        params={"op": "tlxSet"},
        data=payload,
        timeout=15,
    )
    try:
        result = r.json()
    except Exception:
        result = {"_status": r.status_code, "_raw": r.text[:300]}

    success = result.get("success") is True or result.get("msg") == "200"
    print(f"[growatt_tou] seg {segment_id} ({start_hour:02d}:{start_min:02d}–"
          f"{end_hour:02d}:{end_min:02d} mode={mode} en={enabled}): {result}")
    return {"segment_id": segment_id, "success": success, "response": result}


def _write_many(segments: list) -> list:
    results = []
    for seg in segments:
        try:
            res = _write_segment(
                segment_id = int(seg["segment_id"]),
                mode       = int(seg["mode"]),
                start_hour = int(seg.get("start_hour", 0)),
                start_min  = int(seg.get("start_min",  0)),
                end_hour   = int(seg.get("end_hour",   0)),
                end_min    = int(seg.get("end_min",    0)),
                enabled    = bool(seg.get("enabled", True)),
            )
        except Exception as e:
            res = {"segment_id": seg.get("segment_id"), "success": False, "error": str(e)}
        results.append(res)
    return results


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self._send(_read_tou())
        except Exception as e:
            print(f"[growatt_tou GET] {e}")
            self._send({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length)) if length else {}

            # Multiple segments
            if "segments" in body:
                results = _write_many(body["segments"])
                self._send({"ok": True, "results": results})
                return

            # Single segment
            res = _write_segment(
                segment_id = int(body["segment_id"]),
                mode       = int(body["mode"]),
                start_hour = int(body.get("start_hour", 0)),
                start_min  = int(body.get("start_min",  0)),
                end_hour   = int(body.get("end_hour",   0)),
                end_min    = int(body.get("end_min",    0)),
                enabled    = bool(body.get("enabled", True)),
            )
            self._send({"ok": True, **res})

        except Exception as e:
            print(f"[growatt_tou POST] {e}")
            self._send({"ok": False, "error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control",  "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
