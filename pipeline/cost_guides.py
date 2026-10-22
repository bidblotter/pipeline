#!/usr/bin/env python3
"""Build PermitPicker homeowner cost-guide pages under web/costs/.

Reads the public export snapshot (web/data/permits.json, written by
export.py earlier in the ingest workflow) and emits static HTML pages:
a /costs/ index, one Triangle-wide ("Raleigh area") page per job
category, and one page per category x city cell with enough data.

Everything on the pages is an aggregate over permit records whose
`valuation` (the value the applicant declared on the permit application)
is > 0. Pages never show individual permits or street addresses.

Category definitions (v1)
--------------------------
Work-class categories match `work_class` exactly:
  deck       work_class == "Deck"
  addition   work_class == "Addition"
  pool       work_class == "Pool, Spa & Hot Tub"
  accessory  work_class == "Accessory Building"

Remodel categories are description slices (kitchen / bathroom). A record
qualifies when ALL of the following hold:
  * description (lowercased) contains the keyword ("kitchen" / "bath");
  * work_class is NOT one of the new-construction / commercial / hood
    classes in REMODEL_EXCLUDE_WC (this drops whole-house new builds,
    whose descriptions list every room in the house);
  * permit_type is not "Commercial Building";
  * description contains none of the new-construction marker phrases in
    NEW_CONSTRUCTION_MARKERS ("new single-family", "conditioned space",
    "lot ", "townhome", ... — new-home listings enumerate kitchens and
    baths, which would swamp the remodel signal);
  * status is not "Withdrawn" (a withdrawn permit never became a job).

ALL categories also exclude status == "Withdrawn" and require
valuation > 0. Kitchen and bathroom slices can overlap (a permit for a
combined kitchen-and-bath remodel appears in both) — each page is its
own slice of the same records.

A "roof" category is deliberately NOT built: keyword slices on
descriptions catch roof structures/framing on much larger jobs and
produce misleading numbers.

Geography: the region page covers the whole exported footprint (Wake
County / Raleigh / Cary permit records) and is labeled "Raleigh area",
matching the site's existing labels. City cells use the `city` field
normalized case-insensitively (feeds disagree: "RALEIGH" vs "Raleigh");
a city page is emitted only when the cell has >= CITY_MIN_N valued
permits in the category.

Statistics: median and the 25th/75th percentiles (linear interpolation,
the common spreadsheet/numpy default). Money is displayed in whole
dollars.

Contractor sections rank contractors by their count of permits in the
same category+cell set, using the pipeline's contractor normalization
(normalize.contractor_key / is_contractor). "Typical project size" is
the contractor's median declared value within the set.

The quote-request form posts to LEAD_ENDPOINT in the page script. It is
currently null: the Supabase project has no table that can hold a lead
(the `waitlist` table is email+trade only). sql/quote_requests.sql in
this repo creates the table; once it exists, set the endpoint in
render_form() and submissions go live with no other change.

Run after export.py in the ingest workflow. Deterministic: output
depends only on web/data/permits.json.
"""
import datetime as dt
import html
import json
import math
import os
import re
import shutil
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from normalize import contractor_key, display_for, is_contractor  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA_PATH = os.path.join(ROOT, "web", "data", "permits.json")
OUT_DIR = os.path.join(ROOT, "web", "costs")
SITEMAP_PATH = os.path.join(ROOT, "web", "sitemap.xml")
SITE = "https://permitpicker.com"
REGION_NAME = "Raleigh area"
REGION_SLUG = ""  # region pages live at /costs/<category>/
CITY_MIN_N = 25

SUPABASE_URL = "https://jxuarfprpsbokyxujdlj.supabase.co"

# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

