#!/usr/bin/env python3
"""BidBlotter Durham ingest — daily diff of the "Active *" ArcGIS datasets.

Durham's feeds have no date fields, so "what's new" = Permit_IDs present
today but absent from yesterday's snapshot (durham_permit_snapshot).
New permits are normalized into the permits table (jurisdiction='durham').

Phase 2 follow-up: enrich via the LDO portal
(https://ldo4.durhamnc.gov/DurhamWeb/Search/ApplicationSearch) for real
application dates and applicant info. For now, applied_date = first seen
by BidBlotter, which is the honest "new" signal at daily granularity.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from ingest import arcgis_query, sb, log, upsert_permits, record_run  # noqa: E402

BASE = ("https://services2.arcgis.com/G5vR3cOjh6g2Ed8E"
        "/arcgis/rest/services/Permits/FeatureServer")
LAYERS = {
    "building": 13,
    "plumbing": 14,
    "mechanical": 15,
    "electrical": 16,
}
FIELDS = ["Permit_ID", "P_Type", "P_Status", "P_Activity", "P_Descript",
          "SiteAdd", "Inspector", "PID", "PIN"]

SOURCE = "durham_active"


def load_snapshot():
    """Return set of (layer, permit_id) already seen."""
    seen, offset, page = set(), 0, 5000
    while True:
        rows = sb("durham_permit_snapshot",
                  params=f"?select=layer,permit_id&limit={page}&offset={offset}")
        if not rows:
            break
        for r in rows:
            seen.add((r["layer"], r["permit_id"]))
        if len(rows) < page:
            break
        offset += page
    return seen


def norm_durham(layer, a, now_iso):
    return {
        "jurisdiction": "durham",
        "permit_number": str(a.get("Permit_ID")).strip(),
        "permit_type": a.get("P_Type"),
        "work_class": a.get("P_Activity"),
        "description": a.get("P_Descript"),
        "status": a.get("P_Status"),
        "applied_date": now_iso,   # first seen by BidBlotter (no date in feed)
        "issued_date": None,
        "address": a.get("SiteAdd"),
        "city": "Durham",
        "valuation": None,
        "contractor": None,
        "contractor_email": None,
        "contractor_phone": None,
        "contractor_license": None,
        "latitude": None,
        "longitude": None,
        "source_feed": SOURCE,
        "raw": a,
    }


def main():
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    log("loading snapshot")
    seen = load_snapshot()
    log(f"snapshot holds {len(seen)} permits")
    new_rows, new_snap, fetched = [], [], 0
    failures = []
    for layer, lid in LAYERS.items():
        try:
            n_layer = 0
            for attrs in arcgis_query(f"{BASE}/{lid}", "1=1", FIELDS, "Permit_ID"):
                fetched += 1
                n_layer += 1
                pid = attrs.get("Permit_ID")
                if not pid:
                    continue
                pid = str(pid).strip()
                if (layer, pid) in seen:
                    continue
                new_rows.append(norm_durham(layer, attrs, now_iso))
                new_snap.append({
                    "layer": layer,
                    "permit_id": pid,
                    "p_type": attrs.get("P_Type"),
                    "p_status": attrs.get("P_Status"),
                    "p_activity": attrs.get("P_Activity"),
                    "p_descript": attrs.get("P_Descript"),
                    "site_add": attrs.get("SiteAdd"),
                    "pid": attrs.get("PID"),
                })
                seen.add((layer, pid))  # intra-run dupes
            log(f"[{layer}] scanned={n_layer}")
        except Exception as e:
            log(f"[{layer}] ERROR: {e}")
            failures.append(layer)
    log(f"new Durham permits: {len(new_rows)} (scanned {fetched})")
    inserted = 0
    try:
        if new_snap:
            # ignore-duplicates keeps first_seen_at for existing rows
            sb("durham_permit_snapshot", method="POST", body=new_snap,
               params="?on_conflict=layer,permit_id",
               prefer="resolution=ignore-duplicates")
        inserted = upsert_permits(new_rows)
        status = "ok" if not failures else "error"
        record_run(SOURCE, status, fetched, inserted,
                   error=f"failed layers: {failures}" if failures else None)
        log(f"done: fetched={fetched} new={len(new_rows)} upserted={inserted}")
    except Exception as e:
        log(f"ERROR writing: {e}")
        try:
            record_run(SOURCE, "error", fetched, 0, error=str(e))
        except Exception:
            pass
        failures.append("write")
    if failures:
        log(f"failed: {failures}")
        sys.exit(1)
    log("Durham ingest ok")


if __name__ == "__main__":
    main()
