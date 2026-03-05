-- Migration 012: Scout System v2 — Polling windows, rename date→cancellation, cleanup
-- Run in Supabase SQL Editor after 011

-- =====================================================================
-- 1. Add polling_windows JSONB column
--    Format: [{"start": "09:45", "end": "10:30"}, {"start": "13:00", "end": "15:00"}]
-- =====================================================================
ALTER TABLE scout_campaigns ADD COLUMN IF NOT EXISTS polling_windows JSONB DEFAULT '[]'::jsonb;

-- =====================================================================
-- 2. Rename type "date" → "cancellation" for any existing campaigns
-- =====================================================================
UPDATE scout_campaigns SET type = 'cancellation' WHERE type = 'date';

-- =====================================================================
-- 3. Clear all existing auto-populated campaigns (start fresh)
-- =====================================================================
DELETE FROM scout_campaigns;

-- =====================================================================
-- 4. Add check constraint on type values
-- =====================================================================
ALTER TABLE scout_campaigns DROP CONSTRAINT IF EXISTS scout_campaigns_type_check;
ALTER TABLE scout_campaigns ADD CONSTRAINT scout_campaigns_type_check
    CHECK (type IN ('drop', 'cancellation'));
