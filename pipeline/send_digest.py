#!/usr/bin/env python3
"""Send a digest file via the Resend API.

Env: RESEND_API_KEY (GitHub secret in Actions).
Usage:
  python pipeline/send_digest.py --to you@example.com --subject "Hi"
      --body-file /tmp/digest.txt [--from onboarding@resend.dev]
"""
import argparse
import json
import os
import sys
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--from", dest="from_addr",
                    default="onboarding@resend.dev")
    args = ap.parse_args()

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        sys.exit("RESEND_API_KEY is not set")
    with open(args.body_file) as f:
        text = f.read()

    payload = {
        "from": args.from_addr,
        "to": [args.to],
        "subject": args.subject,
        "text": text,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("resend:", resp.status, resp.read().decode()[:200])


if __name__ == "__main__":
    main()
