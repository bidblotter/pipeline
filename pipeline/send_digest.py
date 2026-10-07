#!/usr/bin/env python3
"""Send a digest file via the Resend API.

Env: RESEND_API_KEY (GitHub secret in Actions).
Usage:
  python pipeline/send_digest.py --to you@example.com --subject "Hi"
      --body-file /tmp/digest.txt [--from morning@permitpicker.com]
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
                    default="morning@permitpicker.com")
    ap.add_argument("--html-file", default=None,
                    help="Optional HTML body; sent as multipart with the text version")
    ap.add_argument("--attach", action="append", default=[],
                    help="Attach a file (repeatable); sent base64 via Resend")
    ap.add_argument("--reply-to", default=None,
                    help="Reply-To address (replies go here instead of the From address)")
    args = ap.parse_args()

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        sys.exit("RESEND_API_KEY is not set")
    with open(args.body_file) as f:
        text = f.read()
    print(f"send_digest: to={args.to} subject={args.subject[:60]!r} "
          f"body_bytes={len(text)}", flush=True)

    payload = {
        "from": args.from_addr,
        "to": [args.to],
        "subject": args.subject,
        "text": text,
    }
    if args.reply_to:
        payload["reply_to"] = args.reply_to
        print(f"send_digest: reply_to={args.reply_to}", flush=True)
    if args.html_file:
        with open(args.html_file) as f:
            payload["html"] = f.read()
        print(f"send_digest: html_bytes={len(payload['html'])}", flush=True)
    if args.attach:
        import base64
        payload["attachments"] = []
        for path in args.attach:
            with open(path, "rb") as f:
                content = base64.b64encode(f.read()).decode()
            payload["attachments"].append(
                {"filename": os.path.basename(path), "content": content})
            print(f"send_digest: attached {path} "
                  f"({len(content) * 3 // 4 // 1024}KB)", flush=True)
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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("resend:", resp.status, resp.read().decode()[:500])
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:1000]
        print(f"resend HTTPError: {e.code} {e.reason} :: {body}",
              file=sys.stderr, flush=True)
        raise SystemExit(f"Resend send failed: HTTP {e.code}")


if __name__ == "__main__":
    main()
