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
GROWATT_USER = os.environ.get("GROWATT_USER") or os.environ.get("GROWATT_USERNAME", "")
GROWATT_PASS = os.environ.get("GROWATT_PASS") or os.environ.get("GROWATT_PASSWORD", "")


def _growatt_hash(password: str) -> str:
    """Growatt's custom MD5: replace '0' with 'c' at every even index."""
    import hashlib
    h = list(hashlib.md5(password.encode("utf-8")).hexdigest())
    for i in range(0, len(h), 2):
        if h[i] == "0":
            h[i] = "c"
    return "".join(h)

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
        pw_hash = _growatt_hash(GROWATT_PASS)
        resp = self._s.post(
            GROWATT_API + "/newTwoLoginAPI.do",
            data={"userName": GROWATT_USER, "password": pw_hash},
            timeout=15,
        )
        data = resp.json()
        back = data.get("back", {})
        if not back.get("success"):
            raise RuntimeError(f"Growatt login failed: {back.get('msg','unknown')} | {data}")
        user           = back.get("user") or {}
        self.user_id   = str(user.get("id") or user.get("userId") or "")
        self.logged_in = True
        # Grab plant_id from login response if available
        plant_list = back.get("data") or []
        if plant_list:
            self.plant_id = str(plant_list[0].get("plantId") or "")
        print(f"[Growatt] Logged in as {GROWATT_USER}, plant={self.plant_id}")

    def discover(self):
        # Use known-good fallback values first so we never block on a failed API call
        if not self.plant_id:
            self.plant_id = "10119069"
        if not self.mix_serial:
            self.mix_serial = "KJN6EXV00L"

        # Try to get the real serial from the device list (best-effort)
        try:
            resp = self._s.post(
                GROWATT_API + "/newTlxApi.do",
                params={"op": "getTlxListByPlant"},
                data={"plantId": self.plant_id, "currPage": "1"},
                timeout=10,
            )
            data = resp.json() if resp.text.strip() else {}
            devices = (data.get("obj") or {}).get("datas") or []
            for d in devices:
                sn = d.get("tlxSn") or d.get("sn") or d.get("deviceSn")
                if sn:
                    self.mix_serial = sn
                    break
        except Exception as e:
            print(f"[Growatt] discover fallback (using hardcoded serial): {e}")

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
        from datetime import date as _date, datetime as _dt, timezone as _tz, timedelta as _td
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
                result = self._normalize_tlx(data)
                # Growatt often returns the last complete production day rather than
                # today-so-far. Truncate any slots beyond the current CEST time so the
                # chart never shows "future" data when viewing today.
                if target_date == _date.today().isoformat():
                    cest_now = _dt.now(_tz.utc) + _td(hours=2)
                    cutoff   = cest_now.hour * 60 + cest_now.minute + 30  # 30-min grace
                    cd = (result.get("obj") or {}).get("chartData") or {}
                    empty = {"ppv": 0.0, "sysOut": 0.0, "pacToUser": 0.0,
                             "pacToGrid": 0.0, "pdischarge": 0.0}

                    # Detect stale data: Growatt often serves yesterday's complete day
                    # in the morning. If any slot before 05:00 CEST shows ppv > 1 kW
                    # the data is from a previous day — wipe it all.
                    stale = any(
                        v.get("ppv", 0) > 1.0
                        for label, v in cd.items()
                        if int(label.split(":")[0]) * 60 + int(label.split(":")[1]) < 5 * 60
                    )
                    if stale:
                        print("[Growatt] Stale data detected (yesterday's day) — returning empty chart")
                        for label in cd:
                            cd[label] = empty.copy()
                    else:
                        # Truncate future slots
                        for label in list(cd.keys()):
                            h, m = map(int, label.split(":"))
                            if h * 60 + m > cutoff:
                                cd[label] = empty.copy()
                return result
            if attempt == 0:
                print("[Growatt] Session expired — re-logging in")
                self.logged_in = False
                self.login()
        return data

    def get_live(self) -> dict:
        self.ensure_ready()
        print(f"[live] plant={self.plant_id} serial={self.mix_serial}")

        detail_resp = self._s.get(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getTlxDetailData", "id": self.mix_serial},
            timeout=10,
        )
        print(f"[live] detail status={detail_resp.status_code} body={detail_resp.text[:200]!r}")
        try:
            detail = detail_resp.json().get("data", {}) or {}
        except Exception as e:
            raise RuntimeError(f"detail parse failed ({detail_resp.status_code}): {detail_resp.text[:300]!r}") from e

        status_resp = self._s.post(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getSystemStatus_KW"},
            data={"plantId": self.plant_id, "id": self.mix_serial},
            timeout=10,
        )
        print(f"[live] sysStatus={status_resp.status_code} body={status_resp.text[:200]!r}")
        try:
            status = status_resp.json().get("obj", {}) or {}
        except Exception as e:
            raise RuntimeError(f"status parse failed ({status_resp.status_code}): {status_resp.text[:300]!r}") from e

        overview_resp = self._s.post(
            GROWATT_API + "/newTlxApi.do",
            params={"op": "getEnergyOverview"},
            data={"plantId": self.plant_id, "id": self.mix_serial},
            timeout=10,
        )
        print(f"[live] overview={overview_resp.status_code} body={overview_resp.text[:200]!r}")
        try:
            overview = overview_resp.json().get("obj", {}) or {}
        except Exception as e:
            raise RuntimeError(f"overview parse failed ({overview_resp.status_code}): {overview_resp.text[:300]!r}") from e

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
            # SOC probe — check every plausible field name across all three responses
            "_soc_probe": {
                "detail_soc":        detail.get("soc"),
                "detail_SOC":        detail.get("SOC"),
                "detail_batSoc":     detail.get("batSoc"),
                "detail_bmsSoc":     detail.get("bmsSoc"),
                "detail_capacity":   detail.get("capacity"),
                "status_SOC":        status.get("SOC"),
                "status_soc":        status.get("soc"),
                "status_batCapcity": status.get("batCapcity"),
                "status_capacity":   status.get("capacity"),
                "overview_soc":      overview.get("soc"),
                "overview_SOC":      overview.get("SOC"),
            },
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
        CEST_OFFSET_MIN  = 60  # Growatt uses CET (UTC+1); add 1h for CEST (UTC+2) in summer
        time_cd = {}

        def _v(arr, idx):
            try: return round(float(arr[idx]) / 10.0, 2)
            except: return 0.0

        for i in range(n):
            total_min  = (i * minutes + CEST_OFFSET_MIN) % (24 * 60)
            label      = f"{total_min // 60:02d}:{total_min % 60:02d}"
            is_daylight = 4 * 60 <= total_min < 22 * 60
            # sysOut = total DC input to inverter (solar); use it directly for ppv.
            # epv3 is NOT battery discharge — subtracting it was incorrectly deflating solar.
            ppv = _v(solar, i) if is_daylight else 0.0
            time_cd[label] = {
                "ppv":        ppv,
                "sysOut":     _v(load, i),
                "pacToUser":  _v(pac_user, i) if pac_user else 0.0,
                "pacToGrid":  _v(pac_grid, i),
                "pdischarge": _v(pdis, i),
            }

        # Pad with empty slots to end of day so the chart always shows a full axis
        empty = {"ppv": 0.0, "sysOut": 0.0, "pacToUser": 0.0, "pacToGrid": 0.0, "pdischarge": 0.0}
        total_slots = (24 * 60) // minutes  # 00:00 → 23:30
        for j in range(total_slots):
            label = f"{(j * minutes) // 60:02d}:{(j * minutes) % 60:02d}"
            if label not in time_cd:
                time_cd[label] = empty.copy()

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
