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
    monkeypatch.setenv(
        "COOKOPS_BROWSER_SESSION_HMAC_KEY",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
    )

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
    monkeypatch.setenv("COOKOPS_BROWSER_SESSION_HMAC_KEY", key)

    assert (
        decode_browser_session_hmac_key(Settings().browser_session_hmac_key or "")
        == b"0123456789abcdef0123456789abcdef"
    )


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
