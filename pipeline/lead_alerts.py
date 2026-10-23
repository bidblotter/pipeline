#!/usr/bin/env python3
"""Email Andrew when new homeowner quote requests land in Supabase.

The cost-guide pages (permitpicker.com/costs/) insert quote requests into
the Supabase `quote_requests` table (public-insert-only RLS). This job
polls for rows with id greater than a watermark, emails a work-order
digest to Andrew (lead details + a shortlist of top-matching local
contractors for each lead), and only then advances the watermark.

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

Contractor shortlists use the exact category definitions and ranking
from cost_guides.py (imported, not copied) over the published dataset.
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
                "dispatches": data.get("dispatches", [])}
    except (OSError, ValueError):
        return {"freebies": [], "subscribers": [], "dispatches": []}


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


def shortlist(cat, city_raw, records, roster):
    """Top contractors for the category, city-scoped when the lead's
    city has records in it, else region-wide. Subscribers for this
    category float to the top (they get leads first); the rest keep
    activity-rank order. Returns (scope_label, top)."""
    matched = [r for r in records if cat["match"](r)]
    disp = cg.city_display(city_raw or "")
    top = None
    if disp:
        city_recs = [r for r in matched
                     if cg.city_display(r.get("city")) == disp]
        if city_recs:
            top = cg.top_contractors(city_recs, limit=8)
            scope = disp
    if top is None:
        top = cg.top_contractors(matched, limit=8)
        scope = cg.REGION_NAME

    def sub_entry(c):
        return _roster_match(
            [x for x in roster["subscribers"]
             if (x.get("status") or "active") == "active"], c)

    subs = [c for c in top if sub_entry(c) is not None]
    # Dead-lead credit holders jump the subscriber queue.
    subs.sort(key=lambda c: 0 if (sub_entry(c) or {}).get(
        "priority_next") else 1)
    rest = [c for c in top if sub_entry(c) is None]
    return scope, (subs + rest)[:5]


def format_shortlist(cat, city_raw, records, roster):
    scope, top = shortlist(cat, city_raw, records, roster)
    if city_raw and scope == cg.REGION_NAME:
        head = (f"Top matching contractors — {scope} "
                f"(region-wide; no {city_raw} ranking):")
    else:
        head = f"Top matching contractors — {scope}:"
    if not top:
        return [head, "  (no contractor names in the published permit "
                      "data for this category)"]
    lines = [head]
    for i, c in enumerate(top, 1):
        phone = c["phone"] or "no phone on file"
        lines.append(
            f"  {i}. {c['name']} — {c['count']} {cat['job']} permits, "
            f"typical project {cg.money(c['median'])} — {phone}"
            f"{roster_badge(c, cat, roster)}")
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


def lead_block(lead, records, roster):
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
    cat = find_category(lead.get("job_type"))
    if cat is None:
        lines.append("Top matching contractors: "
                     "(job type not recognized — no shortlist)")
    elif records is None:
        lines.append("Top matching contractors: "
                     "(unavailable — dataset could not be loaded)")
    else:
        lines += format_shortlist(cat, lead.get("city"), records,
                                  roster)
    return lines


def build_email(leads, records, roster, followups=None):
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
        lines += lead_block(lead, records, roster)
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


def send_email(to, subject, body):
    """Send via pipeline/send_digest.py (the send-pitch.yml approach:
    Resend API, RESEND_API_KEY env). Raises SystemExit on failure."""
    body_path = os.path.join(ROOT, "state", ".lead_alert_body.txt")
    with open(body_path, "w") as f:
        f.write(body)
    try:
        r = subprocess.run(
            [sys.executable,
             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "send_digest.py"),
             "--to", to, "--from", FROM_ADDR,
             "--subject", subject, "--body-file", body_path],
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

    if leads:
        try:
            records = load_permits(args.permits)
        except Exception as e:  # shortlist is best-effort; alert is not
            print(f"lead_alerts: permits dataset unavailable ({e}); "
                  "sending without shortlists", file=sys.stderr,
                  flush=True)
            records = None
        subject, body = build_email(leads, records, roster,
                                    followups=due)
        send_email(args.to, subject, body)
        state["last_alerted_id"] = max(int(lead["id"]) for lead in leads)
        print(f"lead_alerts: alert sent for {len(leads)} lead(s); "
              f"watermark advanced {watermark} -> "
              f"{state['last_alerted_id']}", flush=True)
    else:
        subject = (f"PermitPicker: {len(fresh_due)} lead follow-up(s) "
                   "due")
        body = "\n".join(followup_lines(fresh_due)) + (
            "\n\nTell Muse the outcomes and the roster closes them "
            "out.\n")
        send_email(args.to, subject, body)
        print(f"lead_alerts: follow-up reminder sent "
              f"({len(fresh_due)} due)", flush=True)

    # A send succeeded: stamp today's reminders, prune stale keys.
    live_keys = {d["key"] for d in due}
    state["followup_reminded"] = {
        k: v for k, v in reminded.items() if k in live_keys}
    for d in due:
        state["followup_reminded"][d["key"]] = today.isoformat()
    save_state(args.state, state)


if __name__ == "__main__":
    main()
