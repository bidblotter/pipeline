-- PermitPicker pilot signup form backing table.
-- Run once in the Supabase SQL editor. Public inserts only; reads stay locked down.
create table if not exists waitlist (
  id bigint generated always as identity primary key,
  email text not null,
  trade text not null default 'electrical',
  created_at timestamptz not null default now(),
  unique (email, trade)
);

alter table waitlist enable row level security;

drop policy if exists "public insert" on waitlist;
create policy "public insert" on waitlist
  for insert to anon
  with check (true);

-- Explicit grant: Supabase stops auto-granting table privileges on new
-- tables 2026-10-30. The public signup form inserts as anon (reads stay
-- locked down by RLS + the policy above).
grant insert on waitlist to anon;
