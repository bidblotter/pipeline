#!/usr/bin/env python3
"""Send the morning digest to paying customers.

Reads DIGEST_CUSTOMERS env var: a JSON array of
  {"email": "buyer@distributor.com", "trade": "electrical", "days": 7}
Each customer gets the digest for their trade, branded PermitPulse.
If DIGEST_CUSTOMERS is unset or empty, exits quietly (no customers yet).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(*argv):
    p = subprocess.run([sys.executable, *argv], capture_output=True, text=True)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {p.stderr[-500:]}")
    return p.stdout


def main():
    raw = os.environ.get("DIGEST_CUSTOMERS", "").strip()
    if not raw:
        print("no DIGEST_CUSTOMERS set — skipping customer digests")
        return
    try:
        customers = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"DIGEST_CUSTOMERS is not valid JSON: {e} — skipping")
        return
    if not isinstance(customers, list):
        print("DIGEST_CUSTOMERS must be a JSON array — skipping")
        return
    for i, c in enumerate(customers):
        email = (c.get("email") or "").strip()
        trade = (c.get("trade") or "electrical").strip()
        days = str(c.get("days", 7))
        if not email or "@" not in email:
            print(f"customer #{i}: bad email {email!r} — skipping")
            continue
        body = f"/tmp/customer_digest_{i}.txt"
        html_body = f"/tmp/customer_digest_{i}.html"
        out = run(os.path.join(HERE, "digest.py"), "--trade", trade,
                  "--days", days, "--brand", "PermitPulse",
                  "--out", body, "--html-out", html_body)
        subject = "PermitPulse daily digest"
        for line in out.splitlines():
            if line.startswith("SUBJECT: "):
                subject = line[len("SUBJECT: "):]
                break
        run(os.path.join(HERE, "send_digest.py"), "--to", email,
            "--subject", subject, "--body-file", body,
            "--html-file", html_body,
            "--from", "PermitPicker <morning@permitpicker.com>")
        print(f"customer digest sent to {email} (trade={trade} days={days})")


if __name__ == "__main__":
    main()
