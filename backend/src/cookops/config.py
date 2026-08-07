from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEVELOPMENT_BROWSER_SESSION_HMAC_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"


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
    google_client_id: str | None = None
    google_id_token_verification_timeout_seconds: int = Field(default=5, gt=0, le=30)
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+psycopg://cookops:cookops@localhost:5432/cookops"
    )
    browser_session_hmac_key: str | None = None
    browser_session_cookie_name: str = "cookops_session"
    browser_session_cookie_secure: bool = True
    browser_session_cookie_samesite: Literal["lax", "strict"] = "lax"
    browser_session_lifetime_seconds: int = 7 * 24 * 60 * 60
    receipt_media_root: Path = Path("/var/lib/cookops/receipts")
    oauth_issuer: str | None = None
    mcp_resource: str | None = None
    oauth_introspection_url: str | None = None
    oauth_resource_server_secret: str | None = None

    @property
    def resolved_browser_session_hmac_key(self) -> str:
        """Return a valid key for the configured trusted deployment mode.

        Local development and tests use a non-secret deterministic key so a clean
        checkout can exercise the dummy provider. Production never falls back to
        it and requires an explicit deployment secret.
        """

        if self.browser_session_hmac_key is not None:
            return self.browser_session_hmac_key
        if self.environment in (Environment.DEVELOPMENT, Environment.TEST):
            return _DEVELOPMENT_BROWSER_SESSION_HMAC_KEY
        raise RuntimeError("browser session HMAC key must be configured in production")

    @model_validator(mode="after")
    def validate_deployment_boundaries(self) -> Self:
        if (
            self.environment is Environment.PRODUCTION
            and self.human_auth_provider is HumanAuthProvider.DUMMY
        ):
            raise ValueError("dummy authentication cannot be enabled in production")
        if self.human_auth_provider is HumanAuthProvider.GOOGLE and (
            self.google_client_id is None
            or not self.google_client_id
            or self.google_client_id != self.google_client_id.strip()
            or len(self.google_client_id) > 255
        ):
            raise ValueError(
                "google client ID must be a nonblank trimmed string of at most 255 characters"
            )
        if self.database_url.scheme != "postgresql+psycopg":
            raise ValueError("database URL must use the postgresql+psycopg scheme")
        if self.environment is Environment.PRODUCTION and self.browser_session_hmac_key is None:
            raise ValueError("browser session HMAC key must be configured in production")
        if self.environment is Environment.PRODUCTION and not self.browser_session_cookie_secure:
            raise ValueError("browser session cookie must be secure in production")

        oauth_values = (
            self.oauth_issuer,
            self.mcp_resource,
            self.oauth_introspection_url,
            self.oauth_resource_server_secret,
        )
        if any(value is not None for value in oauth_values) and any(
            not isinstance(value, str) or not value for value in oauth_values
        ):
            raise ValueError("MCP OAuth verification settings must be configured together")

        if self.browser_session_hmac_key is not None:
            # Parse configured keys at configuration time rather than leaving a
            # malformed secret to fail only after the ASGI lifespan begins.
            from cookops.application.browser_sessions import decode_browser_session_hmac_key

            decode_browser_session_hmac_key(self.browser_session_hmac_key)
        if not self.browser_session_cookie_name or self.browser_session_cookie_name.strip() != (
            self.browser_session_cookie_name
        ):
            raise ValueError("browser session cookie name must be nonblank and trimmed")
        if self.browser_session_lifetime_seconds <= 0:
            raise ValueError("browser session lifetime must be positive")
        if not self.receipt_media_root.is_absolute():
            raise ValueError("receipt media root must be an absolute path")
        return self
