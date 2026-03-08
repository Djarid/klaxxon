"""Test fixtures for Klaxxon."""

from __future__ import annotations

import pytest

from src.models.reminder import Reminder, ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.repository.schedule_sqlite import SqliteScheduleRepository
from src.services.reminder_service import ReminderService
from src.services.schedule_service import ScheduleService
from src.services.notification.base import (
    IncomingMessage,
    MessageReceiver,
    MessageSender,
)


class MockSender(MessageSender):
    """Mock message sender that records sent messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_message(self, recipient: str, text: str) -> bool:
        self.messages.append((recipient, text))
        return True


class MockReceiver(MessageReceiver):
    """Mock message receiver with configurable responses."""

    def __init__(self) -> None:
        self.queued: list[IncomingMessage] = []

    async def receive_messages(self) -> list[IncomingMessage]:
        msgs = list(self.queued)
        self.queued.clear()
        return msgs


class FailingSender(MessageSender):
    """Mock sender that always fails."""

    async def send_message(self, recipient: str, text: str) -> bool:
        return False


@pytest.fixture
def repo() -> SqliteReminderRepository:
    """In-memory SQLite repository."""
    return SqliteReminderRepository(":memory:")


@pytest.fixture
def service(repo: SqliteReminderRepository) -> ReminderService:
    """Reminder service with in-memory repo."""
    return ReminderService(repo)


@pytest.fixture
def mock_sender() -> MockSender:
    return MockSender()


@pytest.fixture
def mock_receiver() -> MockReceiver:
    return MockReceiver()


@pytest.fixture
def schedule_repo() -> SqliteScheduleRepository:
    """In-memory SQLite schedule repository."""
    return SqliteScheduleRepository(":memory:")


@pytest.fixture
def schedule_service(
    schedule_repo: SqliteScheduleRepository, repo: SqliteReminderRepository
) -> ScheduleService:
    """Schedule service with in-memory repos."""
    return ScheduleService(
        schedule_repo=schedule_repo,
        reminder_repo=repo,
        timezone_name="Europe/London",
    )
