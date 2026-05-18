-- Migration 004: store target pace ranges for unstructured running sessions.
--
-- Run this manually in the Supabase SQL editor before generating live plans
-- that persist easy/long/recovery pace ranges. This migration is not auto-run.

alter table scheduled_sessions
  add column if not exists pace_low_min_per_km numeric,
  add column if not exists pace_high_min_per_km numeric;
