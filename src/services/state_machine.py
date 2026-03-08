"""Meeting state machine.

Single Responsibility: validates and executes state transitions.
Does not persist, does not notify. Just enforces the rules.
"""

from __future__ import annotations

from ..models.meeting import Meeting, MeetingState


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: MeetingState, event: str) -> None:
        self.current = current
        self.event = event
        super().__init__(
            f"Cannot apply event '{event}' to meeting in state '{current.value}'"
        )


# Valid transitions: (current_state, event) -> new_state
_TRANSITIONS: dict[tuple[MeetingState, str], MeetingState] = {
    # Start reminding
    (MeetingState.PENDING, "reminder_sent"): MeetingState.REMINDING,
    # Keep reminding (re-entry)
    (MeetingState.REMINDING, "reminder_sent"): MeetingState.REMINDING,
    # Acknowledge
    (MeetingState.PENDING, "ack"): MeetingState.ACKNOWLEDGED,
    (MeetingState.REMINDING, "ack"): MeetingState.ACKNOWLEDGED,
    # Skip
    (MeetingState.PENDING, "skip"): MeetingState.SKIPPED,
    (MeetingState.REMINDING, "skip"): MeetingState.SKIPPED,
    # Timeout (no response after meeting end)
    (MeetingState.REMINDING, "timeout"): MeetingState.MISSED,
}


class MeetingStateMachine:
    """Enforces valid meeting state transitions."""

    @staticmethod
    def transition(meeting: Meeting, event: str) -> MeetingState:
        """Return the new state for the given event, or raise InvalidTransitionError."""
        key = (meeting.state, event)
        new_state = _TRANSITIONS.get(key)
        if new_state is None:
            raise InvalidTransitionError(meeting.state, event)
        return new_state

    @staticmethod
    def can_transition(meeting: Meeting, event: str) -> bool:
        """Check if a transition is valid without raising."""
        return (meeting.state, event) in _TRANSITIONS
