-- Migration 004: Reservation groups + service hours for smart calendar
-- Run in Supabase SQL Editor after 003

-- ============================================================================
-- Reservation group support: multi-date cancellation sniping
-- ============================================================================

-- group_id links multiple reservations for the same restaurant.
-- When ANY reservation in a group confirms, all others auto-cancel.
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS group_id UUID;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS book_earliest BOOLEAN DEFAULT false;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS latest_notify_hours FLOAT DEFAULT 2.0;
ALTER TABLE reservations ADD COLUMN IF NOT EXISTS poll_tier TEXT DEFAULT 'warm';

CREATE INDEX IF NOT EXISTS idx_reservations_group_id ON reservations(group_id);

-- ============================================================================
-- Restaurant service hours for time picker + hot zone
-- ============================================================================

ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS service_start TEXT DEFAULT '17:00';
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS service_end TEXT DEFAULT '22:00';
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS hot_start TEXT DEFAULT '19:00';
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS hot_end TEXT DEFAULT '20:30';

-- Update with restaurant-specific service hours (sensible defaults)
-- User will provide exact hours later; these are reasonable estimates
UPDATE curated_restaurants SET service_start = '17:00', service_end = '23:00' WHERE venue_id = 329;   -- Carbone
UPDATE curated_restaurants SET service_start = '17:00', service_end = '23:00' WHERE venue_id = 834;   -- 4 Charles Prime Rib
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:30' WHERE venue_id = 62714; -- Torrisi
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:30' WHERE venue_id = 1505;  -- Don Angie
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:00' WHERE venue_id = 1263;  -- Semma
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:30' WHERE venue_id = 25973; -- Via Carota
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:00' WHERE venue_id = 443;   -- I Sodi
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:30' WHERE venue_id = 65452; -- Tatiana
UPDATE curated_restaurants SET service_start = '17:30', service_end = '23:00' WHERE venue_id = 8303;  -- Le Coucou
UPDATE curated_restaurants SET service_start = '17:30', service_end = '23:00' WHERE venue_id = 7241;  -- Raoul's
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:30' WHERE venue_id = 418;   -- Lilia
UPDATE curated_restaurants SET service_start = '17:00', service_end = '22:00' WHERE venue_id = 58848; -- Laser Wolf
UPDATE curated_restaurants SET service_start = '12:00', service_end = '23:00' WHERE venue_id = 50227; -- Balthazar (brunch + dinner)
