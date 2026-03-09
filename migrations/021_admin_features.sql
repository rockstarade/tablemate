-- ═══════════════════════════════════════════════════════════════
-- Migration 021: Admin features — price level, ban list, user locations
-- ═══════════════════════════════════════════════════════════════

-- 1. Price level on curated restaurants (1-4 dollar signs)
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS price_level INTEGER DEFAULT NULL;

-- 2. Banned phones table
CREATE TABLE IF NOT EXISTS banned_phones (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    phone      TEXT NOT NULL UNIQUE,
    reason     TEXT,
    banned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    banned_by  TEXT
);

-- 3. User location tracking columns on profiles
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS last_latitude DOUBLE PRECISION;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS last_longitude DOUBLE PRECISION;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS last_location_city TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
