import base64
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import PostgresDsn
from sqlalchemy import insert, select
from test_create_event_service import ServiceDatabase
from test_receipt_media_service import _event, _jpeg, _receipt

from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app
from cookops.persistence.models import ExternalIdentity, ReceiptAttachment

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)
pytest_plugins = ("test_create_event_service",)

KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").rstrip(b"=").decode()


def _settings(root: Path) -> Settings:
    return Settings(
        environment=Environment.TEST,
        human_auth_provider=HumanAuthProvider.DUMMY,
        database_url=PostgresDsn(os.environ["TEST_DATABASE_URL"]),
        browser_session_hmac_key=KEY,
        receipt_media_root=root,
    )


def test_attachment_status_requires_current_authorized_browser_session(
    service_database: ServiceDatabase, tmp_path: Path
) -> None:
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            insert(ExternalIdentity).values(
                user_id=service_database.actor_id,
                provider="dummy",
                provider_subject="receipt-media-member",
                verified_email="admin@example.test",
                normalized_verified_email="admin@example.test",
            )
        )
    receipt_id = _receipt(service_database, _event(service_database))
    attachment_id, installation_id = uuid4(), uuid4()
    path = f"/media/receipt-attachments/{attachment_id}/status"
    params = {
        "organization_id": str(service_database.organization_id),
        "receipt_id": str(receipt_id),
    }
    raw = _jpeg()
    with TestClient(
        create_app(_settings(tmp_path / "http")), base_url="https://testserver"
    ) as client:
        assert client.get(path, params=params).status_code == 401
        assert (
            client.post("/auth/dummy/session", json={"subject": "receipt-media-member"}).status_code
            == 204
        )
        created = client.post(
            "/media/receipt-attachments",
            json={
                "mutation_id": str(uuid4()),
                "attachment_id": str(attachment_id),
                "organization_id": str(service_database.organization_id),
                "receipt_id": str(receipt_id),
                "client_installation_id": str(installation_id),
                "media_type": "image/jpeg",
                "position_key": "a",
                "client_wall_time": datetime.now(UTC).isoformat(),
            },
        )
        assert created.status_code == 200
        uploaded = client.put(
            f"/media/receipt-attachments/{attachment_id}",
            content=raw,
            headers={
                "content-type": "image/jpeg",
                "x-cookops-client-installation": str(installation_id),
                "x-cookops-mutation-id": str(uuid4()),
                "x-cookops-organization-id": str(service_database.organization_id),
                "x-cookops-receipt-id": str(receipt_id),
                "x-cookops-upload-ticket": created.json()["ticket_secret"],
            },
        )
        assert uploaded.status_code == 200
        with service_database.sync_engine.connect() as connection:
            expected_hash, expected_size = connection.execute(
                select(ReceiptAttachment.content_hash, ReceiptAttachment.byte_size).where(
                    ReceiptAttachment.id == attachment_id
                )
            ).one()
            expected_hash = expected_hash.hex() if expected_hash else None
        response = client.get(path, params=params)
        assert response.status_code == 200
        assert response.json() == {
            "attachment_id": str(attachment_id),
            "storage_state": "ready",
            "content_hash": expected_hash,
            "source_content_hash": hashlib.sha256(raw).hexdigest(),
            "byte_size": expected_size,
            "source_byte_size": len(raw),
            "pixel_width": 3,
            "pixel_height": 2,
            "media_type": "image/jpeg",
            "retired": False,
        }
        assert client.get(path, params={**params, "receipt_id": str(uuid4())}).json() == {
            "attachment_id": str(attachment_id),
            "storage_state": "absent",
            "content_hash": None,
            "source_content_hash": None,
            "byte_size": None,
            "source_byte_size": None,
            "pixel_width": None,
            "pixel_height": None,
            "media_type": None,
            "retired": False,
        }