REMODEL_EXCLUDE_WC = {
    "New One- and Two-Family Dwelling",  # whole-house new builds
    "New",
    "Commercial",                        # commercial jobs, not homeowners
    "Kitchen Hood and Grease Duct",      # fire/hood permits, not remodels
    "Alternative Fire Extinguishing Systems (Hoods)",
}
NEW_CONSTRUCTION_MARKERS = (
    "new single-family", "new single family", "new dwelling", "new home",
    "new residence", "new townhome", "new townhouse", "single-family home",
    "single family home", "townhome", "story single", "story townhome",
    "new construction", "new sfd", "adu", "sq ft conditioned",
    "conditioned space", "lot ", "new duplex",
)


def _valued(r):
    v = r.get("valuation")
    return isinstance(v, (int, float)) and v > 0


def _live(r):
    """A record counts as a job: valued and not withdrawn."""
    return _valued(r) and (r.get("status") or "") != "Withdrawn"


def _work_class(wc):
    return lambda r: _live(r) and (r.get("work_class") or "") == wc


def _remodel(keyword):
    def match(r):
        if not _live(r):
            return False
        desc = (r.get("description") or "").lower()
        if keyword not in desc:
            return False
        if (r.get("work_class") or "") in REMODEL_EXCLUDE_WC:
            return False
        if (r.get("permit_type") or "") == "Commercial Building":
            return False
        return not any(m in desc for m in NEW_CONSTRUCTION_MARKERS)
    return match


CATEGORIES = [
    {"id": "deck", "job": "deck", "label": "Deck",
     "match": _work_class("Deck")},
    {"id": "addition", "job": "home addition", "label": "Home addition",
     "match": _work_class("Addition")},
    {"id": "pool", "job": "pool", "label": "Pool, spa & hot tub",
     "match": _work_class("Pool, Spa & Hot Tub")},
    {"id": "accessory-building", "job": "accessory building",
     "label": "Accessory building (garage, shed, workshop)",
     "match": _work_class("Accessory Building")},
    {"id": "kitchen-remodel", "job": "kitchen remodel",
     "label": "Kitchen remodel", "match": _remodel("kitchen")},
    {"id": "bathroom-remodel", "job": "bathroom remodel",
     "label": "Bathroom remodel", "match": _remodel("bath")},
]

# ---------------------------------------------------------------------------
# City normalization (feeds disagree on casing: "RALEIGH" / "Raleigh" / "Cary")
# ---------------------------------------------------------------------------

CITY_DISPLAY = {
    "RALEIGH": "Raleigh", "CARY": "Cary", "APEX": "Apex",
    "KNIGHTDALE": "Knightdale", "WAKE FOREST": "Wake Forest",
    "WENDELL": "Wendell", "ZEBULON": "Zebulon", "ROLESVILLE": "Rolesville",
    "WILLOW SPRING": "Willow Spring", "FUQUAY VARINA": "Fuquay-Varina",
    "GARNER": "Garner", "HOLLY SPRINGS": "Holly Springs",
    "MORRISVILLE": "Morrisville", "NEW HILL": "New Hill",
    "CLAYTON": "Clayton", "YOUNGSVILLE": "Youngsville",
    "CREEDMOOR": "Creedmoor", "ANGIER": "Angier",
}


