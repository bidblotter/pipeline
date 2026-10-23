#!/usr/bin/env python3
"""Build PermitPicker contractor ranking and profile pages.

This generator runs after cost_guides.py in the ingest workflow.  It uses
exactly the same homeowner-category definitions and city-cell thresholds as
cost_guides.py, but presents the contractor side of the same public permit
snapshot:

  * /contractors/                    rankings hub
  * /contractors/<cell>/             up to 20 contractors for a cost-guide
                                       category x place cell
  * /contractors/<contractor-slug>/  aggregate public-activity profile

The rankings are activity rankings only.  They are not quality ratings,
review rankings, endorsements, or paid placements.  Pages show aggregates
only and never individual permit street addresses.
"""
import datetime as dt
import html
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cost_guides as cg  # noqa: E402
from normalize import contractor_key, display_for, is_contractor  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA_PATH = os.path.join(ROOT, "web", "data", "permits.json")
OUT_DIR = os.path.join(ROOT, "web", "contractors")
SITEMAP_PATH = os.path.join(ROOT, "web", "sitemap.xml")
SITE = cg.SITE

RANKING_LABELS = {
    "deck": "Deck builders",
    "addition": "Home addition contractors",
    "pool": "Pool builders",
    "accessory-building": "Accessory building contractors",
    "kitchen-remodel": "Kitchen remodelers",
    "bathroom-remodel": "Bathroom remodelers",
    "screened-porch": "Screened porch builders",
    "solar": "Solar installers",
}

# The ranking/profile layer is a homeowner-facing directory.  In addition
# to normalize.is_contractor (which rejects owner/tenant/TBD placeholders),
# keep public bodies, schools, utilities, and known non-commercial entities
# out of it.  These checks intentionally do NOT alter cost-guide sections,
# which continue to use cost_guides.top_contractors as before.
INSTITUTIONAL_PATTERNS = (
    r"^(city|town|county|state) of\b",
    r"^(wake county|north carolina|nc department|n c department)\b",
    r"\b(board of education|school district|school system|public schools?)\b",
    r"\b(university|college|academy|school|schools)\b",
    r"\b(housing authority|redevelopment authority|transit authority|"
    r"utility authority|water authority|sewer authority)\b",
    r"\b(department of transportation|department of public utilities|"
    r"public utilities department)\b",
    r"\b(duke energy|dominion energy|piedmont natural gas|progress energy|"
    r"psnc energy|at&t|spectrum|charter communications|verizon|google fiber)\b",
    r"\b(utility|utilities|government|municipal)\b",
    r"\bhabitat for humanity\b",
)
PLACEHOLDER_KEYS = {
    "", "homeowner", "home owner", "property owner", "owner",
    "owner builder", "tenant", "same", "self", "unknown", "none", "n a",
    "na", "tbd",
}

EXTRA_CSS = """
  .method { border-left: 4px solid #1463a0; }
  .cta-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
  .btn { display: inline-block; background: #1463a0; color: #fff; padding: 9px 14px; border-radius: 6px; text-decoration: none; font-weight: 700; font-size: 14px; }
  .btn.secondary { background: #e8eef4; color: #14324a; }
  .btn:hover { filter: brightness(1.08); }
  table.rank td.rank { font-weight: 800; color: #1463a0; width: 42px; }
  .muted { color: #667; font-size: 13px; }
  .profile-actions { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0; }
  @media (prefers-color-scheme: dark) {
    .btn.secondary { background: #243040; color: #dbe4ee; }
    table.rank td.rank { color: #6aa8e0; }
    .muted { color: #93a1b5; }
  }
"""


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def rankable_business(raw_name, selected_name=None):
    """True when a contractor string belongs in the new directory layer."""
    if not is_contractor(raw_name):
        return False
    key = contractor_key(raw_name)
    if key in PLACEHOLDER_KEYS or key.startswith("tbd"):
        return False
    # Check both the raw key and the display name eventually shown.
    haystacks = [key]
    if selected_name:
        haystacks.append(contractor_key(selected_name))
        haystacks.append(selected_name.lower())
    for text in haystacks:
        if any(re.search(pattern, text) for pattern in INSTITUTIONAL_PATTERNS):
            return False
    return True


