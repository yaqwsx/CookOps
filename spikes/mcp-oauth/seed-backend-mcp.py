"""Seed the minimum real event projection for the disposable MCP smoke."""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from cookops.persistence.models import Event, Organization, OrganizationMembership, User
from sqlalchemy import create_engine, insert

ACTOR_ID = UUID("018f7cc9-4a90-7fa0-b7e4-77f6c42d5731")
ORGANIZATION_ID = UUID("018f7cca-4a90-7fa0-b7e4-77f6c42d5731")
EVENT_ID = UUID("018f7ccb-4a90-7fa0-b7e4-77f6c42d5731")
FOREIGN_ORGANIZATION_ID = UUID("018f7ccc-4a90-7fa0-b7e4-77f6c42d5731")
FOREIGN_EVENT_ID = UUID("018f7ccd-4a90-7fa0-b7e4-77f6c42d5731")


def main() -> None:
    import os

    engine = create_engine(os.environ["COOKOPS_DATABASE_URL"])
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=ACTOR_ID,
                display_name="MCP smoke member",
                verified_email="mcp-smoke@example.test",
                normalized_email="mcp-smoke@example.test",
            )
        )
        connection.execute(
            insert(Organization),
            [
                {"id": organization_id, "name": name, "created_by_user_id": ACTOR_ID}
                for organization_id, name in (
                    (ORGANIZATION_ID, "MCP smoke kitchen"),
                    (FOREIGN_ORGANIZATION_ID, "MCP foreign kitchen"),
                )
            ],
        )
        connection.execute(
            insert(OrganizationMembership).values(
                organization_id=ORGANIZATION_ID,
                user_id=ACTOR_ID,
                invited_email="mcp-smoke@example.test",
                role="member",
                state="active",
                invited_by_user_id=ACTOR_ID,
                claimed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        connection.execute(
            insert(Event),
            [
                {
                    "id": event_id,
                    "organization_id": organization_id,
                    "name": name,
                    "start_date": date(2026, 1, 1),
                    "end_date": date(2026, 1, 1),
                    "base_expected_attendance": 12,
                    "budget_amount": Decimal("10.50"),
                    "currency": "CZK",
                    "created_by_user_id": ACTOR_ID,
                }
                for event_id, organization_id, name in (
                    (EVENT_ID, ORGANIZATION_ID, "MCP smoke event"),
                    (FOREIGN_EVENT_ID, FOREIGN_ORGANIZATION_ID, "MCP foreign event"),
                )
            ],
        )


if __name__ == "__main__":
    main()
