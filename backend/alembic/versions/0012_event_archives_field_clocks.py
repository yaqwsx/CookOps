"""Add archive lifecycle guards and synchronizable field clocks.

This revision deliberately provides persistence invariants only.  The archive
materializer is an online-only application service and is introduced separately;
no service may transition an event to ``archived`` until it can create the full,
immutable snapshot required by the archive contract.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_event_archives_field_clocks"
down_revision: str | None = "0011_scheduled_recipes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_archive_snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("previous_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("archive_schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("attachment_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("archive_schema_version > 0", name="ck_event_archive_snapshots_schema"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_event_archive_snapshots_payload"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attachment_manifest) = 'array'",
            name="ck_event_archive_snapshots_attachment_manifest",
        ),
        sa.CheckConstraint(
            "octet_length(content_hash) = 32", name="ck_event_archive_snapshots_content_hash"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["previous_snapshot_id", "event_id"],
            ["event_archive_snapshots.id", "event_archive_snapshots.event_id"],
            ondelete="RESTRICT",
            name="fk_event_archive_snapshots_previous_snapshot",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "event_id", name="uq_event_archive_snapshots_id_event"),
    )
    op.execute(
        """
        CREATE FUNCTION event_archive_snapshot_prevent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'event archive snapshots are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_event_archive_snapshot_prevent_mutation
        BEFORE UPDATE OR DELETE OR TRUNCATE ON event_archive_snapshots
        FOR EACH STATEMENT EXECUTE FUNCTION event_archive_snapshot_prevent_mutation()
        """
    )

    op.add_column(
        "events",
        sa.Column(
            "lifecycle", sa.String(length=16), server_default=sa.text("'active'"), nullable=False
        ),
    )
    op.add_column("events", sa.Column("current_archive_snapshot_id", sa.Uuid(), nullable=True))
    op.add_column("events", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("events", sa.Column("archived_by_user_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        "ck_events_lifecycle",
        "events",
        "lifecycle IN ('active', 'archived')",
    )
    op.create_check_constraint(
        "ck_events_archive_lifecycle_attribution",
        "events",
        "(lifecycle = 'active' AND current_archive_snapshot_id IS NULL "
        "AND archived_at IS NULL AND archived_by_user_id IS NULL) OR "
        "(lifecycle = 'archived' AND current_archive_snapshot_id IS NOT NULL "
        "AND archived_at IS NOT NULL AND archived_by_user_id IS NOT NULL "
        "AND archived_at >= created_at)",
    )
    op.create_foreign_key(
        "fk_events_current_archive_snapshot",
        "events",
        "event_archive_snapshots",
        ["current_archive_snapshot_id", "id"],
        ["id", "event_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_events_archived_by_user",
        "events",
        "users",
        ["archived_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "field_clocks",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("entity_kind", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("field_name", sa.String(length=100), nullable=False),
        sa.Column("winning_client_wall_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("winning_mutation_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "entity_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_field_clocks_entity_kind"
        ),
        sa.CheckConstraint(
            "field_name ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_field_clocks_field_name"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "winning_mutation_id"],
            ["mutations.organization_id", "mutations.id"],
            ondelete="RESTRICT",
            name="fk_field_clocks_winning_mutation",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("organization_id", "entity_kind", "entity_id", "field_name"),
    )
    op.execute(
        """
        CREATE FUNCTION field_clock_verify_winning_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM mutations
                WHERE organization_id = NEW.organization_id
                  AND id = NEW.winning_mutation_id
                  AND outcome = 'accepted'
                  AND client_wall_time = NEW.winning_client_wall_time
            ) THEN
                RAISE EXCEPTION 'field clock must reference its accepted winning mutation';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER tr_field_clock_verify_winning_mutation
        AFTER INSERT OR UPDATE ON field_clocks
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION field_clock_verify_winning_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER tr_field_clock_verify_winning_mutation ON field_clocks")
    op.execute("DROP FUNCTION field_clock_verify_winning_mutation()")
    op.drop_table("field_clocks")
    op.drop_constraint("fk_events_archived_by_user", "events", type_="foreignkey")
    op.drop_constraint("fk_events_current_archive_snapshot", "events", type_="foreignkey")
    op.drop_constraint("ck_events_archive_lifecycle_attribution", "events", type_="check")
    op.drop_constraint("ck_events_lifecycle", "events", type_="check")
    op.drop_column("events", "archived_by_user_id")
    op.drop_column("events", "archived_at")
    op.drop_column("events", "current_archive_snapshot_id")
    op.drop_column("events", "lifecycle")
    op.execute("DROP TRIGGER tr_event_archive_snapshot_prevent_mutation ON event_archive_snapshots")
    op.execute("DROP FUNCTION event_archive_snapshot_prevent_mutation()")
    op.drop_table("event_archive_snapshots")
