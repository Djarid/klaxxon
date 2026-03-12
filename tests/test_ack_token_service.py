"""Unit tests for AckTokenService.

Tests token creation, redemption, expiry, replay prevention, and scoping.
All tests are written against the specification in .Claude/plans/nag-ack-token.md
and MUST FAIL until the implementation exists.

Import strategy: modules that don't exist yet are imported lazily inside each
test so that collection succeeds and failures come from assertions, not
ModuleNotFoundError.  Once the implementation is in place, these imports will
resolve and the tests will drive towards green.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Helper: import the not-yet-existing modules.  Raises ImportError (which
# pytest surfaces as a collection-time ERRORS) only if the symbol truly can't
# be imported.  We convert that to a clear FAIL so the CI output is readable.
# ---------------------------------------------------------------------------


def _import_ack_token():
    """Return AckToken dataclass.  Fails the test if not yet implemented."""
    try:
        from src.models.ack_token import AckToken

        return AckToken
    except ImportError as exc:
        pytest.fail(f"src.models.ack_token not yet implemented: {exc}")


def _import_ack_token_service():
    """Return (AckTokenService, TokenNotFoundError, TokenExpiredError, TokenAlreadyUsedError)."""
    try:
        from src.services.ack_token_service import (
            AckTokenService,
            TokenAlreadyUsedError,
            TokenExpiredError,
            TokenNotFoundError,
        )

        return (
            AckTokenService,
            TokenNotFoundError,
            TokenExpiredError,
            TokenAlreadyUsedError,
        )
    except ImportError as exc:
        pytest.fail(f"src.services.ack_token_service not yet implemented: {exc}")


# ---------------------------------------------------------------------------
# Minimal in-memory repository for service-layer unit tests.
# We do NOT import the real SqliteAckTokenRepository here.
# ---------------------------------------------------------------------------


class InMemoryAckTokenRepository:
    """In-memory AckTokenRepository for fast unit tests."""

    def __init__(self) -> None:
        self._store: dict = {}

    def store_token(
        self,
        token_hash: str,
        reminder_id: int,
        expires_at: datetime,
    ) -> None:
        AckToken = _import_ack_token()
        token = AckToken(
            id=len(self._store) + 1,
            token_hash=token_hash,
            reminder_id=reminder_id,
            created_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            used=False,
            used_at=None,
        )
        self._store[token_hash] = token

    def get_by_hash(self, token_hash: str):
        return self._store.get(token_hash)

    def mark_used(self, token_hash: str) -> bool:
        token = self._store.get(token_hash)
        if token is None or token.used:
            return False
        token.used = True
        token.used_at = datetime.now(timezone.utc)
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ack_token_repo() -> InMemoryAckTokenRepository:
    return InMemoryAckTokenRepository()


@pytest.fixture
def ack_token_service(ack_token_repo: InMemoryAckTokenRepository):
    """AckTokenService with in-memory repo and configured base_url."""
    AckTokenService, *_ = _import_ack_token_service()
    return AckTokenService(
        repository=ack_token_repo,
        base_url="https://klaxxon.example.com",
    )


@pytest.fixture
def ack_token_service_no_base_url(ack_token_repo: InMemoryAckTokenRepository):
    """AckTokenService with no base_url configured."""
    AckTokenService, *_ = _import_ack_token_service()
    return AckTokenService(
        repository=ack_token_repo,
        base_url=None,
    )


# ===========================================================================
# AC-1 / REQ-1,2,3,10,11 — Token creation
# ===========================================================================


class TestCreateToken:
    """Tests for AckTokenService.create_token()."""

    def test_create_token_returns_ack_url(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """create_token() returns a URL containing the raw token when base_url is set."""
        result = ack_token_service.create_token(reminder_id=42)

        assert result is not None
        assert result.startswith("https://klaxxon.example.com/ack/")

    def test_create_token_url_contains_43_char_token(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """The raw token embedded in the URL is 43 characters (32-byte urlsafe b64, REQ-10)."""
        result = ack_token_service.create_token(reminder_id=42)

        assert result is not None
        prefix = "https://klaxxon.example.com/ack/"
        raw_token = result[len(prefix) :]
        assert len(raw_token) == 43, (
            f"Expected 43-char token (secrets.token_urlsafe(32)), got {len(raw_token)}: {raw_token!r}"
        )

    def test_create_token_stores_hash_not_raw(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """The raw token is NOT stored — only its SHA-256 hash is persisted (REQ-11)."""
        result = ack_token_service.create_token(reminder_id=42)

        assert result is not None
        raw_token = result.split("/ack/")[1]

        # Verify hash IS in store
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        stored = ack_token_repo.get_by_hash(token_hash)
        assert stored is not None, "Expected SHA-256 hash to be in the store"

        # Verify raw token is NOT a key in the store
        raw_stored = ack_token_repo.get_by_hash(raw_token)
        assert raw_stored is None, "Raw token must not be stored as a key (REQ-11)"

    def test_create_token_stores_correct_reminder_id(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Stored token is associated with the correct reminder_id (REQ-2)."""
        result = ack_token_service.create_token(reminder_id=99)

        assert result is not None
        raw_token = result.split("/ack/")[1]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        stored = ack_token_repo.get_by_hash(token_hash)

        assert stored is not None
        assert stored.reminder_id == 99

    def test_create_token_stored_with_unused_flag(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Newly created token has used=False (REQ-2)."""
        result = ack_token_service.create_token(reminder_id=1)

        assert result is not None
        raw_token = result.split("/ack/")[1]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        stored = ack_token_repo.get_by_hash(token_hash)

        assert stored is not None
        assert stored.used is False

    def test_create_token_expires_in_24_hours(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Token expires 24 hours from creation (REQ-2, REQ-9)."""
        before = datetime.now(timezone.utc)
        result = ack_token_service.create_token(reminder_id=5)
        after = datetime.now(timezone.utc)

        assert result is not None
        raw_token = result.split("/ack/")[1]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        stored = ack_token_repo.get_by_hash(token_hash)

        assert stored is not None
        assert stored.expires_at is not None
        expected_min = before + timedelta(hours=24)
        expected_max = after + timedelta(hours=24)
        assert expected_min <= stored.expires_at <= expected_max, (
            f"expires_at={stored.expires_at} not in [{expected_min}, {expected_max}]"
        )

    def test_create_token_generates_unique_tokens_per_call(
        self,
        ack_token_service,
    ) -> None:
        """Each call to create_token generates a fresh, unique token (REQ-1)."""
        url1 = ack_token_service.create_token(reminder_id=10)
        url2 = ack_token_service.create_token(reminder_id=10)

        assert url1 != url2

    def test_create_token_returns_none_when_no_base_url(
        self,
        ack_token_service_no_base_url,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """create_token() returns None and stores no token when base_url is None (REQ-4, E-1)."""
        result = ack_token_service_no_base_url.create_token(reminder_id=42)

        assert result is None
        assert len(ack_token_repo._store) == 0

    def test_create_token_base_url_trailing_slash_stripped(
        self,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Trailing slash on base_url is handled gracefully — no double slash (E-11)."""
        AckTokenService, *_ = _import_ack_token_service()
        service = AckTokenService(
            repository=ack_token_repo,
            base_url="https://klaxxon.example.com/",
        )
        result = service.create_token(reminder_id=7)

        assert result is not None
        assert "/ack//" not in result
        assert result.startswith("https://klaxxon.example.com/ack/")


# ===========================================================================
# AC-3 / REQ-6 — Successful token redemption
# ===========================================================================


class TestRedeemToken:
    """Tests for AckTokenService.redeem_token()."""

    def test_redeem_valid_token_returns_reminder_id(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """redeem_token() returns the reminder_id for a valid token."""
        url = ack_token_service.create_token(reminder_id=42)
        assert url is not None
        raw_token = url.split("/ack/")[1]

        result = ack_token_service.redeem_token(raw_token)

        assert result == 42

    def test_redeem_token_marks_it_as_used(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """redeem_token() marks the token used=True (REQ-6e, REQ-7)."""
        url = ack_token_service.create_token(reminder_id=42)
        assert url is not None
        raw_token = url.split("/ack/")[1]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        ack_token_service.redeem_token(raw_token)

        stored = ack_token_repo.get_by_hash(token_hash)
        assert stored is not None
        assert stored.used is True

    def test_redeem_token_sets_used_at(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """redeem_token() sets used_at to approximately now."""
        url = ack_token_service.create_token(reminder_id=42)
        assert url is not None
        raw_token = url.split("/ack/")[1]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        before = datetime.now(timezone.utc)
        ack_token_service.redeem_token(raw_token)
        after = datetime.now(timezone.utc)

        stored = ack_token_repo.get_by_hash(token_hash)
        assert stored is not None
        assert stored.used_at is not None
        assert before <= stored.used_at <= after


# ===========================================================================
# AC-4 / REQ-7 — Replay prevention
# ===========================================================================


class TestReplayPrevention:
    """Tests that a used token cannot be redeemed again."""

    def test_redeem_already_used_token_raises(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """redeem_token() raises TokenAlreadyUsedError on second use (REQ-7, AC-4)."""
        _, _, _, TokenAlreadyUsedError = _import_ack_token_service()

        url = ack_token_service.create_token(reminder_id=42)
        assert url is not None
        raw_token = url.split("/ack/")[1]

        ack_token_service.redeem_token(raw_token)  # first — OK

        with pytest.raises(TokenAlreadyUsedError):
            ack_token_service.redeem_token(raw_token)  # second — must raise

    def test_used_token_stays_used_after_replay_rejection(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """After replay rejection the token remains used=True (not reset)."""
        _, _, _, TokenAlreadyUsedError = _import_ack_token_service()

        url = ack_token_service.create_token(reminder_id=42)
        assert url is not None
        raw_token = url.split("/ack/")[1]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        ack_token_service.redeem_token(raw_token)

        with pytest.raises(TokenAlreadyUsedError):
            ack_token_service.redeem_token(raw_token)

        stored = ack_token_repo.get_by_hash(token_hash)
        assert stored is not None
        assert stored.used is True


# ===========================================================================
# AC-5 / REQ-9 — Expired token
# ===========================================================================


class TestExpiredToken:
    """Tests for expired token behaviour."""

    def test_redeem_expired_token_raises(
        self,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """redeem_token() raises TokenExpiredError for an expired token (REQ-9, AC-5)."""
        AckTokenService, _, TokenExpiredError, _ = _import_ack_token_service()
        service = AckTokenService(
            repository=ack_token_repo,
            base_url="https://klaxxon.example.com",
        )

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expired_at = datetime.now(timezone.utc) - timedelta(hours=1)
        ack_token_repo.store_token(
            token_hash=token_hash,
            reminder_id=42,
            expires_at=expired_at,
        )

        with pytest.raises(TokenExpiredError):
            service.redeem_token(raw_token)

    def test_redeem_expired_token_does_not_mark_used(
        self,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Expired token rejection does not consume the token (AC-5)."""
        AckTokenService, _, TokenExpiredError, _ = _import_ack_token_service()
        service = AckTokenService(
            repository=ack_token_repo,
            base_url="https://klaxxon.example.com",
        )

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expired_at = datetime.now(timezone.utc) - timedelta(hours=1)
        ack_token_repo.store_token(
            token_hash=token_hash,
            reminder_id=42,
            expires_at=expired_at,
        )

        with pytest.raises(TokenExpiredError):
            service.redeem_token(raw_token)

        stored = ack_token_repo.get_by_hash(token_hash)
        assert stored is not None
        assert stored.used is False


# ===========================================================================
# AC-6 / REQ-13 — Unknown / not-found token
# ===========================================================================


class TestUnknownToken:
    """Tests for tokens not present in the database."""

    def test_redeem_unknown_token_raises(
        self,
        ack_token_service,
    ) -> None:
        """redeem_token() raises TokenNotFoundError for a token not in DB (AC-6)."""
        _, TokenNotFoundError, *_ = _import_ack_token_service()

        with pytest.raises(TokenNotFoundError):
            ack_token_service.redeem_token("this-token-does-not-exist-xyz")

    def test_redeem_malformed_short_token_raises(
        self,
        ack_token_service,
    ) -> None:
        """Short / malformed tokens raise TokenNotFoundError (E-8)."""
        _, TokenNotFoundError, *_ = _import_ack_token_service()

        with pytest.raises(TokenNotFoundError):
            ack_token_service.redeem_token("short")

    def test_redeem_correct_length_but_unknown_token_raises(
        self,
        ack_token_service,
    ) -> None:
        """Correct-length token with no matching hash raises TokenNotFoundError (E-8)."""
        _, TokenNotFoundError, *_ = _import_ack_token_service()

        with pytest.raises(TokenNotFoundError):
            ack_token_service.redeem_token("a" * 43)


# ===========================================================================
# REQ-8 — Token scoping: each token only acknowledges its own reminder
# ===========================================================================


class TestTokenScoping:
    """Tests that a token is scoped to exactly one reminder."""

    def test_token_returns_only_its_own_reminder_id(
        self,
        ack_token_service,
    ) -> None:
        """A token for reminder #10 returns 10, not 20 (REQ-8)."""
        url10 = ack_token_service.create_token(reminder_id=10)
        url20 = ack_token_service.create_token(reminder_id=20)

        assert url10 is not None
        assert url20 is not None

        raw10 = url10.split("/ack/")[1]
        raw20 = url20.split("/ack/")[1]

        assert ack_token_service.redeem_token(raw10) == 10
        assert ack_token_service.redeem_token(raw20) == 20

    def test_using_token_a_does_not_affect_token_b(
        self,
        ack_token_service,
        ack_token_repo: InMemoryAckTokenRepository,
    ) -> None:
        """Redeeming token A does not consume token B (REQ-8, AC-8)."""
        url_a = ack_token_service.create_token(reminder_id=10)
        url_b = ack_token_service.create_token(reminder_id=10)

        assert url_a is not None
        assert url_b is not None

        raw_a = url_a.split("/ack/")[1]
        raw_b = url_b.split("/ack/")[1]

        ack_token_service.redeem_token(raw_a)

        hash_b = hashlib.sha256(raw_b.encode()).hexdigest()
        stored_b = ack_token_repo.get_by_hash(hash_b)
        assert stored_b is not None
        assert stored_b.used is False  # B untouched


# ===========================================================================
# REQ-10 — Token entropy
# ===========================================================================


class TestTokenEntropy:
    """Tests that tokens carry sufficient entropy."""

    def test_token_only_contains_urlsafe_chars(
        self,
        ack_token_service,
    ) -> None:
        """Raw token must consist only of URL-safe base64 characters (REQ-10)."""
        url = ack_token_service.create_token(reminder_id=1)
        assert url is not None
        raw_token = url.split("/ack/")[1]
        urlsafe_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"  # pragma: allowlist secret
        assert all(c in urlsafe_chars for c in raw_token), (
            f"Token contains non-urlsafe chars: {raw_token!r}"
        )

    def test_token_is_at_least_43_chars(
        self,
        ack_token_service,
    ) -> None:
        """Token must be at least 43 chars (encodes 32 bytes = 256 bits) (REQ-10)."""
        url = ack_token_service.create_token(reminder_id=1)
        assert url is not None
        raw_token = url.split("/ack/")[1]
        assert len(raw_token) >= 43

    def test_1000_tokens_are_all_unique(
        self,
        ack_token_service,
    ) -> None:
        """1000 generated tokens are all unique (birthday-paradox sanity check)."""
        tokens = {
            ack_token_service.create_token(reminder_id=1).split("/ack/")[1]
            for _ in range(1000)
        }
        assert len(tokens) == 1000