def city_display(raw):
    c = (raw or "").strip().upper()
    if not c:
        return None
    return CITY_DISPLAY.get(c, c.title())


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def percentile(sorted_vals, p):
    """Linear-interpolation percentile (numpy/spreadsheet default)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def money(v):
    return "$" + format(int(round(v)), ",")


def record_date(r):
    return (r.get("applied_date") or r.get("issued_date") or "")[:10]


def date_span(records):
    dates = sorted(d for d in (record_date(r) for r in records) if d)
    if not dates:
        return "", ""
    return dates[0], dates[-1]


def fmt_span(lo, hi):
    def f(d):
        try:
            return dt.date.fromisoformat(d).strftime("%b %Y")
        except ValueError:
            return d
    if not lo:
        return ""
    return f"{f(lo)} – {f(hi)}" if lo != hi else f(lo)


BANDS = [
    (0, 10_000, "Under $10K"), (10_000, 25_000, "$10–25K"),
    (25_000, 50_000, "$25–50K"), (50_000, 75_000, "$50–75K"),
    (75_000, 100_000, "$75–100K"), (100_000, 150_000, "$100–150K"),
    (150_000, 200_000, "$150–200K"), (200_000, 300_000, "$200–300K"),
    (300_000, 500_000, "$300–500K"), (500_000, float("inf"), "$500K+"),
]


def band_counts(vals):
    out = []
    for lo, hi, label in BANDS:
        n = sum(1 for v in vals if lo <= v < hi)
        if n:
            out.append((label, n))
    return out


# ---------------------------------------------------------------------------
# Contractors
# ---------------------------------------------------------------------------

def top_contractors(records, limit=8):
    """Rank contractors by permit count within this category+cell set."""
    groups = {}
    for r in records:
        raw = (r.get("contractor") or "").strip()
        if not is_contractor(raw):
            continue
        key = contractor_key(raw)
        if not key:
            continue
        g = groups.setdefault(key, {"names": [], "displays": [], "vals": [],
                                    "phones": Counter(), "latest": ""})
        g["names"].append(raw)
        if r.get("contractor_display"):
            g["displays"].append(r["contractor_display"])
        g["vals"].append(float(r["valuation"]))
        ph = (r.get("contractor_phone") or "").strip()
        if ph:
            g["phones"][ph] += 1
        d = record_date(r)
        if d > g["latest"]:
            g["latest"] = d
    out = []
    for key, g in groups.items():
        name = (Counter(g["displays"]).most_common(1)[0][0] if g["displays"]
                else display_for(key, g["names"]))
        if not name:
            continue
        phone = g["phones"].most_common(1)[0][0] if g["phones"] else ""
        vals = sorted(g["vals"])
        out.append({
            "name": name, "count": len(vals),
            "median": percentile(vals, 0.5), "phone": phone,
            "latest": g["latest"],
        })
    # Rank by permit count desc, then most recent activity desc.
    out.sort(key=lambda c: (c["count"], c["latest"]), reverse=True)
    return out[:limit]


def tel_href(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = "1" + digits
    return "tel:+" + digits if digits else ""


# ---------------------------------------------------------------------------
# Page rendering (visual style mirrors web/index.html, incl. dark mode)
# ---------------------------------------------------------------------------

CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f4f6f8; color: #1c2733; }
  header { background: #14324a; color: #fff; padding: 20px 16px; }
  header h1 { font-size: 22px; letter-spacing: .5px; }
  header h1 span { color: #ffb020; }
  header h1 a { color: inherit; text-decoration: none; }
  header p { opacity: .75; font-size: 13px; margin-top: 4px; }
  header p a { color: #ffb020; opacity: 1; }
  main { max-width: 880px; margin: 0 auto; padding: 18px 16px 40px; }
  .crumb { font-size: 13px; color: #667; margin-bottom: 10px; }
  .crumb a { color: #1463a0; }
  h1.page { font-size: 26px; line-height: 1.25; margin-bottom: 8px; }
  .lede { font-size: 15px; color: #445; margin-bottom: 16px; }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
  .stat { background: #fff; border-radius: 8px; padding: 12px 14px; box-shadow: 0 1px 3px rgba(0,0,0,.08); flex: 1; min-width: 150px; }
  .stat b { font-size: 22px; display: block; }
  .stat small { color: #667; }
  .card { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 16px; }
  .card h2 { font-size: 17px; margin-bottom: 10px; }
  .card p { font-size: 14px; line-height: 1.55; color: #33414f; margin-bottom: 8px; }
  .honesty { border-left: 4px solid #ffb020; }
  .band { display: flex; align-items: center; gap: 10px; font-size: 13px; margin: 5px 0; }
  .band .bl { width: 86px; flex-shrink: 0; color: #445; text-align: right; }
  .band .track { flex: 1; background: #e8eef4; border-radius: 4px; height: 16px; }
  .band .fill { background: #1463a0; height: 16px; border-radius: 4px; }
  .band .bn { width: 34px; color: #445; }
  table.co { width: 100%; border-collapse: collapse; font-size: 14px; }
  table.co td, table.co th { padding: 9px 6px; border-bottom: 1px solid #e3e9ef; text-align: left; vertical-align: top; }
  table.co th { font-size: 11px; text-transform: uppercase; letter-spacing: .7px; color: #889; }
  .call { display: inline-block; background: #0d7a6f; color: #fff; padding: 6px 12px; border-radius: 6px; text-decoration: none; font-weight: 700; font-size: 13px; white-space: nowrap; }
  .call:hover { background: #0b6a60; }
  .rank-note { font-size: 12.5px; color: #667; margin-top: 8px; }
  .quote { background: #102e36; color: #fff; border-radius: 8px; padding: 18px 16px; box-shadow: 0 1px 3px rgba(0,0,0,.12); margin-bottom: 16px; }
  .quote h2 { font-size: 18px; margin-bottom: 6px; }
  .quote p { font-size: 13.5px; opacity: .85; margin-bottom: 12px; line-height: 1.5; }
  .quote form { display: grid; gap: 8px; }
  .quote input, .quote select, .quote textarea { padding: 9px 10px; border: 1px solid #ccd; border-radius: 6px; font-size: 14px; font-family: inherit; width: 100%; }
  .quote textarea { min-height: 74px; }
  .quote .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  @media (max-width: 640px) { .quote .row2 { grid-template-columns: 1fr; } }
  .quote label.consent { display: flex; gap: 8px; font-size: 12.5px; opacity: .9; line-height: 1.45; }
  .quote label.consent input { width: auto; margin-top: 2px; }
  .quote button { background: #0d7a6f; color: #fff; border: none; border-radius: 6px; padding: 11px 18px; font-size: 15px; font-weight: 700; cursor: pointer; }
  .quote button:hover { background: #0b6a60; }
  #q-msg { font-size: 13px; min-height: 18px; margin-top: 4px; }
  #q-msg.ok { color: #7ce3a5; }
  #q-msg.err { color: #ff9d9d; }
  .links { display: grid; gap: 6px; font-size: 14px; }
  .links a { color: #1463a0; }
  .catgrid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
  .catcard { background: #fff; border-radius: 8px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .catcard h3 { font-size: 15px; margin-bottom: 4px; }
  .catcard h3 a { color: #1463a0; text-decoration: none; }
  .catcard .big { font-size: 20px; font-weight: 700; }
  .catcard small { color: #667; display: block; margin-top: 2px; }
  .catcard .cities { font-size: 13px; margin-top: 8px; }
  footer { text-align: center; color: #889; font-size: 12px; padding: 20px; }
  footer a { color: #1463a0; }
  @media (prefers-color-scheme: dark) {
    :root { color-scheme: dark; }
    body { background: #0e1319; color: #dbe4ee; }
    header { background: #0d2233; }
    .stat, .card, .catcard { background: #17202b; box-shadow: 0 1px 3px rgba(0,0,0,.45); }
    .stat small, .lede, .crumb, .rank-note, .catcard small, footer { color: #93a1b5; }
    .card p { color: #b9c6d6; }
    .band .bl, .band .bn { color: #a7b4c6; }
    .band .track { background: #243040; }
    .band .fill { background: #6aa8e0; }
    table.co td, table.co th { border-bottom-color: #2c3a4d; }
    table.co th { color: #93a1b5; }
    .quote { background: #0b1f24; }
    .quote input, .quote select, .quote textarea { background: #0e1319; border-color: #2c3a4d; color: #dbe4ee; }
    a { color: #6aa8e0; }
  }
"""

