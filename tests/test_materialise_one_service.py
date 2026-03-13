"""TDD tests for the "at most one active instance per schedule" feature.

Covers AC-1 through AC-9, AC-11, AC-12, AC-13 and all edge cases from the
recurrent-materialise-one spec.

All tests FAIL until:
  - `spawn_reminders()` in `src/services/schedule_service.py` is modified to:
      (a) call `has_active_for_schedule()` before processing each schedule and
          skip when it returns True, AND
      (b) create only the FIRST (earliest) non-duplicate occurrence per schedule
          instead of all occurrences in the 48-hour window.
  - `has_active_for_schedule()` is implemented on the repository (see
    test_materialise_one_repository.py for those tests).

Imports are lazy (inside each test) to avoid ModuleNotFoundError at
collection time if implementation files don't exist yet.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.models.reminder import Reminder, ReminderState
from src.models.schedule import Schedule
from src.repository.sqlite import SqliteReminderRepository
from src.repository.schedule_sqlite import SqliteScheduleRepository
from src.services.schedule_service import ScheduleService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _thread_safe_repo() -> SqliteReminderRepository:
    """Return an in-memory repo that works from any thread (needed for helpers)."""
    r = SqliteReminderRepository(":memory:")
    if r._conn:
        r._conn.close()
    r._conn = sqlite3.connect(":memory:", check_same_thread=False)
    r._conn.row_factory = sqlite3.Row
    r._conn.execute("PRAGMA journal_mode=WAL")
    r._conn.execute("PRAGMA foreign_keys=ON")
    r._ensure_schema()
    return r


def _make_service(
    reminder_repo: Optional[SqliteReminderRepository] = None,
    timezone_name: str = "UTC",
) -> tuple[SqliteScheduleRepository, SqliteReminderRepository, ScheduleService]:
    """Return (schedule_repo, reminder_repo, service) wired together."""
    sched_repo = SqliteScheduleRepository(":memory:")
    if reminder_repo is None:
        reminder_repo = SqliteReminderRepository(":memory:")
    svc = ScheduleService(
        schedule_repo=sched_repo,
        reminder_repo=reminder_repo,
        timezone_name=timezone_name,
    )
    return sched_repo, reminder_repo, svc


def _inject_reminder(
    reminder_repo: SqliteReminderRepository,
    *,
    schedule_id: int,
    state: ReminderState = ReminderState.PENDING,
    starts_at: Optional[datetime] = None,
    title: str = "Injected",
) -> Reminder:
    """Directly insert a reminder into the repo in the given state."""
    if starts_at is None:
        starts_at = _future(2)
    r = reminder_repo.create(
        Reminder(
            title=title,
            starts_at=starts_at,
            schedule_id=schedule_id,
            state=ReminderState.PENDING,
        )
    )
    assert r.id is not None

    if state == ReminderState.PENDING:
        pass
    elif state == ReminderState.REMINDING:
        reminder_repo.update_state(r.id, ReminderState.REMINDING)
    elif state == ReminderState.ACKNOWLEDGED:
        reminder_repo.update_state(r.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack")
    elif state == ReminderState.SKIPPED:
        reminder_repo.update_state(r.id, ReminderState.SKIPPED)
    elif state == ReminderState.MISSED:
        reminder_repo.update_state(r.id, ReminderState.REMINDING)
        reminder_repo.update_state(r.id, ReminderState.MISSED)

    return reminder_repo.get(r.id)  # type: ignore[return-value]


def _count_active_for_schedule(
    reminder_repo: SqliteReminderRepository, schedule_id: int
) -> int:
    """Count reminders in PENDING or REMINDING state for a schedule."""
    all_r = reminder_repo.list_all()
    return sum(
        1
        for r in all_r
        if r.schedule_id == schedule_id
        and r.state in (ReminderState.PENDING, ReminderState.REMINDING)
    )


# ---------------------------------------------------------------------------
# AC-1: Single active instance per schedule — PENDING blocks new spawn
# ---------------------------------------------------------------------------


class TestAC1PendingBlocksSpawn:
    """GIVEN a PENDING instance exists → spawn_reminders() must NOT create another."""

    def test_pending_blocks_new_spawn(self) -> None:
        """AC-1: spawn creates 0 new reminders when PENDING instance already exists."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Pre-seed: PENDING instance already exists for this schedule
        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.PENDING,
        )

        spawned = svc.spawn_reminders()

        assert len(spawned) == 0, (
            "AC-1: spawn_reminders() must not create new instances when a "
            f"PENDING instance already exists. Got {len(spawned)} new instances."
        )

    def test_exactly_one_pending_remains_after_spawn(self) -> None:
        """AC-1: exactly 1 PENDING reminder exists for the schedule after spawn."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.PENDING,
        )

        svc.spawn_reminders()

        active_count = _count_active_for_schedule(reminder_repo, schedule.id)
        assert active_count == 1, (
            "AC-1: exactly 1 active (PENDING/REMINDING) reminder should exist "
            f"for schedule {schedule.id} after spawn. Found {active_count}."
        )


# ---------------------------------------------------------------------------
# AC-5: REMINDING instance blocks new spawn
# ---------------------------------------------------------------------------


class TestAC5RemindingBlocksSpawn:
    """GIVEN a REMINDING instance exists → spawn_reminders() must NOT create another."""

    def test_reminding_blocks_new_spawn(self) -> None:
        """AC-5: spawn creates 0 new reminders when a REMINDING instance already exists."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.REMINDING,
        )

        spawned = svc.spawn_reminders()

        assert len(spawned) == 0, (
            "AC-5: spawn_reminders() must not create new instances when a "
            f"REMINDING instance already exists. Got {len(spawned)} new instances."
        )

    def test_reminding_instance_is_only_active_after_spawn(self) -> None:
        """AC-5: the REMINDING instance remains the only active instance."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        existing = _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.REMINDING,
        )

        svc.spawn_reminders()

        active = _count_active_for_schedule(reminder_repo, schedule.id)
        assert active == 1, (
            "AC-5: exactly 1 active reminder should remain after spawn blocked "
            f"by REMINDING. Got {active}."
        )
        # The original REMINDING one should still exist
        still_there = reminder_repo.get(existing.id)
        assert still_there is not None
        assert still_there.state == ReminderState.REMINDING, (
            "AC-5: the original REMINDING instance must not be modified by spawn"
        )


# ---------------------------------------------------------------------------
# AC-2: New instance created after ACKNOWLEDGED
# ---------------------------------------------------------------------------


class TestAC2SpawnAfterAcknowledge:
    """After all instances are ACKNOWLEDGED, spawn creates 1 new PENDING."""

    def test_spawn_after_acknowledge_creates_one(self) -> None:
        """AC-2: exactly 1 new PENDING instance is created when prior is ACKNOWLEDGED."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.ACKNOWLEDGED,
        )

        spawned = svc.spawn_reminders()

        assert len(spawned) == 1, (
            "AC-2: exactly 1 new PENDING instance expected after prior ACKNOWLEDGED. "
            f"Got {len(spawned)}."
        )
        assert spawned[0].state == ReminderState.PENDING
        assert spawned[0].schedule_id == schedule.id

    def test_spawn_after_acknowledge_only_one_total_active(self) -> None:
        """AC-2: only 1 active (PENDING) reminder for the schedule after spawn."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.ACKNOWLEDGED,
        )

        svc.spawn_reminders()

        active_count = _count_active_for_schedule(reminder_repo, schedule.id)
        assert active_count == 1, (
            f"AC-2: expected exactly 1 active instance after spawn, got {active_count}"
        )


# ---------------------------------------------------------------------------
# AC-3: New instance created after SKIPPED
# ---------------------------------------------------------------------------


class TestAC3SpawnAfterSkip:
    """After all instances are SKIPPED, spawn creates 1 new PENDING."""

    def test_spawn_after_skip_creates_one(self) -> None:
        """AC-3: exactly 1 new PENDING instance is created when prior is SKIPPED."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Evening Medication",
            time_of_day="20:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.SKIPPED,
        )

        spawned = svc.spawn_reminders()

        assert len(spawned) == 1, (
            "AC-3: exactly 1 new PENDING instance expected after prior SKIPPED. "
            f"Got {len(spawned)}."
        )
        assert spawned[0].state == ReminderState.PENDING
        assert spawned[0].schedule_id == schedule.id

    def test_spawn_after_skip_total_active_is_one(self) -> None:
        """AC-3: only 1 active reminder remains after new spawn following SKIPPED."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Evening Medication",
            time_of_day="20:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.SKIPPED,
        )
        svc.spawn_reminders()

        active_count = _count_active_for_schedule(reminder_repo, schedule.id)
        assert active_count == 1, (
            f"AC-3: expected 1 active instance after skip-then-spawn, got {active_count}"
        )


# ---------------------------------------------------------------------------
# AC-4: New instance created after MISSED
# ---------------------------------------------------------------------------


class TestAC4SpawnAfterMissed:
    """After all instances are MISSED, spawn creates 1 new PENDING."""

    def test_spawn_after_missed_creates_one(self) -> None:
        """AC-4: exactly 1 new PENDING instance is created when prior is MISSED."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Evening Medication",
            time_of_day="20:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.MISSED,
        )

        spawned = svc.spawn_reminders()

        assert len(spawned) == 1, (
            "AC-4: exactly 1 new PENDING instance expected after prior MISSED. "
            f"Got {len(spawned)}."
        )
        assert spawned[0].state == ReminderState.PENDING
        assert spawned[0].schedule_id == schedule.id

    def test_spawn_after_missed_total_active_is_one(self) -> None:
        """AC-4: only 1 active reminder remains after new spawn following MISSED."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Evening Medication",
            time_of_day="20:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.MISSED,
        )
        svc.spawn_reminders()

        active_count = _count_active_for_schedule(reminder_repo, schedule.id)
        assert active_count == 1, (
            f"AC-4: expected 1 active instance after missed-then-spawn, got {active_count}"
        )


# ---------------------------------------------------------------------------
# AC-6: App offline catch-up — only one instance materialised (no flood)
# ---------------------------------------------------------------------------


class TestAC6NoCatchUpFlood:
    """After app offline for N days, only 1 new instance is materialised (E-6)."""

    def test_no_flood_after_3_days_offline(self) -> None:
        """AC-6: only 1 PENDING instance materialised after 3 missed days."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Simulate being offline: last ACKNOWLEDGED was 3 days ago
        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.ACKNOWLEDGED,
            starts_at=datetime.now(timezone.utc) - timedelta(days=3),
        )

        # App starts, spawn runs for the first time in 3 days
        spawned = svc.spawn_reminders()

        assert len(spawned) == 1, (
            "AC-6: after 3 days offline with daily schedule, exactly 1 new "
            f"instance should be spawned (not 3+). Got {len(spawned)}."
        )

    def test_no_flood_no_instances_for_missed_days(self) -> None:
        """AC-6: only 1 instance spawned (not 3) after 3-days offline."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.ACKNOWLEDGED,
            starts_at=datetime.now(timezone.utc) - timedelta(days=3),
        )

        spawned = svc.spawn_reminders()

        # Old behaviour spawns 3 (48h window = today + tomorrow + day-after).
        # New behaviour should spawn exactly 1.
        assert len(spawned) == 1, (
            "AC-6: after 3 days offline, exactly 1 instance must be spawned "
            f"(the next upcoming occurrence). Got {len(spawned)}."
        )

    def test_single_instance_after_extended_offline(self) -> None:
        """AC-6/E-6: regardless of how long offline, always at most 1 new instance."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Daily Pill",
            time_of_day="09:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Last acknowledged 7 days ago
        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.ACKNOWLEDGED,
            starts_at=datetime.now(timezone.utc) - timedelta(days=7),
        )

        spawned = svc.spawn_reminders()

        assert len(spawned) <= 1, (
            "AC-6: at most 1 instance should be spawned even after 7 days offline. "
            f"Got {len(spawned)}."
        )


