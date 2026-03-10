-- 025: Add subscription columns to profiles table
-- Supports: plan tiers, Stripe subscriptions, monthly booking tracking

-- plan: 'free' | 'single' | 'pro' | 'vip' | 'beta'
DO $$ BEGIN
    ALTER TABLE profiles ADD COLUMN plan TEXT DEFAULT 'free';
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Stripe subscription ID (for Pro/VIP recurring subscriptions)
DO $$ BEGIN
    ALTER TABLE profiles ADD COLUMN stripe_subscription_id TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Bookings used this billing period (resets monthly via webhook)
DO $$ BEGIN
    ALTER TABLE profiles ADD COLUMN plan_bookings_used INTEGER DEFAULT 0;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Billing cycle window (set from Stripe subscription period)
DO $$ BEGIN
    ALTER TABLE profiles ADD COLUMN plan_period_start TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE profiles ADD COLUMN plan_period_end TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Expand transactions type constraint to include new types
DO $$ BEGIN
    ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_type_check;
    ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
        CHECK (type IN ('charge', 'credit_purchase', 'credit_used', 'referral_discount', 'subscription', 'overage'));
EXCEPTION WHEN others THEN NULL;
END $$;
