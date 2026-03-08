"""Tests for the reminder state machine."""

import pytest

from src.models.reminder import Reminder, ReminderState
from src.services.state_machine import InvalidTransitionError, ReminderStateMachine


def _meeting(state: ReminderState) -> Reminder:
    return Reminder(id=1, title="Test", state=state)


class TestValidTransitions:
    def test_pending_to_reminding(self) -> None:
        m = _meeting(ReminderState.PENDING)
        assert (
            ReminderStateMachine.transition(m, "reminder_sent") == ReminderState.REMINDING
        )

    def test_reminding_stays_reminding(self) -> None:
        m = _meeting(ReminderState.REMINDING)
        assert (
            ReminderStateMachine.transition(m, "reminder_sent") == ReminderState.REMINDING
        )

    def test_pending_ack(self) -> None:
        m = _meeting(ReminderState.PENDING)
        assert ReminderStateMachine.transition(m, "ack") == ReminderState.ACKNOWLEDGED

    def test_reminding_ack(self) -> None:
        m = _meeting(ReminderState.REMINDING)
        assert ReminderStateMachine.transition(m, "ack") == ReminderState.ACKNOWLEDGED

    def test_pending_skip(self) -> None:
        m = _meeting(ReminderState.PENDING)
        assert ReminderStateMachine.transition(m, "skip") == ReminderState.SKIPPED

    def test_reminding_skip(self) -> None:
        m = _meeting(ReminderState.REMINDING)
        assert ReminderStateMachine.transition(m, "skip") == ReminderState.SKIPPED

    def test_reminding_timeout(self) -> None:
        m = _meeting(ReminderState.REMINDING)
        assert ReminderStateMachine.transition(m, "timeout") == ReminderState.MISSED


class TestInvalidTransitions:
    def test_acknowledged_cannot_ack(self) -> None:
        m = _meeting(ReminderState.ACKNOWLEDGED)
        with pytest.raises(InvalidTransitionError):
            ReminderStateMachine.transition(m, "ack")

    def test_missed_cannot_ack(self) -> None:
        m = _meeting(ReminderState.MISSED)
        with pytest.raises(InvalidTransitionError):
            ReminderStateMachine.transition(m, "ack")

    def test_skipped_cannot_skip(self) -> None:
        m = _meeting(ReminderState.SKIPPED)
        with pytest.raises(InvalidTransitionError):
            ReminderStateMachine.transition(m, "skip")

    def test_pending_cannot_timeout(self) -> None:
        m = _meeting(ReminderState.PENDING)
        with pytest.raises(InvalidTransitionError):
            ReminderStateMachine.transition(m, "timeout")

    def test_acknowledged_cannot_timeout(self) -> None:
        m = _meeting(ReminderState.ACKNOWLEDGED)
        with pytest.raises(InvalidTransitionError):
            ReminderStateMachine.transition(m, "timeout")

    def test_unknown_event(self) -> None:
        m = _meeting(ReminderState.PENDING)
        with pytest.raises(InvalidTransitionError):
            ReminderStateMachine.transition(m, "nonsense")


class TestCanTransition:
    def test_valid(self) -> None:
        m = _meeting(ReminderState.PENDING)
        assert ReminderStateMachine.can_transition(m, "ack") is True

    def test_invalid(self) -> None:
        m = _meeting(ReminderState.MISSED)
        assert ReminderStateMachine.can_transition(m, "ack") is False
