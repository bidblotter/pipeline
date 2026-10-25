#!/usr/bin/env python3
"""Build PermitPicker homeowner cost-guide pages under web/costs/.

Reads the public export snapshot (web/data/permits.json, written by
export.py earlier in the ingest workflow) and emits static HTML pages:
a /costs/ index, one Triangle-wide ("Raleigh area") page per job
category, and one page per category x city cell with enough data.

Everything on the pages is an aggregate over permit records whose
`valuation` (the value the applicant declared on the permit application)
is > 0. Pages never show individual permits or street addresses.

Category definitions (v2)
--------------------------
Every category is a UNION of two arms:

  * Work-class arm: `work_class` matches exactly (deck "Deck",
    addition "Addition", pool "Pool, Spa & Hot Tub", accessory
    "Accessory Building"). These classes exist only in the Wake County
    building feed, whose exported rows carry no contractor names.
  * Description arm (deck / addition / pool / accessory only): the
    same work filed by the Raleigh and Cary feeds, which use generic
    work classes ("Existing", "New") and name the job in the permit
    description instead. Modeled on the remodel slices below.

Remodel categories (kitchen / bathroom) are description slices only.
A record qualifies when ALL of the following hold:
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

Description-arm guards for the four work-class categories
(_slice_guards) follow the remodel pattern:
  * valued and not Withdrawn;
  * work_class is not one of the four category classes (a record the
    county already classed stays in its own category — pool permits
    mentioning a deck do not leak into deck) and not "Alteration"
    (interior permits whose prose enumerates the whole house);
  * work_class not in REMODEL_EXCLUDE_WC — except that addition and
    accessory ALLOW "New": in the Raleigh/Cary taxonomy "New" is the
    class for a new addition or a new accessory structure, and the
    marker-phrase guard still bars whole-house builds. Deck and pool
    keep the full exclusion (their "New"-class hits were whole-house
    renovations mentioning a deck / pool houses);
  * building permits only: permit_type is not "Commercial Building"
    and not a trade / inspection / demolition / fire / sign /
    development type (a $60 pool-pump electrical permit is not a pool
    job);
  * no NEW_CONSTRUCTION_MARKERS.

Per-category description rules:
  * deck: the word "deck"/"decks", minus parking decks/facilities/
    garages and "pool deck" (the apron around a pool — those jobs are
    in pool). Combo permits that lead with other work ("ADDITION;
    REMODEL; DECK") still count, as the remodel slices already overlap
    each other.
  * pool: "pool"/"pools", "hot tub"/"hot tubs", "swim spa(s)" (bare
    "spa" is mostly salons and trade circuits), minus swim schools.
    Pool houses stay: the county classes them as accessory buildings,
    and a pool house description here lands in both slices.
  * addition: "addition" must be the SUBJECT, via ADDITION_SUBJECT
    (a room/sunroom/etc. addition, a sized addition, Cary's
    ledger-style lead "ADDITION, ...", "addition to the rear/side",
    "addition of a porch/sunroom/...", colon/paren forms, a following
    room noun, or a build verb + "addition" backed by a residential
    context word) — and the description carries no non-residential
    subject marker (ADDITION_NONRESI: church, warehouse, library,
    restaurant, ...). Bare boilerplate — "in addition", "addition of
    lighting and fans", "additional" — never qualifies, so deck and
    pool jobs that mention an "addition of" a component stay out.
  * accessory: an accessory-structure noun (garage, shed, workshop,
    carport, barn, pool house, or the phrases "accessory structure" /
    "accessory building") in a BUILD context (ACCESSORY_SUBJECT:
    detached/attached/storage/prefab modifiers, build verbs, or the
    ledger-style lead), minus dwellings and sales conversions
    (ACCESSORY_EXCLUDE: accessory dwelling units, duplexes, model-home
    sales offices). Garage conversions and in-garage services are not
    builds and mostly fall out on their own.

ALL categories also exclude status == "Withdrawn" and require
valuation > 0. Categories can overlap (a combined kitchen-and-bath
remodel appears in both; a garage addition appears in addition and
accessory) — each page is its own slice of the same records.

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
(normalize.contractor_key / is_contractor). "Median declared value" is
the contractor's median declared value within the set, shown only
with 5 or more valued permits.

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

# Google Analytics 4 (property created by Andrew 2026-10-23 under
# permitpicker@gmail.com). The tel: click listener is registered
# here once; quote-form and signup success handlers fire their own
# conversion events (quote_request / permitpulse_signup).
GA_HEAD = """<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-VW5K8L3BYS"></script>
<script>
window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('js', new Date());
gtag('config', 'G-VW5K8L3BYS');
document.addEventListener('click', function(e){
  var a = e.target && e.target.closest ? e.target.closest('a[href^="tel:"]') : null;
  if (a) { gtag('event', 'call_click', {phone: a.getAttribute('href').replace('tel:', ''), page_path: location.pathname}); }
});
</script>"""

# Sticky quote bar for cost-guide pages (quick pack, 2026-10-23):
# appears after scrolling past the hero, hides while the form itself
# is on screen, and reports clicks as GA4 quote_cta events.
STICKY_TMPL = """<div class="sticky-cta" id="stickyCta" hidden>
  <span class="sc-text"><b>__JOBCAP__ in __PLACE__</b> · typical __MED__</span>
  <a class="cta-btn quote-cta" data-cta="sticky" href="#get-quotes">Get quotes</a>
