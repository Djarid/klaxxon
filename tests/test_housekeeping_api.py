"""API route tests for POST /api/housekeeping/cleanup.

Tests every API acceptance criterion from .Claude/plans/age-out-acknowledged.md:
  AC-10 — POST /api/housekeeping/cleanup with retention_days override
  AC-11 — dry_run=true returns counts without deleting
  AC-12 — (log assertions in test_housekeeping_service.py; response schema tested here)
  AC-5  — active reminders are never deleted (via API path)
  Auth  — endpoint requires bearer token

All tests are written BEFORE the implementation exists and MUST FAIL until
the implementation is correct.  Module-level imports use lazy helpers so
that test collection succeeds even when the modules don't yet exist.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Lazy importers for not-yet-existing modules
# ---------------------------------------------------------------------------


def _import_housekeeping():
    try:
        from src.services.housekeeping_service import CleanupResult, HousekeepingService

        return HousekeepingService, CleanupResult
    except ImportError as exc:
        pytest.fail(f"src.services.housekeeping_service not yet implemented: {exc}")


def _import_housekeeping_routes():
    """Return the housekeeping router module."""
    try:
        # The spec says the route is at POST /api/housekeeping/cleanup.
        # It may be added to the existing routes.py or a separate module.
        # We check routes.py first (it lives on the /api prefix router).
        import src.api.routes as r

        # Verify the route exists — POST /api/housekeeping/cleanup
        path_ops = {route.path: route for route in r.router.routes}
        if "/housekeeping/cleanup" not in path_ops:
            pytest.fail(
                "POST /api/housekeeping/cleanup not registered on routes.router — "
                "expected in src/api/routes.py under /api prefix"
            )
        return r
    except Exception as exc:
        pytest.fail(f"Could not verify housekeeping route registration: {exc}")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-bearer-token-housekeeping"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_ago(days: float = 0, hours: float = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days, hours=hours)


def _utc_future(hours: float = 2) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _backdate_reminder(
    repo: SqliteReminderRepository,
    reminder_id: int,
    *,
    updated_at: Optional[datetime] = None,
    ack_at: Optional[datetime] = None,
) -> None:
    conn = repo._get_conn()
    if updated_at is not None:
        conn.execute(
            "UPDATE reminders SET updated_at = ? WHERE id = ?",
            (updated_at.isoformat(), reminder_id),
        )
    if ack_at is not None:
        conn.execute(
            "UPDATE reminders SET ack_at = ? WHERE id = ?",
            (ack_at.isoformat(), reminder_id),
        )
    conn.commit()


def _insert_ack_token_orphan(
    repo: SqliteReminderRepository,
    *,
    reminder_id: int,
    token_hash: str,
    expires_at: datetime,
    used: bool = False,
) -> None:
    conn = repo._get_conn()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """INSERT INTO ack_tokens (token_hash, reminder_id, created_at, expires_at, used)
           VALUES (?, ?, ?, ?, ?)""",
        (
            token_hash,
            reminder_id,
            datetime.now(timezone.utc).isoformat(),
            expires_at.isoformat(),
            1 if used else 0,
        ),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _count_reminders(repo: SqliteReminderRepository) -> int:
    row = repo._get_conn().execute("SELECT COUNT(*) AS c FROM reminders").fetchone()
    return row["c"]


def _count_ack_tokens(repo: SqliteReminderRepository) -> int:
    row = repo._get_conn().execute("SELECT COUNT(*) AS c FROM ack_tokens").fetchone()
    return row["c"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hk_repo() -> SqliteReminderRepository:
    """Thread-safe in-memory SQLite repo for housekeeping API tests."""
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
def hk_reminder_service(hk_repo: SqliteReminderRepository) -> ReminderService:
    return ReminderService(hk_repo)


@pytest.fixture
def hk_housekeeping_service(hk_repo: SqliteReminderRepository):
    HousekeepingService, _ = _import_housekeeping()
    return HousekeepingService(repository=hk_repo, retention_days=30)


@pytest.fixture
def app(
    hk_reminder_service: ReminderService,
    hk_housekeeping_service,
) -> Generator[FastAPI, None, None]:
    """FastAPI app with housekeeping route registered and dependencies injected."""
    test_app = FastAPI()
    test_app.include_router(routes.router)

    auth.register_token(TEST_TOKEN)

    # The route set_dependencies must accept housekeeping_service
    try:
        routes.set_dependencies(
            service=hk_reminder_service,
            signal_available_fn=None,
            housekeeping_service=hk_housekeeping_service,
        )
    except TypeError:
        # Fallback: if set_dependencies doesn't yet accept housekeeping_service,
        # at least set what we can so auth/service works, but housekeeping route
        # will 503 (which is still a meaningful failure — not a collection error).
        routes.set_dependencies(service=hk_reminder_service, signal_available_fn=None)

    yield test_app

    # Cleanup module-level globals
    routes._reminder_service = None
    routes._signal_available_fn = None
    try:
        routes._housekeeping_service = None
    except AttributeError:
        pass
    auth._valid_token_hashes.clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# ===========================================================================
# AC-10: POST /api/housekeeping/cleanup — basic success
# ===========================================================================


class TestAC10_ManualCleanupEndpoint:
    """AC-10: POST /api/housekeeping/cleanup with retention_days override returns 200."""

    def test_cleanup_returns_200(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup returns 200 OK."""
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )

    def test_cleanup_with_retention_days_override(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup with retention_days=7 deletes 8-day-old reminder."""
        r = hk_repo.create(Reminder(title="Eight Day Old", starts_at=_utc_future()))
        assert r.id is not None
        hk_repo.update_state(
            r.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack", ack_at=_utc_ago(days=8)
        )
        _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=8))

        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 7},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_acknowledged"] >= 1, (
            "Expected at least 1 acknowledged reminder deleted with retention_days=7"
        )
        assert hk_repo.get(r.id) is None, (
            "Reminder must be hard-deleted from the database"
        )

    def test_cleanup_response_schema(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """Response includes all required CleanupResponse fields."""
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()

        required_fields = [
            "dry_run",
            "retention_days",
            "cutoff",
            "deleted_reminders",
            "deleted_acknowledged",
            "deleted_skipped",
            "deleted_missed",
            "deleted_orphan_tokens",
        ]
        for field in required_fields:
            assert field in data, (
                f"Response missing required field '{field}'. "
                f"Got keys: {list(data.keys())}"
            )

    def test_cleanup_response_dry_run_false_by_default(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """Response dry_run field is False when query param not specified."""
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is False

    def test_cleanup_response_contains_retention_days_used(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """Response retention_days reflects the effective retention window used."""
        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 14},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["retention_days"] == 14, (
            f"Response retention_days must be 14 (the override), got {data['retention_days']}"
        )

    def test_cleanup_response_contains_cutoff_timestamp(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """Response cutoff is a valid UTC datetime string."""
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        cutoff = data["cutoff"]
        assert cutoff is not None, "cutoff must be present in response"
        # Should be parseable as a datetime
        try:
            parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            assert parsed < datetime.now(timezone.utc), (
                "cutoff must be in the past (now minus retention_days)"
            )
        except (ValueError, AttributeError) as exc:
            pytest.fail(f"cutoff '{cutoff}' is not a valid datetime: {exc}")

    def test_cleanup_deletes_old_reminders_all_terminal_states(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup deletes reminders in all three terminal states."""
        # ACKNOWLEDGED
        r_ack = hk_repo.create(Reminder(title="Old Ack", starts_at=_utc_future()))
        assert r_ack.id is not None
        hk_repo.update_state(
            r_ack.id,
            ReminderState.ACKNOWLEDGED,
            ack_keyword="ack",
            ack_at=_utc_ago(days=31),
        )
        _backdate_reminder(hk_repo, r_ack.id, updated_at=_utc_ago(days=31))

        # SKIPPED
        r_skip = hk_repo.create(Reminder(title="Old Skip", starts_at=_utc_future()))
        assert r_skip.id is not None
        hk_repo.update_state(r_skip.id, ReminderState.SKIPPED)
        _backdate_reminder(hk_repo, r_skip.id, updated_at=_utc_ago(days=31))

        # MISSED
        r_miss = hk_repo.create(Reminder(title="Old Miss", starts_at=_utc_future()))
        assert r_miss.id is not None
        hk_repo.update_state(r_miss.id, ReminderState.REMINDING)
        hk_repo.update_state(r_miss.id, ReminderState.MISSED)
        _backdate_reminder(hk_repo, r_miss.id, updated_at=_utc_ago(days=31))

        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 30},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_acknowledged"] == 1
        assert data["deleted_skipped"] == 1
        assert data["deleted_missed"] == 1
        assert data["deleted_reminders"] == 3

    def test_cleanup_empty_db_returns_all_zeros(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """POST /api/housekeeping/cleanup on empty database returns all-zero counts."""
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_reminders"] == 0
        assert data["deleted_acknowledged"] == 0
        assert data["deleted_skipped"] == 0
        assert data["deleted_missed"] == 0
        assert data["deleted_orphan_tokens"] == 0


# ===========================================================================
# AC-11: dry_run=true returns counts without deleting
# ===========================================================================


class TestAC11_DryRun:
    """AC-11: dry_run=true returns what would be deleted without actually deleting."""

    def test_dry_run_returns_correct_counts(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup?dry_run=true returns correct counts."""
        # 5 acknowledged + 3 skipped
        for i in range(5):
            r = hk_repo.create(Reminder(title=f"Old Ack {i}", starts_at=_utc_future()))
            assert r.id is not None
            hk_repo.update_state(
                r.id,
                ReminderState.ACKNOWLEDGED,
                ack_keyword="ack",
                ack_at=_utc_ago(days=31),
            )
            _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=31))

        for i in range(3):
            r = hk_repo.create(Reminder(title=f"Old Skip {i}", starts_at=_utc_future()))
            assert r.id is not None
            hk_repo.update_state(r.id, ReminderState.SKIPPED)
            _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=31))

        response = client.post(
            "/api/housekeeping/cleanup?dry_run=true",
            json={},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_acknowledged"] == 5, (
            f"Expected 5 acknowledged to be counted in dry_run, got {data['deleted_acknowledged']}"
        )
        assert data["deleted_skipped"] == 3, (
            f"Expected 3 skipped to be counted in dry_run, got {data['deleted_skipped']}"
        )
        assert data["dry_run"] is True

    def test_dry_run_does_not_delete_rows(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """dry_run=true must not delete any rows from the database."""
        r = hk_repo.create(Reminder(title="Old Ack Dry", starts_at=_utc_future()))
        assert r.id is not None
        hk_repo.update_state(
            r.id,
            ReminderState.ACKNOWLEDGED,
            ack_keyword="ack",
            ack_at=_utc_ago(days=31),
        )
        _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=31))

        client.post(
            "/api/housekeeping/cleanup?dry_run=true",
            json={},
            headers=auth_headers,
        )

        assert hk_repo.get(r.id) is not None, (
            "dry_run=true must not delete any rows from the database"
        )
        assert _count_reminders(hk_repo) == 1, (
            "dry_run=true must leave all reminders intact"
        )

    def test_dry_run_response_has_dry_run_true(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """dry_run=true is reflected in the response schema."""
        response = client.post(
            "/api/housekeeping/cleanup?dry_run=true",
            json={},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True, (
            f"Response dry_run must be True when ?dry_run=true, got {data['dry_run']}"
        )

    def test_dry_run_false_actually_deletes(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """?dry_run=false (explicit) performs actual deletion (control test)."""
        r = hk_repo.create(Reminder(title="Old Ack Non-Dry", starts_at=_utc_future()))
        assert r.id is not None
        hk_repo.update_state(
            r.id,
            ReminderState.ACKNOWLEDGED,
            ack_keyword="ack",
            ack_at=_utc_ago(days=31),
        )
        _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=31))

        response = client.post(
            "/api/housekeeping/cleanup?dry_run=false",
            json={},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is False
        assert hk_repo.get(r.id) is None, "?dry_run=false must perform actual deletion"

    def test_dry_run_with_retention_days_override(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """dry_run=true can be combined with retention_days override."""
        r = hk_repo.create(Reminder(title="Two Day Old", starts_at=_utc_future()))
        assert r.id is not None
        hk_repo.update_state(
            r.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack", ack_at=_utc_ago(days=2)
        )
        _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=2))

        response = client.post(
            "/api/housekeeping/cleanup?dry_run=true",
            json={"retention_days": 1},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["dry_run"] is True
        assert data["retention_days"] == 1
        assert data["deleted_acknowledged"] == 1, (
            "dry_run + retention_days=1 should count the 2-day-old reminder"
        )
        # But nothing should actually be deleted
        assert hk_repo.get(r.id) is not None, (
            "dry_run=true must not delete even with retention_days override"
        )


# ===========================================================================
# Authentication Tests
# ===========================================================================


class TestAuthRequired:
    """POST /api/housekeeping/cleanup requires bearer token."""

    def test_no_auth_returns_401(self, client: TestClient) -> None:
        """Request without Authorization header returns 401."""
        response = client.post("/api/housekeeping/cleanup", json={})
        assert response.status_code == 401, (
            f"Expected 401 without auth, got {response.status_code}"
        )

    def test_invalid_token_returns_401(self, client: TestClient) -> None:
        """Request with wrong token returns 401."""
        bad_headers = {"Authorization": "Bearer totally-wrong-token"}
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=bad_headers
        )
        assert response.status_code == 401, (
            f"Expected 401 with invalid token, got {response.status_code}"
        )

    def test_valid_token_is_accepted(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        """Valid bearer token is accepted."""
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )
        # Must not be 401 (auth failure) — business errors (500, 503) are separate
        assert response.status_code != 401, "Valid bearer token must not return 401"


# ===========================================================================
# Validation: CleanupRequest schema
# ===========================================================================


class TestCleanupRequestValidation:
    """POST /api/housekeeping/cleanup request body validation."""

    def test_omitted_body_uses_global_defaults(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """Empty body (no retention_days) uses global config."""
        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        # Default global is 30 days; response should reflect that
        assert data["retention_days"] == 30, (
            f"Omitted retention_days must default to global config (30), "
            f"got {data['retention_days']}"
        )

    def test_null_body_uses_global_defaults(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """No body at all also uses global config."""
        response = client.post("/api/housekeeping/cleanup", headers=auth_headers)
        assert response.status_code in (200, 422), (
            "Null body should either succeed (200) or return validation error (422), "
            f"not {response.status_code}"
        )

    def test_retention_days_below_minimum_returns_422(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """retention_days=0 in request body is invalid (spec: ge=1)."""
        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 0},
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"retention_days=0 in body must fail validation (spec: ge=1), "
            f"got {response.status_code}"
        )

    def test_retention_days_above_maximum_returns_422(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """retention_days=3651 in request body exceeds spec maximum (le=3650)."""
        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 3651},
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"retention_days=3651 must fail validation (spec: le=3650), "
            f"got {response.status_code}"
        )

    def test_retention_days_at_minimum_is_valid(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """retention_days=1 is the minimum valid value."""
        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 1},
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"retention_days=1 must be accepted as valid, got {response.status_code}"
        )

    def test_retention_days_at_maximum_is_valid(
        self,
        client: TestClient,
        auth_headers: dict,
    ) -> None:
        """retention_days=3650 is the maximum valid value."""
        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 3650},
            headers=auth_headers,
        )
        assert response.status_code == 200, (
            f"retention_days=3650 must be accepted as valid, got {response.status_code}"
        )


# ===========================================================================
# AC-5 via API: Active reminders are never deleted
# ===========================================================================


class TestAC5ViaAPI:
    """AC-5 via the API endpoint: PENDING and REMINDING are never touched."""

    def test_api_does_not_delete_pending_reminders(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup never deletes PENDING reminders."""
        r = hk_repo.create(Reminder(title="Old Pending", starts_at=_utc_future()))
        assert r.id is not None
        _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=90))

        client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 1},
            headers=auth_headers,
        )

        assert hk_repo.get(r.id) is not None, (
            "API cleanup must never delete PENDING reminders"
        )

    def test_api_does_not_delete_reminding_reminders(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup never deletes REMINDING reminders."""
        r = hk_repo.create(Reminder(title="Old Reminding", starts_at=_utc_future()))
        assert r.id is not None
        hk_repo.update_state(r.id, ReminderState.REMINDING)
        _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=60))

        client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 1},
            headers=auth_headers,
        )

        assert hk_repo.get(r.id) is not None, (
            "API cleanup must never delete REMINDING reminders"
        )


# ===========================================================================
# AC-9 via API: Endpoint works even when retention_days=0 (auto-cleanup disabled)
# ===========================================================================


class TestAC9ViaAPI:
    """AC-9: Manual API endpoint must work even when auto-cleanup is disabled."""

    def test_manual_endpoint_works_when_global_retention_days_is_zero(
        self,
        hk_repo: SqliteReminderRepository,
        hk_reminder_service: ReminderService,
    ) -> None:
        """POST /api/housekeeping/cleanup works with retention_days=0 in global config.

        The endpoint must still work when triggered manually; only the scheduler
        skips automatic runs.
        """
        HousekeepingService, _ = _import_housekeeping()
        # Service configured with retention_days=0 (auto-disabled)
        svc_zero = HousekeepingService(repository=hk_repo, retention_days=0)

        test_app = FastAPI()
        test_app.include_router(routes.router)
        auth.register_token(TEST_TOKEN + "_zero")

        try:
            routes.set_dependencies(
                service=hk_reminder_service,
                signal_available_fn=None,
                housekeeping_service=svc_zero,
            )
        except TypeError:
            routes.set_dependencies(
                service=hk_reminder_service, signal_available_fn=None
            )

        client = TestClient(test_app)
        headers = {"Authorization": f"Bearer {TEST_TOKEN}_zero"}

        # The manual endpoint must accept a retention_days override in the body
        r = hk_repo.create(Reminder(title="Old For Manual", starts_at=_utc_future()))
        assert r.id is not None
        hk_repo.update_state(
            r.id, ReminderState.ACKNOWLEDGED, ack_keyword="ack", ack_at=_utc_ago(days=8)
        )
        _backdate_reminder(hk_repo, r.id, updated_at=_utc_ago(days=8))

        response = client.post(
            "/api/housekeeping/cleanup",
            json={"retention_days": 7},  # manual override
            headers=headers,
        )

        # Manual endpoint must work regardless of global retention_days setting
        assert response.status_code == 200, (
            f"Manual cleanup endpoint must return 200 even when global retention_days=0, "
            f"got {response.status_code}: {response.text}"
        )

        # Cleanup
        routes._reminder_service = None
        try:
            routes._housekeeping_service = None
        except AttributeError:
            pass
        auth._valid_token_hashes.discard(
            __import__("hashlib").sha256((TEST_TOKEN + "_zero").encode()).hexdigest()
        )


# ===========================================================================
# AC-6 + AC-7 via API: Orphan tokens and used+expired tokens
# ===========================================================================


class TestOrphanAndExpiredTokensViaAPI:
    """Orphan and used+expired token cleanup via the API endpoint."""

    def test_api_cleans_orphan_tokens(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup deletes orphan ack_tokens."""
        _insert_ack_token_orphan(
            hk_repo,
            reminder_id=77777,
            token_hash="apiorphan" + "h" * 55,
            expires_at=_utc_future(),
        )
        assert _count_ack_tokens(hk_repo) == 1

        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_orphan_tokens"] == 1, (
            f"Expected 1 orphan token deleted, got {data['deleted_orphan_tokens']}"
        )
        assert _count_ack_tokens(hk_repo) == 0

    def test_api_cleans_used_expired_tokens(
        self,
        client: TestClient,
        auth_headers: dict,
        hk_repo: SqliteReminderRepository,
    ) -> None:
        """POST /api/housekeeping/cleanup deletes used+expired ack_tokens."""
        live_reminder = hk_repo.create(
            Reminder(title="Live Reminder", starts_at=_utc_future())
        )
        assert live_reminder.id is not None
        # Store a used+expired token
        hk_repo.store_token(
            "usedexp_api" + "i" * 53, live_reminder.id, _utc_ago(hours=2)
        )
        hk_repo.mark_used("usedexp_api" + "i" * 53)

        response = client.post(
            "/api/housekeeping/cleanup", json={}, headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["deleted_orphan_tokens"] >= 1, (
            "used+expired tokens must be counted in deleted_orphan_tokens"
        )
        assert _count_ack_tokens(hk_repo) == 0, "used+expired token must be deleted"
        # Parent reminder must survive
        assert hk_repo.get(live_reminder.id) is not None, (
            "Parent reminder must not be deleted by token cleanup"
        )
