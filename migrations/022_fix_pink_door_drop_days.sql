-- ═══════════════════════════════════════════════════════════════
-- Migration 022: Fix The Pink Door booking window (90 days, not 14)
-- ═══════════════════════════════════════════════════════════════

UPDATE curated_restaurants
SET drop_days_ahead = 90
WHERE venue_id = 80008;
