-- Migration 005: Add more curated restaurants
-- NOTE: Superseded by migration 006 which includes all these restaurants
-- plus 8 more and adds the slot_interval column. Both are idempotent.
-- Run in Supabase SQL Editor after migrations 003 and 004

INSERT INTO curated_restaurants
    (venue_id, name, cuisine, neighborhood, url_slug, tagline,
     drop_days_ahead, drop_hour, drop_minute,
     service_start, service_end, hot_start, hot_end,
     sort_order)
VALUES
    (72271, 'Cote Korean Steakhouse', 'Korean BBQ', 'Flatiron', 'cote-korean-steakhouse', 'All-you-can-eat Korean steakhouse perfection', 30, 10, 0, '17:00', '23:00', '19:00', '21:00', 14),
    (290, 'L''Artusi', 'Italian', 'West Village', 'lartusi', 'West Village pasta institution', 14, 9, 0, '17:00', '23:00', '19:00', '20:30', 15),
    (1387, 'Le Bernardin', 'French/Seafood', 'Midtown West', 'le-bernardin', 'The gold standard of seafood', 30, 10, 0, '17:00', '22:30', '19:00', '20:30', 16),
    (4287, 'Peter Luger', 'Steakhouse', 'Williamsburg', 'peter-luger-steak-house', 'The OG Brooklyn steakhouse since 1887', 14, 9, 0, '11:45', '21:45', '18:30', '20:00', 17),
    (59536, 'Wenwen', 'Chinese', 'Greenpoint', 'wenwen', 'Sichuan-inspired small plates', 14, 9, 0, '17:00', '22:00', '19:00', '20:30', 18),
    (49453, 'Thai Diner', 'Thai', 'Nolita', 'thai-diner', 'Ann Redding''s Thai comfort food', 7, 9, 0, '11:00', '22:00', '19:00', '20:30', 19),
    (8266, 'Jua', 'Korean', 'Flatiron', 'jua', 'Michelin-starred Korean fine dining', 30, 10, 0, '17:00', '22:00', '19:00', '20:30', 20),
    (62659, 'Claud', 'American', 'East Village', 'claud', 'Neighborhood gem, always packed', 14, 9, 0, '17:30', '22:00', '19:00', '20:30', 21),
    (48994, 'Dhamaka', 'Indian', 'LES', 'dhamaka', 'Bold regional Indian — no holds barred', 30, 10, 0, '17:00', '23:00', '19:00', '20:30', 22),
    (76033, 'COQODAQ', 'Korean Fried Chicken', 'Flatiron', 'coqodaq', 'Dave Park''s fried chicken temple', 14, 10, 0, '17:00', '22:30', '19:00', '20:30', 23),
    (34341, 'Dame', 'Seafood', 'West Village', 'dame', 'Tiny seafood counter, huge demand', 14, 9, 0, '17:00', '22:00', '19:00', '20:30', 24),
    (42534, 'Double Chicken Please', 'Cocktail Bar', 'LES', 'double-chicken-please', 'World''s best bar, impossible tables', 6, 0, 0, '17:00', '00:00', '20:00', '22:00', 25),
    (5771, 'Rezdora', 'Italian', 'Flatiron', 'rezdora', 'Stefano Secchi''s Emilian masterclass', 30, 0, 0, '17:00', '22:30', '19:00', '20:30', 26),
    (7253, 'Atomix', 'Korean Tasting', 'Tribeca', 'atomix', 'Two-Michelin-star Korean tasting menu', 30, 10, 0, '17:00', '22:00', '19:00', '20:30', 27)
ON CONFLICT (venue_id) DO NOTHING;
