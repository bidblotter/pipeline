#!/usr/bin/env python3
"""Email Andrew when new homeowner quote requests land in Supabase.

The cost-guide pages (permitpicker.com/costs/) insert quote requests into
the Supabase `quote_requests` table (public-insert-only RLS). This job
polls for rows with id greater than a watermark, emails a work-order
digest to Andrew (lead details + a shortlist of top-matching local
contractors for each lead), and only then advances the watermark.

Routing: since 2026-10-23 the job also ROUTES each lead — fit-scored
delivery to subscribers first, then freebie-eligible contractors (max
2 recipients), emailed automatically where the roster carries an
address; phone-only recipients are flagged in Andrew's report for a
manual text. Dispatches and freebie marks are written back to the
roster by this job (the workflow commits it with the watermark).

Roster: state/lead_roster.json lists who already got a free lead
("freebies"), who subscribes ("subscribers" — one all-access
subscription, trades/cities on an entry are delivery preferences,
not gates), and every lead dispatch ("dispatches") with its two
follow-up checks: homeowner at +2 days, contractor outcome at +7.
Andrew maintains it by telling Muse ("sent lead 12 to X", "X won
lead 12"); the alert badges shortlist entries from it, floats
subscribers to the top, and surfaces due follow-ups. Follow-ups
ride new-lead alerts, plus a standalone reminder email at most once
per day (throttled via the state file). The roster is read-only
here — the workflow never writes it.

State: state/lead_alerts.json in this repo, {"last_alerted_id": N}. The
lead-alerts workflow commits the file back after a run in which it
changed. The watermark advances ONLY after the Resend send succeeds —
a failed send exits non-zero and the same rows are retried next run.
With no new rows the job exits 0 quietly: no email, no state change.

Contractor shortlists use the exact category definitions from
cost_guides.py (imported, not copied) over the published dataset,
then fit-score the contractors privately for THIS lead (gates:
phone on file, a permit in the last 120 days, 3+ permits in the
last year — relaxed in stages for thin cells; points for the
3–15/yr volume sweet spot, typical-job fit to the lead's budget,
and recency; subscribers first). The public site's rankings are
untouched and remain pure permit activity.
The dataset is fetched from the live site because web/data/ is not in
git (same pattern as send-pitch.yml); a local path may be passed for
testing. Shortlists are best-effort: if the dataset cannot be loaded,
the alert still goes out, with a note in place of the shortlist.

Env (GitHub Actions secrets): SUPABASE_URL, SUPABASE_SERVICE_KEY,
RESEND_API_KEY. Stdlib only.

Usage:
  python pipeline/lead_alerts.py [--state state/lead_alerts.json]
      [--permits https://permitpicker.com/data/permits.json]
      [--to andrew@permitpicker.com]
"""
import argparse
import re
from collections import Counter
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cost_guides as cg  # noqa: E402  (category rules + contractor ranking)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_STATE = os.path.join(ROOT, "state", "lead_alerts.json")
PERMITS_URL = "https://permitpicker.com/data/permits.json"
FROM_ADDR = "andrew@permitpicker.com"
REPLY_TO = "leads@permitpicker.com"  # contractor replies land here
DELIVERY_BCC = "andrew@permitpicker.com"  # Andrew sees every delivery
DEFAULT_TO = "andrew@permitpicker.com"


# ---------------------------------------------------------------------------
# Supabase (same REST pattern as notify_signups.py, service-key access)
# ---------------------------------------------------------------------------

def sb_get(path, params=""):
    url = f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/{path}{params}"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    req = urllib.request.Request(url, headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else []


def fetch_new_leads(watermark):
    q = urllib.parse.urlencode({
        "select": ("id,name,phone,email,city,job_type,timeline,budget,"
                   "details,source_page,created_at"),
        "id": f"gt.{watermark}",
        "order": "id.asc",
        "limit": "200",
    })
    return sb_get("quote_requests", params="?" + q)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path):
    """{"last_alerted_id": N, "followup_reminded": {key: "YYYY-MM-DD"}}.
    Tolerates the v1 file, which held only the watermark."""
    try:
        with open(path) as f:
            data = json.load(f)
        return {"last_alerted_id": int(data.get("last_alerted_id", 0)),
                "followup_reminded": data.get("followup_reminded", {})}
    except (OSError, ValueError):
        return {"last_alerted_id": 0, "followup_reminded": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=1)
        f.write("\n")


# ---------------------------------------------------------------------------
# Dataset + shortlist
# ---------------------------------------------------------------------------