</div>
<script>
(function () {
  var bar = document.getElementById('stickyCta');
  var form = document.getElementById('get-quotes');
  if (!bar || !form) return;
  var formVisible = false;
  function sync() {
    var y = window.scrollY || document.documentElement.scrollTop || 0;
    bar.hidden = !(y > 420 && !formVisible);
  }
  window.addEventListener('scroll', sync, { passive: true });
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) {
      es.forEach(function (e) { formVisible = e.isIntersecting; });
      sync();
    }).observe(form);
  }
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a.quote-cta') : null;
    if (a && window.gtag) gtag('event', 'quote_cta', { cta_location: a.getAttribute('data-cta'), page_path: location.pathname });
  });
  sync();
})();
</script>"""
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


# ---------------------------------------------------------------------------
# Description arms for the work-class categories (see module docstring)
# ---------------------------------------------------------------------------

CATEGORY_WORK_CLASSES = {
    "Deck", "Addition", "Pool, Spa & Hot Tub", "Accessory Building",
}


def _nonbuilding_permit_type(pt):
    """Trade/inspection/demolition/etc. permits are components of a job,
    not the job itself (e.g. a $60 pool-pump electrical permit)."""
    p = pt or ""
    if p == "Commercial Building" or p.startswith("DSINSP"):
        return True
    pl = p.lower()
    return any(t in pl for t in ("electrical", "plumbing", "mechanical",
                                 "demolition", "fire", "sign", "dev"))


def _slice_guards(r, allow_new):
    """Shared guards for the description arms. allow_new lets addition
    and accessory accept the Raleigh/Cary "New" work class (a new
    addition / accessory structure there); the marker phrases still bar
    whole-house construction."""
    if not _live(r):
        return False
    wc = r.get("work_class") or ""
    if wc in CATEGORY_WORK_CLASSES or wc == "Alteration":
        return False
    exclude_wc = REMODEL_EXCLUDE_WC - ({"New"} if allow_new else set())
    if wc in exclude_wc:
        return False
    if _nonbuilding_permit_type(r.get("permit_type")):
        return False
    desc = (r.get("description") or "").lower()
    return not any(m in desc for m in NEW_CONSTRUCTION_MARKERS)


def _desc_text(raw):
    """Lowercased description with 'sq. ft.'-style abbreviations folded
    so size patterns match across the abbreviation's periods."""
    return re.sub(r"sq\.?\s?ft\.?", "sqft", (raw or "").lower())


def _desc_slice(keyword_rx, allow_new=False, extra=None):
    """extra receives the normalized description and must also pass."""
    def match(r):
        if not _slice_guards(r, allow_new):
            return False
        desc = _desc_text(r.get("description"))
        return bool(keyword_rx.search(desc)) and \
            (extra is None or extra(desc))
    return match


