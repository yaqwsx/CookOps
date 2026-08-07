"""Cookie-authenticated read adapter for materialized shopping-list summaries."""

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from cookops.application.browser_sessions import (
    BrowserSessionService,
    decode_browser_session_hmac_key,
)
from cookops.application.shopping_lists import (
    ShoppingListQueryDenied,
    ShoppingListQueryNotFound,
    ShoppingListQueryService,
    ShoppingListSummary,
)
from cookops.config import Settings


@dataclass(frozen=True, slots=True)
class ShoppingHttpServices:
    """Dependencies shared by the browser shopping-list HTTP endpoints."""

    browser_sessions: BrowserSessionService
    queries: ShoppingListQueryService


class ShoppingListSummaryResponse(BaseModel):
    id: UUID
    organization_id: UUID
    event_id: UUID
    name: str
    current_generation_revision_id: UUID | None
    generated_at: datetime | None
    source_scheduled_recipe_count: int
    ingredient_row_count: int
    created_at: datetime


class ShoppingListSummariesResponse(BaseModel):
    shopping_lists: tuple[ShoppingListSummaryResponse, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ShoppingListCursor:
    organization_id: UUID
    event_id: UUID
    before_created_at: datetime
    before_id: UUID


class InvalidShoppingListCursor(ValueError):
    """The opaque list cursor is malformed, forged, or scoped elsewhere."""


class ShoppingListCursorCodec:
    """Sign keyset pagination state so it cannot cross organization/event scopes."""

    def __init__(self, encoded_hmac_key: str) -> None:
        self._key = decode_browser_session_hmac_key(encoded_hmac_key)

    def encode(self, cursor: ShoppingListCursor) -> str:
        payload = json.dumps(
            {
                "before_created_at": cursor.before_created_at.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "before_id": str(cursor.before_id),
                "event_id": str(cursor.event_id),
                "organization_id": str(cursor.organization_id),
                "schema_version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        signature = hmac.new(
            self._key, b"cookops.shopping.cursor.v1:" + encoded.encode("ascii"), hashlib.sha256
        ).digest()
        return (
            "v1." + encoded + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    def decode(self, token: str, *, organization_id: UUID, event_id: UUID) -> ShoppingListCursor:
        try:
            version, payload, encoded_signature = token.split(".")
            if version != "v1" or len(token) > 512:
                raise ValueError
            payload_bytes = self._decode_component(payload)
            signature = self._decode_component(encoded_signature)
            expected = hmac.new(
                self._key,
                b"cookops.shopping.cursor.v1:" + payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            decoded = json.loads(payload_bytes)
            raw_before_created_at = decoded["before_created_at"]
            if not isinstance(raw_before_created_at, str):
                raise ValueError
            before_created_at = datetime.fromisoformat(raw_before_created_at.replace("Z", "+00:00"))
            if before_created_at.tzinfo is None or before_created_at.utcoffset() is None:
                raise ValueError
            cursor = ShoppingListCursor(
                organization_id=UUID(decoded["organization_id"]),
                event_id=UUID(decoded["event_id"]),
                before_created_at=before_created_at.astimezone(UTC),
                before_id=UUID(decoded["before_id"]),
            )
            if (
                not hmac.compare_digest(signature, expected)
                or cursor.organization_id != organization_id
                or cursor.event_id != event_id
                or cursor.before_created_at.tzinfo is None
            ):
                raise ValueError
            return cursor
        except (
            AttributeError,
            binascii.Error,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ) as error:
            raise InvalidShoppingListCursor("invalid cursor") from error

    @staticmethod
    def _decode_component(value: str) -> bytes:
        if not value or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            raise ValueError
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
            raise ValueError
        return decoded


def _services(request: Request) -> ShoppingHttpServices:
    services = getattr(request.app.state, "shopping", None)
    if not isinstance(services, ShoppingHttpServices):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="shopping lists are not available",
        )
    return services


async def _authenticated_actor(
    request: Request, settings: Settings, services: ShoppingHttpServices
) -> UUID:
    secret = request.cookies.get(settings.browser_session_cookie_name)
    if secret is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    authenticated = await services.browser_sessions.authenticate(secret)
    if authenticated is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return authenticated.user_id


def _summary_response(summary: ShoppingListSummary) -> ShoppingListSummaryResponse:
    return ShoppingListSummaryResponse(
        id=summary.id,
        organization_id=summary.organization_id,
        event_id=summary.event_id,
        name=summary.name,
        current_generation_revision_id=summary.current_generation_revision_id,
        generated_at=summary.generated_at,
        source_scheduled_recipe_count=summary.source_scheduled_recipe_count,
        ingredient_row_count=summary.ingredient_row_count,
        created_at=summary.created_at,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})


def create_shopping_router(settings: Settings) -> APIRouter:
    """Expose summary reads; ordinary shopping writes travel through synchronization."""

    router = APIRouter(
        prefix="/api/v1/organizations/{organization_id}/events/{event_id}",
        tags=["shopping"],
    )

    @router.get("/shopping-lists", response_model=ShoppingListSummariesResponse)
    async def list_shopping_lists(
        organization_id: UUID,
        event_id: UUID,
        request: Request,
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        page_size: int = Query(default=50, ge=1, le=100),
    ) -> ShoppingListSummariesResponse:
        services = _services(request)
        actor_user_id = await _authenticated_actor(request, settings, services)
        cursor_codec = ShoppingListCursorCodec(settings.resolved_browser_session_hmac_key)
        try:
            decoded_cursor = (
                cursor_codec.decode(cursor, organization_id=organization_id, event_id=event_id)
                if cursor is not None
                else None
            )
            page = await services.queries.list_summaries(
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                event_id=event_id,
                limit=page_size,
                before_created_at=(
                    decoded_cursor.before_created_at if decoded_cursor is not None else None
                ),
                before_id=decoded_cursor.before_id if decoded_cursor is not None else None,
            )
        except InvalidShoppingListCursor as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor"
            ) from error
        except (ShoppingListQueryDenied, ShoppingListQueryNotFound) as error:
            raise _not_found() from error
        next_cursor = None
        if page.has_more:
            last = page.summaries[-1]
            next_cursor = cursor_codec.encode(
                ShoppingListCursor(
                    organization_id=organization_id,
                    event_id=event_id,
                    before_created_at=last.created_at,
                    before_id=last.id,
                )
            )
        return ShoppingListSummariesResponse(
            shopping_lists=tuple(_summary_response(summary) for summary in page.summaries),
            next_cursor=next_cursor,
        )

    @router.get("/shopping-lists/{shopping_list_id}", response_model=ShoppingListSummaryResponse)
    async def get_shopping_list(
        organization_id: UUID, event_id: UUID, shopping_list_id: UUID, request: Request
    ) -> ShoppingListSummaryResponse:
        services = _services(request)
        actor_user_id = await _authenticated_actor(request, settings, services)
        try:
            summary = await services.queries.get_summary(
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                event_id=event_id,
                shopping_list_id=shopping_list_id,
            )
        except (ShoppingListQueryDenied, ShoppingListQueryNotFound) as error:
            raise _not_found() from error
        return _summary_response(summary)

    return router