HEADER = """<header>
  <h1><a href="/">Permit<span>Picker</span></a></h1>
  <p>Raleigh permit intelligence — live data, refreshed daily · <a href="/costs/">Homeowner cost guides</a></p>
</header>"""

FOOTER = """<footer>PermitPicker — recent Wake County, Cary &amp; Raleigh filings. Public records, refreshed daily.<br>
Cost figures are permit-declared values from public records — see the methodology note on each guide.</footer>"""


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def render_form(cat, place, city_value, p25, med, p75):
    """Quote-request CTA. Backend: see LEAD_ENDPOINT TODO in the script."""
    brackets = [
        f"Under {money(p25)}",
        f"{money(p25)} – {money(med)}",
        f"{money(med)} – {money(p75)}",
        f"Over {money(p75)}",
        "Not sure yet",
    ]
    opts = "\n".join(f'          <option>{esc(b)}</option>' for b in brackets)
    return f"""<section class="quote">
  <h2>Get quotes for your {esc(cat['job'])}</h2>
  <p>Free for homeowners. Your request is shared with <b>at most 2</b> local
  contractors who actively do this work in {esc(place)} — never blasted to a
  list. Contractors reply directly; there's no obligation to hire anyone.</p>
  <form id="q-form">
    <div class="row2">
      <input type="text" id="q-name" placeholder="Your name" required>
      <input type="tel" id="q-phone" placeholder="Phone" required>
    </div>
    <div class="row2">
      <input type="email" id="q-email" placeholder="Email (optional)">
      <input type="text" id="q-city" value="{esc(city_value)}" placeholder="City" required>
    </div>
    <div class="row2">
      <input type="text" id="q-job" value="{esc(cat['label'])}" readonly>
      <select id="q-timeline">
        <option>ASAP</option>
        <option>This season</option>
        <option>Just researching</option>
      </select>
    </div>
    <select id="q-budget">
      <option value="">Budget range…</option>
{opts}
    </select>
    <textarea id="q-details" placeholder="Tell the contractors about the project (size, timing, anything that matters)"></textarea>
    <label class="consent"><input type="checkbox" id="q-consent" required>
      <span>By submitting, I agree that matched local contractors (at most 2) may contact me about my project by phone, text, or email.</span></label>
    <button type="submit">Request quotes</button>
    <p id="q-msg"></p>
  </form>
</section>
<script>
// TODO(backend): quote requests are not stored anywhere yet. The Supabase
// project has no leads table — the existing `waitlist` table is
// email+trade only and cannot hold name/phone/city/project fields.
// To enable: run sql/quote_requests.sql (in the PermitPicker repo) in the
// Supabase SQL editor, then set LEAD_ENDPOINT to
// SUPABASE_URL + "/rest/v1/quote_requests". No other change is needed.
const LEAD_ENDPOINT = null; // TODO: enable when the quote_requests table exists
const SUPABASE_URL = "{SUPABASE_URL}";
const SUPABASE_ANON_KEY = "PASTE_ANON_KEY_HERE";
document.getElementById("q-form").addEventListener("submit", async e => {{
  e.preventDefault();
  const msg = document.getElementById("q-msg");
  if (!LEAD_ENDPOINT) {{
    msg.className = "err";
    msg.textContent = "Quote requests aren't open yet — we're still lining up local contractors for this area. The cost data above is updated daily, so check back soon.";
    return;
  }}
  msg.className = ""; msg.textContent = "Sending…";
  const payload = {{
    name: document.getElementById("q-name").value.trim(),
    phone: document.getElementById("q-phone").value.trim(),
    email: document.getElementById("q-email").value.trim() || null,
    city: document.getElementById("q-city").value.trim(),
    job_type: document.getElementById("q-job").value,
    timeline: document.getElementById("q-timeline").value,
    budget: document.getElementById("q-budget").value || null,
    details: document.getElementById("q-details").value.trim() || null,
    source_page: location.pathname
  }};
  try {{
    const res = await fetch(LEAD_ENDPOINT, {{
      method: "POST",
      headers: {{
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": "Bearer " + SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
      }},
      body: JSON.stringify(payload)
    }});
    if (res.ok) {{
      msg.className = "ok";
      msg.textContent = "Request sent — up to 2 local contractors can now get in touch about your project.";
      e.target.reset();
    }} else {{
      msg.className = "err";
      msg.textContent = "Something went wrong sending your request — please try again.";
    }}
  }} catch (err) {{
    msg.className = "err";
    msg.textContent = "Something went wrong sending your request — please try again.";
  }}
}});
</script>"""


