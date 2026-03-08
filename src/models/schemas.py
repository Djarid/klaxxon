"""Pydantic request/response schemas for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .reminder import ReminderState


class ReminderCreate(BaseModel):
    """Request body for creating a reminder."""

    title: str = Field(..., min_length=1, max_length=500)
    starts_at: datetime
    duration_min: int = Field(default=90, ge=1, le=1440)
    link: Optional[str] = Field(default=None, max_length=2000)
    source: str = Field(default="api", max_length=50)


class ReminderResponse(BaseModel):
    """Response body for a reminder."""

    id: int
    title: str
    starts_at: datetime
    duration_min: int
    link: Optional[str]
    source: str
    state: ReminderState
    ack_keyword: Optional[str]
    ack_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ReminderListResponse(BaseModel):
    """Response body for listing reminders."""

    reminders: list[ReminderResponse]
    count: int


class AckRequest(BaseModel):
    """Request body for acknowledging a reminder."""

    keyword: str = Field(default="ack", max_length=50)


class HealthResponse(BaseModel):
    """Response body for health check."""

    status: str
    signal_connected: bool
    db_ok: bool
    next_reminder: Optional[datetime]
    reminders_pending: int
    reminders_reminding: int
