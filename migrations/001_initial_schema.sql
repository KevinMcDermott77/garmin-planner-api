-- Initial persistence schema for generated training plans.
-- RLS is not configured yet. Enable and test Row Level Security before production.

create table if not exists profiles (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  name text not null,
  weekly_km_recent numeric not null,
  longest_run_km_recent numeric not null,
  easy_pace_min_per_km numeric,
  days_per_week int not null,
  recent_race jsonb,
  injuries text,
  cross_training text,
  years_running numeric,
  schedule jsonb,
  notes text
);

create table if not exists plans (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  profile_id uuid references profiles(id) on delete set null,
  goal text not null,
  weeks int not null,
  race_date date,
  start_date date,
  status text not null default 'draft',
  plan_json jsonb not null,
  assessment_json jsonb,
  tokens_json jsonb
);

create table if not exists scheduled_sessions (
  id uuid primary key default gen_random_uuid(),
  plan_id uuid references plans(id) on delete cascade,
  week_number int not null,
  day_of_week int not null,
  scheduled_date date,
  session_type text not null,
  description text,
  distance_km numeric,
  duration_min int,
  steps jsonb,
  status text not null default 'planned',
  garmin_workout_id bigint,
  garmin_scheduled_id bigint,
  created_at timestamptz default now()
);

create index if not exists idx_plans_profile_id on plans(profile_id);
create index if not exists idx_scheduled_sessions_plan_id on scheduled_sessions(plan_id);
