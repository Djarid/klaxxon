"""TDD tests for has_active_for_schedule() — AC-10.

These tests verify the new repository method that checks whether an active
(PENDING or REMINDING) reminder instance exists for a given schedule_id.

All tests FAIL until:
  - `has_active_for_schedule(schedule_id)` is added to `ReminderRepository` ABC
    in `src/repository/base.py`
  - `has_active_for_schedule(schedule_id)` is implemented on
    `SqliteReminderRepository` in `src/repository/sqlite.py`
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models.reminder import Reminder, ReminderState
from src.repository.sqlite import SqliteReminderRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _create_reminder(
    repo: SqliteReminderRepository,
    *,
    schedule_id: int,
    state: ReminderState = ReminderState.PENDING,
    title: str = "Test Reminder",
) -> Reminder:
    """Create a reminder and transition it to the requested state."""
    r = repo.create(
        Reminder(
            title=title,
            starts_at=_future(),
            schedule_id=schedule_id,
            state=ReminderState.PENDING,
        )
    )
    assert r.id is not None

    if state == ReminderState.PENDING:
        pass  # already PENDING
    elif state == ReminderState.REMINDING:
        repo.update_state(r.id, ReminderState.REMINDING)
    elif state == ReminderState.ACKNOWLEDGED:
        repo.update_state(r.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack")
    elif state == ReminderState.SKIPPED:
        repo.update_state(r.id, ReminderState.SKIPPED)
    elif state == ReminderState.MISSED:
        repo.update_state(r.id, ReminderState.REMINDING)
        repo.update_state(r.id, ReminderState.MISSED)
    else:
        pytest.fail(f"Unsupported state: {state}")

    return repo.get(r.id)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# AC-10 — has_active_for_schedule() existence check
# ---------------------------------------------------------------------------


class TestHasActiveForScheduleMethod:
    """Verify the method exists and is callable on the repository."""

    def test_method_exists_on_repository(self, repo: SqliteReminderRepository) -> None:
        """has_active_for_schedule must be a callable attribute on the repo."""
        assert hasattr(repo, "has_active_for_schedule"), (
            "SqliteReminderRepository is missing has_active_for_schedule() — "
            "add it to base.py (ABC) and sqlite.py (implementation)"
        )
        assert callable(repo.has_active_for_schedule), (
            "has_active_for_schedule must be callable"
        )

    def test_method_on_abstract_base(self) -> None:
        """has_active_for_schedule must be declared abstract on ReminderRepository."""
        try:
            from src.repository.base import ReminderRepository
        except ImportError:
            pytest.fail("Cannot import ReminderRepository from src.repository.base")

        # The ABC must list it as an abstract method so concrete classes must implement it
        abstract_methods = getattr(ReminderRepository, "__abstractmethods__", set())
        assert "has_active_for_schedule" in abstract_methods, (
            "has_active_for_schedule must be declared @abstractmethod on ReminderRepository ABC"
        )


# ---------------------------------------------------------------------------
# AC-10 — PENDING state → returns True
# ---------------------------------------------------------------------------


class TestHasActiveForSchedulePending:
    """Returns True when a PENDING instance exists for the schedule."""

    def test_returns_true_for_pending(self, repo: SqliteReminderRepository) -> None:
        """has_active_for_schedule returns True when schedule has a PENDING instance."""
        _create_reminder(repo, schedule_id=1, state=ReminderState.PENDING)

        result = repo.has_active_for_schedule(1)

        assert result is True, (
            "Expected True when a PENDING instance exists for schedule_id=1"
        )

    def test_returns_true_with_multiple_pending(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Returns True even when there are multiple PENDING instances (pre-migration state)."""
        _create_reminder(repo, schedule_id=5, state=ReminderState.PENDING)
        _create_reminder(repo, schedule_id=5, state=ReminderState.PENDING)
        _create_reminder(repo, schedule_id=5, state=ReminderState.PENDING)

        result = repo.has_active_for_schedule(5)

        assert result is True, (
            "Expected True when 3 PENDING instances exist for schedule_id=5 (E-14)"
        )


# ---------------------------------------------------------------------------
# AC-10 — REMINDING state → returns True
# ---------------------------------------------------------------------------


