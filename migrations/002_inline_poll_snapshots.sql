-- Inline Poll Snapshots + Polling Tiers
-- Run this in the Supabase SQL editor AFTER 001_drop_intelligence.sql

-- ============================================================
-- 1. New columns on slot_snapshots for inline poll recording
-- ============================================================

-- Milliseconds since the snipe loop began (absolute timeline)
ALTER TABLE slot_snapshots ADD COLUMN IF NOT EXISTS ms_since_start DOUBLE PRECISION;

-- Where the snapshot came from: 'snipe_inline' | 'post_drop' | 'collector' | 'monitor'
ALTER TABLE slot_snapshots ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'post_drop';

-- Links snapshot back to a specific snipe job
ALTER TABLE slot_snapshots ADD COLUMN IF NOT EXISTS reservation_id UUID;

-- Index for querying snapshots by reservation (all polls from one snipe)
CREATE INDEX IF NOT EXISTS idx_snapshots_reservation ON slot_snapshots(reservation_id);

-- Index for querying by source type
CREATE INDEX IF NOT EXISTS idx_snapshots_source ON slot_snapshots(source);

-- ============================================================
-- 2. Polling tier on tracked_venues
-- ============================================================

-- 'aggressive' = 1 poll/sec continuous monitoring
-- 'passive' = only poll during drop window (T-5s to T+15s)
ALTER TABLE tracked_venues ADD COLUMN IF NOT EXISTS poll_tier TEXT DEFAULT 'passive';

-- ============================================================
-- 3. Latency + Imperva tracking on drop_observations
-- ============================================================

-- Round-trip latency to Resy API during this observation (ms)
ALTER TABLE drop_observations ADD COLUMN IF NOT EXISTS proxy_latency_ms DOUBLE PRECISION;
ALTER TABLE drop_observations ADD COLUMN IF NOT EXISTS direct_latency_ms DOUBLE PRECISION;

-- How many Imperva cookies were active during this observation
ALTER TABLE drop_observations ADD COLUMN IF NOT EXISTS imperva_cookies_count INT;
