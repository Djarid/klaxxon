#!/usr/bin/env python3
"""Run migration 001: meetings → reminders (v0.1 → v0.2).

Applies each SQL statement individually so the migration is idempotent.
If a step has already been applied (e.g. table already renamed), it skips
that step and continues.

Usage:
    python3 run_migration.py /path/to/meetings.db
    python3 run_migration.py /path/to/meetings.db --dry-run
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def get_tables(conn: sqlite3.Connection) -> set[str]:
    """Return set of table names in the database."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return set of column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def migrate(db_path: str, dry_run: bool = False) -> None:
    path = Path(db_path)
    if not path.exists():
        print(f"ERROR: Database not found: {db_path}")
        sys.exit(1)

    # Back up before migrating
    if not dry_run:
        backup_path = path.with_suffix(".db.v1-backup")
        if not backup_path.exists():
            import shutil

            shutil.copy2(path, backup_path)
            print(f"  Backup created: {backup_path}")
        else:
            print(f"  Backup already exists: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")

    tables = get_tables(conn)
    prefix = "[DRY RUN] " if dry_run else ""

    # Step 1: Rename meetings → reminders
    if "meetings" in tables and "reminders" not in tables:
        print(f"{prefix}Step 1: Renaming meetings → reminders")
        if not dry_run:
            conn.execute("ALTER TABLE meetings RENAME TO reminders")
            conn.commit()
    elif "reminders" in tables:
        print("Step 1: SKIP (reminders table already exists)")
    else:
        print("ERROR: Neither meetings nor reminders table found!")
        sys.exit(1)

    # Refresh tables after rename
    tables = get_tables(conn)
    cols = get_columns(conn, "reminders")

    # Step 2: Add new columns to reminders
    new_columns = [
        ("description", "TEXT"),
        ("profile", "TEXT NOT NULL DEFAULT 'meeting'"),
        ("escalate_to", "TEXT"),
        ("schedule_id", "INTEGER"),
        ("lead_time_min", "INTEGER"),
        ("nag_interval_min", "INTEGER"),
    ]

    for col_name, col_def in new_columns:
        if col_name not in cols:
            print(f"{prefix}Step 2: Adding column reminders.{col_name}")
            if not dry_run:
                conn.execute(f"ALTER TABLE reminders ADD COLUMN {col_name} {col_def}")
                conn.commit()
        else:
            print(f"Step 2: SKIP (reminders.{col_name} already exists)")

    # Step 2b: If both meetings and reminders exist, copy data and drop old table
    tables = get_tables(conn)
    if "meetings" in tables and "reminders" in tables:
        meetings_count = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        reminders_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        if meetings_count > 0 and reminders_count == 0:
            print(
                f"{prefix}Step 2b: Copying {meetings_count} rows from meetings → reminders"
            )
            if not dry_run:
                conn.execute("""
                    INSERT INTO reminders (id, title, starts_at, duration_min, link, source,
                        state, ack_keyword, ack_at, created_at, updated_at, profile)
                    SELECT id, title, starts_at, duration_min, link, source,
                        state, ack_keyword, ack_at, created_at, updated_at, 'meeting'
                    FROM meetings
                """)
                conn.commit()
        print(f"{prefix}Step 2b: Dropping old meetings table")
        if not dry_run:
            conn.execute("DROP TABLE meetings")
            conn.execute(
                "UPDATE sqlite_sequence SET name = 'reminders' WHERE name = 'meetings'"
            )
            conn.commit()
    elif "meetings" not in tables:
        print("Step 2b: SKIP (meetings table already removed)")

    # Step 3: Recreate reminder_log with correct FK reference
    # SQLite RENAME COLUMN doesn't update FK constraint text, so we must
    # recreate the table to point at reminders(id) instead of meetings(id).
    if "reminder_log" in get_tables(conn):
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='reminder_log'"
        ).fetchone()
        if schema and "meetings" in schema[0]:
            print(f"{prefix}Step 3: Recreating reminder_log (fixing FK reference)")
            if not dry_run:
                rows = conn.execute(
                    "SELECT id, reminder_id, sent_at, message, channel FROM reminder_log"
                ).fetchall()
                # Handle old column name if RENAME COLUMN hasn't happened yet
                if not rows:
                    try:
                        rows = conn.execute(
                            "SELECT id, meeting_id, sent_at, message, channel FROM reminder_log"
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                conn.execute("DROP TABLE reminder_log")
                conn.execute("""
                    CREATE TABLE reminder_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
                        sent_at TEXT NOT NULL,
                        message TEXT NOT NULL,
                        channel TEXT NOT NULL DEFAULT 'signal'
                    )
                """)
                for row in rows:
                    conn.execute(
                        "INSERT INTO reminder_log (id, reminder_id, sent_at, message, channel) "
                        "VALUES (?, ?, ?, ?, ?)",
                        row,
                    )
                conn.commit()
        elif schema and "reminders" in schema[0]:
            print("Step 3: SKIP (reminder_log FK already references reminders)")
        else:
            print("Step 3: SKIP (reminder_log schema check inconclusive)")
    else:
        print("Step 3: SKIP (reminder_log table not found)")

    # Step 4: Create schedules table
    if "schedules" not in tables:
        print(f"{prefix}Step 4: Creating schedules table")
        if not dry_run:
            conn.execute("""
                CREATE TABLE schedules (
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
                )
            """)
            conn.commit()
    else:
        print("Step 4: SKIP (schedules table already exists)")

    # Step 5: Create indexes (IF NOT EXISTS is inherently idempotent)
    indexes = [
        ("idx_reminders_state", "reminders(state)"),
        ("idx_reminders_starts_at", "reminders(starts_at)"),
        ("idx_reminders_schedule_id", "reminders(schedule_id)"),
        ("idx_reminder_log_reminder", "reminder_log(reminder_id)"),
        ("idx_schedules_active", "schedules(is_active)"),
    ]

    for idx_name, idx_target in indexes:
        print(f"{prefix}Step 5: Creating index {idx_name}")
        if not dry_run:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_target}")
    if not dry_run:
        conn.commit()

    # Step 6: Re-enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON")

    # Verify
    print("\n--- Verification ---")
    tables = get_tables(conn)
    print(f"Tables: {sorted(tables)}")

    for table in ["reminders", "reminder_log", "schedules", "auth_tokens"]:
        if table in tables:
            cols = get_columns(conn, table)
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {len(cols)} columns, {count} rows")
            print(f"    Columns: {sorted(cols)}")

    conn.close()
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migration complete.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 run_migration.py <db_path> [--dry-run]")
        sys.exit(1)

    db_path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    migrate(db_path, dry_run=dry_run)
