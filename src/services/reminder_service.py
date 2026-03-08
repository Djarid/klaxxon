"""Reminder service: the single source of truth for business logic.

DRY: API routes and Signal handler both delegate here.
No business logic exists outside this module.

Single Responsibility: orchestrates repository + state machine.
Does not send notifications or parse input formats.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..models.reminder import Reminder, ReminderState
from ..repository.base import ReminderRepository
from .state_machine import InvalidTransitionError, ReminderStateMachine

logger = logging.getLogger(__name__)


class DuplicateReminderError(Exception):
    """Raised when a duplicate reminder is detected."""

    pass


class ReminderNotFoundError(Exception):
    """Raised when a reminder is not found."""

    pass


class PastReminderError(Exception):
    """Raised when trying to create a reminder in the past."""

    pass


class ReminderService:
    """Business logic for reminder operations.

    Both the API and Signal handler call these methods.
    This is the DRY layer: change business rules here once.
    """

    def __init__(self, repository: ReminderRepository) -> None:
        self._repo = repository
        self._sm = ReminderStateMachine()

    def create(
        self,
        title: str,
        starts_at: datetime,
        duration_min: int = 90,
        link: Optional[str] = None,
        source: str = "manual",
        description: Optional[str] = None,
    ) -> Reminder:
        """Create a new reminder.

        Validates:
        - starts_at is in the future
        - No duplicate (same title within 30 min of the same time)
        """
        now = datetime.now(timezone.utc)
        if starts_at.tzinfo is None:
            raise ValueError("starts_at must be timezone-aware")
        if starts_at <= now:
            raise PastReminderError(
                f"Cannot create reminder in the past: {starts_at.isoformat()}"
            )

        # Duplicate check: same title within 30 min window
        existing = self._repo.list_all()
        for r in existing:
            if r.title == title and r.starts_at is not None:
                diff = abs((r.starts_at - starts_at).total_seconds())
                if diff < 1800:  # 30 minutes
                    raise DuplicateReminderError(
                        f"Reminder '{title}' already exists at {r.starts_at.isoformat()}"
                    )

        reminder = Reminder(
            title=title,
            description=description,
            starts_at=starts_at,
            duration_min=duration_min,
            link=link,
            source=source,
            state=ReminderState.PENDING,
        )
        return self._repo.create(reminder)

    def get(self, reminder_id: int) -> Reminder:
        """Get a reminder by id."""
        reminder = self._repo.get(reminder_id)
        if reminder is None:
            raise ReminderNotFoundError(f"Reminder {reminder_id} not found")
        return reminder

    def list_reminders(
        self,
        state: Optional[ReminderState] = None,
    ) -> list[Reminder]:
        """List reminders, optionally filtered by state."""
        return self._repo.list_all(state=state)

    def acknowledge(self, reminder_id: int, keyword: str = "ack") -> Reminder:
        """Acknowledge a reminder. Stops reminders.

        Same function called by API and Signal handler (DRY).
        """
        reminder = self.get(reminder_id)
        new_state = self._sm.transition(reminder, "ack")
        now = datetime.now(timezone.utc)
        updated = self._repo.update_state(
            reminder_id,
            state=new_state,
            ack_keyword=keyword,
            ack_at=now,
        )
        if updated is None:
            raise ReminderNotFoundError(f"Reminder {reminder_id} not found")
        logger.info("Reminder %d acknowledged with '%s'", reminder_id, keyword)
        return updated

    def skip(self, reminder_id: int) -> Reminder:
        """Skip a reminder deliberately. Stops reminders.

        Same function called by API and Signal handler (DRY).
        """
        reminder = self.get(reminder_id)
        new_state = self._sm.transition(reminder, "skip")
        updated = self._repo.update_state(reminder_id, state=new_state)
        if updated is None:
            raise ReminderNotFoundError(f"Reminder {reminder_id} not found")
        logger.info("Reminder %d skipped", reminder_id)
        return updated

    def mark_reminding(self, reminder_id: int) -> Reminder:
        """Transition reminder to reminding state on first reminder."""
        reminder = self.get(reminder_id)
        new_state = self._sm.transition(reminder, "reminder_sent")
        updated = self._repo.update_state(reminder_id, state=new_state)
        if updated is None:
            raise ReminderNotFoundError(f"Reminder {reminder_id} not found")
        return updated

    def mark_missed(self, reminder_id: int) -> Reminder:
        """Mark a reminder as missed (timeout, no acknowledgement)."""
        reminder = self.get(reminder_id)
        new_state = self._sm.transition(reminder, "timeout")
        updated = self._repo.update_state(reminder_id, state=new_state)
        if updated is None:
            raise ReminderNotFoundError(f"Reminder {reminder_id} not found")
        logger.info("Reminder %d marked as missed", reminder_id)
        return updated

    def delete(self, reminder_id: int) -> bool:
        """Delete a reminder."""
        return self._repo.delete(reminder_id)

    def count_pending(self) -> int:
        """Count reminders in pending state."""
        return self._repo.count_by_state(ReminderState.PENDING)

    def count_reminding(self) -> int:
        """Count reminders in reminding state."""
        return self._repo.count_by_state(ReminderState.REMINDING)
