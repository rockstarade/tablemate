-- Migration 006: Add slot_interval column + all remaining curated restaurants
-- Combines migration 005 (if not yet run) + 8 new restaurants
-- Run in Supabase SQL Editor
--
-- Idempotent: safe to run multiple times

-- Add slot_interval column (15 or 30 minute reservation slots)
ALTER TABLE curated_restaurants ADD COLUMN IF NOT EXISTS slot_interval INTEGER DEFAULT 15;

INSERT INTO curated_restaurants
    (venue_id, name, cuisine, neighborhood, url_slug,
     slot_interval, drop_days_ahead, drop_hour, drop_minute,
     service_start, service_end, hot_start, hot_end,
     sort_order)
VALUES
    -- From migration 005 (safe to re-run)
    (72271, 'Cote Korean Steakhouse', 'Korean BBQ', 'Flatiron', 'cote-korean-steakhouse', 15, 30, 10, 0, '17:00', '23:00', '19:00', '21:00', 14),
    (290, 'L''Artusi', 'Italian', 'West Village', 'lartusi', 15, 14, 9, 0, '17:00', '23:00', '19:00', '20:30', 15),
    (1387, 'Le Bernardin', 'French/Seafood', 'Midtown West', 'le-bernardin', 30, 30, 10, 0, '17:00', '22:30', '19:00', '20:30', 16),
    (4287, 'Peter Luger', 'Steakhouse', 'Williamsburg', 'peter-luger-steak-house', 15, 14, 9, 0, '11:45', '21:45', '18:30', '20:00', 17),
    (59536, 'Wenwen', 'Chinese', 'Greenpoint', 'wenwen', 15, 14, 9, 0, '17:00', '22:00', '19:00', '20:30', 18),
    (49453, 'Thai Diner', 'Thai', 'Nolita', 'thai-diner', 15, 7, 9, 0, '11:00', '22:00', '19:00', '20:30', 19),
    (8266, 'Jua', 'Korean', 'Flatiron', 'jua', 30, 30, 10, 0, '17:00', '22:00', '19:00', '20:30', 20),
    (62659, 'Claud', 'American', 'East Village', 'claud', 15, 14, 9, 0, '17:30', '22:00', '19:00', '20:30', 21),
    (48994, 'Dhamaka', 'Indian', 'LES', 'dhamaka', 15, 30, 10, 0, '17:00', '23:00', '19:00', '20:30', 22),
    (76033, 'COQODAQ', 'Korean Fried Chicken', 'Flatiron', 'coqodaq', 15, 14, 10, 0, '17:00', '22:30', '19:00', '20:30', 23),
    (34341, 'Dame', 'Seafood', 'West Village', 'dame', 15, 14, 9, 0, '17:00', '22:00', '19:00', '20:30', 24),
    (42534, 'Double Chicken Please', 'Cocktail Bar', 'LES', 'double-chicken-please', 30, 6, 0, 0, '17:00', '00:00', '20:00', '22:00', 25),
    (5771, 'Rezdora', 'Italian', 'Flatiron', 'rezdora', 15, 30, 0, 0, '17:00', '22:30', '19:00', '20:30', 26),
    (7253, 'Atomix', 'Korean Tasting', 'Tribeca', 'atomix', 30, 30, 10, 0, '17:00', '22:00', '19:00', '20:30', 27),

    -- New restaurants (batch 2)
    (89018, 'ADDA', 'Indian', 'East Village', 'adda', 15, 14, 9, 0, '17:00', '22:30', '19:00', '20:30', 28),
    (587,   'ATOBOY', 'Korean', 'NoMad', 'atoboy', 15, 14, 9, 0, '17:00', '22:00', '19:00', '20:30', 29),
    (54591, 'Bonnie''s', 'Cantonese-Caribbean', 'Williamsburg', 'bonnies', 15, 14, 9, 0, '17:00', '22:30', '19:00', '20:30', 30),
    (80201, 'Bungalow', 'Indian', 'East Village', 'bungalow-ny', 15, 14, 9, 0, '17:00', '22:30', '19:00', '20:30', 31),
    (4241,  'Buvette', 'French', 'West Village', 'buvette-nyc', 15, 7, 9, 0, '08:00', '23:00', '19:00', '20:30', 32),
    (70928, 'Eleven Madison Park', 'Fine Dining', 'Flatiron', 'eleven-madison-park', 30, 30, 10, 0, '17:00', '22:00', '19:00', '20:30', 33),
    (60058, 'Monkey Bar', 'American', 'Midtown East', 'monkey-bar-nyc', 15, 14, 9, 0, '17:00', '23:00', '19:00', '20:30', 34),
    (6439,  'The Polo Bar', 'American', 'Midtown East', 'the-polo-bar', 15, 14, 9, 0, '17:00', '23:00', '19:00', '20:30', 35)
ON CONFLICT (venue_id) DO NOTHING;
