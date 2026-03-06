-- Migration 014: Fix incorrect drop policies + add new restaurants
-- Based on authoritative Resy blog (blog.resy.com) + cross-referenced sources
-- Run in Supabase SQL Editor
-- Idempotent: safe to run multiple times

-- ═══════════════════════════════════════════════════════════════
-- PART 1: FIX INCORRECT DROP POLICIES (existing restaurants)
-- Source: https://blog.resy.com/the-one-who-keeps-the-book/toughest-restaurant-reservations-nyc/
-- ═══════════════════════════════════════════════════════════════

-- Dhamaka: was 30d/10:00, actual 6d/midnight
UPDATE curated_restaurants SET drop_days_ahead = 6, drop_hour = 0, drop_minute = 0
WHERE venue_id = 48994;

-- I Sodi: was 30d/0:00, actual 13d/midnight
UPDATE curated_restaurants SET drop_days_ahead = 13
WHERE venue_id = 443;

-- Peter Luger: was 14d/9:00, actual 30d/noon
UPDATE curated_restaurants SET drop_days_ahead = 30, drop_hour = 12, drop_minute = 0
WHERE venue_id = 4287;

-- The Polo Bar: was 14d/9:00, actual 30d/10:00
UPDATE curated_restaurants SET drop_days_ahead = 30, drop_hour = 10, drop_minute = 0
WHERE venue_id = 6439;

-- ATOBOY: was 14d/9:00, actual 29d/9:00
UPDATE curated_restaurants SET drop_days_ahead = 29
WHERE venue_id = 587;

-- 4 Charles Prime Rib: was 14d/9:00, actual 21d/9:00
UPDATE curated_restaurants SET drop_days_ahead = 21
WHERE venue_id = 834;

-- ADDA: was 14d/9:00, actual 7d/9:00
UPDATE curated_restaurants SET drop_days_ahead = 7
WHERE venue_id = 89018;

-- Bungalow: was 14d/9:00, actual 20d/11:00
UPDATE curated_restaurants SET drop_days_ahead = 20, drop_hour = 11, drop_minute = 0
WHERE venue_id = 80201;

-- Buvette: was 7d/9:00, actual 14d/9:00
UPDATE curated_restaurants SET drop_days_ahead = 14
WHERE venue_id = 4241;

-- Dame: was 14d/9:00, actual 21d/10:00
UPDATE curated_restaurants SET drop_days_ahead = 21, drop_hour = 10, drop_minute = 0
WHERE venue_id = 34341;

-- Laser Wolf: was 30d/10:00, actual 21d/10:00
UPDATE curated_restaurants SET drop_days_ahead = 21
WHERE venue_id = 58848;

-- Monkey Bar: was 14d/9:00, actual 21d/9:00
UPDATE curated_restaurants SET drop_days_ahead = 21
WHERE venue_id = 60058;

-- Balthazar: was 10:00, actual midnight
UPDATE curated_restaurants SET drop_hour = 0, drop_minute = 0
WHERE venue_id = 50227;

-- Raoul's: was 10:00, actual 8:00
UPDATE curated_restaurants SET drop_hour = 8, drop_minute = 0
WHERE venue_id = 7241;

-- Tatiana: was 30d/10:00, actual 28d/noon
UPDATE curated_restaurants SET drop_days_ahead = 28, drop_hour = 12, drop_minute = 0
WHERE venue_id = 65452;

-- Lilia: was 30d/10:00, actual 28d/10:00
UPDATE curated_restaurants SET drop_days_ahead = 28
WHERE venue_id = 418;

-- L'Artusi: confirm 14d/9:00 (matches Resy blog)
-- No change needed

-- Cote: confirm 30d/10:00 (matches)
-- No change needed

-- COQODAQ: confirm 14d/10:00 (matches Resy blog)
-- No change needed

-- Rezdora: confirm 30d/midnight (matches)
-- No change needed

-- Semma: confirm 15d/9:00 (matches — already fixed earlier)
-- No change needed

-- Double Chicken Please: confirm 6d/midnight (matches)
-- No change needed

