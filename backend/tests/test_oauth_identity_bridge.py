"""Evidence that provider adapters share the one-time OAuth approval bridge."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cookops.application.browser_sessions import (
    AuthenticatedBrowserSession,
    IssuedBrowserSession,
)
from cookops.application.google_identities import GoogleIdentityProvider
from cookops.application.human_authentication import (
    CompletedHumanAuthentication,
    CurrentHumanIdentity,
    HumanAuthenticationDenied,
    TrustedIdentityAssertion,
)
from cookops.application.oauth_interactions import OAuthInteractionApprovalService


def _session(user_id: UUID) -> IssuedBrowserSession:
    now = datetime.now(UTC)
    return IssuedBrowserSession(
        id=uuid4(),
        secret="opaque-browser-session",
        user_id=user_id,
        expires_at=now + timedelta(hours=1),
    )


@dataclass(frozen=True)
class DummyAdapterStub:
    """The development adapter seam without a database or HTTP transport."""

    subject: str
    user_id: UUID

    async def authenticate(self, raw_selection: str) -> CompletedHumanAuthentication:
        if raw_selection != self.subject:
            raise HumanAuthenticationDenied("authentication denied")
        return CompletedHumanAuthentication(self.user_id, _session(self.user_id))


@dataclass(frozen=True)
class GoogleAdapterStub:
    provider: GoogleIdentityProvider

    async def authenticate(self, raw_token: str) -> CompletedHumanAuthentication:
        return await self.provider.complete_id_token(raw_token)


async def _approve_once(adapter: object, credential: str) -> UUID:
    completed = await adapter.authenticate(credential)  # type: ignore[attr-defined]
    browser_sessions = AsyncMock()
    issued = completed.browser_session
    browser_sessions.authenticate.return_value = AuthenticatedBrowserSession(
        id=issued.id,
        user_id=issued.user_id,
        expires_at=issued.expires_at,
        last_used_at=issued.expires_at - timedelta(minutes=1),
    )
    human_authentication = AsyncMock()
    human_authentication.current_identity.return_value = CurrentHumanIdentity(
        user_id=completed.user_id, display_name="Member", verified_email="member@example.test"
    )
    private_client = AsyncMock()
    private_client.record_approval.return_value = True
    bridge = OAuthInteractionApprovalService(browser_sessions, human_authentication, private_client)
    assert await bridge.submit(
        browser_session_secret=issued.secret,
        interaction_uid="N9E_oxk7dD9t7rR10dj-3",
        decision="approve",
    )
    browser_sessions.authenticate.assert_awaited_once_with(issued.secret)
    private_client.record_approval.assert_awaited_once()
    call = private_client.record_approval.await_args.kwargs
    assert set(call) == {"interaction_uid", "subject", "decision"}
    assert call["subject"] == completed.user_id
    return cast(UUID, call["subject"])


@pytest.mark.anyio
async def test_dummy_and_google_adapters_use_the_same_subject_bound_bridge() -> None:
    dummy_user = uuid4()
    google_user = uuid4()
    dummy = DummyAdapterStub("dummy-alice", dummy_user)
    google_human_authentication = AsyncMock()
    google_human_authentication.complete.return_value = CompletedHumanAuthentication(
        google_user, _session(google_user)
    )
    google_provider = GoogleIdentityProvider(
        google_human_authentication,
        "google-client",
        token_verifier=lambda raw, audience: (
            {
                "iss": "https://accounts.google.com",
                "aud": audience,
                "email_verified": True,
                "sub": "google-subject",
                "email": "member@example.test",
            }
            if raw == "opaque-google-token"
            else {}
        ),
    )

    assert await _approve_once(dummy, "dummy-alice") == dummy_user
    google = GoogleAdapterStub(google_provider)
    assert await _approve_once(google, "opaque-google-token") == google_user
    google_human_authentication.complete.assert_awaited_once()
    assert google_human_authentication.complete.await_args.args == (
        TrustedIdentityAssertion(
            provider="google",
            provider_subject="google-subject",
            verified_email="member@example.test",
        ),
    )
    assert "opaque-google-token" not in repr(google_human_authentication.mock_calls)

    with pytest.raises(HumanAuthenticationDenied):
        await dummy.authenticate("unknown-dummy")
    with pytest.raises(HumanAuthenticationDenied):
        await google_provider.complete_id_token("wrong-google-token")


@pytest.mark.anyio
async def test_bridge_rejects_a_session_without_current_identity() -> None:
    user_id = uuid4()
    completed = await DummyAdapterStub("dummy-alice", user_id).authenticate("dummy-alice")
    browser_sessions = AsyncMock()
    issued = completed.browser_session
    browser_sessions.authenticate.return_value = AuthenticatedBrowserSession(
        id=issued.id,
        user_id=issued.user_id,
        expires_at=issued.expires_at,
        last_used_at=issued.expires_at - timedelta(minutes=1),
    )
    human_authentication = AsyncMock()
    human_authentication.current_identity.return_value = None
    private_client = AsyncMock()
    bridge = OAuthInteractionApprovalService(browser_sessions, human_authentication, private_client)

    assert not await bridge.submit(
        browser_session_secret="opaque-session",
        interaction_uid="N9E_oxk7dD9t7rR10dj-3",
        decision="approve",
    )
    private_client.record_approval.assert_not_awaited()


@pytest.mark.anyio
async def test_bridge_uses_current_identity_not_an_arbitrary_adapter_subject() -> None:
    adapter_user = uuid4()
    current_user = uuid4()
    completed = await DummyAdapterStub("dummy-alice", adapter_user).authenticate("dummy-alice")
    issued = completed.browser_session
    browser_sessions = AsyncMock()
    browser_sessions.authenticate.return_value = AuthenticatedBrowserSession(
        id=issued.id,
        user_id=issued.user_id,
        expires_at=issued.expires_at,
        last_used_at=issued.expires_at - timedelta(minutes=1),
    )
    human_authentication = AsyncMock()
    human_authentication.current_identity.return_value = CurrentHumanIdentity(
        user_id=current_user, display_name="Current member", verified_email="member@example.test"
    )
    private_client = AsyncMock()
    private_client.record_approval.return_value = True
    bridge = OAuthInteractionApprovalService(browser_sessions, human_authentication, private_client)

    assert await bridge.submit(
        browser_session_secret=issued.secret,
        interaction_uid="N9E_oxk7dD9t7rR10dj-3",
        decision="approve",
    )
    assert private_client.record_approval.await_args.kwargs["subject"] == current_user
    assert private_client.record_approval.await_args.kwargs["subject"] != completed.user_id
