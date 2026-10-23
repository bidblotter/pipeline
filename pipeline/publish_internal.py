#!/usr/bin/env python3
"""Publish the internal (phone-bearing) data copies to a PRIVATE
Supabase Storage bucket, then remove the public copies that must not
deploy.

Why: since 2026-10-23 the public site carries no contractor phone
numbers ("the phone is the money field" — Andrew's call). But the
paid/sample generators (digest.py, weekly_report.py via send-pitch)
and the lead-alert shortlists still need phones. They used to read
the public web/data/permits.json; now the ingest publishes the full
export to the private `internal` Storage bucket first, and only then
does strip_public.py remove contractor_phone from the public file.

Also handled here, because they are the same leak class:
  - web/prospects.csv (company + phone + email list) is uploaded as
    internal/prospects.csv and deleted from web/ so it stops
    deploying to the public site.
  - web/data/vcards/ (per-contractor .vcf files with TEL) is zipped
    to internal/vcards.zip and deleted from web/.

Failure contract: any upload failure exits non-zero. In ingest.yml
this step runs before strip_public.py and before the Pages artifact
upload, so a failure leaves yesterday's site (and its data paths)
untouched rather than deploying a phone-less public file whose
internal replacement doesn't exist.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY. Stdlib only.
"""
import io
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WEB = os.path.join(ROOT, "web")
BUCKET = "internal"


def _req(method, path, data=None, ctype=None, upsert=False):
    url = os.environ["SUPABASE_URL"].rstrip("/") + path
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if ctype:
        headers["Content-Type"] = ctype
    if upsert:
        headers["x-upsert"] = "true"
    r = urllib.request.Request(url, data=data, headers=headers,
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def ensure_bucket():
    st, body = _req("POST", "/storage/v1/bucket",
                    json.dumps({"id": BUCKET, "name": BUCKET,
                                "public": False}).encode(),
                    "application/json")
    if st in (200, 201) or "already exists" in body or st == 409:
        print(f"publish_internal: bucket '{BUCKET}' ready (HTTP {st})")
        return
    raise SystemExit(f"publish_internal: bucket create failed "
                     f"HTTP {st}: {body[:200]}")


def upload(name, data, ctype):
    st, body = _req("POST", f"/storage/v1/object/{BUCKET}/{name}",
                    data, ctype, upsert=True)
    if st not in (200, 201):
        raise SystemExit(f"publish_internal: upload {name} failed "
                         f"HTTP {st}: {body[:200]}")
    print(f"publish_internal: uploaded internal/{name} "
          f"({len(data)} bytes)")


def verify_objects(names):
    st, body = _req("POST", f"/storage/v1/object/list/{BUCKET}",
                    json.dumps({"prefix": "", "limit": 100}).encode(),
                    "application/json")
    if st != 200:
        raise SystemExit(f"publish_internal: list failed HTTP {st}")
    found = {o["name"] for o in json.loads(body)}
    missing = [n for n in names if n not in found]
    if missing:
        raise SystemExit(f"publish_internal: objects missing after "
                         f"upload: {missing}")
    print(f"publish_internal: verified in bucket: {sorted(found)}")


def main():
    ensure_bucket()
    uploaded = []

    permits_path = os.path.join(WEB, "data", "permits.json")
    with open(permits_path, "rb") as f:
        raw = f.read()
    payload = json.loads(raw)
    recs = payload["permits"] if isinstance(payload, dict) else payload
    with_phones = sum(1 for r in recs if r.get("contractor_phone"))
    if with_phones < 1000:
        raise SystemExit(
            f"publish_internal: only {with_phones} records carry a "
            "phone — refusing to publish/strip; the export looks "
            "wrong, investigate before the public file loses phones")
    upload("permits_full.json", raw, "application/json")
    uploaded.append("permits_full.json")
    print(f"publish_internal: {len(recs)} records, {with_phones} "
          "with phones")

    prospects = os.path.join(WEB, "prospects.csv")
    if os.path.exists(prospects):
        with open(prospects, "rb") as f:
            upload("prospects.csv", f.read(), "text/csv")
        uploaded.append("prospects.csv")
        os.remove(prospects)
        print("publish_internal: prospects.csv moved internal; "
              "public copy deleted")

    vdir = os.path.join(WEB, "data", "vcards")
    if os.path.isdir(vdir):
        buf = io.BytesIO()
        n = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fn in sorted(os.listdir(vdir)):
                z.write(os.path.join(vdir, fn), arcname=fn)
                n += 1
        upload("vcards.zip", buf.getvalue(), "application/zip")
        uploaded.append("vcards.zip")
        shutil.rmtree(vdir)
        print(f"publish_internal: {n} vCards zipped internal; "
              "public directory deleted")

    verify_objects(uploaded)
    print("publish_internal: done")


if __name__ == "__main__":
    main()
