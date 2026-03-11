-- Add first_name column to profiles table
-- Used for personalized greeting after initial OTP login

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS first_name TEXT;
