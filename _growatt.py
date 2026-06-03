"""Shared Growatt session for Vercel serverless functions.

Each function file imports get_session() which returns a module-level singleton.

Session cookie persistence
--------------------------
On cold starts the module loads the last-saved session from Supabase
(cookies + plant_id + mix_serial). If the stored session is fresh enough
(<SESSION_MAX_AGE_HOURS old) the cookies are injected into requests.Session
and no login call is made. A new login is only triggered when:
  - No stored session exists
  - Stored session is too old
  - A Growatt API call returns a session-expired response

After every successful login the cookies are written back to Supabase so the
next cold-start invocation can reuse them. This reduces Growatt login calls
from O(requests/day) to O(1 per SESSION_MAX_AGE_HOURS).
"""
import json
import os
import threading
import time
import urllib.request
import urllib.error
import requests
from datetime import datetime, timezone, timedelta

GROWATT_API  = "https://openapi.growatt.com"
GROWATT_USER = os.environ.get("GROWATT_USER") or os.environ.get("GROWATT_USERNAME", "")
GROWATT_PASS = os.environ.get("GROWATT_PASS") or os.environ.get("GROWATT_PASSWORD", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
# Service-role key so we can access the no-public-policy growatt_session table
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                or os.environ.get("SUPABASE_ANON_KEY", ""))

# Reuse a stored session for up to this many hours before forcing a fresh login.
# Growatt's JSESSIONID appears to expire after ~1 hour server-side.
# Keep this below 1 hour so we proactively re-login before it goes stale.
SESSION_MAX_AGE_HOURS = 0.75  # 45 minutes

# ---------------------------------------------------------------------------
# Hard pause — set to None to re-enable Growatt API access.
# While set, ensure_ready() raises immediately without touching the network.
# Account was locked 2026-06-01; pause until 2026-06-03 12:00 CEST to be safe.
# ---------------------------------------------------------------------------
_HARD_PAUSED_UNTIL = None  # lifted 2026-06-02 ~22:44 CEST

# Login-failure cooldown: after a failed login, refuse to retry for this many seconds.
# Helps on warm Lambda reuse within the same invocation window.
_LOGIN_COOLDOWN_SECS = 300   # 5 minutes
_login_failed_at: float = 0  # time.monotonic() of last failure; 0 = never failed


# ---------------------------------------------------------------------------
# Supabase session persistence helpers
# ---------------------------------------------------------------------------

def _sb_headers() -> dict:
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def _load_stored_session() -> dict | None:
    """
    Fetch the single row from growatt_session.
    Returns the row dict if it exists and is fresh, else None.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        url = (f"{SUPABASE_URL}/rest/v1/growatt_session"
               f"?id=eq.1&select=cookies,plant_id,mix_serial,user_id,saved_at")
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=5) as r:
            rows = json.loads(r.read())
        if not rows:
            return None
        row = rows[0]
        saved_at = datetime.fromisoformat(row["saved_at"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
        if age_h > SESSION_MAX_AGE_HOURS:
            print(f"[Growatt] Stored session is {age_h:.1f}h old (max {SESSION_MAX_AGE_HOURS}h) — will re-login")
            return None
        print(f"[Growatt] Loaded session from Supabase ({age_h:.1f}h old, "
              f"plant={row.get('plant_id')}, serial={row.get('mix_serial')})")
        return row
    except Exception as e:
        print(f"[Growatt] Could not load session from Supabase: {e}")
        return None


def _save_stored_session(cookies: dict, plant_id: str, mix_serial: str, user_id: str) -> None:
    """Upsert the single session row in Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        body = json.dumps({
            "id":         1,
            "cookies":    cookies,
            "plant_id":   plant_id or "",
            "mix_serial": mix_serial or "",
            "user_id":    user_id or "",
            "saved_at":   datetime.now(timezone.utc).isoformat(),
        }).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/growatt_session",
            data=body,
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
        print("[Growatt] Session saved to Supabase")
    except Exception as e:
        print(f"[Growatt] Could not save session to Supabase: {e}")


