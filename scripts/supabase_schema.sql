-- Tablement Database Schema
-- Run this in Supabase Dashboard → SQL Editor → New Query → paste → Run
-- Creates all tables needed for the SaaS product

-- ============================================================
-- 1. PROFILES — extends Supabase auth.users
-- ============================================================
CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    resy_email TEXT,
    resy_password_encrypted TEXT,
    stripe_customer_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- RLS: users can only read/update their own profile
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own profile"
    ON profiles FOR SELECT
    USING (id = auth.uid());

CREATE POLICY "Users can update own profile"
    ON profiles FOR UPDATE
    USING (id = auth.uid());

CREATE POLICY "Users can insert own profile"
    ON profiles FOR INSERT
    WITH CHECK (id = auth.uid());

-- Service role can do everything (for server-side operations)
CREATE POLICY "Service role full access on profiles"
    ON profiles FOR ALL
    USING (auth.role() = 'service_role');


-- ============================================================
-- 2. RESERVATIONS — each row = one booking job
-- ============================================================
CREATE TABLE IF NOT EXISTS reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    venue_id INTEGER NOT NULL,
    venue_name TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    target_date DATE NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('snipe', 'monitor')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'scheduled', 'monitoring', 'sniping', 'confirmed', 'failed', 'cancelled')),
    time_preferences JSONB NOT NULL DEFAULT '[]'::jsonb,
    drop_time_config JSONB,
    stripe_payment_intent_id TEXT,
    resy_token TEXT,
    attempts INTEGER DEFAULT 0,
    elapsed_seconds FLOAT,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- RLS: users can only see their own reservations
ALTER TABLE reservations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own reservations"
    ON reservations FOR SELECT
    USING (user_id = auth.uid());

CREATE POLICY "Users can insert own reservations"
    ON reservations FOR INSERT
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "Users can update own reservations"
    ON reservations FOR UPDATE
    USING (user_id = auth.uid());

CREATE POLICY "Service role full access on reservations"
    ON reservations FOR ALL
    USING (auth.role() = 'service_role');


-- ============================================================
-- 3. PAYMENT METHODS — Stripe card references
-- ============================================================
CREATE TABLE IF NOT EXISTS payment_methods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    stripe_payment_method_id TEXT NOT NULL,
    brand TEXT,
    last_four TEXT,
    is_default BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- RLS: users can only see their own payment methods
ALTER TABLE payment_methods ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own payment methods"
    ON payment_methods FOR SELECT
    USING (user_id = auth.uid());

CREATE POLICY "Users can insert own payment methods"
    ON payment_methods FOR INSERT
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "Users can delete own payment methods"
    ON payment_methods FOR DELETE
    USING (user_id = auth.uid());

CREATE POLICY "Service role full access on payment_methods"
    ON payment_methods FOR ALL
    USING (auth.role() = 'service_role');


-- ============================================================
-- 4. AUTO-UPDATE updated_at TRIGGER
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER profiles_updated_at
    BEFORE UPDATE ON profiles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER reservations_updated_at
    BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ============================================================
-- 5. INDEXES for common queries
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_reservations_user_id ON reservations(user_id);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
CREATE INDEX IF NOT EXISTS idx_payment_methods_user_id ON payment_methods(user_id);
