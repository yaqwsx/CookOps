from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cookops.application.browser_sessions import AuthenticatedBrowserSession
from cookops.application.human_authentication import CurrentHumanIdentity
from cookops.application.oauth_interactions import OAuthInteractionApprovalService
from cookops.oauth_interaction_client import (
    OAuthInteractionUnavailable,
    OAuthPrivateInteractionClient,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _session(user_id: UUID) -> AuthenticatedBrowserSession:
    now = datetime.now(UTC)
    return AuthenticatedBrowserSession(
        id=uuid4(), user_id=user_id, expires_at=now + timedelta(hours=1), last_used_at=now
    )


def _identity(user_id: UUID) -> CurrentHumanIdentity:
    return CurrentHumanIdentity(
        user_id=user_id, display_name="Member", verified_email="member@example.test"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_interaction_approval_uses_the_provider_independent_browser_session_gate(
    decision: str,
) -> None:
    user_id = uuid4()
    browser_sessions = AsyncMock()
    browser_sessions.authenticate.return_value = _session(user_id)
    human_authentication = AsyncMock()
    human_authentication.current_identity.return_value = _identity(user_id)
    private_client = AsyncMock()
    private_client.record_approval.return_value = True
    service = OAuthInteractionApprovalService(
        browser_sessions=browser_sessions,
        human_authentication=human_authentication,
        private_client=private_client,
    )

    assert await service.submit(
        browser_session_secret="opaque-browser-session",
        interaction_uid="N9E_oxk7dD9t7rR10dj-3",
        decision=decision,  # type: ignore[arg-type]
    )
    human_authentication.current_identity.assert_awaited_once_with(user_id)
    private_client.record_approval.assert_awaited_once_with(
        interaction_uid="N9E_oxk7dD9t7rR10dj-3", subject=user_id, decision=decision
    )


@pytest.mark.anyio
async def test_interaction_approval_does_not_issue_for_missing_or_revoked_browser_authority() -> (
    None
):
    user_id = uuid4()
    browser_sessions = AsyncMock()
    browser_sessions.authenticate.side_effect = [None, _session(user_id)]
    human_authentication = AsyncMock()
    human_authentication.current_identity.return_value = None
    private_client = AsyncMock()
    service = OAuthInteractionApprovalService(
        browser_sessions=browser_sessions,
        human_authentication=human_authentication,
        private_client=private_client,
    )

    assert not await service.submit(
        browser_session_secret="missing",
        interaction_uid="N9E_oxk7dD9t7rR10dj-3",
        decision="approve",
    )
    assert not await service.submit(
        browser_session_secret="revoked",
        interaction_uid="N9E_oxk7dD9t7rR10dj-3",
        decision="approve",
    )
    private_client.record_approval.assert_not_awaited()


@pytest.mark.anyio
async def test_interaction_approval_rejects_an_unknown_decision_before_private_delivery() -> None:
    service = OAuthInteractionApprovalService(
        browser_sessions=AsyncMock(), human_authentication=AsyncMock(), private_client=AsyncMock()
    )

    with pytest.raises(ValueError, match="decision"):
        await service.submit(
            browser_session_secret="opaque",
            interaction_uid="N9E_oxk7dD9t7rR10dj-3",
            decision="unexpected",  # type: ignore[arg-type]
        )


@pytest.mark.anyio
async def test_grant_management_passes_current_identity_and_keeps_missing_session_private() -> None:
    user_id = uuid4()
    browser_sessions = AsyncMock()
    browser_sessions.authenticate.side_effect = [None, _session(user_id), _session(user_id)]
    human_authentication = AsyncMock()
    human_authentication.current_identity.return_value = _identity(user_id)
    private_client = AsyncMock()
    private_client.grants.return_value = []
    private_client.revoke_grant.return_value = True
    service = OAuthInteractionApprovalService(
        browser_sessions, human_authentication, private_client
    )

    assert await service.grants(browser_session_secret="missing") is None
    assert await service.grants(browser_session_secret="opaque") == []
    assert await service.revoke_grant(browser_session_secret="opaque", handle="a" * 64)
    private_client.grants.assert_awaited_once_with(subject=user_id)
    private_client.revoke_grant.assert_awaited_once_with(subject=user_id, handle="a" * 64)


@pytest.mark.anyio
async def test_unconfigured_grant_credential_never_calls_private_endpoint() -> None:
    client = OAuthPrivateInteractionClient(
        "http://invalid/details", "details", "http://invalid/approval", "approval"
    )
    with pytest.raises(OAuthInteractionUnavailable, match="not configured"):
        await client.grants(subject=uuid4())
    with pytest.raises(OAuthInteractionUnavailable, match="not configured"):
        await client.revoke_grant(subject=uuid4(), handle="a" * 64)
