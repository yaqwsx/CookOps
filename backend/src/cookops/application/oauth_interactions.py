"""A narrow, transport-free seam for browser-approved OAuth interactions.

The disposable OAuth spike is deliberately not a production service.  This
module therefore contains no URL, credential, or route: a production OAuth
component must explicitly provide the private client protocol before an HTTP
consent UI can be mounted.  Both Google and dummy login already issue the same
browser session, so this service applies the normal current-membership gate to
either provider without interpreting a provider credential.
"""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.human_authentication import HumanAuthenticationService

InteractionDecision = Literal["approve", "deny"]


class PrivateInteractionApprovalClient(Protocol):
    """Authenticated private call owned by the OAuth service boundary."""

    async def record_approval(
        self, *, interaction_uid: str, subject: UUID, decision: InteractionDecision
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class OAuthInteractionApprovalService:
    """Authorize a one-time interaction with an existing browser session."""

    browser_sessions: BrowserSessionService
    human_authentication: HumanAuthenticationService
    private_client: PrivateInteractionApprovalClient

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
