"""SQLite implementation of the MeetingRepository.

Single-file database, no external dependencies.
Schema is created on first connection if tables don't exist.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models.meeting import Meeting, MeetingState
from .base import MeetingRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 90,
    link TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    state TEXT NOT NULL DEFAULT 'pending',
    ack_keyword TEXT,
    ack_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_meetings_state ON meetings(state);
CREATE INDEX IF NOT EXISTS idx_meetings_starts_at ON meetings(starts_at);
CREATE INDEX IF NOT EXISTS idx_reminder_log_meeting ON reminder_log(meeting_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash ON auth_tokens(token_hash);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_meeting(row: sqlite3.Row) -> Meeting:
    return Meeting(
        id=row["id"],
        title=row["title"],
        starts_at=_parse_dt(row["starts_at"]),
        duration_min=row["duration_min"],
        link=row["link"],
        source=row["source"],
        state=MeetingState(row["state"]),
        ack_keyword=row["ack_keyword"],
        ack_at=_parse_dt(row["ack_at"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


class SqliteMeetingRepository(MeetingRepository):
    """SQLite-backed meeting storage."""

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

    def create(self, meeting: Meeting) -> Meeting:
        now = _now_utc()
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO meetings
            (title, starts_at, duration_min, link, source, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                meeting.title,
                meeting.starts_at.isoformat() if meeting.starts_at else now,
                meeting.duration_min,
                meeting.link,
                meeting.source,
                meeting.state.value,
                now,
                now,
            ),
        )
        conn.commit()
        meeting.id = cursor.lastrowid
        meeting.created_at = _parse_dt(now)
        meeting.updated_at = _parse_dt(now)
        return meeting

    def get(self, meeting_id: int) -> Optional[Meeting]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_meeting(row)

    def list_all(
        self,
        state: Optional[MeetingState] = None,
    ) -> list[Meeting]:
        conn = self._get_conn()
        if state is not None:
            rows = conn.execute(
                "SELECT * FROM meetings WHERE state = ? ORDER BY starts_at",
                (state.value,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM meetings ORDER BY starts_at").fetchall()
        return [_row_to_meeting(r) for r in rows]

    def list_upcoming(
        self,
        before: Optional[datetime] = None,
        states: Optional[list[MeetingState]] = None,
    ) -> list[Meeting]:
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
            f"SELECT * FROM meetings {where} ORDER BY starts_at",
            params,
        ).fetchall()
        return [_row_to_meeting(r) for r in rows]

    def update_state(
        self,
        meeting_id: int,
        state: MeetingState,
        ack_keyword: Optional[str] = None,
        ack_at: Optional[datetime] = None,
    ) -> Optional[Meeting]:
        now = _now_utc()
        conn = self._get_conn()
        conn.execute(
            """UPDATE meetings
            SET state = ?, ack_keyword = ?, ack_at = ?, updated_at = ?
            WHERE id = ?""",
            (
                state.value,
                ack_keyword,
                ack_at.isoformat() if ack_at else None,
                now,
                meeting_id,
            ),
        )
        conn.commit()
        return self.get(meeting_id)

    def delete(self, meeting_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
        return cursor.rowcount > 0

    def count_by_state(self, state: MeetingState) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM meetings WHERE state = ?",
            (state.value,),
        ).fetchone()
        return row["cnt"] if row else 0

    def log_reminder(
        self,
        meeting_id: int,
        message: str,
        channel: str = "signal",
    ) -> None:
        """Log a sent reminder (not part of the ABC, SQLite-specific)."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO reminder_log (meeting_id, sent_at, message, channel)
            VALUES (?, ?, ?, ?)""",
            (meeting_id, _now_utc(), message, channel),
        )
        conn.commit()

    def get_last_reminder_time(self, meeting_id: int) -> Optional[datetime]:
        """Get the timestamp of the last reminder sent for a meeting."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT sent_at FROM reminder_log
            WHERE meeting_id = ? ORDER BY sent_at DESC LIMIT 1""",
            (meeting_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_dt(row["sent_at"])

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
