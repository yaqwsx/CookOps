import pytest
from pydantic import ValidationError

from cookops.application.browser_sessions import decode_browser_session_hmac_key
from cookops.config import Environment, HumanAuthProvider, Settings


def test_environment_rejects_dummy_auth_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "dummy")

    with pytest.raises(ValidationError, match="dummy authentication cannot be enabled"):
        Settings()


def test_environment_allows_google_auth_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "google")
    monkeypatch.setenv("COOKOPS_GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv(
        "COOKOPS_BROWSER_SESSION_HMAC_KEY",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
    )
    monkeypatch.setenv("COOKOPS_BROWSER_ORIGIN", "https://cookops.example")
    monkeypatch.setenv(
        "COOKOPS_OAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWU",
    )
    monkeypatch.setenv(
        "COOKOPS_OAUTH_INTERACTION_APPROVAL_API_CREDENTIAL_BASE64URL",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
    )
    monkeypatch.setenv("COOKOPS_OAUTH_INTERACTION_ORIGIN", "https://cookops.example")

    settings = Settings()

    assert settings.environment is Environment.PRODUCTION
    assert settings.human_auth_provider is HumanAuthProvider.GOOGLE


@pytest.mark.parametrize(
    "value", [None, "not-a-key", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="]
)
def test_production_rejects_missing_or_malformed_browser_session_key(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.setenv("COOKOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "google")
    monkeypatch.setenv("COOKOPS_GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("COOKOPS_BROWSER_ORIGIN", "https://cookops.example")
    if value is None:
        monkeypatch.delenv("COOKOPS_BROWSER_SESSION_HMAC_KEY", raising=False)
    else:
        monkeypatch.setenv("COOKOPS_BROWSER_SESSION_HMAC_KEY", value)

    with pytest.raises(ValidationError, match="browser session HMAC key"):
        Settings()


def test_production_accepts_exactly_the_key_parsed_by_session_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"
    monkeypatch.setenv("COOKOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "google")
    monkeypatch.setenv("COOKOPS_GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("COOKOPS_BROWSER_SESSION_HMAC_KEY", key)
    monkeypatch.setenv("COOKOPS_BROWSER_ORIGIN", "https://cookops.example")
    monkeypatch.setenv(
        "COOKOPS_OAUTH_INTERACTION_DETAILS_API_CREDENTIAL_BASE64URL",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWU",
    )
    monkeypatch.setenv("COOKOPS_OAUTH_INTERACTION_APPROVAL_API_CREDENTIAL_BASE64URL", key)
    monkeypatch.setenv("COOKOPS_OAUTH_INTERACTION_ORIGIN", "https://cookops.example")

    assert (
        decode_browser_session_hmac_key(Settings().browser_session_hmac_key or "")
        == b"0123456789abcdef0123456789abcdef"
    )


def test_production_rejects_an_insecure_browser_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COOKOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "google")
    monkeypatch.setenv("COOKOPS_GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv(
        "COOKOPS_BROWSER_SESSION_HMAC_KEY",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
    )
    monkeypatch.setenv("COOKOPS_BROWSER_SESSION_COOKIE_SECURE", "false")

    with pytest.raises(ValidationError, match="browser session cookie must be secure"):
        Settings()


def production_settings(**overrides: str | None) -> Settings:
    values: dict[str, str | None] = {
        "environment": "production",
        "human_auth_provider": "google",
        "google_client_id": "test-client.apps.googleusercontent.com",
        "browser_session_hmac_key": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
        "oauth_interaction_details_api_credential_base64url": (
            "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWU"
        ),
        "oauth_interaction_approval_api_credential_base64url": (
            "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"
        ),
        "oauth_interaction_origin": "https://cookops.example",
        "browser_origin": "https://cookops.example",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_requires_browser_origin() -> None:
    with pytest.raises(ValidationError, match="browser origin must be configured"):
        production_settings(browser_origin=None)


@pytest.mark.parametrize(
    "browser_origin",
    [
        "http://cookops.example",
        "https://cookops.example/path",
        "https://user:pass@cookops.example",
        "https://cookops.example?query=value",
        "https://cookops.example?",
        "https://cookops.example#fragment",
        "https://cookops.example#",
        "https://cookops.example:invalid",
        "https://cookops.example with-space",
    ],
)
def test_browser_origin_requires_credential_free_https_origin(browser_origin: str) -> None:
    with pytest.raises(ValidationError, match="browser origin"):
        production_settings(browser_origin=browser_origin, oauth_interaction_origin=browser_origin)


def test_valid_browser_origin_succeeds() -> None:
    settings = production_settings(
        browser_origin="https://cookops.example:8443",
        oauth_interaction_origin="https://cookops.example:8443",
    )

    assert settings.browser_origin == "https://cookops.example:8443"


def test_oauth_interaction_origin_must_match_browser_origin() -> None:
    with pytest.raises(ValidationError, match="must match browser origin"):
        production_settings(oauth_interaction_origin="https://other.example")


def test_matching_oauth_interaction_origin_succeeds() -> None:
    settings = production_settings()

    assert settings.oauth_interaction_origin == settings.browser_origin


def test_development_has_a_deterministic_nonproduction_session_key() -> None:
    settings = Settings(
        environment=Environment.DEVELOPMENT, human_auth_provider=HumanAuthProvider.DUMMY
    )

    assert decode_browser_session_hmac_key(settings.resolved_browser_session_hmac_key) == (
        b"0123456789abcdef0123456789abcdef"
    )
    unvalidated_production_copy = settings.model_copy(
        update={"environment": Environment.PRODUCTION}
    )
    with pytest.raises(RuntimeError, match="must be configured in production"):
        _ = unvalidated_production_copy.resolved_browser_session_hmac_key


@pytest.mark.parametrize("value", [None, "", " client.apps.googleusercontent.com", "x" * 256])
def test_google_provider_requires_a_valid_client_id(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "google")
    if value is None:
        monkeypatch.delenv("COOKOPS_GOOGLE_CLIENT_ID", raising=False)
    else:
        monkeypatch.setenv("COOKOPS_GOOGLE_CLIENT_ID", value)

    with pytest.raises(ValidationError, match="google client ID"):
        Settings()


@pytest.mark.parametrize("value", [0, -1, 31])
def test_google_certificate_request_timeout_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(google_id_token_verification_timeout_seconds=value)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://cookops:cookops@database/cookops",
        "postgresql+asyncpg://cookops:cookops@database/cookops",
    ],
)
def test_database_url_requires_psycopg_driver(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("COOKOPS_DATABASE_URL", database_url)

    with pytest.raises(ValidationError, match=r"postgresql\+psycopg"):
        Settings()


def test_database_url_rejects_non_postgresql_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKOPS_DATABASE_URL", "sqlite:///cookops.db")

    with pytest.raises(ValidationError):
        Settings()


def test_passwordless_database_url_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = "postgresql+psycopg://cookops_api@postgres:5432/cookops"
    monkeypatch.setenv("COOKOPS_DATABASE_URL", database_url)

    assert str(Settings().database_url) == database_url
