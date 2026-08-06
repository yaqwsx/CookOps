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
