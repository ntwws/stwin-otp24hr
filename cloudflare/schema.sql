CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  password_salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS activations (
  activation_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  phone_hash TEXT NOT NULL,
  phone_mask TEXT NOT NULL,
  phone_number TEXT NOT NULL DEFAULT '',
  price REAL NOT NULL DEFAULT 0,
  purchased_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  otp_received_at TEXT,
  otp_count INTEGER NOT NULL DEFAULT 0,
  otp_code TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_activations_phone ON activations(phone_hash, purchased_at);
CREATE INDEX IF NOT EXISTS idx_activations_user_success ON activations(user_id, otp_received_at);
CREATE INDEX IF NOT EXISTS idx_activations_history ON activations(purchased_at DESC, activation_id);

CREATE TABLE IF NOT EXISTS number_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone_hash TEXT NOT NULL,
  phone_mask TEXT NOT NULL,
  reporter_user_id INTEGER NOT NULL REFERENCES users(id),
  blocked_days INTEGER NOT NULL,
  reported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  blocked_until TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_reports_phone ON number_reports(phone_hash, blocked_until, active);
