"""Meeting service: the single source of truth for business logic.

DRY: API routes and Signal handler both delegate here.
No business logic exists outside this module.

Single Responsibility: orchestrates repository + state machine.
Does not send notifications or parse input formats.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..models.meeting import Meeting, MeetingState
from ..repository.base import MeetingRepository
from .state_machine import InvalidTransitionError, MeetingStateMachine

logger = logging.getLogger(__name__)


class DuplicateMeetingError(Exception):
    """Raised when a duplicate meeting is detected."""

    pass


class MeetingNotFoundError(Exception):
    """Raised when a meeting is not found."""

    pass


class PastMeetingError(Exception):
    """Raised when trying to create a meeting in the past."""

    pass


class MeetingService:
    """Business logic for meeting operations.

    Both the API and Signal handler call these methods.
    This is the DRY layer: change business rules here once.
    """

    def __init__(self, repository: MeetingRepository) -> None:
        self._repo = repository
        self._sm = MeetingStateMachine()

    def create(
        self,
        title: str,
        starts_at: datetime,
        duration_min: int = 90,
        link: Optional[str] = None,
        source: str = "manual",
    ) -> Meeting:
        """Create a new meeting.

        Validates:
        - starts_at is in the future
        - No duplicate (same title within 30 min of the same time)
        """
        now = datetime.now(timezone.utc)
        if starts_at.tzinfo is None:
            raise ValueError("starts_at must be timezone-aware")
        if starts_at <= now:
            raise PastMeetingError(
                f"Cannot create meeting in the past: {starts_at.isoformat()}"
            )

        # Duplicate check: same title within 30 min window
        existing = self._repo.list_all()
        for m in existing:
            if m.title == title and m.starts_at is not None:
                diff = abs((m.starts_at - starts_at).total_seconds())
                if diff < 1800:  # 30 minutes
                    raise DuplicateMeetingError(
                        f"Meeting '{title}' already exists at {m.starts_at.isoformat()}"
                    )

        meeting = Meeting(
            title=title,
            starts_at=starts_at,
            duration_min=duration_min,
            link=link,
            source=source,
            state=MeetingState.PENDING,
        )
        return self._repo.create(meeting)

    def get(self, meeting_id: int) -> Meeting:
        """Get a meeting by id."""
        meeting = self._repo.get(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting {meeting_id} not found")
        return meeting

    def list_meetings(
        self,
        state: Optional[MeetingState] = None,
    ) -> list[Meeting]:
        """List meetings, optionally filtered by state."""
        return self._repo.list_all(state=state)

    def acknowledge(self, meeting_id: int, keyword: str = "ack") -> Meeting:
        """Acknowledge a meeting. Stops reminders.

        Same function called by API and Signal handler (DRY).
        """
        meeting = self.get(meeting_id)
        new_state = self._sm.transition(meeting, "ack")
        now = datetime.now(timezone.utc)
        updated = self._repo.update_state(
            meeting_id,
            state=new_state,
            ack_keyword=keyword,
            ack_at=now,
        )
        if updated is None:
            raise MeetingNotFoundError(f"Meeting {meeting_id} not found")
        logger.info("Meeting %d acknowledged with '%s'", meeting_id, keyword)
        return updated

    def skip(self, meeting_id: int) -> Meeting:
        """Skip a meeting deliberately. Stops reminders.

        Same function called by API and Signal handler (DRY).
        """
        meeting = self.get(meeting_id)
        new_state = self._sm.transition(meeting, "skip")
        updated = self._repo.update_state(meeting_id, state=new_state)
        if updated is None:
            raise MeetingNotFoundError(f"Meeting {meeting_id} not found")
        logger.info("Meeting %d skipped", meeting_id)
        return updated

    def mark_reminding(self, meeting_id: int) -> Meeting:
        """Transition meeting to reminding state on first reminder."""
        meeting = self.get(meeting_id)
        new_state = self._sm.transition(meeting, "reminder_sent")
        updated = self._repo.update_state(meeting_id, state=new_state)
        if updated is None:
            raise MeetingNotFoundError(f"Meeting {meeting_id} not found")
        return updated

    def mark_missed(self, meeting_id: int) -> Meeting:
        """Mark a meeting as missed (timeout, no acknowledgement)."""
        meeting = self.get(meeting_id)
        new_state = self._sm.transition(meeting, "timeout")
        updated = self._repo.update_state(meeting_id, state=new_state)
        if updated is None:
            raise MeetingNotFoundError(f"Meeting {meeting_id} not found")
        logger.info("Meeting %d marked as missed", meeting_id)
        return updated

    def delete(self, meeting_id: int) -> bool:
        """Delete a meeting."""
        return self._repo.delete(meeting_id)

    def count_pending(self) -> int:
        """Count meetings in pending state."""
        return self._repo.count_by_state(MeetingState.PENDING)

    def count_reminding(self) -> int:
        """Count meetings in reminding state."""
        return self._repo.count_by_state(MeetingState.REMINDING)
