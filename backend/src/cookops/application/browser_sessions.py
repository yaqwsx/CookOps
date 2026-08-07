"""Server-side, revocable browser-session secrets.

The web adapter is the only caller that may put :class:`IssuedBrowserSession.secret`
into an HTTP-only cookie.  This module never stores or returns that value after
issuance; PostgreSQL retains a keyed digest only.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.persistence.models import BrowserSession, User

_HMAC_KEY_BYTES = 32
_SESSION_SECRET_BYTES = 32

Clock = Callable[[], datetime]


class BrowserSessionConfigurationError(ValueError):
    """The session secret deployment configuration is unsafe or malformed."""


def decode_browser_session_hmac_key(encoded_key: str) -> bytes:
    """Decode one canonical, unpadded base64url 256-bit deployment key.

    Accepting only one textual representation avoids accidental whitespace or
    padding changes in deployment configuration. Replacing this key deliberately
    invalidates every existing browser session, which is fail-closed key rotation.
    """

    if not isinstance(encoded_key, str) or not encoded_key:
        raise BrowserSessionConfigurationError("browser session HMAC key must be nonblank")
    if "=" in encoded_key or any(character.isspace() for character in encoded_key):
        raise BrowserSessionConfigurationError(
            "browser session HMAC key must be unpadded base64url without whitespace"
        )
    try:
        decoded = base64.b64decode(encoded_key + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise BrowserSessionConfigurationError(
            "browser session HMAC key must be unpadded base64url"
        ) from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != encoded_key or len(decoded) != _HMAC_KEY_BYTES:
        raise BrowserSessionConfigurationError(
            "browser session HMAC key must encode exactly 32 bytes"
        )
    return decoded


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_timestamp(timestamp: datetime, *, field: str) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return timestamp.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class IssuedBrowserSession:
    id: UUID
    secret: str
    user_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedBrowserSession:
    id: UUID
    user_id: UUID
    expires_at: datetime
    last_used_at: datetime


class BrowserSessionService:
    """Issue, authenticate, and revoke opaque browser sessions atomically."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        encoded_hmac_key: str,
        clock: Clock = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._hmac_key = decode_browser_session_hmac_key(encoded_hmac_key)
        self._clock = clock

    def _digest(self, secret: str) -> bytes | None:
        if not isinstance(secret, str) or not secret or len(secret) > 512:
            return None
        try:
            secret_bytes = secret.encode("ascii")
        except UnicodeEncodeError:
            return None
        return hmac.new(self._hmac_key, secret_bytes, hashlib.sha256).digest()

    def _now(self) -> datetime:
        return _normalize_timestamp(self._clock(), field="clock result")

    async def issue(self, *, user_id: UUID, expires_at: datetime) -> IssuedBrowserSession:
        """Create a session for an enabled internal user.

        A caller receives the raw secret exactly once and must immediately set it
        as a secure HTTP-only cookie. A disabled or unknown user cannot receive a
        session, even if a stale authentication adapter attempts issuance.
        """

        now = self._now()
        normalized_expiry = _normalize_timestamp(expires_at, field="expires_at")
        if normalized_expiry <= now:
            raise ValueError("expires_at must be after issuance time")

        secret = secrets.token_urlsafe(_SESSION_SECRET_BYTES)
        digest = self._digest(secret)
        if digest is None:
            raise RuntimeError("system session secret generator produced an invalid secret")
        record = BrowserSession(
            user_id=user_id,
            secret_hmac=digest,
            created_at=now,
            expires_at=normalized_expiry,
        )
        async with self._session_factory() as session, session.begin():
            active_user_id = await session.scalar(
                select(User.id)
                .where(User.id == user_id, User.disabled_at.is_(None))
                .with_for_update(read=True, of=User)
            )
            if active_user_id is None:
                raise PermissionError("cannot issue a session for an inactive user")
            session.add(record)
            await session.flush()
        return IssuedBrowserSession(
            id=record.id,
            secret=secret,
            user_id=user_id,
            expires_at=normalized_expiry,
        )

    async def authenticate(self, secret: str) -> AuthenticatedBrowserSession | None:
        """Resolve an active opaque secret and record a monotonic use time."""

        digest = self._digest(secret)
        if digest is None:
            return None
        now = self._now()
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(BrowserSession)
                .join(User, User.id == BrowserSession.user_id)
                .where(
                    BrowserSession.secret_hmac == digest,
                    BrowserSession.revoked_at.is_(None),
                    BrowserSession.expires_at > now,
                    User.disabled_at.is_(None),
                )
                # Lock both the credential and its authority source. Otherwise a
                # concurrent user disable could commit between this read and the
                # authentication result becoming visible to an HTTP adapter.
                .with_for_update(of=(BrowserSession, User))
            )
            if record is None:
                return None
            previous_use = record.last_used_at or record.created_at
            last_used_at = max(previous_use, record.created_at, now)
            # The predicate above means the timestamp remains strictly before
            # expiry. The row lock makes concurrent requests monotonic.
            record.last_used_at = last_used_at
            await session.flush()
            return AuthenticatedBrowserSession(
                id=record.id,
                user_id=record.user_id,
                expires_at=record.expires_at,
                last_used_at=last_used_at,
            )

    async def logout(self, secret: str, *, user_id: UUID) -> bool:
        """Revoke the current user's own session and retain their attribution."""

        return await self._revoke(secret, revoked_by_user_id=user_id, expected_user_id=user_id)

    async def revoke(self, secret: str, *, revoked_by_user_id: UUID) -> bool:
        """Revoke a session with explicit attribution after adapter authorization.

        Administrative authorization is intentionally outside this primitive; the
        future HTTP/application adapter supplies a trusted authorized actor.
        """

        return await self._revoke(secret, revoked_by_user_id=revoked_by_user_id)

    async def _revoke(
        self,
        secret: str,
        *,
        revoked_by_user_id: UUID,
        expected_user_id: UUID | None = None,
    ) -> bool:
        digest = self._digest(secret)
        if digest is None:
            return False
        now = self._now()
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(BrowserSession)
                .where(BrowserSession.secret_hmac == digest)
                .with_for_update(of=BrowserSession)
            )
            if (
                record is None
                or record.revoked_at is not None
                or (expected_user_id is not None and record.user_id != expected_user_id)
            ):
                return False
            revoker = await session.scalar(
                select(User.id)
                .where(User.id == revoked_by_user_id, User.disabled_at.is_(None))
                .with_for_update(read=True, of=User)
            )
            if revoker is None:
                return False
            # Preserve database lifecycle invariants under a backwards-moving app
            # clock or a concurrent authentication completed immediately before us.
            record.revoked_at = max(
                now, record.created_at, record.last_used_at or record.created_at
            )
            record.revoked_by_user_id = revoked_by_user_id
            await session.flush()
            return True
