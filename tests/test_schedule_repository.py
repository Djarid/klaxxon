"""Tests for SqliteScheduleRepository."""

from __future__ import annotations

from src.models.schedule import Schedule
from src.repository.schedule_sqlite import SqliteScheduleRepository


def test_create_schedule(schedule_repo: SqliteScheduleRepository) -> None:
    """Test basic schedule creation and retrieval."""
    schedule = Schedule(
        title="Daily standup",
        time_of_day="09:00",
        recurrence="daily",
    )
    created = schedule_repo.create(schedule)

    assert created.id is not None
    assert created.title == "Daily standup"
    assert created.time_of_day == "09:00"
    assert created.recurrence == "daily"
    assert created.is_active is True
    assert created.created_at is not None
    assert created.updated_at is not None

    # Retrieve it
    retrieved = schedule_repo.get(created.id)
    assert retrieved is not None
    assert retrieved.id == created.id
    assert retrieved.title == created.title


def test_create_schedule_with_all_fields(
    schedule_repo: SqliteScheduleRepository,
) -> None:
    """Test schedule creation with all fields populated."""
    schedule = Schedule(
        title="Weekly team meeting",
        description="Discuss progress and blockers",
        time_of_day="14:30",
        duration_min=60,
        link="https://meet.example.com/team",
        source="api",
        profile="meeting",
        escalate_to="+441234567890",
        recurrence="weekly",
        recurrence_rule="mon,wed,fri",
        is_active=True,
    )
    created = schedule_repo.create(schedule)

    assert created.id is not None
    assert created.title == "Weekly team meeting"
    assert created.description == "Discuss progress and blockers"
    assert created.time_of_day == "14:30"
    assert created.duration_min == 60
    assert created.link == "https://meet.example.com/team"
    assert created.source == "api"
    assert created.profile == "meeting"
    assert created.escalate_to == "+441234567890"
    assert created.recurrence == "weekly"
    assert created.recurrence_rule == "mon,wed,fri"
    assert created.is_active is True


def test_get_schedule_not_found(schedule_repo: SqliteScheduleRepository) -> None:
    """Test retrieving a non-existent schedule returns None."""
    result = schedule_repo.get(999)
    assert result is None


def test_list_schedules_active_only(schedule_repo: SqliteScheduleRepository) -> None:
    """Test listing schedules filters inactive ones."""
    # Create active schedule
    active = Schedule(title="Active", time_of_day="09:00", recurrence="daily")
    schedule_repo.create(active)

    # Create inactive schedule
    inactive = Schedule(
        title="Inactive", time_of_day="10:00", recurrence="daily", is_active=False
    )
    created_inactive = schedule_repo.create(inactive)

    # List active only
    schedules = schedule_repo.list(active_only=True)
    assert len(schedules) == 1
    assert schedules[0].title == "Active"

    # Deactivate the active one
    schedule_repo.deactivate(schedules[0].id)

    # Now list should be empty
    schedules = schedule_repo.list(active_only=True)
    assert len(schedules) == 0


def test_list_schedules_all(schedule_repo: SqliteScheduleRepository) -> None:
    """Test listing all schedules includes inactive ones."""
    # Create active schedule
    active = Schedule(title="Active", time_of_day="09:00", recurrence="daily")
    schedule_repo.create(active)

    # Create inactive schedule
    inactive = Schedule(
        title="Inactive", time_of_day="10:00", recurrence="daily", is_active=False
    )
    schedule_repo.create(inactive)

    # List all
    schedules = schedule_repo.list(active_only=False)
    assert len(schedules) == 2
    titles = {s.title for s in schedules}
    assert titles == {"Active", "Inactive"}


def test_update_fields(schedule_repo: SqliteScheduleRepository) -> None:
    """Test partial update of schedule fields."""
    schedule = Schedule(
        title="Original",
        time_of_day="09:00",
        recurrence="daily",
        description="Original description",
    )
    created = schedule_repo.create(schedule)

    # Update some fields
    updated = schedule_repo.update_fields(
        created.id,
        {"title": "Updated", "time_of_day": "10:30"},
    )

    assert updated is not None
    assert updated.id == created.id
    assert updated.title == "Updated"
    assert updated.time_of_day == "10:30"
    assert updated.description == "Original description"  # Unchanged
    assert updated.updated_at > created.updated_at


def test_deactivate(schedule_repo: SqliteScheduleRepository) -> None:
    """Test deactivating a schedule (soft delete)."""
    schedule = Schedule(title="Test", time_of_day="09:00", recurrence="daily")
    created = schedule_repo.create(schedule)

    # Deactivate
    success = schedule_repo.deactivate(created.id)
    assert success is True

    # Verify it's deactivated
    retrieved = schedule_repo.get(created.id)
    assert retrieved is not None
    assert retrieved.is_active is False


def test_deactivate_nonexistent(schedule_repo: SqliteScheduleRepository) -> None:
    """Test deactivating a non-existent schedule returns False."""
    success = schedule_repo.deactivate(999)
    assert success is False
