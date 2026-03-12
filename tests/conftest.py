"""Test fixtures for Klaxxon."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.config import EscalationProfile, EscalationStage
from src.models.reminder import Reminder, ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.repository.schedule_sqlite import SqliteScheduleRepository
from src.services.reminder_engine import ReminderEngine
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


# ---------------------------------------------------------------------------
# Housekeeping fixtures (for age-out-acknowledged feature)
# ---------------------------------------------------------------------------


@pytest.fixture
def housekeeping_repo() -> SqliteReminderRepository:
    """Thread-safe in-memory SQLite repository for housekeeping tests."""
    r = SqliteReminderRepository(":memory:")
    if r._conn:
        r._conn.close()
    r._conn = sqlite3.connect(":memory:", check_same_thread=False)
    r._conn.row_factory = sqlite3.Row
    r._conn.execute("PRAGMA journal_mode=WAL")
    r._conn.execute("PRAGMA foreign_keys=ON")
    r._ensure_schema()
    return r


@pytest.fixture
def housekeeping_service(housekeeping_repo: SqliteReminderRepository):
    """HousekeepingService wired to an in-memory repository with default 30-day retention.

    Lazily imports HousekeepingService so that collection succeeds even before
    the implementation file exists.
    """
    try:
        from src.services.housekeeping_service import HousekeepingService
    except ImportError:
        pytest.skip("src.services.housekeeping_service not yet implemented")
    return HousekeepingService(repository=housekeeping_repo, retention_days=30)


# ---------------------------------------------------------------------------
# Resend fixtures (for resend-notification feature)
# ---------------------------------------------------------------------------

#: Simple 1-stage escalation profile reused by resend fixtures.
_RESEND_TEST_PROFILE: dict[str, EscalationProfile] = {
    "meeting": EscalationProfile(
        stages=[
            EscalationStage(
                offset_hours=-1.0,
                interval_min=None,
                target="self",
                message="Reminder: {title} at {time}",
            )
        ],
        post_start_interval_min=2,
        post_start_target="self",
        post_start_message="Started: {title}",
        overflow=None,
        timeout_after_min=30,
    )
}


@pytest.fixture
def resend_engine_fixture(
    service: ReminderService,
    repo: SqliteReminderRepository,
    mock_sender: MockSender,
):
    """ReminderEngine with AckTokenService (base_url configured) for resend tests.

    Lazily imports AckTokenService so collection succeeds before implementation
    exists.  If AckTokenService is not yet implemented, engine is wired with
    ack_token_service=None and the fixture proceeds (a separate test will
    detect the missing import).
    """
    try:
        from src.services.ack_token_service import AckTokenService

        ack_service = AckTokenService(
            repository=repo,
            base_url="https://klaxxon.example.com",
        )
    except ImportError:
        ack_service = None

    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient="+441234567890",
        escalation_profiles=_RESEND_TEST_PROFILE,
        ack_token_service=ack_service,
    )


@pytest.fixture
def resend_engine_no_base_url_fixture(
    service: ReminderService,
    repo: SqliteReminderRepository,
    mock_sender: MockSender,
):
    """ReminderEngine with no AckTokenService (KLAXXON_BASE_URL not set)."""
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient="+441234567890",
        escalation_profiles=_RESEND_TEST_PROFILE,
        ack_token_service=None,
    )


@pytest.fixture
def resend_engine_failing_fixture(
    service: ReminderService,
    repo: SqliteReminderRepository,
):
    """ReminderEngine whose MessageSender always returns False."""
    try:
        from src.services.ack_token_service import AckTokenService

        ack_service = AckTokenService(
            repository=repo,
            base_url="https://klaxxon.example.com",
        )
    except ImportError:
        ack_service = None

    return ReminderEngine(
        service=service,
        repository=repo,
        sender=FailingSender(),
        recipient="+441234567890",
        escalation_profiles=_RESEND_TEST_PROFILE,
        ack_token_service=ack_service,
    )


def make_backdated_reminder(
    repo: SqliteReminderRepository,
    *,
    state: ReminderState,
    days_ago: float,
    title: str = "Backdated Reminder",
) -> Reminder:
    """Helper: create a reminder in the given terminal state with timestamps backdated.

    Useful for setting up age-out test scenarios in a single call.

    Args:
        repo:      Repository to insert the reminder into.
        state:     Target terminal state (ACKNOWLEDGED, SKIPPED, or MISSED).
        days_ago:  How many days ago the terminal timestamp should be set.
        title:     Optional title for the reminder.

    Returns:
        The created Reminder with its id populated.
    """
    starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
    reminder = repo.create(Reminder(title=title, starts_at=starts_at))
    assert reminder.id is not None

    past = datetime.now(timezone.utc) - timedelta(days=days_ago)

    if state == ReminderState.ACKNOWLEDGED:
        repo.update_state(
            reminder.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack", ack_at=past
        )
    elif state == ReminderState.SKIPPED:
        repo.update_state(reminder.id, ReminderState.SKIPPED)
    elif state == ReminderState.MISSED:
        repo.update_state(reminder.id, ReminderState.REMINDING)
        repo.update_state(reminder.id, ReminderState.MISSED)
    else:
        raise ValueError(f"make_backdated_reminder: unsupported state {state}")

    # Backdate updated_at (and ack_at for acknowledged) directly in SQLite
    conn = repo._get_conn()
    conn.execute(
        "UPDATE reminders SET updated_at = ? WHERE id = ?",
        (past.isoformat(), reminder.id),
    )
    if state == ReminderState.ACKNOWLEDGED:
        conn.execute(
            "UPDATE reminders SET ack_at = ? WHERE id = ?",
            (past.isoformat(), reminder.id),
        )
    conn.commit()

    # Re-fetch to get the updated timestamps
    refreshed = repo.get(reminder.id)
    assert refreshed is not None
    return refreshed
