"""SQLite implementation of the ReminderRepository and AckTokenRepository.

Single-file database, no external dependencies.
Schema is created on first connection if tables don't exist.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models.ack_token import AckToken
from ..models.reminder import Reminder, ReminderState
from .base import AckTokenRepository, ReminderRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    starts_at TEXT NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 90,
    link TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    profile TEXT NOT NULL DEFAULT 'meeting',
    escalate_to TEXT,
    schedule_id INTEGER,
    lead_time_min INTEGER,
    nag_interval_min INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    ack_keyword TEXT,
    ack_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    sent_at TEXT NOT NULL,
    message TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'signal'
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ack_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_state ON reminders(state);
CREATE INDEX IF NOT EXISTS idx_reminders_starts_at ON reminders(starts_at);
CREATE INDEX IF NOT EXISTS idx_reminders_schedule_id ON reminders(schedule_id);
CREATE INDEX IF NOT EXISTS idx_reminder_log_reminder ON reminder_log(reminder_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash ON auth_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_ack_tokens_hash ON ack_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_ack_tokens_reminder ON ack_tokens(reminder_id);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_ack_token(row: sqlite3.Row) -> AckToken:
    return AckToken(
        id=row["id"],
        token_hash=row["token_hash"],
        reminder_id=row["reminder_id"],
        created_at=_parse_dt(row["created_at"]),
        expires_at=_parse_dt(row["expires_at"]),
        used=bool(row["used"]),
        used_at=_parse_dt(row["used_at"]),
    )


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        starts_at=_parse_dt(row["starts_at"]),
        duration_min=row["duration_min"],
        link=row["link"],
        source=row["source"],
        profile=row["profile"],
        escalate_to=row["escalate_to"],
        schedule_id=row["schedule_id"],
        lead_time_min=row["lead_time_min"],
        nag_interval_min=row["nag_interval_min"],
        state=ReminderState(row["state"]),
        ack_keyword=row["ack_keyword"],
        ack_at=_parse_dt(row["ack_at"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


class SqliteReminderRepository(ReminderRepository, AckTokenRepository):
    """SQLite-backed reminder storage."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def create(self, reminder: Reminder) -> Reminder:
        now = _now_utc()
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO reminders
            (title, description, starts_at, duration_min, link, source, profile, escalate_to, schedule_id, lead_time_min, nag_interval_min, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reminder.title,
                reminder.description,
                reminder.starts_at.isoformat() if reminder.starts_at else now,
                reminder.duration_min,
                reminder.link,
                reminder.source,
                reminder.profile,
                reminder.escalate_to,
                reminder.schedule_id,
                reminder.lead_time_min,
                reminder.nag_interval_min,
                reminder.state.value,
                now,
                now,
            ),
        )
        conn.commit()
        reminder.id = cursor.lastrowid
        reminder.created_at = _parse_dt(now)
        reminder.updated_at = _parse_dt(now)
        return reminder

    def get(self, reminder_id: int) -> Optional[Reminder]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_reminder(row)

    def list_all(
        self,
        state: Optional[ReminderState] = None,
    ) -> list[Reminder]:
        conn = self._get_conn()
        if state is not None:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE state = ? ORDER BY starts_at",
                (state.value,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reminders ORDER BY starts_at").fetchall()
        return [_row_to_reminder(r) for r in rows]

    def list_upcoming(
        self,
        before: Optional[datetime] = None,
        states: Optional[list[ReminderState]] = None,
    ) -> list[Reminder]:
        conn = self._get_conn()
        now = _now_utc()
        conditions = []
        params: list = []

        if before is not None:
            conditions.append("starts_at <= ?")
            params.append(before.isoformat())

        if states:
            placeholders = ",".join("?" for _ in states)
            conditions.append(f"state IN ({placeholders})")
            params.extend(s.value for s in states)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        rows = conn.execute(
            f"SELECT * FROM reminders {where} ORDER BY starts_at",
            params,
        ).fetchall()
        return [_row_to_reminder(r) for r in rows]

    def update_state(
        self,
        reminder_id: int,
        state: ReminderState,
        ack_keyword: Optional[str] = None,
        ack_at: Optional[datetime] = None,
    ) -> Optional[Reminder]:
        now = _now_utc()
        conn = self._get_conn()
        conn.execute(
            """UPDATE reminders
            SET state = ?, ack_keyword = ?, ack_at = ?, updated_at = ?
            WHERE id = ?""",
            (
                state.value,
                ack_keyword,
                ack_at.isoformat() if ack_at else None,
                now,
                reminder_id,
            ),
        )
        conn.commit()
        return self.get(reminder_id)

    def delete(self, reminder_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
        return cursor.rowcount > 0

    def count_by_state(self, state: ReminderState) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reminders WHERE state = ?",
            (state.value,),
        ).fetchone()
        return row["cnt"] if row else 0

    def log_reminder(
        self,
        reminder_id: int,
        message: str,
        channel: str = "signal",
    ) -> None:
        """Log a sent reminder (not part of the ABC, SQLite-specific)."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO reminder_log (reminder_id, sent_at, message, channel)
            VALUES (?, ?, ?, ?)""",
            (reminder_id, _now_utc(), message, channel),
        )
        conn.commit()

    def get_last_reminder_time(self, reminder_id: int) -> Optional[datetime]:
        """Get the timestamp of the last reminder sent for a reminder."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT sent_at FROM reminder_log
            WHERE reminder_id = ? ORDER BY sent_at DESC LIMIT 1""",
            (reminder_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_dt(row["sent_at"])

    def get_last_resend_time(self, reminder_id: int) -> Optional[datetime]:
        """Get the timestamp of the most recent 'resend' log entry for a reminder.

        Used by the cooldown check in ReminderEngine.resend_notification().
        Only queries channel='resend' entries — engine-initiated 'signal' entries
        do NOT trigger the cooldown (REQ-9).
        """
        conn = self._get_conn()
        row = conn.execute(
            """SELECT sent_at FROM reminder_log
            WHERE reminder_id = ? AND channel = 'resend'
            ORDER BY sent_at DESC LIMIT 1""",
            (reminder_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_dt(row["sent_at"])

    def update_fields(self, reminder_id: int, fields: dict) -> Optional[Reminder]:
        """Update specific fields on a reminder. Returns updated reminder or None if not found."""
        if not fields:
            return self.get(reminder_id)

        # Whitelist of allowed column names (prevent SQL injection)
        allowed_fields = {
            "title",
            "description",
            "starts_at",
            "duration_min",
            "link",
            "profile",
            "escalate_to",
            "lead_time_min",
            "nag_interval_min",
        }

        # Filter to only allowed fields
        update_fields = {k: v for k, v in fields.items() if k in allowed_fields}
        if not update_fields:
            return self.get(reminder_id)

        # Build dynamic UPDATE query
        set_clauses = []
        params = []
        for field_name, value in update_fields.items():
            set_clauses.append(f"{field_name} = ?")
            # Convert datetime to ISO string if needed
            if isinstance(value, datetime):
                params.append(value.isoformat())
            else:
                params.append(value)

        # Always update updated_at
        set_clauses.append("updated_at = ?")
        params.append(_now_utc())

        # Add reminder_id to params
        params.append(reminder_id)

        query = f"UPDATE reminders SET {', '.join(set_clauses)} WHERE id = ?"

        conn = self._get_conn()
        conn.execute(query, params)
        conn.commit()

        return self.get(reminder_id)

    def find_by_schedule_and_time(
        self, schedule_id: int, starts_at: datetime
    ) -> Optional[Reminder]:
        """Find a reminder by schedule_id and starts_at (for deduplication during spawning).

        Uses 1-minute tolerance for time matching.
        """
        conn = self._get_conn()
        # Get all reminders for this schedule
        rows = conn.execute(
            "SELECT * FROM reminders WHERE schedule_id = ?", (schedule_id,)
        ).fetchall()

        # Check each for time match within 1 minute
        for row in rows:
            reminder = _row_to_reminder(row)
            if reminder.starts_at:
                diff = abs((reminder.starts_at - starts_at).total_seconds())
                if diff < 60:
                    return reminder

        return None

    # ------------------------------------------------------------------
    # AckTokenRepository implementation
    # ------------------------------------------------------------------

    def store_token(
        self,
        token_hash: str,
        reminder_id: int,
        expires_at: datetime,
    ) -> None:
        """Persist a new ack token (hash only — raw token is never stored)."""
        now = _now_utc()
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO ack_tokens (token_hash, reminder_id, created_at, expires_at, used)
            VALUES (?, ?, ?, ?, 0)""",
            (token_hash, reminder_id, now, expires_at.isoformat()),
        )
        conn.commit()

    def get_by_hash(self, token_hash: str) -> Optional[AckToken]:
        """Look up an ack token by its SHA-256 hash."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM ack_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_ack_token(row)

    def mark_used(self, token_hash: str) -> bool:
        """Atomically mark a token as used via single UPDATE … WHERE used = 0.

        Returns True if the row was updated (token existed and was unused).
        Returns False if not found or already used — preventing concurrent double-use.
        """
        now = _now_utc()
        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE ack_tokens
            SET used = 1, used_at = ?
            WHERE token_hash = ? AND used = 0""",
            (now, token_hash),
        )
        conn.commit()
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Housekeeping: delete terminal reminders + orphan tokens
    # ------------------------------------------------------------------

    def delete_terminal_reminders(
        self,
        cutoff: datetime,
        states: list,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Delete (or count) terminal-state reminders older than cutoff.

        For ACKNOWLEDGED: uses ack_at (COALESCE to updated_at if NULL).
        For SKIPPED/MISSED: uses updated_at.

        Returns dict mapping state name -> count of affected rows.
        """
        from ..models.reminder import ReminderState

        cutoff_iso = cutoff.isoformat()
        conn = self._get_conn()
        result: dict[str, int] = {}

        for state in states:
            state_val = state.value if hasattr(state, "value") else state

            if state_val == ReminderState.ACKNOWLEDGED.value:
                # ACKNOWLEDGED: use ack_at, fall back to updated_at if NULL
                if dry_run:
                    row = conn.execute(
                        """SELECT COUNT(*) AS cnt FROM reminders
                           WHERE state = ?
                           AND COALESCE(ack_at, updated_at) < ?""",
                        (state_val, cutoff_iso),
                    ).fetchone()
                    result[state_val] = row["cnt"] if row else 0
                else:
                    cursor = conn.execute(
                        """DELETE FROM reminders
                           WHERE state = ?
                           AND COALESCE(ack_at, updated_at) < ?""",
                        (state_val, cutoff_iso),
                    )
                    result[state_val] = cursor.rowcount
            else:
                # SKIPPED / MISSED: use updated_at
                if dry_run:
                    row = conn.execute(
                        """SELECT COUNT(*) AS cnt FROM reminders
                           WHERE state = ?
                           AND updated_at < ?""",
                        (state_val, cutoff_iso),
                    ).fetchone()
                    result[state_val] = row["cnt"] if row else 0
                else:
                    cursor = conn.execute(
                        """DELETE FROM reminders
                           WHERE state = ?
                           AND updated_at < ?""",
                        (state_val, cutoff_iso),
                    )
                    result[state_val] = cursor.rowcount

        if not dry_run:
            conn.commit()

        return result

    def delete_orphan_tokens(self, dry_run: bool = False) -> int:
        """Delete (or count) ack_tokens that are orphaned or used+expired.

        Orphan: reminder_id NOT IN (SELECT id FROM reminders).
        Used+expired: used = 1 AND expires_at < now.

        Returns count of affected rows.
        """
        now_iso = _now_utc()
        conn = self._get_conn()

        if dry_run:
            row_orphan = conn.execute(
                """SELECT COUNT(*) AS cnt FROM ack_tokens
                   WHERE reminder_id NOT IN (SELECT id FROM reminders)""",
            ).fetchone()
            orphan_count = row_orphan["cnt"] if row_orphan else 0

            row_used_exp = conn.execute(
                """SELECT COUNT(*) AS cnt FROM ack_tokens
                   WHERE used = 1 AND expires_at < ?
                   AND reminder_id IN (SELECT id FROM reminders)""",
                (now_iso,),
            ).fetchone()
            used_exp_count = row_used_exp["cnt"] if row_used_exp else 0

            return orphan_count + used_exp_count
        else:
            cursor_orphan = conn.execute(
                """DELETE FROM ack_tokens
                   WHERE reminder_id NOT IN (SELECT id FROM reminders)""",
            )
            orphan_count = cursor_orphan.rowcount

            cursor_used_exp = conn.execute(
                """DELETE FROM ack_tokens
                   WHERE used = 1 AND expires_at < ?""",
                (now_iso,),
            )
            used_exp_count = cursor_used_exp.rowcount

            conn.commit()
            return orphan_count + used_exp_count

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
