"""AckToken model: one-time web-acknowledge token for nag notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class AckToken:
    """A single-use, time-bounded token that lets a user acknowledge a reminder
    without needing the API bearer token.

    Raw tokens are NEVER stored — only the SHA-256 hash is persisted.
    """

    id: Optional[int] = None
    token_hash: str = ""
    reminder_id: int = 0
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    used: bool = False
    used_at: Optional[datetime] = None
