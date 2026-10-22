#!/usr/bin/env python3
"""Email Andrew when new homeowner quote requests land in Supabase.

The cost-guide pages (permitpicker.com/costs/) insert quote requests into
the Supabase `quote_requests` table (public-insert-only RLS). This job
polls for rows with id greater than a watermark, emails a work-order
digest to Andrew (lead details + a shortlist of top-matching local
contractors for each lead), and only then advances the watermark.

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

def load_watermark(path):
    try:
        with open(path) as f:
            return int(json.load(f).get("last_alerted_id", 0))
    except (OSError, ValueError):
        return 0


def save_watermark(path, last_id):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"last_alerted_id": last_id}, f, indent=1)
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


def shortlist(cat, city_raw, records):
    """Top-5 contractors for the category, city-scoped when the lead's
    city has records in it, else region-wide. Returns (scope_label, top)
    where scope_label says which of the two happened."""
    matched = [r for r in records if cat["match"](r)]
    disp = cg.city_display(city_raw or "")
    if disp:
        city_recs = [r for r in matched
                     if cg.city_display(r.get("city")) == disp]
        if city_recs:
            return disp, cg.top_contractors(city_recs, limit=5)
    return cg.REGION_NAME, cg.top_contractors(matched, limit=5)


def format_shortlist(cat, city_raw, records):
    scope, top = shortlist(cat, city_raw, records)
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
            f"typical project {cg.money(c['median'])} — {phone}")
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


def lead_block(lead, records):
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
        lines += format_shortlist(cat, lead.get("city"), records)
    return lines


def build_email(leads, records):
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
        lines += lead_block(lead, records)
    lines += [
        "",
        "-" * 60,
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
    args = ap.parse_args()

    watermark = load_watermark(args.state)
    leads = fetch_new_leads(watermark)
    print(f"lead_alerts: {len(leads)} new rows above watermark "
          f"{watermark}", flush=True)
    if not leads:
        return  # quiet success: no email, no state change

    try:
        records = load_permits(args.permits)
    except Exception as e:  # shortlist is best-effort; the alert is not
        print(f"lead_alerts: permits dataset unavailable ({e}); "
              "sending without shortlists", file=sys.stderr, flush=True)
        records = None

    subject, body = build_email(leads, records)
    send_email(args.to, subject, body)

    new_watermark = max(int(lead["id"]) for lead in leads)
    save_watermark(args.state, new_watermark)
    print(f"lead_alerts: alert sent for {len(leads)} lead(s); "
          f"watermark advanced {watermark} -> {new_watermark}", flush=True)


if __name__ == "__main__":
    main()
