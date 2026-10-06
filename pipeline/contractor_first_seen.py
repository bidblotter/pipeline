#!/usr/bin/env python3
"""Build a contractor first-seen lookup from FULL database history.

The public permits.json only covers 90 days, so "new to our records"
computed from it can't tell a truly new contractor from one who was
active 4+ months ago and went quiet. This queries the entire permits
table (all history) and writes web/data/first_seen.json:
  {contractor_key: earliest applied_date}
The report generator uses it for a more conclusive first-seen check.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from ingest import sb, log  # noqa: E402
from normalize import contractor_key, is_contractor  # noqa: E402


def main():
    first = {}
    offset, page = 0, 1000
    total = 0
    while True:
        chunk = sb("permits",
                   params=f"?select=contractor,applied_date&limit={page}&offset={offset}")
        if not chunk:
            break
        for r in chunk:
            c = (r.get("contractor") or "").strip()
            if not c or not is_contractor(c):
                continue
            key = contractor_key(c)
            ad = (r.get("applied_date") or "")[:10]
            if not ad:
                continue
            if key not in first or ad < first[key]:
                first[key] = ad
        total += len(chunk)
        offset += page
        if len(chunk) < page:
            break
    log(f"scanned {total} permit rows -> {len(first)} contractors")
    out_dir = os.path.join(os.path.dirname(__file__), "..", "web", "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "first_seen.json")
    with open(out_path, "w") as f:
        json.dump(first, f)
    dates = sorted(first.values())
    log(f"wrote {out_path} (history from {dates[0] if dates else '?'})")


if __name__ == "__main__":
    main()
