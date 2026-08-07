from enum import StrEnum
from typing import Self

from pydantic import PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class HumanAuthProvider(StrEnum):
    DUMMY = "dummy"
    GOOGLE = "google"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COOKOPS_", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    human_auth_provider: HumanAuthProvider = HumanAuthProvider.DUMMY
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+psycopg://cookops:cookops@localhost:5432/cookops"
    )
    browser_session_hmac_key: str | None = None

    @model_validator(mode="after")
    def validate_deployment_boundaries(self) -> Self:
        if (
            self.environment is Environment.PRODUCTION
            and self.human_auth_provider is HumanAuthProvider.DUMMY
        ):
            raise ValueError("dummy authentication cannot be enabled in production")
        if self.database_url.scheme != "postgresql+psycopg":
            raise ValueError("database URL must use the postgresql+psycopg scheme")
        if self.environment is Environment.PRODUCTION:
            if self.browser_session_hmac_key is None:
                raise ValueError("browser session HMAC key must be configured in production")
            # Import lazily to keep settings usable by Alembic without creating a
            # database runtime, while applying the exact same strict key parser as
            # the session issuer.
            from cookops.application.browser_sessions import decode_browser_session_hmac_key

            decode_browser_session_hmac_key(self.browser_session_hmac_key)
        return self
