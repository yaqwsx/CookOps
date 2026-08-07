"""Cookie-authenticated event-overview read adapter."""

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import (
    BrowserSessionService,
    decode_browser_session_hmac_key,
)
from cookops.application.events import EventQueryDenied, EventSummary, list_event_summaries
from cookops.config import Settings


@dataclass(frozen=True, slots=True)
class EventHttpServices:
    browser_sessions: BrowserSessionService
    session_factory: async_sessionmaker[AsyncSession]


class EventSummaryResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    start_date: str
    end_date: str
    base_expected_attendance: int
    budget_amount: Decimal
    currency: str
    lifecycle: str
    archived_at: datetime | None


class EventListResponse(BaseModel):
    events: tuple[EventSummaryResponse, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class EventCursor:
    organization_id: UUID
    lifecycle: Literal["active", "archived"]
    before_created_at: datetime
    before_id: UUID


class InvalidEventCursor(ValueError):
    pass


class EventCursorCodec:
    def __init__(self, encoded_hmac_key: str) -> None:
        self._key = decode_browser_session_hmac_key(encoded_hmac_key)

    def encode(self, cursor: EventCursor) -> str:
        payload = json.dumps(
            {
                "before_created_at": cursor.before_created_at.isoformat().replace("+00:00", "Z"),
                "before_id": str(cursor.before_id),
                "lifecycle": cursor.lifecycle,
                "organization_id": str(cursor.organization_id),
                "schema_version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        signature = hmac.new(
            self._key, b"cookops.events.cursor.v1:" + encoded.encode(), hashlib.sha256
        ).digest()
        return "v1." + encoded + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()

    def decode(self, token: str, *, organization_id: UUID) -> EventCursor:
        try:
            version, encoded, signature = token.split(".")
            if version != "v1" or len(token) > 512:
                raise ValueError
            payload = json.loads(self._decode_component(encoded))
            expected = hmac.new(
                self._key, b"cookops.events.cursor.v1:" + encoded.encode(), hashlib.sha256
            ).digest()
            raw_time = payload["before_created_at"]
            lifecycle = payload["lifecycle"]
            if not isinstance(raw_time, str) or lifecycle not in ("active", "archived"):
                raise ValueError
            cursor = EventCursor(
                organization_id=UUID(payload["organization_id"]),
                lifecycle=lifecycle,
                before_created_at=datetime.fromisoformat(raw_time.replace("Z", "+00:00")),
                before_id=UUID(payload["before_id"]),
            )
            if (
                cursor.before_created_at.tzinfo is None
                or cursor.before_created_at.utcoffset() is None
                or cursor.organization_id != organization_id
                or not hmac.compare_digest(self._decode_component(signature), expected)
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
            raise InvalidEventCursor("invalid cursor") from error

    @staticmethod
    def _decode_component(value: str) -> bytes:
        if not value or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            raise ValueError
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value:
            raise ValueError
        return decoded


def _services(request: Request) -> EventHttpServices:
    services = getattr(request.app.state, "events", None)
    if not isinstance(services, EventHttpServices):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="events are not available",
        )
    return services


async def _authenticated_user_id(
    request: Request, settings: Settings, services: EventHttpServices
) -> UUID:
    secret = request.cookies.get(settings.browser_session_cookie_name)
    if secret is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    authenticated = await services.browser_sessions.authenticate(secret)
    if authenticated is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return authenticated.user_id


def _summary_response(summary: EventSummary) -> EventSummaryResponse:
    return EventSummaryResponse(
        id=summary.id,
        organization_id=summary.organization_id,
        name=summary.name,
        start_date=summary.start_date.isoformat(),
        end_date=summary.end_date.isoformat(),
        base_expected_attendance=summary.base_expected_attendance,
        budget_amount=summary.budget_amount,
        currency=summary.currency,
        lifecycle=summary.lifecycle,
        archived_at=summary.archived_at,
    )


def create_events_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/organizations/{organization_id}/events", tags=["events"])

    @router.get("", response_model=EventListResponse)
    async def list_events(
        organization_id: UUID,
        request: Request,
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        page_size: int = Query(default=50, ge=1, le=100),
    ) -> EventListResponse:
        services = _services(request)
        user_id = await _authenticated_user_id(request, settings, services)
        cursor_codec = EventCursorCodec(settings.resolved_browser_session_hmac_key)
        try:
            decoded_cursor = (
                cursor_codec.decode(cursor, organization_id=organization_id)
                if cursor is not None
                else None
            )
            page = await list_event_summaries(
                services.session_factory,
                actor_user_id=user_id,
                organization_id=organization_id,
                limit=page_size,
                before_lifecycle=(decoded_cursor.lifecycle if decoded_cursor is not None else None),
                before_created_at=(
                    decoded_cursor.before_created_at if decoded_cursor is not None else None
                ),
                before_id=decoded_cursor.before_id if decoded_cursor is not None else None,
            )
        except InvalidEventCursor as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor"
            ) from error
        except EventQueryDenied as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found"},
            ) from error
        next_cursor = None
        if page.has_more:
            last = page.summaries[-1]
            next_cursor = cursor_codec.encode(
                EventCursor(
                    organization_id=organization_id,
                    lifecycle=last.lifecycle,
                    before_created_at=last.created_at,
                    before_id=last.id,
                )
            )
        return EventListResponse(
            events=tuple(_summary_response(summary) for summary in page.summaries),
            next_cursor=next_cursor,
        )

    return router
