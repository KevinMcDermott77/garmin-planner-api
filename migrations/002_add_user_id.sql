-- Add Supabase Auth ownership columns.
-- user_id values reference Supabase auth.users(id), but we avoid a hard cross-schema
-- foreign key here because Supabase manages the auth schema.
-- RLS is still not configured. Enable and test Row Level Security before production.

alter table profiles
  add column if not exists user_id uuid;

alter table plans
  add column if not exists user_id uuid;

create index if not exists idx_profiles_user_id on profiles(user_id);
create index if not exists idx_plans_user_id on plans(user_id);
