"""Tests for the ReminderService (DRY business logic layer)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.models.reminder import ReminderState
from src.services.reminder_service import (
    DuplicateReminderError,
    ReminderNotFoundError,
    ReminderService,
    PastReminderError,
)
from src.services.state_machine import InvalidTransitionError


def _future(hours: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class TestCreate:
    def test_create_meeting(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        assert m.id is not None
        assert m.title == "Test"
        assert m.state == ReminderState.PENDING

    def test_create_with_link(self, service: ReminderService) -> None:
        m = service.create(
            title="Test", starts_at=_future(), link="https://zoom.us/test"
        )
        assert m.link == "https://zoom.us/test"

    def test_reject_past_meeting(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with pytest.raises(PastReminderError):
            service.create(title="Past", starts_at=past)

    def test_reject_naive_datetime(self, service: ReminderService) -> None:
        naive = datetime.now() + timedelta(hours=1)
        with pytest.raises(ValueError, match="timezone-aware"):
            service.create(title="Naive", starts_at=naive)

    def test_reject_duplicate(self, service: ReminderService) -> None:
        t = _future()
        service.create(title="Reminder", starts_at=t)
        with pytest.raises(DuplicateReminderError):
            service.create(title="Reminder", starts_at=t + timedelta(minutes=10))

    def test_allow_different_title(self, service: ReminderService) -> None:
        t = _future()
        service.create(title="Reminder A", starts_at=t)
        m = service.create(title="Reminder B", starts_at=t)
        assert m.id is not None

    def test_allow_same_title_different_time(self, service: ReminderService) -> None:
        service.create(title="Weekly", starts_at=_future(1))
        m = service.create(title="Weekly", starts_at=_future(24))
        assert m.id is not None


class TestGet:
    def test_get_existing(self, service: ReminderService) -> None:
        created = service.create(title="Test", starts_at=_future())
        fetched = service.get(created.id)  # type: ignore
        assert fetched.title == "Test"

    def test_get_nonexistent(self, service: ReminderService) -> None:
        with pytest.raises(ReminderNotFoundError):
            service.get(999)


class TestAcknowledge:
    def test_ack_pending(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        acked = service.acknowledge(m.id, "ack")  # type: ignore
        assert acked.state == ReminderState.ACKNOWLEDGED
        assert acked.ack_keyword == "ack"
        assert acked.ack_at is not None

    def test_ack_reminding(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.mark_reminding(m.id)  # type: ignore
        acked = service.acknowledge(m.id, "joining")  # type: ignore
        assert acked.state == ReminderState.ACKNOWLEDGED
        assert acked.ack_keyword == "joining"

    def test_ack_already_acknowledged(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.acknowledge(m.id, "ack")  # type: ignore
        with pytest.raises(InvalidTransitionError):
            service.acknowledge(m.id, "ack")  # type: ignore

    def test_ack_nonexistent(self, service: ReminderService) -> None:
        with pytest.raises(ReminderNotFoundError):
            service.acknowledge(999, "ack")


class TestSkip:
    def test_skip_pending(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        skipped = service.skip(m.id)  # type: ignore
        assert skipped.state == ReminderState.SKIPPED

    def test_skip_already_skipped(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.skip(m.id)  # type: ignore
        with pytest.raises(InvalidTransitionError):
            service.skip(m.id)  # type: ignore


class TestMarkMissed:
    def test_mark_missed(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.mark_reminding(m.id)  # type: ignore
        missed = service.mark_missed(m.id)  # type: ignore
        assert missed.state == ReminderState.MISSED

    def test_cannot_miss_pending(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        with pytest.raises(InvalidTransitionError):
            service.mark_missed(m.id)  # type: ignore


class TestDelete:
    def test_delete_existing(self, service: ReminderService) -> None:
        m = service.create(title="Test", starts_at=_future())
        assert service.delete(m.id) is True  # type: ignore

    def test_delete_nonexistent(self, service: ReminderService) -> None:
        assert service.delete(999) is False


class TestCounts:
    def test_count_pending(self, service: ReminderService) -> None:
        service.create(title="A", starts_at=_future())
        service.create(title="B", starts_at=_future(2))
        assert service.count_pending() == 2

    def test_count_reminding(self, service: ReminderService) -> None:
        m = service.create(title="A", starts_at=_future())
        service.mark_reminding(m.id)  # type: ignore
        assert service.count_reminding() == 1
