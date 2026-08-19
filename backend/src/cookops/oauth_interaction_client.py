"""Authenticated, private OAuth interaction bridge client."""

import asyncio
import json
import math
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from cookops.application.oauth_interactions import (
    InteractionDecision,
    OAuthInteractionDetails,
)

_TIMEOUT_SECONDS = 5


class OAuthInteractionUnavailable(RuntimeError):
    """The private OAuth interaction bridge cannot safely be used."""


@dataclass(frozen=True, slots=True)
class AuthorizedGrant:
    handle: str
    client_id: str
    issued_at: float | None = None
    expires_at: float | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _request(
    url: str, credential: str, *, method: str, payload: object | None = None
) -> tuple[int, bytes]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = Request(
        url,
        data=data,
        headers={
            "authorization": f"Bearer {credential}",
            **({"content-type": "application/json"} if data else {}),
        },
        method=method,
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(
            request, timeout=_TIMEOUT_SECONDS
        ) as response:
            return int(response.status), response.read()
    except HTTPError as error:
        return error.code, error.read()
    except URLError as error:
        raise OAuthInteractionUnavailable(
            "private OAuth interaction endpoint is unavailable"
        ) from error


def _details(value: bytes, interaction_uid: str) -> OAuthInteractionDetails:
    try:
        parsed: object = json.loads(value)
        if not isinstance(parsed, dict) or set(parsed) != {
            "interactionUid",
            "clientId",
            "clientName",
            "resource",
            "scopes",
            "prompt",
        }:
            raise ValueError
        client_name = parsed["clientName"]
        resource = parsed["resource"]
        scopes = parsed["scopes"]
        if (
            parsed["interactionUid"] != interaction_uid
            or not isinstance(client_name, str)
            or not isinstance(resource, str)
            or not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(scope, str) or not scope for scope in scopes)
        ):
            raise ValueError
        return OAuthInteractionDetails(interaction_uid, client_name, resource, tuple(scopes))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OAuthInteractionUnavailable(
            "private OAuth interaction details are invalid"
        ) from error


def _grants(value: bytes) -> list[AuthorizedGrant]:
    try:
        parsed: object = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError
        result: list[AuthorizedGrant] = []
        for item in parsed:
            if not isinstance(item, dict) or set(item) - {
                "handle",
                "clientId",
                "issuedAt",
                "expiresAt",
            }:
                raise ValueError
            handle, client_id = item.get("handle"), item.get("clientId")
            if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{64}", handle):
                raise ValueError
            if not isinstance(client_id, str) or not client_id or len(client_id) > 255:
                raise ValueError
            times: list[float | None] = []
            for key in ("issuedAt", "expiresAt"):
                raw_value = item.get(key)
                if raw_value is not None and (
                    not isinstance(raw_value, (int, float))
                    or isinstance(raw_value, bool)
                    or not math.isfinite(raw_value)
                ):
                    raise ValueError
                times.append(float(raw_value) if raw_value is not None else None)
            result.append(AuthorizedGrant(handle, client_id, times[0], times[1]))
        return result
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise OAuthInteractionUnavailable("private OAuth grants are invalid") from error


@dataclass(frozen=True, slots=True)
class OAuthPrivateInteractionClient:
    details_url: str
    details_credential: str
    approval_url: str
    approval_credential: str
    grants_url: str = ""
    grants_credential: str = ""

    async def grants(self, *, subject: UUID) -> list[AuthorizedGrant]:
        if not self.grants_credential:
            raise OAuthInteractionUnavailable("private OAuth grants are not configured")
        status, body = await asyncio.to_thread(
            _request,
            f"{self.grants_url}?subject={quote(str(subject), safe='')}",
            self.grants_credential,
            method="GET",
        )
        if status != 200:
            raise OAuthInteractionUnavailable("private OAuth grants were rejected")
        return _grants(body)

    async def revoke_grant(self, *, subject: UUID, handle: str) -> bool:
        if not self.grants_credential:
            raise OAuthInteractionUnavailable("private OAuth grants are not configured")
        status, _ = await asyncio.to_thread(
            _request,
            f"{self.grants_url}/{handle}",
            self.grants_credential,
            method="DELETE",
            payload={"subject": str(subject)},
        )
        if status == 404:
            return False
        if status != 204:
            raise OAuthInteractionUnavailable("private OAuth grant revocation was rejected")
        return True

    async def interaction_details(self, *, interaction_uid: str) -> OAuthInteractionDetails | None:
        status, body = await asyncio.to_thread(
            _request, f"{self.details_url}/{interaction_uid}", self.details_credential, method="GET"
        )
        if status == 404:
            return None
        if status != 200:
            raise OAuthInteractionUnavailable("private OAuth interaction details were rejected")
        return _details(body, interaction_uid)

    async def record_approval(
        self, *, interaction_uid: str, subject: UUID, decision: InteractionDecision
    ) -> bool:
        status, _ = await asyncio.to_thread(
            _request,
            self.approval_url,
            self.approval_credential,
            method="POST",
            payload={
                "interactionUid": interaction_uid,
                "subject": str(subject),
                "decision": decision,
            },
        )
        if status == 204:
            return True
        if status == 409:
            return False
        raise OAuthInteractionUnavailable("private OAuth interaction approval was rejected")
