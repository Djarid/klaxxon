"""Tests for the meeting state machine."""

import pytest

from src.models.meeting import Meeting, MeetingState
from src.services.state_machine import InvalidTransitionError, MeetingStateMachine


def _meeting(state: MeetingState) -> Meeting:
    return Meeting(id=1, title="Test", state=state)


class TestValidTransitions:
    def test_pending_to_reminding(self) -> None:
        m = _meeting(MeetingState.PENDING)
        assert (
            MeetingStateMachine.transition(m, "reminder_sent") == MeetingState.REMINDING
        )

    def test_reminding_stays_reminding(self) -> None:
        m = _meeting(MeetingState.REMINDING)
        assert (
            MeetingStateMachine.transition(m, "reminder_sent") == MeetingState.REMINDING
        )

    def test_pending_ack(self) -> None:
        m = _meeting(MeetingState.PENDING)
        assert MeetingStateMachine.transition(m, "ack") == MeetingState.ACKNOWLEDGED

    def test_reminding_ack(self) -> None:
        m = _meeting(MeetingState.REMINDING)
        assert MeetingStateMachine.transition(m, "ack") == MeetingState.ACKNOWLEDGED

    def test_pending_skip(self) -> None:
        m = _meeting(MeetingState.PENDING)
        assert MeetingStateMachine.transition(m, "skip") == MeetingState.SKIPPED

    def test_reminding_skip(self) -> None:
        m = _meeting(MeetingState.REMINDING)
        assert MeetingStateMachine.transition(m, "skip") == MeetingState.SKIPPED

    def test_reminding_timeout(self) -> None:
        m = _meeting(MeetingState.REMINDING)
        assert MeetingStateMachine.transition(m, "timeout") == MeetingState.MISSED


class TestInvalidTransitions:
    def test_acknowledged_cannot_ack(self) -> None:
        m = _meeting(MeetingState.ACKNOWLEDGED)
        with pytest.raises(InvalidTransitionError):
            MeetingStateMachine.transition(m, "ack")

    def test_missed_cannot_ack(self) -> None:
        m = _meeting(MeetingState.MISSED)
        with pytest.raises(InvalidTransitionError):
            MeetingStateMachine.transition(m, "ack")

    def test_skipped_cannot_skip(self) -> None:
        m = _meeting(MeetingState.SKIPPED)
        with pytest.raises(InvalidTransitionError):
            MeetingStateMachine.transition(m, "skip")

    def test_pending_cannot_timeout(self) -> None:
        m = _meeting(MeetingState.PENDING)
        with pytest.raises(InvalidTransitionError):
            MeetingStateMachine.transition(m, "timeout")

    def test_acknowledged_cannot_timeout(self) -> None:
        m = _meeting(MeetingState.ACKNOWLEDGED)
        with pytest.raises(InvalidTransitionError):
            MeetingStateMachine.transition(m, "timeout")

    def test_unknown_event(self) -> None:
        m = _meeting(MeetingState.PENDING)
        with pytest.raises(InvalidTransitionError):
            MeetingStateMachine.transition(m, "nonsense")


class TestCanTransition:
    def test_valid(self) -> None:
        m = _meeting(MeetingState.PENDING)
        assert MeetingStateMachine.can_transition(m, "ack") is True

    def test_invalid(self) -> None:
        m = _meeting(MeetingState.MISSED)
        assert MeetingStateMachine.can_transition(m, "ack") is False
