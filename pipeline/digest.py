#!/usr/bin/env python3
"""Build a plain-text morning digest for one trade from web/data/permits.json.

Usage:
  python pipeline/digest.py --trade electrical --days 7 --out /tmp/digest.txt

Matches the trade keyword (case-insensitive) against permit_type and keeps
permits with applied_date inside the window. Prints to stdout and,
with --out, writes the file.
"""
import argparse
import datetime as dt
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data_path = os.path.join(os.path.dirname(__file__), "..", "web",
                             "data", "permits.json")
    with open(data_path) as f:
        permits = json.load(f)["permits"]

    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=args.days)).isoformat()
    kw = args.trade.lower()
    rows = [p for p in permits
            if kw in (p.get("permit_type") or "").lower()
            and (p.get("applied_date") or "") >= cutoff]
    rows.sort(key=lambda p: p.get("applied_date") or "", reverse=True)
    open_rows = [p for p in rows if not (p.get("contractor") or "").strip()]

    L = []
    L.append("BIDBLOTTER — MORNING DIGEST")
    L.append(f"Trade: {args.trade.title()} | Raleigh-Durham | "
             f"Last {args.days} days")
    L.append(f"{len(rows)} new permits filed. "
             f"{len(open_rows)} have no contractor listed yet.")
    L.append("")
    if open_rows:
        L.append("OPEN OPPORTUNITIES (no contractor assigned):")
        for p in open_rows[:10]:
            desc = (p.get("description") or p.get("permit_type") or "")[:70]
            L.append(f"  {p['applied_date'][:10]} | {p.get('address', '')}, "
                     f"{p.get('city', '')} | {desc}")
        if len(open_rows) > 10:
            L.append(f"  ... and {len(open_rows) - 10} more without a contractor")
        L.append("")
    L.append("ALL FILINGS:")
    for p in rows:
        desc = (p.get("description") or p.get("permit_type") or "")[:60]
        co = (p.get("contractor") or "no contractor listed").strip()[:40]
        L.append(f"  {p['applied_date'][:10]} | {p.get('address', '')}, "
                 f"{p.get('city', '')} | {desc} | {co}")
    L.append("")
    L.append("---")
    L.append("BidBlotter — Raleigh-Durham permit intelligence. "
             "Public records, refreshed daily.")
    text = "\n".join(L)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    print(f"digest: {len(rows)} permits, {len(open_rows)} open "
          f"({len(text)} chars)")


if __name__ == "__main__":
    main()
