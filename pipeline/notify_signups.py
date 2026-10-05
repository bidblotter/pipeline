#!/usr/bin/env python3
"""Email Hartland when new waitlist signups arrived since the last check.

Queries the Supabase `waitlist` table for rows created in the trailing
--hours window (default 30, to cover small schedule drift) and writes a
plain-text summary to --out. Writes an empty file and exits 0 when there
is nothing new or the query fails — the workflow only sends mail when the
output file is non-empty, so a failure here never breaks the run.

Env (GitHub Actions secrets): SUPABASE_URL, SUPABASE_SERVICE_KEY

Usage:
  python pipeline/notify_signups.py --out /tmp/signups.txt [--hours 30]
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]


def sb(path, params=""):
    url = f"{SUPABASE_URL}/rest/v1/{path}{params}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--hours", type=float, default=30)
    args = ap.parse_args()
    try:
        since = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(hours=args.hours)).isoformat()
        q = urllib.parse.urlencode({
            "select": "email,trade,created_at",
            "created_at": f"gt.{since}",
            "order": "created_at.asc",
            "limit": "100",
        })
        rows = sb("waitlist", params="?" + q)
    except Exception as e:  # never break the workflow over a notification
        print(f"notify_signups: query failed: {e}", file=sys.stderr, flush=True)
        open(args.out, "w").write("")
        return
    if not rows:
        open(args.out, "w").write("")
        return
    lines = [f"New PermitPicker waitlist signups ({len(rows)}):", ""]
    for r in rows:
        ts = (r.get("created_at") or "")[:16].replace("T", " ")
        lines.append(f"- {r.get('email')} — {r.get('trade') or 'no trade'} ({ts} UTC)")
    lines += ["", "Full list lives in the Supabase waitlist table."]
    open(args.out, "w").write("\n".join(lines) + "\n")
    print(f"notify_signups: {len(rows)} new signups", flush=True)


if __name__ == "__main__":
    main()
