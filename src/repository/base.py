"""Abstract repository interfaces for reminder and schedule storage.

Dependency Inversion: high-level modules (ReminderService, ReminderEngine, ScheduleService)
depend on these ABCs, not on the concrete SQLite implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..models.ack_token import AckToken
from ..models.reminder import Reminder, ReminderState
from ..models.schedule import Schedule


class ReminderRepository(ABC):
    """Abstract interface for reminder persistence."""

    @abstractmethod
    def create(self, reminder: Reminder) -> Reminder:
        """Persist a new reminder. Returns the reminder with id populated."""
        ...

    @abstractmethod
    def get(self, reminder_id: int) -> Optional[Reminder]:
        """Retrieve a reminder by id. Returns None if not found."""
        ...

    @abstractmethod
    def list_all(
        self,
        state: Optional[ReminderState] = None,
    ) -> list[Reminder]:
        """List reminders, optionally filtered by state."""
        ...

    @abstractmethod
    def list_upcoming(
        self,
        before: Optional[datetime] = None,
        states: Optional[list[ReminderState]] = None,
    ) -> list[Reminder]:
        """List upcoming reminders (starts_at in the future or recently started)."""
        ...

    @abstractmethod
    def update_state(
        self,
        reminder_id: int,
        state: ReminderState,
        ack_keyword: Optional[str] = None,
        ack_at: Optional[datetime] = None,
    ) -> Optional[Reminder]:
        """Update a reminder's state. Returns updated reminder or None."""
        ...

    @abstractmethod
    def delete(self, reminder_id: int) -> bool:
        """Delete a reminder. Returns True if deleted."""
        ...

    @abstractmethod
    def count_by_state(self, state: ReminderState) -> int:
        """Count reminders in a given state."""
        ...

    @abstractmethod
    def update_fields(self, reminder_id: int, fields: dict) -> Optional[Reminder]:
        """Update specific fields on a reminder. Returns updated reminder or None if not found."""
        ...

    @abstractmethod
    def has_active_for_schedule(self, schedule_id: int) -> bool:
        """Return True if a PENDING or REMINDING reminder exists for this schedule.

        Used by spawn_reminders() to skip spawning when an active instance
        already exists (at-most-one gate).
        """
        ...

    @abstractmethod
    def delete_terminal_reminders(
        self,
        cutoff: datetime,
        states: list[ReminderState],
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Delete (or count, if dry_run) reminders in given terminal states
        whose terminal timestamp is before cutoff.

        For ACKNOWLEDGED state, uses ack_at (with fallback to updated_at if NULL).
        For SKIPPED/MISSED states, uses updated_at.

        Returns dict mapping state name -> count of affected rows.
        e.g. {"acknowledged": 5, "skipped": 2, "missed": 1}
        """
        ...


class ScheduleRepository(ABC):
    """Abstract interface for schedule persistence."""

    @abstractmethod
    def create(self, schedule: Schedule) -> Schedule:
        """Persist a new schedule. Returns the schedule with id populated."""
        ...

    @abstractmethod
    def get(self, schedule_id: int) -> Optional[Schedule]:
        """Retrieve a schedule by id. Returns None if not found."""
        ...

    @abstractmethod
    def list(self, active_only: bool = True) -> list[Schedule]:
        """List schedules, optionally filtered by active status."""
        ...

    @abstractmethod
    def update_fields(self, schedule_id: int, fields: dict) -> Optional[Schedule]:
        """Update specific fields on a schedule. Returns updated schedule or None if not found."""
        ...

    @abstractmethod
    def deactivate(self, schedule_id: int) -> bool:
        """Deactivate a schedule (soft delete). Returns True if successful."""
        ...


class AckTokenRepository(ABC):
    """Abstract interface for one-time ack token persistence.

    Raw tokens are NEVER stored — only SHA-256 hashes.
    """

    @abstractmethod
    def store_token(
        self,
        token_hash: str,
        reminder_id: int,
        expires_at: datetime,
    ) -> None:
        """Persist a new ack token.

        Stores only the hash (not the raw token), the associated reminder_id,
        and expiry timestamp.  created_at is set to now by the implementation.
        """
        ...

    @abstractmethod
    def get_by_hash(self, token_hash: str) -> Optional[AckToken]:
        """Look up an ack token by its SHA-256 hash.  Returns None if not found."""
        ...

    @abstractmethod
    def mark_used(self, token_hash: str) -> bool:
        """Atomically mark a token as used.

        Uses a single UPDATE … WHERE used = 0 so that concurrent requests
        cannot both succeed.

        Returns True if the token was successfully marked used (i.e. it existed
        and was not already used).  Returns False otherwise (already used or not
        found), which the caller should treat as a replay-prevention failure.
        """
        ...

    @abstractmethod
    def delete_orphan_tokens(self, dry_run: bool = False) -> int:
        """Delete (or count, if dry_run) ack_tokens that reference
        non-existent reminders or are expired AND used.

        Returns count of affected rows.
        """
        ...
