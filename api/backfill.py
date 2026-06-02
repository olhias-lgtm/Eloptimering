"""
backfill — GET ?from=YYYY-MM-DD&to=YYYY-MM-DD&area=SE3
Computes and upserts daily_summary rows for a range of past dates.
Fetches energy readings from Supabase and spot prices from elprisetjustnu.se,
then replicates the frontend cost/earn formula in Python.

Optional tariff overrides (all öre/kWh unless noted, same defaults as the UI):
  natavg_in=26.0        nätavgift rörlig import
  energiskatt=54.875    energy tax
  fortum_paslag=6.96    Fortum per-kWh add-on
  fortum_fast=55.20     Fortum fixed monthly fee (kr/month)
  fast_avgift=390       Ellevio fixed monthly fee (kr/month)
  natnytta_high=5.50    nätnytta high-season
  natnytta_low=4.12     nätnytta low-season
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
ELPRISET_BASE = "https://www.elprisetjustnu.se/api/v1/prices"
SLOT_MIN = 5
KWH5 = SLOT_MIN / 60


# ---------------------------------------------------------------------------
# Energy helpers (mirrors energy.py logic)
# ---------------------------------------------------------------------------

def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _fetch_readings(date_str: str) -> list:
    """Fetch energy_readings for a CEST calendar date."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    day = date.fromisoformat(date_str)
    start = datetime(day.year, day.month, day.day, 0, 0, 0,
                     tzinfo=timezone(timedelta(hours=2))).isoformat()
    end   = datetime(day.year, day.month, day.day, 23, 59, 59,
                     tzinfo=timezone(timedelta(hours=2))).isoformat()
    url = (
        f"{SUPABASE_URL}/rest/v1/energy_readings"
        f"?ts=gte.{urllib.parse.quote(start)}"
        f"&ts=lte.{urllib.parse.quote(end)}"
        f"&order=ts.asc"
        f"&select=ts,ppv_kw,load_kw,export_kw,import_kw,soc_pct"
    )
    try:
        req = urllib.request.Request(url, headers=_sb_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[backfill] fetch_readings {date_str}: {e}")
        return []


def _bucket_readings(rows: list) -> dict:
    """Return {HH:MM: {ppv, load, export, import, soc}} averaged per 5-min slot."""
    total_slots = (24 * 60) // SLOT_MIN
    empty = lambda: {"ppv": 0.0, "load": 0.0, "export": 0.0,
                     "import": 0.0, "soc": None}
    buckets = {f"{(j*SLOT_MIN)//60:02d}:{(j*SLOT_MIN)%60:02d}": empty()
               for j in range(total_slots)}
    counts  = {k: 0 for k in buckets}

    tz_cest = timezone(timedelta(hours=2))
    for row in rows:
        ts_str = row["ts"]
        try:
            ts_bare = ts_str[:19].replace("T", " ")
            ts = datetime.strptime(ts_bare, "%Y-%m-%d %H:%M:%S").replace(
                     tzinfo=timezone.utc).astimezone(tz_cest)
        except Exception:
            continue
        slot_min = (ts.hour * 60 + ts.minute) // SLOT_MIN * SLOT_MIN
        label = f"{slot_min//60:02d}:{slot_min%60:02d}"
        if label not in buckets:
            continue
        b = buckets[label]
        b["ppv"]    += float(row.get("ppv_kw")    or 0)
        b["load"]   += float(row.get("load_kw")   or 0)
        b["export"] += float(row.get("export_kw") or 0)
        b["import"] += float(row.get("import_kw") or 0)
        soc = row.get("soc_pct")
        if soc is not None:
            b["soc"] = (b["soc"] or 0) + float(soc)
        counts[label] += 1

    for label, n in counts.items():
        if n > 0:
            b = buckets[label]
            for k in b:
                if k == "soc":
                    b[k] = round(b[k] / n, 1) if b[k] is not None else None
                else:
                    b[k] = round(b[k] / n, 3)
    return buckets


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _fetch_prices(date_str: str, area: str) -> tuple:
    """Return (price_map {HH:MM: ore_per_kwh}, raw_prices list)."""
    y, mo, d = date_str.split("-")
    url = f"{ELPRISET_BASE}/{y}/{mo}-{d}_{area}.json"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "electricity-dashboard/backfill"})
        with urllib.request.urlopen(req, timeout=10) as r:
            prices = json.loads(r.read())
    except Exception as e:
        print(f"[backfill] fetch_prices {date_str}: {e}")
        return {}, []

    if len(prices) < 2:
        return {}, []

    sorted_p = sorted(prices, key=lambda p: p["time_start"])
    t0 = datetime.fromisoformat(sorted_p[0]["time_start"])
    t1 = datetime.fromisoformat(sorted_p[1]["time_start"])
    interval_min = int((t1 - t0).total_seconds() / 60)

    price_map = {}
    for p in sorted_p:
        dt = datetime.fromisoformat(p["time_start"])
        ore = p["SEK_per_kWh"] * 100
        start_min = dt.hour * 60 + dt.minute
        for m in range(start_min, start_min + interval_min, SLOT_MIN):
            h, mn = divmod(m % (24 * 60), 60)
            price_map[f"{h:02d}:{mn:02d}"] = ore
    return price_map, sorted_p


# ---------------------------------------------------------------------------
# Tariff engine (mirrors frontend calcInterval / natnyttaAt)
# ---------------------------------------------------------------------------

