-- Durham "Active *" permit snapshot (BidBlotter phase 2)
-- Run once in the Supabase SQL editor.
-- The Durham ArcGIS feeds have no date fields, so the daily ingest diffs
-- Permit_IDs against this snapshot: new IDs = new permits.

create table if not exists durham_permit_snapshot (
  layer text not null,                    -- 'building' | 'plumbing' | 'mechanical' | 'electrical'
  permit_id text not null,
  p_type text,
  p_status text,
  p_activity text,
  p_descript text,
  site_add text,
  pid text,                               -- parcel id
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  primary key (layer, permit_id)
);

create index if not exists durham_snapshot_seen_idx
  on durham_permit_snapshot (last_seen_at desc);

-- Explicit grants: Supabase stops auto-granting table privileges on new
-- tables 2026-10-30. The pipeline talks to Supabase as service_role.
grant all on durham_permit_snapshot to service_role;
