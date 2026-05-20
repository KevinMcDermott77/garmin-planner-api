-- Run this migration manually in the Supabase SQL editor before using Strava features.
-- The application does not run migrations automatically.

create table if not exists public.strava_tokens (
  user_id uuid primary key references auth.users(id) on delete cascade,
  access_token text not null,
  refresh_token text not null,
  expires_at timestamptz not null,
  athlete_id bigint,
  scope text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists strava_tokens_expires_at_idx
  on public.strava_tokens (expires_at);
