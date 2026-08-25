from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEVELOPMENT_BROWSER_SESSION_HMAC_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"


def _validate_https_origin(value: str, setting_name: str) -> None:
    try:
        origin = urlsplit(value)
        port = origin.port
    except ValueError as error:
        raise ValueError(f"{setting_name} must be a credential-free HTTPS origin") from error
    if (
        any(character.isspace() for character in value)
        or "?" in value
        or "#" in value
        or origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path
        or origin.query
        or origin.fragment
        or port is None
        and origin.netloc.endswith(":")
    ):
        raise ValueError(f"{setting_name} must be a credential-free HTTPS origin")


def _validate_mcp_resource(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("MCP resource must be a canonical HTTPS URL ending in /mcp") from error
    if (
        any(character.isspace() for character in value)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or port is None
        and parsed.netloc.endswith(":")
    ):
        raise ValueError("MCP resource must be a canonical HTTPS URL ending in /mcp")


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
    browser_origin: str | None = None
    receipt_media_root: Path = Path("/var/lib/cookops/receipts")
    oauth_issuer: str | None = None
    mcp_resource: str | None = None
    oauth_introspection_url: str | None = None
    oauth_resource_server_secret: str | None = None
    oauth_interaction_details_api_credential_base64url: str | None = None
    oauth_interaction_approval_api_credential_base64url: str | None = None
    oauth_interaction_origin: str | None = None
    oauth_interaction_private_base_url: str = "http://oauth-server:3000/oauth/private/interactions"
    oauth_grants_private_url: str = "http://oauth-server:3000/oauth/private/grants"
    oauth_grants_api_credential_base64url: str | None = None

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
        if self.environment is Environment.PRODUCTION and self.browser_origin is None:
            raise ValueError("browser origin must be configured in production")
        if self.browser_origin is not None:
            _validate_https_origin(self.browser_origin, "browser origin")

        mcp_values = (
            self.oauth_issuer,
            self.mcp_resource,
            self.oauth_introspection_url,
            self.oauth_resource_server_secret,
        )
        if any(value is not None for value in mcp_values) and any(
            not isinstance(value, str) or not value for value in mcp_values
        ):
            raise ValueError("MCP OAuth verification settings must be configured together")
        if self.mcp_resource is not None:
            _validate_mcp_resource(self.mcp_resource)

        if self.browser_session_hmac_key is not None:
            # Parse configured keys at configuration time rather than leaving a
            # malformed secret to fail only after the ASGI lifespan begins.
            from cookops.application.browser_sessions import decode_browser_session_hmac_key

            decode_browser_session_hmac_key(self.browser_session_hmac_key)
        oauth_values = (
            self.oauth_interaction_details_api_credential_base64url,
            self.oauth_interaction_approval_api_credential_base64url,
            self.oauth_interaction_origin,
        )
        if any(value is not None for value in oauth_values) and any(
            value is None for value in oauth_values
        ):
            raise ValueError("OAuth interaction configuration must be complete")
        if self.environment is Environment.PRODUCTION and any(
            value is None for value in oauth_values
        ):
            raise ValueError("OAuth interaction configuration must be set in production")
        if self.oauth_interaction_origin is not None:
            _validate_https_origin(self.oauth_interaction_origin, "OAuth interaction origin")
            if (
                self.browser_origin is not None
                and self.oauth_interaction_origin != self.browser_origin
            ):
                raise ValueError("OAuth interaction origin must match browser origin")
        private_base = urlsplit(self.oauth_interaction_private_base_url)
        expected_private = "http://oauth-server:3000/oauth/private/interactions"
        if (
            self.environment is Environment.PRODUCTION
            and self.oauth_interaction_private_base_url != expected_private
        ):
            raise ValueError(
                "OAuth interaction private API must use the Compose oauth-server authority"
            )
        if (
            not private_base.scheme
            or not private_base.hostname
            or private_base.query
            or private_base.fragment
        ):
            raise ValueError("OAuth interaction private API URL is invalid")
        if self.oauth_interaction_details_api_credential_base64url is not None:
            from cookops.application.browser_sessions import decode_browser_session_hmac_key

            decode_browser_session_hmac_key(self.oauth_interaction_details_api_credential_base64url)
            decode_browser_session_hmac_key(
                self.oauth_interaction_approval_api_credential_base64url or ""
            )
            if (
                self.oauth_interaction_details_api_credential_base64url
                == self.oauth_interaction_approval_api_credential_base64url
            ):
                raise ValueError("OAuth interaction credentials must be distinct")
        if (
            self.environment is Environment.PRODUCTION
            and self.oauth_grants_api_credential_base64url is None
        ):
            raise ValueError("OAuth grants configuration must be set in production")
        grants_url = urlsplit(self.oauth_grants_private_url)
        if (
            not grants_url.scheme
            or not grants_url.hostname
            or grants_url.query
            or grants_url.fragment
        ):
            raise ValueError("OAuth grants private API URL is invalid")
        if (
            self.environment is Environment.PRODUCTION
            and self.oauth_grants_private_url != "http://oauth-server:3000/oauth/private/grants"
        ):
            raise ValueError("OAuth grants private API must use the Compose oauth-server authority")
        if self.oauth_grants_api_credential_base64url is not None:
            from cookops.application.browser_sessions import decode_browser_session_hmac_key

            decode_browser_session_hmac_key(self.oauth_grants_api_credential_base64url)
            if self.oauth_grants_api_credential_base64url in {
                self.oauth_interaction_details_api_credential_base64url,
                self.oauth_interaction_approval_api_credential_base64url,
            }:
                raise ValueError("OAuth private API credentials must be distinct")
        if not self.browser_session_cookie_name or self.browser_session_cookie_name.strip() != (
            self.browser_session_cookie_name
        ):
            raise ValueError("browser session cookie name must be nonblank and trimmed")
        if self.browser_session_lifetime_seconds <= 0:
            raise ValueError("browser session lifetime must be positive")
        if not self.receipt_media_root.is_absolute():
            raise ValueError("receipt media root must be an absolute path")
        return self
