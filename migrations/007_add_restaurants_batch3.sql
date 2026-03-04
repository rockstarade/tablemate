-- Migration 007: Add Salt Hank's, Casa Mono, Ambassador's Clubhouse, Chez Fifi
-- These are placeholder venue_ids (99901-99904) — update with real Resy IDs when available.

INSERT INTO curated_restaurants (
    venue_id, name, cuisine, neighborhood, url_slug,
    drop_days_ahead, drop_hour, drop_minute,
    service_start, service_end, hot_start, hot_end,
    slot_interval, sort_order, is_active
) VALUES
    (99901, 'Salt Hank''s', 'American', 'Various', 'salt-hanks',
     14, 10, 0, '17:00', '22:00', '19:00', '20:30', 30, 40, true),
    (99902, 'Casa Mono', 'Spanish', 'Gramercy', 'casa-mono',
     30, 10, 0, '17:00', '23:00', '19:00', '20:30', 15, 41, true),
    (99903, 'Ambassador''s Clubhouse', 'American', 'Manhattan', 'ambassadors-clubhouse-new-york',
     14, 9, 0, '17:00', '22:00', '19:00', '20:30', 30, 42, true),
    (99904, 'Chez Fifi', 'French', 'Manhattan', 'chez-fifi',
     30, 10, 0, '17:30', '23:00', '19:00', '20:30', 15, 43, true)
ON CONFLICT (venue_id) DO NOTHING;