def render_guide(cat, place, place_slug, records, gen_date, all_pages):
    """all_pages: list of dicts {cat, place, slug} for cross-linking."""
    vals = sorted(float(r["valuation"]) for r in records)
    med = percentile(vals, 0.5)
    p25 = percentile(vals, 0.25)
    p75 = percentile(vals, 0.75)
    lo, hi = date_span(records)
    span = fmt_span(lo, hi)
    n = len(vals)
    slug = cat["id"] if not place_slug else f"{cat['id']}-{place_slug}"
    url = f"{SITE}/costs/{slug}/"
    job = cat["job"]
    title = f"How much does a {job} cost in {place}? — PermitPicker"
    desc = (f"Real declared costs from {n} permitted {job} jobs in {place} "
            f"({span}): typical {money(med)}, most jobs between {money(p25)} "
            f"and {money(p75)}. Plus local contractors actively doing this work.")
    bands = band_counts(vals)
    max_band = max(c for _, c in bands) if bands else 0
    band_rows = "\n".join(
        f'      <div class="band"><span class="bl">{esc(label)}</span>'
        f'<span class="track"><span class="fill" style="display:block;width:'
        f'{round(100 * c / max_band) if max_band else 0}%"></span></span>'
        f'<span class="bn">{c}</span></div>'
        for label, c in bands)
    contractors = top_contractors(records)
    if contractors:
        rows_html = "\n".join(
            f"""        <tr><td><b>{esc(c['name'])}</b></td>
          <td>{c['count']}</td><td>{money(c['median'])}</td>
          <td>{f'<a class="call" href="{tel_href(c["phone"])}">Call</a>' if c['phone'] else '—'}</td></tr>"""
            for c in contractors)
        co_html = f"""<div class="card">
    <h2>Contractors actively doing this work in {esc(place)}</h2>
    <table class="co">
      <tr><th>Contractor</th><th>Permits here</th><th>Typical project size</th><th></th></tr>
{rows_html}
    </table>
    <p class="rank-note">Ranked by recent permit activity from public records — not advertising.
    “Typical project size” is the median declared value of that contractor's {esc(job)} permits in this set.</p>
  </div>"""
    else:
        co_html = ""
    # Cross-links: same place, other categories; same category, other places.
    same_place = [p for p in all_pages
                  if p["place"] == place and p["cat"]["id"] != cat["id"]]
    same_cat = [p for p in all_pages
                if p["cat"]["id"] == cat["id"] and p["place"] != place]
    link_lines = []
    for p in same_place:
        link_lines.append(
            f'      <a href="/costs/{p["slug"]}/">How much does a '
            f'{esc(p["cat"]["job"])} cost in {esc(place)}?</a>')
    for p in same_cat:
        link_lines.append(
            f'      <a href="/costs/{p["slug"]}/">{esc(cat["label"])} costs '
            f'in {esc(p["place"])}</a>')
    links_html = "\n".join(link_lines)
    city_value = "" if place == REGION_NAME else place
    form_html = render_form(cat, place, city_value, p25, med, p75)
    return slug, f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PermitPicker">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{url}">
