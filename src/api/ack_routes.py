"""Public ack routes: GET /ack/{token} — no bearer token required.

This module implements REQ-5, REQ-6, REQ-13 from .Claude/plans/nag-ack-token.md.

Note: this router intentionally has NO authentication dependency (AC-9, REQ-5).
      It must be mounted separately from the authenticated /api/* router.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..services.ack_token_service import (
    AckTokenService,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenNotFoundError,
)
from ..services.reminder_service import (
    InvalidTransitionError,
    ReminderNotFoundError,
    ReminderService,
)

router = APIRouter()

# Set by composition root (main.py / test fixtures)
_reminder_service: Optional[ReminderService] = None
_ack_token_service: Optional[AckTokenService] = None


def set_dependencies(
    service: ReminderService,
    ack_token_service: AckTokenService,
) -> None:
    """Set service dependencies.  Called from main.py and test fixtures."""
    global _reminder_service, _ack_token_service
    _reminder_service = service
    _ack_token_service = ack_token_service


def _get_service() -> ReminderService:
    if _reminder_service is None:
        raise RuntimeError("ReminderService not initialised in ack_routes")
    return _reminder_service


def _get_ack_token_service() -> AckTokenService:
    if _ack_token_service is None:
        raise RuntimeError("AckTokenService not initialised in ack_routes")
    return _ack_token_service


# ---------------------------------------------------------------------------
# HTML page helpers
# ---------------------------------------------------------------------------

_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Acknowledged</title></head>
<body>
<h1>&#10003; Acknowledged: {title}</h1>
<p>Your reminder has been acknowledged.</p>
</body>
</html>"""

_USED_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Link already used</title></head>
<body>
<h1>This link has already been used</h1>
<p>The acknowledgement link was already used.</p>
</body>
</html>"""

_EXPIRED_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Link expired</title></head>
<body>
<h1>This link has expired</h1>
<p>The acknowledgement link has expired (links are valid for 24 hours).</p>
</body>
</html>"""

_NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Invalid link</title></head>
<body>
<h1>Invalid or unknown link</h1>
<p>This acknowledgement link is not valid.</p>
</body>
</html>"""

_ALREADY_ACKED_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Already acknowledged</title></head>
<body>
<h1>Already acknowledged</h1>
<p>This reminder was already acknowledged.</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/ack/{token}", response_class=HTMLResponse)
async def ack_via_token(token: str) -> HTMLResponse:
    """Public endpoint: acknowledge a reminder using a one-time token.

    No bearer token required (REQ-5, AC-9).

    Operation order (important for AC-7 token-not-consumed guarantee):
      1. Look up token by hash — return 404 if not found.
      2. Check expiry — return 410 if expired (do NOT mark used).
      3. Check used flag — return 410 if already used.
      4. Get the reminder and check its state — return 409 if terminal
         (do NOT mark token used per AC-7/E-2).
      5. Mark token as used (atomic).
      6. Acknowledge the reminder.
    """
    import hashlib
    from datetime import datetime, timezone

    ack_svc = _get_ack_token_service()
    reminder_svc = _get_service()

    # Step 1: Look up token by hash
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    stored_token = ack_svc._repo.get_by_hash(token_hash)
    if stored_token is None:
        return HTMLResponse(content=_NOT_FOUND_HTML, status_code=404)

    # Step 2: Check expiry
    now = datetime.now(timezone.utc)
    expires_at = stored_token.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now >= expires_at:
            return HTMLResponse(content=_EXPIRED_HTML, status_code=410)

    # Step 3: Check if already used
    if stored_token.used:
        return HTMLResponse(content=_USED_HTML, status_code=410)

    # Step 4: Check reminder state BEFORE consuming the token (AC-7, E-2)
    reminder_id = stored_token.reminder_id
    try:
        reminder = reminder_svc.get(reminder_id)
    except ReminderNotFoundError:
        return HTMLResponse(content=_NOT_FOUND_HTML, status_code=404)

    # Attempt the acknowledgement state transition check without committing
    from ..models.reminder import ReminderState
    from ..services.state_machine import ReminderStateMachine

    _sm = ReminderStateMachine()
    try:
        _sm.transition(reminder, "ack")
    except InvalidTransitionError:
        # Reminder is already in a terminal state — do NOT consume token (AC-7)
        return HTMLResponse(content=_ALREADY_ACKED_HTML, status_code=409)

    # Step 5: Atomically mark token as used
    marked = ack_svc._repo.mark_used(token_hash)
    if not marked:
        # Concurrent request won the race
        return HTMLResponse(content=_USED_HTML, status_code=410)

    # Step 6: Acknowledge the reminder
    try:
        reminder = reminder_svc.acknowledge(reminder_id, "web-token")
    except ReminderNotFoundError:
        return HTMLResponse(content=_NOT_FOUND_HTML, status_code=404)
    except InvalidTransitionError:
        # Should not happen since we checked above, but handle defensively
        return HTMLResponse(content=_ALREADY_ACKED_HTML, status_code=409)

    return HTMLResponse(
        content=_SUCCESS_HTML.format(title=reminder.title),
        status_code=200,
    )
