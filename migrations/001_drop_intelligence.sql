-- Drop Intelligence tables for Tablement
-- Run this in the Supabase SQL editor

-- ============================================================
-- 1. Tracked venues — restaurants we're collecting drop data on
-- ============================================================
CREATE TABLE IF NOT EXISTS tracked_venues (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    venue_id INT NOT NULL UNIQUE,
    venue_name TEXT NOT NULL DEFAULT '',
    drop_hour INT NOT NULL DEFAULT 10,        -- Expected drop hour (0-23)
    drop_minute INT NOT NULL DEFAULT 0,       -- Expected drop minute (0-59)
    drop_timezone TEXT NOT NULL DEFAULT 'America/New_York',
    days_ahead INT NOT NULL DEFAULT 30,
    party_size INT NOT NULL DEFAULT 2,
    active BOOLEAN NOT NULL DEFAULT TRUE,     -- Whether to actively poll
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 2. Drop observations — each time we observe slots appearing
-- ============================================================
CREATE TABLE IF NOT EXISTS drop_observations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    venue_id INT NOT NULL,
    venue_name TEXT NOT NULL DEFAULT '',
    target_date TEXT NOT NULL,                 -- The date the slots are for (YYYY-MM-DD)
    expected_drop_at TIMESTAMPTZ NOT NULL,     -- When we expected the drop (10:00:00.000)
    actual_drop_at TIMESTAMPTZ NOT NULL,       -- When slots actually appeared
    offset_ms DOUBLE PRECISION NOT NULL,       -- actual - expected in milliseconds
    slots_found INT NOT NULL DEFAULT 0,        -- How many slots appeared
    slot_types TEXT[] DEFAULT '{}',            -- Array of seating types seen
    first_slot_time TEXT,                       -- Earliest slot time
    last_slot_time TEXT,                        -- Latest slot time
    poll_attempt INT NOT NULL DEFAULT 0,       -- Which poll attempt caught it
    poll_interval_ms DOUBLE PRECISION,         -- Avg polling interval during this observation
    resy_clock_offset_ms DOUBLE PRECISION,     -- Resy server clock - local clock in ms
    source TEXT NOT NULL DEFAULT 'collector',   -- 'collector' (background) or 'snipe' (real booking)
    booking_result TEXT,                        -- 'booked', 'failed', 'dry_run', NULL (collector)
    booking_elapsed_ms DOUBLE PRECISION,       -- Time from slots appearing to booking confirmed
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_drop_obs_venue ON drop_observations(venue_id);
CREATE INDEX IF NOT EXISTS idx_drop_obs_date ON drop_observations(target_date);

-- ============================================================
-- 3. Slot snapshots — periodic slot counts for velocity tracking
-- ============================================================
CREATE TABLE IF NOT EXISTS slot_snapshots (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    venue_id INT NOT NULL,
    target_date TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    slot_count INT NOT NULL DEFAULT 0,
    slots_json JSONB,                          -- Full slot data (time, type, token)
    ms_since_drop DOUBLE PRECISION,            -- Milliseconds since the drop
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snapshots_venue_date ON slot_snapshots(venue_id, target_date);

-- ============================================================
-- 4. Slot claims — track which Tablement users claimed which slots
-- ============================================================
CREATE TABLE IF NOT EXISTS slot_claims (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id),
    venue_id INT NOT NULL,
    venue_name TEXT NOT NULL DEFAULT '',
    target_date TEXT NOT NULL,                 -- YYYY-MM-DD
    preferred_time TEXT NOT NULL,              -- HH:MM
    seating_type TEXT,                         -- Dining Room, Bar, etc.
    window_minutes INT NOT NULL DEFAULT 30,
    reservation_id UUID REFERENCES reservations(id),
    status TEXT NOT NULL DEFAULT 'active',     -- active, completed, cancelled
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_claims_venue_date ON slot_claims(venue_id, target_date);
CREATE INDEX IF NOT EXISTS idx_claims_user ON slot_claims(user_id);

-- Unique constraint: one user can't claim the same time twice at the same venue
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_unique
    ON slot_claims(user_id, venue_id, target_date, preferred_time)
    WHERE status = 'active';
