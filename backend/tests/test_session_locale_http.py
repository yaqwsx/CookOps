import base64
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cookops.application.human_authentication import CurrentHumanIdentity
from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.http_auth import BrowserAuthenticationServices, create_auth_router

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


def test_session_locale_http_contract_is_strict_and_origin_bound() -> None:
    user_id = uuid4()
    identity = CurrentHumanIdentity(user_id, "Alice", "alice@example.test", "cs")
    browser_sessions = MagicMock()
    browser_sessions.authenticate = AsyncMock(return_value=MagicMock(user_id=user_id))
    human_authentication = MagicMock()
    human_authentication.current_identity = AsyncMock(return_value=identity)
    human_authentication.set_current_identity_locale = AsyncMock(
        return_value=CurrentHumanIdentity(user_id, "Alice", "alice@example.test", "en")
    )
    app_settings = Settings(
        environment=Environment.DEVELOPMENT,
        human_auth_provider=HumanAuthProvider.DUMMY,
        browser_session_hmac_key=KEY,
        browser_origin="https://testserver",
    )
    app = FastAPI()
    app.state.browser_authentication = BrowserAuthenticationServices(
        browser_sessions, human_authentication, None, None
    )
    app.include_router(create_auth_router(app_settings))

    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(app_settings.browser_session_cookie_name, "valid")
        assert client.get("/auth/session").json()["preferred_locale"] == "cs"
        updated = client.patch(
            "/auth/session/locale",
            headers={"Origin": "https://testserver"},
            json={"preferred_locale": "en"},
        )
        assert updated.status_code == 200
        assert updated.json()["preferred_locale"] == "en"
        human_authentication.set_current_identity_locale.assert_awaited_once_with(user_id, "en")
        assert (
            client.patch("/auth/session/locale", json={"preferred_locale": "en"}).status_code == 403
        )
        assert (
            client.patch(
                "/auth/session/locale",
                headers={"Origin": "https://foreign.test"},
                json={"preferred_locale": "en"},
            ).status_code
            == 403
        )
        for payload in (
            {},
            {"preferred_locale": "de"},
            {"preferred_locale": "en", "extra": 1},
            {"preferred_locale": 1},
        ):
            assert (
                client.patch(
                    "/auth/session/locale",
                    headers={"Origin": "https://testserver"},
                    json=payload,
                ).status_code
                == 422
            )
        client.cookies.delete(app_settings.browser_session_cookie_name)
        assert (
            client.patch(
                "/auth/session/locale",
                headers={"Origin": "https://testserver"},
                json={"preferred_locale": "en"},
            ).status_code
            == 401
        )
        client.cookies.set(app_settings.browser_session_cookie_name, "invalid")
        browser_sessions.authenticate.return_value = None
        assert (
            client.patch(
                "/auth/session/locale",
                headers={"Origin": "https://testserver"},
                json={"preferred_locale": "en"},
            ).status_code
            == 401
        )
        browser_sessions.authenticate.return_value = MagicMock(user_id=user_id)
        human_authentication.set_current_identity_locale.return_value = None
        assert (
            client.patch(
                "/auth/session/locale",
                headers={"Origin": "https://testserver"},
                json={"preferred_locale": "en"},
            ).status_code
            == 401
        )
