"""Tests for the MeetingService (DRY business logic layer)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.models.meeting import MeetingState
from src.services.meeting_service import (
    DuplicateMeetingError,
    MeetingNotFoundError,
    MeetingService,
    PastMeetingError,
)
from src.services.state_machine import InvalidTransitionError


def _future(hours: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class TestCreate:
    def test_create_meeting(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        assert m.id is not None
        assert m.title == "Test"
        assert m.state == MeetingState.PENDING

    def test_create_with_link(self, service: MeetingService) -> None:
        m = service.create(
            title="Test", starts_at=_future(), link="https://zoom.us/test"
        )
        assert m.link == "https://zoom.us/test"

    def test_reject_past_meeting(self, service: MeetingService) -> None:
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with pytest.raises(PastMeetingError):
            service.create(title="Past", starts_at=past)

    def test_reject_naive_datetime(self, service: MeetingService) -> None:
        naive = datetime.now() + timedelta(hours=1)
        with pytest.raises(ValueError, match="timezone-aware"):
            service.create(title="Naive", starts_at=naive)

    def test_reject_duplicate(self, service: MeetingService) -> None:
        t = _future()
        service.create(title="Meeting", starts_at=t)
        with pytest.raises(DuplicateMeetingError):
            service.create(title="Meeting", starts_at=t + timedelta(minutes=10))

    def test_allow_different_title(self, service: MeetingService) -> None:
        t = _future()
        service.create(title="Meeting A", starts_at=t)
        m = service.create(title="Meeting B", starts_at=t)
        assert m.id is not None

    def test_allow_same_title_different_time(self, service: MeetingService) -> None:
        service.create(title="Weekly", starts_at=_future(1))
        m = service.create(title="Weekly", starts_at=_future(24))
        assert m.id is not None


class TestGet:
    def test_get_existing(self, service: MeetingService) -> None:
        created = service.create(title="Test", starts_at=_future())
        fetched = service.get(created.id)  # type: ignore
        assert fetched.title == "Test"

    def test_get_nonexistent(self, service: MeetingService) -> None:
        with pytest.raises(MeetingNotFoundError):
            service.get(999)


class TestAcknowledge:
    def test_ack_pending(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        acked = service.acknowledge(m.id, "ack")  # type: ignore
        assert acked.state == MeetingState.ACKNOWLEDGED
        assert acked.ack_keyword == "ack"
        assert acked.ack_at is not None

    def test_ack_reminding(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.mark_reminding(m.id)  # type: ignore
        acked = service.acknowledge(m.id, "joining")  # type: ignore
        assert acked.state == MeetingState.ACKNOWLEDGED
        assert acked.ack_keyword == "joining"

    def test_ack_already_acknowledged(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.acknowledge(m.id, "ack")  # type: ignore
        with pytest.raises(InvalidTransitionError):
            service.acknowledge(m.id, "ack")  # type: ignore

    def test_ack_nonexistent(self, service: MeetingService) -> None:
        with pytest.raises(MeetingNotFoundError):
            service.acknowledge(999, "ack")


class TestSkip:
    def test_skip_pending(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        skipped = service.skip(m.id)  # type: ignore
        assert skipped.state == MeetingState.SKIPPED

    def test_skip_already_skipped(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.skip(m.id)  # type: ignore
        with pytest.raises(InvalidTransitionError):
            service.skip(m.id)  # type: ignore


class TestMarkMissed:
    def test_mark_missed(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        service.mark_reminding(m.id)  # type: ignore
        missed = service.mark_missed(m.id)  # type: ignore
        assert missed.state == MeetingState.MISSED

    def test_cannot_miss_pending(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        with pytest.raises(InvalidTransitionError):
            service.mark_missed(m.id)  # type: ignore


class TestDelete:
    def test_delete_existing(self, service: MeetingService) -> None:
        m = service.create(title="Test", starts_at=_future())
        assert service.delete(m.id) is True  # type: ignore

    def test_delete_nonexistent(self, service: MeetingService) -> None:
        assert service.delete(999) is False


class TestCounts:
    def test_count_pending(self, service: MeetingService) -> None:
        service.create(title="A", starts_at=_future())
        service.create(title="B", starts_at=_future(2))
        assert service.count_pending() == 2

    def test_count_reminding(self, service: MeetingService) -> None:
        m = service.create(title="A", starts_at=_future())
        service.mark_reminding(m.id)  # type: ignore
        assert service.count_reminding() == 1
