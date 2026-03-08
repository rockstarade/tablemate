-- Migration 018: Add platform column + OpenTable restaurants + fix Estela
-- Adds 'platform' column to distinguish Resy vs OpenTable restaurants
-- Adds 'city' column for multi-city support (NYC, Boston, etc.)
-- Deactivates Estela's Resy entry (no longer on Resy)
-- Adds 7 OpenTable restaurants

-- ═══════════════════════════════════════════════════════════════
-- PART 1: Add platform and city columns
-- ═══════════════════════════════════════════════════════════════

ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS platform TEXT DEFAULT 'resy';
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS city TEXT DEFAULT 'nyc';

-- Tag Tonino as Boston
UPDATE curated_restaurants SET city = 'boston' WHERE venue_id = 73777;

-- ═══════════════════════════════════════════════════════════════
-- PART 2: Fix Estela — no longer on Resy, migrated to OpenTable
-- ═══════════════════════════════════════════════════════════════

UPDATE curated_restaurants SET is_active = false WHERE venue_id = 4288;

-- ═══════════════════════════════════════════════════════════════
-- PART 3: Add OpenTable restaurants
-- venue_ids use 80xxx range for OpenTable (OT restaurant IDs)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO curated_restaurants (
    venue_id, name, cuisine, neighborhood, url_slug,
    drop_days_ahead, drop_hour, drop_minute,
    service_start, service_end, hot_start, hot_end,
    slot_interval, sort_order, is_active, platform, city, tagline
) VALUES
    -- Don Angie: OpenTable restaurant ID 994474
    -- Switched from Resy to OpenTable May 2025. Viral pinwheel lasagna.
    -- Drops daily at 9 AM, 7 days ahead
    (80001, 'Don Angie', 'Italian', 'West Village', 'don-angie-new-york',
     7, 9, 0, '16:00', '22:30', '19:00', '20:30', 15, 55, true,
     'opentable', 'nyc', 'Iconic Italian-American. Famous pinwheel lasagna.'),

    -- Soothr: OpenTable restaurant
    -- Michelin Guide Thai noodle bar, moved from Resy to OpenTable
    (80002, 'Soothr', 'Thai', 'East Village', 'soothr-new-york',
     14, 9, 0, '12:00', '23:00', '18:00', '20:00', 15, 56, true,
     'opentable', 'nyc', 'Michelin Guide Thai noodle bar.'),

    -- Una Pizza Napoletana: OpenTable
    -- World's #1 pizza. Thu-Sat only. 14 days ahead at 9 AM.
    (80003, 'Una Pizza Napoletana', 'Pizza', 'Lower East Side', 'una-pizza-napoletana-new-york',
     14, 9, 0, '17:00', '22:30', '18:00', '20:00', 15, 57, true,
     'opentable', 'nyc', 'World''s #1 pizza. Limited covers, Thu-Sat only.'),

    -- Le Veau d'Or: OpenTable
    -- 15-table historic French bistro. 14 days ahead at 9 AM.
    (80004, 'Le Veau d''Or', 'French', 'Upper East Side', 'le-veau-dor-new-york',
     14, 9, 0, '11:30', '21:30', '18:00', '20:00', 15, 58, true,
     'opentable', 'nyc', 'Historic 1937 French bistro. Only 15 tables.'),

    -- San Sabino: OpenTable
    -- Sister to Don Angie. Switched to OpenTable May 2025.
    (80005, 'San Sabino', 'Italian Seafood', 'West Village', 'san-sabino-new-york',
     7, 9, 0, '11:30', '22:30', '19:00', '20:30', 15, 59, true,
     'opentable', 'nyc', 'Italian-American seafood. Sister restaurant to Don Angie.'),

    -- Estela: OpenTable (slug is just /r/estela, no -new-york)
    -- Migrated from Resy. Michelin star. Chef Ignacio Mattos.
    (80006, 'Estela', 'Mediterranean', 'NoLita', 'estela',
     14, 9, 0, '17:30', '23:00', '19:00', '21:00', 15, 60, true,
     'opentable', 'nyc', 'Michelin-starred Mediterranean by Ignacio Mattos.'),

    -- Altro Paradiso: OpenTable (slug is /r/altro-paradiso)
    -- Same group as Estela. Chef Ignacio Mattos.
    (80007, 'Altro Paradiso', 'Italian', 'SoHo', 'altro-paradiso',
     30, 9, 0, '12:00', '22:30', '19:00', '20:30', 15, 61, true,
     'opentable', 'nyc', 'Italian from Ignacio Mattos. Sister to Estela.')
ON CONFLICT (venue_id) DO NOTHING;
