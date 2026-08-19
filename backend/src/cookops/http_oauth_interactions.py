"""Browser session consent page for validated private OAuth interaction details."""

import re
from dataclasses import dataclass
from html import escape
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from cookops.application.oauth_interactions import OAuthInteractionApprovalService
from cookops.config import Settings
from cookops.oauth_interaction_client import OAuthInteractionUnavailable

_UID = re.compile(r"^[A-Za-z0-9_-]{16,255}$")


@dataclass(frozen=True, slots=True)
class OAuthInteractionHttpServices:
    approvals: OAuthInteractionApprovalService


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "deny"]


class GrantResponse(BaseModel):
    handle: str
    clientId: str
    issuedAt: float | None = None
    expiresAt: float | None = None


def _services(request: Request) -> OAuthInteractionHttpServices:
    services = getattr(request.app.state, "oauth_interactions", None)
    if not isinstance(services, OAuthInteractionHttpServices):
        raise HTTPException(status_code=503, detail="OAuth is unavailable")
    return services


def _secret(request: Request, settings: Settings) -> str:
    secret = request.cookies.get(settings.browser_session_cookie_name)
    if secret is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return secret


def create_oauth_interaction_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/auth/mcp-interactions", tags=["authentication"])

    @router.get("/{interaction_uid}", response_class=HTMLResponse)
    async def show(interaction_uid: str, request: Request) -> HTMLResponse:
        if not _UID.fullmatch(interaction_uid):
            raise HTTPException(status_code=404, detail="not found")
        try:
            detail = await _services(request).approvals.details(
                browser_session_secret=_secret(request, settings), interaction_uid=interaction_uid
            )
        except OAuthInteractionUnavailable as error:
            raise HTTPException(status_code=503, detail="OAuth is unavailable") from error
        if detail is None:
            raise HTTPException(status_code=403, detail="forbidden")
        scopes = "".join(f"<li>{escape(scope)}</li>" for scope in detail.scopes)
        completion_url = (
            f"{settings.oauth_interaction_origin}/oauth/interaction/{interaction_uid}/complete"
        )
        page = "".join(
            [
                "<!doctype html><title>CookOps consent</title>",
                f"<h1>Allow {escape(detail.client_name)}?</h1>",
                f"<p>Resource: {escape(detail.resource)}</p><ul>{scopes}</ul>",
                "<button onclick=\"decide('approve')\">Allow</button>",
                "<button onclick=\"decide('deny')\">Deny</button>",
                "<script>async function decide(decision){",
                f"const response=await fetch({request.url.path!r},{{",
                "method:'POST',headers:{'content-type':'application/json'},",
                "credentials:'same-origin',body:JSON.stringify({decision})});",
                f"if(response.ok)location.assign({completion_url!r});}}</script>",
            ]
        )
        return HTMLResponse(
            page,
            headers={"cache-control": "no-store"},
        )

    @router.post("/{interaction_uid}", status_code=status.HTTP_204_NO_CONTENT)
    async def decide(interaction_uid: str, body: ApprovalRequest, request: Request) -> Response:
        if not _UID.fullmatch(interaction_uid):
            raise HTTPException(status_code=404, detail="not found")
        if request.headers.get("origin") != settings.oauth_interaction_origin:
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            recorded = await _services(request).approvals.submit(
                browser_session_secret=_secret(request, settings),
                interaction_uid=interaction_uid,
                decision=body.decision,
            )
        except OAuthInteractionUnavailable as error:
            raise HTTPException(status_code=503, detail="OAuth is unavailable") from error
        if not recorded:
            raise HTTPException(status_code=403, detail="forbidden")
        return Response(status_code=204)

    return router


def create_oauth_grants_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/auth/mcp-grants", tags=["authentication"])

    def secret(request: Request) -> str:
        value = request.cookies.get(settings.browser_session_cookie_name)
        if value is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return value

    @router.get("", response_model=list[GrantResponse], response_model_exclude_none=True)
    async def list_grants(request: Request) -> list[GrantResponse]:
        try:
            grants = await _services(request).approvals.grants(
                browser_session_secret=secret(request)
            )
        except OAuthInteractionUnavailable as error:
            raise HTTPException(status_code=503, detail="OAuth is unavailable") from error
        if grants is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return [
            GrantResponse(
                handle=g.handle, clientId=g.client_id, issuedAt=g.issued_at, expiresAt=g.expires_at
            )
            for g in grants
        ]

    @router.delete("/{handle}", status_code=204)
    async def revoke_grant(handle: str, request: Request) -> Response:
        if (
            not re.fullmatch(r"[0-9a-f]{64}", handle)
            or settings.browser_origin is None
            or request.headers.get("origin") != settings.browser_origin
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        try:
            result = await _services(request).approvals.revoke_grant(
                browser_session_secret=secret(request), handle=handle
            )
        except OAuthInteractionUnavailable as error:
            raise HTTPException(status_code=503, detail="OAuth is unavailable") from error
        if result is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        if not result:
            raise HTTPException(status_code=404, detail="not found")
        return Response(status_code=204)

    return router
