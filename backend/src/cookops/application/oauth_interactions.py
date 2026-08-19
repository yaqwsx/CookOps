"""A narrow, transport-free seam for browser-approved OAuth interactions.

The disposable OAuth spike is deliberately not a production service.  This
module therefore contains no URL, credential, or route: a production OAuth
component must explicitly provide the private client protocol before an HTTP
consent UI can be mounted.  Both Google and dummy login already issue the same
browser session, so this service applies the normal current-membership gate to
either provider without interpreting a provider credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.human_authentication import HumanAuthenticationService

if TYPE_CHECKING:
    from cookops.oauth_interaction_client import AuthorizedGrant

InteractionDecision = Literal["approve", "deny"]


@dataclass(frozen=True, slots=True)
class OAuthInteractionDetails:
    interaction_uid: str
    client_name: str
    resource: str
    scopes: tuple[str, ...]


class PrivateInteractionApprovalClient(Protocol):
    """Authenticated private call owned by the OAuth service boundary."""

    async def record_approval(
        self, *, interaction_uid: str, subject: UUID, decision: InteractionDecision
    ) -> bool: ...

    async def interaction_details(
        self, *, interaction_uid: str
    ) -> OAuthInteractionDetails | None: ...

    async def grants(self, *, subject: UUID) -> list[AuthorizedGrant]: ...

    async def revoke_grant(self, *, subject: UUID, handle: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class OAuthInteractionApprovalService:
    """Authorize a one-time interaction with an existing browser session."""

    browser_sessions: BrowserSessionService
    human_authentication: HumanAuthenticationService
    private_client: PrivateInteractionApprovalClient

    async def details(
        self, *, browser_session_secret: str, interaction_uid: str
    ) -> OAuthInteractionDetails | None:
        session = await self.browser_sessions.authenticate(browser_session_secret)
        if session is None:
            return None
        identity = await self.human_authentication.current_identity(session.user_id)
        if identity is None:
            return None
        return await self.private_client.interaction_details(interaction_uid=interaction_uid)

    async def submit(
        self, *, browser_session_secret: str, interaction_uid: str, decision: InteractionDecision
    ) -> bool:
        """Record a decision only for a currently authorized CookOps user.

        The OAuth server, not a browser request, owns all grant binding (client,
        resource, scopes, challenge, expiry and single consumption).  This
        service only supplies the authenticated stable CookOps user UUID.
        """

        if decision not in ("approve", "deny"):
            raise ValueError("decision must be approve or deny")
        session = await self.browser_sessions.authenticate(browser_session_secret)
        if session is None:
            return False
        identity = await self.human_authentication.current_identity(session.user_id)
        if identity is None:
            return False
        return await self.private_client.record_approval(
            interaction_uid=interaction_uid,
            subject=identity.user_id,
            decision=decision,
        )

    async def grants(self, *, browser_session_secret: str) -> list[AuthorizedGrant] | None:
        session = await self.browser_sessions.authenticate(browser_session_secret)
        if session is None:
            return None
        identity = await self.human_authentication.current_identity(session.user_id)
        if identity is None:
            return None
        return await self.private_client.grants(subject=identity.user_id)

    async def revoke_grant(self, *, browser_session_secret: str, handle: str) -> bool | None:
        session = await self.browser_sessions.authenticate(browser_session_secret)
        if session is None:
            return None
        identity = await self.human_authentication.current_identity(session.user_id)
        if identity is None:
            return None
        return await self.private_client.revoke_grant(subject=identity.user_id, handle=handle)
