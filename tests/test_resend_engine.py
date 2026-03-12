"""Unit tests for ReminderEngine.resend_notification().

Tests every acceptance criterion that involves engine-level logic:

  AC-1/REQ-7   — send_message called with owner's number
  AC-1/REQ-8   — message contains [RESEND], title, starts_at time, ack URL
  AC-4/E-1     — ResendNotEligibleError raised for PENDING state
  AC-5/E-2     — ResendNotEligibleError raised for SKIPPED state
  AC-6/REQ-2   — ReminderNotFoundError raised for non-existent id
  AC-8/REQ-9   — ResendCooldownError raised when within 60-second cooldown
  AC-9/E-9     — No error when cooldown has just expired (>= 60 s)
  AC-10/REQ-5  — Each resend produces a distinct ack token; old tokens remain
  AC-11/REQ-12 — Token NOT committed when send_message returns False
  AC-12/REQ-10 — reminder_log row written with channel='resend' on success
  AC-13/REQ-4  — (sent, None) returned when ack_token_service is None (no base_url)
  REQ-6        — Reminder state is unchanged after resend
  REQ-13       — resend_notification is a method on ReminderEngine
  E-10         — Resend goes to owner, not escalate_to
  E-12         — ReminderNotFoundError when reminder deleted between steps

All tests are written AGAINST THE SPEC and MUST FAIL until implementation exists.

Import strategy: modules not yet created are imported lazily so collection
succeeds. Failures come from pytest.fail() or assertion errors, not raw
ImportError at collection time.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import EscalationProfile, EscalationStage
from src.models.reminder import Reminder, ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.services.reminder_engine import ReminderEngine
from src.services.reminder_service import ReminderService
from tests.conftest import FailingSender, MockSender


# ---------------------------------------------------------------------------
# Lazy importers
# ---------------------------------------------------------------------------


def _import_resend_errors():
    try:
        from src.services.reminder_service import (
            ResendCooldownError,
            ResendDeliveryError,
            ResendNotEligibleError,
        )

        return ResendNotEligibleError, ResendCooldownError, ResendDeliveryError
    except ImportError as exc:
        pytest.fail(
            f"Resend error classes not yet in src.services.reminder_service: {exc}"
        )


def _import_reminder_not_found_error():
    try:
        from src.services.reminder_service import ReminderNotFoundError

        return ReminderNotFoundError
    except ImportError as exc:
        pytest.fail(f"ReminderNotFoundError not importable: {exc}")


def _import_ack_token_service():
    try:
        from src.services.ack_token_service import AckTokenService

        return AckTokenService
    except ImportError as exc:
        pytest.fail(f"AckTokenService not importable: {exc}")


# ---------------------------------------------------------------------------
# In-memory AckTokenRepository
# ---------------------------------------------------------------------------


class InMemoryAckTokenRepository:
    """Minimal in-memory AckTokenRepository for engine unit tests."""

    def __init__(self) -> None:
        self._tokens: dict = {}  # token_hash → AckToken-like record

    def store_token(
        self,
        token_hash: str,
        reminder_id: int,
        expires_at: datetime,
    ) -> None:
        self._tokens[token_hash] = {
            "token_hash": token_hash,
            "reminder_id": reminder_id,
            "expires_at": expires_at,
            "used": False,
            "used_at": None,
            "created_at": datetime.now(timezone.utc),
        }

    def get_by_hash(self, token_hash: str):
        return self._tokens.get(token_hash)

    def mark_used(self, token_hash: str) -> bool:
        token = self._tokens.get(token_hash)
        if token is None or token["used"]:
            return False
        token["used"] = True
        token["used_at"] = datetime.now(timezone.utc)
        return True

    def count(self) -> int:
        return len(self._tokens)

    def all_hashes(self) -> list[str]:
        return list(self._tokens.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNER_PHONE = "+441234567890"
BASE_URL = "https://klaxxon.example.com"


def _utc_future(hours: float = 2) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _backdate_resend_log(
    repo: SqliteReminderRepository,
    reminder_id: int,
    seconds_ago: float,
) -> None:
    """Insert a reminder_log row with channel='resend' backdated by seconds_ago."""
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    conn = repo._get_conn()
    conn.execute(
        "INSERT INTO reminder_log (reminder_id, channel, message, sent_at) "
        "VALUES (?, ?, ?, ?)",
        (reminder_id, "resend", "[RESEND] backdated", past.isoformat()),
    )
    conn.commit()


def _count_resend_log_entries(repo: SqliteReminderRepository, reminder_id: int) -> int:
    row = (
        repo._get_conn()
        .execute(
            "SELECT COUNT(*) AS c FROM reminder_log "
            "WHERE reminder_id = ? AND channel = 'resend'",
            (reminder_id,),
        )
        .fetchone()
    )
    return row["c"]


def _get_last_resend_log(repo: SqliteReminderRepository, reminder_id: int):
    return (
        repo._get_conn()
        .execute(
            "SELECT * FROM reminder_log WHERE reminder_id = ? AND channel = 'resend' "
            "ORDER BY sent_at DESC LIMIT 1",
            (reminder_id,),
        )
        .fetchone()
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SIMPLE_PROFILE: dict[str, EscalationProfile] = {
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
def ack_repo() -> InMemoryAckTokenRepository:
    return InMemoryAckTokenRepository()


@pytest.fixture
def ack_service(ack_repo: InMemoryAckTokenRepository):
    AckTokenService = _import_ack_token_service()
    return AckTokenService(repository=ack_repo, base_url=BASE_URL)


@pytest.fixture
def engine(
    service: ReminderService,
    repo: SqliteReminderRepository,
    mock_sender: MockSender,
    ack_service,
) -> ReminderEngine:
    """Engine with AckTokenService wired in (base_url configured)."""
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient=OWNER_PHONE,
        escalation_profiles=SIMPLE_PROFILE,
        ack_token_service=ack_service,
    )


@pytest.fixture
def engine_no_ack(
    service: ReminderService,
    repo: SqliteReminderRepository,
    mock_sender: MockSender,
) -> ReminderEngine:
    """Engine with no AckTokenService (base_url not configured)."""
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=mock_sender,
        recipient=OWNER_PHONE,
        escalation_profiles=SIMPLE_PROFILE,
        ack_token_service=None,
    )


@pytest.fixture
def engine_failing(
    service: ReminderService,
    repo: SqliteReminderRepository,
    ack_service,
) -> ReminderEngine:
    """Engine whose sender always fails."""
    failing = FailingSender()
    return ReminderEngine(
        service=service,
        repository=repo,
        sender=failing,
        recipient=OWNER_PHONE,
        escalation_profiles=SIMPLE_PROFILE,
        ack_token_service=ack_service,
    )


@pytest.fixture
def reminding_reminder(
    service: ReminderService,
    repo: SqliteReminderRepository,
) -> Reminder:
    starts_at = datetime.now(timezone.utc).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    if starts_at <= datetime.now(timezone.utc):
        starts_at += timedelta(days=1)
    reminder = service.create(title="Daily Standup", starts_at=starts_at)
    assert reminder.id is not None
    service.mark_reminding(reminder.id)
    return service.get(reminder.id)


@pytest.fixture
def acknowledged_reminder(
    repo: SqliteReminderRepository,
) -> Reminder:
    reminder = repo.create(Reminder(title="Acked", starts_at=_utc_future()))
    assert reminder.id is not None
    repo.update_state(
        reminder.id,
        ReminderState.ACKNOWLEDGED,
        ack_keyword="ack",
        ack_at=datetime.now(timezone.utc),
    )
    return repo.get(reminder.id)


@pytest.fixture
def missed_reminder(
    repo: SqliteReminderRepository,
) -> Reminder:
    reminder = repo.create(Reminder(title="Missed", starts_at=_utc_future()))
    assert reminder.id is not None
    repo.update_state(reminder.id, ReminderState.REMINDING)
    repo.update_state(reminder.id, ReminderState.MISSED)
    return repo.get(reminder.id)


@pytest.fixture
def pending_reminder(service: ReminderService) -> Reminder:
    return service.create(title="Pending", starts_at=_utc_future())


@pytest.fixture
def skipped_reminder(
    repo: SqliteReminderRepository,
) -> Reminder:
    reminder = repo.create(Reminder(title="Skipped", starts_at=_utc_future()))
    assert reminder.id is not None
    repo.update_state(reminder.id, ReminderState.SKIPPED)
    return repo.get(reminder.id)


# ===========================================================================
# REQ-13: resend_notification is a method on ReminderEngine
# ===========================================================================


class TestMethodExists:
    """REQ-13: resend_notification() must exist as a method on ReminderEngine."""

    def test_resend_notification_method_exists(self, engine: ReminderEngine) -> None:
        """ReminderEngine must have a resend_notification async method."""
        assert hasattr(engine, "resend_notification"), (
            "ReminderEngine must have resend_notification method (REQ-13)"
        )
        import asyncio
        import inspect

        assert inspect.iscoroutinefunction(engine.resend_notification), (
            "resend_notification must be an async method (coroutine function)"
        )

    def test_resend_notification_signature(self, engine: ReminderEngine) -> None:
        """resend_notification(reminder_id: int) -> tuple[bool, Optional[str]]."""
        import inspect

        sig = inspect.signature(engine.resend_notification)
        params = list(sig.parameters.keys())
        assert "reminder_id" in params, (
            f"resend_notification must accept 'reminder_id' parameter, got: {params}"
        )


# ===========================================================================
# REQ-2 / AC-6: ReminderNotFoundError for non-existent id
# ===========================================================================


class TestNotFound:
    """AC-6: ReminderNotFoundError raised for non-existent reminder id."""

    @pytest.mark.asyncio
    async def test_raises_reminder_not_found_for_missing_id(
        self, engine: ReminderEngine
    ) -> None:
        """resend_notification(9999) → ReminderNotFoundError (AC-6)."""
        ReminderNotFoundError = _import_reminder_not_found_error()

        with pytest.raises(ReminderNotFoundError):
            await engine.resend_notification(9999)


# ===========================================================================
# REQ-3 / AC-4: ResendNotEligibleError for PENDING
# ===========================================================================


class TestIneligibleStatePending:
    """AC-4 / E-1: PENDING reminder raises ResendNotEligibleError."""

    @pytest.mark.asyncio
    async def test_pending_raises_not_eligible(
        self,
        engine: ReminderEngine,
        pending_reminder: Reminder,
    ) -> None:
        """resend_notification on PENDING → ResendNotEligibleError (AC-4)."""
        ResendNotEligibleError, _, _ = _import_resend_errors()

        with pytest.raises(ResendNotEligibleError):
            await engine.resend_notification(pending_reminder.id)

    @pytest.mark.asyncio
    async def test_pending_sends_no_message(
        self,
        engine: ReminderEngine,
        pending_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """No message sent for PENDING reminder (AC-4)."""
        ResendNotEligibleError, _, _ = _import_resend_errors()

        try:
            await engine.resend_notification(pending_reminder.id)
        except ResendNotEligibleError:
            pass

        assert len(mock_sender.messages) == 0

    @pytest.mark.asyncio
    async def test_pending_stores_no_ack_token(
        self,
        engine: ReminderEngine,
        pending_reminder: Reminder,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """No ack token stored for PENDING reminder (AC-4)."""
        ResendNotEligibleError, _, _ = _import_resend_errors()

        try:
            await engine.resend_notification(pending_reminder.id)
        except ResendNotEligibleError:
            pass

        assert ack_repo.count() == 0


# ===========================================================================
# REQ-3 / AC-5: ResendNotEligibleError for SKIPPED
# ===========================================================================


class TestIneligibleStateSkipped:
    """AC-5 / E-2: SKIPPED reminder raises ResendNotEligibleError."""

    @pytest.mark.asyncio
    async def test_skipped_raises_not_eligible(
        self,
        engine: ReminderEngine,
        skipped_reminder: Reminder,
    ) -> None:
        """resend_notification on SKIPPED → ResendNotEligibleError (AC-5)."""
        ResendNotEligibleError, _, _ = _import_resend_errors()

        with pytest.raises(ResendNotEligibleError):
            await engine.resend_notification(skipped_reminder.id)

    @pytest.mark.asyncio
    async def test_skipped_sends_no_message(
        self,
        engine: ReminderEngine,
        skipped_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """No message sent for SKIPPED reminder (AC-5)."""
        ResendNotEligibleError, _, _ = _import_resend_errors()

        try:
            await engine.resend_notification(skipped_reminder.id)
        except ResendNotEligibleError:
            pass

        assert len(mock_sender.messages) == 0


# ===========================================================================
# AC-1 / REQ-7 / REQ-8 / REQ-4: Happy path — REMINDING state
# ===========================================================================


class TestHappyPathReminding:
    """AC-1: Successful resend for REMINDING state."""

    @pytest.mark.asyncio
    async def test_returns_tuple_sent_true_and_ack_url(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
    ) -> None:
        """Returns (True, ack_url) on success (spec return type)."""
        result = await engine.resend_notification(reminding_reminder.id)
        assert isinstance(result, tuple), (
            f"resend_notification must return a tuple, got {type(result)}"
        )
        sent, ack_url = result
        assert sent is True, f"sent must be True on success, got {sent!r}"
        assert ack_url is not None, (
            "ack_url must not be None when base_url is configured"
        )
        assert ack_url.startswith(BASE_URL + "/ack/"), (
            f"ack_url must start with {BASE_URL}/ack/, got: {ack_url!r}"
        )

    @pytest.mark.asyncio
    async def test_message_sent_to_owner(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """Message sent to owner's phone number (REQ-7)."""
        await engine.resend_notification(reminding_reminder.id)

        assert len(mock_sender.messages) == 1, (
            f"Expected 1 message sent, got {len(mock_sender.messages)}"
        )
        recipient, _ = mock_sender.messages[0]
        assert recipient == OWNER_PHONE, (
            f"Message sent to wrong recipient: {recipient!r}"
        )

    @pytest.mark.asyncio
    async def test_message_starts_with_resend_prefix(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """Message starts with [RESEND] (REQ-8, AC-14)."""
        await engine.resend_notification(reminding_reminder.id)

        _, text = mock_sender.messages[0]
        assert text.startswith("[RESEND]"), (
            f"Message must start with '[RESEND]', got: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_message_contains_title(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """Message contains reminder title (REQ-8)."""
        await engine.resend_notification(reminding_reminder.id)

        _, text = mock_sender.messages[0]
        assert "Daily Standup" in text, (
            f"Message must contain title 'Daily Standup', got: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_message_contains_time(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """Message contains starts_at time in HH:MM format (REQ-8)."""
        await engine.resend_notification(reminding_reminder.id)

        _, text = mock_sender.messages[0]
        assert re.search(r"\d{2}:\d{2}", text), (
            f"Message must contain HH:MM time, got: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_message_contains_ack_url(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """Message contains ack URL when base_url is configured (REQ-8, AC-14)."""
        await engine.resend_notification(reminding_reminder.id)

        _, text = mock_sender.messages[0]
        assert BASE_URL + "/ack/" in text, (
            f"Message must contain ack URL, got: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_message_contains_ack_label(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """Message contains 'Ack:' label before ack URL (AC-14)."""
        await engine.resend_notification(reminding_reminder.id)

        _, text = mock_sender.messages[0]
        assert "Ack:" in text or "ack:" in text.lower(), (
            f"Message must contain 'Ack:' label before URL, got: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_ack_token_committed_to_repo(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """AckToken is committed to repository on success (REQ-4)."""
        await engine.resend_notification(reminding_reminder.id)

        assert ack_repo.count() == 1, (
            f"Expected 1 ack token stored, got {ack_repo.count()}"
        )

    @pytest.mark.asyncio
    async def test_token_hash_matches_url_token(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """SHA-256 hash of URL token matches stored token_hash (REQ-4)."""
        await engine.resend_notification(reminding_reminder.id)

        _, text = mock_sender.messages[0]
        match = re.search(rf"{re.escape(BASE_URL)}/ack/(\S+)", text)
        assert match is not None, f"No ack URL found in message: {text!r}"

        raw_token = match.group(1)
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        assert expected_hash in ack_repo.all_hashes(), (
            "SHA-256 of URL token must match a stored token_hash"
        )

    @pytest.mark.asyncio
    async def test_reminder_log_written_with_resend_channel(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """reminder_log row with channel='resend' is written on success (AC-12, REQ-10)."""
        await engine.resend_notification(reminding_reminder.id)

        count = _count_resend_log_entries(repo, reminding_reminder.id)
        assert count == 1, (
            f"Expected 1 reminder_log row with channel='resend', got {count}"
        )

    @pytest.mark.asyncio
    async def test_reminder_log_message_contains_resend_prefix(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """reminder_log message contains '[RESEND]' (AC-12)."""
        await engine.resend_notification(reminding_reminder.id)

        row = _get_last_resend_log(repo, reminding_reminder.id)
        assert row is not None, "reminder_log row not found"
        assert "[RESEND]" in row["message"], (
            f"reminder_log message must contain '[RESEND]', got: {row['message']!r}"
        )

    @pytest.mark.asyncio
    async def test_reminder_log_sent_at_is_recent(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """reminder_log sent_at is within the last few seconds (AC-12)."""
        before = datetime.now(timezone.utc) - timedelta(seconds=2)
        await engine.resend_notification(reminding_reminder.id)
        after = datetime.now(timezone.utc) + timedelta(seconds=2)

        row = _get_last_resend_log(repo, reminding_reminder.id)
        assert row is not None
        sent_at = datetime.fromisoformat(row["sent_at"])
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        assert before <= sent_at <= after, (
            f"sent_at {sent_at} is not within expected window [{before}, {after}]"
        )

    @pytest.mark.asyncio
    async def test_state_unchanged_after_resend(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """Reminder state remains REMINDING after resend — no state transition (REQ-6)."""
        await engine.resend_notification(reminding_reminder.id)

        updated = repo.get(reminding_reminder.id)
        assert updated.state == ReminderState.REMINDING, (
            f"State must remain REMINDING, got: {updated.state}"
        )


# ===========================================================================
# E-3: ACKNOWLEDGED state — eligible
# ===========================================================================


class TestEligibleStateAcknowledged:
    """E-3: ACKNOWLEDGED reminder can be resent; state stays ACKNOWLEDGED."""

    @pytest.mark.asyncio
    async def test_acknowledged_returns_sent_true(
        self,
        engine: ReminderEngine,
        acknowledged_reminder: Reminder,
    ) -> None:
        """resend_notification on ACKNOWLEDGED → (True, ack_url) (E-3)."""
        sent, ack_url = await engine.resend_notification(acknowledged_reminder.id)
        assert sent is True

    @pytest.mark.asyncio
    async def test_acknowledged_state_unchanged(
        self,
        engine: ReminderEngine,
        acknowledged_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """State remains ACKNOWLEDGED after resend (E-3, REQ-6)."""
        await engine.resend_notification(acknowledged_reminder.id)

        updated = repo.get(acknowledged_reminder.id)
        assert updated.state == ReminderState.ACKNOWLEDGED, (
            f"State must remain ACKNOWLEDGED, got: {updated.state}"
        )


# ===========================================================================
# E-4: MISSED state — eligible
# ===========================================================================


class TestEligibleStateMissed:
    """E-4: MISSED reminder can be resent; state stays MISSED."""

    @pytest.mark.asyncio
    async def test_missed_returns_sent_true(
        self,
        engine: ReminderEngine,
        missed_reminder: Reminder,
    ) -> None:
        """resend_notification on MISSED → (True, ack_url) (E-4)."""
        sent, ack_url = await engine.resend_notification(missed_reminder.id)
        assert sent is True

    @pytest.mark.asyncio
    async def test_missed_state_unchanged(
        self,
        engine: ReminderEngine,
        missed_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """State remains MISSED after resend (E-4, REQ-6)."""
        await engine.resend_notification(missed_reminder.id)

        updated = repo.get(missed_reminder.id)
        assert updated.state == ReminderState.MISSED, (
            f"State must remain MISSED, got: {updated.state}"
        )


# ===========================================================================
# REQ-9 / AC-8: Cooldown enforcement
# ===========================================================================


class TestCooldown:
    """AC-8 / REQ-9: ResendCooldownError raised within 60-second cooldown."""

    @pytest.mark.asyncio
    async def test_raises_cooldown_error_within_60_seconds(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """resend_notification within cooldown → ResendCooldownError (AC-8)."""
        _, ResendCooldownError, _ = _import_resend_errors()
        _backdate_resend_log(repo, reminding_reminder.id, seconds_ago=30)

        with pytest.raises(ResendCooldownError):
            await engine.resend_notification(reminding_reminder.id)

    @pytest.mark.asyncio
    async def test_cooldown_error_has_retry_after(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """ResendCooldownError.retry_after is approximately 30 when 30 s remain (AC-8)."""
        _, ResendCooldownError, _ = _import_resend_errors()
        _backdate_resend_log(repo, reminding_reminder.id, seconds_ago=30)

        try:
            await engine.resend_notification(reminding_reminder.id)
            pytest.fail("Expected ResendCooldownError to be raised")
        except ResendCooldownError as exc:
            assert hasattr(exc, "retry_after"), (
                "ResendCooldownError must have 'retry_after' attribute"
            )
            # Should be ~30 (60 - 30 elapsed), allow ±5 s for test timing
            assert 25 <= exc.retry_after <= 35, (
                f"retry_after should be ~30, got {exc.retry_after}"
            )

    @pytest.mark.asyncio
    async def test_cooldown_sends_no_message(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """No message sent when cooldown is active (AC-8)."""
        _, ResendCooldownError, _ = _import_resend_errors()
        _backdate_resend_log(repo, reminding_reminder.id, seconds_ago=30)

        try:
            await engine.resend_notification(reminding_reminder.id)
        except ResendCooldownError:
            pass

        assert len(mock_sender.messages) == 0

    @pytest.mark.asyncio
    async def test_cooldown_stores_no_ack_token(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """No ack token stored when cooldown is active (AC-8)."""
        _, ResendCooldownError, _ = _import_resend_errors()
        _backdate_resend_log(repo, reminding_reminder.id, seconds_ago=30)

        try:
            await engine.resend_notification(reminding_reminder.id)
        except ResendCooldownError:
            pass

        assert ack_repo.count() == 0


# ===========================================================================
# AC-9 / E-9: Cooldown expires at >= 60 seconds
# ===========================================================================


class TestCooldownExpiry:
    """AC-9 / E-9: No error when >= 60 s have elapsed since last resend."""

    @pytest.mark.asyncio
    async def test_no_error_after_61_seconds(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """resend_notification succeeds after 61 s cooldown has expired (AC-9)."""
        _backdate_resend_log(repo, reminding_reminder.id, seconds_ago=61)

        # Must not raise
        sent, ack_url = await engine.resend_notification(reminding_reminder.id)
        assert sent is True

    @pytest.mark.asyncio
    async def test_no_error_at_exactly_60_seconds(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """Resend at exactly 60 s → allowed (boundary is inclusive, E-9)."""
        _backdate_resend_log(repo, reminding_reminder.id, seconds_ago=60)

        # Must not raise (>= 60 seconds means cooldown has expired)
        sent, _ = await engine.resend_notification(reminding_reminder.id)
        assert sent is True


# ===========================================================================
# AC-10 / REQ-5: Fresh token per resend; old tokens remain valid
# ===========================================================================


class TestFreshTokenPerResend:
    """AC-10 / REQ-5: Each resend produces a distinct token; old tokens remain."""

    @pytest.mark.asyncio
    async def test_two_resends_produce_distinct_tokens(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Two successful resends store two distinct token_hashes (AC-10)."""
        # First resend
        await engine.resend_notification(reminding_reminder.id)
        assert ack_repo.count() == 1
        hash_1 = ack_repo.all_hashes()[0]

        # Clear cooldown
        conn = repo._get_conn()
        past = datetime.now(timezone.utc) - timedelta(seconds=61)
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? "
            "WHERE reminder_id = ? AND channel = 'resend'",
            (past.isoformat(), reminding_reminder.id),
        )
        conn.commit()

        # Second resend
        await engine.resend_notification(reminding_reminder.id)
        assert ack_repo.count() == 2, (
            f"Expected 2 tokens after 2 resends, got {ack_repo.count()}"
        )
        hashes = ack_repo.all_hashes()
        assert hashes[0] != hashes[1], (
            "Two resends must produce distinct token hashes (AC-10)"
        )

    @pytest.mark.asyncio
    async def test_old_token_remains_in_repo_after_second_resend(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Old ack token is NOT deleted after a second resend (REQ-5)."""
        # First resend
        await engine.resend_notification(reminding_reminder.id)
        original_hash = ack_repo.all_hashes()[0]

        # Clear cooldown
        conn = repo._get_conn()
        past = datetime.now(timezone.utc) - timedelta(seconds=61)
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? "
            "WHERE reminder_id = ? AND channel = 'resend'",
            (past.isoformat(), reminding_reminder.id),
        )
        conn.commit()

        # Second resend
        await engine.resend_notification(reminding_reminder.id)

        # Original token must still be present
        assert original_hash in ack_repo.all_hashes(), (
            "Old ack token must remain valid after a second resend (REQ-5)"
        )


# ===========================================================================
# AC-11 / REQ-12 / E-7: Token NOT committed when send fails
# ===========================================================================


class TestSendFailure:
    """AC-11 / E-7: ResendDeliveryError raised when send fails; no token stored."""

    @pytest.mark.asyncio
    async def test_raises_delivery_error_when_send_fails(
        self,
        engine_failing: ReminderEngine,
        reminding_reminder: Reminder,
    ) -> None:
        """resend_notification raises ResendDeliveryError when send returns False (AC-11)."""
        _, _, ResendDeliveryError = _import_resend_errors()

        with pytest.raises(ResendDeliveryError):
            await engine_failing.resend_notification(reminding_reminder.id)

    @pytest.mark.asyncio
    async def test_no_ack_token_committed_on_send_failure(
        self,
        engine_failing: ReminderEngine,
        reminding_reminder: Reminder,
        ack_repo: InMemoryAckTokenRepository,
    ) -> None:
        """No ack token is committed when send fails (AC-11, REQ-12)."""
        _, _, ResendDeliveryError = _import_resend_errors()

        try:
            await engine_failing.resend_notification(reminding_reminder.id)
        except ResendDeliveryError:
            pass

        assert ack_repo.count() == 0, (
            f"No ack token must be stored when send fails, got {ack_repo.count()}"
        )

    @pytest.mark.asyncio
    async def test_no_reminder_log_on_send_failure(
        self,
        engine_failing: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """No reminder_log row inserted when send fails (AC-11)."""
        _, _, ResendDeliveryError = _import_resend_errors()

        try:
            await engine_failing.resend_notification(reminding_reminder.id)
        except ResendDeliveryError:
            pass

        count = _count_resend_log_entries(repo, reminding_reminder.id)
        assert count == 0, (
            f"No reminder_log row must be inserted when send fails, got {count}"
        )

    @pytest.mark.asyncio
    async def test_state_unchanged_when_send_fails(
        self,
        engine_failing: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """State remains unchanged when send fails (REQ-6)."""
        _, _, ResendDeliveryError = _import_resend_errors()

        try:
            await engine_failing.resend_notification(reminding_reminder.id)
        except ResendDeliveryError:
            pass

        updated = repo.get(reminding_reminder.id)
        assert updated.state == ReminderState.REMINDING, (
            f"State must remain REMINDING even when send fails, got: {updated.state}"
        )


# ===========================================================================
# AC-13 / E-6: No ack token when base_url not configured (graceful degradation)
# ===========================================================================


class TestNoBaseUrl:
    """AC-13 / E-6: (True, None) returned when ack_token_service is None."""

    @pytest.mark.asyncio
    async def test_returns_none_ack_url_without_base_url(
        self,
        engine_no_ack: ReminderEngine,
        reminding_reminder: Reminder,
    ) -> None:
        """Returns (True, None) when ack_token_service is None (AC-13)."""
        sent, ack_url = await engine_no_ack.resend_notification(reminding_reminder.id)
        assert sent is True, "sent must be True even without base_url"
        assert ack_url is None, (
            f"ack_url must be None when base_url is not configured, got: {ack_url!r}"
        )

    @pytest.mark.asyncio
    async def test_message_sent_without_ack_url(
        self,
        engine_no_ack: ReminderEngine,
        reminding_reminder: Reminder,
        mock_sender: MockSender,
    ) -> None:
        """Message is sent even without ack URL (AC-13)."""
        await engine_no_ack.resend_notification(reminding_reminder.id)

        assert len(mock_sender.messages) == 1, (
            "Message must be sent even without ack URL"
        )
        _, text = mock_sender.messages[0]
        assert "[RESEND]" in text, "Message must still contain [RESEND] prefix"
        assert "/ack/" not in text, (
            "Message must NOT contain ack URL when base_url is not configured"
        )

    @pytest.mark.asyncio
    async def test_reminder_log_written_without_base_url(
        self,
        engine_no_ack: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """reminder_log row is still written even without base_url (AC-13, REQ-10)."""
        await engine_no_ack.resend_notification(reminding_reminder.id)

        count = _count_resend_log_entries(repo, reminding_reminder.id)
        assert count == 1, (
            f"reminder_log must be written even without base_url, got {count}"
        )


# ===========================================================================
# E-10: Resend sends to owner only, not escalate_to
# ===========================================================================


class TestOwnerOnlyTarget:
    """E-10: Resend goes to reminder owner, not escalation recipient."""

    @pytest.mark.asyncio
    async def test_resend_sends_to_owner_only(
        self,
        engine: ReminderEngine,
        repo: SqliteReminderRepository,
        service: ReminderService,
        mock_sender: MockSender,
    ) -> None:
        """Resend message is sent only to the owner, not to escalate_to (E-10)."""
        reminder = repo.create(
            Reminder(
                title="Escalated Reminder",
                starts_at=_utc_future(),
                escalate_to="+447700900123",
            )
        )
        assert reminder.id is not None
        repo.update_state(reminder.id, ReminderState.REMINDING)

        await engine.resend_notification(reminder.id)

        recipients = [r for r, _ in mock_sender.messages]
        assert OWNER_PHONE in recipients, "Owner must receive resend"
        assert "+447700900123" not in recipients, (
            "escalate_to must NOT receive resend (E-10: escalation is engine concern)"
        )
        assert len(mock_sender.messages) == 1, (
            f"Only 1 message should be sent (to owner only), got {len(mock_sender.messages)}"
        )


# ===========================================================================
# REQ-4: Message format includes link when present
# ===========================================================================


class TestMessageFormatWithLink:
    """REQ-8: Message includes reminder link when configured."""

    @pytest.mark.asyncio
    async def test_message_contains_link_when_present(
        self,
        engine: ReminderEngine,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Message contains the reminder link when it is set (REQ-8)."""
        reminder = repo.create(
            Reminder(
                title="Linked Meeting",
                starts_at=_utc_future(),
                link="https://zoom.us/j/99999",
            )
        )
        assert reminder.id is not None
        repo.update_state(reminder.id, ReminderState.REMINDING)

        await engine.resend_notification(reminder.id)

        _, text = mock_sender.messages[0]
        assert "https://zoom.us/j/99999" in text, (
            f"Message must contain the link, got: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_message_sent_without_link_when_none(
        self,
        engine: ReminderEngine,
        repo: SqliteReminderRepository,
        mock_sender: MockSender,
    ) -> None:
        """Message is sent normally even when reminder has no link (REQ-8)."""
        reminder = repo.create(
            Reminder(title="No Link Reminder", starts_at=_utc_future(), link=None)
        )
        assert reminder.id is not None
        repo.update_state(reminder.id, ReminderState.REMINDING)

        await engine.resend_notification(reminder.id)

        assert len(mock_sender.messages) == 1
        _, text = mock_sender.messages[0]
        assert "[RESEND]" in text


# ===========================================================================
# Cooldown uses reminder_log channel='resend' — not 'signal' entries
# ===========================================================================


class TestCooldownUsesResendChannelOnly:
    """REQ-9 / data model: Cooldown check uses channel='resend' entries only."""

    @pytest.mark.asyncio
    async def test_signal_channel_log_does_not_trigger_cooldown(
        self,
        engine: ReminderEngine,
        reminding_reminder: Reminder,
        repo: SqliteReminderRepository,
    ) -> None:
        """A recent 'signal' log entry does NOT trigger the resend cooldown (REQ-9)."""
        # Insert a recent 'signal' channel entry (engine-initiated)
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        conn = repo._get_conn()
        conn.execute(
            "INSERT INTO reminder_log (reminder_id, channel, message, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (reminding_reminder.id, "signal", "Normal nag message", recent.isoformat()),
        )
        conn.commit()

        # Resend should NOT be blocked by the 'signal' channel entry
        _, ResendCooldownError, _ = _import_resend_errors()
        try:
            sent, _ = await engine.resend_notification(reminding_reminder.id)
            assert sent is True
        except ResendCooldownError:
            pytest.fail(
                "Cooldown should only check channel='resend' entries, "
                "not channel='signal' entries"
            )
