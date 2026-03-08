"""Abstract repository interfaces for reminder and schedule storage.

Dependency Inversion: high-level modules (ReminderService, ReminderEngine, ScheduleService)
depend on these ABCs, not on the concrete SQLite implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

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
