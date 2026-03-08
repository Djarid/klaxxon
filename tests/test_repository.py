"""Tests for SQLite meeting repository."""

from datetime import datetime, timedelta, timezone

import pytest

from src.models.meeting import Meeting, MeetingState
from src.repository.sqlite import SqliteMeetingRepository


def _future(hours: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def test_create_and_get(repo: SqliteMeetingRepository) -> None:
    meeting = Meeting(title="Test", starts_at=_future(), link="https://zoom.us/test")
    created = repo.create(meeting)
    assert created.id is not None
    assert created.created_at is not None

    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.title == "Test"
    assert fetched.link == "https://zoom.us/test"


def test_get_nonexistent(repo: SqliteMeetingRepository) -> None:
    assert repo.get(999) is None


def test_list_all(repo: SqliteMeetingRepository) -> None:
    repo.create(Meeting(title="A", starts_at=_future(1)))
    repo.create(Meeting(title="B", starts_at=_future(2)))
    meetings = repo.list_all()
    assert len(meetings) == 2


def test_list_all_by_state(repo: SqliteMeetingRepository) -> None:
    repo.create(Meeting(title="A", starts_at=_future(1), state=MeetingState.PENDING))
    repo.create(Meeting(title="B", starts_at=_future(2), state=MeetingState.REMINDING))
    pending = repo.list_all(state=MeetingState.PENDING)
    assert len(pending) == 1
    assert pending[0].title == "A"


def test_list_upcoming_by_states(repo: SqliteMeetingRepository) -> None:
    repo.create(Meeting(title="A", starts_at=_future(1), state=MeetingState.PENDING))
    repo.create(
        Meeting(title="B", starts_at=_future(2), state=MeetingState.ACKNOWLEDGED)
    )
    upcoming = repo.list_upcoming(states=[MeetingState.PENDING, MeetingState.REMINDING])
    assert len(upcoming) == 1
    assert upcoming[0].title == "A"


def test_update_state(repo: SqliteMeetingRepository) -> None:
    created = repo.create(Meeting(title="Test", starts_at=_future()))
    assert created.id is not None
    now = datetime.now(timezone.utc)
    updated = repo.update_state(
        created.id, MeetingState.ACKNOWLEDGED, ack_keyword="ack", ack_at=now
    )
    assert updated is not None
    assert updated.state == MeetingState.ACKNOWLEDGED
    assert updated.ack_keyword == "ack"
    assert updated.ack_at is not None


def test_delete(repo: SqliteMeetingRepository) -> None:
    created = repo.create(Meeting(title="Test", starts_at=_future()))
    assert created.id is not None
    assert repo.delete(created.id) is True
    assert repo.get(created.id) is None


def test_delete_nonexistent(repo: SqliteMeetingRepository) -> None:
    assert repo.delete(999) is False


def test_count_by_state(repo: SqliteMeetingRepository) -> None:
    repo.create(Meeting(title="A", starts_at=_future(), state=MeetingState.PENDING))
    repo.create(Meeting(title="B", starts_at=_future(), state=MeetingState.PENDING))
    repo.create(Meeting(title="C", starts_at=_future(), state=MeetingState.REMINDING))
    assert repo.count_by_state(MeetingState.PENDING) == 2
    assert repo.count_by_state(MeetingState.REMINDING) == 1
    assert repo.count_by_state(MeetingState.ACKNOWLEDGED) == 0


def test_log_reminder(repo: SqliteMeetingRepository) -> None:
    created = repo.create(Meeting(title="Test", starts_at=_future()))
    assert created.id is not None
    repo.log_reminder(created.id, "Test reminder")
    last = repo.get_last_reminder_time(created.id)
    assert last is not None


def test_get_last_reminder_time_none(repo: SqliteMeetingRepository) -> None:
    created = repo.create(Meeting(title="Test", starts_at=_future()))
    assert created.id is not None
    assert repo.get_last_reminder_time(created.id) is None