<meta name="theme-color" content="#f4f6f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1319" media="(prefers-color-scheme: dark)">
<style>{CSS}</style>
</head>
<body>
{HEADER}
<main>
  <div class="crumb"><a href="/">PermitPicker</a> / <a href="/costs/">Cost guides</a> / {esc(cat['label'])} — {esc(place)}</div>
  <h1 class="page">How much does a {esc(job)} cost in {esc(place)}?</h1>
  <p class="lede">Based on actual permit records — the values homeowners and
  contractors declared to the city and county when the work was permitted —
  not survey estimates.</p>
  <div class="stats">
    <div class="stat"><b>{money(med)}</b><small>typical (median) declared value</small></div>
    <div class="stat"><b>{money(p25)} – {money(p75)}</b><small>where most jobs fall (middle 50%)</small></div>
    <div class="stat"><b>{n}</b><small>permitted jobs in {esc(place)}, {esc(span)}</small></div>
  </div>
  <div class="card">
    <h2>How declared values spread out</h2>
{band_rows}
    <p class="rank-note">Each bar counts permitted {esc(job)} jobs by declared value band.</p>
  </div>
  <div class="card honesty">
    <h2>Read this before trusting the numbers</h2>
    <p><b>These are declared values, not invoices.</b> When someone applies
    for a permit, they state an estimated project value up front — before
    the work starts — and permit fees scale with that number, so declared
    values tend to run <b>below</b> what the job finally costs. Treat them
    as a market anchor, not a quote.</p>
    <p><b>Only permitted work is counted.</b> Jobs done without a permit
    (a large share of small repairs) never appear here, and withdrawn
    permits are excluded. Figures cover {n} permitted jobs filed
    {esc(span)} and are refreshed daily from public records; the middle
    50% range is a better budgeting guide than any single number.</p>
  </div>
{co_html}
{form_html}
  <div class="card">
    <h2>More cost guides</h2>
    <div class="links">
{links_html}
      <a href="/costs/">All homeowner cost guides</a>
      <a href="/">Browse the live permit feed</a>
    </div>
  </div>
  <p class="rank-note">Data generated {esc(gen_date)} from public permit records (Wake County, Raleigh &amp; Cary).</p>
