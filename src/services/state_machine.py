"""Reminder state machine.

Single Responsibility: validates and executes state transitions.
Does not persist, does not notify. Just enforces the rules.
"""

from __future__ import annotations

from ..models.reminder import Reminder, ReminderState


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""

    def __init__(self, current: ReminderState, event: str) -> None:
        self.current = current
        self.event = event
        super().__init__(
            f"Cannot apply event '{event}' to reminder in state '{current.value}'"
        )


# Valid transitions: (current_state, event) -> new_state
_TRANSITIONS: dict[tuple[ReminderState, str], ReminderState] = {
    # Start reminding
    (ReminderState.PENDING, "reminder_sent"): ReminderState.REMINDING,
    # Keep reminding (re-entry)
    (ReminderState.REMINDING, "reminder_sent"): ReminderState.REMINDING,
    # Acknowledge
    (ReminderState.PENDING, "ack"): ReminderState.ACKNOWLEDGED,
    (ReminderState.REMINDING, "ack"): ReminderState.ACKNOWLEDGED,
    # Skip
    (ReminderState.PENDING, "skip"): ReminderState.SKIPPED,
    (ReminderState.REMINDING, "skip"): ReminderState.SKIPPED,
    # Timeout (no response after reminder end)
    (ReminderState.REMINDING, "timeout"): ReminderState.MISSED,
}


class ReminderStateMachine:
    """Enforces valid reminder state transitions."""

    @staticmethod
    def transition(reminder: Reminder, event: str) -> ReminderState:
        """Return the new state for the given event, or raise InvalidTransitionError."""
        key = (reminder.state, event)
        new_state = _TRANSITIONS.get(key)
        if new_state is None:
            raise InvalidTransitionError(reminder.state, event)
        return new_state

    @staticmethod
    def can_transition(reminder: Reminder, event: str) -> bool:
        """Check if a transition is valid without raising."""
        return (reminder.state, event) in _TRANSITIONS
