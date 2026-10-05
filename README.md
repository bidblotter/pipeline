# PermitPicker pipeline

Daily ingest of Wake County (Raleigh/Cary/Apex/Morrisville/…) permit feeds into Supabase.
Runs on GitHub Actions — no server, no AI quota involved.

## Sources

| Feed | Records | Freshness | What's in it |
|---|---|---|---|
| Wake County Building Permits | ~122 new/week | nightly | permit number, type, description, dates, status, address, valuation, contractor |
| Wake County Permits Other Than Buildings | ~466 new/week | nightly | trade permits (electrical/plumbing/mechanical) + contractor company, email, phone, license |

## Setup (one time)

1. **Supabase**: create a free project, open the SQL editor, run `sql/schema.sql`.
2. **GitHub**: in the repo, go to Settings → Secrets and variables → Actions → New repository secret, and add:
   - `SUPABASE_URL` — e.g. `https://xyzcompany.supabase.co`
   - `SUPABASE_SERVICE_KEY` — the service-role key (Settings → API). This bypasses row-level security, so keep it secret.
3. Push these files. The workflow runs daily at 10:00 UTC and can also be triggered manually from the Actions tab ("Run workflow").

## How it works

- `pipeline/ingest.py` reads a per-source watermark (last successful run) from `ingest_runs`, pulls everything newer from each ArcGIS feed, normalizes to one schema, and upserts into `permits` keyed on `(jurisdiction, permit_number)`. Sources are independent: one flaky feed can't block or stall the others.
- Every run writes a row per source to `ingest_runs` (`ok`/`error`) — that's the heartbeat. If a run fails, the Actions UI shows it red.
- stdlib only: no pip dependencies to break.

## Durham County (phase 2)

Durham's open-data permit feeds aren't ingest-ready: "All Building Permits" is stale (2021) and "Active Building Permits" has no date fields. Options when we get there: scrape the city's permit portal, or check whether their newer inspections system exposes an API. The schema already has a `jurisdiction` column ready for it.

## Local test

```bash
export SUPABASE_URL=... SUPABASE_SERVICE_KEY=...
python3 pipeline/ingest.py
```