# ---------------------------------------------------------------------------
# AC-7: Multiple schedules evaluated independently
# ---------------------------------------------------------------------------


class TestAC7MultipleSchedulesIndependent:
    """Each schedule is evaluated independently (E-7, E-9)."""

    def test_blocked_schedule_does_not_block_other(self) -> None:
        """AC-7: schedule A (PENDING) blocked; schedule B (ACKNOWLEDGED) spawns 1."""
        _, reminder_repo, svc = _make_service()

        sched_a = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        sched_b = svc.create(
            title="Evening Medication",
            time_of_day="20:00",
            recurrence="daily",
        )
        assert sched_a.id is not None
        assert sched_b.id is not None

        # A has a PENDING instance (blocked)
        _inject_reminder(
            reminder_repo,
            schedule_id=sched_a.id,
            state=ReminderState.PENDING,
        )
        # B has an ACKNOWLEDGED instance (eligible for new spawn)
        _inject_reminder(
            reminder_repo,
            schedule_id=sched_b.id,
            state=ReminderState.ACKNOWLEDGED,
        )

        spawned = svc.spawn_reminders()

        spawned_ids = [r.schedule_id for r in spawned]
        assert sched_a.id not in spawned_ids, (
            "AC-7: schedule A (PENDING) should NOT have a new instance spawned"
        )
        assert sched_b.id in spawned_ids, (
            "AC-7: schedule B (ACKNOWLEDGED) SHOULD have a new instance spawned"
        )
        b_spawned = [r for r in spawned if r.schedule_id == sched_b.id]
        assert len(b_spawned) == 1, (
            f"AC-7: exactly 1 new instance expected for schedule B, got {len(b_spawned)}"
        )

    def test_both_schedules_get_one_instance_when_no_active(self) -> None:
        """AC-7/E-9: two schedules with no active instances each get exactly 1 new one."""
        _, reminder_repo, svc = _make_service()

        sched_a = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        sched_b = svc.create(
            title="Evening Medication",
            time_of_day="20:00",
            recurrence="daily",
        )
        assert sched_a.id is not None
        assert sched_b.id is not None

        # Both have only terminal (ACKNOWLEDGED) instances
        _inject_reminder(
            reminder_repo,
            schedule_id=sched_a.id,
            state=ReminderState.ACKNOWLEDGED,
        )
        _inject_reminder(
            reminder_repo,
            schedule_id=sched_b.id,
            state=ReminderState.ACKNOWLEDGED,
        )

        spawned = svc.spawn_reminders()

        a_spawned = [r for r in spawned if r.schedule_id == sched_a.id]
        b_spawned = [r for r in spawned if r.schedule_id == sched_b.id]

        assert len(a_spawned) == 1, (
            f"AC-7: expected 1 new instance for schedule A, got {len(a_spawned)}"
        )
        assert len(b_spawned) == 1, (
            f"AC-7: expected 1 new instance for schedule B, got {len(b_spawned)}"
        )

    def test_total_spawned_is_at_most_one_per_schedule(self) -> None:
        """AC-7: with N schedules and no active instances, at most N instances spawned."""
        _, reminder_repo, svc = _make_service()

        n = 4
        schedule_ids = []
        for i in range(n):
            s = svc.create(
                title=f"Schedule {i}",
                time_of_day=f"{8 + i:02d}:00",
                recurrence="daily",
            )
            assert s.id is not None
            schedule_ids.append(s.id)
            _inject_reminder(
                reminder_repo,
                schedule_id=s.id,
                state=ReminderState.ACKNOWLEDGED,
            )

        spawned = svc.spawn_reminders()

        for sid in schedule_ids:
            sid_spawned = [r for r in spawned if r.schedule_id == sid]
            assert len(sid_spawned) <= 1, (
                f"At most 1 instance should be spawned per schedule. "
                f"Schedule {sid} got {len(sid_spawned)}."
            )


