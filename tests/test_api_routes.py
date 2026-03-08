"""Comprehensive tests for Klaxxon API routes.

Tests the thin HTTP layer that delegates to MeetingService.
Uses FastAPI TestClient (synchronous, no @pytest.mark.asyncio needed).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api import auth, routes
from src.models.meeting import MeetingState
from src.repository.sqlite import SqliteMeetingRepository
from src.services.meeting_service import MeetingService

# Test bearer token
TEST_TOKEN = "test-token-12345"


@pytest.fixture
def thread_safe_repo() -> SqliteMeetingRepository:
    """In-memory SQLite repository with thread-safety for TestClient.

    TestClient runs in a different thread, so we need check_same_thread=False.
    """
    repo = SqliteMeetingRepository(":memory:")
    # Replace the connection with a thread-safe one
    if repo._conn:
        repo._conn.close()
    repo._conn = sqlite3.connect(":memory:", check_same_thread=False)
    repo._conn.row_factory = sqlite3.Row
    repo._conn.execute("PRAGMA journal_mode=WAL")
    repo._conn.execute("PRAGMA foreign_keys=ON")
    repo._ensure_schema()
    return repo


@pytest.fixture
def thread_safe_service(thread_safe_repo: SqliteMeetingRepository) -> MeetingService:
    """Meeting service with thread-safe repository for API tests."""
    return MeetingService(thread_safe_repo)


@pytest.fixture
def app(thread_safe_service: MeetingService) -> Generator[FastAPI, None, None]:
    """FastAPI app with routes and injected service."""
    # Create app
    test_app = FastAPI()
    test_app.include_router(routes.router)

    # Register test token
    auth.register_token(TEST_TOKEN)

    # Inject service dependency
    routes.set_dependencies(service=thread_safe_service, signal_available_fn=None)

    yield test_app

    # Cleanup: clear module-level globals
    routes._meeting_service = None
    routes._signal_available_fn = None
    auth._valid_token_hashes.clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Test client with authentication."""
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Authorization headers with valid bearer token."""
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def future_time() -> datetime:
    """A datetime 2 hours in the future (timezone-aware)."""
    return datetime.now(timezone.utc) + timedelta(hours=2)


@pytest.fixture
def past_time() -> datetime:
    """A datetime 2 hours in the past (timezone-aware)."""
    return datetime.now(timezone.utc) - timedelta(hours=2)


# ============================================================================
# POST /api/meetings - Create Meeting
# ============================================================================


def test_create_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings returns 201 with MeetingResponse JSON."""
    payload = {
        "title": "Team Standup",
        "starts_at": future_time.isoformat(),
        "duration_min": 30,
        "link": "https://meet.example.com/standup",
        "source": "api",
    }

    response = client.post("/api/meetings", json=payload, headers=auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Team Standup"
    assert data["duration_min"] == 30
    assert data["link"] == "https://meet.example.com/standup"
    assert data["source"] == "api"
    assert data["state"] == "pending"
    assert data["id"] is not None
    assert data["created_at"] is not None


def test_create_meeting_past_date(
    client: TestClient, auth_headers: dict, past_time: datetime
) -> None:
    """POST /api/meetings with past date returns 400."""
    payload = {
        "title": "Past Meeting",
        "starts_at": past_time.isoformat(),
    }

    response = client.post("/api/meetings", json=payload, headers=auth_headers)

    assert response.status_code == 400
    assert "past" in response.json()["detail"].lower()


def test_create_meeting_duplicate(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings with duplicate returns 409."""
    payload = {
        "title": "Duplicate Meeting",
        "starts_at": future_time.isoformat(),
    }

    # Create first meeting
    response1 = client.post("/api/meetings", json=payload, headers=auth_headers)
    assert response1.status_code == 201

    # Attempt duplicate (same title, within 30 min window)
    response2 = client.post("/api/meetings", json=payload, headers=auth_headers)
    assert response2.status_code == 409
    assert "already exists" in response2.json()["detail"].lower()


def test_create_meeting_missing_title(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings without title returns 422 validation error."""
    payload = {
        "starts_at": future_time.isoformat(),
    }

    response = client.post("/api/meetings", json=payload, headers=auth_headers)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(err["loc"] == ["body", "title"] for err in errors)


def test_create_meeting_invalid_duration(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings with invalid duration returns 422."""
    payload = {
        "title": "Invalid Duration",
        "starts_at": future_time.isoformat(),
        "duration_min": 0,  # Must be >= 1
    }

    response = client.post("/api/meetings", json=payload, headers=auth_headers)

    assert response.status_code == 422


# ============================================================================
# GET /api/meetings - List Meetings
# ============================================================================


def test_list_meetings_empty(client: TestClient, auth_headers: dict) -> None:
    """GET /api/meetings returns empty list when no meetings exist."""
    response = client.get("/api/meetings", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["meetings"] == []
    assert data["count"] == 0


def test_list_meetings_all(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/meetings returns all meetings with count."""
    # Create two meetings
    payload1 = {"title": "Meeting 1", "starts_at": future_time.isoformat()}
    payload2 = {
        "title": "Meeting 2",
        "starts_at": (future_time + timedelta(hours=1)).isoformat(),
    }

    client.post("/api/meetings", json=payload1, headers=auth_headers)
    client.post("/api/meetings", json=payload2, headers=auth_headers)

    response = client.get("/api/meetings", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert len(data["meetings"]) == 2
    titles = {m["title"] for m in data["meetings"]}
    assert titles == {"Meeting 1", "Meeting 2"}


def test_list_meetings_filter_by_state(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/meetings?state=pending filters by state."""
    # Create a meeting
    payload = {"title": "Pending Meeting", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    # Acknowledge it (changes state to ACKNOWLEDGED)
    client.post(f"/api/meetings/{meeting_id}/ack", headers=auth_headers)

    # Create another pending meeting
    payload2 = {
        "title": "Still Pending",
        "starts_at": (future_time + timedelta(hours=1)).isoformat(),
    }
    client.post("/api/meetings", json=payload2, headers=auth_headers)

    # Filter by pending state
    response = client.get("/api/meetings?state=pending", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["meetings"][0]["title"] == "Still Pending"
    assert data["meetings"][0]["state"] == "pending"


# ============================================================================
# GET /api/meetings/{id} - Get Single Meeting
# ============================================================================


def test_get_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/meetings/{id} returns the meeting."""
    payload = {"title": "Get Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    response = client.get(f"/api/meetings/{meeting_id}", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == meeting_id
    assert data["title"] == "Get Me"


def test_get_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """GET /api/meetings/{id} returns 404 when meeting doesn't exist."""
    response = client.get("/api/meetings/99999", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ============================================================================
# POST /api/meetings/{id}/ack - Acknowledge Meeting
# ============================================================================


def test_ack_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings/{id}/ack returns updated meeting with ACKNOWLEDGED state."""
    payload = {"title": "Ack Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    response = client.post(f"/api/meetings/{meeting_id}/ack", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == meeting_id
    assert data["state"] == "acknowledged"
    assert data["ack_keyword"] == "ack"
    assert data["ack_at"] is not None


def test_ack_meeting_custom_keyword(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings/{id}/ack with custom keyword."""
    payload = {"title": "Custom Ack", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    ack_payload = {"keyword": "confirmed"}
    response = client.post(
        f"/api/meetings/{meeting_id}/ack", json=ack_payload, headers=auth_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "acknowledged"
    assert data["ack_keyword"] == "confirmed"


def test_ack_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """POST /api/meetings/{id}/ack returns 404 when meeting doesn't exist."""
    response = client.post("/api/meetings/99999/ack", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_ack_meeting_invalid_transition(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings/{id}/ack returns 409 for invalid state transition."""
    payload = {"title": "Double Ack", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    # First ack succeeds
    response1 = client.post(f"/api/meetings/{meeting_id}/ack", headers=auth_headers)
    assert response1.status_code == 200

    # Second ack fails (already acknowledged)
    response2 = client.post(f"/api/meetings/{meeting_id}/ack", headers=auth_headers)
    assert response2.status_code == 409


# ============================================================================
# POST /api/meetings/{id}/skip - Skip Meeting
# ============================================================================


def test_skip_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings/{id}/skip returns updated meeting with SKIPPED state."""
    payload = {"title": "Skip Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    response = client.post(f"/api/meetings/{meeting_id}/skip", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == meeting_id
    assert data["state"] == "skipped"


def test_skip_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """POST /api/meetings/{id}/skip returns 404 when meeting doesn't exist."""
    response = client.post("/api/meetings/99999/skip", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_skip_meeting_invalid_transition(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/meetings/{id}/skip returns 409 for invalid state transition."""
    payload = {"title": "Double Skip", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    # First skip succeeds
    response1 = client.post(f"/api/meetings/{meeting_id}/skip", headers=auth_headers)
    assert response1.status_code == 200

    # Second skip fails (already skipped)
    response2 = client.post(f"/api/meetings/{meeting_id}/skip", headers=auth_headers)
    assert response2.status_code == 409


# ============================================================================
# DELETE /api/meetings/{id} - Delete Meeting
# ============================================================================


def test_delete_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """DELETE /api/meetings/{id} returns 204 no content."""
    payload = {"title": "Delete Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/meetings", json=payload, headers=auth_headers)
    meeting_id = create_resp.json()["id"]

    response = client.delete(f"/api/meetings/{meeting_id}", headers=auth_headers)

    assert response.status_code == 204
    assert response.content == b""

    # Verify it's gone
    get_resp = client.get(f"/api/meetings/{meeting_id}", headers=auth_headers)
    assert get_resp.status_code == 404


def test_delete_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """DELETE /api/meetings/{id} returns 404 when meeting doesn't exist."""
    response = client.delete("/api/meetings/99999", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ============================================================================
# GET /api/health - Health Check
# ============================================================================


def test_health_check(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/health returns health info with counts."""
    # Create some meetings in different states
    payload1 = {"title": "Pending 1", "starts_at": future_time.isoformat()}
    payload2 = {
        "title": "Pending 2",
        "starts_at": (future_time + timedelta(hours=1)).isoformat(),
    }
    create_resp = client.post("/api/meetings", json=payload1, headers=auth_headers)
    client.post("/api/meetings", json=payload2, headers=auth_headers)

    # Acknowledge one (moves to ACKNOWLEDGED, not counted as pending)
    meeting_id = create_resp.json()["id"]
    client.post(f"/api/meetings/{meeting_id}/ack", headers=auth_headers)

    response = client.get("/api/health", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["db_ok"] is True
    assert data["signal_connected"] is False  # No signal_available_fn set
    assert data["meetings_pending"] == 1  # Only one pending left
    assert data["meetings_reminding"] == 0


def test_health_check_with_signal_fn(
    client: TestClient,
    auth_headers: dict,
    app: FastAPI,
    thread_safe_service: MeetingService,
) -> None:
    """GET /api/health with signal_available_fn returns signal status."""

    async def mock_signal_available() -> bool:
        return True

    # Re-inject with signal function
    routes.set_dependencies(
        service=thread_safe_service, signal_available_fn=mock_signal_available
    )

    response = client.get("/api/health", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["signal_connected"] is True


# ============================================================================
# Authentication Tests
# ============================================================================


def test_no_auth_token(client: TestClient, future_time: datetime) -> None:
    """Request without Authorization header returns 401."""
    payload = {"title": "No Auth", "starts_at": future_time.isoformat()}

    response = client.post("/api/meetings", json=payload)

    assert response.status_code == 401


def test_invalid_auth_token(client: TestClient, future_time: datetime) -> None:
    """Request with invalid token returns 401."""
    payload = {"title": "Bad Auth", "starts_at": future_time.isoformat()}
    bad_headers = {"Authorization": "Bearer invalid-token-xyz"}

    response = client.post("/api/meetings", json=payload, headers=bad_headers)

    assert response.status_code == 401
    assert "invalid" in response.json()["detail"].lower()


def test_malformed_auth_header(client: TestClient, future_time: datetime) -> None:
    """Request with malformed Authorization header returns 401."""
    payload = {"title": "Malformed Auth", "starts_at": future_time.isoformat()}
    bad_headers = {"Authorization": "NotBearer token"}

    response = client.post("/api/meetings", json=payload, headers=bad_headers)

    assert response.status_code == 401


# ============================================================================
# Service Not Initialised
# ============================================================================


def test_service_not_initialised() -> None:
    """Requests fail with 503 when service is not initialised."""
    # Create app without setting dependencies
    test_app = FastAPI()
    test_app.include_router(routes.router)
    auth.register_token(TEST_TOKEN)

    # Do NOT call set_dependencies()
    routes._meeting_service = None

    client = TestClient(test_app)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

    response = client.get("/api/meetings", headers=headers)

    assert response.status_code == 503
    assert "not initialised" in response.json()["detail"].lower()

    # Cleanup
    auth._valid_token_hashes.clear()
