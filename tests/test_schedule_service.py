"""Tests for ScheduleService."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models.reminder import ReminderState
from src.models.schedule import Schedule
from src.services.schedule_service import (
    ScheduleNotFoundError,
    ScheduleService,
    ScheduleValidationError,
)


def test_create_daily_schedule(schedule_service: ScheduleService) -> None:
    """Test creating a basic daily schedule."""
    schedule = schedule_service.create(
        title="Daily standup",
        time_of_day="09:00",
        recurrence="daily",
    )

    assert schedule.id is not None
    assert schedule.title == "Daily standup"
    assert schedule.time_of_day == "09:00"
    assert schedule.recurrence == "daily"
    assert schedule.is_active is True


def test_create_weekly_schedule(schedule_service: ScheduleService) -> None:
    """Test creating a weekly schedule with recurrence_rule."""
    schedule = schedule_service.create(
        title="Team meeting",
        time_of_day="14:00",
        recurrence="weekly",
        recurrence_rule="mon,wed,fri",
    )

    assert schedule.id is not None
    assert schedule.recurrence == "weekly"
    assert schedule.recurrence_rule == "mon,wed,fri"


def test_create_schedule_invalid_time_format(schedule_service: ScheduleService) -> None:
    """Test validation error for invalid time_of_day format."""
    with pytest.raises(ScheduleValidationError, match="HH:MM format"):
        schedule_service.create(
            title="Test",
            time_of_day="9:00",  # Missing leading zero
            recurrence="daily",
        )

    with pytest.raises(ScheduleValidationError, match="HH:MM format"):
        schedule_service.create(
            title="Test",
            time_of_day="09:00:00",  # Too many parts
            recurrence="daily",
        )


def test_create_weekly_without_rule_fails(schedule_service: ScheduleService) -> None:
    """Test validation error when weekly/custom recurrence lacks recurrence_rule."""
    with pytest.raises(ScheduleValidationError, match="recurrence_rule is required"):
        schedule_service.create(
            title="Test",
            time_of_day="09:00",
            recurrence="weekly",
            recurrence_rule=None,
        )

    with pytest.raises(ScheduleValidationError, match="recurrence_rule is required"):
        schedule_service.create(
            title="Test",
            time_of_day="09:00",
            recurrence="custom",
            recurrence_rule="",
        )


def test_create_schedule_invalid_recurrence_rule(
    schedule_service: ScheduleService,
) -> None:
    """Test validation error for invalid day abbreviations in recurrence_rule."""
    with pytest.raises(ScheduleValidationError, match="Invalid day abbreviations"):
        schedule_service.create(
            title="Test",
            time_of_day="09:00",
            recurrence="weekly",
            recurrence_rule="monday,tuesday",  # Full names not allowed
        )


def test_spawn_daily_reminders(schedule_service: ScheduleService) -> None:
    """Test spawning reminders for a daily schedule within 48h window."""
    # Create a daily schedule at 10:00 local time
    schedule = schedule_service.create(
        title="Daily reminder",
        time_of_day="10:00",
        recurrence="daily",
        description="Test daily",
    )

    # Spawn reminders
    spawned = schedule_service.spawn_reminders()

    # Should create reminders for today and tomorrow (within 48h window)
    # Exact count depends on current time, but should be at least 1
    assert len(spawned) >= 1
    assert len(spawned) <= 3  # Today, tomorrow, day after

    # Check first reminder
    reminder = spawned[0]
    assert reminder.title == "Daily reminder"
    assert reminder.description == "Test daily"
    assert reminder.schedule_id == schedule.id
    assert reminder.state == ReminderState.PENDING


def test_spawn_weekly_reminders(schedule_service: ScheduleService) -> None:
    """Test spawning reminders for a weekly schedule (only on matching days)."""
    # Create a weekly schedule for Monday only
    schedule = schedule_service.create(
        title="Monday meeting",
        time_of_day="09:00",
        recurrence="weekly",
        recurrence_rule="mon",
    )

    # Spawn reminders
    spawned = schedule_service.spawn_reminders()

    # Should only create reminders for Mondays within 48h window
    # Depending on current day, might be 0 or 1
    assert len(spawned) <= 1

    # If any spawned, check it's a Monday
    if spawned:
        reminder = spawned[0]
        assert reminder.starts_at is not None
        # Convert to local time to check day of week
        local_dt = reminder.starts_at.astimezone(schedule_service._timezone)
        # 0 = Monday
        assert local_dt.weekday() == 0


def test_spawn_no_duplicates(schedule_service: ScheduleService) -> None:
    """Test that spawning doesn't recreate existing reminders."""
    # Create a daily schedule
    schedule = schedule_service.create(
        title="Daily reminder",
        time_of_day="10:00",
        recurrence="daily",
    )

    # Spawn reminders first time
    spawned1 = schedule_service.spawn_reminders()
    count1 = len(spawned1)
    assert count1 >= 1

    # Spawn again immediately
    spawned2 = schedule_service.spawn_reminders()

    # Should not create duplicates
    assert len(spawned2) == 0