</main>
{FOOTER}
</body>
</html>
"""


def render_index(cells, gen_date):
    """cells: list of {cat, region: stats dict, cities: [(place, slug, stats)]}"""
    cards = []
    for cell in cells:
        cat, st = cell["cat"], cell["region"]
        city_links = " · ".join(
            f'<a href="/costs/{slug}/">{esc(place)}</a>'
            for place, slug, _ in cell["cities"])
        cards.append(f"""    <div class="catcard">
      <h3><a href="/costs/{cat['id']}/">{esc(cat['label'])} cost in the {REGION_NAME}</a></h3>
      <span class="big">{money(st['median'])}</span>
      <small>typical declared value · most jobs {money(st['p25'])} – {money(st['p75'])} · based on {st['n']} permitted jobs, {esc(st['span'])}</small>
      {f'<div class="cities">By city: {city_links}</div>' if city_links else ''}
    </div>""")
    cards_html = "\n".join(cards)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Homeowner cost guides — what local jobs actually cost | PermitPicker</title>
<meta name="description" content="What decks, additions, pools, accessory buildings, and kitchen and bathroom remodels actually cost in the Raleigh area — from real permit records, not surveys.">
<link rel="canonical" href="{SITE}/costs/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PermitPicker">
<meta property="og:title" content="Homeowner cost guides — PermitPicker">
<meta property="og:description" content="Typical declared costs for local home projects, computed from public permit records in Wake County, Raleigh & Cary.">
<meta property="og:url" content="{SITE}/costs/">
<meta name="theme-color" content="#f4f6f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1319" media="(prefers-color-scheme: dark)">
<style>{CSS}</style>
</head>
<body>
{HEADER}
<main>
  <div class="crumb"><a href="/">PermitPicker</a> / Cost guides</div>
  <h1 class="page">What does it cost? Real numbers from local permits</h1>
  <p class="lede">National cost guides survey homeowners and blend the whole
  country. These guides are computed from actual permit records filed in
  Wake County, Raleigh &amp; Cary — the value declared for each job when it
  was permitted. Declared values tend to run below final invoices, so use
  them as an anchor, not a quote; every guide explains its data.</p>
  <div class="catgrid">
{cards_html}
  </div>
  <p class="rank-note" style="margin-top:14px">Data generated {esc(gen_date)} from public permit records. Figures refresh daily.</p>
</main>
{FOOTER}
</body>
</html>
"""


