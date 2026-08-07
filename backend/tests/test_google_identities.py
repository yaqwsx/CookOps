import asyncio
import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from google.auth import transport as google_transport
from google.oauth2 import id_token

from cookops.application import google_identities
from cookops.application.google_identities import (
    GoogleIdentityProvider,
    GoogleIdTokenVerifier,
    GoogleTokenVerifier,
)
from cookops.application.human_authentication import (
    CompletedHumanAuthentication,
    HumanAuthenticationDenied,
    HumanAuthenticationService,
    TrustedIdentityAssertion,
)

CLIENT_ID = "cookops-test-client.apps.googleusercontent.com"
CERTS_URL = "https://identity.example.test/certs"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class _CertificateResponse:
    def __init__(self, data: bytes) -> None:
        self.status = 200
        self.data = data
        self.headers: Mapping[str, str] = {}


class _LocalCertificateRequest:
    def __init__(self, certificate_pem: str) -> None:
        self._response = _CertificateResponse(
            json.dumps({"local-test-key": certificate_pem}).encode("utf-8")
        )

    def __call__(self, url: str, **_kwargs: object) -> _CertificateResponse:
        if url != CERTS_URL:
            raise AssertionError(f"unexpected certificate URL: {url}")
        return self._response


@pytest.fixture
def local_token_verifier() -> tuple[rsa.RSAPrivateKey, GoogleTokenVerifier]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    now = datetime.now(UTC)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CookOps test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    request = _LocalCertificateRequest(
        certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    )

    def verifier(raw_id_token: str, audience: str) -> Mapping[str, object]:
        return cast(
            Mapping[str, object],
            id_token.verify_token(
                raw_id_token,
                cast(google_transport.Request, request),
                audience=audience,
                certs_url=CERTS_URL,
            ),
        )

    return private_key, verifier


def _signed_token(
    private_key: rsa.RSAPrivateKey,
    **claim_overrides: object,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-member-subject",
        "email": "google_member@example.test",
        "email_verified": True,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    claims.update(claim_overrides)
    header = {"alg": "RS256", "kid": "local-test-key", "typ": "JWT"}
    signed = ".".join(
        (
            _base64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _base64url(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
        )
    ).encode("ascii")
    signature = private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{signed.decode('ascii')}.{_base64url(signature)}"


def _provider(token_verifier: GoogleTokenVerifier) -> tuple[GoogleIdentityProvider, AsyncMock]:
    human_authentication = MagicMock(spec=HumanAuthenticationService)
    completed = cast(CompletedHumanAuthentication, object())
    complete = AsyncMock(return_value=completed)
    human_authentication.complete = complete
    return (
        GoogleIdentityProvider(
            cast(HumanAuthenticationService, human_authentication),
            CLIENT_ID,
            token_verifier=token_verifier,
        ),
        complete,
    )


def test_valid_signed_google_token_completes_the_shared_human_authentication_service(
    local_token_verifier: tuple[rsa.RSAPrivateKey, GoogleTokenVerifier],
) -> None:
    private_key, token_verifier = local_token_verifier
    provider, complete = _provider(token_verifier)

    completed = asyncio.run(provider.complete_id_token(_signed_token(private_key)))

    assert completed is complete.return_value
    complete.assert_awaited_once_with(
        TrustedIdentityAssertion(
            provider="google",
            provider_subject="google-member-subject",
            verified_email="google_member@example.test",
        )
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.example.test"},
        {"aud": "other-client.apps.googleusercontent.com"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())},
    ],
    ids=["issuer", "audience", "unverified-email", "string-email-verified", "expired"],
)
def test_invalid_google_claims_are_denied_without_calling_human_authentication(
    local_token_verifier: tuple[rsa.RSAPrivateKey, GoogleTokenVerifier],
    overrides: dict[str, object],
) -> None:
    private_key, token_verifier = local_token_verifier
    provider, complete = _provider(token_verifier)

    with pytest.raises(HumanAuthenticationDenied, match="authentication denied"):
        asyncio.run(provider.complete_id_token(_signed_token(private_key, **overrides)))

    complete.assert_not_awaited()


def test_invalid_google_signature_is_denied_without_calling_human_authentication(
    local_token_verifier: tuple[rsa.RSAPrivateKey, GoogleTokenVerifier],
) -> None:
    private_key, token_verifier = local_token_verifier
    provider, complete = _provider(token_verifier)
    token = _signed_token(private_key)
    signed, signature = token.rsplit(".", maxsplit=1)
    tampered = f"{signed}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"

    with pytest.raises(HumanAuthenticationDenied, match="authentication denied"):
        asyncio.run(provider.complete_id_token(tampered))

    complete.assert_not_awaited()


def test_google_provider_rejects_a_raw_token_too_large_to_be_a_login_presentation() -> None:
    provider, complete = _provider(lambda _token, _audience: {})

    with pytest.raises(HumanAuthenticationDenied, match="authentication denied"):
        asyncio.run(provider.complete_id_token("x" * 16_385))

    complete.assert_not_awaited()


def test_production_verifier_delegates_to_google_auth_without_a_network_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_claims: Mapping[str, object] = {"sub": "subject"}
    received: list[object] = []

    def fake_verify(raw_id_token: str, request: object, audience: str) -> Mapping[str, object]:
        received.extend((raw_id_token, request, audience))
        return verified_claims

    monkeypatch.setattr(id_token, "verify_oauth2_token", fake_verify)

    assert GoogleIdTokenVerifier(timeout_seconds=3)("opaque-token", CLIENT_ID) == verified_claims
    assert received[0] == "opaque-token"
    assert isinstance(received[1], google_identities._BoundedGoogleRequest)
    assert received[2] == CLIENT_ID


def test_bounded_google_request_ignores_a_library_supplied_timeout() -> None:
    received: dict[str, object] = {}
    response = cast(google_transport.Response, object())

    def request(*_args: object, **kwargs: object) -> google_transport.Response:
        received.update(kwargs)
        return response

    bounded = google_identities._BoundedGoogleRequest(
        cast(google_transport.Request, request), timeout_seconds=3
    )

    assert bounded(CERTS_URL, timeout=999) is response
    assert received["timeout"] == 3
