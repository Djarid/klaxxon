"""Comprehensive tests for Klaxxon API routes.

Tests the thin HTTP layer that delegates to ReminderService.
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
from src.models.reminder import ReminderState
from src.repository.sqlite import SqliteReminderRepository
from src.services.reminder_service import ReminderService

# Test bearer token
TEST_TOKEN = "test-token-12345"


@pytest.fixture
def thread_safe_repo() -> SqliteReminderRepository:
    """In-memory SQLite repository with thread-safety for TestClient.

    TestClient runs in a different thread, so we need check_same_thread=False.
    """
    repo = SqliteReminderRepository(":memory:")
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
def thread_safe_service(thread_safe_repo: SqliteReminderRepository) -> ReminderService:
    """Reminder service with thread-safe repository for API tests."""
    return ReminderService(thread_safe_repo)


@pytest.fixture
def app(thread_safe_service: ReminderService) -> Generator[FastAPI, None, None]:
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
    routes._reminder_service = None
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
# POST /api/reminders - Create Reminder
# ============================================================================


def test_create_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders returns 201 with MeetingResponse JSON."""
    payload = {
        "title": "Team Standup",
        "starts_at": future_time.isoformat(),
        "duration_min": 30,
        "link": "https://meet.example.com/standup",
        "source": "api",
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

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
    """POST /api/reminders with past date returns 400."""
    payload = {
        "title": "Past Reminder",
        "starts_at": past_time.isoformat(),
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

    assert response.status_code == 400
    assert "past" in response.json()["detail"].lower()


def test_create_meeting_duplicate(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders with duplicate returns 409."""
    payload = {
        "title": "Duplicate Reminder",
        "starts_at": future_time.isoformat(),
    }

    # Create first reminder
    response1 = client.post("/api/reminders", json=payload, headers=auth_headers)
    assert response1.status_code == 201

    # Attempt duplicate (same title, within 30 min window)
    response2 = client.post("/api/reminders", json=payload, headers=auth_headers)
    assert response2.status_code == 409
    assert "already exists" in response2.json()["detail"].lower()


def test_create_meeting_missing_title(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders without title returns 422 validation error."""
    payload = {
        "starts_at": future_time.isoformat(),
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(err["loc"] == ["body", "title"] for err in errors)


def test_create_meeting_invalid_duration(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders with invalid duration returns 422."""
    payload = {
        "title": "Invalid Duration",
        "starts_at": future_time.isoformat(),
        "duration_min": 0,  # Must be >= 1
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

    assert response.status_code == 422


# ============================================================================
# GET /api/reminders - List Meetings
# ============================================================================


def test_list_meetings_empty(client: TestClient, auth_headers: dict) -> None:
    """GET /api/reminders returns empty list when no reminders exist."""
    response = client.get("/api/reminders", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["reminders"] == []
    assert data["count"] == 0


def test_list_meetings_all(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/reminders returns all reminders with count."""
    # Create two reminders
    payload1 = {"title": "Reminder 1", "starts_at": future_time.isoformat()}
    payload2 = {
        "title": "Reminder 2",
        "starts_at": (future_time + timedelta(hours=1)).isoformat(),
    }

    client.post("/api/reminders", json=payload1, headers=auth_headers)
    client.post("/api/reminders", json=payload2, headers=auth_headers)

    response = client.get("/api/reminders", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert len(data["reminders"]) == 2
    titles = {m["title"] for m in data["reminders"]}
    assert titles == {"Reminder 1", "Reminder 2"}


def test_list_meetings_filter_by_state(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/reminders?state=pending filters by state."""
    # Create a reminder
    payload = {"title": "Pending Reminder", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # Acknowledge it (changes state to ACKNOWLEDGED)
    client.post(f"/api/reminders/{reminder_id}/ack", headers=auth_headers)

    # Create another pending reminder
    payload2 = {
        "title": "Still Pending",
        "starts_at": (future_time + timedelta(hours=1)).isoformat(),
    }
    client.post("/api/reminders", json=payload2, headers=auth_headers)

    # Filter by pending state
    response = client.get("/api/reminders?state=pending", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["reminders"][0]["title"] == "Still Pending"
    assert data["reminders"][0]["state"] == "pending"


# ============================================================================
# GET /api/reminders/{id} - Get Single Reminder
# ============================================================================


def test_get_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/reminders/{id} returns the reminder."""
    payload = {"title": "Get Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    response = client.get(f"/api/reminders/{reminder_id}", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == reminder_id
    assert data["title"] == "Get Me"


def test_get_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """GET /api/reminders/{id} returns 404 when reminder doesn't exist."""
    response = client.get("/api/reminders/99999", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ============================================================================
# POST /api/reminders/{id}/ack - Acknowledge Reminder
# ============================================================================


def test_ack_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders/{id}/ack returns updated reminder with ACKNOWLEDGED state."""
    payload = {"title": "Ack Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    response = client.post(f"/api/reminders/{reminder_id}/ack", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == reminder_id
    assert data["state"] == "acknowledged"
    assert data["ack_keyword"] == "ack"
    assert data["ack_at"] is not None


def test_ack_meeting_custom_keyword(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders/{id}/ack with custom keyword."""
    payload = {"title": "Custom Ack", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    ack_payload = {"keyword": "confirmed"}
    response = client.post(
        f"/api/reminders/{reminder_id}/ack", json=ack_payload, headers=auth_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "acknowledged"
    assert data["ack_keyword"] == "confirmed"


def test_ack_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """POST /api/reminders/{id}/ack returns 404 when reminder doesn't exist."""
    response = client.post("/api/reminders/99999/ack", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_ack_meeting_invalid_transition(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders/{id}/ack returns 409 for invalid state transition."""
    payload = {"title": "Double Ack", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # First ack succeeds
    response1 = client.post(f"/api/reminders/{reminder_id}/ack", headers=auth_headers)
    assert response1.status_code == 200

    # Second ack fails (already acknowledged)
    response2 = client.post(f"/api/reminders/{reminder_id}/ack", headers=auth_headers)
    assert response2.status_code == 409


# ============================================================================
# POST /api/reminders/{id}/skip - Skip Reminder
# ============================================================================


def test_skip_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders/{id}/skip returns updated reminder with SKIPPED state."""
    payload = {"title": "Skip Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    response = client.post(f"/api/reminders/{reminder_id}/skip", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == reminder_id
    assert data["state"] == "skipped"


def test_skip_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """POST /api/reminders/{id}/skip returns 404 when reminder doesn't exist."""
    response = client.post("/api/reminders/99999/skip", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_skip_meeting_invalid_transition(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders/{id}/skip returns 409 for invalid state transition."""
    payload = {"title": "Double Skip", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # First skip succeeds
    response1 = client.post(f"/api/reminders/{reminder_id}/skip", headers=auth_headers)
    assert response1.status_code == 200

    # Second skip fails (already skipped)
    response2 = client.post(f"/api/reminders/{reminder_id}/skip", headers=auth_headers)
    assert response2.status_code == 409


# ============================================================================
# DELETE /api/reminders/{id} - Delete Reminder
# ============================================================================


def test_delete_meeting_success(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """DELETE /api/reminders/{id} returns 204 no content."""
    payload = {"title": "Delete Me", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    response = client.delete(f"/api/reminders/{reminder_id}", headers=auth_headers)

    assert response.status_code == 204
    assert response.content == b""

    # Verify it's gone
    get_resp = client.get(f"/api/reminders/{reminder_id}", headers=auth_headers)
    assert get_resp.status_code == 404


def test_delete_meeting_not_found(client: TestClient, auth_headers: dict) -> None:
    """DELETE /api/reminders/{id} returns 404 when reminder doesn't exist."""
    response = client.delete("/api/reminders/99999", headers=auth_headers)

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ============================================================================
# GET /api/health - Health Check
# ============================================================================


def test_health_check(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """GET /api/health returns health info with counts."""
    # Create some reminders in different states
    payload1 = {"title": "Pending 1", "starts_at": future_time.isoformat()}
    payload2 = {
        "title": "Pending 2",
        "starts_at": (future_time + timedelta(hours=1)).isoformat(),
    }
    create_resp = client.post("/api/reminders", json=payload1, headers=auth_headers)
    client.post("/api/reminders", json=payload2, headers=auth_headers)

    # Acknowledge one (moves to ACKNOWLEDGED, not counted as pending)
    reminder_id = create_resp.json()["id"]
    client.post(f"/api/reminders/{reminder_id}/ack", headers=auth_headers)

    response = client.get("/api/health", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["db_ok"] is True
    assert data["signal_connected"] is False  # No signal_available_fn set
    assert data["reminders_pending"] == 1  # Only one pending left
    assert data["reminders_reminding"] == 0


def test_health_check_with_signal_fn(
    client: TestClient,
    auth_headers: dict,
    app: FastAPI,
    thread_safe_service: ReminderService,
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

    response = client.post("/api/reminders", json=payload)

    assert response.status_code == 401


def test_invalid_auth_token(client: TestClient, future_time: datetime) -> None:
    """Request with invalid token returns 401."""
    payload = {"title": "Bad Auth", "starts_at": future_time.isoformat()}
    bad_headers = {"Authorization": "Bearer invalid-token-xyz"}

    response = client.post("/api/reminders", json=payload, headers=bad_headers)

    assert response.status_code == 401
    assert "invalid" in response.json()["detail"].lower()


def test_malformed_auth_header(client: TestClient, future_time: datetime) -> None:
    """Request with malformed Authorization header returns 401."""
    payload = {"title": "Malformed Auth", "starts_at": future_time.isoformat()}
    bad_headers = {"Authorization": "NotBearer token"}

    response = client.post("/api/reminders", json=payload, headers=bad_headers)

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
    routes._reminder_service = None

    client = TestClient(test_app)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

    response = client.get("/api/reminders", headers=headers)

    assert response.status_code == 503
    assert "not initialised" in response.json()["detail"].lower()

    # Cleanup
    auth._valid_token_hashes.clear()


# ============================================================================
# Description Field Tests
# ============================================================================


def test_create_reminder_with_description(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders with description field returns 201 with description in response."""
    payload = {
        "title": "Medication Reminder",
        "description": "Take 10mg Ramipril with water",
        "starts_at": future_time.isoformat(),
        "duration_min": 5,
        "source": "api",
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Medication Reminder"
    assert data["description"] == "Take 10mg Ramipril with water"
    assert data["state"] == "pending"


def test_create_reminder_without_description(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """POST /api/reminders without description returns 201 with null description."""
    payload = {
        "title": "Quick Meeting",
        "starts_at": future_time.isoformat(),
        "source": "api",
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Quick Meeting"
    assert data["description"] is None
    assert data["state"] == "pending"


# ============================================================================
# Profile and escalate_to tests
# ============================================================================


def test_create_reminder_with_profile(
    client: TestClient,
    auth_headers: dict[str, str],
    future_time: datetime,
) -> None:
    """POST /api/reminders with profile field."""
    payload = {
        "title": "Medication Reminder",
        "starts_at": future_time.isoformat(),
        "profile": "persistent",
        "source": "api",
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Medication Reminder"
    assert data["profile"] == "persistent"
    assert data["escalate_to"] is None


def test_create_reminder_with_escalate_to(
    client: TestClient,
    auth_headers: dict[str, str],
    future_time: datetime,
) -> None:
    """POST /api/reminders with valid E.164 escalate_to number."""
    payload = {
        "title": "Important Meeting",
        "starts_at": future_time.isoformat(),
        "profile": "meeting",
        "escalate_to": "+447700900123",
        "source": "api",
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Important Meeting"
    assert data["profile"] == "meeting"
    assert data["escalate_to"] == "+447700900123"


def test_create_reminder_with_invalid_escalate_to(
    client: TestClient,
    auth_headers: dict[str, str],
    future_time: datetime,
) -> None:
    """POST /api/reminders with invalid phone number returns 422."""
    # Invalid: missing +
    payload = {
        "title": "Test",
        "starts_at": future_time.isoformat(),
        "escalate_to": "447700900123",
        "source": "api",
    }

    response = client.post("/api/reminders", json=payload, headers=auth_headers)
    assert response.status_code == 422

    # Invalid: too short
    payload["escalate_to"] = "+44123"
    response = client.post("/api/reminders", json=payload, headers=auth_headers)
    assert response.status_code == 422

    # Invalid: starts with +0
    payload["escalate_to"] = "+0441234567890"
    response = client.post("/api/reminders", json=payload, headers=auth_headers)
    assert response.status_code == 422


# ============================================================================
# PATCH /api/reminders/{id} - Update Reminder
# ============================================================================


def test_update_reminder_title(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """PATCH /api/reminders/{id} with new title returns 200 with updated reminder."""
    # Create a reminder
    payload = {"title": "Original Title", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # Update the title
    update_payload = {"title": "Updated Title"}
    response = client.patch(
        f"/api/reminders/{reminder_id}", json=update_payload, headers=auth_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == reminder_id
    assert data["title"] == "Updated Title"
    assert data["state"] == "pending"


def test_update_reminder_multiple_fields(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """PATCH /api/reminders/{id} with multiple fields updates all provided fields."""
    # Create a reminder
    payload = {
        "title": "Original",
        "starts_at": future_time.isoformat(),
        "link": "https://old.example.com",
    }
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # Update multiple fields
    update_payload = {
        "title": "Updated Title",
        "description": "New description",
        "link": "https://new.example.com",
    }
    response = client.patch(
        f"/api/reminders/{reminder_id}", json=update_payload, headers=auth_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == reminder_id
    assert data["title"] == "Updated Title"
    assert data["description"] == "New description"
    assert data["link"] == "https://new.example.com"


def test_update_reminder_profile(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """PATCH /api/reminders/{id} with new profile."""
    # Create a reminder with default profile
    payload = {"title": "Test", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # Update the profile
    update_payload = {"profile": "persistent"}
    response = client.patch(
        f"/api/reminders/{reminder_id}", json=update_payload, headers=auth_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["profile"] == "persistent"


def test_update_reminder_escalate_to(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """PATCH /api/reminders/{id} with valid E.164 escalate_to number."""
    # Create a reminder
    payload = {"title": "Test", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # Update escalate_to
    update_payload = {"escalate_to": "+447700900456"}
    response = client.patch(
        f"/api/reminders/{reminder_id}", json=update_payload, headers=auth_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["escalate_to"] == "+447700900456"


def test_update_reminder_not_found(client: TestClient, auth_headers: dict) -> None:
    """PATCH /api/reminders/{id} with nonexistent id returns 404."""
    update_payload = {"title": "Updated"}
    response = client.patch(
        "/api/reminders/99999", json=update_payload, headers=auth_headers
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_update_reminder_terminal_state(
    client: TestClient, auth_headers: dict, future_time: datetime
) -> None:
    """PATCH /api/reminders/{id} on acknowledged reminder returns 409."""
    # Create and acknowledge a reminder
    payload = {"title": "Test", "starts_at": future_time.isoformat()}
    create_resp = client.post("/api/reminders", json=payload, headers=auth_headers)
    reminder_id = create_resp.json()["id"]

    # Acknowledge it
    client.post(f"/api/reminders/{reminder_id}/ack", headers=auth_headers)

    # Try to update it
    update_payload = {"title": "Should Fail"}
    response = client.patch(
        f"/api/reminders/{reminder_id}", json=update_payload, headers=auth_headers
    )

    assert response.status_code == 409
    assert "cannot edit" in response.json()["detail"].lower()