class TestHasActiveForScheduleReminding:
    """Returns True when a REMINDING instance exists for the schedule."""

    def test_returns_true_for_reminding(self, repo: SqliteReminderRepository) -> None:
        """has_active_for_schedule returns True when schedule has a REMINDING instance."""
        _create_reminder(repo, schedule_id=2, state=ReminderState.REMINDING)

        result = repo.has_active_for_schedule(2)

        assert result is True, (
            "Expected True when a REMINDING instance exists for schedule_id=2 (E-2)"
        )


# ---------------------------------------------------------------------------
# AC-10 — Terminal states → returns False
# ---------------------------------------------------------------------------


class TestHasActiveForScheduleTerminalStates:
    """Returns False when only terminal-state instances exist for the schedule."""

    def test_returns_false_for_acknowledged(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Returns False when schedule's only instance is ACKNOWLEDGED."""
        _create_reminder(repo, schedule_id=3, state=ReminderState.ACKNOWLEDGED)

        result = repo.has_active_for_schedule(3)

        assert result is False, (
            "Expected False when only ACKNOWLEDGED instance exists for schedule_id=3 (E-3)"
        )

    def test_returns_false_for_skipped(self, repo: SqliteReminderRepository) -> None:
        """Returns False when schedule's only instance is SKIPPED."""
        _create_reminder(repo, schedule_id=4, state=ReminderState.SKIPPED)

        result = repo.has_active_for_schedule(4)

        assert result is False, (
            "Expected False when only SKIPPED instance exists for schedule_id=4 (E-4)"
        )

    def test_returns_false_for_missed(self, repo: SqliteReminderRepository) -> None:
        """Returns False when schedule's only instance is MISSED."""
        _create_reminder(repo, schedule_id=6, state=ReminderState.MISSED)

        result = repo.has_active_for_schedule(6)

        assert result is False, (
            "Expected False when only MISSED instance exists for schedule_id=6 (E-5)"
        )

    def test_returns_false_for_multiple_terminal_states(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Returns False when all instances are in terminal states (mix)."""
        _create_reminder(repo, schedule_id=7, state=ReminderState.ACKNOWLEDGED)
        _create_reminder(repo, schedule_id=7, state=ReminderState.SKIPPED)
        _create_reminder(repo, schedule_id=7, state=ReminderState.MISSED)

        result = repo.has_active_for_schedule(7)

        assert result is False, (
            "Expected False when all instances are terminal for schedule_id=7"
        )


# ---------------------------------------------------------------------------
# AC-10 — No reminders → returns False
# ---------------------------------------------------------------------------


class TestHasActiveForScheduleNoReminders:
    """Returns False when no reminders exist at all for the schedule."""

    def test_returns_false_for_unknown_schedule(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Returns False when no reminders exist for this schedule_id."""
        result = repo.has_active_for_schedule(999)

        assert result is False, (
            "Expected False for schedule_id=999 which has no reminders at all"
        )

    def test_returns_false_for_brand_new_schedule(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Returns False for a schedule that has never had any instances."""
        # Create a reminder for a different schedule
        _create_reminder(repo, schedule_id=1, state=ReminderState.PENDING)

        result = repo.has_active_for_schedule(2)

        assert result is False, (
            "Expected False for schedule_id=2 which has no reminders (only schedule 1 does)"
        )


# ---------------------------------------------------------------------------
# AC-10 — Isolation between schedules
# ---------------------------------------------------------------------------


class TestHasActiveForScheduleIsolation:
    """Results are per-schedule — one schedule's state does not affect another."""

    def test_spec_example_schedule_1_has_active(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Spec AC-10: reminder A: schedule_id=1 PENDING → has_active(1) returns True."""
        _create_reminder(repo, schedule_id=1, state=ReminderState.PENDING, title="A")
        _create_reminder(
            repo, schedule_id=1, state=ReminderState.ACKNOWLEDGED, title="B"
        )
        _create_reminder(repo, schedule_id=2, state=ReminderState.MISSED, title="C")

        result = repo.has_active_for_schedule(1)

        assert result is True, (
            "AC-10: reminder A (PENDING, schedule_id=1) should make has_active(1)=True"
        )

    def test_spec_example_schedule_2_no_active(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Spec AC-10: reminder C: schedule_id=2 MISSED → has_active(2) returns False."""
        _create_reminder(repo, schedule_id=1, state=ReminderState.PENDING, title="A")
        _create_reminder(
            repo, schedule_id=1, state=ReminderState.ACKNOWLEDGED, title="B"
        )
        _create_reminder(repo, schedule_id=2, state=ReminderState.MISSED, title="C")

        result = repo.has_active_for_schedule(2)

        assert result is False, (
            "AC-10: reminder C (MISSED, schedule_id=2) → has_active(2) must be False"
        )

    def test_spec_example_unknown_schedule_no_active(
        self, repo: SqliteReminderRepository
    ) -> None:
        """Spec AC-10: has_active(999) returns False — no reminders for this schedule."""
        _create_reminder(repo, schedule_id=1, state=ReminderState.PENDING, title="A")

        result = repo.has_active_for_schedule(999)

        assert result is False, (
            "AC-10: has_active(999) must be False when no reminders for that schedule"
        )

    def test_active_in_one_schedule_does_not_affect_another(
        self, repo: SqliteReminderRepository
    ) -> None:
        """PENDING in schedule A should not cause has_active(B) to return True."""
        _create_reminder(repo, schedule_id=10, state=ReminderState.PENDING)
        _create_reminder(repo, schedule_id=11, state=ReminderState.ACKNOWLEDGED)

        assert repo.has_active_for_schedule(10) is True
        assert repo.has_active_for_schedule(11) is False, (
            "Schedule 11 has no active instances — schedule 10's PENDING must not bleed across"
        )

    def test_one_off_reminders_not_confused_with_schedules(
        self, repo: SqliteReminderRepository
    ) -> None:
        """One-off reminders (schedule_id=NULL) must not interfere with schedule checks."""
        # Create a one-off reminder (schedule_id = None)
        repo.create(
            Reminder(
                title="One-off",
                starts_at=_future(),
                schedule_id=None,
                state=ReminderState.PENDING,
            )
        )

        # Schedule 1 has no reminders — should still return False
        result = repo.has_active_for_schedule(1)

        assert result is False, (
            "One-off reminder (schedule_id=NULL) must not cause has_active(1) to return True"
        )


# ---------------------------------------------------------------------------
# AC-10 — State transitions update the result correctly
# ---------------------------------------------------------------------------


class TestHasActiveForScheduleAfterTransition:
    """Verifies the method reflects state transitions correctly."""

    def test_becomes_false_after_acknowledgement(
        self, repo: SqliteReminderRepository
    ) -> None:
        """After PENDING → ACKNOWLEDGED, has_active returns False (E-3)."""
        r = _create_reminder(repo, schedule_id=20, state=ReminderState.PENDING)

        assert repo.has_active_for_schedule(20) is True

        repo.update_state(r.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack")

        assert repo.has_active_for_schedule(20) is False, (
            "After acknowledgement, has_active must return False — next spawn should proceed"
        )

    def test_becomes_false_after_skip(self, repo: SqliteReminderRepository) -> None:
        """After PENDING → SKIPPED, has_active returns False (E-4)."""
        r = _create_reminder(repo, schedule_id=21, state=ReminderState.PENDING)

        assert repo.has_active_for_schedule(21) is True

        repo.update_state(r.id, ReminderState.SKIPPED)

        assert repo.has_active_for_schedule(21) is False, (
            "After skip, has_active must return False"
        )

    def test_becomes_false_after_missed(self, repo: SqliteReminderRepository) -> None:
        """After REMINDING → MISSED, has_active returns False (E-5)."""
        r = _create_reminder(repo, schedule_id=22, state=ReminderState.REMINDING)

        assert repo.has_active_for_schedule(22) is True

        repo.update_state(r.id, ReminderState.MISSED)

        assert repo.has_active_for_schedule(22) is False, (
            "After missed, has_active must return False"
        )

    def test_returns_bool_type(self, repo: SqliteReminderRepository) -> None:
        """has_active_for_schedule must return a proper bool, not a truthy/falsy int."""
        _create_reminder(repo, schedule_id=30, state=ReminderState.PENDING)

        result_true = repo.has_active_for_schedule(30)
        result_false = repo.has_active_for_schedule(999)

        assert isinstance(result_true, bool), (
            f"Expected bool, got {type(result_true).__name__}"
        )
        assert isinstance(result_false, bool), (
            f"Expected bool, got {type(result_false).__name__}"
        )
