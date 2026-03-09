-- ═══════════════════════════════════════════════════════════════
-- Migration 023: Add referral_discount flag to profiles
--
-- Changes referral system from "1 free credit" to "50% off
-- first successful reservation". Boolean flag, one-time use.
-- ═══════════════════════════════════════════════════════════════

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS referral_discount boolean DEFAULT false;
