"""Signal incoming message handler.

Parses Signal messages and delegates to ReminderService (DRY).
Single Responsibility: parses text commands, delegates to service layer.
"""

from __future__ import annotations

import logging
from typing import Optional

from .models.reminder import Reminder, ReminderState
from .services.reminder_service import (
    ReminderNotFoundError,
    ReminderService,
)
from .services.notification.base import MessageReceiver, MessageSender
from .services.state_machine import InvalidTransitionError

logger = logging.getLogger(__name__)


class SignalHandler:
    """Handles incoming Signal messages and dispatches commands."""

    def __init__(
        self,
        service: ReminderService,
        receiver: MessageReceiver,
        sender: MessageSender,
        owner_number: str,
        ack_keywords: list[str],
        skip_keywords: list[str],
        list_keywords: list[str],
        help_keywords: list[str],
    ) -> None:
        self._service = service
        self._receiver = receiver
        self._sender = sender
        self._owner = owner_number
        self._ack_keywords = [k.lower() for k in ack_keywords]
        self._skip_keywords = [k.lower() for k in skip_keywords]
        self._list_keywords = [k.lower() for k in list_keywords]
        self._help_keywords = [k.lower() for k in help_keywords]

    async def poll(self) -> None:
        """Poll for incoming messages and process commands."""
        messages = await self._receiver.receive_messages()

        for msg in messages:
            # Only process messages from the owner
            if msg.sender != self._owner:
                logger.debug("Ignoring message from %s", msg.sender)
                continue

            body = msg.body.strip().lower()
            if not body:
                continue

            await self._handle_command(body, msg.body.strip())

    async def _handle_command(self, body_lower: str, body_raw: str) -> None:
        """Route a command to the appropriate handler."""
        if body_lower in self._ack_keywords:
            await self._handle_ack(body_raw)
        elif body_lower in self._skip_keywords:
            await self._handle_skip()
        elif body_lower in self._list_keywords:
            await self._handle_list()
        elif body_lower in self._help_keywords:
            await self._handle_help()
        else:
            logger.debug("Unrecognised Signal command: %s", body_lower)

    async def _handle_ack(self, keyword: str) -> None:
        """Acknowledge the most recent reminding reminder."""
        reminder = self._find_active_reminder()
        if reminder is None:
            await self._sender.send_message(
                self._owner, "No active reminder to acknowledge."
            )
            return

        try:
            self._service.acknowledge(reminder.id, keyword)  # type: ignore
            await self._sender.send_message(
                self._owner,
                f"Acknowledged: {reminder.title}. Reminders stopped.",
            )
        except InvalidTransitionError:
            await self._sender.send_message(
                self._owner,
                f"Cannot acknowledge {reminder.title} (state: {reminder.state.value}).",
            )

    async def _handle_skip(self) -> None:
        """Skip the most recent reminding reminder."""
        reminder = self._find_active_reminder()
        if reminder is None:
            await self._sender.send_message(self._owner, "No active reminder to skip.")
            return

        try:
            self._service.skip(reminder.id)  # type: ignore
            await self._sender.send_message(
                self._owner,
                f"Skipped: {reminder.title}. Reminders stopped.",
            )
        except InvalidTransitionError:
            await self._sender.send_message(
                self._owner,
                f"Cannot skip {reminder.title} (state: {reminder.state.value}).",
            )

    async def _handle_list(self) -> None:
        """List upcoming reminders."""
        reminders = self._service.list_reminders()
        active = [
            r
            for r in reminders
            if r.state in (ReminderState.PENDING, ReminderState.REMINDING)
        ]

        if not active:
            await self._sender.send_message(self._owner, "No upcoming reminders.")
            return

        lines = []
        for r in active:
            time_str = r.starts_at.strftime("%d %b %H:%M") if r.starts_at else "?"
            lines.append(f"[{r.id}] {r.title} - {time_str} ({r.state.value})")

        await self._sender.send_message(
            self._owner, "Upcoming reminders:\n" + "\n".join(lines)
        )

    async def _handle_help(self) -> None:
        """Send help text."""
        await self._sender.send_message(
            self._owner,
            "Klaxxon commands:\n"
            "  ack / joining - acknowledge active reminder\n"
            "  skip - skip active reminder\n"
            "  list / reminders - show upcoming\n"
            "  help - this message",
        )

    def _find_active_reminder(self) -> Optional[Reminder]:
        """Find the most recently reminding reminder, or most recent pending."""
        reminding = self._service.list_reminders(state=ReminderState.REMINDING)
        if reminding:
            return reminding[0]  # Most urgent (earliest starts_at)

        pending = self._service.list_reminders(state=ReminderState.PENDING)
        if pending:
            return pending[0]

        return None