def _natnytta(time_str: str, date_str: str, c: dict) -> float:
    h = int(time_str[:2])
    d = datetime.strptime(date_str, "%Y-%m-%d")
    is_weekday    = d.weekday() < 5
    is_high_season = d.month >= 11 or d.month <= 3
    is_daytime     = 6 <= h < 22
    return c["natnytta_high"] if (is_weekday and is_high_season and is_daytime) \
           else c["natnytta_low"]


def _compute_summary(buckets: dict, price_map: dict,
                     date_str: str, c: dict) -> dict:
    total_cost = total_earn = total_saved = 0.0
    solar_kwh = export_kwh = import_kwh = 0.0

    for time_str, b in buckets.items():
        spot = price_map.get(time_str)
        ppv  = b["ppv"];  load = b["load"]
        exp  = b["export"]; imp  = b["import"]

        solar_kwh  += ppv * KWH5
        export_kwh += exp * KWH5
        import_kwh += imp * KWH5

        if spot is None:
            continue

        import_rate = (spot + c["natavg_in"] + c["energiskatt"] + c["fortum_paslag"]) * c["moms"]
        export_rate = spot + _natnytta(time_str, date_str, c)

        total_cost += imp * KWH5 * import_rate / 100
        total_earn += exp * KWH5 * export_rate / 100

        self_kwh    = max(0.0, load - imp) * KWH5
        total_saved += self_kwh * import_rate / 100

    fixed_day  = (c["fast_avgift"] + c["fortum_fast"]) * c["moms"] / 30
    total_cost += fixed_day

    return {
        "solar_kwh":  round(solar_kwh,  2),
        "export_kwh": round(export_kwh, 2),
        "import_kwh": round(import_kwh, 2),
        "cost_kr":    round(total_cost - fixed_day, 2),   # variable only
        "earn_kr":    round(total_earn, 2),
        "fixed_kr":   round(fixed_day,  2),
        "net_kr":     round(total_earn - total_cost, 2),
        "saved_kr":   round(total_saved, 2),
    }


# ---------------------------------------------------------------------------
# Supabase upsert
# ---------------------------------------------------------------------------

def _upsert_spot_prices(raw_prices: list, area: str):
    """Batch-upsert native price intervals into spot_prices table."""
    rows = [
        {"ts": p["time_start"], "area": area, "sek_per_kwh": p["SEK_per_kWh"]}
        for p in raw_prices
    ]
    body = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/spot_prices?on_conflict=ts,area",
        data=body, method="POST",
        headers={
            **_sb_headers(),
            "Content-Type": "application/json",
            "Prefer":       "resolution=merge-duplicates,return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=10).read()


def _upsert(date_str: str, area: str, summary: dict):
    body = json.dumps({
        "day":            date_str,
        "area":           area,
        "solar_kwh":      summary["solar_kwh"],
        "export_kwh":     summary["export_kwh"],
        "import_kwh":     summary["import_kwh"],
        "import_cost_kr": summary["cost_kr"],
        "export_earn_kr": summary["earn_kr"],
        "fixed_cost_kr":  summary["fixed_kr"],
        "net_kr":         summary["net_kr"],
        "saved_kr":       summary["saved_kr"],
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/daily_summary?on_conflict=day,area",
        data=body, method="POST",
        headers={
            **_sb_headers(),
            "Content-Type": "application/json",
            "Prefer":       "resolution=merge-duplicates,return=minimal",
        },
    )
    urllib.request.urlopen(req, timeout=10).read()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))

        tz_cest = timezone(timedelta(hours=2))
        today   = datetime.now(timezone.utc).astimezone(tz_cest).date()

        try:
            from_date = date.fromisoformat(params.get("from", str(today - timedelta(days=30))))
            to_date   = date.fromisoformat(params.get("to",   str(today - timedelta(days=1))))
        except ValueError:
            self._send({"error": "invalid from/to date"}, 400)
            return

        if to_date >= today:
            to_date = today - timedelta(days=1)   # never backfill today

        if (to_date - from_date).days > 365:
            self._send({"error": "range too large (max 365 days)"}, 400)
            return

        area = params.get("area", "SE3")

        # Tariff config with UI defaults
        c = {
            "natavg_in":      float(params.get("natavg_in",      26.0)),
            "energiskatt":    float(params.get("energiskatt",     54.875)),
            "fortum_paslag":  float(params.get("fortum_paslag",  6.96)),
            "fortum_fast":    float(params.get("fortum_fast",    55.20)),
            "fast_avgift":    float(params.get("fast_avgift",    390.0)),
            "natnytta_high":  float(params.get("natnytta_high",  5.50)),
            "natnytta_low":   float(params.get("natnytta_low",   4.12)),
            "moms":           1.25,
        }

        results = []
        d = from_date
        while d <= to_date:
            date_str = str(d)
            try:
                rows     = _fetch_readings(date_str)
                if not rows:
                    results.append({"date": date_str, "status": "no_data"})
                    d += timedelta(days=1)
                    continue

                buckets             = _bucket_readings(rows)
                price_map, raw_prices = _fetch_prices(date_str, area)
                if not price_map:
                    results.append({"date": date_str, "status": "no_prices"})
                    d += timedelta(days=1)
                    continue

                summary = _compute_summary(buckets, price_map, date_str, c)
                _upsert(date_str, area, summary)
                _upsert_spot_prices(raw_prices, area)
                results.append({"date": date_str, "status": "ok", **summary})
                print(f"[backfill] {date_str}: earn={summary['earn_kr']} net={summary['net_kr']}")
            except Exception as e:
                print(f"[backfill] {date_str} error: {e}")
                results.append({"date": date_str, "status": "error", "error": str(e)})
            d += timedelta(days=1)

        self._send({"processed": len(results), "results": results})

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass
