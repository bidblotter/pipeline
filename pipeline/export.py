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
from normalize import contractor_key, clean_name, is_contractor  # noqa: E402

FIELDS = ("permit_number,permit_type,work_class,description,status,"
          "applied_date,issued_date,address,city,valuation,contractor,"
          "contractor_phone,contractor_license,source_feed,first_seen_at")


def durham_applied_year(permit_number):
    """Durham permit numbers are YY-prefixed (validated 2026-10-04: the
    2-digit prefix matches the application year on ~97% of permits with
    known issue dates; the rest applied in Dec and issued the next Jan).
    Gives every Durham row an honest year-precision applied date."""
    pn = (permit_number or "").strip()
    if len(pn) >= 2 and pn[:2].isdigit():
        yy = int(pn[:2])
        return 2000 + yy if yy <= 30 else 1900 + yy
    return None


def main():
    days = int(os.environ.get("EXPORT_DAYS", "90") or 90)
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=days)).isoformat()
    rows, offset, page = [], 0, 1000
    since_q = urllib.parse.quote(since, safe="")
    while True:
        # Wake rows are date-windowed. Durham is OFF for now (2026-10-04):
        # its active-permit snapshot needs the stale-permit retirement fix
        # before the data is trustworthy. Ingest still runs so the snapshot
        # history stays intact; flip DURHAM_ON to re-enable.
        DURHAM_ON = False
        or_clause = (f"(applied_date.gte.{since_q},issued_date.gte.{since_q}"
                     + (",source_feed.eq.durham_active" if DURHAM_ON else "") + ")")
        # Durham's active snapshot has no real dates (applied_date = first
        # seen), so date filtering alone can't exclude it — neq it outright.
        durham_filter = "" if DURHAM_ON else "&source_feed.neq.durham_active"
        params = (f"?select={FIELDS}&or={or_clause}{durham_filter}"
                  f"&order=applied_date.desc.nullslast,source_feed.asc,"
                  f"permit_number.asc&limit={page}&offset={offset}")
        chunk = sb("permits", params=params)
        if not chunk:
            break
        # TEMP DEBUG 2026-10-06: why is neq not filtering Durham?
        n_dur = sum(1 for r in chunk if r.get("source_feed") == "durham_active")
        log(f"DEBUG params={params[:200]} chunk={len(chunk)} durham_in_chunk={n_dur}")
        for r in chunk:
            if r.get("source_feed") == "durham_active":
                r["applied_year"] = durham_applied_year(r.get("permit_number"))
        rows.extend(chunk)
        log(f"exported {len(rows)} rows so far")
        if len(chunk) < page:
            break
        offset += page
    # Contractor normalization (2026-10-05): group by normalized key, display
    # the most common cleaned variant. Raw `contractor` kept for filtering.
    from collections import Counter, defaultdict
    key_names = defaultdict(list)
    for r in rows:
        c = (r.get("contractor") or "").strip()
        if is_contractor(c):
            key_names[contractor_key(c)].append(c)
    key_display = {}
    for k, names in key_names.items():
        c = Counter(clean_name(n) for n in names if clean_name(n))
        if c:
            key_display[k] = c.most_common(1)[0][0]
    for r in rows:
        c = (r.get("contractor") or "").strip()
        r["contractor_display"] = key_display.get(contractor_key(c), c) if is_contractor(c) else ""
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
