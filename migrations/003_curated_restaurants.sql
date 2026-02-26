-- Migration 003: Curated restaurants for the browse grid
-- Run in Supabase SQL Editor after 001 and 002

-- Table for the featured restaurant grid
CREATE TABLE IF NOT EXISTS curated_restaurants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    venue_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    cuisine TEXT DEFAULT '',
    neighborhood TEXT DEFAULT '',
    url_slug TEXT DEFAULT '',
    image_url TEXT DEFAULT '',
    tagline TEXT DEFAULT '',
    drop_days_ahead INTEGER,
    drop_hour INTEGER,
    drop_minute INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- RLS: anyone can read active restaurants, service role can write
ALTER TABLE curated_restaurants ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can view active curated restaurants"
    ON curated_restaurants FOR SELECT
    USING (is_active = true);

CREATE POLICY "Service role full access on curated_restaurants"
    ON curated_restaurants FOR ALL
    USING (auth.role() = 'service_role');

CREATE INDEX IF NOT EXISTS idx_curated_restaurants_sort ON curated_restaurants(sort_order);

-- Add resy_auth_token column to profiles for phone+OTP Resy linking
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS resy_auth_token TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS resy_phone TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS resy_token_expires_at TIMESTAMPTZ;

-- Seed initial curated restaurants
INSERT INTO curated_restaurants (venue_id, name, cuisine, neighborhood, url_slug, tagline, drop_days_ahead, drop_hour, sort_order)
VALUES
    (329,   'Carbone',              'Italian',          'Greenwich Village', 'carbone',              'The toughest table in NYC',                30, 10, 1),
    (834,   '4 Charles Prime Rib',  'Steakhouse',       'West Village',      '4-charles-prime-rib',  'Speakeasy steakhouse, 2 weeks out',        14,  9, 2),
    (62714, 'Torrisi',              'Italian',          'Nolita',            'torrisi',              'Red-sauce revival, books fast',             30, 10, 3),
    (1505,  'Don Angie',            'Italian',          'West Village',      'don-angie',            'Cult-favorite pinwheel lasagna',            30, 10, 4),
    (1263,  'Semma',                'South Indian',     'Greenwich Village', 'semma',                'Releases 15 days ahead at 9am',            15,  9, 5),
    (25973, 'Via Carota',           'Italian',          'West Village',      'via-carota',           'Walk-in vibes, impossible reservations',    30, 10, 6),
    (443,   'I Sodi',               'Italian',          'West Village',      'i-sodi',               'Tiny gem, always booked solid',             30, 10, 7),
    (65452, 'Tatiana',              'Afro-Caribbean',   'Lincoln Center',    'tatiana',              'Kwame Onwuachi''s masterpiece',             30, 10, 8),
    (8303,  'Le Coucou',            'French',           'SoHo',             'le-coucou',            'Haute French cuisine, always packed',       30, 10, 9),
    (7241,  'Raoul''s',             'French Bistro',    'SoHo',             'raouls',               'SoHo institution since 1975',               30, 10, 10),
    (418,   'Lilia',                'Italian',          'Williamsburg',      'lilia',                'Missy Robbins'' pasta empire',              30, 10, 11),
    (58848, 'Laser Wolf',           'Israeli',          'Williamsburg',      'laser-wolf-brooklyn',  'Michael Solomonov''s grill house',          30, 10, 12),
    (50227, 'Balthazar',            'French Bistro',    'SoHo',             'balthazar',            'The quintessential NYC brasserie',          30, 10, 13)
ON CONFLICT (venue_id) DO NOTHING;
