#!/usr/bin/env python3
"""
PermitPicker daily ingest — Wake County permit feeds -> Supabase.

Sources (public ArcGIS REST APIs, no auth):
  wake_building: Wake County Building Permits (nightly refresh)
    https://maps.wake.gov/arcgis/rest/services/Inspections/Building_Permits/MapServer/0
  wake_trade:    Wake County Permits Other Than Buildings (electrical/plumbing/mechanical)
    https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services/Permits_Other_Than_Buildings/FeatureServer/0

Durham County is PHASE 2: its open-data permit feeds are either stale (2021) or
lack date fields, so "what's new" ingestion isn't possible from them yet.
See README.md.

Env (GitHub Actions secrets):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

SOURCES = {
    "wake_building": "https://maps.wake.gov/arcgis/rest/services/Inspections/Building_Permits/MapServer/0",
    "wake_trade": "https://services.arcgis.com/v400IkDOw1ad7Yad/arcgis/rest/services/Permits_Other_Than_Buildings/FeatureServer/0",
}

PAGE = 1000
UA = {"User-Agent": "PermitPicker-ingest/1.0"}

# Trailing window (days) subtracted from the per-source watermark before
# querying the county feeds. The feeds publish records ~2-4 days AFTER the
# date stored in the record itself (measured 2026-10-04: the trade feed's max
# issueddate trailed real time by 3 days, building ~2 days). A strict
# "since last run" query would permanently miss those late-published records
# and the DB would freeze at the last backfill — so the query always trails
# the watermark by this buffer. Overlap is harmless: upserts dedupe on
# (jurisdiction, permit_number), and first_seen_at is not in the upsert
# payload, so the digest's "new since yesterday" keeps reflecting true
# first discovery. A failed/skipped run still backfills, because the
# watermark only advances on a successful run.
LAG_BUFFER_DAYS = 7


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def arcgis_query(base, where, out_fields, order_field, retries=5):
    """Yield attribute dicts, paginating with resultOffset."""
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": ",".join(out_fields),
            "orderByFields": order_field,
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE),
            "returnGeometry": "false",
            "f": "json",
        }
        url = base + "/query?" + urllib.parse.urlencode(params)
        data = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                # ArcGIS can return transient 400s in the body; retry those too
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
        if len(feats) < PAGE or data.get("exceededTransferLimit"):
            # exceededTransferLimit with full page -> keep paging by offset anyway
            if len(feats) < PAGE:
                break
        offset += PAGE


def ms_to_iso(ms):
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


def norm_wake_building(a):
    addr = " ".join(x for x in [a.get("MAILING_ADDRESS"), a.get("MAILING_CITY")] if x)
    return {
        "jurisdiction": "wake",
        "permit_number": a.get("PERMIT_NUMBER"),
        "permit_type": a.get("PERMIT_TYPE"),
        "work_class": a.get("WORK_CLASS"),
        "description": a.get("DESCRIPTION"),
        "status": a.get("PERMIT_STATUS"),
        "applied_date": ms_to_iso(a.get("APPLICATION_DATE")),
        "issued_date": ms_to_iso(a.get("ISSUE_DATE")),
        "address": a.get("MAILING_ADDRESS"),
        "city": a.get("MAILING_CITY"),
        "valuation": a.get("VALUATION"),
        "contractor": a.get("CONTRACTOR"),
        "contractor_email": None,
        "contractor_phone": None,
        "contractor_license": None,
        "latitude": a.get("Y"),
        "longitude": a.get("X"),
        "source_feed": "wake_building",
        "raw": a,
    }


def norm_wake_trade(a):
    return {
        "jurisdiction": "wake",
        "permit_number": a.get("permitnum"),
        "permit_type": a.get("permittype"),
        "work_class": a.get("workclass"),
        "description": a.get("proposedworkdescription") or a.get("description"),
        "status": a.get("statuscurrent"),
        "applied_date": ms_to_iso(a.get("insertion_date")),
        "issued_date": ms_to_iso(a.get("issueddate")),
        "address": a.get("originaladdressfull") or a.get("originaladdress1"),
        "city": a.get("originalcity"),
        "valuation": None,
        "contractor": a.get("contractorcompanyname"),
        "contractor_email": a.get("contractoremail"),
        "contractor_phone": a.get("contractorphone"),
        "contractor_license": a.get("contractorlicnum"),
        "latitude": None,
        "longitude": None,
        "source_feed": "wake_trade",
        "raw": a,
    }


NORMALIZERS = {"wake_building": norm_wake_building, "wake_trade": norm_wake_trade}
DATE_FIELDS = {"wake_building": "APPLICATION_DATE", "wake_trade": "issueddate"}
OUT_FIELDS = {
    "wake_building": ["PERMIT_NUMBER", "DESCRIPTION", "APPLICATION_DATE", "ISSUE_DATE",
                      "PERMIT_STATUS", "MAILING_ADDRESS", "MAILING_CITY", "PERMIT_TYPE",
                      "WORK_CLASS", "VALUATION", "CONTRACTOR", "X", "Y"],
    "wake_trade": ["permitnum", "permittype", "workclass", "description",
                   "proposedworkdescription", "statuscurrent", "insertion_date",
                   "issueddate", "originaladdressfull", "originaladdress1",
                   "originalcity", "contractorcompanyname", "contractoremail",
                   "contractorphone", "contractorlicnum"],
}


def sb(path, method="GET", body=None, params="", prefer=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else None


def get_watermark(source):
    rows = sb("ingest_runs", params=f"?select=started_at&status=eq.ok&source=eq.{source}&order=started_at.desc&limit=1")
    if rows:
        return rows[0]["started_at"]
    fallback = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    return fallback.isoformat()


def record_run(source, status, fetched, inserted, error=None):
    sb("ingest_runs", method="POST", body={
        "source": source,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": status,
        "records_fetched": fetched,
        "records_inserted": inserted,
        "error": (error or "")[:2000] or None,
    })


def upsert_permits(rows):
    if not rows:
        return 0
    # A feed can repeat the same permit_number (multi-row permits); Postgres
    # rejects intra-batch duplicates on upsert ("cannot affect row a second
    # time"), so dedupe first, keeping the last occurrence.
    deduped = {}
    for row in rows:
        deduped[(row["jurisdiction"], row["permit_number"])] = row
    rows = list(deduped.values())
    # Supabase REST upsert in chunks of 200
    inserted = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        sb("permits", method="POST", body=chunk,
           params="?on_conflict=jurisdiction,permit_number",
           prefer="resolution=merge-duplicates")
        inserted += len(chunk)
    return inserted


def main():
    failures = []
    for name, base in SOURCES.items():
        # One flaky feed must not block the others: watermark and runs are per-source.
        watermark = get_watermark(name)
        backfill_days = int(os.environ.get("BACKFILL_DAYS", "0") or 0)
        if backfill_days > 0:
            watermark = (dt.datetime.now(dt.timezone.utc)
                         - dt.timedelta(days=backfill_days)).isoformat()
            log(f"[{name}] backfill override: {backfill_days} days")
        wm_date = watermark[:10]
        # Trail the watermark (see LAG_BUFFER_DAYS): the feeds publish
        # records days after the record's own date.
        query_date = (dt.date.fromisoformat(wm_date)
                      - dt.timedelta(days=LAG_BUFFER_DAYS)).isoformat()
        log(f"[{name}] watermark: {watermark} (query {DATE_FIELDS[name]} > {query_date})")
        rows, fetched = [], 0
        try:
            date_field = DATE_FIELDS[name]
            where = f"{date_field} > DATE '{query_date}'"
            norm = NORMALIZERS[name]
            for attrs in arcgis_query(base, where, OUT_FIELDS[name], date_field):
                row = norm(attrs)
                if not row["permit_number"]:
                    continue
                rows.append(row)
                fetched += 1
            log(f"[{name}] {fetched} new records since {wm_date}")
            inserted = upsert_permits(rows)
            record_run(name, "ok", fetched, inserted)
            log(f"[{name}] done: fetched={fetched} upserted={inserted}")
        except Exception as e:
            log(f"[{name}] ERROR: {e}")
            try:
                record_run(name, "error", fetched, 0, error=str(e))
            except Exception:
                pass
            failures.append(name)
    if failures:
        # A failed source must not kill the whole job: its watermark only
        # advances on success, so it retries cleanly on the next run, and
        # the export/deploy steps still run for the sources that succeeded.
        log(f"WARNING: failed sources (will retry next run): {failures}")
    log("done")


if __name__ == "__main__":
    main()
