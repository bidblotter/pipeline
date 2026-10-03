#!/usr/bin/env python3
"""Export permits to web/data/permits.json for the GitHub Pages preview.

Reads from Supabase (service key) and writes a trimmed, public-safe JSON
snapshot. Run after ingest in the workflow.
"""
import datetime as dt
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from ingest import sb, log  # noqa: E402

FIELDS = ("permit_number,permit_type,work_class,description,status,"
          "applied_date,issued_date,address,city,valuation,contractor,"
          "contractor_phone,contractor_license,source_feed")


def main():
    days = int(os.environ.get("EXPORT_DAYS", "90") or 90)
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=days)).isoformat()
    rows, offset, page = [], 0, 1000
    since_q = urllib.parse.quote(since, safe="")
    while True:
        params = (f"?select={FIELDS}&or=(applied_date.gte.{since_q},issued_date.gte.{since_q})"
                  f"&order=applied_date.desc.nullslast&limit={page}&offset={offset}")
        chunk = sb("permits", params=params)
        if not chunk:
            break
        rows.extend(chunk)
        log(f"exported {len(rows)} rows so far")
        if len(chunk) < page:
            break
        offset += page
    out_dir = os.path.join(os.path.dirname(__file__), "..", "web", "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "permits.json")
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "count": len(rows),
        "permits": rows,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f)
    log(f"wrote {out_path} ({len(rows)} permits)")


if __name__ == "__main__":
    main()
