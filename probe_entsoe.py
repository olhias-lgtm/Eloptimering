"""
ENTSO-E Transparency Platform — probe script for Swedish nuclear data.

Usage:
    ENTSOE_TOKEN=<your_token> python3 probe_entsoe.py

Requires a free API token from:
    https://transparency.entsoe.eu → My Account Settings → Web API Security Token

Probes two endpoints:
  1. Actual Generation per Production Type (A75) — nuclear output right now
  2. Installed Capacity per Production Type (A68) — nominal capacity per reactor/unit

ENTSO-E API base: https://web-api.tp.entsoe.eu/api
Date format: YYYYMMDDHHMM (UTC)
"""
import os
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from _tz import STHLM

TOKEN = os.environ.get("ENTSOE_TOKEN", "")
if not TOKEN:
    print("ERROR: set ENTSOE_TOKEN environment variable")
    print("  Get a free token at https://transparency.entsoe.eu → My Account Settings")
    sys.exit(1)

BASE = "https://web-api.tp.entsoe.eu/api"

# Sweden area EIC codes
SE_ALL = "10YSE-1--------K"   # Sweden whole (SvK CA)
SE1    = "10Y1001A1001A44P"
SE2    = "10Y1001A1001A45N"
SE3    = "10Y1001A1001A46L"   # our bidding zone
SE4    = "10Y1001A1001A47J"

PSR_NUCLEAR = "B14"


def _fmt(dt: datetime) -> str:
    """Format datetime as ENTSOE periodStart/End: YYYYMMDDHHMM (UTC)."""
    return dt.strftime("%Y%m%d%H%M")


def _fetch(params: dict) -> str:
    params["securityToken"] = TOKEN
    url = BASE + "?" + urllib.parse.urlencode(params)
    print(f"\nGET {url[:120]}...")
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code}: {body[:500]}")
        return ""