# ---------------------------------------------------------------------------
# AC-8: Weekly schedule — only next matching day materialised
# ---------------------------------------------------------------------------


class TestAC8WeeklyNextDayOnly:
    """For a weekly schedule, only the NEXT matching day is materialised (REQ-7)."""

    def test_weekly_spawn_creates_at_most_one_instance(self) -> None:
        """AC-8: weekly schedule spawns at most 1 instance (not multiple matching days)."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Team Meeting",
            time_of_day="14:00",
            recurrence="weekly",
            recurrence_rule="mon,wed,fri",
        )
        assert schedule.id is not None

        # No prior instances — eligible to spawn
        spawned = svc.spawn_reminders()

        assert len(spawned) <= 1, (
            "AC-8: weekly mon,wed,fri schedule must materialise at most 1 instance, "
            f"not {len(spawned)}. Only the next matching day should be spawned."
        )

    def test_weekly_spawn_after_ack_creates_at_most_one(self) -> None:
        """AC-8: after ACKNOWLEDGED, only 1 new instance for the next matching day."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Team Meeting",
            time_of_day="14:00",
            recurrence="weekly",
            recurrence_rule="mon,wed,fri",
        )
        assert schedule.id is not None

        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.ACKNOWLEDGED,
        )

        spawned = svc.spawn_reminders()

        assert len(spawned) <= 1, (
            "AC-8: after ACK, weekly schedule must materialise at most 1 new instance. "
            f"Got {len(spawned)}."
        )


