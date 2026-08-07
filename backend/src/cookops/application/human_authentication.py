"""Complete a trusted human-provider assertion into one CookOps browser session.

Provider adapters are responsible for verifying provider credentials before they
construct :class:`TrustedIdentityAssertion`.  This application service deliberately
does not parse HTTP input or implement a provider-specific verification protocol.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import BrowserSessionService, IssuedBrowserSession
from cookops.persistence.models import (
    ExternalIdentity,
    Organization,
    OrganizationMembership,
    SystemRoleAssignment,
    User,
)

IdentityProvider = Literal["google", "dummy"]


class HumanAuthenticationDenied(PermissionError):
    """A verified provider identity is not currently allowed to use CookOps.

    The deliberately non-enumerating error is safe for an HTTP adapter to map to
    one generic failed-login response.
    """


@dataclass(frozen=True, slots=True)
class TrustedIdentityAssertion:
    """Identity claims already verified by the selected provider adapter.

    This is a narrow trust boundary: Google token validation and the deterministic
    development identity lookup happen outside this service.  Raw HTTP payloads
    must never be passed here directly.
    """

    provider: IdentityProvider
    provider_subject: str
    verified_email: str

    def __post_init__(self) -> None:
        if self.provider not in ("google", "dummy"):
            raise ValueError("provider must be google or dummy")
        if (
            not isinstance(self.provider_subject, str)
            or not self.provider_subject
            or self.provider_subject != self.provider_subject.strip()
            or len(self.provider_subject) > 255
        ):
            raise ValueError(
                "provider_subject must be a nonblank trimmed string of at most 255 characters"
            )
        if (
            not isinstance(self.verified_email, str)
            or not self.verified_email
            or self.verified_email != self.verified_email.strip()
            or len(self.verified_email) > 320
        ):
            raise ValueError(
                "verified_email must be a nonblank trimmed string of at most 320 characters"
            )

    @property
    def normalized_verified_email(self) -> str:
        """Match the database's normalized-email representation."""

        return self.verified_email.lower()


@dataclass(frozen=True, slots=True)
class CompletedHumanAuthentication:
    """The internal user resolved from a provider assertion and their new session."""

    user_id: UUID
    browser_session: IssuedBrowserSession


@dataclass(frozen=True, slots=True)
class CurrentHumanIdentity:
    """An enabled user whose current CookOps access gate still passes."""

    user_id: UUID
    display_name: str
    verified_email: str


@dataclass(frozen=True, slots=True)
class AvailableOrganization:
    """An active organization the current user may open."""

    organization_id: UUID
    name: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalized_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock result must include a timezone")
    return now.astimezone(UTC)


class HumanAuthenticationService:
    """Resolve an existing trusted identity, gate it, then issue a browser session.

    It never creates users, external identities, memberships, or role assignments.
    An invitation/role claim flow is intentionally a separate explicit command.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        browser_sessions: BrowserSessionService,
        *,
        session_lifetime: timedelta,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if session_lifetime <= timedelta():
            raise ValueError("session_lifetime must be positive")
        self._session_factory = session_factory
        self._browser_sessions = browser_sessions
        self._session_lifetime = session_lifetime
        self._clock = clock

    async def complete(self, assertion: TrustedIdentityAssertion) -> CompletedHumanAuthentication:
        """Authorize an existing verified identity and create its opaque session."""

        now = _normalized_now(self._clock)
        async with self._session_factory() as session, session.begin():
            resolved_identity = (
                await session.execute(
                    select(User, ExternalIdentity)
                    .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
                    .where(
                        ExternalIdentity.provider == assertion.provider,
                        ExternalIdentity.provider_subject == assertion.provider_subject,
                        ExternalIdentity.verified_email == assertion.verified_email,
                        ExternalIdentity.normalized_verified_email
                        == assertion.normalized_verified_email,
                        User.verified_email == assertion.verified_email,
                        User.normalized_email == assertion.normalized_verified_email,
                        User.disabled_at.is_(None),
                    )
                    .with_for_update(of=(User, ExternalIdentity))
                )
            ).one_or_none()
            if resolved_identity is None:
                raise HumanAuthenticationDenied("authentication denied")
            user, identity = resolved_identity
            if not await self._has_current_access(
                session,
                user.id,
                assertion.normalized_verified_email,
            ):
                raise HumanAuthenticationDenied("authentication denied")

            # The identity was verified by this completed provider authentication,
            # and a session will be issued only after the current role gate passed.
            user.last_successful_login_at = now
            identity.last_verified_at = now
            user_id = user.id
            browser_session = await self._browser_sessions.issue_in_transaction(
                session,
                user_id=user_id,
                expires_at=now + self._session_lifetime,
            )
        return CompletedHumanAuthentication(user_id=user_id, browser_session=browser_session)

    async def current_identity(self, user_id: UUID) -> CurrentHumanIdentity | None:
        """Re-evaluate access for a previously authenticated browser session.

        A session proves who presented an opaque secret, not an eternal right to
        use CookOps.  Membership and system-role changes therefore take effect on
        the next HTTP operation that requests a current identity.
        """

        async with self._session_factory() as session, session.begin():
            user = await session.scalar(
                select(User)
                .where(User.id == user_id, User.disabled_at.is_(None))
                .with_for_update(of=User)
            )
            if user is None or not await self._has_current_access(
                session, user.id, user.normalized_email
            ):
                return None
            return CurrentHumanIdentity(
                user_id=user.id,
                display_name=user.display_name,
                verified_email=user.verified_email,
            )

    async def available_organizations(
        self, user_id: UUID
    ) -> tuple[AvailableOrganization, ...] | None:
        """List currently readable active organizations without accepting client scope."""

        async with self._session_factory() as session:
            user = await session.scalar(
                select(User).where(User.id == user_id, User.disabled_at.is_(None))
            )
            if user is None or not await self._has_current_access(
                session, user.id, user.normalized_email
            ):
                return None
            is_system_admin = await session.scalar(
                select(SystemRoleAssignment.id).where(
                    SystemRoleAssignment.user_id == user_id,
                    SystemRoleAssignment.role == "system_admin",
                    SystemRoleAssignment.revoked_at.is_(None),
                )
            )
            statement = select(Organization.id, Organization.name).where(
                Organization.retired_at.is_(None)
            )
            if is_system_admin is None:
                statement = statement.join(
                    OrganizationMembership,
                    OrganizationMembership.organization_id == Organization.id,
                ).where(
                    OrganizationMembership.user_id == user_id,
                    OrganizationMembership.invited_email == user.normalized_email,
                    OrganizationMembership.state == "active",
                )
            rows = await session.execute(
                statement.order_by(func.lower(Organization.name), Organization.id)
            )
            return tuple(
                AvailableOrganization(organization_id=organization_id, name=name)
                for organization_id, name in rows
            )

    @staticmethod
    async def _has_current_access(
        session: AsyncSession,
        user_id: UUID,
        normalized_verified_email: str,
    ) -> bool:
        active_membership = await session.scalar(
            select(OrganizationMembership.id)
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.invited_email == normalized_verified_email,
                OrganizationMembership.state == "active",
            )
            .with_for_update(of=OrganizationMembership)
        )
        active_system_role = await session.scalar(
            select(SystemRoleAssignment.id)
            .where(
                SystemRoleAssignment.user_id == user_id,
                SystemRoleAssignment.invited_email == normalized_verified_email,
                SystemRoleAssignment.role == "system_admin",
                SystemRoleAssignment.revoked_at.is_(None),
            )
            .with_for_update(of=SystemRoleAssignment)
        )
        return active_membership is not None or active_system_role is not None
