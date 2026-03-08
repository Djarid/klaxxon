"""Pydantic request/response schemas for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .meeting import MeetingState


class MeetingCreate(BaseModel):
    """Request body for creating a meeting."""

    title: str = Field(..., min_length=1, max_length=500)
    starts_at: datetime
    duration_min: int = Field(default=90, ge=1, le=1440)
    link: Optional[str] = Field(default=None, max_length=2000)
    source: str = Field(default="api", max_length=50)


class MeetingResponse(BaseModel):
    """Response body for a meeting."""

    id: int
    title: str
    starts_at: datetime
    duration_min: int
    link: Optional[str]
    source: str
    state: MeetingState
    ack_keyword: Optional[str]
    ack_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MeetingListResponse(BaseModel):
    """Response body for listing meetings."""

    meetings: list[MeetingResponse]
    count: int


class AckRequest(BaseModel):
    """Request body for acknowledging a meeting."""

    keyword: str = Field(default="ack", max_length=50)


class HealthResponse(BaseModel):
    """Response body for health check."""

    status: str
    signal_connected: bool
    db_ok: bool
    next_reminder: Optional[datetime]
    meetings_pending: int
    meetings_reminding: int
