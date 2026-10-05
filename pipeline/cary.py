#!/usr/bin/env python3
"""PermitPicker Cary ingest — Town of Cary building permit applications.

Source: Cary's Opendatasoft open-data portal (daily refresh, no auth):
  https://data.townofcary.org/api/explore/v2.1/catalog/datasets/permit-applications/records

41 fields including applied/issue dates, project cost (valuation), contractor
name/phone, owner name, lat/lon, and pre-mapped taxonomies (permit class,
work class, trade, status).

Quirks:
- A few rows carry future applieddate values (e.g. 2027); the query window
  excludes applieddate >= today so junk never enters.
- workclassmapped is coarse (New/Existing); Existing maps to our
  "alteration" bucket as the honest best-fit for work on existing structures.
- permittypemapped covers Mechanical/Building/Electrical/Plumbing/Demolition;
  rows with null mapping fall back to the raw permittype code.

Watermarking follows the Wake pattern: per-source watermark in ingest_runs,
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

BASE = ("https://data.townofcary.org/api/explore/v2.1/catalog"
        "/datasets/permit-applications/records")
SOURCE = "cary"
LAG_BUFFER_DAYS = 7
PAGE = 100

FIELDS = ("permitnum,description,applieddate,issuedate,statuscurrent,"
          "statuscurrentmapped,originaladdress1,originalcity,permitclassmapped,"
          "workclassmapped,permittype,permittypemapped,totalsqft,latitude,"
          "longitude,projectcost,contractorcompanyname,contractorphone")

STATUS_MAP = {
    "Permit Issued": "Issued",
    "In Review": "In Review",
    "Occupancy": "Complete",
    "Permit Finaled": "Complete",
    "Permit Finaled with Conditions": "Complete",
    "Permit Cancelled": "Withdrawn",
    "Application Accepted": "Submitted",
    "Fees/Payment": "Submitted",
    "Appeal": "On Hold",
}


def cary_query(where, offset):
    params = {
        "where": where,
        "select": FIELDS,
        "order_by": "permitnum",
        "limit": str(PAGE),
        "offset": str(offset),
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "PermitPicker-ingest/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def norm_cary(a, today):
    applied = (a.get("applieddate") or "")[:10]
    if not applied or applied >= today:
        applied = None
    tmap = a.get("permittypemapped")
    return {
        "jurisdiction": "cary",
        "permit_number": (a.get("permitnum") or "").strip(),
        "permit_type": tmap.lower() if tmap else (a.get("permittype") or "").strip(),
        "work_class": "New" if a.get("workclassmapped") == "New" else "Existing",
        "description": a.get("description"),
        "status": STATUS_MAP.get(a.get("statuscurrentmapped"),
                                 a.get("statuscurrentmapped") or a.get("statuscurrent")),
        "applied_date": applied,
        "issued_date": (a.get("issuedate") or "")[:10] or None,
        "address": a.get("originaladdress1"),
        "city": "Cary",
        "valuation": a.get("projectcost"),
        "contractor": a.get("contractorcompanyname"),
        "contractor_email": None,
        "contractor_phone": a.get("contractorphone"),
        "contractor_license": None,
        "latitude": a.get("latitude"),
        "longitude": a.get("longitude"),
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
    where = (f"applieddate > date'{query_date}' AND applieddate < date'{today}'")
    rows, fetched, offset = [], 0, 0
    try:
        while True:
            d = cary_query(where, offset)
            batch = d.get("results", [])
            if not batch:
                break
            for a in batch:
                row = norm_cary(a, today)
                if not row["permit_number"]:
                    continue
                rows.append(row)
                fetched += 1
            if len(batch) < PAGE:
                break
            offset += PAGE
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