def _union(work_class, slice_match):
    wc_match = _work_class(work_class)
    return lambda r: wc_match(r) or slice_match(r)


# --- deck -------------------------------------------------------------------

DECK_EXCLUDE = ("parking deck", "parking facility", "parking garage",
                "pool deck")

# --- pool -------------------------------------------------------------------

POOL_EXCLUDE = ("swim school",)

# --- addition ---------------------------------------------------------------

_ADD_NOUN = (r"(room|rm|sunroom|bedroom|bathroom|kitchen|primary|master|"
             r"den|mudroom|garage|porch|suite|nook|laundry|closet|office|"
             r"study|attic|basement|dining|floor|space)")
_ADD_SIZE = r"\d[\d,]*\s*(sqft|sf|square[-\s]?f\w*)"
ADDITION_SUBJECT = re.compile(
    r"\b" + _ADD_NOUN + r"\s+addition\b"                      # room addition
    r"|\baddition\b[^.]{0,45}?" + _ADD_SIZE +                 # addition of 500 sqft
    r"|" + _ADD_SIZE + r"[^.]{0,45}?\baddition\b"              # 783sqft addition
    r"|\baddition is\b"                                       # addition is 12x24
    r"|^\s*addition\b"                                        # Cary ledger lead
    r"|\b(new|proposed|residential|single-story|two-story|2-story|1-story|"
    r"story|attached|detached|rear|side|front|upper|heated|conditioned|"
    r"open)\s+addition\b"                                     # rear addition
    r"|\baddition\s+(off|to|on|onto)\s+the\s+"
    r"(rear|side|front|back|left|right)\b"                    # addition to the rear
    r"|\baddition\s+of\s+(a\s+|an\s+|the\s+)?(screened?\s+)?"
    r"(porch|sunroom|bedroom|bathroom|garage|room|deck|patio|suite|closet)\b"
    r"|\baddition\s*[:(&]"                                    # ADDITION: / (EXT...
    r"|\baddition\b[^.]{0,60}?\b(bathroom|bedroom|kitchen|suite|closet)\b")
ADDITION_VERB = re.compile(
    r"\b(add|adds|adding|construct|build|building|extend|extending)\b"
    r"[^.]{0,45}?\baddition\b")
ADDITION_RES_CONTEXT = re.compile(
    r"\b(house|home|dwelling|residence|residential|bedroom|bathroom|"
    r"kitchen|suite|family|living|porch|garage|sunroom|closet|laundry|"
    r"mudroom|den|room|attic|basement)\b")
ADDITION_NONRESI = (
    "warehouse", "church", "sanctuary", "narthex", "library",
    "restaurant", "pemb", "school", "retail", "storefront",
    "industrial", "tenant", "office building",
)


def _addition_subject(desc):
    hits = [rx for rx in (ADDITION_SUBJECT, ADDITION_VERB) if rx.search(desc)]
    if not hits:
        return False
    # A lone build-verb match ("add an addition to an existing
    # structure") needs a residential context word somewhere in the
    # description; subject-pattern matches stand on their own.
    if hits == [ADDITION_VERB] and not ADDITION_RES_CONTEXT.search(desc):
        return False
    return not any(m in desc for m in ADDITION_NONRESI)


# --- accessory building -----------------------------------------------------

_ACC_NOUN = r"(garage|shed|workshop|carport|barn|pool house)"
ACCESSORY_SUBJECT = re.compile(
    r"\baccessory\s+(structure|building)\b"
    r"|\b(detached|attached|new|prefab|prefabricated|pre-?manufactured|"
    r"prebuilt|free ?standing|metal|steel|storage|residential|enclosed)"
    r"\s+" + _ACC_NOUN + r"\b"
    r"|\bdetached\b[^.]{0,25}?\b" + _ACC_NOUN + r"\b"
    r"|\b(construct|build|erect)\w*\b[^.]{0,40}?\b" + _ACC_NOUN + r"\b"
    r"|\b(prefab|prefabricated|pre-?manufactured|prebuilt)\b[^.]{0,30}?\b"
    r"(garage|shed|building|structure)\b"
    r"|\b" + _ACC_NOUN + r"\s+addition\b"
    r"|^\s*(detached\s+|attached\s+)?" + _ACC_NOUN + r"\b")
