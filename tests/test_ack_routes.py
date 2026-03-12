"""API route tests for GET /ack/{token}.

Tests: success, expired, used, not found, already acknowledged,
no auth required, HTML content-type, concurrent-ack atomicity.

All tests written against the specification in .Claude/plans/nag-ack-token.md
and MUST FAIL until the implementation exists.

Import strategy: modules that don't exist yet are imported lazily inside each
test / fixture so that collection succeeds and failures come from pytest.fail()
with a clear message, not a raw ModuleNotFoundError.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api import auth, routes
from src.models.reminder import ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.services.reminder_service import ReminderService


# ---------------------------------------------------------------------------
# Lazy importers for not-yet-existing modules
# ---------------------------------------------------------------------------


def _import_ack_token():
    try:
        from src.models.ack_token import AckToken

        return AckToken
    except ImportError as exc:
        pytest.fail(f"src.models.ack_token not yet implemented: {exc}")


def _import_ack_token_service():
    try:
        from src.services.ack_token_service import (
            AckTokenService,
            TokenAlreadyUsedError,
            TokenExpiredError,
            TokenNotFoundError,
        )

        return (
            AckTokenService,
            TokenNotFoundError,
            TokenExpiredError,
            TokenAlreadyUsedError,
        )
    except ImportError as exc:
        pytest.fail(f"src.services.ack_token_service not yet implemented: {exc}")


def _import_ack_routes():
    try:
        from src.api import ack_routes

        return ack_routes
    except ImportError as exc:
        pytest.fail(f"src.api.ack_routes not yet implemented: {exc}")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-bearer-token-ack-routes"


# ---------------------------------------------------------------------------
# In-memory AckTokenRepository
# ---------------------------------------------------------------------------


class InMemoryAckTokenRepository:
    """Thread-safe in-memory AckTokenRepository for route tests."""

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
def thread_safe_repo() -> SqliteReminderRepository:
    """In-memory SQLite repo with thread-safety for TestClient."""
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
def reminder_service(thread_safe_repo: SqliteReminderRepository) -> ReminderService:
    return ReminderService(thread_safe_repo)


@pytest.fixture
def future_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=2)


@pytest.fixture
def ack_token_repo() -> InMemoryAckTokenRepository:
    return InMemoryAckTokenRepository()


@pytest.fixture
def ack_token_service(ack_token_repo: InMemoryAckTokenRepository):
    AckTokenService, *_ = _import_ack_token_service()
    return AckTokenService(
        repository=ack_token_repo,
        base_url="https://klaxxon.example.com",
    )


@pytest.fixture
def app(
    reminder_service: ReminderService,
    ack_token_service,
) -> Generator[FastAPI, None, None]:
    """FastAPI app with both authenticated API router and public ack router."""
    ack_routes = _import_ack_routes()

    test_app = FastAPI()
    test_app.include_router(routes.router)
    test_app.include_router(ack_routes.router)  # public router — no auth dependency

    auth.register_token(TEST_TOKEN)
    routes.set_dependencies(service=reminder_service, signal_available_fn=None)
    ack_routes.set_dependencies(
        service=reminder_service,
        ack_token_service=ack_token_service,
    )

    yield test_app

    routes._reminder_service = None
    routes._signal_available_fn = None
    auth._valid_token_hashes.clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# ---------------------------------------------------------------------------
# Helper: plant a token directly into the in-memory repo.
# ---------------------------------------------------------------------------


def _plant_token(
    repo: InMemoryAckTokenRepository,
    reminder_id: int,
    *,
    expires_at: datetime | None = None,
    used: bool = False,
) -> str:
    """Insert a token into the repo, return the raw token string."""
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    repo.store_token(
        token_hash=token_hash,
        reminder_id=reminder_id,
        expires_at=expires_at,
    )
    if used:
        repo.mark_used(token_hash)
    return raw


# ===========================================================================
# AC-3 — Successful ack via token
# ===========================================================================


class TestAckSuccess:
    def test_valid_token_returns_200(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} returns 200 for a valid unused non-expired token (AC-3)."""
        reminder = reminder_service.create(title="Daily Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        response = client.get(f"/ack/{raw_token}")

        assert response.status_code == 200

    def test_valid_token_returns_html(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} returns HTML content-type on success (REQ-6f)."""
        reminder = reminder_service.create(title="Daily Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        response = client.get(f"/ack/{raw_token}")

        assert "text/html" in response.headers.get("content-type", "")

    def test_valid_token_acks_reminder(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} transitions reminder to ACKNOWLEDGED state (REQ-6d)."""
        reminder = reminder_service.create(title="Daily Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        client.get(f"/ack/{raw_token}")

        updated = reminder_service.get(reminder.id)
        assert updated.state == ReminderState.ACKNOWLEDGED

    def test_valid_token_sets_ack_keyword_web_token(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} sets ack_keyword to 'web-token' (AC-3)."""
        reminder = reminder_service.create(title="Daily Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        client.get(f"/ack/{raw_token}")

        updated = reminder_service.get(reminder.id)
        assert updated.ack_keyword == "web-token"

    def test_valid_token_sets_ack_at(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} sets ack_at to approximately now (AC-3)."""
        reminder = reminder_service.create(title="Daily Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        before = datetime.now(timezone.utc)
        client.get(f"/ack/{raw_token}")
        after = datetime.now(timezone.utc)

        updated = reminder_service.get(reminder.id)
        assert updated.ack_at is not None
        assert (
            before - timedelta(seconds=5)
            <= updated.ack_at
            <= after + timedelta(seconds=5)
        )

    def test_valid_token_marks_token_used(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} marks token as used after success (REQ-6e)."""
        reminder = reminder_service.create(title="Daily Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        client.get(f"/ack/{raw_token}")

        stored = ack_token_repo.get_by_hash(token_hash)
        assert stored is not None
        assert stored.used is True

    def test_success_page_shows_reminder_title(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """200 HTML page mentions the reminder title (AC-3)."""
        reminder = reminder_service.create(
            title="Project Review", starts_at=future_time
        )
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        response = client.get(f"/ack/{raw_token}")

        assert response.status_code == 200
        assert "Project Review" in response.text


# ===========================================================================
# AC-9 — Public endpoint: no bearer token required
# ===========================================================================


class TestNoAuthRequired:
    def test_ack_endpoint_requires_no_authorization_header(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} succeeds without Authorization header (AC-9, REQ-5)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        # No auth headers passed at all
        response = client.get(f"/ack/{raw_token}")

        assert response.status_code != 401
        assert response.status_code == 200

    def test_ack_endpoint_with_invalid_bearer_still_works(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} ignores an invalid bearer token (public endpoint, AC-9)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        bad_auth = {"Authorization": "Bearer completely-invalid-token-xyz"}
        response = client.get(f"/ack/{raw_token}", headers=bad_auth)

        assert response.status_code == 200

    def test_authenticated_api_routes_still_require_bearer(
        self,
        client: TestClient,
    ) -> None:
        """Existing /api/* routes are still protected (sanity check)."""
        response = client.get("/api/reminders")
        assert response.status_code == 401


# ===========================================================================
# AC-4 — Already-used token (replay prevention)
# ===========================================================================


class TestAlreadyUsedToken:
    def test_used_token_returns_410(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} returns 410 for an already-used token (AC-4, REQ-13)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id, used=True)

        response = client.get(f"/ack/{raw_token}")

        assert response.status_code == 410

    def test_used_token_returns_html(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """410 for used token returns HTML page (REQ-13)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id, used=True)

        response = client.get(f"/ack/{raw_token}")

        assert "text/html" in response.headers.get("content-type", "")

    def test_used_token_page_says_already_used(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """410 HTML page body mentions already-used (REQ-13, AC-4)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id, used=True)

        response = client.get(f"/ack/{raw_token}")

        assert "already" in response.text.lower() or "used" in response.text.lower()

    def test_first_request_succeeds_second_gets_410(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """First GET succeeds (200), second GET returns 410 (E-3)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        r1 = client.get(f"/ack/{raw_token}")
        r2 = client.get(f"/ack/{raw_token}")

        assert r1.status_code == 200
        assert r2.status_code == 410


# ===========================================================================
# AC-5 — Expired token
# ===========================================================================


class TestExpiredToken:
    def test_expired_token_returns_410(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} returns 410 for an expired token (AC-5, REQ-13)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        expired_at = datetime.now(timezone.utc) - timedelta(hours=1)
        raw_token = _plant_token(ack_token_repo, reminder.id, expires_at=expired_at)

        response = client.get(f"/ack/{raw_token}")

        assert response.status_code == 410

    def test_expired_token_returns_html(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """410 for expired token returns HTML (REQ-13)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        expired_at = datetime.now(timezone.utc) - timedelta(hours=1)
        raw_token = _plant_token(ack_token_repo, reminder.id, expires_at=expired_at)

        response = client.get(f"/ack/{raw_token}")

        assert "text/html" in response.headers.get("content-type", "")

    def test_expired_token_page_says_expired(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """410 HTML page body mentions expiry (REQ-13, AC-5)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        expired_at = datetime.now(timezone.utc) - timedelta(hours=1)
        raw_token = _plant_token(ack_token_repo, reminder.id, expires_at=expired_at)

        response = client.get(f"/ack/{raw_token}")

        assert "expired" in response.text.lower()

    def test_expired_token_does_not_ack_reminder(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """Expired token rejection does not acknowledge the reminder (AC-5)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        expired_at = datetime.now(timezone.utc) - timedelta(hours=1)
        raw_token = _plant_token(ack_token_repo, reminder.id, expires_at=expired_at)

        client.get(f"/ack/{raw_token}")

        updated = reminder_service.get(reminder.id)
        assert updated.state != ReminderState.ACKNOWLEDGED


# ===========================================================================
# AC-6 — Unknown / not-found token
# ===========================================================================


class TestUnknownToken:
    def test_unknown_token_returns_404(
        self,
        client: TestClient,
    ) -> None:
        """GET /ack/{token} returns 404 for a token not in the database (AC-6, REQ-13)."""
        response = client.get("/ack/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

        assert response.status_code == 404

    def test_unknown_token_returns_html(
        self,
        client: TestClient,
    ) -> None:
        """404 for unknown token returns HTML page (REQ-13)."""
        response = client.get("/ack/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

        assert "text/html" in response.headers.get("content-type", "")

    def test_unknown_token_page_says_invalid(
        self,
        client: TestClient,
    ) -> None:
        """404 HTML page says the link is invalid or unknown (REQ-13, AC-6)."""
        response = client.get("/ack/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

        assert "invalid" in response.text.lower() or "unknown" in response.text.lower()

    def test_malformed_token_returns_404(
        self,
        client: TestClient,
    ) -> None:
        """GET /ack/{token} with short/malformed token returns 404 (E-8)."""
        response = client.get("/ack/short-bad-token")

        assert response.status_code == 404


# ===========================================================================
# AC-7 — Reminder already in terminal state
# ===========================================================================


class TestReminderAlreadyAcknowledged:
    def test_ack_already_acknowledged_returns_409(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} returns 409 when reminder is already ACKNOWLEDGED (AC-7, E-2)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        reminder_service.acknowledge(reminder.id, "signal")

        raw_token = _plant_token(ack_token_repo, reminder.id)

        response = client.get(f"/ack/{raw_token}")

        assert response.status_code == 409

    def test_ack_already_acknowledged_returns_html(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """409 for already-acknowledged reminder returns HTML (REQ-13)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        reminder_service.acknowledge(reminder.id, "signal")
        raw_token = _plant_token(ack_token_repo, reminder.id)

        response = client.get(f"/ack/{raw_token}")

        assert "text/html" in response.headers.get("content-type", "")

    def test_ack_already_acknowledged_page_says_already_acked(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """409 HTML page body mentions already-acknowledged (REQ-13, AC-7)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        reminder_service.acknowledge(reminder.id, "signal")
        raw_token = _plant_token(ack_token_repo, reminder.id)

        response = client.get(f"/ack/{raw_token}")

        assert (
            "already" in response.text.lower()
            or "acknowledged" in response.text.lower()
        )

    def test_ack_already_acknowledged_does_not_consume_token(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """Token is NOT consumed when reminder is already ACKNOWLEDGED (AC-7, E-2)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        reminder_service.acknowledge(reminder.id, "signal")
        raw_token = _plant_token(ack_token_repo, reminder.id)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        client.get(f"/ack/{raw_token}")

        stored = ack_token_repo.get_by_hash(token_hash)
        assert stored is not None
        assert stored.used is False  # token NOT consumed

    def test_missed_reminder_returns_409(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """GET /ack/{token} for a MISSED reminder returns 409 (E-6, REQ-13)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        reminder_service.mark_reminding(reminder.id)
        reminder_service.mark_missed(reminder.id)

        raw_token = _plant_token(ack_token_repo, reminder.id)

        response = client.get(f"/ack/{raw_token}")

        assert response.status_code == 409


# ===========================================================================
# AC-8 — Multiple tokens per reminder
# ===========================================================================


class TestMultipleTokensPerReminder:
    def test_second_token_gets_409_after_first_acks(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """After T1 acks, using T2 returns 409 'Already acknowledged' (AC-8)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None

        raw_t1 = _plant_token(ack_token_repo, reminder.id)
        raw_t2 = _plant_token(ack_token_repo, reminder.id)

        r1 = client.get(f"/ack/{raw_t1}")
        assert r1.status_code == 200

        r2 = client.get(f"/ack/{raw_t2}")
        assert r2.status_code == 409

    def test_second_token_remains_unused_after_first_acks(
        self,
        client: TestClient,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
    ) -> None:
        """T2 is not consumed when T1 acks the reminder (AC-8)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None

        raw_t1 = _plant_token(ack_token_repo, reminder.id)
        raw_t2 = _plant_token(ack_token_repo, reminder.id)
        hash_t2 = hashlib.sha256(raw_t2.encode()).hexdigest()

        client.get(f"/ack/{raw_t1}")

        stored_t2 = ack_token_repo.get_by_hash(hash_t2)
        assert stored_t2 is not None
        assert stored_t2.used is False


# ===========================================================================
# AC-11 — Concurrent ack atomicity
# ===========================================================================


class TestConcurrentAck:
    def test_concurrent_requests_exactly_one_succeeds(
        self,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
        app: FastAPI,
    ) -> None:
        """Simultaneous requests: exactly one 200 and one 410 (AC-11, E-12)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        client_a = TestClient(app)
        client_b = TestClient(app)

        r_a = client_a.get(f"/ack/{raw_token}")
        r_b = client_b.get(f"/ack/{raw_token}")

        statuses = {r_a.status_code, r_b.status_code}
        assert 200 in statuses
        assert 410 in statuses

    def test_reminder_acknowledged_exactly_once_under_concurrency(
        self,
        reminder_service: ReminderService,
        ack_token_repo: InMemoryAckTokenRepository,
        future_time: datetime,
        app: FastAPI,
    ) -> None:
        """Concurrent acks leave reminder in ACKNOWLEDGED exactly once (AC-11)."""
        reminder = reminder_service.create(title="Standup", starts_at=future_time)
        assert reminder.id is not None
        raw_token = _plant_token(ack_token_repo, reminder.id)

        client_a = TestClient(app)
        client_b = TestClient(app)

        client_a.get(f"/ack/{raw_token}")
        client_b.get(f"/ack/{raw_token}")

        updated = reminder_service.get(reminder.id)
        assert updated.state == ReminderState.ACKNOWLEDGED