# ---------------------------------------------------------------------------
# AC-9: One-off reminders unaffected
# ---------------------------------------------------------------------------


class TestAC9OneOffRemindersUnaffected:
    """One-off reminders (schedule_id=NULL) are completely unaffected (REQ-5, E-10)."""

    def test_spawn_does_not_touch_one_off_reminders(self) -> None:
        """AC-9: spawn_reminders() does not modify or touch one-off reminders."""
        _, reminder_repo, svc = _make_service()

        # Create a one-off reminder (no schedule, schedule_id=None)
        one_off = reminder_repo.create(
            Reminder(
                title="One-off Doctor Appointment",
                starts_at=_future(4),
                schedule_id=None,
                state=ReminderState.PENDING,
            )
        )
        assert one_off.id is not None

        # Create a schedule (so spawn has something to potentially process)
        svc.create(
            title="Daily Schedule",
            time_of_day="09:00",
            recurrence="daily",
        )

        before_spawn = reminder_repo.list_all()
        one_off_before = reminder_repo.get(one_off.id)

        svc.spawn_reminders()

        one_off_after = reminder_repo.get(one_off.id)
        assert one_off_after is not None, "AC-9: one-off reminder must not be deleted"
        assert one_off_after.state == one_off_before.state, (
            "AC-9: one-off reminder state must not be changed by spawn_reminders()"
        )
        assert one_off_after.starts_at == one_off_before.starts_at, (
            "AC-9: one-off reminder starts_at must not be changed by spawn_reminders()"
        )

    def test_one_off_not_in_schedule_active_check(self) -> None:
        """AC-9: one-off PENDING reminder must NOT block schedule-based spawning.

        The 'has_active_for_schedule' check uses schedule_id — it must NEVER
        confuse schedule_id=NULL with a real schedule_id.
        """
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Create a one-off PENDING reminder — should not block schedule spawning
        reminder_repo.create(
            Reminder(
                title="One-off (schedule_id=None)",
                starts_at=_future(2),
                schedule_id=None,
                state=ReminderState.PENDING,
            )
        )

        # Schedule has only an ACKNOWLEDGED instance → should spawn
        _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.ACKNOWLEDGED,
        )

        spawned = svc.spawn_reminders()

        sched_spawned = [r for r in spawned if r.schedule_id == schedule.id]
        assert len(sched_spawned) == 1, (
            "AC-9: one-off PENDING reminder must not block schedule-based spawning. "
            f"Expected 1 new instance for the schedule, got {len(sched_spawned)}."
        )