def load_permits(src):
    if os.path.exists(src):
        with open(src) as f:
            data = json.load(f)
    else:
        req = urllib.request.Request(src, headers={
            "User-Agent": "PermitPicker lead-alerts (pipeline)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
    return data["permits"] if isinstance(data, dict) else data


def find_category(job_type):
    """Map a lead's job_type (the form sends the category label, e.g.
    'Deck', 'Home addition', 'Kitchen remodel') to a cost_guides
    category. Tolerates ids ('deck') and loose casing."""
    jt = (job_type or "").strip().lower()
    if not jt:
        return None
    for cat in cg.CATEGORIES:
        if jt in (cat["id"], cat["label"].lower(), cat["job"].lower()):
            return cat
    for cat in cg.CATEGORIES:
        if jt in cat["label"].lower() or jt in cat["job"].lower():
            return cat
    return None


# ---------------------------------------------------------------------------
# Roster (freebies + subscribers) — state/lead_roster.json, maintained by
# Andrew via Muse; read-only in this job. Entries:
#   {"contractor": name, "phone": digits-or-formatted,
#    "category": cost_guides id or "all", "city": display city or "",
#    "date"/"since": "YYYY-MM-DD", "status": "active" (subscribers)}
# ---------------------------------------------------------------------------

DEFAULT_ROSTER = os.path.join(ROOT, "state", "lead_roster.json")


def load_roster(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return {"freebies": data.get("freebies", []),
                "subscribers": data.get("subscribers", []),
                "dispatches": data.get("dispatches", []),
                "stripe_lead_link": data.get("stripe_lead_link", "")}
    except (OSError, ValueError):
        return {"freebies": [], "subscribers": [], "dispatches": [],
                "stripe_lead_link": ""}


def save_roster(path, roster):
    """Written back by the router (auto deliveries, freebie marks,
    dispatch log). The lead-alerts workflow commits it alongside the
    watermark state."""
    data = {"freebies": roster.get("freebies", []),
            "subscribers": roster.get("subscribers", []),
            "dispatches": roster.get("dispatches", [])}
    if roster.get("stripe_lead_link"):
        data["stripe_lead_link"] = roster["stripe_lead_link"]
    with open(path, "r") as f:
        existing = json.load(f)
    for k, v in existing.items():
        data.setdefault(k, v)
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")


def _norm_name(name):
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _digits(phone):
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _roster_match(entries, contractor):
    """First roster entry matching this contractor by phone digits,
    else by normalized name. None when no match."""
    cphone = _digits(contractor.get("phone"))
    cname = _norm_name(contractor.get("name"))
    for e in entries:
        ephone = _digits(e.get("phone"))
        if cphone and ephone and cphone[-10:] == ephone[-10:]:
            return e
        if cname and cname == _norm_name(e.get("contractor")):
            return e
    return None


def _cell_relevant(entry, cat):
    """All-access model: a subscriber is relevant to every lead. A
    trades preference on the entry (category set to one trade) only
    means 'prefers this work' — it never gates delivery."""
    ec = (entry.get("category") or "all").strip().lower()
    return ec in ("all", "", cat["id"], cat["label"].lower())


def roster_badge(contractor, cat, roster):
    """Short status tag for a shortlist line, or '' when the contractor
    has no roster history (their free lead is still available)."""
    sub = _roster_match(
        [e for e in roster["subscribers"]
         if (e.get("status") or "active") == "active"], contractor)
    if sub is not None:
        badge = " ★ SUBSCRIBER"
        if sub.get("priority_next"):
            badge += " ⚑ priority (dead-lead credit)"
        ec = (sub.get("category") or "all").strip().lower()
        if ec not in ("all", "") and not _cell_relevant(sub, cat):
            badge += " (prefers other work)"
        return badge
    free = _roster_match(roster["freebies"], contractor)
    if free is not None:
        return f" ✓ free lead sent {free.get('date') or ''}".rstrip()
    return ""


def _parse_budget(text):
    """Lead budget is one of the form's brackets ('Under $25,000',
    '$25,000 – $41,000', 'Over $59,000', 'Not sure yet'). Returns
    (lo, hi) in dollars, either side None when open-ended, or None
    when the budget carries no numbers."""
    nums = [float(x.replace(",", ""))
            for x in re.findall(r"\$([\d,]+)", text or "")]
    if not nums:
        return None
    t = (text or "").lower()
    if t.startswith("under"):
        return (None, nums[0])
    if t.startswith("over"):
        return (nums[0], None)
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return None


def _contractor_stats(records):
    """Per-contractor aggregates over a record set (same grouping as
    cost_guides.top_contractors, plus dates for recency/volume)."""
    groups = {}
    for r in records:
        raw = (r.get("contractor") or "").strip()
        if not cg.is_contractor(raw):
            continue
        key = cg.contractor_key(raw)
        if not key:
            continue
        g = groups.setdefault(key, {"names": [], "displays": [],
                                    "vals": [], "phones": Counter(),
                                    "dates": []})
        g["names"].append(raw)
        if r.get("contractor_display"):
            g["displays"].append(r["contractor_display"])
        try:
            g["vals"].append(float(r["valuation"]))
        except (TypeError, ValueError):
            pass
        ph = (r.get("contractor_phone") or "").strip()
        if ph:
            g["phones"][ph] += 1
        d = cg.record_date(r)
        if d:
            g["dates"].append(d)
    out = []
    for key, g in groups.items():
        name = (Counter(g["displays"]).most_common(1)[0][0]
                if g["displays"] else cg.display_for(key, g["names"]))
        if not name:
            continue
        vals = sorted(g["vals"])
        out.append({
            "name": name,
            "phone": (g["phones"].most_common(1)[0][0]
                      if g["phones"] else ""),
            "count": len(g["dates"]) or len(g["names"]),
            "median": vals[len(vals) // 2] if vals else 0.0,
            "latest": max(g["dates"]) if g["dates"] else "",
            "dates": g["dates"],
        })
    return out


def _fit_score(c, as_of, budget):
    """Transparent points, Andrew's criteria (2026-10-23): medium
    volume is the sweet spot; job value scores as a MATCH to the
    lead's budget; recency breaks toward active-now. Returns
    (score, [reason phrases])."""
    pts, why = 0, []
    d12 = [d for d in c["dates"]
           if (as_of - dt.date.fromisoformat(d)).days <= 365] \
        if c["dates"] else []
    n12 = len(d12)
    c["count_12m"] = n12
    if 3 <= n12 <= 15:
        pts += 3
        why.append(f"{n12} permits in the last year (sweet spot)")
    elif 16 <= n12 <= 25:
        pts += 1
        why.append(f"{n12} permits in the last year (large firm)")
    elif n12 > 25:
        why.append(f"{n12} permits in the last year (very large firm)")
    elif n12:
        why.append(f"{n12} permit(s) in the last year (light presence)")
    if budget and c["median"]:
        lo, hi = budget
        med = c["median"]
        if (lo is None or med >= lo) and (hi is None or med <= hi):
            pts += 3
            why.append(f"typical job {cg.money(med)} fits the budget")
        else:
            edge = hi if (hi is not None and med > hi) else lo
            if edge and abs(med - edge) <= 0.25 * edge:
                pts += 1
                why.append(f"typical job {cg.money(med)} near the budget")
            else:
                why.append(f"typical job {cg.money(med)} off the "
                           "budget band")
    elif c["median"]:
        why.append(f"typical job {cg.money(c['median'])}")
    if c["latest"]:
        age = (as_of - dt.date.fromisoformat(c["latest"])).days
        c["age_days"] = age
        if age <= 45:
            pts += 2
            why.append(f"last permit {age}d ago")
        elif age <= 120:
            pts += 1
            why.append(f"last permit {age}d ago")
        else:
            why.append(f"last permit {age}d ago (gone quiet)")
    return pts, why


def _passes(c, stage):
    """Gates by relaxation stage. Stage 0 = Andrew's gates (phone,
    active within 120 days, 3+ permits in the last year). Higher
    stages relax so thin cells still produce a pick list."""
    if stage == 0:
        return bool(c["phone"]) and c.get("age_days", 9999) <= 120 \
            and c.get("count_12m", 0) >= 3
    if stage == 1:
        return bool(c["phone"]) and c.get("age_days", 9999) <= 180 \
            and c.get("count_12m", 0) >= 1
    return bool(c["phone"])


def fit_shortlist(cat, city_raw, records, lead):
    """Fit-scored contractor picks for one lead. Returns
    (scope_label, relaxed_note, [(contractor, score, reasons)]).
    City-scoped when the lead's city has category records, else
    region-wide (same scoping as the old count ranking)."""
    matched = [r for r in records if cat["match"](r)]
    all_dates = [cg.record_date(r) for r in records if cg.record_date(r)]
    as_of = dt.date.fromisoformat(max(all_dates)) if all_dates \
        else today_et()
    budget = _parse_budget((lead or {}).get("budget"))
    disp = cg.city_display(city_raw or "")
    pool, scope = matched, cg.REGION_NAME
    if disp:
        city_recs = [r for r in matched
                     if cg.city_display(r.get("city")) == disp]
        if city_recs:
            pool, scope = city_recs, disp
    stats = _contractor_stats(pool)
    scored = []
    for c in stats:
        pts, why = _fit_score(c, as_of, budget)
        scored.append((c, pts, why))
    for stage in (0, 1, 2):
        kept = [x for x in scored if _passes(x[0], stage)]
        if kept:
            note = "" if stage == 0 else (
                " — thin local field, gates relaxed"
                if stage == 1 else
                " — very thin field, showing any contractor with a "
                "phone on file")
            kept.sort(key=lambda x: (-x[1], -x[0].get("count_12m", 0),
                                     x[0].get("age_days", 9999)))
            return scope, note, kept[:8]
    return scope, "", []


def shortlist(cat, city_raw, records, roster, lead=None):
    """Subscriber-first ordering over the fit-scored pool (Andrew's
    delivery rule), then fit score. Returns (scope_label, note, top)
    where top is [(contractor, score, reasons)] capped at 5."""
    scope, note, scored = fit_shortlist(cat, city_raw, records, lead)

    def sub_entry(c):
        return _roster_match(
            [x for x in roster["subscribers"]
             if (x.get("status") or "active") == "active"], c)

    subs = [x for x in scored if sub_entry(x[0]) is not None]
    subs.sort(key=lambda x: 0 if (sub_entry(x[0]) or {}).get(
        "priority_next") else 1)
    rest = [x for x in scored if sub_entry(x[0]) is None]
    return scope, note, (subs + rest)[:5]


def format_shortlist(cat, city_raw, records, roster, lead=None):
    scope, note, top = shortlist(cat, city_raw, records, roster, lead)
    if city_raw and scope == cg.REGION_NAME:
        head = (f"Top matching contractors — {scope} "
                f"(region-wide; no {city_raw} ranking){note}:")
    else:
        head = f"Top matching contractors — {scope}{note}:"
    if not top:
        return [head, "  (no contractor names in the published permit "
                      "data for this category)"]
    lines = [head,
             "  (fit-scored for this lead: phone + active + 3–15 "
             "permits/yr preferred, typical job matched to budget; "
             "subscribers first)"]
    for i, (c, pts, why) in enumerate(top, 1):
        phone = c["phone"] or "no phone on file"
        lines.append(
            f"  {i}. {c['name']} — {phone}"
            f"{roster_badge(c, cat, roster)}")
        lines.append(f"       fit {pts}: " + " · ".join(why))
    return lines



# ---------------------------------------------------------------------------
# Router (2026-10-23): automatic routing + delivery + logging.
# Per lead: fit-score the pool, deliver to subscribers first, then
# freebie-eligible contractors, up to MAX_RECIPIENTS total. Email
# delivery is automatic where the roster carries an address;
# phone-only recipients are flagged in Andrew's report for a manual
# text (their dispatch stays "awaiting_manual" and the freebie is
# reserved, not burned, until Andrew confirms). Contractors who
# match but already used their freebie become pitch targets in the
# report — the system never pitches; Andrew does.
# ---------------------------------------------------------------------------

MAX_RECIPIENTS = 2


def _active_sub(roster, contractor):
    return _roster_match(
        [e for e in roster["subscribers"]
         if (e.get("status") or "active") == "active"], contractor)


def _freebie_used(roster, contractor):
    if _roster_match(roster["freebies"], contractor) is not None:
        return True
    # Reserved by an earlier dispatch that is still awaiting manual
    # delivery — don't hand the same contractor a second freebie.
    for d in roster.get("dispatches", []):
        if d.get("status") == "awaiting_manual":
            for r in d.get("to", []):
                if r.get("kind") == "freebie" and _roster_match(
                        [{"contractor": r.get("contractor"),
                          "phone": r.get("phone")}], contractor):
                    return True
    return False


def _supabase_email(contractor):
    """Contractor email from the production permits table (the
    export carries no emails; prospects.py reads them the same way).
    Matched on the exact phone string, which the export copies from
    the table verbatim. Best-effort: '' on any failure."""
    phone = (contractor.get("phone") or "").strip()
    if not phone or not os.environ.get("SUPABASE_URL") \
            or not os.environ.get("SUPABASE_SERVICE_KEY"):
        return ""
    try:
        q = urllib.parse.urlencode({
            "select": "contractor_email",
            "contractor_phone": f"eq.{phone}",
            "contractor_email": "not.is.null",
            "limit": "1",
        })
        rows = sb_get("permits", params="?" + q)
        return (rows[0].get("contractor_email") or "").strip() \
            if rows else ""
    except Exception:
        return ""


def _entry_email(roster, contractor):
    for entries in (roster["subscribers"], roster["freebies"]):
        e = _roster_match(entries, contractor)
        if e is not None and e.get("email"):
            return e["email"]
    return ""


def route_lead(lead, records, roster):
    """Decide who gets this lead. Returns a routing dict or None
    when the lead can't be routed (unknown job type / no data)."""
    cat = find_category(lead.get("job_type"))
    if cat is None or records is None:
        return None
    scope, note, scored = fit_shortlist(cat, lead.get("city"),
                                        records, lead)
    subs, freebies, pitch = [], [], []
    for c, pts, why in scored:
        if _active_sub(roster, c) is not None:
            subs.append((c, pts, why))
        elif _freebie_used(roster, c):
            pitch.append((c, pts, why))
        else:
            freebies.append((c, pts, why))
    subs.sort(key=lambda x: 0 if (_active_sub(roster, x[0]) or {}).get(
        "priority_next") else 1)
    picked = []
    for c, pts, why in (subs + freebies)[:MAX_RECIPIENTS]:
        kind = "subscriber" if _active_sub(roster, c) is not None \
            else "freebie"
        email = _entry_email(roster, c) or _supabase_email(c)
        picked.append({"contractor": c, "kind": kind, "fit": pts,
                       "why": why,
                       "channel": "email" if email else "manual",
                       "email": email})
    return {"cat": cat, "scope": scope, "note": note,
            "recipients": picked,
            "pitch_targets": pitch[:3],
            "pool_size": len(scored)}


def _first_name(lead):
    name = (lead.get("name") or "").strip()
    return name.split()[0] if name else "the homeowner"


def _display_co(name):
    """Contractor names in the data are often ALL CAPS; soften for
    use inside a greeting."""
    n = (name or "").strip()
    return n.title() if n.isupper() else n


def _contact_links(lead, contractor):
    """Prefilled ways for the contractor to reach the HOMEOWNER:
    a mailto (CC'ing leads@ so Andrew sees the outreach) and an sms
    link. Replying to the delivery email itself reaches Andrew —
    the email copy keeps the two paths clearly separate."""
    import urllib.parse as _up
    co = _display_co(contractor.get("name"))
    first = _first_name(lead)
    job = lead.get("job_type") or "project"
    city = lead.get("city") or "the Raleigh area"
    mailto = None
    if (lead.get("email") or "").strip():
        subj = f"Re: your {job} request in {city} (via PermitPicker)"
        msg = (f"Hi {first},\n\nThis is {co} — PermitPicker passed "
               f"me your {job} request in {city}, and I'd be glad to "
               f"give you a quote.\n\nWhen's a good time to "
               f"talk?\n\n— {co} (via PermitPicker)")
        mailto = (f"mailto:{lead['email'].strip()}"
                  f"?cc={_up.quote(REPLY_TO, safe='@')}"
                  f"&subject={_up.quote(subj)}"
                  f"&body={_up.quote(msg)}")
    sms = None
    digits = "".join(ch for ch in (lead.get("phone") or "")
                     if ch.isdigit())
    if digits:
        text = (f"Hi {first}, this is {co} about your {job} request "
                f"in {city} via PermitPicker — happy to give you a "
                f"quote. When's a good time to talk?")
        sms = f"sms:{digits}?&body={_up.quote(text)}"
    return mailto, sms


def delivery_email(lead, recipient):
    """Returns (subject, text_body, html_body). Structure keeps two
    actions unmistakably separate: CONTACT THE HOMEOWNER (prefilled
    links that CC PermitPicker) vs REPLY TO PERMITPICKER (replying
    to this email reaches Andrew, never the homeowner)."""
    import html as _html
    c = recipient["contractor"]
    job = lead.get("job_type") or "project"
    city = lead.get("city") or "the Raleigh area"
    first = _first_name(lead)
    mailto, sms = _contact_links(lead, c)
    details = [
        f"Name: {lead.get('name') or '—'}",
        f"Phone: {lead.get('phone') or '—'}",
        f"Email: {lead.get('email') or '—'}",
        f"City: {city}",
        f"Job: {job}",
        f"Timeline: {lead.get('timeline') or '—'}",
        f"Budget: {lead.get('budget') or '—'}",
        f"Details: {lead.get('details') or '—'}",
    ]
    if recipient["kind"] == "subscriber":
        subject = f"PermitPicker lead #{lead['id']}: {job} in {city}"
        intro = ("New homeowner request, delivered per your "
                 "PermitPicker lead subscription.")
        reply_extra = "Questions about your subscription? Same reply."
    else:
        subject = (f"PermitPicker lead #{lead['id']}: a {job} request "
                   f"in {city} — yours, free")
        intro = ("Hi — Andrew with PermitPicker. We publish what home "
                 "projects actually cost around Raleigh and Cary, "
                 "from real permit records, and homeowners request "
                 "quotes through the site.\n\n"
                 "This one just came in — yours, free, a sample of "
                 "what our subscribers get the hour requests land. "
                 "The homeowner consented to be contacted by matched "
                 "local contractors.")
        reply_extra = ("Want these regularly? That reply is also how "
                       "you get set up.")
    contact_lines = [f"CONTACT {first.upper()} ABOUT THIS JOB:"]
    if mailto:
        contact_lines.append(
            f"Email {first} (message prefilled, copies PermitPicker "
            f"automatically):\n{mailto}")
    if lead.get("phone"):
        contact_lines.append(
            f"Call or text {first}: {lead['phone']}")
    if not mailto and not lead.get("phone"):
        contact_lines.append("(no contact details on this request)")
    text = (intro + "\n\nTHE REQUEST\n" + "\n".join(details)
            + "\n\n" + "\n".join(contact_lines)
            + f"\n\nREPLY TO PERMITPICKER:\nJust reply to this "
              f"email — it comes to me (Andrew at PermitPicker), "
              f"not to {first}. {reply_extra}\n\n"
              "— Andrew, PermitPicker\n")
    esc = _html.escape
    btn = ('display:inline-block;background:#0e7a6f;color:#ffffff;'
           'text-decoration:none;padding:10px 16px;border-radius:6px;'
           'font-weight:600;margin:4px 6px 4px 0')
    buttons = ""
    if mailto:
        buttons += (f'<a href="{esc(mailto)}" style="{btn}">'
                    f"Email {esc(first)} — prefilled, copies me</a>")
    if sms:
        buttons += (f'<a href="{esc(sms)}" style="{btn}">'
                    f"Text {esc(first)}</a>")
    rows = "".join(
        f"<tr><td style='padding:2px 10px 2px 0;color:#55606a'>"
        f"{esc(d.split(':', 1)[0])}</td><td>{esc(d.split(':', 1)[1].strip())}</td></tr>"
        for d in details)
    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#1c2733;line-height:1.5">
<p>{esc(intro).replace(chr(10) + chr(10), '</p><p>')}</p>
<p style="margin-bottom:4px"><strong>THE REQUEST</strong></p>
<table style="border-collapse:collapse">{rows}</table>
<p style="margin:16px 0 6px"><strong>CONTACT {esc(first.upper())} ABOUT THIS JOB:</strong></p>
<p>{buttons}</p>
<p style="color:#55606a;font-size:13.5px">The email opens prefilled and copies PermitPicker automatically, so I can see the request was answered.</p>
<p style="margin:16px 0 6px"><strong>REPLY TO PERMITPICKER:</strong></p>
<p>Just reply to this email — it comes to me (Andrew at PermitPicker), not to {esc(first)}. {esc(reply_extra)}</p>
<p>— Andrew, PermitPicker</p>
</div>"""
    return subject, text, html_body


def apply_routing(roster, lead, routing, today):
    """Log the dispatch (+ freebie marks for emailed freebies) into
    the roster. Returns True when the roster changed."""
    if any(int(d.get("lead_id") or 0) == int(lead["id"])
           for d in roster.get("dispatches", [])):
        return False  # idempotent: already logged
    sent = today.isoformat()
    tos = []
    all_email = True
    for r in routing["recipients"]:
        c = r["contractor"]
        tos.append({"contractor": c["name"], "phone": c["phone"],
                    "kind": r["kind"], "channel": r["channel"]})
        if r["channel"] != "email":
            all_email = False
        if r["kind"] == "freebie" and r["channel"] == "email":
            roster["freebies"].append({
                "contractor": c["name"], "phone": c["phone"],
                "category": routing["cat"]["id"],
                "city": lead.get("city") or "",
                "date": sent, "lead_id": int(lead["id"])})
    delivered = bool(tos) and all_email
    roster["dispatches"].append({
        "lead_id": int(lead["id"]),
        "job_type": lead.get("job_type") or "",
        "city": lead.get("city") or "",
        "sent": sent,
        "status": "delivered" if delivered else "awaiting_manual",
        "to": tos,
        "homeowner_check": {
            "due": (today + dt.timedelta(days=2)).isoformat()
            if delivered else None, "done": None},
        "contractor_check": {
            "due": (today + dt.timedelta(days=7)).isoformat()
            if delivered else None, "done": None},
    })
    return True


def routing_lines(lead, routing, roster):
    if routing is None:
        return ["ROUTING SKIPPED — dataset unavailable this run; "
                "route this one by hand from the details above."]
    lines = [f"Routing — {routing['scope']}{routing['note']}:"]
    if not routing["recipients"]:
        lines.append("  (no deliverable match — nobody passed the "
                     "gates; widen by hand or treat as a recruiting "
                     "signal)")
    for r in routing["recipients"]:
        c = r["contractor"]
        badge = roster_badge(c, routing["cat"], roster)
        if r["kind"] == "freebie" and not badge:
            badge = " (freebie)"
        via = (f"emailed to {r['email']}" if r["channel"] == "email"
               else "ACTION FOR YOU: text them the lead, then tell "
                    "Muse 'sent' to start the follow-up clocks")
        lines.append(f"  → {c['name']} — {c['phone']}{badge} "
                     f"[fit {r['fit']}] — {via}")
    link = roster.get("stripe_lead_link") or "(Stripe link not set yet)"
    for c, pts, why in routing["pitch_targets"]:
        lines.append(
            f"  ✉ PITCH TARGET: {c['name']} matched but already used "
            f"their freebie. Teaser: '{lead.get('job_type')} in "
            f"{lead.get('city')}, budget {lead.get('budget') or '—'}, "
            f"just came in — subscribers got it.' + {link}")
    return lines


# ---------------------------------------------------------------------------
# Follow-ups (from roster dispatches)
# ---------------------------------------------------------------------------

def today_et():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return dt.datetime.now(dt.timezone.utc).date()


def followups_due(roster, today):
    """Due, unfinished follow-up checks from the dispatch log.
    Each: {"key", "kind": "homeowner"|"contractor", "dispatch"}."""
    out = []
    t = today.isoformat()
    for d in roster.get("dispatches", []):
        for kind, field in (("homeowner", "homeowner_check"),
                            ("contractor", "contractor_check")):
            chk = d.get(field) or {}
            if chk.get("done"):
                continue
            due = chk.get("due") or ""
            if due and due <= t:
                out.append({"key": f"{d.get('lead_id')}:{kind}",
                            "kind": kind, "dispatch": d})
    return out


def followup_lines(due_items):
    lines = ["FOLLOW-UPS DUE", ""]
    for item in due_items:
        d = item["dispatch"]
        who = ", ".join(r.get("contractor") or "?"
                        for r in d.get("to", [])) or "?"
        ref = (f"LEAD #{d.get('lead_id')} ({d.get('job_type') or 'job'}"
               f" — {d.get('city') or '?'}, sent {d.get('sent') or '?'}"
               f" to {who})")
        if item["kind"] == "homeowner":
            lines.append(
                f"  • {ref} — HOMEOWNER check (~48h): did the "
                "contractor reach out, and how did it go? Homeowner "
                "contact is in that lead's alert email. Tell Muse "
                "the answer to close this.")
        else:
            lines.append(
                f"  • {ref} — CONTRACTOR outcome (~7d): reached the "
                "homeowner? quoted? won it? A 'dead lead' verdict "
                "(bad number / not a real project) earns them a "
                "priority credit on the next lead. Tell Muse the "
                "outcome to close this.")
    return lines


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def fmt_ts(iso):
    try:
        t = dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return iso or "—"
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo("America/New_York")).strftime(
            "%b %d, %Y %I:%M %p %Z")
    except Exception:
        return t.astimezone(dt.timezone.utc).strftime("%b %d, %Y %H:%M UTC")


def lead_block(lead, records, roster, routing=None):
    job = lead.get("job_type") or "—"
    city = lead.get("city") or "—"
    lines = [
        f"LEAD #{lead.get('id')} — {job} in {city}",
        f"Submitted: {fmt_ts(lead.get('created_at'))} "
        f"(via {lead.get('source_page') or '—'})",
        f"Name: {lead.get('name') or '—'}",
        f"Phone: {lead.get('phone') or '—'}",
        f"Email: {lead.get('email') or '—'}",
        f"City: {city}",
        f"Job type: {job}",
        f"Timeline: {lead.get('timeline') or '—'}",
        f"Budget: {lead.get('budget') or '—'}",
        f"Details: {lead.get('details') or '—'}",
        "",
    ]
    if routing is not None:
        lines += routing_lines(lead, routing, roster)
        return lines
    cat = find_category(lead.get("job_type"))
    if cat is None:
        lines.append("Top matching contractors: "
                     "(job type not recognized — no shortlist)")
    elif records is None:
        lines.append("Top matching contractors: "
                     "(unavailable — dataset could not be loaded)")
    else:
        lines += format_shortlist(cat, lead.get("city"), records,
                                  roster, lead)
    return lines


def build_email(leads, records, roster, followups=None,
                routings=None):
    if len(leads) == 1:
        lead = leads[0]
        subject = (f"New PermitPicker quote request: "
                   f"{lead.get('job_type') or 'job'} in "
                   f"{lead.get('city') or 'unknown city'}")
        head = []
    else:
        subject = f"{len(leads)} new PermitPicker quote requests"
        head = [f"{len(leads)} new quote requests since the last check:", ""]
    lines = list(head)
    for i, lead in enumerate(leads):
        if i:
            lines += ["", "-" * 60, ""]
        lines += lead_block(lead, records, roster,
                            (routings or {}).get(int(lead["id"])))
    if followups:
        lines += ["", "-" * 60, ""] + followup_lines(followups)
    lines += [
        "",
        "-" * 60,
        "Roster marks: ★ SUBSCRIBER = paying, deliver first "
        "(max 2 recipients per lead, subscribers rotated); "
        "✓ free lead sent = their one freebie is used, next lead "
        "for them is the subscription pitch; no mark = freebie "
        "still available. Tell Muse who you sent a freebie to (or "
        "who subscribed) and the roster updates.",
        "",
        "Automated alert from the PermitPicker lead pipeline "
        "(GitHub Actions). Leads live in the Supabase quote_requests "
        "table; rankings are by recent permit activity from public "
        "records.",
    ]
    return subject, "\n".join(lines) + "\n"


def send_email(to, subject, body, reply_to=None, bcc=None,
               html=None):
    """Send via pipeline/send_digest.py (the send-pitch.yml approach:
    Resend API, RESEND_API_KEY env). Raises SystemExit on failure."""
    body_path = os.path.join(ROOT, "state", ".lead_alert_body.txt")
    with open(body_path, "w") as f:
        f.write(body)
    html_path = None
    if html:
        html_path = os.path.join(ROOT, "state",
                                 ".lead_alert_body.html")
        with open(html_path, "w") as f:
            f.write(html)
    try:
        r = subprocess.run(
            [sys.executable,
             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "send_digest.py"),
             "--to", to, "--from", FROM_ADDR,
             "--subject", subject, "--body-file", body_path]
            + (["--reply-to", reply_to] if reply_to else [])
            + (["--bcc", bcc] if bcc else [])
            + (["--html-file", html_path] if html_path else []),
            capture_output=True, text=True, timeout=120)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        if r.returncode != 0:
            raise SystemExit(
                f"lead_alerts: send failed (send_digest rc={r.returncode}); "
                "watermark NOT advanced")
    finally:
        if os.path.exists(body_path):
            os.unlink(body_path)
        if html_path and os.path.exists(html_path):
            os.unlink(html_path)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--permits", default=PERMITS_URL,
                    help="Published permits dataset: live URL or local path")
    ap.add_argument("--to", default=DEFAULT_TO)
    ap.add_argument("--roster", default=DEFAULT_ROSTER)
    args = ap.parse_args()

    state = load_state(args.state)
    watermark = state["last_alerted_id"]
    leads = fetch_new_leads(watermark)
    print(f"lead_alerts: {len(leads)} new rows above watermark "
          f"{watermark}", flush=True)

    roster = load_roster(args.roster)
    today = today_et()
    due = followups_due(roster, today)
    reminded = state.get("followup_reminded", {})
    fresh_due = [d for d in due
                 if reminded.get(d["key"]) != today.isoformat()]

    if not leads and not fresh_due:
        return  # quiet success: nothing new, no reminders owed today

    processed, routings = [], {}
    roster_dirty = False
    routing_failed = False
    if leads:
        try:
            records = load_permits(args.permits)
        except Exception as e:  # dataset is required for routing now
            print(f"lead_alerts: permits dataset unavailable ({e}); "
                  "alerting without routing", file=sys.stderr,
                  flush=True)
            records = None
        already = {int(d.get("lead_id") or 0)
                   for d in roster.get("dispatches", [])}
        for lead in leads:
            if int(lead["id"]) in already:
                processed.append(lead)  # routed in an earlier run
                continue
            routing = (route_lead(lead, records, roster)
                       if records is not None else None)
            if routing is None:
                # Alert-only for this lead (no dataset / unknown job
                # type): the report carries full details to route by
                # hand. Counted as processed — Andrew is the router.
                processed.append(lead)
                continue
            failed = False
            for r in routing["recipients"]:
                if r["channel"] != "email":
                    continue
                subject, body, html_body = delivery_email(lead, r)
                try:
                    send_email(r["email"], subject, body,
                               reply_to=REPLY_TO, bcc=DELIVERY_BCC,
                               html=html_body)
                except SystemExit:
                    failed = True
                    break
            if failed:
                routing_failed = True
                print(f"lead_alerts: delivery failed for lead "
                      f"{lead['id']}; holding watermark below it",
                      file=sys.stderr, flush=True)
                break  # retry this lead (and the rest) next run
            if apply_routing(roster, lead, routing, today):
                roster_dirty = True
            routings[int(lead["id"])] = routing
            processed.append(lead)

        if processed:
            subject, body = build_email(processed, records, roster,
                                        followups=due,
                                        routings=routings)
            if routing_failed:
                body = ("NOTE: a delivery failed mid-batch; the "
                        "remaining new leads will retry next run.\n\n"
                        + body)
            send_email(args.to, subject, body)
            state["last_alerted_id"] = max(
                int(lead["id"]) for lead in processed)
            print(f"lead_alerts: report sent for "
                  f"{len(processed)} lead(s); watermark advanced "
                  f"{watermark} -> {state['last_alerted_id']}",
                  flush=True)
    else:
        subject = (f"PermitPicker: {len(fresh_due)} lead follow-up(s) "
                   "due")
        body = "\n".join(followup_lines(fresh_due)) + (
            "\n\nTell Muse the outcomes and the roster closes them "
            "out.\n")
        send_email(args.to, subject, body)
        print(f"lead_alerts: follow-up reminder sent "
              f"({len(fresh_due)} due)", flush=True)

    if roster_dirty:
        save_roster(args.roster, roster)
        print("lead_alerts: roster updated (dispatches/freebies)",
              flush=True)

    # A send succeeded: stamp today's reminders, prune stale keys.
    live_keys = {d["key"] for d in due}
    state["followup_reminded"] = {
        k: v for k, v in reminded.items() if k in live_keys}
    for d in due:
        state["followup_reminded"][d["key"]] = today.isoformat()
    save_state(args.state, state)


if __name__ == "__main__":
    main()
