-- Migration 009: Add is_hot column for featured/trending restaurants
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS is_hot BOOLEAN DEFAULT false;

-- Mark the hot restaurants
UPDATE curated_restaurants SET is_hot = true WHERE venue_id IN (
    329,    -- Carbone
    834,    -- 4 Charles Prime Rib
    62714,  -- Torrisi
    7253    -- Atomix
);