# ---------------------------------------------------------------------------
# AC-11: Backward compatibility — multiple PENDING instances resolve naturally
# ---------------------------------------------------------------------------


class TestAC11BackwardCompatMultiplePending:
    """Pre-migration state (multiple PENDING) resolves naturally without deletion (E-14)."""

    def test_no_new_spawn_with_multiple_pending(self) -> None:
        """AC-11: if 3 PENDING instances exist, no new ones are created."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Simulate pre-migration state: 3 PENDING instances for the same schedule
        for i in range(3):
            _inject_reminder(
                reminder_repo,
                schedule_id=schedule.id,
                state=ReminderState.PENDING,
                starts_at=_future(24 + i),
            )

        spawned = svc.spawn_reminders()

        assert len(spawned) == 0, (
            "AC-11: 3 pre-existing PENDING instances should block all new spawning. "
            f"Got {len(spawned)} new instances."
        )

    def test_existing_pending_instances_not_deleted(self) -> None:
        """AC-11: the existing PENDING instances are NOT deleted by spawn."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Create 3 PENDING instances
        created_ids = []
        for i in range(3):
            r = _inject_reminder(
                reminder_repo,
                schedule_id=schedule.id,
                state=ReminderState.PENDING,
                starts_at=_future(24 + i),
            )
            created_ids.append(r.id)

        svc.spawn_reminders()

        # All 3 must still exist
        for rid in created_ids:
            found = reminder_repo.get(rid)
            assert found is not None, (
                f"AC-11: PENDING reminder id={rid} was deleted by spawn_reminders() — "
                "existing instances must be left untouched."
            )
            assert found.state == ReminderState.PENDING, (
                f"AC-11: PENDING reminder id={rid} had its state changed to {found.state} "
                "by spawn_reminders() — must be left in PENDING."
            )

    def test_spawn_after_all_acknowledged_creates_one(self) -> None:
        """AC-11: once all are acknowledged, exactly 1 fresh instance is spawned."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Medication",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Create and acknowledge 3 instances (simulates the natural resolution)
        for i in range(3):
            r = _inject_reminder(
                reminder_repo,
                schedule_id=schedule.id,
                state=ReminderState.ACKNOWLEDGED,
                starts_at=_future(24 + i),
            )

        # Now all are terminal — spawn should create 1
        spawned = svc.spawn_reminders()

        assert len(spawned) == 1, (
            "AC-11: after all prior instances acknowledged, exactly 1 new instance "
            f"should be spawned. Got {len(spawned)}."
        )


# ---------------------------------------------------------------------------
# AC-12: API — GET /api/reminders?state=pending shows at most 1 per schedule
# ---------------------------------------------------------------------------


class TestAC12ApiPendingListClean:
    """GET /api/reminders?state=pending returns at most 1 per schedule (REQ-9)."""

    @pytest.fixture
    def thread_safe_reminder_repo(self) -> SqliteReminderRepository:
        """Thread-safe in-memory repo for API tests."""
        return _thread_safe_repo()

    @pytest.fixture
    def api_client(self, thread_safe_reminder_repo: SqliteReminderRepository):
        """FastAPI TestClient wired up with a schedule-aware service."""
        import sqlite3 as _sqlite3

        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from src.api import auth, routes
        from src.services.reminder_service import ReminderService

        sched_repo = SqliteScheduleRepository(":memory:")
        # Use same reminder repo for both service and schedule_service
        reminder_svc = ReminderService(thread_safe_reminder_repo)

        test_app = FastAPI()
        test_app.include_router(routes.router)

        TEST_TOKEN = "ac12-test-token"
        auth.register_token(TEST_TOKEN)
        routes.set_dependencies(service=reminder_svc, signal_available_fn=None)

        client = TestClient(test_app)
        client._auth_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        client._repo = thread_safe_reminder_repo

        yield client

        routes._reminder_service = None
        routes._signal_available_fn = None
        auth._valid_token_hashes.clear()

    def test_pending_list_shows_at_most_one_per_schedule(self, api_client) -> None:
        """AC-12: 3 active schedules each with 1 PENDING → response has exactly 3 reminders."""
        repo = api_client._repo
        headers = api_client._auth_headers

        # Inject 3 schedules with exactly 1 PENDING instance each
        for i in range(1, 4):
            _inject_reminder(repo, schedule_id=i, state=ReminderState.PENDING)

        response = api_client.get("/api/reminders?state=pending", headers=headers)

        assert response.status_code == 200
        body = response.json()
        # API returns {"count": N, "reminders": [...]}
        reminders = body.get("reminders", body) if isinstance(body, dict) else body
        assert len(reminders) == 3, (
            "AC-12: with 3 schedules each having 1 PENDING instance, "
            f"GET /api/reminders?state=pending should return 3 items. Got {len(reminders)}."
        )

    def test_pending_list_not_inflated_by_multiple_per_schedule(
        self, api_client
    ) -> None:
        """AC-12: the pending list must show at most 1 entry per schedule_id.

        Under the NEW spawn behaviour, spawn_reminders() produces at most 1
        PENDING per schedule.  This test verifies that the schedules we set up
        with 1 PENDING each appear as exactly 1 entry per schedule in the API
        response — i.e. the list is usable as a clean "what's next" dashboard.
        """
        repo = api_client._repo
        headers = api_client._auth_headers

        # 3 separate schedules, each with 1 PENDING (clean post-fix state)
        _inject_reminder(repo, schedule_id=100, state=ReminderState.PENDING, title="A")
        _inject_reminder(repo, schedule_id=101, state=ReminderState.PENDING, title="B")
        _inject_reminder(repo, schedule_id=102, state=ReminderState.PENDING, title="C")

        response = api_client.get("/api/reminders?state=pending", headers=headers)

        assert response.status_code == 200
        body = response.json()
        reminders = body.get("reminders", body) if isinstance(body, dict) else body

        assert len(reminders) == 3, (
            "AC-12: 3 schedules × 1 PENDING each = 3 total in pending list. "
            f"Got {len(reminders)}."
        )

        # Verify at most 1 per schedule_id — the key AC-12 property
        from collections import Counter

        counts: Counter = Counter(
            r["schedule_id"]
            for r in reminders
            if isinstance(r, dict) and r.get("schedule_id") is not None
        )
        for sid, count in counts.items():
            assert count == 1, (
                f"AC-12: schedule_id={sid} appears {count} times in pending list "
                "— expected exactly 1 per schedule (new spawn behaviour)."
            )


# ---------------------------------------------------------------------------
# AC-13: Deduplication still works as a secondary guard
# ---------------------------------------------------------------------------


class TestAC13DeduplicationSecondaryGuard:
    """_reminder_exists() time-based dedup still prevents double-create on rapid calls."""

    def test_double_spawn_creates_only_one(self) -> None:
        """AC-13: two spawn calls in quick succession create exactly 1 reminder."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Daily at 10:00",
            time_of_day="10:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # No active instances
        spawned1 = svc.spawn_reminders()
        spawned2 = svc.spawn_reminders()

        assert len(spawned1) + len(spawned2) <= 1 or len(spawned2) == 0, (
            "AC-13: two rapid spawn calls should not create 2 instances. "
            f"First call: {len(spawned1)}, second call: {len(spawned2)}."
        )

    def test_second_spawn_creates_zero(self) -> None:
        """AC-13: after first spawn creates 1, second spawn creates 0."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Daily at 10:00",
            time_of_day="10:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        spawned1 = svc.spawn_reminders()
        # First spawn may or may not produce an instance (depends on time window)
        # but regardless, second spawn must not create ADDITIONAL instances
        count_after_first = _count_active_for_schedule(reminder_repo, schedule.id)

        spawned2 = svc.spawn_reminders()

        count_after_second = _count_active_for_schedule(reminder_repo, schedule.id)

        assert count_after_second == count_after_first, (
            "AC-13: second spawn must not create additional instances. "
            f"Active count before={count_after_first}, after={count_after_second}."
        )
        assert len(spawned2) == 0, (
            f"AC-13: second spawn must return 0 new reminders. Got {len(spawned2)}."
        )


# ---------------------------------------------------------------------------
# REQ-7: spawn_reminders() creates at most ONE occurrence per schedule
# ---------------------------------------------------------------------------


class TestReq7AtMostOneOccurrencePerSchedule:
    """Even when the 48h window contains multiple matches, only 1 is materialised.

    This is the key behavioural change: _calculate_occurrences() may still
    return multiple, but spawn_reminders() must stop after creating the first
    (earliest) non-duplicate one per schedule.
    """

    def test_daily_schedule_spawns_at_most_one(self) -> None:
        """REQ-7: daily schedule spawns exactly 1 instance per spawn call."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Daily Pill",
            time_of_day="09:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        spawned = svc.spawn_reminders()

        assert len(spawned) <= 1, (
            "REQ-7: daily schedule must spawn at most 1 instance per spawn call. "
            f"The 48h window may contain 2-3 occurrences, but only 1 should be "
            f"materialised. Got {len(spawned)}."
        )

    def test_fresh_daily_schedule_spawns_exactly_one(self) -> None:
        """REQ-7: brand new daily schedule with no prior instances gets exactly 1."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Brand New Schedule",
            time_of_day="12:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        spawned = svc.spawn_reminders()

        # The spec says E-8: "next occurrence is materialised immediately"
        # and REQ-7: only the FIRST one
        assert len(spawned) == 1, (
            "REQ-7/E-8: brand new daily schedule should spawn exactly 1 instance "
            f"(the earliest occurrence). Got {len(spawned)}."
        )

    def test_spawned_reminder_is_earliest_occurrence(self) -> None:
        """REQ-7: when multiple occurrences in window, the EARLIEST is materialised."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Daily Earliest",
            time_of_day="01:00",  # Very early — ensures today's occurrence is in window
            recurrence="daily",
        )
        assert schedule.id is not None

        spawned = svc.spawn_reminders()

        # If 2 occurrences are in the 48h window (today + tomorrow), only 1 should be created
        assert len(spawned) <= 1, (
            "REQ-7: must materialise at most 1 occurrence even when 2 are in window."
        )


