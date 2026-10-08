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


PHONE_SVG = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
             'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
             'stroke-linejoin="round" style="vertical-align:-2px">'
             '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 '
             '19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 '
             '2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 '
             '16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 '
             '2 0 0 1 22 16.92z"/></svg>')


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


VCARD_BASE = "https://permitpicker.com/data/vcards/"


def vcard_url(key):
    if not key:
        return None
    return VCARD_BASE + urllib.parse.quote(key, safe="") + ".vcf"


def badge_pills(badges):
    """Render context badges as pills; the surging-mover pill is prominent."""
    pills = []
    for b in badges or []:
        if "vs " in b and "usual" in b:
            pills.append(
                '<span style="display:inline-block;background:#0d7a6f;color:#ffffff;'
                'font-weight:bold;font-size:12.5px;padding:5px 12px;border-radius:20px;'
                'margin:2px 6px 2px 0;">&#9650; ' + esc(b) + '</span>')
        else:
            pills.append(
                '<span style="display:inline-block;background:#d9efec;color:#0b5f56;'
                'font-weight:bold;font-size:12.5px;padding:5px 12px;border-radius:20px;'
                'margin:2px 6px 2px 0;">' + esc(b) + '</span>')
    return ('<div style="margin-top:9px;">' + "".join(pills) + '</div>' if pills else "")


def permit_card(p, badges=None, key=""):
    co = clean(p.get("contractor"))
    addr = clean(p.get("address"))
    city = clean((p.get("city") or "").title())
    loc = ", ".join(x for x in [addr, city] if x)
    desc = clean(p.get("description") or p.get("permit_type") or "")
    if len(desc) > 90:
        desc = desc[:90].rsplit(" ", 1)[0] + "\u2026"
    val = money(p.get("valuation"))
    filed = (p.get("applied_date") or "")[:10]
    phone = norm_phone(p.get("contractor_phone"))
    tel = tel_link(p.get("contractor_phone"))
    maps_q = urllib.parse.quote(loc) if loc else ""
    call_btn = (f'<a href="{tel}" style="display:inline-block;background:#0d7a6f;color:#ffffff !important;'
                f'text-decoration:none;font-weight:bold;padding:10px 18px;border-radius:8px;font-size:15px;">'
                f'{PHONE_SVG} {esc(phone)}</a>' if tel and phone != "\u2014" else "")
    # Parked 2026-10-07 (Hartland): "+ Add to contacts" button hidden until
    # contact info is richer. vCards still build daily in the workflow.
    vcard_btn = ""
    maps_html = (f' &nbsp;<a href="https://www.google.com/maps/search/?api=1&query={maps_q}" '
                 f'style="color:#0d7a6f;font-size:12.5px;">map</a>' if maps_q else "")
    meta_bits = [f"Filed {esc(filed)}" if filed else "", esc(val) if val else ""]
    meta = " &nbsp;\u00b7&nbsp; ".join(b for b in meta_bits if b)
    name = (display_for(contractor_key(co), [co])[:40] if co and is_contractor(co)
            else (co or "No contractor listed"))
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px;background:#ffffff;'
            f'border:1px solid #dde7e7;border-radius:10px;"><tr><td style="padding:14px 16px;">'
            f'<div style="font-size:16px;font-weight:bold;color:#102e36;">{esc(name)}</div>'
            f'<div style="font-size:13.5px;color:#3d4d54;margin-top:4px;">{esc(loc)}{maps_html}'
            f'{" &nbsp;\u00b7&nbsp; " + meta if meta else ""}</div>'
            f'<div style="font-size:13.5px;color:#5b6b73;margin-top:4px;">{esc(desc)}</div>'
            f'{badge_pills(badges)}'
            f'<div style="margin-top:10px;">{call_btn}{vcard_btn}</div>'
            f'</td></tr></table>')


