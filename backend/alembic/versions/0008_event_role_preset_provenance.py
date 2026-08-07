"""Retain copied event meal-role preset provenance."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_event_role_provenance"
down_revision: str | None = "0007_event_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_meal_roles",
        sa.Column("source_preset_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_event_meal_roles_source_preset",
        "event_meal_roles",
        "organization_meal_role_presets",
        ["source_preset_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE FUNCTION event_meal_role_validate_source_preset_organization()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.source_preset_id IS NULL THEN
                RETURN NEW;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM events AS event
                JOIN organization_meal_role_presets AS preset
                  ON preset.id = NEW.source_preset_id
                WHERE event.id = NEW.event_id
                  AND preset.organization_id = event.organization_id
            ) THEN
                RAISE EXCEPTION 'event meal-role source preset belongs to another organization';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_event_meal_role_validate_source_preset_organization
        BEFORE INSERT OR UPDATE OF event_id, source_preset_id ON event_meal_roles
        FOR EACH ROW EXECUTE FUNCTION event_meal_role_validate_source_preset_organization()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS tr_event_meal_role_validate_source_preset_organization "
        "ON event_meal_roles"
    )
    op.execute("DROP FUNCTION IF EXISTS event_meal_role_validate_source_preset_organization()")
    op.drop_constraint("fk_event_meal_roles_source_preset", "event_meal_roles", type_="foreignkey")
    op.drop_column("event_meal_roles", "source_preset_id")
