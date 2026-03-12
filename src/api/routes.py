"""API routes: thin layer that delegates to ReminderService.

Single Responsibility: HTTP request/response handling.
DRY: all business logic is in ReminderService, not here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..models.reminder import ReminderState
from ..models.schemas import (
    AckRequest,
    CleanupRequest,
    CleanupResponse,
    HealthResponse,
    ReminderCreate,
    ReminderListResponse,
    ReminderResponse,
    ReminderUpdate,
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleUpdate,
)
from ..services.reminder_service import (
    DuplicateReminderError,
    InvalidStateError,
    ReminderNotFoundError,
    ReminderService,
    PastReminderError,
)
from ..services.schedule_service import (
    ScheduleNotFoundError,
    ScheduleService,
    ScheduleValidationError,
)
from ..services.state_machine import InvalidTransitionError
from .auth import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


# These will be set by the composition root (main.py)
_reminder_service: Optional[ReminderService] = None
_schedule_service: Optional[ScheduleService] = None
_signal_available_fn = None
_housekeeping_service = None


def set_dependencies(
    service: ReminderService,
    schedule_service: Optional[ScheduleService] = None,
    signal_available_fn=None,
    housekeeping_service=None,
) -> None:
    """Set the service dependencies. Called from main.py."""
    global \
        _reminder_service, \
        _schedule_service, \
        _signal_available_fn, \
        _housekeeping_service
    _reminder_service = service
    _schedule_service = schedule_service
    _signal_available_fn = signal_available_fn
    _housekeeping_service = housekeeping_service


def _get_housekeeping_service():
    if _housekeeping_service is None:
        raise HTTPException(
            status_code=503, detail="Housekeeping service not initialised"
        )
    return _housekeeping_service


def _get_service() -> ReminderService:
    if _reminder_service is None:
        raise HTTPException(status_code=503, detail="Service not initialised")
    return _reminder_service


def _get_schedule_service() -> ScheduleService:
    if _schedule_service is None:
        raise HTTPException(status_code=503, detail="Schedule service not initialised")
    return _schedule_service


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
            profile=body.profile,
            escalate_to=body.escalate_to,
            lead_time_min=body.lead_time_min,
            nag_interval_min=body.nag_interval_min,
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


@router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
async def update_reminder(reminder_id: int, body: ReminderUpdate) -> ReminderResponse:
    """Update a reminder's fields (partial update)."""
    svc = _get_service()

    # Extract only non-None fields
    fields = body.model_dump(exclude_none=True)

    try:
        reminder = svc.update(reminder_id, **fields)
    except ReminderNotFoundError:
        raise HTTPException(status_code=404, detail="Reminder not found")
    except InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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


# Schedule endpoints


@router.post("/schedules", response_model=ScheduleResponse, status_code=201)
async def create_schedule(body: ScheduleCreate) -> ScheduleResponse:
    """Create a new schedule."""
    svc = _get_schedule_service()
    try:
        schedule = svc.create(
            title=body.title,
            description=body.description,
            time_of_day=body.time_of_day,
            duration_min=body.duration_min,
            link=body.link,
            profile=body.profile,
            escalate_to=body.escalate_to,
            lead_time_min=body.lead_time_min,
            nag_interval_min=body.nag_interval_min,
            recurrence=body.recurrence,
            recurrence_rule=body.recurrence_rule,
        )
    except ScheduleValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ScheduleResponse.model_validate(schedule)


@router.get("/schedules", response_model=ScheduleListResponse)
async def list_schedules(active_only: bool = True) -> ScheduleListResponse:
    """List schedules, optionally filtered by active status."""
    svc = _get_schedule_service()
    schedules = svc.list(active_only=active_only)
    return ScheduleListResponse(
        schedules=[ScheduleResponse.model_validate(s) for s in schedules],
        count=len(schedules),
    )


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(schedule_id: int) -> ScheduleResponse:
    """Get a single schedule."""
    svc = _get_schedule_service()
    try:
        schedule = svc.get(schedule_id)
    except ScheduleNotFoundError:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleResponse.model_validate(schedule)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(schedule_id: int, body: ScheduleUpdate) -> ScheduleResponse:
    """Update a schedule's fields (partial update)."""
    svc = _get_schedule_service()

    # Extract only non-None fields
    fields = body.model_dump(exclude_none=True)

    try:
        schedule = svc.update(schedule_id, **fields)
    except ScheduleNotFoundError:
        raise HTTPException(status_code=404, detail="Schedule not found")
    except ScheduleValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ScheduleResponse.model_validate(schedule)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: int) -> None:
    """Deactivate a schedule (soft delete)."""
    svc = _get_schedule_service()
    if not svc.deactivate(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")


@router.post("/housekeeping/cleanup", response_model=CleanupResponse)
async def housekeeping_cleanup(
    body: CleanupRequest = CleanupRequest(),
    dry_run: bool = False,
) -> CleanupResponse:
    """Trigger an immediate cleanup of terminal-state reminders.

    Deletes ACKNOWLEDGED, SKIPPED, and MISSED reminders older than retention_days,
    and cleans up orphaned ack_tokens. Supports dry_run mode for preview.
    """
    svc = _get_housekeeping_service()

    # Use override from body if provided, otherwise fall back to service default
    effective_days = (
        body.retention_days if body.retention_days is not None else svc.retention_days
    )

    # Compute cutoff for response
    cutoff = datetime.now(timezone.utc) - timedelta(days=effective_days)

    result = svc.cleanup(retention_days=effective_days, dry_run=dry_run)

    return CleanupResponse(
        dry_run=dry_run,
        retention_days=effective_days,
        cutoff=cutoff,
        deleted_reminders=result.deleted_reminders,
        deleted_acknowledged=result.deleted_acknowledged,
        deleted_skipped=result.deleted_skipped,
        deleted_missed=result.deleted_missed,
        deleted_orphan_tokens=result.deleted_orphan_tokens,
    )