-- Don Angie: Mark as inactive — moved to OpenTable May 2025
UPDATE curated_restaurants SET is_active = false
WHERE venue_id = 1505;

-- Ambassador's Clubhouse: Update placeholder venue_id to real one (94741)
-- and fix neighborhood
UPDATE curated_restaurants SET venue_id = 94741, neighborhood = 'NoMad'
WHERE venue_id = 99903;


-- ═══════════════════════════════════════════════════════════════
-- PART 2: ADD NEW RESTAURANTS — Hardest to Book + Speakeasies
-- ═══════════════════════════════════════════════════════════════

INSERT INTO curated_restaurants (
    venue_id, name, cuisine, neighborhood, url_slug,
    drop_days_ahead, drop_hour, drop_minute,
    service_start, service_end, hot_start, hot_end,
    slot_interval, sort_order, is_active, tagline
) VALUES
    -- Via Carota: fix drop policy (was already in DB, just update)
    -- The Four Horsemen: 30d at 7:00 AM
    (2492, 'The Four Horsemen', 'Wine Bar', 'Williamsburg', 'the-four-horsemen',
     30, 7, 0, '17:30', '23:00', '19:00', '21:00', 15, 44, true,
     'Natural wine destination with seasonal Italian small plates'),

    -- Bistrot Ha: 6d at midnight
    (93718, 'Bistrot Ha', 'Vietnamese-French', 'LES', 'bistrot-ha',
     6, 0, 0, '17:00', '23:00', '19:00', '21:00', 15, 45, true,
     'Vietnamese soul meets French bistro technique'),

    -- I Cavallini: 14d at 8:00 AM
    (90079, 'I Cavallini', 'Italian', 'Williamsburg', 'i-cavallini',
     14, 8, 0, '17:00', '22:30', '19:00', '20:30', 15, 46, true,
     'Seasonal Italian from the Four Horsemen team'),

    -- Schmuck: 14d at 10:00 AM
    (84430, 'Schmuck', 'Cocktail Bar', 'East Village', 'schmuck-nyc',
     14, 10, 0, '17:00', '01:00', '20:00', '23:00', 30, 47, true,
     'Barcelona cocktail legends in the East Village'),

    -- Le Cafe Louis Vuitton: 28d at midnight
    (85214, 'Le Cafe Louis Vuitton', 'French', 'Midtown East', 'le-cafe-louis-vuitton',
     28, 0, 0, '12:00', '22:00', '19:00', '20:30', 15, 48, true,
     'Louis Vuitton fine dining on Fifth Avenue'),

    -- Yamada: 18d ahead
    (77405, 'Yamada', 'Japanese Kaiseki', 'Chinatown', 'yamada',
     18, 10, 0, '17:30', '22:00', '17:30', '20:30', 30, 49, true,
     'Michelin-starred kaiseki with only two seatings a night'),

    -- Bemelmans Bar: ~90d ahead
    (85500, 'Bemelmans Bar', 'Cocktail Bar', 'Upper East Side', 'bemelmans-bar',
     90, 10, 0, '17:00', '01:00', '20:00', '23:00', 30, 50, true,
     'Art Deco glamour and live jazz at The Carlyle'),

    -- PDT (Please Don't Tell): 7d ahead
    (10500, 'PDT', 'Speakeasy', 'East Village', 'pdt',
     7, 10, 0, '17:00', '02:00', '20:00', '00:00', 30, 51, true,
     'Enter through a phone booth in a hot dog shop'),

    -- Attaboy: walk-in mostly, limited Resy
    (37368, 'Attaboy', 'Speakeasy', 'LES', 'attaboy',
     7, 10, 0, '18:00', '02:00', '21:00', '00:00', 30, 52, true,
     'No menu, bespoke cocktails in the original Milk & Honey space'),

    -- Raines Law Room: varies
    (5886, 'Raines Law Room', 'Speakeasy', 'Chelsea', 'raines-law-room',
     14, 10, 0, '17:00', '02:00', '20:00', '23:00', 30, 53, true,
     '1920s cocktail parlor with velvet couches and bell service'),

    -- Bathtub Gin: varies
    (1245, 'Bathtub Gin', 'Speakeasy', 'Chelsea', 'bathtub-gin',
     14, 10, 0, '18:00', '02:00', '20:00', '23:00', 30, 54, true,
     'Prohibition-era speakeasy hidden behind a coffee shop'),

    -- Nothing Really Matters: varies
    (65218, 'Nothing Really Matters', 'Cocktail Bar', 'Midtown', 'nothing-really-matters',
     14, 10, 0, '17:00', '02:00', '20:00', '23:00', 30, 55, true,
     'Subterranean cocktails down a set of subway stairs'),

    -- Misi: 30d at midnight
    (3015, 'Misi', 'Italian', 'Williamsburg', 'misi',
     30, 0, 0, '17:00', '22:30', '19:00', '20:30', 15, 56, true,
     'Waterfront pasta from the Lilia team'),

    -- Rubirosa: 7d at midnight
    (466, 'Rubirosa', 'Italian', 'Nolita', 'rubirosa',
     7, 0, 0, '11:30', '23:00', '19:00', '20:30', 15, 57, true,
     'Thin-crust pizza and red-sauce classics in Nolita'),

    -- Golden Diner: 30d at midnight
    (9520, 'Golden Diner', 'American-Asian', 'Chinatown', 'golden-diner',
     30, 0, 0, '10:00', '15:00', '11:00', '13:00', 15, 58, true,
     'All-day diner with Asian-American comfort food'),

    -- Bangkok Supper Club: 30d at midnight
    (73418, 'Bangkok Supper Club', 'Thai', 'Meatpacking', 'bangkok-supper-club',
     30, 0, 0, '17:00', '23:00', '19:00', '21:00', 15, 59, true,
     'Thai fine dining in the Meatpacking District'),

    -- Kisa: 15d at midnight
    (84165, 'Kisa', 'Japanese', 'LES', 'kisa',
     15, 0, 0, '17:00', '22:00', '19:00', '20:30', 30, 60, true,
     'Intimate Japanese omakase on the Lower East Side'),

    -- Bridges: 21d at noon
    (83681, 'Bridges', 'Chinese', 'Chinatown', 'bridges',
     21, 12, 0, '17:00', '22:00', '19:00', '20:30', 15, 61, true,
     'Modern Chinese cuisine in Chinatown'),

    -- Theodora: 30d at 9 AM
    (73589, 'Theodora', 'Mediterranean', 'Fort Greene', 'theodora',
     30, 9, 0, '17:00', '22:00', '19:00', '20:30', 15, 62, true,
     'Mediterranean dining in Fort Greene, Brooklyn'),

    -- Penny: 14d at 9 AM
    (79460, 'Penny', 'Italian', 'East Village', 'penny',
     14, 9, 0, '17:00', '22:30', '19:00', '20:30', 15, 63, true,
     'Neighborhood Italian in the East Village'),

    -- Bong: 20d at midnight
    (86413, 'Bong', 'Korean', 'Crown Heights', 'bong',
     20, 0, 0, '17:00', '22:00', '19:00', '20:30', 15, 64, true,
     'Korean fine dining in Crown Heights'),

    -- Le Chene: 14d at 9 AM
    (88264, 'Le Chene', 'French', 'West Village', 'le-chene',
     14, 9, 0, '17:30', '22:30', '19:00', '20:30', 15, 65, true,
     'French bistro in the West Village'),

    -- Ha''s Snack Bar: 21d at noon
    (85855, 'Ha''s Snack Bar', 'Vietnamese', 'LES', 'has-snack-bar',
     21, 12, 0, '17:00', '22:00', '19:00', '20:30', 15, 66, true,
     'Vietnamese small plates on the Lower East Side'),

    -- Lei: 14d at 9 AM
    (89991, 'Lei', 'Chinese', 'Chinatown', 'lei',
     14, 9, 0, '17:00', '22:00', '19:00', '20:30', 15, 67, true,
     'Modern Chinese in Chinatown'),

    -- Borgo: 21d at 10 AM
    (84290, 'Borgo', 'Italian', 'Flatiron', 'borgo',
     21, 10, 0, '17:00', '22:30', '19:00', '20:30', 15, 68, true,
     'Contemporary Italian in the Flatiron District'),

    -- Ramen By Ra: twice monthly (1st+15th) at 9 AM
    (79848, 'Ramen By Ra', 'Japanese Ramen', 'East Village', 'ramen-by-ra',
     15, 9, 0, '11:30', '22:00', '18:00', '20:00', 30, 69, true,
     'Cult-following ramen with twice-monthly drops')

ON CONFLICT (venue_id) DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- PART 2B: FIX Le Bernardin drop policy (Michelin research)
-- Was 30d/10:00, actually ~60d/9:00
-- ═══════════════════════════════════════════════════════════════
UPDATE curated_restaurants SET drop_days_ahead = 60, drop_hour = 9, drop_minute = 0
WHERE venue_id = 1387;


-- ═══════════════════════════════════════════════════════════════
-- PART 3: ADD MICHELIN-STARRED RESTAURANTS (not already in DB)
-- Source: Michelin Guide NYC 2025 + Resy blog
-- Restaurants already in DB: EMP (70928), Le Bernardin (1387),
--   Atomix (7253), COTE (72271), Torrisi (64593), Semma (1263),
--   Le Coucou (8303), Jua (8266), Rezdora (5771), Dhamaka (48994)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO curated_restaurants (
    venue_id, name, cuisine, neighborhood, url_slug,
    drop_days_ahead, drop_hour, drop_minute,
    service_start, service_end, hot_start, hot_end,
    slot_interval, sort_order, is_active, tagline
) VALUES
    -- 2-star Michelin
    (78907, 'Chef''s Table at Brooklyn Fare', 'French-Japanese', 'Hell''s Kitchen', 'chefs-table-at-brooklyn-fare',
     30, 10, 0, '18:00', '22:00', '19:00', '20:30', 30, 70, true,
     'Intimate counter for French-Japanese omakase'),

    (85216, 'Cesar', 'French-Japanese Seafood', 'Hudson Square', 'cesar',
     30, 10, 0, '17:30', '22:00', '19:00', '20:30', 30, 71, true,
     'Refined seafood tasting menu in Hudson Square'),

    (10401, 'Gabriel Kreuther', 'French', 'Bryant Park', 'gabriel-kreuther',
     30, 10, 0, '12:00', '22:00', '19:00', '21:00', 30, 72, true,
     'Alsatian-inspired French cuisine near Bryant Park'),

    (5951, 'Jean-Georges', 'French-Asian', 'Upper West Side', 'jean-georges',
     30, 10, 0, '12:00', '22:00', '19:00', '20:30', 30, 73, true,
     'Vongerichten flagship French-Asian fine dining'),

    (82455, 'Joo Ok', 'Korean', 'Koreatown', 'joo-ok',
     30, 10, 0, '17:30', '22:00', '19:00', '20:30', 30, 74, true,
     'Korean royal cuisine from Seoul'),

    (7369, 'The Modern', 'Contemporary American', 'Midtown', 'the-modern',
     30, 10, 0, '12:00', '22:00', '19:00', '20:30', 30, 75, true,
     'Refined dining overlooking MoMA sculpture garden'),

    (52855, 'Saga', 'New American', 'Financial District', 'saga-ny',
     30, 10, 0, '17:30', '22:00', '19:00', '20:30', 30, 76, true,
     'Sky-high tasting menu with 360-degree views'),

    -- 1-star Michelin (not already in DB)
    (10726, 'Crown Shy', 'New American', 'Financial District', 'crown-shy',
     21, 12, 0, '17:00', '22:00', '19:00', '20:30', 15, 77, true,
     'Refined American dining at 70 Pine Street'),

    (29947, 'Restaurant Daniel', 'French', 'Upper East Side', 'daniel',
     30, 10, 0, '17:00', '22:00', '19:00', '20:30', 30, 78, true,
     'Daniel Boulud grand French flagship'),

    (73320, 'Cafe Boulud at Maison Barnes', 'French', 'Upper East Side', 'cafe-boulud-at-maison-barnes',
     30, 10, 0, '12:00', '22:00', '19:00', '20:30', 15, 79, true,
     'Daniel Boulud beloved French brasserie'),

    (74270, 'Corima', 'Modern Mexican', 'Chinatown', 'corima',
     30, 10, 0, '17:30', '22:00', '19:00', '20:30', 30, 80, true,
     'Modern Mexican tasting menu in Chinatown'),

    (29371, 'Dirt Candy', 'Vegetable-Forward', 'LES', 'dirt-candy',
     14, 10, 0, '17:30', '22:00', '19:00', '20:30', 15, 81, true,
     'Pioneering vegetable-forward tasting menu'),

    (4288, 'Estela', 'Mediterranean', 'Nolita', 'estela',
     14, 10, 0, '17:30', '23:00', '19:30', '21:00', 15, 82, true,
     'Brilliant Mediterranean small plates since 2013'),

    (3731, 'Gramercy Tavern', 'American', 'Flatiron', 'gramercy-tavern',
     30, 10, 0, '12:00', '22:00', '19:00', '20:30', 15, 83, true,
     'Seasonal American fine dining by Danny Meyer'),

    (84818, 'Huso', 'Contemporary Seafood', 'Tribeca', 'huso-nyc',
     30, 10, 0, '17:30', '22:00', '19:00', '20:30', 30, 84, true,
     'Top Chef winner caviar-fueled tasting menu'),

    (1543, 'Jeju Noodle Bar', 'Korean', 'West Village', 'jeju-noodle-bar',
     14, 10, 0, '17:00', '22:00', '19:00', '20:30', 15, 85, true,
     'Michelin-starred Korean ramyun in the Village'),

    (62836, 'Joji', 'Japanese Omakase', 'Midtown East', 'joji',
     14, 10, 0, '17:30', '22:00', '18:30', '20:30', 30, 86, true,
     'Hidden omakase below One Vanderbilt'),

    (8346, 'Kochi', 'Korean', 'Midtown West', 'kochi',
     14, 10, 0, '17:00', '22:00', '19:00', '20:30', 30, 87, true,
     'Per Se vet Korean skewer tasting counter'),

    (59006, 'l''abeille', 'French-Japanese', 'Tribeca', 'labielle',
     30, 10, 0, '17:30', '22:00', '19:00', '20:30', 30, 88, true,
     'French-Japanese harmony on Tribeca cobblestones'),

    (50955, 'Le Pavillon', 'French Seafood', 'Midtown East', 'le-pavillon',
     30, 10, 0, '12:00', '22:00', '19:00', '20:30', 30, 89, true,
     'Boulud seafood showcase in One Vanderbilt'),

    (54791, 'Mari', 'Korean', 'Hell''s Kitchen', 'mari',
     14, 10, 0, '17:00', '22:00', '19:00', '20:30', 30, 90, true,
     'Hand roll tasting menu from Kochi chef'),

    (79842, 'Meju', 'Korean', 'Long Island City', 'meju',
     14, 10, 0, '17:30', '22:00', '18:30', '20:30', 30, 91, true,
     'Eight-seat Korean fermentation counter'),

    (73578, 'Noksu', 'Contemporary Korean', 'Koreatown', 'noksu',
     14, 10, 0, '17:30', '22:00', '18:00', '20:30', 30, 92, true,
     'Hidden Korean omakase inside a subway station'),

    (58543, 'Oiji Mi', 'Korean', 'Flatiron', 'oiji-mi',
     14, 10, 0, '17:30', '22:00', '19:00', '20:30', 15, 93, true,
     'Contemporary Korean five-course in Flatiron'),

    (2601, 'Oxomoco', 'Mexican', 'Greenpoint', 'oxomoco',
     14, 10, 0, '17:30', '22:30', '19:30', '21:00', 15, 94, true,
     'Wood-fired Mexican with a glowing backyard'),

    (888, 'Shion 69 Leonard Street', 'Japanese Omakase', 'Tribeca', '69leonardstreet',
     30, 10, 0, '17:30', '22:00', '18:30', '20:30', 30, 95, true,
     'Edomae sushi from Sushi Saito protege'),

    (59072, 'Shmone', 'Mediterranean', 'Greenwich Village', 'shmone',
     14, 10, 0, '17:30', '22:00', '19:00', '20:30', 15, 96, true,
     'Intimate Mediterranean gem in the Village'),

    (73542, 'Shota Omakase', 'Japanese Sushi', 'Williamsburg', 'shota-omakase',
     14, 10, 0, '17:30', '22:00', '18:30', '20:30', 30, 97, true,
     'Intimate sushi counter in Williamsburg'),

    (52711, 'Sixty Three Clinton', 'New American', 'LES', 'sixty-three-clinton',
     30, 10, 0, '18:00', '23:00', '19:00', '20:30', 30, 98, true,
     'Innovative seven-course tasting on the LES'),

    (5589, 'Sushi Nakazawa', 'Japanese Sushi', 'West Village', 'sushi-nakazawa',
     30, 10, 0, '17:00', '22:00', '19:00', '20:30', 30, 99, true,
     'Jiro Ono protege celebrated sushi counter'),

    (7318, 'Torien', 'Japanese Yakitori', 'NoHo', 'torien',
     14, 10, 0, '17:30', '22:00', '19:00', '20:30', 30, 100, true,
     'Counter-only yakitori omakase in NoHo'),

    (117, 'Tuome', 'New American-Asian', 'East Village', 'tuome',
     14, 10, 0, '17:30', '22:00', '19:00', '20:30', 15, 101, true,
     'Chinese-influenced fine dining, EMP alumni')

ON CONFLICT (venue_id) DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- PART 4: UPDATE is_hot flags
-- Set trending restaurants based on current demand
-- ═══════════════════════════════════════════════════════════════

-- Reset all hot flags first
UPDATE curated_restaurants SET is_hot = false WHERE is_hot = true;

-- Set current hot restaurants (most in-demand right now)
UPDATE curated_restaurants SET is_hot = true
WHERE venue_id IN (
    329,    -- Carbone
    834,    -- 4 Charles Prime Rib
    7253,   -- Atomix
    1263    -- Semma
);


-- ═══════════════════════════════════════════════════════════════
-- PART 5: ADD taglines to existing restaurants that don't have one
-- ═══════════════════════════════════════════════════════════════

UPDATE curated_restaurants SET tagline = 'Italian-American power dining in Greenwich Village' WHERE venue_id = 329 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'A 30-seat subterranean steakhouse with the mystique of a private club' WHERE venue_id = 834 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Inventive Italian-American in the Puck Building' WHERE venue_id = 62714 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Michelin-starred South Indian in the Village' WHERE venue_id = 1263 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Italian trattoria everyone loves in the West Village' WHERE venue_id = 25973 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Intimate Tuscan Italian with only 8 tables' WHERE venue_id = 443 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'NYC crown jewel by Kwame Onwuachi at Lincoln Center' WHERE venue_id = 65452 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Sumptuous French classics in a soaring SoHo space' WHERE venue_id = 8303 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'French bistro charm in SoHo since 2004' WHERE venue_id = 7241 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'The pasta temple in a Williamsburg garage' WHERE venue_id = 418 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Israeli grill on a Williamsburg rooftop' WHERE venue_id = 58848 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'French brasserie institution in SoHo' WHERE venue_id = 50227 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Michelin-starred Korean steakhouse in Flatiron' WHERE venue_id = 72271 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Eric Ripert temple of French seafood' WHERE venue_id = 1387 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Iconic Williamsburg steakhouse since 1887' WHERE venue_id = 4287 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Korean fried chicken perfection in Flatiron' WHERE venue_id = 76033 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Two-Michelin-starred Korean tasting menu' WHERE venue_id = 7253 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Three-Michelin-starred iconic tasting menu' WHERE venue_id = 70928 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Emilia-Romagna pastas, handmade daily' WHERE venue_id = 5771 AND (tagline IS NULL OR tagline = '');
UPDATE curated_restaurants SET tagline = 'Indian street food fireworks on the LES' WHERE venue_id = 48994 AND (tagline IS NULL OR tagline = '');