ACCESSORY_EXCLUDE = (
    "accessory dwelling", "dwelling", "quadplex", "duplex",
    "sales office", "sales center", "nsfd",
)


def _accessory_subject(desc):
    return bool(ACCESSORY_SUBJECT.search(desc)) and \
        not any(m in desc for m in ACCESSORY_EXCLUDE)


def _screened_porch_note(records, place):
    """Combos disclosure: most screened porches here were permitted
    together with a deck, and the declared value covers both."""
    deck = next(c for c in CATEGORIES if c["id"] == "deck")
    both = sum(1 for r in records if deck["match"](r))
    if not both:
        return None
    return (f"Many screened porches are permitted together with a deck: "
            f"{both} of the {len(records)} jobs on this page also appear "
            f"in our deck cost guide, and the declared value covers the "
            f"combined project, not the porch alone.")


def _solar_note(records, place):
    """Cary-heavy disclosure (Andrew approved, labeled, 2026-10-22):
    Raleigh/Wake solar jobs are trade permits with no declared value,
    so the valued records behind this guide are almost all Cary's."""
    total = len(records)
    cary = sum(1 for r in records if r.get("source_feed") == "cary")
    if not total:
        return None
    return (f"Solar permits in Raleigh and Wake County are trade permits "
            f"that do not declare a project value, so the valued records "
            f"behind this guide come almost entirely from Cary — {cary} "
            f"of these {total} jobs are Cary permits. Treat these "
            f"figures as Cary data, not a Raleigh-area average.")


SOLAR_FAQ_SOURCE = (
    "They are computed from public permit records — with one gap: "
    "Raleigh and Wake County solar jobs are permitted as trade permits "
    "that do not declare a project value, so nearly all valued solar "
    "records are Cary building permits. See the data note on this page.")


