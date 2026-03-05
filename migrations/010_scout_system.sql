-- Migration 010: Scout System — continuous restaurant availability monitoring
-- Run in Supabase SQL Editor after 009

-- =====================================================================
-- scout_campaigns: Configuration for each scout (1 per restaurant)
-- =====================================================================
CREATE TABLE IF NOT EXISTS scout_campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    venue_id INTEGER NOT NULL UNIQUE,
    venue_name TEXT NOT NULL DEFAULT '',

    -- Polling intervals
    poll_interval_s INTEGER NOT NULL DEFAULT 45,
    enhanced_interval_s INTEGER NOT NULL DEFAULT 5,
    enhanced_window_min INTEGER NOT NULL DEFAULT 10,

    -- Drop timing config (for enhanced polling around drops)
    drop_hour INTEGER,
    drop_minute INTEGER DEFAULT 0,
    drop_timezone TEXT DEFAULT 'America/New_York',
    days_ahead INTEGER DEFAULT 30,
    party_size INTEGER DEFAULT 2,

    -- State
    active BOOLEAN DEFAULT true,
    priority INTEGER DEFAULT 0,

    -- Stats (updated by scout engine)
    total_polls INTEGER DEFAULT 0,
    total_events INTEGER DEFAULT 0,
    last_poll_at TIMESTAMPTZ,
    last_event_at TIMESTAMPTZ,
    consecutive_errors INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- RLS
ALTER TABLE scout_campaigns ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access on scout_campaigns"
    ON scout_campaigns FOR ALL
    USING (auth.role() = 'service_role');

CREATE INDEX IF NOT EXISTS idx_scout_campaigns_active ON scout_campaigns(active);
CREATE INDEX IF NOT EXISTS idx_scout_campaigns_venue ON scout_campaigns(venue_id);

-- =====================================================================
-- scout_events: Change events (slot appeared/disappeared)
-- Only recorded when availability actually changes
-- =====================================================================
CREATE TABLE IF NOT EXISTS scout_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    venue_id INTEGER NOT NULL,
    target_date DATE,

    -- Event classification
    event_type TEXT NOT NULL,  -- 'slots_appeared', 'slots_disappeared', 'slots_changed'
    prev_slot_count INTEGER DEFAULT 0,
    new_slot_count INTEGER DEFAULT 0,

    -- Slot details
    slots_added JSONB DEFAULT '[]'::jsonb,
    slots_removed JSONB DEFAULT '[]'::jsonb,

    -- Pre-computed for heatmaps
    hour_et INTEGER,          -- 0-23, hour in ET when event occurred
    day_of_week INTEGER,      -- 0=Mon, 6=Sun (ISO weekday)

    -- Timing
    ms_since_last_poll REAL,
    captured_at TIMESTAMPTZ DEFAULT now()
);

-- RLS
ALTER TABLE scout_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access on scout_events"
    ON scout_events FOR ALL
    USING (auth.role() = 'service_role');

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_scout_events_venue ON scout_events(venue_id);
CREATE INDEX IF NOT EXISTS idx_scout_events_venue_date ON scout_events(venue_id, target_date);
CREATE INDEX IF NOT EXISTS idx_scout_events_type ON scout_events(event_type);
CREATE INDEX IF NOT EXISTS idx_scout_events_hour ON scout_events(hour_et);
CREATE INDEX IF NOT EXISTS idx_scout_events_captured ON scout_events(captured_at);

-- =====================================================================
-- scout_snapshots: Periodic full availability captures (every ~5 min)
-- =====================================================================
CREATE TABLE IF NOT EXISTS scout_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    venue_id INTEGER NOT NULL,
    target_date DATE,

    slot_count INTEGER DEFAULT 0,
    slots_json JSONB DEFAULT '[]'::jsonb,

    -- Pre-computed for aggregation
    hour_et INTEGER,
    day_of_week INTEGER,

    captured_at TIMESTAMPTZ DEFAULT now()
);

-- RLS
ALTER TABLE scout_snapshots ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access on scout_snapshots"
    ON scout_snapshots FOR ALL
    USING (auth.role() = 'service_role');

CREATE INDEX IF NOT EXISTS idx_scout_snapshots_venue ON scout_snapshots(venue_id);
CREATE INDEX IF NOT EXISTS idx_scout_snapshots_venue_date ON scout_snapshots(venue_id, target_date);
CREATE INDEX IF NOT EXISTS idx_scout_snapshots_captured ON scout_snapshots(captured_at);
