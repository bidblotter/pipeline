#!/usr/bin/env python3
"""Build a scannable morning digest for one trade from web/data/permits.json.

Two sections:
  NEW SINCE YESTERDAY — permits first seen by PermitPicker in the last 24h
    (discovery date, not filing date: the county feed lags ~3 days, so
    "filed yesterday" would usually be empty).
  THIS WEEK — permits filed in the last --days (default 7), the running look.

Usage:
  python pipeline/digest.py --trade electrical --days 7 --out /tmp/digest.txt

Writes the digest to --out and prints a "SUBJECT: ..." line for the mail step.
"""
import argparse
import datetime as dt
import json
import os

SITE = "https://permitpicker.com/"
LIST_CAP = 25
# The digest product went live 2026-10-04; rows first seen before that are the
# initial backfill, not "new". (Without this guard the backfill batch would
# flood the NEW section once.)
LIVE_DATE = "2026-10-05T04:00:00+00:00"  # raised 2026-10-05: Cary ingest
# backfill (~3k rows) + Wake 90-day re-backfill (~5.6k recovered rows) all
# landed with first_seen_at ~= 2026-10-05 02:xx UTC; without this they would
# flood NEW SINCE YESTERDAY as ~8.7k "new" permits. Genuinely new filings
# still appear under THIS WEEK.


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
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data_path = os.path.join(os.path.dirname(__file__), "..", "web",
                             "data", "permits.json")
    with open(data_path) as f:
        permits = json.load(f)["permits"]

    now = dt.datetime.now(dt.timezone.utc)
    day_ago = (now - dt.timedelta(hours=24)).isoformat()
    week_ago = (now - dt.timedelta(days=args.days)).isoformat()
    kw = args.trade.lower()
    trade = args.trade.title()

    def matches(p):
        return (kw in (p.get("permit_type") or "").lower())

    new_rows = sorted(
        [p for p in permits
         if matches(p)
         and (p.get("first_seen_at") or "") >= day_ago
         and (p.get("first_seen_at") or "") >= LIVE_DATE],
        key=lambda p: p.get("first_seen_at") or "", reverse=True)
    week_rows = sorted(
        [p for p in permits
         if matches(p)
         and (p.get("applied_date") or "") >= week_ago
         and p not in new_rows],
        key=lambda p: p.get("applied_date") or "", reverse=True)

    L = []
    L.append(f"{trade} permits — Raleigh-Durham")
    L.append(f"{len(new_rows)} new since yesterday · "
             f"{len(new_rows) + len(week_rows)} filed in the last "
             f"{args.days} days")
    L.append("")
    if new_rows:
        L.append(f"NEW SINCE YESTERDAY ({len(new_rows)}):")
        L.append("")
        for p in new_rows[:LIST_CAP]:
            L.append(entry(p))
            L.append("")
    else:
        L.append("No new filings published since yesterday.")
        L.append("")
    if week_rows:
        L.append(f"THIS WEEK ({len(week_rows)} more):")
        L.append("")
        shown = week_rows[:max(0, LIST_CAP - len(new_rows))]
        for p in shown:
            L.append(entry(p))
            L.append("")
        rest = len(week_rows) - len(shown)
        if rest > 0:
            L.append(f"  ...and {rest} more. Browse them all:")
            L.append(f"  {SITE}?q={kw}&days={args.days}")
            L.append("")
    L.append("---")
    L.append("PermitPicker — Raleigh-Durham permit intelligence. "
             "Public records, refreshed daily.")
    L.append("You're receiving this as a PermitPicker pilot subscriber.")
    text = "\n".join(L).rstrip() + "\n"

    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    n, w = len(new_rows), len(week_rows)
    if n:
        subject = f"{n} new {kw} permit{'s' if n != 1 else ''} — PermitPicker"
    else:
        subject = (f"{w} {kw} permits this week — PermitPicker"
                   if w else f"No new {kw} permits — PermitPicker")
    print(f"SUBJECT: {subject}")


if __name__ == "__main__":
    main()
