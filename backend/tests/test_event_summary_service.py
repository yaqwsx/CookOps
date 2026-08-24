import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import update
from test_schedule_recipe_service import ServiceDatabase

from cookops.application.events import EventQueryDenied, get_event_summary
from cookops.persistence.models import OrganizationMembership

pytest_plugins = ("test_schedule_recipe_service",)
pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


def test_event_summary_allows_current_member(service_database: ServiceDatabase) -> None:
    summary = asyncio.run(
        get_event_summary(
            service_database.sessions,
            actor_user_id=service_database.actor_id,
            organization_id=service_database.organization_id,
            event_id=service_database.event_id,
        )
    )
    assert summary.id == service_database.event_id
    assert summary.organization_id == service_database.organization_id


def test_event_summary_rechecks_revoked_membership(
    service_database: ServiceDatabase,
) -> None:
    asyncio.run(
        get_event_summary(
            service_database.sessions,
            actor_user_id=service_database.actor_id,
            organization_id=service_database.organization_id,
            event_id=service_database.event_id,
        )
    )
    with service_database.sync_engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == service_database.organization_id,
                OrganizationMembership.user_id == service_database.actor_id,
            )
            .values(
                state="removed",
                removed_at=datetime.now(UTC),
                removed_by_user_id=service_database.actor_id,
            )
        )
    with pytest.raises(EventQueryDenied, match="organization access denied"):
        asyncio.run(
            get_event_summary(
                service_database.sessions,
                actor_user_id=service_database.actor_id,
                organization_id=service_database.organization_id,
                event_id=service_database.event_id,
            )
        )


@pytest.mark.parametrize("target", ["foreign_organization", "missing_event"])
def test_event_summary_does_not_enumerate_denied_targets(
    service_database: ServiceDatabase, target: str
) -> None:
    organization_id = (
        service_database.other_organization_id
        if target == "foreign_organization"
        else service_database.organization_id
    )
    event_id = service_database.event_id if target == "foreign_organization" else uuid4()
    with pytest.raises(EventQueryDenied, match="organization access denied|event unavailable"):
        asyncio.run(
            get_event_summary(
                service_database.sessions,
                actor_user_id=service_database.actor_id,
                organization_id=organization_id,
                event_id=event_id,
            )
        )
