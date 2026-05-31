"""Probe which date format Growatt accepts for historical data."""
import json
from http.server import BaseHTTPRequestHandler
from _growatt import get_session, GROWATT_API

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            s = get_session()
            s.ensure_ready()
            results = {}
            # Past date in multiple formats
            # Also try alternate ops that might support historical dates
            alt_ops = [
                ("getTlxEnergyDayChart", {"date": "2026-05-29"}),
                ("getTlxEnergyData",     {"date": "2026-05-29", "type": "1"}),
                ("getEnergyProdAndCons", {"date": "2026-05-29", "type": "1"}),
                ("getTlxEnergyInfo",     {"date": "2026-05-29"}),
            ]
            for op, extra in alt_ops:
                try:
                    r2 = s._s.post(GROWATT_API + "/newTlxApi.do",
                        params={"op": op},
                        data={"plantId": s.plant_id, "id": s.mix_serial, **extra},
                        timeout=10)
                    body = r2.text.strip()[:120] if r2.text.strip() else "(empty)"
                    results[f"op:{op}"] = {"status": r2.status_code, "preview": body}
                except Exception as ex:
                    results[f"op:{op}"] = {"error": str(ex)}

            for label, params in {
                "YYYY-MM-DD": {"date": "2026-05-29", "type": "1"},
                "type2":      {"date": "2026-05-29", "type": "2"},
            }.items():
                resp = s._s.post(
                    GROWATT_API + "/newTlxApi.do",
                    params={"op": "getEnergyProdAndCons_KW"},
                    data={"plantId": s.plant_id, "language": "1",
                          "id": s.mix_serial, **params},
                    timeout=15,
                )
                if not resp.text.strip():
                    results[label] = {"error": "empty response"}
                    continue
                try:
                    rj = resp.json()
                except Exception:
                    results[label] = {"error": f"non-JSON: {resp.text[:100]!r}"}
                    continue
                obj = rj.get("obj", {})
                cd  = obj.get("chartData", {})
                # Find total non-zero slots and last non-zero time
                if isinstance(cd, dict):
                    sysout = cd.get("sysOut") or []
                    n_slots = len(sysout)
                    nonzero = [i for i,v in enumerate(sysout) if v and float(v) > 0]
                    last_nz = nonzero[-1] if nonzero else -1
                    # Also grab the date stored in obj if any
                    obj_date = obj.get("date", "?")
                    results[label] = {"n_slots": n_slots, "nonzero": len(nonzero),
                                      "last_nonzero_idx": last_nz, "obj_date": obj_date,
                                      "first3_sysout": sysout[:3]}
                else:
                    results[label] = {"error": f"chartData type={type(cd).__name__}"}
            self._send(results)
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
