import pytest
from pydantic import ValidationError

from cookops.config import Environment, HumanAuthProvider, Settings


def test_environment_rejects_dummy_auth_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "dummy")

    with pytest.raises(ValidationError, match="dummy authentication cannot be enabled"):
        Settings()


def test_environment_allows_google_auth_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("COOKOPS_HUMAN_AUTH_PROVIDER", "google")

    settings = Settings()

    assert settings.environment is Environment.PRODUCTION
    assert settings.human_auth_provider is HumanAuthProvider.GOOGLE


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