def _clear_stored_session() -> None:
    """Delete the stored session row so next invocation is forced to re-login."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/growatt_session?id=eq.1",
            headers=_sb_headers(),
            method="DELETE",
        )
        urllib.request.urlopen(req, timeout=5).read()
        print("[Growatt] Stored session cleared from Supabase")
    except Exception as e:
        print(f"[Growatt] Could not clear session from Supabase: {e}")


# ---------------------------------------------------------------------------
# Growatt password hash
# ---------------------------------------------------------------------------

def _growatt_hash(password: str) -> str:
    """Growatt's custom MD5: replace '0' with 'c' at every even index."""
    import hashlib
    h = list(hashlib.md5(password.encode("utf-8")).hexdigest())
    for i in range(0, len(h), 2):
        if h[i] == "0":
            h[i] = "c"
    return "".join(h)


# ---------------------------------------------------------------------------
# GrowattSession
# ---------------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Restore from Supabase (no network call to Growatt)
    # ------------------------------------------------------------------

    def _restore(self, stored: dict) -> None:
        """Inject stored cookies and metadata into this session."""
        for name, value in (stored.get("cookies") or {}).items():
            self._s.cookies.set(name, value)
        self.plant_id   = stored.get("plant_id") or None
        self.mix_serial = stored.get("mix_serial") or None
        self.user_id    = stored.get("user_id") or ""
        self.logged_in  = True

    # ------------------------------------------------------------------
    # Login (fresh authentication against Growatt)
    # ------------------------------------------------------------------

    def login(self) -> None:
        global _login_failed_at  # noqa: PLW0603

        # Respect per-process cooldown on warm Lambda reuse
        if _login_failed_at:
            elapsed = time.monotonic() - _login_failed_at
            if elapsed < _LOGIN_COOLDOWN_SECS:
                remaining = int(_LOGIN_COOLDOWN_SECS - elapsed)
                raise RuntimeError(
                    f"Growatt login on cooldown — retry in {remaining}s "
                    f"(last failure {int(elapsed)}s ago)"
                )

        pw_hash = _growatt_hash(GROWATT_PASS)
        try:
            resp = self._s.post(
                GROWATT_API + "/newTwoLoginAPI.do",
                data={"userName": GROWATT_USER, "password": pw_hash},
                timeout=15,
            )
        except Exception as e:
            _login_failed_at = time.monotonic()
            raise RuntimeError(f"Growatt login network error: {e}") from e

        data = resp.json()
        back = data.get("back", {})
        if not back.get("success"):
            _login_failed_at = time.monotonic()
            raise RuntimeError(f"Growatt login failed: {back.get('msg','unknown')} | {data}")

        user           = back.get("user") or {}
        self.user_id   = str(user.get("id") or user.get("userId") or "")
        self.logged_in = True
        _login_failed_at = 0  # clear cooldown

        plant_list = back.get("data") or []
        if plant_list:
            self.plant_id = str(plant_list[0].get("plantId") or "")

        print(f"[Growatt] Fresh login as {GROWATT_USER}, plant={self.plant_id}")

        # Persist cookies so future cold-starts can skip the login
        _save_stored_session(
            cookies    = dict(self._s.cookies),
            plant_id   = self.plant_id or "",
            mix_serial = self.mix_serial or "",
            user_id    = self.user_id,
        )

    # ------------------------------------------------------------------
    # Discover plant / serial (best-effort, falls back to hardcoded values)
    # ------------------------------------------------------------------

    def discover(self) -> None:
        if not self.plant_id:
            self.plant_id = "10119069"
        if not self.mix_serial:
            self.mix_serial = "KJN6EXV00L"

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

        # Update persisted session with confirmed serial/plant
        if self.logged_in:
            _save_stored_session(
                cookies    = dict(self._s.cookies),
                plant_id   = self.plant_id or "",
                mix_serial = self.mix_serial or "",
                user_id    = self.user_id or "",
            )

    # ------------------------------------------------------------------
    # ensure_ready — called by every API method
    # ------------------------------------------------------------------

    def ensure_ready(self) -> None:
        # Hard pause check — zero network traffic while paused
        if _HARD_PAUSED_UNTIL is not None:
            now = datetime.now(timezone.utc)
            if now < _HARD_PAUSED_UNTIL:
                remaining_h = int((_HARD_PAUSED_UNTIL - now).total_seconds() / 3600)
                raise RuntimeError(
                    f"Growatt API paused — återkommer om ca {remaining_h}h "
                    f"(konto låst 2026-06-01, paus till 2026-06-03 12:00 CEST)"
                )

        if not self.logged_in:
            # Try to restore from Supabase before falling back to a fresh login
            stored = _load_stored_session()
            if stored:
                self._restore(stored)
            else:
                print("[GROWATT LIVE CALL] login — no stored session, authenticating fresh")
                self.login()
        else:
            print("[GROWATT LIVE CALL] using existing session (no re-login)")

        if not self.plant_id or not self.mix_serial:
            self.discover()

    # ------------------------------------------------------------------
    # Session-expiry detection
    # ------------------------------------------------------------------

    def _session_expired(self, data: dict) -> bool:
        if not isinstance(data, dict):
            return True
        back = data.get("back") or data
        if isinstance(back, dict):
            # Only treat as session expiry if the message explicitly says so.
            # A generic success=false can be a temporary server error — don't
            # tear down the session for that.
            msg = str(back.get("msg", "")).lower()
            if any(k in msg for k in ("login", "session", "expire", "timeout", "not login")):
                return True
        return False

    def _handle_expiry(self) -> None:
        """Called when a Growatt response indicates the session has expired."""
        print("[Growatt] Session expired — re-logging in (keeping old session until new one confirmed)")
        self.logged_in = False
        # Login first; only clear the stored session if login succeeds so a
        # failed re-login doesn't leave us with nothing to fall back on.
        self.login()
        _clear_stored_session()  # replaced by the save inside login()

    # ------------------------------------------------------------------
    # Data methods
    # ------------------------------------------------------------------

    def get_energy(self, target_date: str) -> dict:
        from datetime import date as _date, datetime as _dt, timezone as _tz, timedelta as _td
        self.ensure_ready()
        print(f"[GROWATT LIVE CALL] get_energy date={target_date}")
        for attempt in range(2):
            resp = self._s.post(
                GROWATT_API + "/newTlxApi.do",
                params={"op": "getEnergyProdAndCons_KW"},
                data={
                    "date":     target_date,
                    "plantId":  self.plant_id,
                    "language": "1",
                    "id":       self.mix_serial,
                    "type":     "0",  # 0=hour(5-min slots), 1=day(30-min), 2=month
                },
                timeout=15,
            )
            data = resp.json()
            if not self._session_expired(data):
                result = self._normalize_tlx(data)
                if target_date == _date.today().isoformat():
                    cest_now = _dt.now(_tz.utc) + _td(hours=2)
                    cutoff   = cest_now.hour * 60 + cest_now.minute + 30
                    cd = (result.get("obj") or {}).get("chartData") or {}
                    empty = {"ppv": 0.0, "sysOut": 0.0, "pacToUser": 0.0,
                             "pacToGrid": 0.0, "pdischarge": 0.0}
                    stale = any(
                        v.get("ppv", 0) > 1.0
                        for label, v in cd.items()
                        if int(label.split(":")[0]) * 60 + int(label.split(":")[1]) < 5 * 60
                    )
                    if stale:
                        print("[Growatt] Stale data detected — returning empty chart")
                        for label in cd:
                            cd[label] = empty.copy()
                    else:
                        for label in list(cd.keys()):
                            h, m = map(int, label.split(":"))
                            if h * 60 + m > cutoff:
                                cd[label] = empty.copy()
                return result
            if attempt == 0:
                self._handle_expiry()
        return data

    def get_live(self) -> dict:
        self.ensure_ready()
        print(f"[GROWATT LIVE CALL] get_live plant={self.plant_id} serial={self.mix_serial}")

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
            "soc_pct":          int(detail.get("bmsSoc") or 0) or None,
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

        minutes         = 5 if n > 48 else 30
        CEST_OFFSET_MIN = 60
        time_cd = {}

        def _v(arr, idx):
            try: return round(float(arr[idx]) / 10.0, 2)
            except: return 0.0

        for i in range(n):
            total_min   = (i * minutes + CEST_OFFSET_MIN) % (24 * 60)
            label       = f"{total_min // 60:02d}:{total_min % 60:02d}"
            is_daylight = 4 * 60 <= total_min < 22 * 60
            ppv = _v(solar, i) if is_daylight else 0.0
            time_cd[label] = {
                "ppv":        ppv,
                "sysOut":     _v(load, i),
                "pacToUser":  _v(pac_user, i) if pac_user else 0.0,
                "pacToGrid":  _v(pac_grid, i),
                "pdischarge": _v(pdis, i),
            }

        empty = {"ppv": 0.0, "sysOut": 0.0, "pacToUser": 0.0, "pacToGrid": 0.0, "pdischarge": 0.0}
        total_slots = (24 * 60) // minutes
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