# ---------------------------------------------------------------------------
# E-8: Brand new schedule — spawns next occurrence immediately
# ---------------------------------------------------------------------------


class TestE8BrandNewSchedule:
    """E-8: a brand new schedule (no prior instances) gets its next occurrence."""

    def test_brand_new_schedule_gets_one_instance(self) -> None:
        """E-8: newly created schedule spawns its first instance on the next spawn call."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="New Schedule",
            time_of_day="10:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Confirm no reminders yet
        all_before = reminder_repo.list_all()
        assert len(all_before) == 0

        spawned = svc.spawn_reminders()

        assert len(spawned) == 1, (
            f"E-8: brand new schedule should materialise 1 instance. Got {len(spawned)}."
        )
        assert spawned[0].schedule_id == schedule.id

    def test_brand_new_schedule_instance_is_pending(self) -> None:
        """E-8: the newly spawned instance for a brand new schedule is PENDING."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="New Schedule",
            time_of_day="10:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        spawned = svc.spawn_reminders()

        assert len(spawned) == 1
        assert spawned[0].state == ReminderState.PENDING, (
            f"E-8: new instance should be PENDING, got {spawned[0].state}"
        )


# ---------------------------------------------------------------------------
# E-11: Acknowledge → immediate next spawn produces 1 instance
# ---------------------------------------------------------------------------


