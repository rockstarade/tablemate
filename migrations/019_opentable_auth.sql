-- Migration 019: Add OpenTable credential columns to profiles
-- Stores encrypted bearer token + identity data for OT booking

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS opentable_linked BOOLEAN DEFAULT false;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS opentable_bearer_token_encrypted TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS opentable_diner_id TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS opentable_gpid TEXT;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS opentable_phone TEXT;