def collect_groups(indexed_records):
    """Group (record_index, record) pairs by normalized contractor.

    Mirrors cost_guides.top_contractors' grouping/display/phone choices,
    while retaining first/latest dates, cities, and record indices for the
    richer ranking/profile pages.
    """
    groups = {}
    for idx, r in indexed_records:
        raw = (r.get("contractor") or "").strip()
        if not rankable_business(raw):
            continue
        key = contractor_key(raw)
        if not key:
            continue
        g = groups.setdefault(key, {
            "key": key, "raw_names": [], "displays": [],
            "vals": [], "phones": Counter(), "dates": [],
            "cities": Counter(), "indices": set(),
        })
        g["raw_names"].append(raw)
        if r.get("contractor_display"):
            g["displays"].append(r["contractor_display"])
        try:
            g["vals"].append(float(r.get("valuation") or 0))
        except (TypeError, ValueError):
            g["vals"].append(0.0)
        phone = (r.get("contractor_phone") or "").strip()
        if phone:
            g["phones"][phone] += 1
        date = cg.record_date(r)
        if date:
            g["dates"].append(date)
        city = cg.city_display(r.get("city"))
        if city:
            g["cities"][city] += 1
        g["indices"].add(idx)

    out = []
    for key, g in groups.items():
        name = (Counter(g["displays"]).most_common(1)[0][0]
                if g["displays"] else display_for(key, g["raw_names"]))
        if not name or not rankable_business(g["raw_names"][0], name):
            continue
        vals = sorted(g["vals"])
        dates = sorted(g["dates"])
        out.append({
            "key": key,
            "name": name,
            "count": len(g["indices"]),
            "median": cg.percentile(vals, 0.5),
            "phone": g["phones"].most_common(1)[0][0] if g["phones"] else "",
            "first": dates[0] if dates else "",
            "latest": dates[-1] if dates else "",
            "cities": g["cities"],
            "indices": g["indices"],
            "raw_names": g["raw_names"],
            "displays": g["displays"],
            "phones": g["phones"],
        })
    # Same primary ordering as cost_guides.top_contractors: permit count,
    # then latest activity.  Group insertion order is deterministic for a
    # given published snapshot and is the stable tie-breaker.
    out.sort(key=lambda c: (c["count"], c["latest"]), reverse=True)
    return out


def build_cells(records):
    """Reproduce cost_guides.main's cell selection using its matchers."""
    cells = []
    for cat in cg.CATEGORIES:
        selected = [r for r in records if cat["match"](r)]
        cells.append({
            "cat": cat, "place": cg.REGION_NAME, "slug": cat["id"],
            "records": selected, "is_region": True,
        })
        by_city = defaultdict(list)
        for r in selected:
            city = cg.city_display(r.get("city"))
            if city:
                by_city[city].append(r)
        for place in sorted(by_city):
            recs = by_city[place]
            if len(recs) >= cg.CITY_MIN_N:
                cells.append({
                    "cat": cat, "place": place,
                    "slug": f"{cat['id']}-{cg.slugify(place)}",
                    "records": recs, "is_region": False,
                })
    return cells


def fmt_date(date_str):
    try:
        return dt.date.fromisoformat(date_str).strftime("%b %d, %Y")
    except (TypeError, ValueError):
        return date_str or "—"


def place_phrase(cell):
    return f"the {cg.REGION_NAME}" if cell["is_region"] else cell["place"]


def ranking_label(cat):
    return RANKING_LABELS[cat["id"]]


def render_hub(cells, rankings, profiles, gen_date):
    cards = []
    # Group cells by category in cost-guide order; region first, then cities.
    for cat in cg.CATEGORIES:
        cat_cells = [c for c in cells if c["cat"]["id"] == cat["id"]]
        if not cat_cells:
            continue
        region = next(c for c in cat_cells if c["is_region"])
        city_links = []
        for cell in cat_cells:
            if not cell["is_region"]:
                city_links.append(
                    f'<a href="/contractors/{cell["slug"]}/">{esc(cell["place"])}</a>')
        named = len(rankings[(cat["id"], cg.REGION_NAME)])
        cards.append(f"""    <div class="catcard">
      <h3><a href="/contractors/{region['slug']}/">{esc(ranking_label(cat))} in the {esc(cg.REGION_NAME)}</a></h3>
      <span class="big">{len(region['records'])}</span>
      <small>permitted projects in this category · {named} named contractors ranked by activity</small>
      {f'<div class="cities">By city: {" · ".join(city_links)}</div>' if city_links else ''}
      <div class="cities"><a href="/costs/{cat['id']}/">See what a {esc(cat['job'])} costs</a></div>
    </div>""")
    cards_html = "\n".join(cards)
    hub_items = []
    for i, cell in enumerate(cells, 1):
        hub_items.append({
            "@type": "ListItem", "position": i,
            "name": f"{ranking_label(cell['cat'])} in {place_phrase(cell)}",
            "url": f"{SITE}/contractors/{cell['slug']}/"})
    ld_html = cg.ld_script({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "CollectionPage",
             "name": "Local contractor rankings by permit activity | PermitPicker",
             "url": f"{SITE}/contractors/",
             "isPartOf": {"@type": "WebSite", "name": "PermitPicker",
                          "url": f"{SITE}/"}},
            cg.breadcrumb_ld([("PermitPicker", f"{SITE}/"),
                              ("Contractors", f"{SITE}/contractors/")]),
            {"@type": "ItemList", "itemListElement": hub_items},
        ],
    })
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local contractor rankings by permit activity | PermitPicker</title>
<meta name="description" content="Raleigh-area contractor rankings based on recent permitted activity in public records — not reviews, quality scores, or paid placement.">
<link rel="canonical" href="{SITE}/contractors/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PermitPicker">
<meta property="og:title" content="Local contractor rankings by permit activity | PermitPicker">
<meta property="og:description" content="See which local contractors are actively pulling permits for decks, additions, pools, accessory buildings, and remodels.">
<meta property="og:url" content="{SITE}/contractors/">
<meta name="theme-color" content="#f4f6f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1319" media="(prefers-color-scheme: dark)">
{cg.GA_HEAD}
{ld_html}
<style>{cg.CSS}{EXTRA_CSS}</style>
</head>
<body>
{cg.HEADER}
<main>
  <div class="crumb"><a href="/">PermitPicker</a> / Contractors</div>
  <h1 class="page">Contractors actively doing local work</h1>
  <p class="lede">Rankings built from public permit records in Wake County,
  Raleigh &amp; Cary. Start with a project type, compare recent permitted
  activity, then check local costs or request quotes.</p>
  <div class="card method">
    <h2>How these rankings work</h2>
    <p>Contractors are ranked by the number of matching permitted projects
    in public records, then by most recent activity. <b>This is not a
    quality, workmanship, review, licensing, or “best contractor” ranking</b>,
    and no contractor can pay for placement.</p>
  </div>
  <div class="catgrid">
{cards_html}
  </div>
  <div class="card">
    <h2>Contractor activity profiles</h2>
    <p>{len(profiles)} contractors have an aggregate public-activity profile.
    Profiles summarize permitted work across the {len(cg.CATEGORIES)} homeowner categories;
    they are not endorsements.</p>
    <div class="cta-row"><a class="btn" href="/costs/">Browse project costs</a>
    <a class="btn secondary" href="/">Explore permit activity</a></div>
  </div>
  <p class="rank-note">Data generated {esc(gen_date)} from public permit records. Figures refresh daily.</p>
</main>
{cg.FOOTER}
</body>
</html>
"""


def render_ranking(cell, ranking, profile_slugs, all_cells, gen_date):
    cat = cell["cat"]
    place = place_phrase(cell)
    label = ranking_label(cat)
    title = f"{label} in {place} — ranked by permitted activity | PermitPicker"
    lo, hi = cg.date_span(cell["records"])
    span = cg.fmt_span(lo, hi)
    data_through = fmt_date(hi)
    top = ranking[:20]
    rows = []
    for i, c in enumerate(top, 1):
        profile = profile_slugs.get(c["key"])
        name_html = (f'<a href="/contractors/{profile}/"><b>{esc(c["name"])}</b></a>'
                     if profile else f'<b>{esc(c["name"])}</b>')
        activity = cg.fmt_span(c["first"], c["latest"]) or "—"
        call = (f'<a class="call" href="/contractors/{profile}/">Details</a>'
                if profile else
                '<a class="call" href="/permitpulse/">Get contact details</a>')
        rows.append(f"""        <tr><td class="rank">{i}</td><td>{name_html}</td>
          <td>{c['count']}</td><td>{esc(activity)}</td>
          <td>{cg.money(c['median'])}</td><td>{call}</td></tr>""")
    rows_html = "\n".join(rows)
    # Sibling links: same category in other places, then this place's other
    # homeowner categories.
    siblings = []
    for other in all_cells:
        if other["cat"]["id"] == cat["id"] and other["slug"] != cell["slug"]:
            siblings.append(
                f'<a href="/contractors/{other["slug"]}/">{esc(label)} in {esc(place_phrase(other))}</a>')
    for other in all_cells:
        if (other["place"] == cell["place"]
                and other["cat"]["id"] != cat["id"]):
            siblings.append(
                f'<a href="/contractors/{other["slug"]}/">{esc(ranking_label(other["cat"]))} in {esc(place)}</a>')
    siblings_html = "\n      ".join(siblings)
    rank_items = []
    for i, c in enumerate(top, 1):
        item = {"@type": "ListItem", "position": i, "name": c["name"]}
        prof = profile_slugs.get(c["key"])
        if prof:
            item["url"] = f"{SITE}/contractors/{prof}/"
        rank_items.append(item)
    ld_html = cg.ld_script({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": title,
             "url": f"{SITE}/contractors/{cell['slug']}/",
             "isPartOf": {"@type": "WebSite", "name": "PermitPicker",
                          "url": f"{SITE}/"}},
            cg.breadcrumb_ld([
                ("PermitPicker", f"{SITE}/"),
                ("Contractors", f"{SITE}/contractors/"),
                (f"{label} — {cell['place']}",
                 f"{SITE}/contractors/{cell['slug']}/")]),
            {"@type": "ItemList",
             "name": f"{label} in {place} — ranked by permitted activity",
             "itemListElement": rank_items},
        ],
    })
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(label)} in {esc(place)}, ranked by recent permitted activity in public records. {len(cell['records'])} matching permitted projects and {len(ranking)} named contractors; data through {esc(data_through)}.">
<link rel="canonical" href="{SITE}/contractors/{cell['slug']}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PermitPicker">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="Activity-based local contractor ranking from public permit records — not a quality or paid-placement ranking.">
<meta property="og:url" content="{SITE}/contractors/{cell['slug']}/">
<meta name="theme-color" content="#f4f6f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1319" media="(prefers-color-scheme: dark)">
{cg.GA_HEAD}
{ld_html}
<style>{cg.CSS}{EXTRA_CSS}</style>
</head>
<body>
{cg.HEADER}
<main>
  <div class="crumb"><a href="/">PermitPicker</a> / <a href="/contractors/">Contractors</a> / {esc(label)} — {esc(cell['place'])}</div>
  <h1 class="page">{esc(label)} in {esc(place)}</h1>
  <p class="lede">Local contractors ranked by how much matching permitted
  work appears in public records for {esc(place)}.</p>
  <div class="stats">
    <div class="stat"><b>{len(cell['records'])}</b><small>matching permitted projects, {esc(span)}</small></div>
    <div class="stat"><b>{len(ranking)}</b><small>named contractors in this ranking</small></div>
    <div class="stat"><b>{esc(data_through)}</b><small>latest matching permit activity</small></div>
  </div>
  <div class="card method">
    <h2>Methodology — activity, not quality</h2>
    <p>This ranking counts matching permitted projects in public records
    and breaks ties by most recent activity. It is <b>not</b> a rating of
    quality, workmanship, customer reviews, licensing, or value, and it is
    not paid placement. A high rank means the contractor appears often in
    this permit category and place.</p>
    <div class="cta-row">
      <a class="btn" href="/costs/{cell['slug']}/">What does a {esc(cat['job'])} cost in {esc(cell['place'])}?</a>
      <a class="btn secondary" href="/costs/{cell['slug']}/">Request quotes</a>
    </div>
  </div>
  <div class="card">
    <h2>Top {len(top)} by permitted activity</h2>
    <table class="co rank">
      <tr><th>#</th><th>Contractor</th><th>Permits</th><th>Activity</th><th>Typical project size</th><th></th></tr>
{rows_html}
    </table>
    <p class="rank-note">“Typical project size” is the median declared permit
    value for that contractor within this category and place. Declared values
    are permit estimates, not final invoices.</p>
  </div>
  <div class="card">
    <h2>More contractor rankings</h2>
    <div class="links">
      {siblings_html}
      <a href="/contractors/">All contractor rankings</a>
      <a href="/costs/">All homeowner cost guides</a>
    </div>
  </div>
  <p class="rank-note">Data generated {esc(gen_date)} from public permit records (Wake County, Raleigh &amp; Cary).</p>
</main>
{cg.FOOTER}
</body>
</html>
"""


def strongest_cost_cell(profile, cells):
    """Pick the profile's strongest category, then its strongest city cell
    in that category when one exists; otherwise the region cost page."""
    cat_counts = {cid: s["count"]
                  for cid, s in profile["categories"].items()}
    if not cat_counts:
        return None
    # Category order is the deterministic final tie-breaker.
    cat_order = {cat["id"]: i for i, cat in enumerate(cg.CATEGORIES)}
    strongest_cat = sorted(
        cat_counts,
        key=lambda cid: (cat_counts[cid],
                         profile["categories"][cid]["latest"],
                         -cat_order[cid]),
        reverse=True)[0]
    city_counts = profile["cat_city_counts"].get(strongest_cat, {})
    city_cells = [c for c in cells
                  if c["cat"]["id"] == strongest_cat and not c["is_region"]]
    best_city = None
    best_count = 0
    for cell in city_cells:
        n = city_counts.get(cell["place"], 0)
        if n > best_count:
            best_city = cell
            best_count = n
    if best_city:
        return best_city
    return next(c for c in cells
                if c["cat"]["id"] == strongest_cat and c["is_region"])


def render_profile(profile, cells, gen_date):
    name = profile["name"]
    lo, hi = profile["first"], profile["latest"]
    span = cg.fmt_span(lo, hi)
    cities = profile["cities"].most_common(6)
    cities_text = ", ".join(f"{esc(city)} ({n})" for city, n in cities) or "—"
    cat_rows = []
    for cat in cg.CATEGORIES:
        s = profile["categories"].get(cat["id"])
        if not s:
            continue
        cat_rows.append(f"""        <tr><td><a href="/contractors/{cat['id']}/"><b>{esc(cat['label'])}</b></a></td>
          <td>{s['count']}</td><td>{cg.money(s['median'])}</td>
          <td>{esc(cg.fmt_span(s['first'], s['latest']))}</td></tr>""")
    cat_rows_html = "\n".join(cat_rows)
    appearances = []
    for app in profile["ranking_appearances"]:
        cell = app["cell"]
        appearances.append(
            f'<a href="/contractors/{cell["slug"]}/">#{app["rank"]} — '
            f'{esc(ranking_label(cell["cat"]))} in {esc(place_phrase(cell))}</a>')
    appearances_html = "\n      ".join(appearances) or "—"
    strongest = strongest_cost_cell(profile, cells)
    quote_html = ""
    if strongest:
        quote_html = f"""  <section class="quote">
    <h2>Planning a {esc(strongest['cat']['job'])}?</h2>
    <p>Compare local declared costs and request quotes from matched local
    contractors in {esc(strongest['place'])}. PermitPicker does not promise
    that this specific contractor will receive or respond to a request.</p>
    <div class="cta-row"><a class="btn" href="/costs/{strongest['slug']}/">See costs &amp; request quotes</a></div>
  </section>"""
    call_html = ("<a class=\"call\" href=\"/permitpulse/\">Get this "
                 "firm's contact details — PermitPulse</a>"
                 if profile["phone"] else "")
    org = {"@type": "Organization", "name": name,
           "url": f"{SITE}/contractors/{profile['slug']}/",
           "description": (f"Aggregate public permit activity profile for "
                           f"{name} across PermitPicker homeowner "
                           f"categories. Activity is not a quality rating "
                           f"or endorsement.")}
    ld_html = cg.ld_script({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "ProfilePage",
             "url": f"{SITE}/contractors/{profile['slug']}/",
             "mainEntity": org},
            cg.breadcrumb_ld([
                ("PermitPicker", f"{SITE}/"),
                ("Contractors", f"{SITE}/contractors/"),
                (name, f"{SITE}/contractors/{profile['slug']}/")]),
        ],
    })
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(name)} — public permit activity profile | PermitPicker</title>
<meta name="description" content="Aggregate public permit activity for {esc(name)} across PermitPicker homeowner categories: {profile['total']} category permits, activity {esc(span)}. Activity is not a quality rating or endorsement.">
<link rel="canonical" href="{SITE}/contractors/{profile['slug']}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="PermitPicker">
<meta property="og:title" content="{esc(name)} — public permit activity profile | PermitPicker">
<meta property="og:description" content="Aggregate permitted activity by category and city from public records. Not a quality rating or endorsement.">
<meta property="og:url" content="{SITE}/contractors/{profile['slug']}/">
<meta name="theme-color" content="#f4f6f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1319" media="(prefers-color-scheme: dark)">
{cg.GA_HEAD}
{ld_html}
<style>{cg.CSS}{EXTRA_CSS}</style>
</head>
<body>
{cg.HEADER}
<main>
  <div class="crumb"><a href="/">PermitPicker</a> / <a href="/contractors/">Contractors</a> / {esc(name)}</div>
  <h1 class="page">{esc(name)}</h1>
  <p class="lede">Public permit activity profile — activity is not a quality
  rating or endorsement.</p>
  <div class="profile-actions">{call_html}</div>
  <div class="stats">
    <div class="stat"><b>{profile['total']}</b><small>permits across the {len(cg.CATEGORIES)} homeowner categories</small></div>
    <div class="stat"><b>{len(profile['categories'])}</b><small>homeowner categories with activity</small></div>
    <div class="stat"><b>{esc(span)}</b><small>category activity span</small></div>
  </div>
  <div class="card">
    <h2>Activity by category</h2>
    <table class="co">
      <tr><th>Category</th><th>Permits</th><th>Typical project size</th><th>Activity</th></tr>
{cat_rows_html}
    </table>
    <p class="rank-note">Typical project size is the median declared permit
    value in that category. Categories can overlap on combined projects, so
    category rows may count a combined permit more than once; the headline
    total counts each matching permit once.</p>
  </div>
  <div class="card">
    <h2>Where the permitted work is</h2>
    <p>{cities_text}</p>
    <p class="rank-note">Cities are aggregated from permit records. No
    individual project addresses are shown on this profile.</p>
  </div>
  <div class="card">
    <h2>Ranking appearances</h2>
    <div class="links">
      {appearances_html}
    </div>
  </div>
{quote_html}
  <p class="rank-note">Data generated {esc(gen_date)} from public permit records (Wake County, Raleigh &amp; Cary).</p>
