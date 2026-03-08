"""Bearer token authentication middleware.

Single Responsibility: validates auth tokens. Nothing else.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer()

# In-memory token store (loaded from config at startup)
_valid_token_hashes: set[str] = set()


def hash_token(token: str) -> str:
    """SHA-256 hash a bearer token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def register_token(token: str) -> None:
    """Register a valid bearer token."""
    _valid_token_hashes.add(hash_token(token))


def generate_token() -> str:
    """Generate a new random bearer token."""
    return secrets.token_urlsafe(32)


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Security(_security),
) -> str:
    """FastAPI dependency that verifies the bearer token."""
    token_hash = hash_token(credentials.credentials)
    if token_hash not in _valid_token_hashes:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return credentials.credentials
