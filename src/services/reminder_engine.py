"""Reminder engine: escalating notification scheduler.

Dependency Inversion: depends on MessageSender (ABC), not SignalClient.
Open/Closed: escalation patterns loaded from config, not hardcoded.
Single Responsibility: decides WHEN to send. Does not decide HOW.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models.meeting import Meeting, MeetingState
from ..repository.sqlite import SqliteMeetingRepository
from .meeting_service import MeetingService
from .notification.base import MessageSender

logger = logging.getLogger(__name__)


@dataclass
class EscalationStage:
    """A single stage in the escalation pattern."""

    offset_hours: float
    interval_min: Optional[int]  # None = single ping
    message: str


@dataclass
class EscalationConfig:
    """Full escalation configuration."""

    stages: list[EscalationStage]
    post_start_interval_min: int = 2
    post_start_message: str = "MEETING STARTED {mins_ago} min ago: {title}. {link}"
    timeout_after_min: int = 90


class ReminderEngine:
    """Decides when to send reminders and delegates sending to MessageSender."""

    def __init__(
        self,
        service: MeetingService,
        repository: SqliteMeetingRepository,
        sender: MessageSender,
        recipient: str,
        config: EscalationConfig,
    ) -> None:
        self._service = service
        self._repo = repository
        self._sender = sender
        self._recipient = recipient
        self._config = config

    async def tick(self) -> None:
        """Run one scheduler cycle. Called periodically by the main loop."""
        now = datetime.now(timezone.utc)

        # Get all meetings that need attention
        meetings = self._repo.list_upcoming(
            states=[MeetingState.PENDING, MeetingState.REMINDING],
        )

        for meeting in meetings:
            if meeting.starts_at is None:
                continue
            try:
                await self._process_meeting(meeting, now)
            except Exception:
                logger.exception("Error processing meeting %d", meeting.id or 0)

    async def _process_meeting(self, meeting: Meeting, now: datetime) -> None:
        """Process a single meeting: check if a reminder is due."""
        assert meeting.starts_at is not None
        assert meeting.id is not None

        starts_at = meeting.starts_at
        meeting_end = starts_at + timedelta(minutes=meeting.duration_min)
        timeout = starts_at + timedelta(minutes=self._config.timeout_after_min)

        # Check timeout first
        if now >= timeout and meeting.state == MeetingState.REMINDING:
            self._service.mark_missed(meeting.id)
            msg = f"MISSED: {meeting.title} (no acknowledgement received)"
            await self._sender.send_message(self._recipient, msg)
            return

        # After meeting start: use post-start pattern
        if now >= starts_at and meeting.state == MeetingState.REMINDING:
            await self._maybe_send_post_start(meeting, now)
            return

        # Before meeting start: check escalation stages
        await self._check_escalation_stages(meeting, now, starts_at)

    async def _check_escalation_stages(
        self,
        meeting: Meeting,
        now: datetime,
        starts_at: datetime,
    ) -> None:
        """Check if any escalation stage should fire."""
        assert meeting.id is not None

        # Find the most aggressive applicable stage
        applicable_stage: Optional[EscalationStage] = None
        for stage in self._config.stages:
            trigger_time = starts_at + timedelta(hours=stage.offset_hours)
            if now >= trigger_time:
                applicable_stage = stage

        if applicable_stage is None:
            return

        # Check if we should send based on interval
        last_sent = self._repo.get_last_reminder_time(meeting.id)

        if applicable_stage.interval_min is None:
            # Single ping: only send if we haven't sent in this stage
            trigger_time = starts_at + timedelta(hours=applicable_stage.offset_hours)
            if last_sent is not None and last_sent >= trigger_time:
                return
        else:
            # Repeating: check interval
            if last_sent is not None:
                next_send = last_sent + timedelta(minutes=applicable_stage.interval_min)
                if now < next_send:
                    return

        # Send the reminder
        msg = self._format_message(applicable_stage.message, meeting, now)
        sent = await self._sender.send_message(self._recipient, msg)
        if sent:
            self._repo.log_reminder(meeting.id, msg)
            # Transition to reminding if still pending
            if meeting.state == MeetingState.PENDING:
                self._service.mark_reminding(meeting.id)

    async def _maybe_send_post_start(self, meeting: Meeting, now: datetime) -> None:
        """Send post-start reminders at configured interval."""
        assert meeting.id is not None
        assert meeting.starts_at is not None

        last_sent = self._repo.get_last_reminder_time(meeting.id)
        if last_sent is not None:
            next_send = last_sent + timedelta(
                minutes=self._config.post_start_interval_min
            )
            if now < next_send:
                return

        msg = self._format_message(self._config.post_start_message, meeting, now)
        sent = await self._sender.send_message(self._recipient, msg)
        if sent:
            self._repo.log_reminder(meeting.id, msg)

    def _format_message(self, template: str, meeting: Meeting, now: datetime) -> str:
        """Format a reminder message template."""
        assert meeting.starts_at is not None

        mins_until = max(
            0,
            int((meeting.starts_at - now).total_seconds() / 60),
        )
        mins_ago = max(
            0,
            int((now - meeting.starts_at).total_seconds() / 60),
        )
        time_str = meeting.starts_at.strftime("%H:%M")

        return template.format(
            title=meeting.title,
            time=time_str,
            link=meeting.link or "(no link)",
            mins_until=mins_until,
            mins_ago=mins_ago,
        )
