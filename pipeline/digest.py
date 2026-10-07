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
import html as htmlmod
import json
import os
import re
import sys
import urllib.parse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import contractor_key, display_for, is_contractor

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


def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return (p or "").strip()


def tel_link(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"tel:+1{d}"
    return None


def esc(t):
    return htmlmod.escape(t or "")


def clean(t):
    return re.sub(r"\s+", " ", t or "").strip()


def load_first_seen(data_dir):
    """contractor_key -> earliest applied_date, from the full-history lookup
    built daily by contractor_first_seen.py. Falls back to {} (caller then
    derives first-seen from the 90-day export window)."""
    try:
        with open(os.path.join(data_dir, "first_seen.json")) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def rank_contractors(trade_rows, week_rows_all, first_seen, wk_start, prev_start):
    """Split this week's trade contractors into ranked buckets.

    Returns (call_first_keys, info) where info[key] = dict with display name,
    phone, counts, is_new, is_hot, badges.
    """
    def key_of(p):
        c = (p.get("contractor") or "").strip()
        return contractor_key(c) if c and is_contractor(c) else ""

    # week counts per contractor (this week) and prior 3-week baseline
    cw = Counter(key_of(p) for p in trade_rows if key_of(p))
    cp = Counter(key_of(p) for p in week_rows_all if key_of(p))
    names = defaultdict(list)
    phones = {}
    for p in trade_rows + week_rows_all:
        c = (p.get("contractor") or "").strip()
        if c and is_contractor(c):
            k = contractor_key(c)
            names[k].append(c)
            ph = norm_phone(p.get("contractor_phone"))
            if ph and ph != "\u2014" and k not in phones:
                phones[k] = ph

    # fallback first-seen from the 90-day window when the lookup is missing
    if not first_seen:
        for p in sorted(week_rows_all + trade_rows,
                        key=lambda r: r.get("applied_date") or ""):
            k = key_of(p)
            if k and k not in first_seen:
                first_seen[k] = (p.get("applied_date") or "")[:10]

    info = {}
    call_first = []
    for k, n in cw.most_common():
        base = cp.get(k, 0) / 3.0
        is_new = (first_seen.get(k, "") or "") >= wk_start
        is_hot = n >= 3 and n > base * 1.5
        badges = []
        if is_new:
            badges.append("First seen in our records")
        if is_hot:
            badges.append(f"{n} permits this week (vs {base:.1f}/wk usual)")
        elif n >= 2:
            badges.append(f"{n} permits this week")
        info[k] = {
            "name": display_for(k, names[k])[:40],
            "phone": phones.get(k, ""),
            "n": n,
            "is_new": is_new,
            "is_hot": is_hot,
            "badges": badges,
        }
        if is_new or is_hot:
            call_first.append(k)
    # call-first ordered: new first, then hottest
    call_first.sort(key=lambda k: (not info[k]["is_new"], -(info[k]["n"])))
    return call_first, info


def permit_card(p, badge=None):
    co = clean(p.get("contractor"))
    addr = clean(p.get("address"))
    city = clean((p.get("city") or "").title())
    loc = ", ".join(x for x in [addr, city] if x)
    desc = clean(p.get("description") or p.get("permit_type") or "")
    if len(desc) > 90:
        desc = desc[:90].rsplit(" ", 1)[0] + "\u2026"
    val = money(p.get("valuation"))
    phone = norm_phone(p.get("contractor_phone"))
    tel = tel_link(p.get("contractor_phone"))
    maps_q = urllib.parse.quote(loc) if loc else ""
    phone_html = (f'<a href="{tel}" style="display:inline-block;background:#0d7a6f;color:#ffffff !important;'
                  f'text-decoration:none;font-weight:bold;padding:10px 18px;border-radius:8px;font-size:15px;">'
                  f'\u260e {esc(phone)}</a>' if tel and phone != "\u2014"
                else (f'<span style="color:#5b6b73;">{esc(phone)}</span>' if phone and phone != "\u2014" else ""))
    badge_html = (f'<div style="margin-top:6px;font-size:12.5px;color:#0d6e64;">'
                  f'{" &nbsp;\u2022&nbsp; ".join(esc(b) for b in badge)}</div>' if badge else "")
    maps_html = (f' &nbsp;<a href="https://www.google.com/maps/search/?api=1&query={maps_q}" '
                 f'style="color:#0d7a6f;font-size:12.5px;">map</a>' if maps_q else "")
    val_html = f" &nbsp;\u00b7&nbsp; {esc(val)}" if val else ""
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px;background:#ffffff;'
            f'border:1px solid #dde7e7;border-radius:10px;"><tr><td style="padding:14px 16px;">'
            f'<div style="font-size:16px;font-weight:bold;color:#102e36;">{esc(display_for(contractor_key(co), [co])[:40] if co and is_contractor(co) else (co or "No contractor listed"))}</div>'
            f'<div style="font-size:13.5px;color:#3d4d54;margin-top:4px;">{esc(loc)}{maps_html}{val_html}</div>'
            f'<div style="font-size:13.5px;color:#5b6b73;margin-top:4px;">{esc(desc)}</div>'
            f'{badge_html}'
            f'<div style="margin-top:10px;">{phone_html}</div>'
            f'</td></tr></table>')