def build_html(brand, trade, date_label, sections, n_new, n_week, site_url,
               new_label="new since yesterday", n_records=0):
    """sections: list of (title, [(permit, contractor_key, badges)])."""
    parts = []
    for title, items in sections:
        if not items:
            continue
        cards = "".join(permit_card(p, badges, k) for p, k, badges in items)
        parts.append(
            f'<div style="font-size:17px;font-weight:bold;color:#102e36;'
            f'margin:20px 0 10px;">{esc(title)}</div>{cards}')
    if not parts:
        parts.append('<p style="color:#5b6b73;">No new filings since yesterday.</p>')
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(brand)} — {esc(trade)} permits</title></head>'
            f'<body style="margin:0;padding:0;background:#f2f6f6;">'
            f'<div style="max-width:600px;margin:0 auto;padding:20px 12px;">'
            f'<div style="background:#102e36;border-radius:12px;padding:22px 20px;margin-bottom:6px;">'
            f'<div style="font-size:24px;font-weight:bold;color:#ffffff;">Permit<span style="color:#2dd4bf;">Pulse</span></div>'
            f'<div style="font-size:13px;color:#b9cdc9;margin-top:4px;">{esc(trade)} permits — Raleigh &middot; {esc(date_label)}</div>'
            f'</div>'
            f'<div style="background:#ffffff;border:1px solid #dde7e7;border-radius:12px;padding:16px 18px;margin:12px 0;">'
            f'<div style="font-size:15px;color:#102e36;"><b>{n_records}</b> new to our records &nbsp;\u00b7&nbsp; '
            f'<b>{n_new}</b> {esc(new_label.lower())} &nbsp;\u00b7&nbsp; '
            f'<b>{n_week}</b> filed in the last 7 days</div></div>'
            f'{"".join(parts)}'
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
    # No sends on weekends, so Monday's digest looks back to Friday's send.
    from zoneinfo import ZoneInfo
    is_monday = now.astimezone(ZoneInfo("America/New_York")).weekday() == 0
    lookback_h = 72 if is_monday else 24
    new_label = "New since Friday" if is_monday else "New today"
    day_ago = (now - dt.timedelta(hours=lookback_h)).isoformat()
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
    L.append(f"{len(new_rows)} {new_label.lower()} · "
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
        # permits grouped into sections (each newest first)
        key_of = lambda p: (contractor_key((p.get("contractor") or "").strip())
                            if (p.get("contractor") or "").strip()
                            and is_contractor((p.get("contractor") or "").strip()) else "")
        new_keys = {k for k, v in info.items() if v["is_new"]}
        seen_p = set()
        sec_new, sec_today, sec_week = [], [], []
        for p in sorted(new_rows + week_rows,
                        key=lambda r: r.get("applied_date") or "", reverse=True):
            pn = p.get("permit_number")
            if pn in seen_p:
                continue
            seen_p.add(pn)
            k = key_of(p)
            badges = [b for b in info.get(k, {}).get("badges", [])]
            if k in new_keys:
                badges = [b for b in badges if b != "First seen in our records"]
                sec_new.append((p, k, badges))
            elif p in new_rows:
                sec_today.append((p, k, badges))
            else:
                sec_week.append((p, k, badges))
        sections = [("New to our records", sec_new),
                    (new_label, sec_today),
                    ("Earlier this week", sec_week)]
        date_label = now.strftime("%b %-d, %Y")
        html_doc = build_html(args.brand, trade, date_label, sections,
                              n, n + w, SITE, new_label.lower(),
                              n_records=len(sec_new))
        with open(args.html_out, "w") as f:
            f.write(html_doc)
        print(f"HTML call sheet: {args.html_out} "
              f"({len(sec_new)} new to records, {len(sec_today)} new today, "
              f"{len(sec_week)} earlier this week)")

    if n:
        subject = f"{n} new {kw} permit{'s' if n != 1 else ''} — {args.brand}"
    else:
        subject = (f"{w} {kw} permits this week — {args.brand}"
                   if w else f"No new {kw} permits — {args.brand}")
    print(f"SUBJECT: {subject}")


if __name__ == "__main__":
    main()