STATIC_SITEMAP_URLS = [
    ("https://permitpicker.com/", "daily", "1.0"),
    ("https://permitpicker.com/?type=electrical", "daily", "0.8"),
    ("https://permitpicker.com/?type=plumbing", "daily", "0.8"),
    ("https://permitpicker.com/?type=mechanical", "daily", "0.8"),
    ("https://permitpicker.com/?type=building", "daily", "0.8"),
    ("https://permitpicker.com/?type=demolition", "daily", "0.8"),
    ("https://permitpicker.com/?type=fire", "daily", "0.8"),
    ("https://permitpicker.com/?type=sitework", "daily", "0.8"),
]


def write_sitemap(page_slugs):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, freq, pri in STATIC_SITEMAP_URLS:
        lines.append(f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq>"
                     f"<priority>{pri}</priority></url>")
    lines.append('  <url><loc>https://permitpicker.com/costs/</loc>'
                 '<changefreq>daily</changefreq><priority>0.9</priority></url>')
    for slug in page_slugs:
        lines.append(f"  <url><loc>{SITE}/costs/{slug}/</loc>"
                     "<changefreq>daily</changefreq>"
                     "<priority>0.8</priority></url>")
    lines.append("</urlset>")
    with open(SITEMAP_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def stats_for(records):
    vals = sorted(float(r["valuation"]) for r in records)
    lo, hi = date_span(records)
    return {"n": len(vals), "median": percentile(vals, 0.5),
            "p25": percentile(vals, 0.25), "p75": percentile(vals, 0.75),
            "span": fmt_span(lo, hi)}


def main():
    with open(DATA_PATH) as f:
        data = json.load(f)
    records = data.get("permits", [])
    gen_date = (data.get("generated_at") or "")[:10] or \
        dt.date.today().isoformat()

    # Select cells: region per category + city cells meeting the threshold.
    cells = []          # per category: region records + city records
    pages = []          # flat list for cross-linking {cat, place, slug}
    for cat in CATEGORIES:
        sel = [r for r in records if cat["match"](r)]
        by_city = defaultdict(list)
        for r in sel:
            c = city_display(r.get("city"))
            if c:
                by_city[c].append(r)
        city_cells = []
        for place in sorted(by_city):
            recs = by_city[place]
            if len(recs) >= CITY_MIN_N:
                slug = f"{cat['id']}-{slugify(place)}"
                city_cells.append((place, slug, recs))
                pages.append({"cat": cat, "place": place, "slug": slug})
        pages.append({"cat": cat, "place": REGION_NAME, "slug": cat["id"]})
        cells.append({"cat": cat, "region": sel, "cities": city_cells})

    # Regenerate web/costs from scratch (this dir holds only generated pages).
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR)

    written = []
    index_cells = []
    for cell in cells:
        cat = cell["cat"]
        slug, page_html = render_guide(cat, REGION_NAME, "", cell["region"],
                                       gen_date, pages)
        d = os.path.join(OUT_DIR, slug)
        os.makedirs(d)
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write(page_html)
        written.append(slug)
        city_stats = []
        for place, cslug, recs in cell["cities"]:
            _, page_html = render_guide(cat, place, slugify(place), recs,
                                        gen_date, pages)
            d = os.path.join(OUT_DIR, cslug)
            os.makedirs(d)
            with open(os.path.join(d, "index.html"), "w") as f:
                f.write(page_html)
            written.append(cslug)
            city_stats.append((place, cslug, stats_for(recs)))
        index_cells.append({"cat": cat, "region": stats_for(cell["region"]),
                            "cities": city_stats})

    with open(os.path.join(OUT_DIR, "index.html"), "w") as f:
        f.write(render_index(index_cells, gen_date))
    write_sitemap(written)
    print(f"cost_guides: wrote /costs/ index + {len(written)} guide pages "
          f"from {len(records)} permits (data generated {gen_date})")
    for cell in index_cells:
        st = cell["region"]
        cities = ", ".join(f"{p} n={s['n']}" for p, _, s in cell["cities"])
        print(f"  {cell['cat']['id']}: region n={st['n']} "
              f"median={money(st['median'])} [{st['span']}]"
              + (f" | city pages: {cities}" if cities else ""))


if __name__ == "__main__":
    main()
