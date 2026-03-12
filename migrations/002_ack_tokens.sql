-- Migration 002: Add ack_tokens table (v0.2 → v0.3)
--
-- Adds the one-time web-acknowledge token table used by the nag-ack-token
-- feature.  Tokens are per-send-event, expire after 24 hours, and are
-- single-use (used=1 after redemption).
--
-- Raw tokens are NEVER stored — only SHA-256 hashes.
--
-- Safe to run against an existing v0.2 database.  All statements are
-- guarded with IF NOT EXISTS so repeated runs are idempotent.
--
-- IMPORTANT: Use run_migration.py instead of running this SQL directly.

PRAGMA foreign_keys = ON;

-- ============================================================
-- Step 1: Create ack_tokens table
-- ============================================================

CREATE TABLE IF NOT EXISTS ack_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_at TEXT
);

-- ============================================================
-- Step 2: Create indexes
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_ack_tokens_hash ON ack_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_ack_tokens_reminder ON ack_tokens(reminder_id);
