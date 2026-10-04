#!/usr/bin/env python3
"""Build a scannable morning digest for one trade from web/data/permits.json.

Usage:
  python pipeline/digest.py --trade electrical --days 1 --out /tmp/digest.txt

Matches the trade keyword (case-insensitive) against permit_type and keeps
permits with applied_date inside the window. Writes the digest to --out and
prints a "SUBJECT: ..." line for the mail step to use.
"""
import argparse
import datetime as dt
import json
import os

SITE = "https://bidblotter.github.io/pipeline/"
LIST_CAP = 25


def money(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    return f"${n:,.0f}"


def fdate(s):
    try:
        return dt.datetime.fromisoformat(s).strftime("%b %-d")
    except (TypeError, ValueError):
        return s[:10] if s else "?"


def entry(p):
    """One compact permit block."""
    lines = []
    addr = f"{p.get('address', '')}, {p.get('city', '')}".strip(" ,")
    lines.append(f"  {addr}")
    desc = (p.get("description") or p.get("permit_type") or "").strip()
    val = money(p.get("valuation"))
    meta = f"Filed {fdate(p.get('applied_date'))}"
    if val:
        meta += f" · {val}"
    co = (p.get("contractor") or "").strip()
    meta += f" · {co}" if co else " · no contractor listed"
    lines.append(f"    {desc[:80]}")
    lines.append(f"    {meta}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade", required=True)
    ap.add_argument("--days", type=int, default=1)
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

    trade = args.trade.title()
    day_word = "today" if args.days == 1 else f"in the last {args.days} days"
    L = []
    L.append(f"{len(rows)} new {trade.lower()} permits {day_word} "
             f"(Raleigh-Durham)")
    L.append("")
    if open_rows:
        L.append(f"OPEN OPPORTUNITIES — {len(open_rows)} with no contractor "
                 f"listed yet:")
        L.append("")
        for p in open_rows[:10]:
            L.append(entry(p))
            L.append("")
        if len(open_rows) > 10:
            L.append(f"  ...and {len(open_rows) - 10} more open opportunities.")
            L.append("")
    shown = rows[:LIST_CAP]
    L.append(f"ALL {trade.upper()} FILINGS ({len(rows)}):")
    L.append("")
    for p in shown:
        L.append(entry(p))
        L.append("")
    if len(rows) > LIST_CAP:
        L.append(f"  ...and {len(rows) - LIST_CAP} more. Browse them all:")
        L.append(f"  {SITE}")
        L.append("")
    L.append("---")
    L.append("BidBlotter — Raleigh-Durham permit intelligence. "
             "Public records, refreshed daily.")
    L.append("You're receiving this as a BidBlotter pilot subscriber.")
    text = "\n".join(L).rstrip() + "\n"

    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    n = len(rows)
    plural = "" if n == 1 else "s"
    print(f"SUBJECT: {n} new {trade.lower()} permit{plural} {day_word} "
          f"— BidBlotter")


if __name__ == "__main__":
    main()
