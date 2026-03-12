"""AckTokenService: generate and redeem one-time web-acknowledge tokens.

Implements REQ-1 through REQ-11 from .Claude/plans/nag-ack-token.md.

Security notes:
- Raw tokens are NEVER stored (REQ-11).  Only SHA-256 hashes are persisted.
- Tokens are 256-bit random (secrets.token_urlsafe(32) = 43 chars, REQ-10).
- Single-use: mark_used is atomic at the repository layer (REQ-7).
- 24-hour expiry (REQ-9).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..repository.base import AckTokenRepository


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class TokenNotFoundError(Exception):
    """Raised when the token hash is not present in the database (AC-6)."""

    pass


class TokenExpiredError(Exception):
    """Raised when the token's expires_at is in the past (AC-5, REQ-9)."""

    pass


class TokenAlreadyUsedError(Exception):
    """Raised when the token has already been redeemed (AC-4, REQ-7)."""

    pass


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

_TOKEN_EXPIRY_HOURS = 24


class AckTokenService:
    """Generates and validates one-time ack tokens.

    Parameters
    ----------
    repository:
        Concrete AckTokenRepository used for persistence.
    base_url:
        The public base URL (e.g. "https://klaxxon.example.com").
        Trailing slash is stripped defensively (E-11).
        If None or empty, create_token() returns None and no token is stored.
    """

    def __init__(
        self,
        repository: AckTokenRepository,
        base_url: Optional[str],
    ) -> None:
        self._repo = repository
        # Strip trailing slash (E-11)
        if base_url:
            self._base_url: Optional[str] = base_url.rstrip("/")
        else:
            self._base_url = None

    def create_token(self, reminder_id: int) -> Optional[str]:
        """Generate a one-time ack token for a reminder.

        Returns the full ack URL (``{base_url}/ack/{raw_token}``) when
        base_url is configured, or None when it is not (REQ-4, E-1).

        The raw token is returned to the caller for URL construction but is
        NOT stored — only the SHA-256 hash is persisted (REQ-11).
        """
        if not self._base_url:
            return None  # REQ-4: graceful degradation

        # REQ-10: 256 bits of entropy (secrets.token_urlsafe(32) → 43 chars)
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        expires_at = datetime.now(timezone.utc) + timedelta(hours=_TOKEN_EXPIRY_HOURS)

        self._repo.store_token(
            token_hash=token_hash,
            reminder_id=reminder_id,
            expires_at=expires_at,
        )

        return f"{self._base_url}/ack/{raw_token}"

    def redeem_token(self, raw_token: str) -> int:
        """Validate and consume a token.  Returns the reminder_id on success.

        Raises
        ------
        TokenNotFoundError
            Token hash not found in the database.
        TokenExpiredError
            Token has passed its expires_at timestamp.
        TokenAlreadyUsedError
            Token has already been marked used (replay prevention).
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        token = self._repo.get_by_hash(token_hash)
        if token is None:
            raise TokenNotFoundError(f"Token not found: {raw_token!r}")

        # Check expiry BEFORE checking used (order matters for error messages)
        now = datetime.now(timezone.utc)
        expires_at = token.expires_at
        if expires_at is not None:
            # Make expires_at timezone-aware if stored naively
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at:
                raise TokenExpiredError(f"Token expired at {expires_at.isoformat()}")

        if token.used:
            raise TokenAlreadyUsedError("Token has already been used")

        # Atomically mark as used — returns False if concurrent request won
        marked = self._repo.mark_used(token_hash)
        if not marked:
            raise TokenAlreadyUsedError("Token has already been used")

        return token.reminder_id
