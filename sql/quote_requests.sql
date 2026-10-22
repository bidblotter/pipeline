-- PermitPicker homeowner quote-request leads (from the /costs/ guide pages).
-- Run once in the Supabase SQL editor, then set LEAD_ENDPOINT in
-- pipeline/cost_guides.py's rendered form script to
-- SUPABASE_URL + "/rest/v1/quote_requests" and re-run the ingest workflow.
-- Public inserts only; reads stay locked down (same pattern as waitlist.sql).
create table if not exists quote_requests (
  id bigint generated always as identity primary key,
  name text not null,
  phone text not null,
  email text,
  city text,
  job_type text not null,
  timeline text,
  budget text,
  details text,
  source_page text,
  created_at timestamptz not null default now()
);

alter table quote_requests enable row level security;

drop policy if exists "public insert" on quote_requests;
create policy "public insert" on quote_requests
  for insert to anon
  with check (true);

-- Explicit grant: Supabase stops auto-granting table privileges on new
-- tables 2026-10-30. The cost-guide form inserts as anon (reads stay
-- locked down by RLS + the policy above).
grant insert on quote_requests to anon;
