"""Notification channel interfaces.

Interface Segregation: MessageSender and MessageReceiver are separate.
ReminderEngine only needs MessageSender.
SignalHandler only needs MessageReceiver (and MessageSender for replies).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class IncomingMessage:
    """A message received from a notification channel."""

    sender: str
    body: str
    timestamp: Optional[datetime] = None


class MessageSender(ABC):
    """Interface for sending messages. Used by ReminderEngine."""

    @abstractmethod
    async def send_message(self, recipient: str, text: str) -> bool:
        """Send a message. Returns True on success."""
        ...


class MessageReceiver(ABC):
    """Interface for receiving messages. Used by SignalHandler."""

    @abstractmethod
    async def receive_messages(self) -> list[IncomingMessage]:
        """Poll for new incoming messages."""
        ...
