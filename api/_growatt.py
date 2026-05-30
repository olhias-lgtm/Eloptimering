"""Shared Growatt session for Vercel serverless functions.

Each function file imports get_session() which returns a module-level singleton.
On cold starts the session re-logs in using env vars. On warm Lambda reuse the
existing authenticated session is reused.
"""
import os
import threading
import time
import requests

GROWATT_API  = "https://openapi.growatt.com"
GROWATT_USER = os.environ.get("GROWATT_USER", "")
GROWATT_PASS = os.environ.get("GROWATT_PASS", "")  # plain password — Growatt hashes internally

_lock    = threading.Lock()
_session = None   # module-level singleton


class GrowattSession:
    def __init__(self):
        self.logged_in  = False
        self.plant_id   = None
        self.mix_serial = None
        self.user_id    = None
        self._s = requests.Session()
        self._s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        })

    def login(self):
        import hashlib
        pw_hash = hashlib.md5(GROWATT_PASS.encode()).hexdigest()
        resp = self._s.post(
            GROWATT_API + "/newTwoLoginAPI.do",
            data={"userName": GROWATT_USER, "password": pw_hash},
            timeout=15,
        )
        data = resp.json()
        back = data.get("back", {})
        if not back.get("success"):
            raise RuntimeError(f"Growatt login failed: {back.get('msg','unknown')}")
        self.user_id   = back.get("user", {}).get("id")
        self.logged_in = True
        print(f"[Growatt] Logged in as {GROWATT_USER}")

    def discover(self):
        resp = self._s.post(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getTlxListByPlant"},
            data={"plantId": self.plant_id or "", "currPage": "1"},
            timeout=15,
        )
        data = resp.json()
        # Try to extract plant_id from plant list if not already set
        if not self.plant_id:
            plant_resp = self._s.post(
                GROWATT_API + "/newTwoLoginAPI.do",
                params={"op": "getAllPlantList"},
                data={"userId": self.user_id},
                timeout=15,
            )
            plants = plant_resp.json().get("back", {}).get("data", []) or []
            if plants:
                self.plant_id = str(plants[0].get("plantId") or plants[0].get("id", ""))

        # Fallback to known values from local proxy
        if not self.plant_id:
            self.plant_id = "10119069"
        if not self.mix_serial:
            self.mix_serial = "KJN6EXV00L"
        print(f"[Growatt] plant={self.plant_id} serial={self.mix_serial}")

    def ensure_ready(self):
        if not self.logged_in:
            self.login()
        if not self.plant_id or not self.mix_serial:
            self.discover()

    def _session_expired(self, data):
        if not isinstance(data, dict):
            return True
        back = data.get("back") or data
        if isinstance(back, dict):
            if back.get("success") is False:
                return True
            msg = str(back.get("msg", "")).lower()
            if any(k in msg for k in ("login", "session", "expire", "timeout")):
                return True
        return False

    def get_energy(self, target_date: str) -> dict:
        self.ensure_ready()
        for attempt in range(2):
            resp = self._s.post(
                GROWATT_API + "/newTlxApi.do",
                params={"op": "getEnergyProdAndCons_KW"},
                data={
                    "date":     target_date,
                    "plantId":  self.plant_id,
                    "language": "1",
                    "id":       self.mix_serial,
                    "type":     "1",
                },
                timeout=15,
            )
            data = resp.json()
            if not self._session_expired(data):
                return self._normalize_tlx(data)
            if attempt == 0:
                print("[Growatt] Session expired — re-logging in")
                self.logged_in = False
                self.login()
        return data

    def get_live(self) -> dict:
        self.ensure_ready()
        detail_resp = self._s.get(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getTlxDetailData", "id": self.mix_serial},
            timeout=10,
        )
        detail = detail_resp.json().get("data", {}) or {}

        status_resp = self._s.post(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getSystemStatus_KW"},
            data={"plantId": self.plant_id, "id": self.mix_serial},
            timeout=10,
        )
        status = status_resp.json().get("obj", {}) or {}

        overview_resp = self._s.post(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getEnergyOverview"},
            data={"plantId": self.plant_id, "id": self.mix_serial},
            timeout=10,
        )
        overview = overview_resp.json().get("obj", {}) or {}

        def _w(d, k):
            try: return round(float(d.get(k, 0) or 0) / 1000, 3)
            except: return 0.0

        def _kwh(d, k):
            try: return round(float(d.get(k, 0) or 0), 2)
            except: return 0.0

        return {
            "ppv_kw":           _w(detail, "ppv"),
            "ppv1_kw":          _w(detail, "ppv1"),
            "ppv2_kw":          _w(detail, "ppv2"),
            "pac_kw":           _w(detail, "pac"),
            "load_kw":          _w(detail, "pacToLocalLoad"),
            "export_kw":        _w(detail, "pacToGridTotal"),
            "import_kw":        _w(detail, "pacToUserTotal"),
            "self_kw":          _w(detail, "pself"),
            "discharge_kw":     _kwh(status, "pdisCharge"),
            "charge_kw":        _kwh(status, "chargePower"),
            "epv_today":        _kwh(overview, "epvToday"),
            "eac_today":        _kwh(detail, "eacToday"),
            "echarge_today":    _kwh(detail, "echargeToday"),
            "edischarge_today": _kwh(detail, "edischargeToday"),
            "eload_today":      _kwh(detail, "elocalLoadToday"),
            "export_today":     _kwh(detail, "etoGridToday"),
            "import_today":     _kwh(detail, "etoUserToday"),
        }

    def _normalize_tlx(self, data: dict) -> dict:
        obj = data.get("obj", {})
        cd  = obj.get("chartData", {})

        solar    = cd.get("sysOut")    or []
        load     = cd.get("acCharge")  or []
        pac_grid = cd.get("pacToGrid") or []
        pdis     = cd.get("epv3")      or []
        pac_user = []

        n = max(len(solar), len(load), len(pac_grid), len(pdis), 0)
        if n == 0:
            return data

        minutes          = 5 if n > 48 else 30
        CEST_OFFSET_MIN  = -6 * 60
        time_cd = {}

        def _v(arr, idx):
            try: return round(float(arr[idx]) / 12.5, 2)
            except: return 0.0

        for i in range(n):
            total_min  = (i * minutes + CEST_OFFSET_MIN) % (24 * 60)
            if total_min >= 18 * 60:
                continue
            label      = f"{total_min // 60:02d}:{total_min % 60:02d}"
            is_daylight = 4 * 60 <= total_min < 22 * 60
            ppv = max(0.0, round(_v(solar, i) - _v(pdis, i), 2)) if is_daylight else 0.0
            time_cd[label] = {
                "ppv":        ppv,
                "sysOut":     _v(load, i),
                "pacToUser":  _v(pac_user, i) if pac_user else 0.0,
                "pacToGrid":  _v(pac_grid, i),
                "pdischarge": _v(pdis, i),
            }

        obj["chartData"] = time_cd
        data["obj"] = obj
        return data


# ---------------------------------------------------------------------------
# Module-level singleton — survives warm Lambda reuse
# ---------------------------------------------------------------------------
_session_instance = None
_session_lock     = threading.Lock()


def get_session() -> GrowattSession:
    global _session_instance
    if _session_instance is None:
        with _session_lock:
            if _session_instance is None:
                _session_instance = GrowattSession()
    return _session_instance
