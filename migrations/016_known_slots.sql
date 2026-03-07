-- Add known_slots column to curated_restaurants.
-- Stores actual time slots observed from scout data as a JSON array.
-- Example: ["17:00","17:15","17:30","19:00","19:15","19:30","21:00","21:15","21:30"]
-- When present, the frontend uses these exact times instead of generating
-- a continuous range from service_start to service_end.

ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS known_slots JSONB;

-- Semma (venue_id 1263): observed from scout run on 2026-03-06
-- Drop date 2026-03-21: 9 slots in 3 clusters (5pm, 7pm, 9pm) at 15-min intervals
UPDATE curated_restaurants
SET known_slots = '["17:00","17:15","17:30","19:00","19:15","19:30","21:00","21:15","21:30"]'::jsonb
WHERE venue_id = 1263;
