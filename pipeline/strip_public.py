#!/usr/bin/env python3
"""Remove contractor phone numbers from the PUBLIC permits export.

Runs in ingest.yml AFTER publish_internal.py (which has already
stored the full file in the private Storage bucket) and after every
in-workflow consumer that needs phones (cost guides, contractor
pages, digests all read the in-workflow file before this step), and
BEFORE the Pages artifact upload. Since 2026-10-23 the public site
carries no phone numbers: "the phone is the money field."
"""
import json
import os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PATH = os.path.join(ROOT, "web", "data", "permits.json")
PHONE_KEYS = ("contractor_phone", "phone")


def main():
    with open(PATH) as f:
        payload = json.load(f)
    recs = payload["permits"] if isinstance(payload, dict) else payload
    stripped = 0
    for r in recs:
        for k in PHONE_KEYS:
            if r.pop(k, None):
                stripped += 1
    with open(PATH, "w") as f:
        json.dump(payload, f)
    print(f"strip_public: removed phone fields from {stripped} "
          f"records in public permits.json ({len(recs)} records)")


if __name__ == "__main__":
    main()
