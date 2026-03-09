-- Migration 024: Shorten taglines to fit one line on cards
-- All taglines should be descriptive but concise (~40 chars max)
-- Run in Supabase SQL Editor

-- Carbone (329): was 51 chars
UPDATE curated_restaurants SET tagline = 'Italian-American power dining, Greenwich Village'
WHERE venue_id = 329;

-- 4 Charles Prime Rib (834): was 69 chars
UPDATE curated_restaurants SET tagline = 'Subterranean speakeasy steakhouse'
WHERE venue_id = 834;

-- Torrisi (62714): was 47 chars
UPDATE curated_restaurants SET tagline = 'Italian-American revival in the Puck Building'
WHERE venue_id = 62714;

-- Via Carota (25973): was 52 chars
UPDATE curated_restaurants SET tagline = 'Beloved West Village Italian trattoria'
WHERE venue_id = 25973;

-- Tatiana (65452): was 51 chars
UPDATE curated_restaurants SET tagline = 'Kwame Onwuachi at Lincoln Center'
WHERE venue_id = 65452;

-- Le Coucou (8303): was 50 chars
UPDATE curated_restaurants SET tagline = 'Haute French in a soaring SoHo space'
WHERE venue_id = 8303;

-- The Four Horsemen (2492): was 58 chars
UPDATE curated_restaurants SET tagline = 'Natural wine and seasonal Italian plates'
WHERE venue_id = 2492;

-- Yamada (77405): was 55 chars
UPDATE curated_restaurants SET tagline = 'Michelin-starred kaiseki, two seatings nightly'
WHERE venue_id = 77405;

-- Attaboy (37368): was 61 chars
UPDATE curated_restaurants SET tagline = 'Bespoke cocktails in the old Milk & Honey'
WHERE venue_id = 37368;

-- Raines Law Room (5886): was 57 chars
UPDATE curated_restaurants SET tagline = '1920s speakeasy with bell service'
WHERE venue_id = 5886;

-- Bathtub Gin (1245): was 53 chars
UPDATE curated_restaurants SET tagline = 'Speakeasy hidden behind a coffee shop'
WHERE venue_id = 1245;

-- Nothing Really Matters (65218): was 50 chars
UPDATE curated_restaurants SET tagline = 'Underground cocktails below subway stairs'
WHERE venue_id = 65218;

-- Gabriel Kreuther (10401): was 49 chars
UPDATE curated_restaurants SET tagline = 'Alsatian-French fine dining near Bryant Park'
WHERE venue_id = 10401;

-- Jean-Georges (5951): was 46 chars
UPDATE curated_restaurants SET tagline = 'Vongerichten French-Asian flagship'
WHERE venue_id = 5951;

-- The Modern (7369): was 48 chars
UPDATE curated_restaurants SET tagline = 'Refined dining overlooking MoMA garden'
WHERE venue_id = 7369;

-- l'abeille (59006): was 47 chars
UPDATE curated_restaurants SET tagline = 'French-Japanese tasting in Tribeca'
WHERE venue_id = 59006;

-- Estela (4288): was 47 chars
UPDATE curated_restaurants SET tagline = 'Mediterranean small plates since 2013'
WHERE venue_id = 4288;

-- Kisa (84165): was 48 chars
UPDATE curated_restaurants SET tagline = 'Intimate Japanese omakase on the LES'
WHERE venue_id = 84165;

-- Ha's Snack Bar (85855): was 46 chars
UPDATE curated_restaurants SET tagline = 'Vietnamese small plates on the LES'
WHERE venue_id = 85855;

-- Tonino Boston (73777): was 64 chars
UPDATE curated_restaurants SET tagline = 'Boston 28-seat Italian gem, #1 on Resy'
WHERE venue_id = 73777;

-- Pink Door Seattle (80008): was 51 chars
UPDATE curated_restaurants SET tagline = 'Pike Place Italian with cabaret vibes'
WHERE venue_id = 80008;

-- Sushi Nakazawa (5589): clunky phrasing
UPDATE curated_restaurants SET tagline = 'Jiro Ono protege sushi counter'
WHERE venue_id = 5589;

-- Le Pavillon (50955)
UPDATE curated_restaurants SET tagline = 'Boulud seafood in One Vanderbilt'
WHERE venue_id = 50955;

-- Huso (84818): odd phrasing
UPDATE curated_restaurants SET tagline = 'Caviar-driven tasting menu in Tribeca'
WHERE venue_id = 84818;

-- Semma (1263): old tagline was timing info, not descriptive
UPDATE curated_restaurants SET tagline = 'Michelin-starred South Indian in the Village'
WHERE venue_id = 1263;

-- Gramercy Tavern (3731): was 44 chars
UPDATE curated_restaurants SET tagline = 'Seasonal American by Danny Meyer'
WHERE venue_id = 3731;

-- Borgo (84290): was 45 chars
UPDATE curated_restaurants SET tagline = 'Contemporary Italian in Flatiron'
WHERE venue_id = 84290;

-- Rubirosa (466): was 49 chars
UPDATE curated_restaurants SET tagline = 'Thin-crust pizza and red-sauce in Nolita'
WHERE venue_id = 466;

-- Golden Diner (9520): was 46 chars
UPDATE curated_restaurants SET tagline = 'Asian-American diner in Chinatown'
WHERE venue_id = 9520;

-- Bangkok Supper Club (73418): was 44 chars (borderline)
UPDATE curated_restaurants SET tagline = 'Thai fine dining in Meatpacking'
WHERE venue_id = 73418;

-- Michelin-starred Korean ramyun in the Village (1543): was 45 chars
UPDATE curated_restaurants SET tagline = 'Michelin-starred Korean ramyun'
WHERE venue_id = 1543;

-- Contemporary Korean five-course in Flatiron (58543): was 43 chars (OK but cleaner)
UPDATE curated_restaurants SET tagline = 'Contemporary Korean five-course'
WHERE venue_id = 58543;
