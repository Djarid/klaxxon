"""Signal notification adapter via signal-cli REST API.

Implements both MessageSender and MessageReceiver.
Liskov Substitution: can be swapped for any other implementation
of these interfaces without changing the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from .base import IncomingMessage, MessageReceiver, MessageSender

logger = logging.getLogger(__name__)


class SignalClient(MessageSender, MessageReceiver):
    """Signal adapter using signal-cli REST API."""

    def __init__(
        self,
        api_url: str,
        account: str,
        timeout: float = 10.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._account = account
        self._timeout = timeout

    async def send_message(self, recipient: str, text: str) -> bool:
        """Send a Signal message via the REST API."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._api_url}/v2/send",
                    json={
                        "message": text,
                        "number": self._account,
                        "recipients": [recipient],
                    },
                )
                if resp.status_code == 201 or resp.status_code == 200:
                    logger.debug("Signal message sent to %s", recipient)
                    return True
                logger.warning("Signal send failed: %d %s", resp.status_code, resp.text)
                return False
        except httpx.HTTPError as e:
            logger.error("Signal send error: %s", e)
            return False

    async def receive_messages(self) -> list[IncomingMessage]:
        """Poll signal-cli for incoming messages."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._api_url}/v1/receive/{self._account}")
                if resp.status_code != 200:
                    logger.warning("Signal receive failed: %d", resp.status_code)
                    return []

                messages = []
                for item in resp.json():
                    envelope = item.get("envelope", {})
                    data_msg = envelope.get("dataMessage")
                    if data_msg is None:
                        continue

                    body = data_msg.get("message", "")
                    if not body:
                        continue

                    sender = envelope.get("sourceNumber", "")
                    ts = envelope.get("timestamp")
                    timestamp = None
                    if ts:
                        timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

                    messages.append(
                        IncomingMessage(
                            sender=sender,
                            body=body.strip(),
                            timestamp=timestamp,
                        )
                    )
                return messages
        except httpx.HTTPError as e:
            logger.error("Signal receive error: %s", e)
            return []

    async def is_available(self) -> bool:
        """Check if the signal-cli REST API is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._api_url}/v1/about")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
