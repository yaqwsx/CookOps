"""Deterministic development identities at the trusted-provider boundary.

This adapter deliberately resolves only pre-existing ``dummy`` external identities.
It never accepts an email address or creates an identity, user, membership, or role.
The shared human-authentication service remains responsible for the access gate and
browser-session issuance.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.human_authentication import (
    HumanAuthenticationDenied,
    TrustedIdentityAssertion,
)
from cookops.persistence.models import ExternalIdentity, User


@dataclass(frozen=True, slots=True)
class DummyIdentity:
    """One selectable, pre-provisioned development identity."""

    subject: str
    display_name: str


class DummyIdentityProvider:
    """Look up deterministic dummy-provider assertions from existing records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_selectable(self) -> tuple[DummyIdentity, ...]:
        """Return enabled dummy identities in a deterministic presentation order.

        This is intentionally available only through the development HTTP adapter.
        It includes recognizable identities without CookOps access so automated
        tests can exercise the shared denial path.
        """

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ExternalIdentity.provider_subject, User.display_name)
                    .join(User, User.id == ExternalIdentity.user_id)
                    .where(
                        ExternalIdentity.provider == "dummy",
                        User.disabled_at.is_(None),
                    )
                    .order_by(User.display_name, ExternalIdentity.provider_subject)
                )
            ).all()
        return tuple(
            DummyIdentity(subject=provider_subject, display_name=display_name)
            for provider_subject, display_name in rows
        )

    async def assertion_for_subject(self, subject: str) -> TrustedIdentityAssertion:
        """Resolve one existing enabled dummy identity or fail non-enumeratingly."""

        async with self._session_factory() as session:
            identity = await session.scalar(
                select(ExternalIdentity)
                .join(User, User.id == ExternalIdentity.user_id)
                .where(
                    ExternalIdentity.provider == "dummy",
                    ExternalIdentity.provider_subject == subject,
                    User.disabled_at.is_(None),
                )
            )
        if identity is None:
            raise HumanAuthenticationDenied("authentication denied")
        return TrustedIdentityAssertion(
            provider="dummy",
            provider_subject=identity.provider_subject,
            verified_email=identity.verified_email,
        )
