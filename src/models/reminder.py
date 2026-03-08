"""Reminder model and state definitions."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class ReminderState(str, enum.Enum):
    """Reminder lifecycle states."""

    PENDING = "pending"
    REMINDING = "reminding"
    ACKNOWLEDGED = "acknowledged"
    SKIPPED = "skipped"
    MISSED = "missed"


@dataclass
class Reminder:
    """A scheduled reminder that requires reminder escalation."""

    id: Optional[int] = None
    title: str = ""
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    duration_min: int = 90
    link: Optional[str] = None
    source: str = "manual"
    profile: str = "meeting"
    escalate_to: Optional[str] = None
    schedule_id: Optional[int] = None
    lead_time_min: Optional[int] = None
    nag_interval_min: Optional[int] = None
    state: ReminderState = ReminderState.PENDING
    ack_keyword: Optional[str] = None
    ack_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
