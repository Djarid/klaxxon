"""Pydantic request/response schemas for the API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .reminder import ReminderState


class ReminderCreate(BaseModel):
    """Request body for creating a reminder."""

    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=2000)
    starts_at: datetime
    duration_min: int = Field(default=90, ge=1, le=1440)
    link: Optional[str] = Field(default=None, max_length=2000)
    source: str = Field(default="api", max_length=50)
    profile: str = Field(default="meeting", max_length=50)
    escalate_to: Optional[str] = Field(default=None, max_length=20)

    @field_validator("escalate_to")
    @classmethod
    def validate_escalate_to(cls, v: Optional[str]) -> Optional[str]:
        """Validate E.164 phone number format."""
        if v is None or v == "":
            return None
        # E.164: +[1-9][0-9]{6,14}
        if not re.match(r"^\+[1-9]\d{6,14}$", v):
            raise ValueError(
                "escalate_to must be a valid E.164 phone number (e.g., +441234567890)"
            )
        return v


class ReminderResponse(BaseModel):
    """Response body for a reminder."""

    id: int
    title: str
    description: Optional[str]
    starts_at: datetime
    duration_min: int
    link: Optional[str]
    source: str
    profile: str
    escalate_to: Optional[str]
    schedule_id: Optional[int]
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


class ReminderUpdate(BaseModel):
    """Request body for updating a reminder (partial update)."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=2000)
    starts_at: Optional[datetime] = None
    duration_min: Optional[int] = Field(default=None, ge=1, le=1440)
    link: Optional[str] = Field(default=None, max_length=2000)
    profile: Optional[str] = Field(default=None, max_length=50)
    escalate_to: Optional[str] = Field(default=None, max_length=20)

    @field_validator("escalate_to")
    @classmethod
    def validate_escalate_to(cls, v: Optional[str]) -> Optional[str]:
        """Validate E.164 phone number format."""
        if v is None or v == "":
            return None
        # E.164: +[1-9][0-9]{6,14}
        if not re.match(r"^\+[1-9]\d{6,14}$", v):
            raise ValueError(
                "escalate_to must be a valid E.164 phone number (e.g., +441234567890)"
            )
        return v


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


class ScheduleCreate(BaseModel):
    """Request body for creating a schedule."""

    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=2000)
    time_of_day: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    duration_min: int = Field(default=0, ge=0, le=1440)
    link: Optional[str] = Field(default=None, max_length=2000)
    profile: str = Field(default="meeting", max_length=50)
    escalate_to: Optional[str] = Field(default=None, max_length=20)
    recurrence: str = Field(..., pattern=r"^(daily|weekly|custom)$")
    recurrence_rule: Optional[str] = Field(default=None, max_length=100)

    @field_validator("escalate_to")
    @classmethod
    def validate_escalate_to(cls, v: Optional[str]) -> Optional[str]:
        """Validate E.164 phone number format."""
        if v is None or v == "":
            return None
        # E.164: +[1-9][0-9]{6,14}
        if not re.match(r"^\+[1-9]\d{6,14}$", v):
            raise ValueError(
                "escalate_to must be a valid E.164 phone number (e.g., +441234567890)"
            )
        return v

    @field_validator("recurrence_rule")
    @classmethod
    def validate_recurrence_rule(cls, v: Optional[str], info) -> Optional[str]:
        """Validate recurrence_rule format and requirement."""
        # Get recurrence from values (if available)
        recurrence = info.data.get("recurrence")

        # If recurrence is weekly or custom, recurrence_rule is required
        if recurrence in ("weekly", "custom"):
            if not v:
                raise ValueError(
                    f"recurrence_rule is required for {recurrence} recurrence"
                )
            # Validate day abbreviations
            valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
            days = {d.strip().lower() for d in v.split(",")}
            invalid_days = days - valid_days
            if invalid_days:
                raise ValueError(
                    f"Invalid day abbreviations: {invalid_days}. "
                    f"Valid: mon,tue,wed,thu,fri,sat,sun"
                )

        return v


class ScheduleUpdate(BaseModel):
    """Request body for updating a schedule (partial update)."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=2000)
    time_of_day: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    duration_min: Optional[int] = Field(default=None, ge=0, le=1440)
    link: Optional[str] = Field(default=None, max_length=2000)
    profile: Optional[str] = Field(default=None, max_length=50)
    escalate_to: Optional[str] = Field(default=None, max_length=20)
    recurrence: Optional[str] = Field(default=None, pattern=r"^(daily|weekly|custom)$")
    recurrence_rule: Optional[str] = Field(default=None, max_length=100)
    is_active: Optional[bool] = None

    @field_validator("escalate_to")
    @classmethod
    def validate_escalate_to(cls, v: Optional[str]) -> Optional[str]:
        """Validate E.164 phone number format."""
        if v is None or v == "":
            return None
        # E.164: +[1-9][0-9]{6,14}
        if not re.match(r"^\+[1-9]\d{6,14}$", v):
            raise ValueError(
                "escalate_to must be a valid E.164 phone number (e.g., +441234567890)"
            )
        return v


class ScheduleResponse(BaseModel):
    """Response body for a schedule."""

    id: int
    title: str
    description: Optional[str]
    time_of_day: str
    duration_min: int
    link: Optional[str]
    source: str
    profile: str
    escalate_to: Optional[str]
    recurrence: str
    recurrence_rule: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScheduleListResponse(BaseModel):
    """Response body for listing schedules."""

    schedules: list[ScheduleResponse]
    count: int
