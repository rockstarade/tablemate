-- Migration 017: Add Tonino (Boston) to curated restaurants
-- Venue ID 73777 confirmed from Resy
-- 28-seat Italian restaurant in Jamaica Plain, Boston
-- Drops daily at noon, 30 days ahead
-- #1 most popular restaurant on Resy in Boston
-- Service: Sun-Mon 5-9:30pm, Thu-Sat 5-10:30pm (closed Tue-Wed)

INSERT INTO curated_restaurants (
    venue_id, name, cuisine, neighborhood, url_slug,
    drop_days_ahead, drop_hour, drop_minute,
    service_start, service_end, hot_start, hot_end,
    slot_interval, sort_order, is_active, tagline
) VALUES
    (73777, 'Tonino', 'Italian', 'Jamaica Plain, Boston', 'tonino',
     30, 12, 0, '17:00', '22:30', '19:00', '20:30', 15, 50, true,
     'Boston''s #1 most popular restaurant on Resy. 28-seat Italian gem.')
ON CONFLICT (venue_id) DO NOTHING;
