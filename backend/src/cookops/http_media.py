"""Cookie-authenticated receipt-media transport over the local private store."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cookops.application.browser_sessions import BrowserSessionService
from cookops.application.organizations import ApplicationServiceError, ExecutionContext
from cookops.application.receipt_media import (
    CreateReceiptAttachmentCommand,
    FinalizeReceiptAttachmentCommand,
    IssueReceiptAttachmentUploadTicketCommand,
    SetReceiptAttachmentLifecycleCommand,
    create_receipt_attachment,
    finalize_receipt_attachment,
    issue_receipt_attachment_upload_ticket,
    set_receipt_attachment_lifecycle,
)
from cookops.media_storage import InvalidReceiptImage, LocalReceiptMediaStorage
from cookops.persistence.models import (
    ClientInstallation,
    Organization,
    OrganizationMembership,
    ReceiptAttachment,
    SystemRoleAssignment,
    User,
)


@dataclass(frozen=True, slots=True)
class MediaHttpServices:
    browser_sessions: BrowserSessionService
    session_factory: async_sessionmaker[AsyncSession]
    storage: LocalReceiptMediaStorage


class CreateAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    attachment_id: UUID
    organization_id: UUID
    receipt_id: UUID
    client_installation_id: UUID
    media_type: str
    position_key: str
    client_wall_time: datetime


class CreateAttachmentResponse(BaseModel):
    attachment_id: UUID
    ticket_secret: str
    ticket_expires_at: datetime


class FinalizeAttachmentResponse(BaseModel):
    attachment_id: UUID
    storage_state: str
    media_type: str
    byte_size: int
    pixel_width: int
    pixel_height: int


class AttachmentStatusResponse(BaseModel):
    attachment_id: UUID
    storage_state: str
    content_hash: str | None
    source_content_hash: str | None
    byte_size: int | None
    source_byte_size: int | None
    pixel_width: int | None
    pixel_height: int | None
    media_type: str | None
    retired: bool


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mutation_id: UUID
    organization_id: UUID
    receipt_id: UUID
    client_installation_id: UUID
    client_wall_time: datetime


class AttachmentLifecycleRequest(TicketRequest):
    operation: Literal["retire", "restore"]


def _services(request: Request) -> MediaHttpServices:
    services = getattr(request.app.state, "media", None)
    if not isinstance(services, MediaHttpServices):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="media unavailable"
        )
    return services


async def _context(
    request: Request, services: MediaHttpServices, installation_id: UUID
) -> ExecutionContext:
    user_id = await _authenticated_user_id(request, services)
    async with services.session_factory() as session, session.begin():
        await session.execute(
            insert(ClientInstallation)
            .values(id=installation_id, user_id=user_id, installation_kind="browser")
            .on_conflict_do_nothing(index_elements=("id",))
        )
        owned = await session.scalar(
            select(ClientInstallation.id).where(
                ClientInstallation.id == installation_id,
                ClientInstallation.user_id == user_id,
                ClientInstallation.installation_kind == "browser",
                ClientInstallation.disabled_at.is_(None),
            )
        )
    if owned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
    return ExecutionContext(user_id, installation_id)


async def _authenticated_user_id(request: Request, services: MediaHttpServices) -> UUID:
    settings = request.app.state.settings
    secret = request.cookies.get(settings.browser_session_cookie_name)
    authenticated = await services.browser_sessions.authenticate(secret) if secret else None
    if authenticated is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return authenticated.user_id


async def _require_read_access(
    session: AsyncSession, actor_user_id: UUID, organization_id: UUID
) -> None:
    """Read media with a current browser session; installations only attribute writes."""
    active_user = await session.scalar(
        select(User.id).where(User.id == actor_user_id, User.disabled_at.is_(None))
    )
    active_organization = await session.scalar(
        select(Organization.id).where(
            Organization.id == organization_id, Organization.retired_at.is_(None)
        )
    )
    if active_user is None or active_organization is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
    system_admin = await session.scalar(
        select(SystemRoleAssignment.id).where(
            SystemRoleAssignment.user_id == actor_user_id,
            SystemRoleAssignment.role == "system_admin",
            SystemRoleAssignment.revoked_at.is_(None),
        )
    )
    membership = system_admin or await session.scalar(
        select(OrganizationMembership.id).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == actor_user_id,
            OrganizationMembership.state == "active",
            OrganizationMembership.role.in_(("member", "organization_admin")),
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})


def _error(error: ApplicationServiceError) -> HTTPException:
    code = (
        status.HTTP_409_CONFLICT
        if error.code == "idempotency_mismatch"
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(
        status_code=code,
        detail={
            "code": error.code,
            "field_violations": [item.path for item in error.field_violations],
        },
    )


def create_media_router() -> APIRouter:
    router = APIRouter(prefix="/media", tags=["media"])

    @router.post("/receipt-attachments", response_model=CreateAttachmentResponse)
    async def create_attachment(
        payload: CreateAttachmentRequest, request: Request
    ) -> CreateAttachmentResponse:
        services = _services(request)
        context = await _context(request, services, payload.client_installation_id)
        try:
            result = await create_receipt_attachment(
                services.session_factory,
                context,
                CreateReceiptAttachmentCommand(
                    payload.mutation_id,
                    payload.attachment_id,
                    payload.organization_id,
                    payload.receipt_id,
                    cast(Literal["image/jpeg", "image/webp"], payload.media_type),
                    payload.position_key,
                    payload.client_wall_time,
                ),
            )
        except ApplicationServiceError as error:
            raise _error(error) from error
        if result.ticket_secret is None or result.ticket_expires_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail={"code": "ticket_reissue_required"}
            )
        return CreateAttachmentResponse(
            attachment_id=result.attachment_id,
            ticket_secret=result.ticket_secret,
            ticket_expires_at=result.ticket_expires_at,
        )

    @router.post(
        "/receipt-attachments/{attachment_id}/upload-ticket",
        response_model=CreateAttachmentResponse,
    )
    async def issue_upload_ticket(
        attachment_id: UUID, payload: TicketRequest, request: Request
    ) -> CreateAttachmentResponse:
        services = _services(request)
        context = await _context(request, services, payload.client_installation_id)
        try:
            result = await issue_receipt_attachment_upload_ticket(
                services.session_factory,
                context,
                IssueReceiptAttachmentUploadTicketCommand(
                    payload.mutation_id,
                    attachment_id,
                    payload.organization_id,
                    payload.receipt_id,
                    payload.client_wall_time,
                ),
            )
        except ApplicationServiceError as error:
            raise _error(error) from error
        if result.ticket_secret is None or result.ticket_expires_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail={"code": "retry_ticket"}
            )
        return CreateAttachmentResponse(
            attachment_id=result.attachment_id,
            ticket_secret=result.ticket_secret,
            ticket_expires_at=result.ticket_expires_at,
        )

    @router.post(
        "/receipt-attachments/{attachment_id}/lifecycle", status_code=status.HTTP_204_NO_CONTENT
    )
    async def change_attachment_lifecycle(
        attachment_id: UUID, payload: AttachmentLifecycleRequest, request: Request
    ) -> None:
        services = _services(request)
        context = await _context(request, services, payload.client_installation_id)
        try:
            await set_receipt_attachment_lifecycle(
                services.session_factory,
                context,
                SetReceiptAttachmentLifecycleCommand(
                    payload.mutation_id,
                    attachment_id,
                    payload.organization_id,
                    payload.receipt_id,
                    payload.operation,
                    payload.client_wall_time,
                ),
            )
        except ApplicationServiceError as error:
            raise _error(error) from error

    @router.put("/receipt-attachments/{attachment_id}", response_model=FinalizeAttachmentResponse)
    async def upload_attachment(
        attachment_id: UUID, request: Request
    ) -> FinalizeAttachmentResponse:
        services = _services(request)
        try:
            installation_id = UUID(request.headers["x-cookops-client-installation"])
            mutation_id = UUID(request.headers["x-cookops-mutation-id"])
            organization_id = UUID(request.headers["x-cookops-organization-id"])
            receipt_id = UUID(request.headers["x-cookops-receipt-id"])
            ticket_secret = request.headers["x-cookops-upload-ticket"]
            replaces_attachment_id = (
                UUID(request.headers["x-cookops-replace-attachment-id"])
                if request.headers.get("x-cookops-replace-attachment-id")
                else None
            )
            if not ticket_secret.isascii() or len(ticket_secret) > 128:
                raise ValueError
        except (KeyError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid upload"
            ) from error
        length = request.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > 2_000_000):
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image is too large"
            )
        context = await _context(request, services, installation_id)
        stage = services.storage.new_stage_path()
        chunks: list[bytes] = []
        staged = None
        result = None
        try:
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > 2_000_000:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="image is too large",
                    )
                chunks.append(chunk)
            staged = services.storage.stage(stage, chunks, 2_000_000)
            result = await finalize_receipt_attachment(
                services.session_factory,
                context,
                FinalizeReceiptAttachmentCommand(
                    mutation_id,
                    attachment_id,
                    organization_id,
                    receipt_id,
                    ticket_secret,
                    datetime.now(UTC),
                    replaces_attachment_id,
                ),
                staged,
                services.storage,
            )
        except InvalidReceiptImage as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid image"
            ) from error
        except ApplicationServiceError as error:
            raise _error(error) from error
        finally:
            if staged is not None and result is None and not stage.exists():
                services.storage.discard(attachment_id)
            stage.unlink(missing_ok=True)
            stage.with_name(f"{stage.name}.thumbnail").unlink(missing_ok=True)
        return FinalizeAttachmentResponse(
            attachment_id=result.attachment_id,
            storage_state=result.storage_state,
            media_type=result.media_type,
            byte_size=result.byte_size,
            pixel_width=result.pixel_width,
            pixel_height=result.pixel_height,
        )

    @router.get("/receipt-attachments/{attachment_id}")
    async def download_attachment(
        attachment_id: UUID, organization_id: UUID, request: Request
    ) -> Response:
        services = _services(request)
        actor_user_id = await _authenticated_user_id(request, services)
        async with services.session_factory() as session, session.begin():
            await _require_read_access(session, actor_user_id, organization_id)
            attachment = await session.scalar(
                select(ReceiptAttachment).where(
                    ReceiptAttachment.id == attachment_id,
                    ReceiptAttachment.organization_id == organization_id,
                    ReceiptAttachment.storage_state == "ready",
                    ReceiptAttachment.retired_at.is_(None),
                )
            )
            if attachment is None or attachment.created_by_user_id is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
                )
        try:
            with services.storage.open(attachment.storage_object_key or "") as source:
                data = source.read()
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
            ) from error
        return Response(
            data, media_type=attachment.media_type, headers={"cache-control": "private, no-store"}
        )

    @router.get(
        "/receipt-attachments/{attachment_id}/status", response_model=AttachmentStatusResponse
    )
    async def attachment_status(
        attachment_id: UUID, organization_id: UUID, receipt_id: UUID, request: Request
    ) -> AttachmentStatusResponse:
        services = _services(request)
        actor_user_id = await _authenticated_user_id(request, services)
        async with services.session_factory() as session, session.begin():
            await _require_read_access(session, actor_user_id, organization_id)
            attachment = await session.scalar(
                select(ReceiptAttachment).where(
                    ReceiptAttachment.id == attachment_id,
                    ReceiptAttachment.organization_id == organization_id,
                    ReceiptAttachment.receipt_id == receipt_id,
                )
            )
        if attachment is None:
            return AttachmentStatusResponse(
                attachment_id=attachment_id,
                storage_state="absent",
                content_hash=None,
                source_content_hash=None,
                byte_size=None,
                source_byte_size=None,
                pixel_width=None,
                pixel_height=None,
                media_type=None,
                retired=False,
            )
        return AttachmentStatusResponse(
            attachment_id=attachment.id,
            storage_state=attachment.storage_state,
            content_hash=attachment.content_hash.hex() if attachment.content_hash else None,
            source_content_hash=(
                attachment.source_content_hash.hex() if attachment.source_content_hash else None
            ),
            byte_size=attachment.byte_size,
            source_byte_size=attachment.source_byte_size,
            pixel_width=attachment.pixel_width,
            pixel_height=attachment.pixel_height,
            media_type=attachment.media_type,
            retired=attachment.retired_at is not None,
        )

    return router
