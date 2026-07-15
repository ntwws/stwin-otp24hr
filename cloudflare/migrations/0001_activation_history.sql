ALTER TABLE activations ADD COLUMN phone_number TEXT NOT NULL DEFAULT '';
ALTER TABLE activations ADD COLUMN otp_code TEXT NOT NULL DEFAULT '';
ALTER TABLE activations ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE activations ADD COLUMN updated_at TEXT;

UPDATE activations
SET status = CASE WHEN otp_received_at IS NOT NULL THEN 'success' ELSE 'active' END,
    updated_at = COALESCE(otp_received_at, purchased_at);

CREATE INDEX IF NOT EXISTS idx_activations_history
ON activations(purchased_at DESC, activation_id);
