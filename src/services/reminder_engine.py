"""Reminder engine: escalating notification scheduler.

Dependency Inversion: depends on MessageSender (ABC), not SignalClient.
Open/Closed: escalation patterns loaded from config, not hardcoded.
Single Responsibility: decides WHEN to send. Does not decide HOW.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from ..config import EscalationProfile
from ..models.reminder import Reminder, ReminderState
from ..repository.sqlite import SqliteReminderRepository
from .reminder_service import ReminderService
from .notification.base import MessageSender

if TYPE_CHECKING:
    from .ack_token_service import AckTokenService

logger = logging.getLogger(__name__)


class ReminderEngine:
    """Decides when to send reminders and delegates sending to MessageSender."""

    def __init__(
        self,
        service: ReminderService,
        repository: SqliteReminderRepository,
        sender: MessageSender,
        recipient: str,
        escalation_profiles: dict[str, EscalationProfile],
        ack_token_service: Optional["AckTokenService"] = None,
    ) -> None:
        self._service = service
        self._repo = repository
        self._sender = sender
        self._recipient = recipient
        self._profiles = escalation_profiles
        self._ack_token_service = ack_token_service

    async def tick(self) -> None:
        """Run one scheduler cycle. Called periodically by the main loop."""
        now = datetime.now(timezone.utc)

        # Get all reminders that need attention
        reminders = self._repo.list_upcoming(
            states=[ReminderState.PENDING, ReminderState.REMINDING],
        )

        for reminder in reminders:
            if reminder.starts_at is None:
                continue
            try:
                await self._process_reminder(reminder, now)
            except Exception:
                logger.exception("Error processing reminder %d", reminder.id or 0)

    async def _process_reminder(self, reminder: Reminder, now: datetime) -> None:
        """Process a single reminder: check if a reminder is due."""
        assert reminder.starts_at is not None
        assert reminder.id is not None

        # Load profile for this reminder
        profile = self._get_profile(reminder)
        starts_at = reminder.starts_at

        # Check timeout first (if profile has timeout)
        if profile.timeout_after_min is not None:
            timeout = starts_at + timedelta(minutes=profile.timeout_after_min)
            if now >= timeout and reminder.state == ReminderState.REMINDING:
                self._service.mark_missed(reminder.id)
                msg = f"MISSED: {reminder.title} (no acknowledgement received)"
                await self._send_to_recipients(reminder, msg, "self")
                return

        # After reminder start: check overflow first, then post-start
        if now >= starts_at and reminder.state == ReminderState.REMINDING:
            # Check if overflow should trigger
            if profile.overflow is not None:
                overflow_trigger = starts_at + timedelta(
                    minutes=profile.overflow.after_min
                )
                if now >= overflow_trigger:
                    await self._maybe_send_overflow(reminder, now, profile)
                    return
            # Otherwise use post-start pattern
            await self._maybe_send_post_start(reminder, now, profile)
            return

        # Before reminder start: check escalation stages
        await self._check_escalation_stages(reminder, now, starts_at, profile)

    def _get_profile(self, reminder: Reminder) -> EscalationProfile:
        """Get the escalation profile for a reminder, with fallback."""
        profile_name = getattr(reminder, "profile", "meeting")
        if profile_name in self._profiles:
            return self._profiles[profile_name]

        # Profile not found, try to fall back to meeting
        logger.warning(
            "Profile '%s' not found for reminder %d, falling back to 'meeting'",
            profile_name,
            reminder.id or 0,
        )
        if "meeting" in self._profiles:
            return self._profiles["meeting"]

        # No meeting profile either, use first available profile
        if self._profiles:
            first_profile = next(iter(self._profiles.values()))
            logger.warning(
                "No 'meeting' profile found, using first available profile for reminder %d",
                reminder.id or 0,
            )
            return first_profile

        # No profiles at all - this should never happen in production
        raise ValueError("No escalation profiles configured")

    async def _check_escalation_stages(
        self,
        reminder: Reminder,
        now: datetime,
        starts_at: datetime,
        profile: EscalationProfile,
    ) -> None:
        """Check if any escalation stage should fire."""
        assert reminder.id is not None

        # Apply lead_time_min override if set
        if reminder.lead_time_min is not None:
            # Override: treat as if profile has a single first stage at -lead_time_min minutes
            trigger_time = starts_at - timedelta(minutes=reminder.lead_time_min)
            if now < trigger_time:
                return  # Not time yet

            # Check if we've already sent the first reminder
            last_sent = self._repo.get_last_reminder_time(reminder.id)
            if last_sent is None or last_sent < trigger_time:
                # Send first reminder using first stage's message template
                first_stage = profile.stages[0] if profile.stages else None
                if first_stage:
                    msg = self._format_message(first_stage.message, reminder, now)
                    sent = await self._send_to_recipients(
                        reminder, msg, first_stage.target
                    )
                    if sent:
                        self._repo.log_reminder(reminder.id, msg)
                        if reminder.state == ReminderState.PENDING:
                            self._service.mark_reminding(reminder.id)
                return

            # After first reminder, fall through to use profile stages for escalation
            # (but skip stages that would have fired before lead_time_min)
            # Find stages that should fire AFTER the lead_time trigger
            applicable_stage = None
            for stage in profile.stages:
                stage_trigger = starts_at + timedelta(hours=stage.offset_hours)
                if stage_trigger > trigger_time and now >= stage_trigger:
                    applicable_stage = stage

            if applicable_stage is None:
                return
        else:
            # No override: use profile stages as-is
            # Find the most aggressive applicable stage
            applicable_stage = None
            for stage in profile.stages:
                trigger_time = starts_at + timedelta(hours=stage.offset_hours)
                if now >= trigger_time:
                    applicable_stage = stage

            if applicable_stage is None:
                return

        # Check if we should send based on interval (with nag_interval_min override)
        last_sent = self._repo.get_last_reminder_time(reminder.id)

        # Determine effective interval
        effective_interval = applicable_stage.interval_min
        if (
            reminder.nag_interval_min is not None
            and applicable_stage.interval_min is not None
        ):
            # Override the interval for repeating stages
            effective_interval = reminder.nag_interval_min

        if effective_interval is None:
            # Single ping: only send if we haven't sent in this stage
            trigger_time = starts_at + timedelta(hours=applicable_stage.offset_hours)
            if last_sent is not None and last_sent >= trigger_time:
                return
        else:
            # Repeating: check interval
            if last_sent is not None:
                next_send = last_sent + timedelta(minutes=effective_interval)
                if now < next_send:
                    return

        # Send the reminder
        msg = self._format_message(applicable_stage.message, reminder, now)
        sent = await self._send_to_recipients(reminder, msg, applicable_stage.target)
        if sent:
            self._repo.log_reminder(reminder.id, msg)
            # Transition to reminding if still pending
            if reminder.state == ReminderState.PENDING:
                self._service.mark_reminding(reminder.id)

    async def _maybe_send_post_start(
        self, reminder: Reminder, now: datetime, profile: EscalationProfile
    ) -> None:
        """Send post-start reminders at configured interval."""
        assert reminder.id is not None
        assert reminder.starts_at is not None

        # Apply nag_interval_min override if set
        effective_interval = profile.post_start_interval_min
        if reminder.nag_interval_min is not None:
            effective_interval = reminder.nag_interval_min

        last_sent = self._repo.get_last_reminder_time(reminder.id)
        if last_sent is not None:
            next_send = last_sent + timedelta(minutes=effective_interval)
            if now < next_send:
                return

        msg = self._format_message(profile.post_start_message, reminder, now)
        sent = await self._send_to_recipients(reminder, msg, profile.post_start_target)
        if sent:
            self._repo.log_reminder(reminder.id, msg)

    async def _maybe_send_overflow(
        self, reminder: Reminder, now: datetime, profile: EscalationProfile
    ) -> None:
        """Send overflow reminders after configured time with no ack."""
        assert reminder.id is not None
        assert reminder.starts_at is not None
        assert profile.overflow is not None

        # Apply nag_interval_min override if set
        effective_interval = profile.overflow.interval_min
        if reminder.nag_interval_min is not None:
            effective_interval = reminder.nag_interval_min

        last_sent = self._repo.get_last_reminder_time(reminder.id)
        if last_sent is not None:
            next_send = last_sent + timedelta(minutes=effective_interval)
            if now < next_send:
                return

        msg = self._format_message(profile.overflow.message, reminder, now)
        sent = await self._send_to_recipients(reminder, msg, profile.overflow.target)
        if sent:
            self._repo.log_reminder(reminder.id, msg)

    async def _send_to_recipients(
        self, reminder: Reminder, message: str, target: str
    ) -> bool:
        """Send message to resolved recipients based on target.

        If ``ack_token_service`` is configured, a one-time ack URL is
        generated and appended to the message *before* sending.  The token
        hash is only persisted if the send succeeds (E-10, REQ-12).

        Returns True if at least one message was sent successfully.
        """
        recipients = self._resolve_target(reminder, target)
        if not recipients:
            return False

        # Prepare ack token (no storage yet) — REQ-3, REQ-12, E-10
        token_meta = None  # (token_hash, reminder_id, expires_at)
        outbound_message = message
        if self._ack_token_service is not None and reminder.id is not None:
            prepared = self._ack_token_service.prepare_token(reminder.id)
            if prepared is not None:
                ack_url, token_hash, expires_at = prepared
                outbound_message = message + f"\nAck: {ack_url}"
                token_meta = (token_hash, reminder.id, expires_at)

        sent_any = False
        for recipient in recipients:
            sent = await self._sender.send_message(recipient, outbound_message)
            if sent:
                sent_any = True

        # Persist token only after at least one successful send (E-10)
        if sent_any and token_meta is not None and self._ack_token_service is not None:
            token_hash, r_id, expires_at = token_meta
            self._ack_token_service.commit_token(
                token_hash=token_hash,
                reminder_id=r_id,
                expires_at=expires_at,
            )

        return sent_any

    def _resolve_target(self, reminder: Reminder, target: str) -> list[str]:
        """Resolve target to list of recipient phone numbers.

        - "self" → [owner_number]
        - "escalate" → [owner_number, escalate_to] if escalate_to is set, else [owner_number]
        """
        recipients = [self._recipient]  # Owner always gets their own reminders

        if target == "escalate":
            escalate_to = getattr(reminder, "escalate_to", None)
            if escalate_to:
                # Add escalate_to recipient (owner already in list)
                if escalate_to not in recipients:
                    recipients.append(escalate_to)

        return recipients

    def _format_message(self, template: str, reminder: Reminder, now: datetime) -> str:
        """Format a reminder message template."""
        assert reminder.starts_at is not None

        mins_until = max(
            0,
            int((reminder.starts_at - now).total_seconds() / 60),
        )
        mins_ago = max(
            0,
            int((now - reminder.starts_at).total_seconds() / 60),
        )
        time_str = reminder.starts_at.strftime("%H:%M")

        return template.format(
            title=reminder.title,
            time=time_str,
            link=reminder.link or "(no link)",
            description=reminder.description or "",
            mins_until=mins_until,
            mins_ago=mins_ago,
        )
