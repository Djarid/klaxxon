"""Test fixtures for Klaxxon."""

from __future__ import annotations

import pytest

from src.models.meeting import Meeting, MeetingState
from src.repository.sqlite import SqliteMeetingRepository
from src.services.meeting_service import MeetingService
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
def repo() -> SqliteMeetingRepository:
    """In-memory SQLite repository."""
    return SqliteMeetingRepository(":memory:")


@pytest.fixture
def service(repo: SqliteMeetingRepository) -> MeetingService:
    """Meeting service with in-memory repo."""
    return MeetingService(repo)


@pytest.fixture
def mock_sender() -> MockSender:
    return MockSender()


@pytest.fixture
def mock_receiver() -> MockReceiver:
    return MockReceiver()