def test_spawn_inactive_schedule_skipped(schedule_service: ScheduleService) -> None:
    """Test that inactive schedules are skipped during spawning."""
    # Create an inactive schedule
    schedule = schedule_service.create(
        title="Inactive",
        time_of_day="10:00",
        recurrence="daily",
    )

    # Deactivate it
    schedule_service.deactivate(schedule.id)

    # Spawn reminders
    spawned = schedule_service.spawn_reminders()

    # Should not spawn from inactive schedule
    assert len(spawned) == 0


def test_spawn_inherits_schedule_fields(schedule_service: ScheduleService) -> None:
    """Test that spawned reminders inherit fields from schedule."""
    # Create a schedule with all fields
    schedule = schedule_service.create(
        title="Team meeting",
        description="Weekly sync",
        time_of_day="14:00",
        duration_min=60,
        link="https://meet.example.com/team",
        profile="meeting",
        escalate_to="+441234567890",
        recurrence="daily",
        source="api",
    )

    # Spawn reminders
    spawned = schedule_service.spawn_reminders()
    assert len(spawned) >= 1

    # Check inherited fields
    reminder = spawned[0]
    assert reminder.title == "Team meeting"
    assert reminder.description == "Weekly sync"
    assert reminder.duration_min == 60
    assert reminder.link == "https://meet.example.com/team"
    assert reminder.profile == "meeting"
    assert reminder.escalate_to == "+441234567890"
    assert reminder.source == "api"
    assert reminder.schedule_id == schedule.id


def test_deactivate_schedule(schedule_service: ScheduleService) -> None:
    """Test deactivating a schedule (soft delete)."""
    schedule = schedule_service.create(
        title="Test",
        time_of_day="09:00",
        recurrence="daily",
    )

    # Deactivate
    success = schedule_service.deactivate(schedule.id)
    assert success is True

    # Verify it's not in active list
    active_schedules = schedule_service.list(active_only=True)
    assert len(active_schedules) == 0

    # But it's in the full list
    all_schedules = schedule_service.list(active_only=False)
    assert len(all_schedules) == 1
    assert all_schedules[0].is_active is False


def test_update_schedule(schedule_service: ScheduleService) -> None:
    """Test updating schedule fields."""
    schedule = schedule_service.create(
        title="Original",
        time_of_day="09:00",
        recurrence="daily",
    )

    # Update
    updated = schedule_service.update(
        schedule.id,
        title="Updated",
        time_of_day="10:30",
    )

    assert updated.id == schedule.id
    assert updated.title == "Updated"
    assert updated.time_of_day == "10:30"


def test_update_schedule_not_found(schedule_service: ScheduleService) -> None:
    """Test updating a non-existent schedule raises error."""
    with pytest.raises(ScheduleNotFoundError):
        schedule_service.update(999, title="Test")


def test_spawn_handles_timezone(schedule_service: ScheduleService) -> None:
    """Test that spawning correctly converts local time to UTC."""
    # Create a schedule at 09:00 local time
    schedule = schedule_service.create(
        title="Morning reminder",
        time_of_day="09:00",
        recurrence="daily",
    )

    # Spawn reminders
    spawned = schedule_service.spawn_reminders()
    assert len(spawned) >= 1

    # Check that starts_at is in UTC
    reminder = spawned[0]
    assert reminder.starts_at is not None
    assert reminder.starts_at.tzinfo == timezone.utc

    # Convert back to local time and check it's 09:00
    local_dt = reminder.starts_at.astimezone(schedule_service._timezone)
    assert local_dt.hour == 9
    assert local_dt.minute == 0


def test_spawn_inherits_timing_overrides(schedule_service: ScheduleService) -> None:
    """Test that spawned reminders inherit lead_time_min and nag_interval_min from schedule."""
    schedule = schedule_service.create(
        title="Custom timing schedule",
        time_of_day="10:00",
        recurrence="daily",
        lead_time_min=15,
        nag_interval_min=2,
    )

    # Spawn reminders
    spawned = schedule_service.spawn_reminders()
    assert len(spawned) >= 1

    # Check that timing overrides are inherited
    reminder = spawned[0]
    assert reminder.lead_time_min == 15
    assert reminder.nag_interval_min == 2


def test_create_schedule_with_timing_overrides(
    schedule_service: ScheduleService,
) -> None:
    """Test creating a schedule with lead_time_min and nag_interval_min."""
    schedule = schedule_service.create(
        title="Custom schedule",
        time_of_day="14:00",
        recurrence="daily",
        lead_time_min=20,
        nag_interval_min=5,
    )

    assert schedule.id is not None
    assert schedule.lead_time_min == 20
    assert schedule.nag_interval_min == 5

    # Verify persistence
    fetched = schedule_service.get(schedule.id)
    assert fetched.lead_time_min == 20
    assert fetched.nag_interval_min == 5
