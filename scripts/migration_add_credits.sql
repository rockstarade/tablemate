-- Migration: Add credits + transactions
-- Run this in Supabase Dashboard → SQL Editor → New Query → paste → Run

-- 1. Add credits column to profiles
DO $$ BEGIN
    ALTER TABLE profiles ADD COLUMN credits INTEGER DEFAULT 0;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- 2. Create transactions table
CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reservation_id UUID REFERENCES reservations(id) ON DELETE SET NULL,
    type TEXT NOT NULL CHECK (type IN ('charge', 'credit_purchase', 'credit_used')),
    amount_cents INTEGER NOT NULL,
    credits_delta INTEGER DEFAULT 0,
    stripe_payment_intent_id TEXT,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 3. RLS policies
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own transactions"
    ON transactions FOR SELECT
    USING (user_id = auth.uid());

CREATE POLICY "Service role full access on transactions"
    ON transactions FOR ALL
    USING (auth.role() = 'service_role');

-- 4. Index
CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
