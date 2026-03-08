"""Meeting model and state definitions."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class MeetingState(str, enum.Enum):
    """Meeting lifecycle states."""

    PENDING = "pending"
    REMINDING = "reminding"
    ACKNOWLEDGED = "acknowledged"
    SKIPPED = "skipped"
    MISSED = "missed"


@dataclass
class Meeting:
    """A scheduled meeting that requires reminder escalation."""

    id: Optional[int] = None
    title: str = ""
    starts_at: Optional[datetime] = None
    duration_min: int = 90
    link: Optional[str] = None
    source: str = "manual"
    state: MeetingState = MeetingState.PENDING
    ack_keyword: Optional[str] = None
    ack_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
