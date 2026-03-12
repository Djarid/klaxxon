"""API-level tests for POST /api/reminders/{id}/resend.

Tests all acceptance criteria from .Claude/plans/resend-notification.md:

  AC-1   — Successful resend for REMINDING reminder (happy path)
  AC-2   — Successful resend for ACKNOWLEDGED reminder
  AC-3   — Successful resend for MISSED reminder
  AC-4   — 409 for PENDING reminder (never been sent)
  AC-5   — 409 for SKIPPED reminder (user explicitly opted out)
  AC-6   — 404 for non-existent reminder
  AC-7   — 401 without bearer token
  AC-8   — 429 with Retry-After when cooldown active (~30 s remaining)
  AC-9   — 200 when cooldown has expired (>= 60 s since last resend)
  AC-10  — Two sequential resends produce distinct ack tokens
  AC-11  — 502 when MessageSender fails; no ack token stored
  AC-12  — Successful resend writes reminder_log row with channel='resend'
  AC-13  — ack_url is null when base_url is not configured
  AC-14  — Message includes [RESEND] prefix, title, starts_at time, ack URL

All tests MUST FAIL until the implementation exists.

Import strategy: modules that don't yet exist are imported via lazy helpers
so that test collection succeeds even before implementation is in place.
Failures are surfaced as pytest.fail() with a clear message, not as
collection-level ImportError.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api import auth, routes
from src.models.reminder import Reminder, ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.services.reminder_service import ReminderService
from tests.conftest import FailingSender, MockSender


# ---------------------------------------------------------------------------
# Lazy importers for modules that do not yet exist
# ---------------------------------------------------------------------------


def _import_reminder_engine():
    try:
        from src.services.reminder_engine import ReminderEngine

        return ReminderEngine
    except ImportError as exc:
        pytest.fail(f"src.services.reminder_engine not importable: {exc}")


def _import_resend_errors():
    """Return (ResendNotEligibleError, ResendCooldownError, ResendDeliveryError)."""
    try:
        from src.services.reminder_service import (
            ResendCooldownError,
            ResendDeliveryError,
            ResendNotEligibleError,
        )

        return ResendNotEligibleError, ResendCooldownError, ResendDeliveryError
    except ImportError as exc:
        pytest.fail(
            f"Resend error classes not yet implemented in reminder_service: {exc}"
        )


def _import_resend_response_schema():
    try:
        from src.models.schemas import ResendResponse

        return ResendResponse
    except ImportError as exc:
        pytest.fail(f"ResendResponse not yet implemented in src.models.schemas: {exc}")


def _verify_resend_route_registered():
    """Assert that POST /reminders/{reminder_id}/resend is on the router."""
    path_ops = {getattr(route, "path", None): route for route in routes.router.routes}
    if "/reminders/{reminder_id}/resend" not in path_ops:
        pytest.fail(
            "POST /reminders/{reminder_id}/resend not registered on routes.router. "
            "Expected in src/api/routes.py."
        )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_TOKEN = "resend-test-token-99999"
OWNER_PHONE = "+441234567890"
BASE_URL = "https://klaxxon.example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_future(hours: float = 2) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _backdate_reminder_log(
    repo: SqliteReminderRepository,
    reminder_id: int,
    *,
    seconds_ago: float,
    channel: str = "resend",
) -> None:
    """Directly insert a reminder_log row backdated by `seconds_ago`."""
    past = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    conn = repo._get_conn()
    conn.execute(
        "INSERT INTO reminder_log (reminder_id, channel, message, sent_at) "
        "VALUES (?, ?, ?, ?)",
        (reminder_id, channel, "[RESEND] backdated entry", past.isoformat()),
    )
    conn.commit()


def _count_ack_tokens(repo: SqliteReminderRepository, reminder_id: int) -> int:
    row = (
        repo._get_conn()
        .execute(
            "SELECT COUNT(*) AS c FROM ack_tokens WHERE reminder_id = ?",
            (reminder_id,),
        )
        .fetchone()
    )
    return row["c"]


def _count_resend_log_entries(repo: SqliteReminderRepository, reminder_id: int) -> int:
    row = (
        repo._get_conn()
        .execute(
            "SELECT COUNT(*) AS c FROM reminder_log WHERE reminder_id = ? AND channel = 'resend'",
            (reminder_id,),
        )
        .fetchone()
    )
    return row["c"]


def _get_last_resend_log(
    repo: SqliteReminderRepository, reminder_id: int
) -> Optional[sqlite3.Row]:
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


@pytest.fixture
def resend_repo() -> SqliteReminderRepository:
    """Thread-safe in-memory SQLite repo for resend API tests."""
    repo = SqliteReminderRepository(":memory:")
    if repo._conn:
        repo._conn.close()
    repo._conn = sqlite3.connect(":memory:", check_same_thread=False)
    repo._conn.row_factory = sqlite3.Row
    repo._conn.execute("PRAGMA journal_mode=WAL")
    repo._conn.execute("PRAGMA foreign_keys=ON")
    repo._ensure_schema()
    return repo


@pytest.fixture
def resend_service(resend_repo: SqliteReminderRepository) -> ReminderService:
    return ReminderService(resend_repo)


@pytest.fixture
def mock_sender_resend() -> MockSender:
    return MockSender()


@pytest.fixture
def resend_engine(
    resend_service: ReminderService,
    resend_repo: SqliteReminderRepository,
    mock_sender_resend: MockSender,
):
    """ReminderEngine with AckTokenService wired in, for resend API tests."""
    ReminderEngine = _import_reminder_engine()

    try:
        from src.services.ack_token_service import AckTokenService

        ack_service = AckTokenService(
            repository=resend_repo,
            base_url=BASE_URL,
        )
    except ImportError:
        ack_service = None

    from src.config import EscalationProfile, EscalationStage

    profiles = {
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

    return ReminderEngine(
        service=resend_service,
        repository=resend_repo,
        sender=mock_sender_resend,
        recipient=OWNER_PHONE,
        escalation_profiles=profiles,
        ack_token_service=ack_service,
    )


@pytest.fixture
def resend_engine_no_base_url(
    resend_service: ReminderService,
    resend_repo: SqliteReminderRepository,
    mock_sender_resend: MockSender,
):
    """ReminderEngine with AckTokenService=None (no base_url configured)."""
    ReminderEngine = _import_reminder_engine()

    from src.config import EscalationProfile, EscalationStage

    profiles = {
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

    return ReminderEngine(
        service=resend_service,
        repository=resend_repo,
        sender=mock_sender_resend,
        recipient=OWNER_PHONE,
        escalation_profiles=profiles,
        ack_token_service=None,
    )


@pytest.fixture
def resend_engine_failing(
    resend_service: ReminderService,
    resend_repo: SqliteReminderRepository,
):
    """ReminderEngine whose MessageSender always fails."""
    ReminderEngine = _import_reminder_engine()
    failing = FailingSender()

    try:
        from src.services.ack_token_service import AckTokenService

        ack_service = AckTokenService(
            repository=resend_repo,
            base_url=BASE_URL,
        )
    except ImportError:
        ack_service = None

    from src.config import EscalationProfile, EscalationStage

    profiles = {
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

    return ReminderEngine(
        service=resend_service,
        repository=resend_repo,
        sender=failing,
        recipient=OWNER_PHONE,
        escalation_profiles=profiles,
        ack_token_service=ack_service,
    )


def _make_app(
    reminder_service: ReminderService,
    engine=None,
) -> FastAPI:
    """Create a FastAPI test app with the resend endpoint registered."""
    test_app = FastAPI()
    test_app.include_router(routes.router)
    auth.register_token(TEST_TOKEN)

    try:
        routes.set_dependencies(
            service=reminder_service,
            signal_available_fn=None,
            reminder_engine=engine,
        )
    except TypeError:
        # Fallback: set_dependencies doesn't yet accept reminder_engine;
        # tests will still fail meaningfully at the route level (503 / 404).
        routes.set_dependencies(service=reminder_service, signal_available_fn=None)

    return test_app


@pytest.fixture
def app(
    resend_service: ReminderService,
    resend_engine,
) -> Generator[FastAPI, None, None]:
    test_app = _make_app(resend_service, resend_engine)
    yield test_app
    # Cleanup module-level globals
    routes._reminder_service = None
    routes._signal_available_fn = None
    try:
        routes._reminder_engine = None
    except AttributeError:
        pass
    auth._valid_token_hashes.clear()


@pytest.fixture
def app_no_base_url(
    resend_service: ReminderService,
    resend_engine_no_base_url,
) -> Generator[FastAPI, None, None]:
    test_app = _make_app(resend_service, resend_engine_no_base_url)
    yield test_app
    routes._reminder_service = None
    routes._signal_available_fn = None
    try:
        routes._reminder_engine = None
    except AttributeError:
        pass
    auth._valid_token_hashes.clear()


@pytest.fixture
def app_failing_sender(
    resend_service: ReminderService,
    resend_engine_failing,
) -> Generator[FastAPI, None, None]:
    test_app = _make_app(resend_service, resend_engine_failing)
    yield test_app
    routes._reminder_service = None
    routes._signal_available_fn = None
    try:
        routes._reminder_engine = None
    except AttributeError:
        pass
    auth._valid_token_hashes.clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def reminding_reminder(
    resend_repo: SqliteReminderRepository,
) -> Reminder:
    """A reminder in REMINDING state with a predictable title and starts_at."""
    starts_at = datetime.now(timezone.utc).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    if starts_at <= datetime.now(timezone.utc):
        starts_at += timedelta(days=1)
    reminder = resend_repo.create(Reminder(title="Daily Standup", starts_at=starts_at))
    assert reminder.id is not None
    resend_repo.update_state(reminder.id, ReminderState.REMINDING)
    return resend_repo.get(reminder.id)


@pytest.fixture
def acknowledged_reminder(
    resend_repo: SqliteReminderRepository,
) -> Reminder:
    reminder = resend_repo.create(
        Reminder(title="Acked Reminder", starts_at=_utc_future())
    )
    assert reminder.id is not None
    resend_repo.update_state(
        reminder.id,
        ReminderState.ACKNOWLEDGED,
        ack_keyword="ack",
        ack_at=datetime.now(timezone.utc),
    )
    return resend_repo.get(reminder.id)


@pytest.fixture
def missed_reminder(
    resend_repo: SqliteReminderRepository,
) -> Reminder:
    reminder = resend_repo.create(
        Reminder(title="Missed Reminder", starts_at=_utc_future())
    )
    assert reminder.id is not None
    resend_repo.update_state(reminder.id, ReminderState.REMINDING)
    resend_repo.update_state(reminder.id, ReminderState.MISSED)
    return resend_repo.get(reminder.id)


@pytest.fixture
def pending_reminder(
    resend_repo: SqliteReminderRepository,
) -> Reminder:
    reminder = resend_repo.create(
        Reminder(title="Pending Reminder", starts_at=_utc_future())
    )
    return reminder


@pytest.fixture
def skipped_reminder(
    resend_repo: SqliteReminderRepository,
) -> Reminder:
    reminder = resend_repo.create(
        Reminder(title="Skipped Reminder", starts_at=_utc_future())
    )
    assert reminder.id is not None
    resend_repo.update_state(reminder.id, ReminderState.SKIPPED)
    return resend_repo.get(reminder.id)


# ===========================================================================
# Route registration sanity check
# ===========================================================================


class TestRouteRegistered:
    """Verify the endpoint is registered before any HTTP tests run."""

    def test_resend_route_is_registered(self) -> None:
        """POST /reminders/{reminder_id}/resend must exist on the router."""
        _verify_resend_route_registered()

    def test_resend_response_schema_exists(self) -> None:
        """ResendResponse Pydantic model must be importable from src.models.schemas."""
        _import_resend_response_schema()

    def test_resend_error_classes_exist(self) -> None:
        """ResendNotEligibleError, ResendCooldownError, ResendDeliveryError must exist."""
        _import_resend_errors()


# ===========================================================================
# AC-7: Authentication required
# ===========================================================================


class TestAuthRequired:
    """AC-7: The resend endpoint requires bearer token authentication."""

    def test_no_auth_returns_401(
        self,
        client: TestClient,
        reminding_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend without Authorization header → 401."""
        response = client.post(f"/api/reminders/{reminding_reminder.id}/resend")
        assert response.status_code in (401, 403), (
            f"Expected 401/403 without auth, got {response.status_code}"
        )

    def test_invalid_token_returns_401(
        self,
        client: TestClient,
        reminding_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend with wrong token → 401."""
        bad = {"Authorization": "Bearer totally-wrong-token"}
        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=bad
        )
        assert response.status_code in (401, 403), (
            f"Expected 401/403 with invalid token, got {response.status_code}"
        )


# ===========================================================================
# AC-6: 404 for non-existent reminder
# ===========================================================================


class TestNotFound:
    """AC-6: 404 when reminder does not exist."""

    def test_resend_nonexistent_reminder_returns_404(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """POST /api/reminders/9999/resend → 404 with 'Reminder not found'."""
        response = client.post("/api/reminders/9999/resend", headers=auth_headers)
        assert response.status_code == 404, (
            f"Expected 404 for non-existent reminder, got {response.status_code}: "
            f"{response.text}"
        )
        detail = response.json().get("detail", "")
        assert "not found" in detail.lower(), (
            f"Expected 'not found' in detail, got: {detail!r}"
        )


# ===========================================================================
# AC-4: 409 for PENDING reminder
# ===========================================================================


class TestIneligibleStatePending:
    """AC-4 / E-1: PENDING reminders cannot be resent."""

    def test_resend_pending_returns_409(
        self,
        client: TestClient,
        auth_headers: dict,
        pending_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend on PENDING reminder → 409."""
        response = client.post(
            f"/api/reminders/{pending_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 409, (
            f"Expected 409 for PENDING reminder, got {response.status_code}: "
            f"{response.text}"
        )
        detail = response.json().get("detail", "")
        assert "pending" in detail.lower(), (
            f"Expected 'pending' in 409 detail, got: {detail!r}"
        )

    def test_resend_pending_sends_no_message(
        self,
        client: TestClient,
        auth_headers: dict,
        pending_reminder: Reminder,
        mock_sender_resend: MockSender,
    ) -> None:
        """No notification is sent when resend is rejected for PENDING state."""
        client.post(
            f"/api/reminders/{pending_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 0, (
            "No message should be sent when resend is rejected (PENDING state)"
        )

    def test_resend_pending_stores_no_ack_token(
        self,
        client: TestClient,
        auth_headers: dict,
        pending_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """No ack token is stored when resend is rejected for PENDING state."""
        client.post(
            f"/api/reminders/{pending_reminder.id}/resend", headers=auth_headers
        )
        assert _count_ack_tokens(resend_repo, pending_reminder.id) == 0, (
            "No ack token should be stored when resend is rejected (PENDING state)"
        )


# ===========================================================================
# AC-5: 409 for SKIPPED reminder
# ===========================================================================


class TestIneligibleStateSkipped:
    """AC-5 / E-2: SKIPPED reminders cannot be resent."""

    def test_resend_skipped_returns_409(
        self,
        client: TestClient,
        auth_headers: dict,
        skipped_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend on SKIPPED reminder → 409."""
        response = client.post(
            f"/api/reminders/{skipped_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 409, (
            f"Expected 409 for SKIPPED reminder, got {response.status_code}: "
            f"{response.text}"
        )
        detail = response.json().get("detail", "")
        assert "skipped" in detail.lower(), (
            f"Expected 'skipped' in 409 detail, got: {detail!r}"
        )

    def test_resend_skipped_sends_no_message(
        self,
        client: TestClient,
        auth_headers: dict,
        skipped_reminder: Reminder,
        mock_sender_resend: MockSender,
    ) -> None:
        """No notification is sent when resend is rejected for SKIPPED state."""
        client.post(
            f"/api/reminders/{skipped_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 0


# ===========================================================================
# AC-1: Happy path — REMINDING state
# ===========================================================================


class TestHappyPathReminding:
    """AC-1: Successful resend for reminder in REMINDING state."""

    def test_resend_reminding_returns_200(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend on REMINDING reminder → 200."""
        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200, (
            f"Expected 200 for REMINDING resend, got {response.status_code}: "
            f"{response.text}"
        )

    def test_resend_reminding_response_schema(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
    ) -> None:
        """Response body matches ResendResponse schema (REQ-11)."""
        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()

        assert data["reminder_id"] == reminding_reminder.id
        assert data["state"] == "reminding"
        assert data["sent"] is True
        assert "ack_url" in data
        assert "message" in data

    def test_resend_reminding_message_delivered(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        mock_sender_resend: MockSender,
    ) -> None:
        """MessageSender.send_message is called with the owner's number (REQ-7)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 1, (
            f"Expected exactly 1 message sent, got {len(mock_sender_resend.messages)}"
        )
        recipient, _ = mock_sender_resend.messages[0]
        assert recipient == OWNER_PHONE, (
            f"Message sent to wrong recipient: expected {OWNER_PHONE}, got {recipient}"
        )

    def test_resend_reminding_message_contains_resend_prefix(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        mock_sender_resend: MockSender,
    ) -> None:
        """Message text starts with [RESEND] (REQ-8, AC-14)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 1
        _, text = mock_sender_resend.messages[0]
        assert "[RESEND]" in text, (
            f"Expected '[RESEND]' prefix in message, got: {text!r}"
        )

    def test_resend_reminding_message_contains_title(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        mock_sender_resend: MockSender,
    ) -> None:
        """Message text contains the reminder title (REQ-8, AC-14)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 1
        _, text = mock_sender_resend.messages[0]
        assert "Daily Standup" in text, (
            f"Expected title 'Daily Standup' in message, got: {text!r}"
        )

    def test_resend_reminding_message_contains_time(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        mock_sender_resend: MockSender,
    ) -> None:
        """Message text contains the starts_at time (REQ-8, AC-14)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 1
        _, text = mock_sender_resend.messages[0]
        # Expect HH:MM format somewhere in message
        assert re.search(r"\d{2}:\d{2}", text), (
            f"Expected HH:MM time in message, got: {text!r}"
        )

    def test_resend_reminding_message_contains_ack_url(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        mock_sender_resend: MockSender,
    ) -> None:
        """Message contains ack URL when base_url is configured (REQ-4, AC-14)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 1
        _, text = mock_sender_resend.messages[0]
        assert BASE_URL + "/ack/" in text, (
            f"Expected ack URL containing {BASE_URL!r} in message, got: {text!r}"
        )

    def test_resend_reminding_ack_url_in_response(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
    ) -> None:
        """Response ack_url is a valid URL starting with base_url (REQ-4)."""
        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        data = response.json()
        ack_url = data.get("ack_url")
        assert ack_url is not None, (
            "ack_url must not be null when base_url is configured"
        )
        assert ack_url.startswith(BASE_URL + "/ack/"), (
            f"ack_url must start with {BASE_URL}/ack/, got: {ack_url!r}"
        )

    def test_resend_reminding_writes_reminder_log(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """A reminder_log row with channel='resend' is inserted on success (AC-12, REQ-10)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        count = _count_resend_log_entries(resend_repo, reminding_reminder.id)
        assert count == 1, (
            f"Expected 1 reminder_log entry with channel='resend', got {count}"
        )

    def test_resend_reminding_log_message_contains_resend_prefix(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """reminder_log message contains '[RESEND]' (AC-12)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        row = _get_last_resend_log(resend_repo, reminding_reminder.id)
        assert row is not None, "reminder_log row not found"
        assert "[RESEND]" in row["message"], (
            f"Expected '[RESEND]' in log message, got: {row['message']!r}"
        )

    def test_resend_reminding_state_unchanged(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """State remains REMINDING after resend — no state transition (REQ-6)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        updated = resend_repo.get(reminding_reminder.id)
        assert updated.state == ReminderState.REMINDING, (
            f"State must remain REMINDING after resend, got: {updated.state}"
        )

    def test_resend_reminding_stores_ack_token(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """An ack_token row is stored on successful resend (REQ-4)."""
        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        count = _count_ack_tokens(resend_repo, reminding_reminder.id)
        assert count == 1, f"Expected 1 ack_token stored after resend, got {count}"

    def test_resend_response_message_contains_title(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
    ) -> None:
        """Response 'message' field is human-readable and contains the title (REQ-11)."""
        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        data = response.json()
        msg = data.get("message", "")
        assert "Daily Standup" in msg, (
            f"Expected title in response message, got: {msg!r}"
        )

    def test_resend_sends_to_owner_not_escalate_to(
        self,
        resend_repo: SqliteReminderRepository,
        resend_service: ReminderService,
        mock_sender_resend: MockSender,
        resend_engine,
        app: FastAPI,
        auth_headers: dict,
    ) -> None:
        """Resend sends to owner only, never to escalate_to (E-10)."""
        reminder = resend_repo.create(
            Reminder(
                title="Escalated",
                starts_at=_utc_future(),
                escalate_to="+447700900123",
            )
        )
        assert reminder.id is not None
        resend_repo.update_state(reminder.id, ReminderState.REMINDING)

        client = TestClient(app)
        client.post(f"/api/reminders/{reminder.id}/resend", headers=auth_headers)

        recipients = [r for r, _ in mock_sender_resend.messages]
        assert OWNER_PHONE in recipients, "Owner must receive the resend"
        assert "+447700900123" not in recipients, (
            "escalate_to must NOT receive a resend (E-10)"
        )


# ===========================================================================
# AC-2: Happy path — ACKNOWLEDGED state
# ===========================================================================


class TestHappyPathAcknowledged:
    """AC-2 / E-3: ACKNOWLEDGED reminders can be resent; state stays ACKNOWLEDGED."""

    def test_resend_acknowledged_returns_200(
        self,
        client: TestClient,
        auth_headers: dict,
        acknowledged_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend on ACKNOWLEDGED → 200."""
        response = client.post(
            f"/api/reminders/{acknowledged_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200, (
            f"Expected 200 for ACKNOWLEDGED resend, got {response.status_code}: "
            f"{response.text}"
        )

    def test_resend_acknowledged_state_unchanged(
        self,
        client: TestClient,
        auth_headers: dict,
        acknowledged_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """State remains ACKNOWLEDGED after resend (E-3, REQ-6)."""
        client.post(
            f"/api/reminders/{acknowledged_reminder.id}/resend", headers=auth_headers
        )
        updated = resend_repo.get(acknowledged_reminder.id)
        assert updated.state == ReminderState.ACKNOWLEDGED, (
            f"State must remain ACKNOWLEDGED after resend, got: {updated.state}"
        )

    def test_resend_acknowledged_response_state_field(
        self,
        client: TestClient,
        auth_headers: dict,
        acknowledged_reminder: Reminder,
    ) -> None:
        """Response state field is 'acknowledged' (AC-2)."""
        response = client.post(
            f"/api/reminders/{acknowledged_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "acknowledged", (
            f"Expected state='acknowledged' in response, got: {data['state']!r}"
        )


# ===========================================================================
# AC-3: Happy path — MISSED state
# ===========================================================================


class TestHappyPathMissed:
    """AC-3 / E-4: MISSED reminders can be resent; state stays MISSED."""

    def test_resend_missed_returns_200(
        self,
        client: TestClient,
        auth_headers: dict,
        missed_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend on MISSED → 200."""
        response = client.post(
            f"/api/reminders/{missed_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200, (
            f"Expected 200 for MISSED resend, got {response.status_code}: "
            f"{response.text}"
        )

    def test_resend_missed_state_unchanged(
        self,
        client: TestClient,
        auth_headers: dict,
        missed_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """State remains MISSED after resend (E-4, REQ-6)."""
        client.post(f"/api/reminders/{missed_reminder.id}/resend", headers=auth_headers)
        updated = resend_repo.get(missed_reminder.id)
        assert updated.state == ReminderState.MISSED, (
            f"State must remain MISSED after resend, got: {updated.state}"
        )

    def test_resend_missed_response_state_field(
        self,
        client: TestClient,
        auth_headers: dict,
        missed_reminder: Reminder,
    ) -> None:
        """Response state field is 'missed' (AC-3)."""
        response = client.post(
            f"/api/reminders/{missed_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "missed"


# ===========================================================================
# AC-8: Cooldown — 429 when within 60-second window
# ===========================================================================


class TestCooldown:
    """AC-8 / E-8: Second resend within 60 s returns 429 with Retry-After."""

    def test_second_resend_within_cooldown_returns_429(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """Second resend within 60 s → 429 (AC-8)."""
        # Simulate a resend 30 seconds ago via reminder_log
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=30, channel="resend"
        )

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 429, (
            f"Expected 429 within cooldown window, got {response.status_code}: "
            f"{response.text}"
        )

    def test_cooldown_response_has_retry_after_header(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """429 response includes Retry-After header (REQ-9)."""
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=30, channel="resend"
        )

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 429
        assert "retry-after" in response.headers or "Retry-After" in response.headers, (
            f"Expected Retry-After header in 429 response. "
            f"Headers: {dict(response.headers)}"
        )

    def test_cooldown_retry_after_value_is_approximately_30(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """Retry-After value is ~30 when last resend was 30 s ago (AC-8)."""
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=30, channel="resend"
        )

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 429
        header_key = (
            "Retry-After" if "Retry-After" in response.headers else "retry-after"
        )
        retry_after = int(response.headers[header_key])
        # Should be approximately 30 (60 - 30), allow ±5 s for test timing
        assert 25 <= retry_after <= 35, f"Expected Retry-After ~30, got {retry_after}"

    def test_cooldown_sends_no_message(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
        mock_sender_resend: MockSender,
    ) -> None:
        """No message is sent when cooldown is active (AC-8)."""
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=30, channel="resend"
        )

        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert len(mock_sender_resend.messages) == 0, (
            "No message should be sent during cooldown"
        )

    def test_cooldown_stores_no_ack_token(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """No ack token is stored when cooldown is active (AC-8)."""
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=30, channel="resend"
        )

        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert _count_ack_tokens(resend_repo, reminding_reminder.id) == 0, (
            "No ack token should be stored during cooldown"
        )

    def test_cooldown_detail_message_present(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """429 response includes a descriptive error detail (REQ-9)."""
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=30, channel="resend"
        )
        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 429
        detail = response.json().get("detail", "")
        assert detail, "429 response must include a 'detail' field"


# ===========================================================================
# AC-9: Cooldown expires after 60 seconds
# ===========================================================================


class TestCooldownExpiry:
    """AC-9 / E-9: Resend succeeds when >= 60 s have elapsed since last resend."""

    def test_resend_succeeds_after_cooldown_expires(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """Resend after 61 s cooldown → 200 (AC-9)."""
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=61, channel="resend"
        )

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200, (
            f"Expected 200 when cooldown has expired (61 s ago), "
            f"got {response.status_code}: {response.text}"
        )

    def test_resend_at_exact_boundary_succeeds(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """Resend at exactly 60 s → 200 (boundary is inclusive, E-9)."""
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=60, channel="resend"
        )

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        # 60 seconds exactly should be allowed (>= 60)
        assert response.status_code == 200, (
            f"Expected 200 at 60-second boundary (E-9), "
            f"got {response.status_code}: {response.text}"
        )


# ===========================================================================
# AC-10: Fresh ack token per resend
# ===========================================================================


class TestFreshAckTokenPerResend:
    """AC-10: Two separate resends produce two distinct ack tokens."""

    def test_two_resends_produce_distinct_tokens(
        self,
        client: TestClient,
        auth_headers: dict,
        reminding_reminder: Reminder,
        resend_repo: SqliteReminderRepository,
    ) -> None:
        """Two successful resends store two distinct ack_token rows (AC-10, REQ-4, REQ-5)."""
        # First resend — no cooldown
        resp1 = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert resp1.status_code == 200
        url1 = resp1.json().get("ack_url")

        # Expire the cooldown so second resend is allowed
        _backdate_reminder_log(
            resend_repo, reminding_reminder.id, seconds_ago=61, channel="resend"
        )
        # Also backdate the first resend entry so cooldown is cleared
        conn = resend_repo._get_conn()
        past = datetime.now(timezone.utc) - timedelta(seconds=61)
        conn.execute(
            "UPDATE reminder_log SET sent_at = ? WHERE reminder_id = ? AND channel = 'resend'",
            (past.isoformat(), reminding_reminder.id),
        )
        conn.commit()

        # Second resend
        resp2 = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert resp2.status_code == 200
        url2 = resp2.json().get("ack_url")

        assert url1 != url2, (
            f"Two resends must produce distinct ack URLs. "
            f"Got the same URL both times: {url1!r}"
        )

        count = _count_ack_tokens(resend_repo, reminding_reminder.id)
        assert count == 2, f"Expected 2 ack_token rows after two resends, got {count}"


# ===========================================================================
# AC-11: 502 when MessageSender fails; no ack token stored, no log row
# ===========================================================================


class TestSendFailure:
    """AC-11 / E-7: 502 when MessageSender.send_message returns False."""

    def test_send_failure_returns_502(
        self,
        resend_service: ReminderService,
        resend_repo: SqliteReminderRepository,
        resend_engine_failing,
        reminding_reminder: Reminder,
    ) -> None:
        """POST /api/reminders/{id}/resend → 502 when send fails (AC-11)."""
        app = _make_app(resend_service, resend_engine_failing)
        client = TestClient(app)
        auth_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 502, (
            f"Expected 502 when send fails, got {response.status_code}: {response.text}"
        )

        # Cleanup
        routes._reminder_service = None
        try:
            routes._reminder_engine = None
        except AttributeError:
            pass

    def test_send_failure_stores_no_ack_token(
        self,
        resend_service: ReminderService,
        resend_repo: SqliteReminderRepository,
        resend_engine_failing,
        reminding_reminder: Reminder,
    ) -> None:
        """No ack token committed when send fails (AC-11, REQ-12)."""
        app = _make_app(resend_service, resend_engine_failing)
        client = TestClient(app)
        auth_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert _count_ack_tokens(resend_repo, reminding_reminder.id) == 0, (
            "No ack token must be stored when send fails (REQ-12)"
        )

        routes._reminder_service = None
        try:
            routes._reminder_engine = None
        except AttributeError:
            pass

    def test_send_failure_stores_no_log_entry(
        self,
        resend_service: ReminderService,
        resend_repo: SqliteReminderRepository,
        resend_engine_failing,
        reminding_reminder: Reminder,
    ) -> None:
        """No reminder_log row inserted when send fails (AC-11)."""
        app = _make_app(resend_service, resend_engine_failing)
        client = TestClient(app)
        auth_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        count = _count_resend_log_entries(resend_repo, reminding_reminder.id)
        assert count == 0, (
            f"Expected 0 reminder_log rows after failed send, got {count}"
        )

        routes._reminder_service = None
        try:
            routes._reminder_engine = None
        except AttributeError:
            pass


# ===========================================================================
# AC-13: No ack_url when base_url not configured
# ===========================================================================


class TestNoBaseUrl:
    """AC-13 / E-6: ack_url is null when KLAXXON_BASE_URL is not set."""

    def test_ack_url_is_null_without_base_url(
        self,
        resend_service: ReminderService,
        resend_repo: SqliteReminderRepository,
        resend_engine_no_base_url,
        reminding_reminder: Reminder,
    ) -> None:
        """ack_url in response is null when AckTokenService has no base_url (AC-13)."""
        test_app = _make_app(resend_service, resend_engine_no_base_url)
        client = TestClient(test_app)
        auth_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200, (
            f"Expected 200 even without base_url, got {response.status_code}: "
            f"{response.text}"
        )
        data = response.json()
        assert data.get("ack_url") is None, (
            f"ack_url must be null when base_url is not configured, got: {data.get('ack_url')!r}"
        )

        routes._reminder_service = None
        try:
            routes._reminder_engine = None
        except AttributeError:
            pass

    def test_sent_true_without_base_url(
        self,
        resend_service: ReminderService,
        resend_repo: SqliteReminderRepository,
        resend_engine_no_base_url,
        reminding_reminder: Reminder,
    ) -> None:
        """sent=true even when base_url is not configured (AC-13)."""
        test_app = _make_app(resend_service, resend_engine_no_base_url)
        client = TestClient(test_app)
        auth_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        response = client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["sent"] is True, (
            "sent must be True even when no ack_url is generated"
        )

        routes._reminder_service = None
        try:
            routes._reminder_engine = None
        except AttributeError:
            pass

    def test_no_ack_token_stored_without_base_url(
        self,
        resend_service: ReminderService,
        resend_repo: SqliteReminderRepository,
        resend_engine_no_base_url,
        reminding_reminder: Reminder,
    ) -> None:
        """No ack_token row stored when base_url is not configured (AC-13)."""
        test_app = _make_app(resend_service, resend_engine_no_base_url)
        client = TestClient(test_app)
        auth_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        client.post(
            f"/api/reminders/{reminding_reminder.id}/resend", headers=auth_headers
        )
        count = _count_ack_tokens(resend_repo, reminding_reminder.id)
        assert count == 0, (
            f"No ack_token should be stored when base_url is not configured, got {count}"
        )

        routes._reminder_service = None
        try:
            routes._reminder_engine = None
        except AttributeError:
            pass
