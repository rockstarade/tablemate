-- Expand transactions type constraint to include admin and referral types
-- Fixes: admin_credit, referral_received, referral_sent not being valid types

ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_type_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
    CHECK (type IN ('charge', 'credit_purchase', 'credit_used', 'referral_discount', 'subscription', 'overage', 'admin_credit', 'referral_received', 'referral_sent'));
