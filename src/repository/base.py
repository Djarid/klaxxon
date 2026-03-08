"""Abstract repository interface for meeting storage.

Dependency Inversion: high-level modules (MeetingService, ReminderEngine)
depend on this ABC, not on the concrete SQLite implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..models.meeting import Meeting, MeetingState


class MeetingRepository(ABC):
    """Abstract interface for meeting persistence."""

    @abstractmethod
    def create(self, meeting: Meeting) -> Meeting:
        """Persist a new meeting. Returns the meeting with id populated."""
        ...

    @abstractmethod
    def get(self, meeting_id: int) -> Optional[Meeting]:
        """Retrieve a meeting by id. Returns None if not found."""
        ...

    @abstractmethod
    def list_all(
        self,
        state: Optional[MeetingState] = None,
    ) -> list[Meeting]:
        """List meetings, optionally filtered by state."""
        ...

    @abstractmethod
    def list_upcoming(
        self,
        before: Optional[datetime] = None,
        states: Optional[list[MeetingState]] = None,
    ) -> list[Meeting]:
        """List upcoming meetings (starts_at in the future or recently started)."""
        ...

    @abstractmethod
    def update_state(
        self,
        meeting_id: int,
        state: MeetingState,
        ack_keyword: Optional[str] = None,
        ack_at: Optional[datetime] = None,
    ) -> Optional[Meeting]:
        """Update a meeting's state. Returns updated meeting or None."""
        ...

    @abstractmethod
    def delete(self, meeting_id: int) -> bool:
        """Delete a meeting. Returns True if deleted."""
        ...

    @abstractmethod
    def count_by_state(self, state: MeetingState) -> int:
        """Count meetings in a given state."""
        ...
