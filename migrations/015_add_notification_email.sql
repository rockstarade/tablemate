-- Migration 015: Add notification_email column to reservations
-- Stores the user's preferred email for booking confirmation notifications.
-- Collected at booking time via the restaurant page email prompt.

ALTER TABLE reservations ADD COLUMN IF NOT EXISTS notification_email TEXT;
