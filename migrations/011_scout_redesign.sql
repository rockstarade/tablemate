-- Migration 011: Scout System Redesign — two scout types + per-slot lifecycle tracking
-- Run in Supabase SQL Editor after 010

-- =====================================================================
-- Extend scout_campaigns with type and target_dates
-- =====================================================================
ALTER TABLE scout_campaigns ADD COLUMN IF NOT EXISTS type TEXT DEFAULT 'drop';
ALTER TABLE scout_campaigns ADD COLUMN IF NOT EXISTS target_dates JSONB DEFAULT '[]'::jsonb;

-- Drop the old UNIQUE constraint on venue_id (allow drop + date for same venue)
ALTER TABLE scout_campaigns DROP CONSTRAINT IF EXISTS scout_campaigns_venue_id_key;

-- New unique constraint: one campaign per venue per type
CREATE UNIQUE INDEX IF NOT EXISTS idx_scout_campaigns_venue_type
    ON scout_campaigns(venue_id, type);

-- =====================================================================
-- scout_slot_sightings: Per-slot lifecycle tracking for Date Scouts
-- Each row = one "life" of a slot (appeared → disappeared)
-- =====================================================================
CREATE TABLE IF NOT EXISTS scout_slot_sightings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    venue_id INTEGER NOT NULL,
    target_date DATE NOT NULL,
    slot_time TEXT NOT NULL,        -- e.g., "2026-03-26 19:30:00"
    slot_type TEXT DEFAULT '',      -- e.g., "Dining Room"

    -- Lifecycle timestamps
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    gone_at TIMESTAMPTZ,           -- NULL if still visible
    duration_seconds REAL,         -- computed when gone_at is set

    -- Tracking
    poll_count INTEGER DEFAULT 1,  -- how many consecutive polls saw this slot
    campaign_id UUID,

    created_at TIMESTAMPTZ DEFAULT now()
);

-- RLS
ALTER TABLE scout_slot_sightings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access on scout_slot_sightings"
    ON scout_slot_sightings FOR ALL
    USING (auth.role() = 'service_role');

-- Indexes
CREATE INDEX IF NOT EXISTS idx_sightings_venue_date
    ON scout_slot_sightings(venue_id, target_date);

CREATE INDEX IF NOT EXISTS idx_sightings_active
    ON scout_slot_sightings(venue_id, target_date)
    WHERE gone_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_sightings_venue
    ON scout_slot_sightings(venue_id);

CREATE INDEX IF NOT EXISTS idx_sightings_first_seen
    ON scout_slot_sightings(first_seen_at DESC);
