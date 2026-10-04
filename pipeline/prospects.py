#!/usr/bin/env python3
"""Rebuild web/prospects.csv from the permits table.

Ranks contractor companies by permit activity in the last 30 days, with
contact info (phone/email/license) taken from the DB — the public
permits.json excludes emails, so this must query Supabase directly.
Runs after export in the daily workflow.
"""
import csv
import datetime as dt
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from ingest import sb, log  # noqa: E402

DAYS = 30


def digits(s):
    d = re.sub(r"\D", "", s or "")
    return d


def main():
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=DAYS)).isoformat()
    fields = ("contractor,contractor_phone,contractor_email,"
              "contractor_license,permit_type")
    rows, offset, page = [], 0, 1000
    while True:
        params = (f"?select={fields}&or=(applied_date.gte.{since},"
                  f"first_seen_at.gte.{since})"
                  f"&limit={page}&offset={offset}")
        chunk = sb("permits", params=params)
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    log(f"scanned {len(rows)} permit rows for prospects")

    by_co = defaultdict(lambda: {"n": 0, "phones": Counter(),
                                 "emails": Counter(), "licenses": Counter(),
                                 "trades": set()})
    for r in rows:
        co = (r.get("contractor") or "").strip()
        if not co:
            continue
        e = by_co[co]
        e["n"] += 1
        ph = digits(r.get("contractor_phone"))
        if ph:
            e["phones"][ph] += 1
        em = (r.get("contractor_email") or "").strip()
        if em:
            e["emails"][em] += 1
        li = (r.get("contractor_license") or "").strip()
        if li:
            e["licenses"][li] += 1
        if r.get("permit_type"):
            e["trades"].add(r["permit_type"])

    ranked = sorted(by_co.items(), key=lambda kv: kv[1]["n"], reverse=True)
    out_path = os.path.join(os.path.dirname(__file__), "..", "web",
                            "prospects.csv")
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "permits_30d", "phone", "email",
                    "license", "trades"])
        for co, e in ranked:
            w.writerow([
                co,
                e["n"],
                e["phones"].most_common(1)[0][0] if e["phones"] else "",
                e["emails"].most_common(1)[0][0] if e["emails"] else "",
                e["licenses"].most_common(1)[0][0] if e["licenses"] else "",
                "; ".join(sorted(e["trades"])),
            ])
    log(f"wrote {out_path} ({len(ranked)} companies)")


if __name__ == "__main__":
    main()
