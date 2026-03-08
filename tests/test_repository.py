"""Tests for SQLite reminder repository."""

from datetime import datetime, timedelta, timezone

import pytest

from src.models.reminder import Reminder, ReminderState
from src.repository.sqlite import SqliteReminderRepository


def _future(hours: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def test_create_and_get(repo: SqliteReminderRepository) -> None:
    reminder = Reminder(title="Test", starts_at=_future(), link="https://zoom.us/test")
    created = repo.create(reminder)
    assert created.id is not None
    assert created.created_at is not None

    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.title == "Test"
    assert fetched.link == "https://zoom.us/test"


def test_get_nonexistent(repo: SqliteReminderRepository) -> None:
    assert repo.get(999) is None


def test_list_all(repo: SqliteReminderRepository) -> None:
    repo.create(Reminder(title="A", starts_at=_future(1)))
    repo.create(Reminder(title="B", starts_at=_future(2)))
    reminders = repo.list_all()
    assert len(reminders) == 2


def test_list_all_by_state(repo: SqliteReminderRepository) -> None:
    repo.create(Reminder(title="A", starts_at=_future(1), state=ReminderState.PENDING))
    repo.create(
        Reminder(title="B", starts_at=_future(2), state=ReminderState.REMINDING)
    )
    pending = repo.list_all(state=ReminderState.PENDING)
    assert len(pending) == 1
    assert pending[0].title == "A"


def test_list_upcoming_by_states(repo: SqliteReminderRepository) -> None:
    repo.create(Reminder(title="A", starts_at=_future(1), state=ReminderState.PENDING))
    repo.create(
        Reminder(title="B", starts_at=_future(2), state=ReminderState.ACKNOWLEDGED)
    )
    upcoming = repo.list_upcoming(
        states=[ReminderState.PENDING, ReminderState.REMINDING]
    )
    assert len(upcoming) == 1
    assert upcoming[0].title == "A"


def test_update_state(repo: SqliteReminderRepository) -> None:
    created = repo.create(Reminder(title="Test", starts_at=_future()))
    assert created.id is not None
    now = datetime.now(timezone.utc)
    updated = repo.update_state(
        created.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack", ack_at=now
    )
    assert updated is not None
    assert updated.state == ReminderState.ACKNOWLEDGED
    assert updated.ack_keyword == "ack"
    assert updated.ack_at is not None


def test_delete(repo: SqliteReminderRepository) -> None:
    created = repo.create(Reminder(title="Test", starts_at=_future()))
    assert created.id is not None
    assert repo.delete(created.id) is True
    assert repo.get(created.id) is None


def test_delete_nonexistent(repo: SqliteReminderRepository) -> None:
    assert repo.delete(999) is False


def test_count_by_state(repo: SqliteReminderRepository) -> None:
    repo.create(Reminder(title="A", starts_at=_future(), state=ReminderState.PENDING))
    repo.create(Reminder(title="B", starts_at=_future(), state=ReminderState.PENDING))
    repo.create(Reminder(title="C", starts_at=_future(), state=ReminderState.REMINDING))
    assert repo.count_by_state(ReminderState.PENDING) == 2
    assert repo.count_by_state(ReminderState.REMINDING) == 1
    assert repo.count_by_state(ReminderState.ACKNOWLEDGED) == 0


def test_log_reminder(repo: SqliteReminderRepository) -> None:
    created = repo.create(Reminder(title="Test", starts_at=_future()))
    assert created.id is not None
    repo.log_reminder(created.id, "Test reminder")
    last = repo.get_last_reminder_time(created.id)
    assert last is not None


def test_get_last_reminder_time_none(repo: SqliteReminderRepository) -> None:
    created = repo.create(Reminder(title="Test", starts_at=_future()))
    assert created.id is not None
    assert repo.get_last_reminder_time(created.id) is None


def test_create_reminder_with_description(repo: SqliteReminderRepository) -> None:
    """Test creating a reminder with a description field."""
    reminder = Reminder(
        title="Team Meeting",
        description="Discuss Q2 roadmap and budget allocation",
        starts_at=_future(),
        link="https://zoom.us/j/123",
    )
    created = repo.create(reminder)
    assert created.id is not None
    assert created.description == "Discuss Q2 roadmap and budget allocation"

    # Verify it persists
    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.description == "Discuss Q2 roadmap and budget allocation"


def test_create_reminder_without_description(repo: SqliteReminderRepository) -> None:
    """Test creating a reminder without a description defaults to None."""
    reminder = Reminder(title="Quick Standup", starts_at=_future())
    created = repo.create(reminder)
    assert created.id is not None
    assert created.description is None

    # Verify it persists as None
    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.description is None


def test_create_reminder_with_profile_and_escalate_to(
    repo: SqliteReminderRepository,
) -> None:
    """Test creating a reminder with profile and escalate_to fields."""
    reminder = Reminder(
        title="Important Meeting",
        starts_at=_future(),
        profile="persistent",
        escalate_to="+447700900123",
    )
    created = repo.create(reminder)
    assert created.id is not None
    assert created.profile == "persistent"
    assert created.escalate_to == "+447700900123"

    # Verify persistence
    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.profile == "persistent"
    assert fetched.escalate_to == "+447700900123"


def test_create_reminder_default_profile(repo: SqliteReminderRepository) -> None:
    """Test creating a reminder without profile defaults to 'meeting'."""
    reminder = Reminder(title="Test", starts_at=_future())
    created = repo.create(reminder)
    assert created.id is not None
    assert created.profile == "meeting"
    assert created.escalate_to is None

    # Verify persistence
    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.profile == "meeting"
    assert fetched.escalate_to is None
