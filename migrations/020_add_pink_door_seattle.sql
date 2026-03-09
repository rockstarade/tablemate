-- ═══════════════════════════════════════════════════════════════
-- Migration 020: Add The Pink Door (Seattle) — OpenTable
-- ═══════════════════════════════════════════════════════════════

INSERT INTO curated_restaurants (
    venue_id, name, cuisine, neighborhood, url_slug,
    drop_days_ahead, drop_hour, drop_minute,
    service_start, service_end, hot_start, hot_end,
    slot_interval, sort_order, is_active, platform, city, tagline
) VALUES
    (80008, 'The Pink Door', 'Italian', 'Pike Place Market', 'the-pink-door-seattle',
     14, 9, 0, '11:30', '22:00', '18:00', '20:00', 15, 110, true,
     'opentable', 'seattle', 'Beloved Pike Place Italian with cabaret and trapeze.')
ON CONFLICT (venue_id) DO NOTHING;