class TestE11AckThenSpawnProducesOne:
    """E-11: user acknowledges the only PENDING; next spawn produces 1 new instance."""

    def test_ack_then_spawn_produces_one(self) -> None:
        """E-11: spawn after acknowledgement of the only PENDING creates exactly 1."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Morning Pill",
            time_of_day="08:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # First spawn — creates 1
        spawned1 = svc.spawn_reminders()
        assert len(spawned1) == 1, "Setup: first spawn should create 1 instance"

        # Acknowledge it
        assert spawned1[0].id is not None
        reminder_repo.update_state(
            spawned1[0].id, ReminderState.ACKNOWLEDGED, ack_keyword="ack"
        )

        # Second spawn — no active instances, should create 1 new
        spawned2 = svc.spawn_reminders()

        assert len(spawned2) == 1, (
            "E-11: after acknowledging the only PENDING instance, "
            f"next spawn should create exactly 1 new instance. Got {len(spawned2)}."
        )
        # Must be a different reminder
        assert spawned2[0].id != spawned1[0].id


# ---------------------------------------------------------------------------
# E-12: Deactivated schedule — no new spawns
# ---------------------------------------------------------------------------


class TestE12DeactivatedScheduleNoNewSpawns:
    """E-12: deactivated schedule does not spawn new instances (existing PENDING remains)."""

    def test_deactivated_schedule_not_spawned(self) -> None:
        """E-12: inactive schedule skipped — no new instances created."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Deactivated Schedule",
            time_of_day="09:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        svc.deactivate(schedule.id)

        spawned = svc.spawn_reminders()

        assert len(spawned) == 0, (
            "E-12: deactivated schedule must not spawn any instances. "
            f"Got {len(spawned)}."
        )

    def test_existing_pending_preserved_after_deactivation(self) -> None:
        """E-12: PENDING instance created before deactivation is NOT removed by spawn."""
        _, reminder_repo, svc = _make_service()

        schedule = svc.create(
            title="Deactivated Schedule",
            time_of_day="09:00",
            recurrence="daily",
        )
        assert schedule.id is not None

        # Create a PENDING instance before deactivation
        existing = _inject_reminder(
            reminder_repo,
            schedule_id=schedule.id,
            state=ReminderState.PENDING,
        )

        svc.deactivate(schedule.id)

        svc.spawn_reminders()

        # Existing PENDING should still be there
        found = reminder_repo.get(existing.id)
        assert found is not None, (
            "E-12: the PENDING instance created before deactivation must persist."
        )
        assert found.state == ReminderState.PENDING, (
            f"E-12: the PENDING instance state must be unchanged, got {found.state}."
        )