def build_html(brand, trade, date_label, call_first_permits, info, new_rest, week_rest,
               n_new, n_week, site_url):
    sec_title = ('font-size:13px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;'
                 'color:#0d7a6f;margin:22px 0 10px;')
    body_cards = []
    if call_first_permits:
        body_cards.append(f'<div style="{sec_title}">\u2605 Call first — {len(call_first_permits)}</div>')
        for p, k in call_first_permits:
            body_cards.append(permit_card(p, info[k]["badges"]))
    if new_rest:
        body_cards.append(f'<div style="{sec_title}">New since yesterday</div>')
        for p in new_rest:
            body_cards.append(permit_card(p))
    if week_rest:
        body_cards.append(f'<div style="{sec_title}">Earlier this week</div>')
        for p in week_rest:
            body_cards.append(permit_card(p))
    if not (call_first_permits or new_rest or week_rest):
        body_cards.append('<p style="color:#5b6b73;">No new filings since yesterday.</p>')
    return (f'<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f2f6f6;">'
            f'<div style="max-width:600px;margin:0 auto;padding:20px 12px;">'
            f'<div style="background:#102e36;border-radius:12px;padding:22px 20px;margin-bottom:6px;">'
            f'<div style="font-size:24px;font-weight:bold;color:#ffffff;">Permit<span style="color:#2dd4bf;">Pulse</span></div>'
            f'<div style="font-size:13px;color:#b9cdc9;margin-top:4px;">{esc(trade)} permits — Raleigh &middot; {esc(date_label)}</div>'
            f'</div>'
            f'<div style="background:#ffffff;border:1px solid #dde7e7;border-radius:12px;padding:16px 18px;margin:12px 0;">'
            f'<div style="font-size:15px;color:#102e36;"><b>{n_new}</b> new since yesterday &nbsp;\u00b7&nbsp; '
            f'<b>{n_week}</b> filed in the last 7 days</div></div>'
            f'{"".join(body_cards)}'
            f'<div style="margin-top:24px;padding-top:14px;border-top:1px solid #dde7e7;font-size:12px;color:#8a9aa1;">'
            f'{esc(brand)} — Raleigh permit intelligence. Public records, refreshed daily.<br>'
            f'You\u2019re receiving this as a {esc(brand)} subscriber. '
            f'<a href="{esc(site_url)}" style="color:#0d7a6f;">Browse all permits</a></div>'
            f'</div></body></html>')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=None)
    ap.add_argument("--brand", default="PermitPicker",
                    help="Brand name used in subject/footer (PermitPicker or PermitPulse)")
    ap.add_argument("--html-out", default=None,
                    help="Also write the PermitPulse HTML call-sheet version to this path")
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
    L.append(f"{trade} permits — Raleigh")
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
    L.append(f"{args.brand} — Raleigh permit intelligence. "
             "Public records, refreshed daily.")
    if args.brand == "PermitPicker":
        L.append("You're receiving this as a PermitPicker pilot subscriber.")
    else:
        L.append(f"You're receiving this as a {args.brand} subscriber.")
    text = "\n".join(L).rstrip() + "\n"

    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    n, w = len(new_rows), len(week_rows)

    if args.html_out:
        data_dir = os.path.join(os.path.dirname(__file__), "..", "web", "data")
        first_seen = load_first_seen(data_dir)
        today = now.date()
        wk_start_s = (today - dt.timedelta(days=7)).isoformat()
        prev_start_s = (today - dt.timedelta(days=28)).isoformat()
        trade_week = [x for x in permits
                      if matches(x) and (x.get("applied_date") or "") >= wk_start_s]
        trade_prev = [x for x in permits
                      if matches(x) and prev_start_s <= (x.get("applied_date") or "") < wk_start_s]
        call_keys, info = rank_contractors(trade_week, trade_prev, first_seen,
                                           wk_start_s, prev_start_s)
        # permits grouped for the call-first section (newest first)
        key_of = lambda p: (contractor_key((p.get("contractor") or "").strip())
                            if (p.get("contractor") or "").strip()
                            and is_contractor((p.get("contractor") or "").strip()) else "")
        call_permits, seen_p = [], set()
        for p in new_rows + week_rows:
            k = key_of(p)
            if k in call_keys and p.get("permit_number") not in seen_p:
                call_permits.append((p, k))
                seen_p.add(p.get("permit_number"))
        rest_new = [p for p in new_rows if p.get("permit_number") not in seen_p]
        rest_week = [p for p in week_rows if p.get("permit_number") not in seen_p]
        date_label = now.strftime("%b %-d, %Y")
        html_doc = build_html(args.brand, trade, date_label, call_permits, info,
                              rest_new, rest_week, n, n + w, SITE)
        with open(args.html_out, "w") as f:
            f.write(html_doc)
        print(f"HTML call sheet: {args.html_out} "
              f"({len(call_permits)} call-first, {len(rest_new)+len(rest_week)} more)")

    if n:
        subject = f"{n} new {kw} permit{'s' if n != 1 else ''} — {args.brand}"
    else:
        subject = (f"{w} {kw} permits this week — {args.brand}"
                   if w else f"No new {kw} permits — {args.brand}")
    print(f"SUBJECT: {subject}")


if __name__ == "__main__":
    main()