def _parse_timeseries(xml_text: str) -> list[dict]:
    """Parse ENTSO-E GL_MarketDocument / Unavailability XML into a flat list of rows."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"XML parse error: {e}")
        print(xml_text[:500])
        return []

    # Strip namespace for easier querying
    ns_map = {}
    for match in ET.iterparse(xml_text.encode() if isinstance(xml_text, str) else xml_text, events=["start-ns"]):
        _, (prefix, uri) = match
        if prefix == "":
            ns_map["ns"] = uri
            break

    def tag(name):
        if "ns" in ns_map:
            return f"{{{ns_map['ns']}}}{name}"
        return name

    rows = []
    for ts in root.iter(tag("TimeSeries")):
        unit_name = ""
        registered_cap = None
        psr_type = ""

        # Registered resource / mRID
        rr = ts.find(tag("registeredResource.mRID"))
        if rr is not None:
            unit_name = rr.text or ""

        # registered capacity (for capacity documents)
        rc = ts.find(tag("registeredCapacity"))
        if rc is not None:
            registered_cap = rc.text

        # psrType
        pt = ts.find(f".//{tag('psrType')}")
        if pt is not None:
            psr_type = pt.text or ""

        # Plant name if present
        plant_name = ""
        pn = ts.find(f".//{tag('name')}")
        if pn is not None:
            plant_name = pn.text or ""

        for period in ts.iter(tag("Period")):
            start_el = period.find(f".//{tag('start')}")
            resolution_el = period.find(tag("resolution"))
            start_str = start_el.text if start_el is not None else ""
            resolution = resolution_el.text if resolution_el is not None else "PT60M"

            # Parse resolution to minutes
            res_min = 60
            if resolution == "PT15M":
                res_min = 15
            elif resolution == "PT30M":
                res_min = 30
            elif resolution == "PT60M":
                res_min = 60

            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except Exception:
                start_dt = None

            for point in period.iter(tag("Point")):
                pos_el = point.find(tag("position"))
                qty_el = point.find(tag("quantity"))
                pos = int(pos_el.text) if pos_el is not None else 0
                qty = float(qty_el.text) if qty_el is not None else None

                if start_dt:
                    pt_dt = start_dt + timedelta(minutes=res_min * (pos - 1))
                    pt_local = pt_dt.astimezone(STHLM)
                    pt_str = pt_local.strftime(f"%Y-%m-%d %H:%M {pt_local.tzname()}")
                else:
                    pt_str = f"pos {pos}"

                rows.append({
                    "unit":    unit_name,
                    "name":    plant_name,
                    "psr":     psr_type,
                    "cap":     registered_cap,
                    "time":    pt_str,
                    "mw":      qty,
                })
    return rows


def probe_actual_generation():
    """A75 — Actual Generation per Production Type, nuclear only, last 24 h."""
    print("\n" + "="*60)
    print("1. ACTUAL GENERATION — Nuclear (B14), Sweden, last 24 h")
    print("="*60)

    now_utc = datetime.now(timezone.utc)
    start   = now_utc - timedelta(hours=24)

    xml = _fetch({
        "documentType": "A75",
        "processType":  "A16",
        "in_Domain":    SE_ALL,
        "psrType":      PSR_NUCLEAR,
        "periodStart":  _fmt(start),
        "periodEnd":    _fmt(now_utc),
    })

    rows = _parse_timeseries(xml)
    if not rows:
        print("No rows returned — trying per-bidding-zone...")
        for zone, eic in [("SE1", SE1), ("SE2", SE2), ("SE3", SE3), ("SE4", SE4)]:
            xml = _fetch({
                "documentType": "A75",
                "processType":  "A16",
                "in_Domain":    eic,
                "psrType":      PSR_NUCLEAR,
                "periodStart":  _fmt(start),
                "periodEnd":    _fmt(now_utc),
            })
            rows = _parse_timeseries(xml)
            if rows:
                print(f"  → Found data for {zone}")
                break

    print(f"\nRows returned: {len(rows)}")
    for r in rows[-12:]:   # last 12 points
        print(f"  {r['time']}  {r['mw']:>8.1f} MW  unit={r['unit'] or r['name'] or '—'}")


def probe_installed_capacity():
    """A68 — Installed Capacity per Production Type, nuclear only."""
    print("\n" + "="*60)
    print("2. INSTALLED CAPACITY — Nuclear (B14), Sweden")
    print("="*60)

    # Use current year; ENTSO-E returns annual data
    now_utc = datetime.now(timezone.utc)
    year    = now_utc.year
    start   = datetime(year, 1, 1, 0, 0, tzinfo=timezone.utc)
    end     = datetime(year, 12, 31, 23, 0, tzinfo=timezone.utc)

    xml = _fetch({
        "documentType": "A68",
        "processType":  "A33",
        "in_Domain":    SE_ALL,
        "psrType":      PSR_NUCLEAR,
        "periodStart":  _fmt(start),
        "periodEnd":    _fmt(end),
    })

    rows = _parse_timeseries(xml)
    print(f"\nRows returned: {len(rows)}")
    seen = set()
    for r in rows:
        key = (r["unit"], r["name"])
        if key not in seen:
            seen.add(key)
            print(f"  unit={r['unit'] or '—'}  name={r['name'] or '—'}  "
                  f"cap={r['cap'] or '—'} MW  mw={r['mw']}")


def probe_generation_per_unit():
    """A73 — Actual Generation per Generation Unit (individual reactors)."""
    print("\n" + "="*60)
    print("3. ACTUAL GENERATION PER UNIT — Nuclear reactors, last 24 h")
    print("="*60)

    now_utc = datetime.now(timezone.utc)
    start   = now_utc - timedelta(hours=24)

    xml = _fetch({
        "documentType": "A73",
        "processType":  "A16",
        "in_Domain":    SE_ALL,
        "psrType":      PSR_NUCLEAR,
        "periodStart":  _fmt(start),
        "periodEnd":    _fmt(now_utc),
    })

    rows = _parse_timeseries(xml)
    print(f"\nRows returned: {len(rows)}")
    # Show latest reading per unit
    latest = {}
    for r in rows:
        k = r["unit"] or r["name"]
        if r["mw"] is not None:
            latest[k] = r
    for k, r in sorted(latest.items()):
        print(f"  {r['time']}  {r['mw']:>8.1f} MW  unit={k}")


if __name__ == "__main__":
    probe_actual_generation()
    probe_installed_capacity()
    probe_generation_per_unit()
    print("\nDone.")
