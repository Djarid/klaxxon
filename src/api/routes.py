"""API routes: thin layer that delegates to MeetingService.

Single Responsibility: HTTP request/response handling.
DRY: all business logic is in MeetingService, not here.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..models.meeting import MeetingState
from ..models.schemas import (
    AckRequest,
    HealthResponse,
    MeetingCreate,
    MeetingListResponse,
    MeetingResponse,
)
from ..services.meeting_service import (
    DuplicateMeetingError,
    MeetingNotFoundError,
    MeetingService,
    PastMeetingError,
)
from ..services.state_machine import InvalidTransitionError
from .auth import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


# These will be set by the composition root (main.py)
_meeting_service: Optional[MeetingService] = None
_signal_available_fn = None


def set_dependencies(
    service: MeetingService,
    signal_available_fn=None,
) -> None:
    """Set the service dependencies. Called from main.py."""
    global _meeting_service, _signal_available_fn
    _meeting_service = service
    _signal_available_fn = signal_available_fn


def _get_service() -> MeetingService:
    if _meeting_service is None:
        raise HTTPException(status_code=503, detail="Service not initialised")
    return _meeting_service


@router.post("/meetings", response_model=MeetingResponse, status_code=201)
async def create_meeting(body: MeetingCreate) -> MeetingResponse:
    """Create a new meeting."""
    svc = _get_service()
    try:
        meeting = svc.create(
            title=body.title,
            starts_at=body.starts_at,
            duration_min=body.duration_min,
            link=body.link,
            source=body.source,
        )
    except PastMeetingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DuplicateMeetingError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return MeetingResponse.model_validate(meeting)


@router.get("/meetings", response_model=MeetingListResponse)
async def list_meetings(state: Optional[MeetingState] = None) -> MeetingListResponse:
    """List meetings, optionally filtered by state."""
    svc = _get_service()
    meetings = svc.list_meetings(state=state)
    return MeetingListResponse(
        meetings=[MeetingResponse.model_validate(m) for m in meetings],
        count=len(meetings),
    )


@router.get("/meetings/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(meeting_id: int) -> MeetingResponse:
    """Get a single meeting."""
    svc = _get_service()
    try:
        meeting = svc.get(meeting_id)
    except MeetingNotFoundError:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return MeetingResponse.model_validate(meeting)


@router.post("/meetings/{meeting_id}/ack", response_model=MeetingResponse)
async def ack_meeting(
    meeting_id: int, body: AckRequest = AckRequest()
) -> MeetingResponse:
    """Acknowledge a meeting. Stops reminders."""
    svc = _get_service()
    try:
        meeting = svc.acknowledge(meeting_id, body.keyword)
    except MeetingNotFoundError:
        raise HTTPException(status_code=404, detail="Meeting not found")
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return MeetingResponse.model_validate(meeting)


@router.post("/meetings/{meeting_id}/skip", response_model=MeetingResponse)
async def skip_meeting(meeting_id: int) -> MeetingResponse:
    """Skip a meeting deliberately. Stops reminders."""
    svc = _get_service()
    try:
        meeting = svc.skip(meeting_id)
    except MeetingNotFoundError:
        raise HTTPException(status_code=404, detail="Meeting not found")
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return MeetingResponse.model_validate(meeting)


@router.delete("/meetings/{meeting_id}", status_code=204)
async def delete_meeting(meeting_id: int) -> None:
    """Delete a meeting."""
    svc = _get_service()
    if not svc.delete(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")


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
        meetings_pending=svc.count_pending(),
        meetings_reminding=svc.count_reminding(),
    )