</main>
{cg.FOOTER}
</body>
</html>
"""


def aggregate_profiles(records, cells, rankings):
    """Build profile accumulators from category groups and ranking cells."""
    profiles = {}
    # Per-category aggregates.
    for cat in cg.CATEGORIES:
        indexed = [(i, r) for i, r in enumerate(records) if cat["match"](r)]
        for g in collect_groups(indexed):
            p = profiles.setdefault(g["key"], {
                "key": g["key"], "raw_names": [], "displays": [],
                "phones": Counter(), "indices": set(), "categories": {},
                "cat_city_counts": {}, "ranking_appearances": [],
            })
            p["raw_names"].extend(g["raw_names"])
            p["displays"].extend(g["displays"])
            p["phones"].update(g["phones"])
            p["indices"].update(g["indices"])
            p["categories"][cat["id"]] = {
                "count": g["count"], "median": g["median"],
                "first": g["first"], "latest": g["latest"],
            }
            p["cat_city_counts"][cat["id"]] = g["cities"]

    # Ranking appearances (top 20 only, as displayed).
    cell_by_slug = {c["slug"]: c for c in cells}
    for (cat_id, place), ranking in rankings.items():
        cell = next(c for c in cells
                    if c["cat"]["id"] == cat_id and c["place"] == place)
        for rank, entry in enumerate(ranking[:20], 1):
            if entry["key"] in profiles:
                profiles[entry["key"]]["ranking_appearances"].append(
                    {"cell": cell_by_slug[cell["slug"]], "rank": rank})

    qualified = set()
    for key, p in profiles.items():
        if p["ranking_appearances"]:
            qualified.add(key)
        elif any(s["count"] >= 3 for s in p["categories"].values()):
            qualified.add(key)

    out = {}
    for key in qualified:
        p = profiles[key]
        name = (Counter(p["displays"]).most_common(1)[0][0]
                if p["displays"] else display_for(key, p["raw_names"]))
        if not name:
            continue
        unique_records = [records[i] for i in sorted(p["indices"])]
        lo, hi = cg.date_span(unique_records)
        cities = Counter()
        for r in unique_records:
            city = cg.city_display(r.get("city"))
            if city:
                cities[city] += 1
        # Deterministic appearance order: category order, region before
        # city, then rank.
        cat_order = {cat["id"]: i for i, cat in enumerate(cg.CATEGORIES)}
        p["ranking_appearances"].sort(
            key=lambda a: (cat_order[a["cell"]["cat"]["id"]],
                           0 if a["cell"]["is_region"] else 1,
                           a["cell"]["place"], a["rank"]))
        out[key] = {
            "key": key, "name": name, "total": len(p["indices"]),
            "phone": p["phones"].most_common(1)[0][0] if p["phones"] else "",
            "categories": p["categories"],
            "cat_city_counts": p["cat_city_counts"],
            "ranking_appearances": p["ranking_appearances"],
            "cities": cities, "first": lo, "latest": hi,
        }
    return out


def assign_profile_slugs(profiles, reserved):
    """Deterministic slugs; ranking-cell slugs are reserved first."""
    used = set(reserved)
    by_base = {}
    for key, p in profiles.items():
        base = cg.slugify(p["name"]) or cg.slugify(key) or "contractor"
        by_base.setdefault(base, []).append(key)
    slugs = {}
    for base in sorted(by_base):
        for key in sorted(by_base[base],
                          key=lambda k: (profiles[k]["name"].lower(), k)):
            slug = base
            n = 2
            while slug in used:
                slug = f"{base}-{n}"
                n += 1
            used.add(slug)
            slugs[key] = slug
            profiles[key]["slug"] = slug
    return slugs


def update_sitemap(new_urls):
    """Preserve the sitemap cost_guides just wrote; replace contractor URLs."""
    blocks = []
    if os.path.exists(SITEMAP_PATH):
        with open(SITEMAP_PATH) as f:
            text = f.read()
        for block in re.findall(r"  <url>.*?</url>", text, flags=re.S):
            loc = re.search(r"<loc>(.*?)</loc>", block)
            if loc and loc.group(1).startswith(f"{SITE}/contractors/"):
                continue
            blocks.append(block)
    for url, priority in new_urls:
        blocks.append(
            f"  <url><loc>{url}</loc><changefreq>daily</changefreq>"
            f"<priority>{priority}</priority></url>")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    lines.extend(blocks)
    lines.append("</urlset>")
    with open(SITEMAP_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    with open(DATA_PATH) as f:
        data = json.load(f)
    records = data.get("permits", [])
    gen_date = (data.get("generated_at") or "")[:10] or \
        dt.date.today().isoformat()

    cells = build_cells(records)
    rankings = {}
    for cell in cells:
        indexed = [(i, r) for i, r in enumerate(cell["records"])]
        rankings[(cell["cat"]["id"], cell["place"])] = collect_groups(indexed)

    profiles = aggregate_profiles(records, cells, rankings)
    reserved = {cell["slug"] for cell in cells} | {"index"}
    profile_slugs = assign_profile_slugs(profiles, reserved)

    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR)

    with open(os.path.join(OUT_DIR, "index.html"), "w") as f:
        f.write(render_hub(cells, rankings, profiles, gen_date))

    for cell in cells:
        ranking = rankings[(cell["cat"]["id"], cell["place"])]
        d = os.path.join(OUT_DIR, cell["slug"])
        os.makedirs(d)
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write(render_ranking(cell, ranking, profile_slugs, cells,
                                   gen_date))

    for key in sorted(profiles, key=lambda k: profiles[k]["slug"]):
        p = profiles[key]
        d = os.path.join(OUT_DIR, p["slug"])
        os.makedirs(d)
        with open(os.path.join(d, "index.html"), "w") as f:
            f.write(render_profile(p, cells, gen_date))

    urls = [(f"{SITE}/contractors/", "0.9")]
    urls += [(f"{SITE}/contractors/{cell['slug']}/", "0.8")
             for cell in cells]
    urls += [(f"{SITE}/contractors/{profiles[k]['slug']}/", "0.6")
             for k in sorted(profiles, key=lambda k: profiles[k]["slug"])]
    update_sitemap(urls)

    print(f"contractor_pages: wrote hub + {len(cells)} ranking pages + "
          f"{len(profiles)} profiles from {len(records)} permits "
          f"(data generated {gen_date})")
    for cell in cells:
        ranking = rankings[(cell["cat"]["id"], cell["place"])]
        print(f"  ranking {cell['slug']}: {len(cell['records'])} permits, "
              f"{len(ranking)} named contractors, top20 shown")
    print(f"  profiles qualified: {len(profiles)}")


if __name__ == "__main__":
    main()
