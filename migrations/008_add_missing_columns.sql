-- Migration 008: Add missing columns to curated_restaurants
-- These columns were referenced in code but never formally added via migration.
-- Run in Supabase SQL Editor. Safe to run multiple times (IF NOT EXISTS).

ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS slot_interval INTEGER DEFAULT 15;
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS service_start TEXT DEFAULT '17:00';
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS service_end TEXT DEFAULT '22:00';
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS hot_start TEXT DEFAULT '19:00';
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS hot_end TEXT DEFAULT '20:30';
