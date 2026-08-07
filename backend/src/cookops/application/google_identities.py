"""Google ID-token verification at the trusted human-authentication boundary.

The browser and any future Google Sign-In callback provide an opaque ID token to
this adapter.  It uses Google's maintained verifier, then translates only the
claims CookOps needs into the same trusted assertion consumed by the common
membership gate and browser-session issuer.  It never creates CookOps records.
"""

import asyncio
from collections.abc import Callable, Mapping
from threading import Lock
from typing import cast

import cachecontrol
import requests
from google.auth import exceptions as google_auth_exceptions
from google.auth import transport as google_transport
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from cookops.application.human_authentication import (
    CompletedHumanAuthentication,
    HumanAuthenticationDenied,
    HumanAuthenticationService,
    TrustedIdentityAssertion,
)

GoogleTokenVerifier = Callable[[str, str], Mapping[str, object]]
_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


class _BoundedGoogleRequest:
    """Force a finite certificate-fetch timeout for Google's verifier."""

    def __init__(self, request: google_transport.Request, timeout_seconds: int) -> None:
        self._request = request
        self._timeout_seconds = timeout_seconds

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int | None = None,
        **kwargs: object,
    ) -> google_transport.Response:
        del timeout
        return cast(
            google_transport.Response,
            self._request(
                url,
                method=method,
                body=body,
                headers=headers,
                timeout=self._timeout_seconds,
                **kwargs,
            ),
        )


class GoogleIdTokenVerifier:
    """Google's verifier with process-local certificate caching and a timeout.

    CacheControl respects the cache directives on Google's rotating certificate
    endpoint.  The lock also avoids concurrently using a mutable ``requests``
    session, while web handlers use a worker thread so its I/O never blocks the
    ASGI event loop.
    """

    def __init__(self, timeout_seconds: int) -> None:
        if not isinstance(timeout_seconds, int) or not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be an integer between 1 and 30")
        cached_session = cachecontrol.CacheControl(requests.Session())
        self._request = _BoundedGoogleRequest(
            google_requests.Request(session=cached_session), timeout_seconds
        )
        self._lock = Lock()

    def __call__(self, raw_id_token: str, audience: str) -> Mapping[str, object]:
        """Verify using Google's maintained signature, issuer, and claim checks."""

        with self._lock:
            return cast(
                Mapping[str, object],
                id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
                    raw_id_token,
                    cast(google_transport.Request, self._request),
                    audience,
                ),
            )


class GoogleIdentityProvider:
    """Complete a verified Google ID token through CookOps' common auth service."""

    def __init__(
        self,
        human_authentication: HumanAuthenticationService,
        client_id: str,
        *,
        token_verifier: GoogleTokenVerifier | None = None,
    ) -> None:
        if (
            not isinstance(client_id, str)
            or not client_id
            or client_id != client_id.strip()
            or len(client_id) > 255
        ):
            raise ValueError(
                "client_id must be a nonblank trimmed string of at most 255 characters"
            )
        self._human_authentication = human_authentication
        self._client_id = client_id
        self._token_verifier = token_verifier or GoogleIdTokenVerifier(timeout_seconds=5)

    async def complete_id_token(self, raw_id_token: str) -> CompletedHumanAuthentication:
        """Verify one presentation and issue a CookOps session if access permits.

        All provider-validation failures deliberately collapse to the common
        non-enumerating authentication denial.  The future HTTP adapter can map
        this directly to its generic failed-login response.
        """

        if not isinstance(raw_id_token, str) or not raw_id_token or len(raw_id_token) > 16_384:
            raise HumanAuthenticationDenied("authentication denied")
        try:
            claims = await asyncio.to_thread(self._token_verifier, raw_id_token, self._client_id)
            assertion = self._assertion_from_verified_claims(claims)
        except (KeyError, TypeError, ValueError, google_auth_exceptions.GoogleAuthError) as error:
            raise HumanAuthenticationDenied("authentication denied") from error
        return await self._human_authentication.complete(assertion)

    def _assertion_from_verified_claims(
        self, claims: Mapping[str, object]
    ) -> TrustedIdentityAssertion:
        """Convert the subset of an already signature-verified token we trust."""

        if claims.get("iss") not in _GOOGLE_ISSUERS:
            raise ValueError("Google token issuer is invalid")
        if claims.get("aud") != self._client_id:
            raise ValueError("Google token audience is invalid")
        if claims.get("email_verified") is not True:
            raise ValueError("Google token email is not verified")
        provider_subject = claims["sub"]
        verified_email = claims["email"]
        if not isinstance(provider_subject, str) or not isinstance(verified_email, str):
            raise ValueError("Google token identity claims are invalid")
        return TrustedIdentityAssertion(
            provider="google",
            provider_subject=provider_subject,
            verified_email=verified_email,
        )
