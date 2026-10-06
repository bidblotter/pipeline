#!/usr/bin/env python3
"""PermitPicker Raleigh ingest — City of Raleigh building permits.

Source: Raleigh's ArcGIS open-data portal (same org as the trade feed, no auth):
  https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services/Building_Permits/FeatureServer/0

Layer 0 "Building Permits": ~184k records back to 2000 (publisher: City of
Raleigh), with applied/issue dates, estimated project cost (valuation),
contractor name/phone/email/license, parcel owner name/address, and lat/lon.

This fills the building-permit gap: wake_building is county-issued only
(unincorporated areas + contract towns), and Raleigh-issued building permits
were previously missing entirely. Raleigh TRADE permits are already covered
by the wake_trade feed (verified 2026-10-06: it carries Raleigh permit
numbers, Raleigh addresses, and the same DSINSP type taxonomy as Raleigh's
EnerGov system), so this ingester is building-only by design.

Quirks:
- statuscurrentmapped uses the same mapped taxonomy as Cary
  ("Permit Issued", "Permit Finaled", "Fees/Payment", ...).
- workclassmapped is coarse (New/Existing); Existing maps to our
  "alteration" bucket as the honest best-fit, same as Cary.
- The feed lags real time by ~6 days (max applieddate trailed 2026-10-06);
  the watermark trails by LAG_BUFFER_DAYS so late-published records are
  still caught. Overlap is harmless: upserts dedupe on
  (jurisdiction, permit_number).
- permitnum formats: BLDR-038528-2026 (residential), BLD-... etc.

Watermarking follows the Cary pattern: per-source watermark in ingest_runs,
trailed by LAG_BUFFER_DAYS, backfill via BACKFILL_DAYS env.

Env (GitHub Actions secrets): SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from ingest import sb, log, upsert_permits, get_watermark, record_run  # noqa: E402

BASE = ("https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services"
        "/Building_Permits/FeatureServer/0")
SOURCE = "raleigh"
LAG_BUFFER_DAYS = 7
PAGE = 1000
UA = {"User-Agent": "PermitPicker-ingest/1.0"}

OUT_FIELDS = [
    "permitnum", "permittype", "permittypemapped", "permitclassmapped",
    "workclass", "workclassmapped", "description", "proposedworkdescription",
    "statuscurrent", "statuscurrentmapped", "applieddate", "issueddate",
    "estprojectcost", "totalsqft", "originaladdress1", "originalcity",
    "originalstate", "originalzip", "contractorcompanyname", "contractorphone",
    "contractoremail", "contractorlicnum", "latitude_perm", "longitude_perm",
    "parcelownername", "parcelowneraddress1", "parcelowneraddress2",
    "projectname", "pin",
]

# Same mapped taxonomy as Cary's feed.
STATUS_MAP = {
    "Permit Issued": "Issued",
    "Permit Finaled": "Complete",
    "Permit Finaled with Conditions": "Complete",
    "Permit Cancelled": "Withdrawn",
    "Application Accepted": "Submitted",
    "Fees/Payment": "Submitted",
    "In Review": "In Review",
    "Occupancy": "Complete",
    "Appeal": "On Hold",
}


def ms_to_date(ms):
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()


def arcgis_query(where, retries=3):
    """Yield attribute dicts, paginating with resultOffset."""
    import time
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": ",".join(OUT_FIELDS),
            "orderByFields": "permitnum",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE),
            "returnGeometry": "false",
            "f": "json",
        }
        url = BASE + "/query?" + urllib.parse.urlencode(params)
        data = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                if "error" in data:
                    raise RuntimeError(f"ArcGIS error: {data['error']}")
                break
            except Exception as e:
                if attempt == retries - 1:
                    raise
                log(f"retry {attempt+1} after error: {e}")
                time.sleep(2 ** attempt)
        feats = data.get("features", [])
        for ft in feats:
            yield ft["attributes"]
        if len(feats) < PAGE:
            break
        offset += PAGE


def norm_raleigh(a):
    applied = ms_to_date(a.get("applieddate"))
    status = (STATUS_MAP.get(a.get("statuscurrentmapped"))
              or a.get("statuscurrentmapped") or a.get("statuscurrent"))
    addr = a.get("originaladdress1")
    city = (a.get("originalcity") or "").strip()
    city = city.title() if city else None
    # Owner name/address stay in `raw` (no dedicated columns yet) —
    # same as the Cary ingester.
    return {
        "jurisdiction": "raleigh",
        "permit_number": (a.get("permitnum") or "").strip(),
        "permit_type": (a.get("permittypemapped") or a.get("permittype") or "").strip(),
        "work_class": ("New" if a.get("workclassmapped") == "New" else "Existing"),
        "description": a.get("proposedworkdescription") or a.get("description"),
        "status": status,
        "applied_date": applied,
        "issued_date": ms_to_date(a.get("issueddate")),
        "address": addr,
        "city": city,
        "valuation": a.get("estprojectcost"),
        "contractor": a.get("contractorcompanyname"),
        "contractor_email": a.get("contractoremail"),
        "contractor_phone": a.get("contractorphone"),
        "contractor_license": a.get("contractorlicnum"),
        "latitude": a.get("latitude_perm"),
        "longitude": a.get("longitude_perm"),
        "source_feed": SOURCE,
        "raw": a,
    }


def main():
    today = dt.date.today().isoformat()
    backfill_days = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
    if backfill_days > 0:
        watermark = (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(days=backfill_days)).isoformat()
        log(f"[{SOURCE}] backfill override: {backfill_days} days")
    else:
        watermark = get_watermark(SOURCE)
    wm_date = watermark[:10]
    query_date = (dt.date.fromisoformat(wm_date)
                  - dt.timedelta(days=LAG_BUFFER_DAYS)).isoformat()
    log(f"[{SOURCE}] watermark: {watermark} (query applieddate > {query_date})")
    where = f"applieddate > DATE '{query_date}'"
    rows, fetched = [], 0
    try:
        for attrs in arcgis_query(where):
            row = norm_raleigh(attrs)
            if not row["permit_number"]:
                continue
            rows.append(row)
            fetched += 1
        log(f"[{SOURCE}] {fetched} new records since {wm_date}")
        inserted = upsert_permits(rows)
        record_run(SOURCE, "ok", fetched, inserted)
        log(f"[{SOURCE}] done: fetched={fetched} upserted={inserted}")
    except Exception as e:
        record_run(SOURCE, "error", fetched, 0, error=str(e))
        log(f"[{SOURCE}] ERROR: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
