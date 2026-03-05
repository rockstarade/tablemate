-- Migration 013: Scout System v3 — Precision polling, error logging, settings
-- Run in Supabase SQL Editor after 012

-- =====================================================================
-- 1. Add venue_name to events/sightings (self-contained, no enrichment)
-- =====================================================================
ALTER TABLE scout_events ADD COLUMN IF NOT EXISTS venue_name TEXT DEFAULT '';
ALTER TABLE scout_slot_sightings ADD COLUMN IF NOT EXISTS venue_name TEXT DEFAULT '';

-- =====================================================================
-- 2. Add days_ahead per campaign (from curated data, not hardcoded 30)
-- =====================================================================
ALTER TABLE scout_campaigns ADD COLUMN IF NOT EXISTS days_ahead INT DEFAULT 30;

-- =====================================================================
-- 3. Phase tracking for drop scouts
-- =====================================================================
ALTER TABLE scout_campaigns ADD COLUMN IF NOT EXISTS current_phase TEXT DEFAULT 'idle';

-- =====================================================================
-- 4. Scout error log (persistent, visible in UI)
-- =====================================================================
CREATE TABLE IF NOT EXISTS scout_errors (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    venue_id INT NOT NULL,
    venue_name TEXT DEFAULT '',
    campaign_type TEXT DEFAULT 'drop',
    error_type TEXT DEFAULT 'unknown',
    error_message TEXT DEFAULT '',
    poll_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scout_errors_venue ON scout_errors(venue_id);
CREATE INDEX IF NOT EXISTS idx_scout_errors_created ON scout_errors(created_at DESC);

-- =====================================================================
-- 5. Scout global settings (single-row, persists across restarts)
-- =====================================================================
CREATE TABLE IF NOT EXISTS scout_settings (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    -- Drop scout phases
    drop_warmup_min_minutes INT DEFAULT 15,
    drop_warmup_max_minutes INT DEFAULT 25,
    drop_verify_min_minutes INT DEFAULT 2,
    drop_verify_max_minutes INT DEFAULT 3,
    drop_activate_min_seconds NUMERIC DEFAULT 3,
    drop_activate_max_seconds NUMERIC DEFAULT 4,
    drop_poll_min_ms INT DEFAULT 20,
    drop_poll_max_ms INT DEFAULT 50,
    drop_timeout_minutes INT DEFAULT 5,
    -- Cancellation scout burst/pause
    cancel_poll_min_seconds NUMERIC DEFAULT 1,
    cancel_poll_max_seconds NUMERIC DEFAULT 7,
    cancel_burst_duration_seconds INT DEFAULT 30,
    cancel_pause_min_seconds INT DEFAULT 5,
    cancel_pause_max_seconds INT DEFAULT 15,
    -- System
    default_party_size INT DEFAULT 2,
    updated_at TIMESTAMPTZ DEFAULT now()
);
INSERT INTO scout_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
