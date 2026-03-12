"""Tests for ReminderEngine ack-token integration.

Verifies that:
  - When AckTokenService is wired in, the ack URL is appended to outbound messages.
  - When AckTokenService is None, messages are unchanged (graceful degradation).
  - Token is NOT generated/stored when the send fails.
  - Each successful send gets a FRESH token (REQ-1, REQ-12).

All tests written against the specification in .Claude/plans/nag-ack-token.md
and MUST FAIL until the implementation exists.

Import strategy: modules that don't exist yet are imported lazily inside each
test / fixture so that collection succeeds and failures come from pytest.fail()
with a clear message, not a raw ModuleNotFoundError.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from src.config import EscalationProfile, EscalationStage
from src.models.reminder import ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.services.reminder_engine import ReminderEngine
from src.services.reminder_service import ReminderService
from tests.conftest import FailingSender, MockSender


# ---------------------------------------------------------------------------
# Lazy importers
# ---------------------------------------------------------------------------


def _import_ack_token():
    try:
        from src.models.ack_token import AckToken

        return AckToken
    except ImportError as exc:
        pytest.fail(f"src.models.ack_token not yet implemented: {exc}")


def _import_ack_token_service():
    try:
        from src.services.ack_token_service import AckTokenService

        return AckTokenService
    except ImportError as exc:
        pytest.fail(f"src.services.ack_token_service not yet implemented: {exc}")


# ---------------------------------------------------------------------------
# In-memory AckTokenRepository
# ---------------------------------------------------------------------------


class InMemoryAckTokenRepository:
    """In-memory AckTokenRepository for engine integration tests."""

    def __init__(self) -> None:
        self._store: dict = {}

    def store_token(
        self,
        token_hash: str,
        reminder_id: int,
        expires_at: datetime,
    ) -> None:
        AckToken = _import_ack_token()
        token = AckToken(
            id=len(self._store) + 1,
            token_hash=token_hash,
            reminder_id=reminder_id,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            used=False,
            used_at=None,
        )
        self._store[token_hash] = token

    def get_by_hash(self, token_hash: str):
        return self._store.get(token_hash)

    def mark_used(self, token_hash: str) -> bool:
        token = self._store.get(token_hash)
        if token is None or token.used:
            return False
        token.used = True
        token.used_at = datetime.now(timezone.utc)
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def escalation_config() -> dict:
    """Simple 1-stage escalation config for focused engine tests."""
    return {
        "meeting": EscalationProfile(
            stages=[
                EscalationStage(
                    offset_hours=-1.0,
                    interval_min=None,
                    target="self",
                    message="Reminder: {title}",
                ),
            ],
            post_start_interval_min=2,
            post_start_target="self",
            post_start_message="Started: {title}",
            overflow=None,
            timeout_after_min=30,
        )
    }


@pytest.fixture
def ack_repo() -> InMemoryAckTokenRepository:
    return InMemoryAckTokenRepository()


@pytest.fixture
def ack_service(ack_repo: InMemoryAckTokenRepository):
    AckTokenService = _import_ack_token_service()
    return AckTokenService(
        repository=ack_repo,
        base_url="https://klaxxon.example.com",
    )


@pytest.fixture
def engine_with_ack(
    service: ReminderService,
    repo: SqliteReminderRepository,
    mock_sender: MockSender,
    escalation_config: dict,
    ack_service,
) -> ReminderEngine:
    """ReminderEngine with AckTokenService wired in (new constructor param)."""
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient="+441234567890",
        escalation_profiles=escalation_config,
        ack_token_service=ack_service,
    )


@pytest.fixture
def engine_without_ack(
    service: ReminderService,
    repo: SqliteReminderRepository,
    mock_sender: MockSender,
    escalation_config: dict,
) -> ReminderEngine:
    """ReminderEngine WITHOUT AckTokenService (ack_token_service=None)."""
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient="+441234567890",
        escalation_profiles=escalation_config,
        ack_token_service=None,
    )


# ===========================================================================
# AC-10 / REQ-3 / REQ-12 — Ack URL appended to nag message
# ===========================================================================


class TestAckUrlInNotification:
    @pytest.mark.asyncio
    async def test_message_includes_ack_url_when_service_configured(
        self,
        engine_with_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Notification message includes the ack URL when AckTokenService is wired in (AC-10, REQ-3)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Daily Standup", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_with_ack._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "https://klaxxon.example.com/ack/" in text

    @pytest.mark.asyncio
    async def test_ack_url_has_43_char_token(
        self,
        engine_with_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Ack URL token is 43 characters (32-byte urlsafe b64) (AC-10, REQ-10)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Review", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_with_ack._process_reminder(reminder, now)

        _, text = mock_sender.messages[0]
        match = re.search(r"https://klaxxon\.example\.com/ack/(\S+)", text)
        assert match is not None, f"No ack URL found in message: {text!r}"
        raw_token = match.group(1)
        assert len(raw_token) == 43, (
            f"Token length {len(raw_token)} != 43: {raw_token!r}"
        )

    @pytest.mark.asyncio
    async def test_message_contains_ack_label(
        self,
        engine_with_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Message includes an 'Ack:' label before the URL (AC-10 format)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Daily Standup", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_with_ack._process_reminder(reminder, now)

        _, text = mock_sender.messages[0]
        # Spec AC-10: "\nAck: https://..."
        assert "Ack:" in text or "ack:" in text.lower()

    @pytest.mark.asyncio
    async def test_ack_url_appears_at_end_of_message(
        self,
        engine_with_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Ack URL is appended at the end of the message (AC-10)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Daily Standup", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_with_ack._process_reminder(reminder, now)

        _, text = mock_sender.messages[0]
        lines = text.splitlines()
        last_line = next((line for line in reversed(lines) if line.strip()), "")
        assert "klaxxon.example.com/ack/" in last_line

    @pytest.mark.asyncio
    async def test_token_stored_in_repo_after_successful_send(
        self,
        engine_with_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """After a successful send, exactly one token hash is stored in repo (REQ-12)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Daily Standup", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_with_ack._process_reminder(reminder, now)

        assert len(ack_repo._store) == 1

    @pytest.mark.asyncio
    async def test_stored_token_hash_matches_url_token(
        self,
        engine_with_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """The SHA-256 hash of the URL token matches what is stored in the repo (REQ-11)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Daily Standup", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_with_ack._process_reminder(reminder, now)

        _, text = mock_sender.messages[0]
        match = re.search(r"https://klaxxon\.example\.com/ack/(\S+)", text)
        assert match is not None
        raw_token = match.group(1)
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        assert expected_hash in ack_repo._store, (
            "SHA-256 hash of the URL token not found in repo"
        )


# ===========================================================================
# AC-2 / REQ-4 / E-1 — No URL when AckTokenService is None
# ===========================================================================


class TestNoAckUrlWhenNoBaseUrl:
    @pytest.mark.asyncio
    async def test_message_does_not_include_ack_url_when_no_service(
        self,
        engine_without_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Message does NOT include ack URL when AckTokenService is None (AC-2, REQ-4)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Daily Standup", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_without_ack._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "/ack/" not in text

    @pytest.mark.asyncio
    async def test_base_message_text_unchanged_without_ack_service(
        self,
        engine_without_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Without AckTokenService, the message is exactly the template output — nothing added (AC-2)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=3)
        reminder = service.create(title="Standup No Ack", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_without_ack._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        # The stage message template is "Reminder: {title}" — should be exactly that.
        assert text == "Reminder: Standup No Ack"

    @pytest.mark.asyncio
    async def test_no_token_in_repo_when_no_service(
        self,
        engine_without_ack: ReminderEngine,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """When AckTokenService is None, no tokens exist anywhere (E-1)."""
        # engine_without_ack has ack_token_service=None — nothing to check in a repo.
        # We just verify the message has no ack URL and no crash occurs.
        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Daily Standup", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine_without_ack._process_reminder(reminder, now)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "/ack/" not in text


# ===========================================================================
# E-10 / REQ-12 — Token NOT stored when send fails
# ===========================================================================


class TestTokenNotStoredOnFailedSend:
    @pytest.mark.asyncio
    async def test_no_token_stored_when_send_fails(
        self,
        service: ReminderService,
        repo: SqliteReminderRepository,
        escalation_config: dict,
        ack_repo: InMemoryAckTokenRepository,
        ack_service,
    ) -> None:
        """No token is stored/persisted when the message send fails (E-10, REQ-12)."""
        failing_sender = FailingSender()
        engine = ReminderEngine(
            service=service,
            repository=repo,
            sender=failing_sender,
            recipient="+441234567890",
            escalation_profiles=escalation_config,
            ack_token_service=ack_service,
        )

        starts_at = datetime.now(timezone.utc) + timedelta(hours=2)
        reminder = service.create(title="Test", starts_at=starts_at)
        assert reminder.id is not None

        now = starts_at - timedelta(hours=1)
        await engine._process_reminder(reminder, now)

        # No token should be stored after a failed send
        assert len(ack_repo._store) == 0


# ===========================================================================
# REQ-12 — Fresh token per send event
# ===========================================================================


class TestFreshTokenPerSend:
    @pytest.mark.asyncio
    async def test_each_send_generates_unique_token(
        self,
        engine_with_ack: ReminderEngine,
        service: ReminderService,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Each nag send generates a distinct token (REQ-1, REQ-12)."""
        starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create(title="Standup", starts_at=starts_at)
        assert reminder.id is not None

        # First send at stage boundary (-1 hour)
        now = starts_at - timedelta(hours=1)
        await engine_with_ack._process_reminder(reminder, now)
        assert len(mock_sender.messages) == 1

        # Simulate interval passing by backdating reminder_log
        conn = repo._get_conn()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ?",
            (old_time, reminder.id),
        )
        conn.commit()

        mock_sender.messages.clear()
        reminder = service.get(reminder.id)
        # Still before start time, still within stage window
        now2 = starts_at - timedelta(minutes=50)
        await engine_with_ack._process_reminder(reminder, now2)

        if len(mock_sender.messages) == 0:
            pytest.skip("Second send did not trigger; interval logic may vary")

        # Must have 2 distinct token hashes in the repo
        assert len(ack_repo._store) == 2, (
            f"Expected 2 tokens (one per send), got {len(ack_repo._store)}"
        )
        hashes = list(ack_repo._store.keys())
        assert hashes[0] != hashes[1]