CATEGORIES = [
    {"id": "deck", "job": "deck", "label": "Deck",
     "match": _union("Deck", _desc_slice(
         re.compile(r"\bdecks?\b"),
         extra=lambda d: not any(m in d for m in DECK_EXCLUDE)))},
    {"id": "addition", "job": "home addition", "label": "Home addition",
     "match": _union("Addition", _desc_slice(
         re.compile(r"\baddition\b"), allow_new=True,
         extra=_addition_subject))},
    {"id": "pool", "job": "pool", "label": "Pool, spa & hot tub",
     "match": _union("Pool, Spa & Hot Tub", _desc_slice(
         re.compile(r"\bpools?\b|\bhot tubs?\b|\bswim spas?\b"),
         extra=lambda d: not any(m in d for m in POOL_EXCLUDE)))},
    {"id": "accessory-building", "job": "accessory building",
     "label": "Accessory building (garage, shed, workshop)",
     "match": _union("Accessory Building", _desc_slice(
         re.compile(r"\b(accessory|garages?|sheds?|workshops?|carports?|"
                    r"barns?|pool house)\b"),
         allow_new=True, extra=_accessory_subject))},
    {"id": "kitchen-remodel", "job": "kitchen remodel",
     "label": "Kitchen remodel", "match": _remodel("kitchen")},
    {"id": "bathroom-remodel", "job": "bathroom remodel",
     "label": "Bathroom remodel", "match": _remodel("bath")},
    # Screened porch (added 2026-10-22, expansion audit): description
    # slice only, same guards as the other slices. Pool enclosures are
    # a different product and are excluded. Most hits are porch+deck
    # combos — the page carries a data note saying so.
    {"id": "screened-porch", "job": "screened porch",
     "label": "Screened porch",
     "match": _desc_slice(
         re.compile(r"\b(screened|screen)\s+(porch|room|enclosure)\b"
                    r"|\bporch\s+(enclosure|screen)"),
         extra=lambda d: "pool" not in d),
     "data_note": _screened_porch_note},
    # Solar (added 2026-10-22, expansion audit; Andrew approved with
    # the Cary-heavy label): description slice only. Wake's
    # "Photovoltaic (Solar Panel)" work-class records are trade
    # permits with $0 declared values, so the valued slice is
    # effectively Cary building permits — disclosed on the page via
    # data_note and a category-specific FAQ source answer.
    {"id": "solar", "job": "solar panel system",
     "label": "Solar panel system",
     "match": _desc_slice(re.compile(r"\bsolar\b|\bphotovoltaic\b")),
     "data_note": _solar_note, "faq_source": SOLAR_FAQ_SOURCE},
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

MIN_VALUED_FOR_MEDIAN = 5


def median_cell(c):
    """Per-contractor median display: suppressed ("—") when based on
    fewer than MIN_VALUED_FOR_MEDIAN valued permits."""
    n = c.get("n_vals", c.get("count", 0))
    return money(c["median"]) if n >= MIN_VALUED_FOR_MEDIAN else "—"


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
        # Median uses valued permits only: missing/zero declarations
        # must not drag the contractor's median down. Count still
        # counts every grouped record (see "count" below).
        try:
            v = float(r["valuation"])
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            g["vals"].append(v)
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
            "name": name, "count": len(g["names"]),
            "median": percentile(vals, 0.5), "n_vals": len(vals),
            "phone": phone,
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
  .hero-cta { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin: 2px 0 18px; }
  .cta-btn { display: inline-block; background: #0d7a6f; color: #fff; padding: 12px 22px; border-radius: 8px; text-decoration: none; font-weight: 700; font-size: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.18); }
  .cta-btn:hover { background: #0b6a60; }
  .cta-note { font-size: 13px; color: #55606a; }
  @media (prefers-color-scheme: dark) { .cta-note { color: #9fb0c0; } }
  .quote .trust { font-size: 12.5px; opacity: .85; margin: 2px 0 0; line-height: 1.45; }
  body.guide { padding-bottom: 64px; }
  .sticky-cta { position: fixed; left: 0; right: 0; bottom: 0; z-index: 60; background: #102e36; color: #fff; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px calc(10px + env(safe-area-inset-bottom)); box-shadow: 0 -2px 10px rgba(0,0,0,.25); }
  .sticky-cta .sc-text { font-size: 13.5px; line-height: 1.35; }
  .sticky-cta .cta-btn { padding: 9px 16px; font-size: 14.5px; white-space: nowrap; box-shadow: none; }
  @media (max-width: 640px) { .hero-cta .cta-btn { width: 100%; text-align: center; } }
  #q-msg { font-size: 13px; min-height: 18px; margin-top: 4px; }
  #q-msg.ok { color: #7ce3a5; }
  #q-msg.err { color: #ff9d9d; }
  .faq h3 { font-size: 15px; margin: 14px 0 4px; }
  .faq h3:first-of-type { margin-top: 2px; }
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
  <p>Raleigh permit intelligence — live data, refreshed daily · <a href="/costs/">Homeowner cost guides</a> · <a href="/permitpulse/">PermitPulse for pros</a></p>
</header>"""

FOOTER = """<footer>PermitPicker — recent Wake County, Cary &amp; Raleigh filings. Public records, refreshed daily.<br>
Cost figures are permit-declared values from public records — see the methodology note on each guide.</footer>"""


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def ld_script(obj):
    """Serialize a schema.org object as a JSON-LD script tag."""
    payload = json.dumps(obj, indent=2, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")
    return f'<script type="application/ld+json">\n{payload}\n</script>'


def breadcrumb_ld(items):
    """items: [(name, url), ...] -> BreadcrumbList schema object."""
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name,
             "item": url}
            for i, (name, url) in enumerate(items)],
    }


def faq_for(cat, place, n, med, p25, p75, span, source_answer=None):
    """Data-backed FAQs for a cost guide. Every number in an answer comes
    from this page's own slice — nothing is invented, and no permit-law
    thresholds are asserted (rules vary by jurisdiction and job)."""
    job = cat["job"]
    return [
        (f"How much does a {job} cost in {place}?",
         f"The typical (median) declared value for a permitted {job} in "
         f"{place} is {money(med)}, based on {n} permitted jobs filed "
         f"{span}. Most jobs — the middle 50% — fall between "
         f"{money(p25)} and {money(p75)}."),
        ("Where do these cost figures come from?",
         source_answer or
         "They are computed from public permit records filed in Wake "
         "County, Raleigh & Cary. Each figure is the value the applicant "
         "declared on the permit application for that job, aggregated "
         "across every matching permitted job counted on this page. "
         "Figures refresh daily."),
        ("Are declared permit values the same as final project costs?",
         "No. A declared value is a pre-job estimate stated when the "
         "permit is applied for, and permit fees scale with that number, "
         "so declared values tend to run below the final invoice. Use "
         "them as a local market anchor for budgeting, not as a quote."),
        (f"Why might a quote for my {job} be higher or lower than the "
         f"typical figure?",
         "Size, materials, site conditions, finish level, and any "
         "structural, electrical, or plumbing work involved all move the "
         "price. Combined projects also count here, so a job built as "
         "part of a larger remodel may carry a higher declared value "
         "than a standalone job."),
        ("How do I get quotes from contractors doing this work locally?",
         f"Use the quote-request form on this page, or contact a "
         f"contractor from the {place} activity ranking linked below. "
         f"Rankings are based only on recent permitted activity in "
         f"public records — not reviews, quality scores, or paid "
         f"placement."),
    ]


def render_faq(faqs):
    rows = "\n".join(
        f"      <h3>{esc(q)}</h3>\n      <p>{esc(a)}</p>" for q, a in faqs)
    return f"""  <div class="card faq">
    <h2>Frequently asked questions</h2>
{rows}
  </div>"""


def render_form(cat, place, city_value, p25, med, p75, n_contractors=0):
    """Quote-request CTA. Backend: see LEAD_ENDPOINT TODO in the script."""
    brackets = [
        f"Under {money(p25)}",
        f"{money(p25)} – {money(med)}",
        f"{money(med)} – {money(p75)}",
        f"Over {money(p75)}",
        "Not sure yet",
    ]
    opts = "\n".join(f'          <option>{esc(b)}</option>' for b in brackets)
    proof = (f" {n_contractors} contractors pulled {cat['job']} permits "
             f"in {esc(place)} recently, so quotes come from people "
             f"actually doing this work here.") if n_contractors >= 3 else ""
    # NO phone reveal (Andrew's call, 2026-10-23, "watertight"):
    # contractor numbers never ship in the page — not printed, not
    # encoded. The submission routes through the lead router;
    # contractors contact the homeowner, never the reverse.
    return f"""<section class="quote" id="get-quotes">
  <h2>Get quotes for your {esc(cat['job'])}</h2>
  <p>Free for homeowners. Your request goes to local
  contractors who actively do this work in {esc(place)} — never blasted to a
  list. Contractors reply directly; there's no obligation to hire anyone.{proof}</p>
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
      <span>By submitting, I agree that matched local contractors may contact me about my project by phone, text, or email.</span></label>
    <button type="submit">Get my free quotes</button>
    <p class="trust">Free · No obligation · Your number goes only to matched local contractors — never sold, never shared with anyone else.</p>
    <p id="q-msg"></p>
  </form>
</section>
<script>
// Backend: posts to the Supabase `quote_requests` table (created via
// sql/quote_requests.sql; public-insert RLS policy, reads locked down).
const LEAD_ENDPOINT = "{SUPABASE_URL}/rest/v1/quote_requests";
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
      if (window.gtag) gtag("event", "quote_request", {{job_type: payload.job_type, city: payload.city, page_path: location.pathname}});
      msg.textContent = "Request sent — matched local contractors can now get in touch about your project.";
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
          <td>{c['count']}</td><td>{median_cell(c)}</td>
          <td><a class="call" href="#get-quotes">Get contact details</a></td></tr>"""
            for c in contractors)
        co_html = f"""<div class="card">
    <h2>Contractors actively doing this work in {esc(place)}</h2>
    <table class="co">
      <tr><th>Contractor</th><th>Permits here</th><th>Median declared value</th><th></th></tr>
{rows_html}
    </table>
    <p class="rank-note">Ranked by recent permit activity from public records — not advertising.
    “Median declared value” is the median declared value of that contractor's {esc(job)} permits in this set. Shown only where based on 5 or more valued permits.</p>
    <p class="rank-note"><a href="/contractors/{slug}/">See the full activity ranking for {esc(place)}</a></p>
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
    n_contractors = len(top_contractors(records, limit=100000))
    form_html = render_form(cat, place, city_value, p25, med, p75,
                            n_contractors=n_contractors)
    sticky_html = (STICKY_TMPL
                   .replace("__JOBCAP__", esc(job[:1].upper() + job[1:]))
                   .replace("__PLACE__", esc(place))
                   .replace("__MED__", money(med)))
    faqs = faq_for(cat, place, n, med, p25, p75, span,
                   source_answer=cat.get("faq_source"))
    note_fn = cat.get("data_note")
    note = note_fn(records, place) if note_fn else None
    note_html = (f'    <p><b>Data note.</b> {esc(note)}</p>\n'
                 if note else "")
    faq_html = render_faq(faqs)
    ld_graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": title, "description": desc,
             "url": url,
             "isPartOf": {"@type": "WebSite", "name": "PermitPicker",
                          "url": f"{SITE}/"}},
            breadcrumb_ld([
                ("PermitPicker", f"{SITE}/"),
                ("Cost guides", f"{SITE}/costs/"),
                (f"{cat['label']} — {place}", url)]),
            {"@type": "Dataset",
             "name": f"{cat['label']} costs in {place} — declared permit values",
             "description": desc, "url": url,
             # Compiled from NC public records (N.C.G.S. ch. 132:
             # property of the people); dedicated to the public
             # domain so the dataset itself is freely reusable.
             "license": "https://creativecommons.org/publicdomain/zero/1.0/",
             "creator": {"@type": "Organization", "name": "PermitPicker",
                         "url": f"{SITE}/"},
             "spatialCoverage": place,
             "temporalCoverage": f"{lo}/{hi}"},
            {"@type": "FAQPage",
             "mainEntity": [
                 {"@type": "Question", "name": q,
                  "acceptedAnswer": {"@type": "Answer", "text": a}}
                 for q, a in faqs]},
        ],
    }
    ld_html = ld_script(ld_graph)
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
{GA_HEAD}
{ld_html}
<style>{CSS}</style>
</head>
<body class="guide">
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
  <div class="hero-cta">
    <a class="cta-btn quote-cta" data-cta="hero" href="#get-quotes">Get {esc(job)} quotes in {esc(place)}</a>
    <span class="cta-note">Free · no obligation · matched local contractors, never a list</span>
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
{note_html}  </div>
{co_html}
{form_html}
{faq_html}
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
{sticky_html}
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
    item_list = []
    pos = 0
    for cell in cells:
        pos += 1
        item_list.append({
            "@type": "ListItem", "position": pos,
            "name": f"{cell['cat']['label']} cost in the {REGION_NAME}",
            "url": f"{SITE}/costs/{cell['cat']['id']}/"})
        for place, slug, _st in cell["cities"]:
            pos += 1
            item_list.append({
                "@type": "ListItem", "position": pos,
                "name": f"{cell['cat']['label']} cost in {place}",
                "url": f"{SITE}/costs/{slug}/"})
    ld_html = ld_script({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "CollectionPage",
             "name": "Homeowner cost guides — PermitPicker",
             "url": f"{SITE}/costs/",
             "isPartOf": {"@type": "WebSite", "name": "PermitPicker",
                          "url": f"{SITE}/"}},
            breadcrumb_ld([("PermitPicker", f"{SITE}/"),
                           ("Cost guides", f"{SITE}/costs/")]),
            {"@type": "ItemList", "itemListElement": item_list},
        ],
    })
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
{GA_HEAD}
{ld_html}
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


# Only canonical URLs belong in the sitemap. The permit explorer's
# ?type= filter URLs all canonical back to the homepage, and listing
# them caused Search Console "Alternate page with proper canonical
# tag" coverage warnings (flagged 2026-10-12, fixed 2026-10-22).
STATIC_SITEMAP_URLS = [
    ("https://permitpicker.com/", "daily", "1.0"),
    ("https://permitpicker.com/permitpulse/", "weekly", "0.7"),
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
