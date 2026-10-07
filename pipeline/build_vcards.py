#!/usr/bin/env python3
"""Generate vCard (.vcf) contact files for recently-seen contractors.

Scans web/data/permits.json for permits applied in the last VCARD_DAYS,
collects contractors with phone numbers, and writes one vCard per
contractor_key into web/data/vcards/<url-quoted-key>.vcf.

The daily digest links each lead card's "Add to contacts" button to
https://permitpicker.com/data/vcards/<key>.vcf — tapping it on iOS or
Android opens the contact importer with name + work number prefilled.

Runs in the daily workflow after export, before the Pages artifact upload,
so the files deploy with the site.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import contractor_key, display_for, is_contractor

VCARD_DAYS = 14


def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return ""


def e164(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"+1{d}" if len(d) == 10 else ""


def main():
    data_dir = os.path.join(os.path.dirname(__file__), "..", "web", "data")
    with open(os.path.join(data_dir, "permits.json")) as f:
        permits = json.load(f)["permits"]
    cutoff = (dt.datetime.now(dt.timezone.utc).date()
              - dt.timedelta(days=VCARD_DAYS)).isoformat()

    names = defaultdict(list)
    phones = defaultdict(Counter)
    for p in permits:
        if (p.get("applied_date") or "")[:10] < cutoff:
            continue
        c = (p.get("contractor") or "").strip()
        if not c or not is_contractor(c):
            continue
        k = contractor_key(c)
        names[k].append(c)
        ph = norm_phone(p.get("contractor_phone"))
        if ph:
            phones[k][ph] += 1

    out_dir = os.path.join(data_dir, "vcards")
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for k, variants in names.items():
        if not phones[k]:
            continue
        disp = display_for(k, variants)[:60].replace("\n", " ")
        tel = e164(phones[k].most_common(1)[0][0])
        if not tel:
            continue
        vcf = (f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:{disp}\r\n"
               f"ORG:{disp}\r\nTEL;TYPE=WORK,VOICE:{tel}\r\n"
               f"NOTE:Contractor via PermitPulse permit intelligence\r\n"
               f"END:VCARD\r\n")
        fname = urllib.parse.quote(k, safe="") + ".vcf"
        with open(os.path.join(out_dir, fname), "w") as f:
            f.write(vcf)
        n += 1
    print(f"vcards: {n} contractors -> {out_dir}")


if __name__ == "__main__":
    main()
