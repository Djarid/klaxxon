-- Migration 001: meetings → reminders (v0.1 → v0.2)
--
-- Transforms the v0.1 meetings schema to the v0.2 reminders schema.
-- Safe to run against a fresh v0.2 database (all operations are idempotent or guarded).
--
-- Production DB: /opt/klaxxon/data/meetings.db
-- Existing data: 1 meeting row (PWG, id=1), 1 reminder_log entry, 0 auth_tokens
--
-- IMPORTANT: Use run_migration.py instead of running this SQL directly.
-- The Python runner handles idempotency, data migration, and FK recreation.
--
-- Changes:
--   1. Rename table: meetings → reminders
--   2. Add columns: description, profile, escalate_to, schedule_id, lead_time_min, nag_interval_min
--   2b. Copy data from meetings → reminders (if both exist) and drop old table
--   3. Recreate reminder_log table (SQLite RENAME COLUMN doesn't update FK text)
--   4. Create table: schedules
--   5. Create indexes
--
-- Requires SQLite >= 3.25.0 (RENAME COLUMN support)

-- Safety: enable foreign keys
PRAGMA foreign_keys = OFF;

-- ============================================================
-- Step 1: Rename meetings → reminders
-- ============================================================
-- Guard: only rename if meetings exists and reminders doesn't
-- SQLite ALTER TABLE RENAME is atomic.
-- If already migrated (reminders exists), this will fail harmlessly
-- when wrapped in the Python runner.

ALTER TABLE meetings RENAME TO reminders;

-- ============================================================
-- Step 2: Add new columns to reminders
-- ============================================================
-- ALTER TABLE ADD COLUMN is safe to repeat if column already exists
-- (SQLite will error, but the Python runner handles this per-statement).
-- Defaults match the application schema in sqlite.py.

ALTER TABLE reminders ADD COLUMN description TEXT;
ALTER TABLE reminders ADD COLUMN profile TEXT NOT NULL DEFAULT 'meeting';
ALTER TABLE reminders ADD COLUMN escalate_to TEXT;
ALTER TABLE reminders ADD COLUMN schedule_id INTEGER;
ALTER TABLE reminders ADD COLUMN lead_time_min INTEGER;
ALTER TABLE reminders ADD COLUMN nag_interval_min INTEGER;

-- ============================================================
-- Step 3: Rename reminder_log.meeting_id → reminder_id
-- ============================================================

ALTER TABLE reminder_log RENAME COLUMN meeting_id TO reminder_id;

-- ============================================================
-- Step 4: Create schedules table
-- ============================================================

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    time_of_day TEXT NOT NULL,
    duration_min INTEGER DEFAULT 0,
    link TEXT,
    source TEXT DEFAULT 'manual',
    profile TEXT DEFAULT 'meeting',
    escalate_to TEXT,
    lead_time_min INTEGER,
    nag_interval_min INTEGER,
    recurrence TEXT NOT NULL DEFAULT 'daily',
    recurrence_rule TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ============================================================
-- Step 5: Create indexes (IF NOT EXISTS is idempotent)
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_reminders_state ON reminders(state);
CREATE INDEX IF NOT EXISTS idx_reminders_starts_at ON reminders(starts_at);
CREATE INDEX IF NOT EXISTS idx_reminders_schedule_id ON reminders(schedule_id);
CREATE INDEX IF NOT EXISTS idx_reminder_log_reminder ON reminder_log(reminder_id);
CREATE INDEX IF NOT EXISTS idx_schedules_active ON schedules(is_active);

-- ============================================================
-- Step 6: Re-enable foreign keys and verify
-- ============================================================

PRAGMA foreign_keys = ON;
