-- BidBlotter pipeline schema (Supabase / Postgres)
-- Run this once in the Supabase SQL editor.

create table if not exists permits (
  id bigint generated always as identity primary key,
  jurisdiction text not null,              -- 'wake' | 'durham'
  permit_number text not null,
  permit_type text,
  work_class text,
  description text,
  status text,
  applied_date timestamptz,
  issued_date timestamptz,
  address text,
  city text,
  valuation numeric,
  contractor text,
  contractor_email text,
  contractor_phone text,
  contractor_license text,
  latitude double precision,
  longitude double precision,
  source_feed text not null,               -- 'wake_building' | 'wake_trade' | 'durham_building'
  raw jsonb,
  first_seen_at timestamptz not null default now(),
  unique (jurisdiction, permit_number)
);

create index if not exists permits_issued_idx on permits (issued_date desc);
create index if not exists permits_applied_idx on permits (applied_date desc);
create index if not exists permits_type_idx on permits (permit_type, work_class);

create table if not exists ingest_runs (
  id bigint generated always as identity primary key,
  source text,                             -- 'wake_building' | 'wake_trade' (null = legacy)
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null,                    -- 'ok' | 'error'
  records_fetched int not null default 0,
  records_inserted int not null default 0,
  error text
);
