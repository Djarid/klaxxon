"""Schedule model for recurring reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Schedule:
    """A recurring schedule that spawns reminder instances."""

    id: Optional[int] = None
    title: str = ""
    description: Optional[str] = None
    time_of_day: str = "09:00"  # HH:MM local time
    duration_min: int = 0  # 0 for non-meeting reminders
    link: Optional[str] = None
    source: str = "manual"
    profile: str = "meeting"
    escalate_to: Optional[str] = None
    lead_time_min: Optional[int] = None
    nag_interval_min: Optional[int] = None
    recurrence: str = "daily"  # daily, weekly, custom
    recurrence_rule: Optional[str] = None  # e.g. "mon,wed,fri"
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
