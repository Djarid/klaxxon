"""API routes: thin layer that delegates to ReminderService.

Single Responsibility: HTTP request/response handling.
DRY: all business logic is in ReminderService, not here.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..models.reminder import ReminderState
from ..models.schemas import (
    AckRequest,
    HealthResponse,
    ReminderCreate,
    ReminderListResponse,
    ReminderResponse,
)
from ..services.reminder_service import (
    DuplicateReminderError,
    ReminderNotFoundError,
    ReminderService,
    PastReminderError,
)
from ..services.state_machine import InvalidTransitionError
from .auth import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


# These will be set by the composition root (main.py)
_reminder_service: Optional[ReminderService] = None
_signal_available_fn = None


def set_dependencies(
    service: ReminderService,
    signal_available_fn=None,
) -> None:
    """Set the service dependencies. Called from main.py."""
    global _reminder_service, _signal_available_fn
    _reminder_service = service
    _signal_available_fn = signal_available_fn


def _get_service() -> ReminderService:
    if _reminder_service is None:
        raise HTTPException(status_code=503, detail="Service not initialised")
    return _reminder_service


@router.post("/reminders", response_model=ReminderResponse, status_code=201)
async def create_reminder(body: ReminderCreate) -> ReminderResponse:
    """Create a new reminder."""
    svc = _get_service()
    try:
        reminder = svc.create(
            title=body.title,
            starts_at=body.starts_at,
            duration_min=body.duration_min,
            link=body.link,
            source=body.source,
            description=body.description,
        )
    except PastReminderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DuplicateReminderError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ReminderResponse.model_validate(reminder)


@router.get("/reminders", response_model=ReminderListResponse)
async def list_reminders(state: Optional[ReminderState] = None) -> ReminderListResponse:
    """List reminders, optionally filtered by state."""
    svc = _get_service()
    reminders = svc.list_reminders(state=state)
    return ReminderListResponse(
        reminders=[ReminderResponse.model_validate(r) for r in reminders],
        count=len(reminders),
    )


@router.get("/reminders/{reminder_id}", response_model=ReminderResponse)
async def get_reminder(reminder_id: int) -> ReminderResponse:
    """Get a single reminder."""
    svc = _get_service()
    try:
        reminder = svc.get(reminder_id)
    except ReminderNotFoundError:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return ReminderResponse.model_validate(reminder)


@router.post("/reminders/{reminder_id}/ack", response_model=ReminderResponse)
async def ack_reminder(
    reminder_id: int, body: AckRequest = AckRequest()
) -> ReminderResponse:
    """Acknowledge a reminder. Stops reminders."""
    svc = _get_service()
    try:
        reminder = svc.acknowledge(reminder_id, body.keyword)
    except ReminderNotFoundError:
        raise HTTPException(status_code=404, detail="Reminder not found")
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return ReminderResponse.model_validate(reminder)


@router.post("/reminders/{reminder_id}/skip", response_model=ReminderResponse)
async def skip_reminder(reminder_id: int) -> ReminderResponse:
    """Skip a reminder deliberately. Stops reminders."""
    svc = _get_service()
    try:
        reminder = svc.skip(reminder_id)
    except ReminderNotFoundError:
        raise HTTPException(status_code=404, detail="Reminder not found")
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return ReminderResponse.model_validate(reminder)


@router.delete("/reminders/{reminder_id}", status_code=204)
async def delete_reminder(reminder_id: int) -> None:
    """Delete a reminder."""
    svc = _get_service()
    if not svc.delete(reminder_id):
        raise HTTPException(status_code=404, detail="Reminder not found")


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    svc = _get_service()

    signal_ok = False
    if _signal_available_fn:
        try:
            signal_ok = await _signal_available_fn()
        except Exception:
            signal_ok = False

    return HealthResponse(
        status="ok",
        signal_connected=signal_ok,
        db_ok=True,
        next_reminder=None,  # TODO: calculate from scheduler
        reminders_pending=svc.count_pending(),
        reminders_reminding=svc.count_reminding(),
    )
