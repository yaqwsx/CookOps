"""Create named event dietary exceptions and tag selections."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_event_dietary_exceptions"
down_revision: str | None = "0017_media_source_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_dietary_exceptions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("retired_by_user_id", sa.Uuid()),
        sa.CheckConstraint(
            "btrim(name) <> '' AND name = btrim(name)", name="ck_event_dietary_exceptions_name"
        ),
        sa.CheckConstraint(
            "note IS NULL OR octet_length(note) <= 131072", name="ck_event_dietary_exceptions_note"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_event_dietary_exceptions_retirement",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["events.id", "events.organization_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_event_dietary_exceptions_id_org"),
    )
    op.create_index(
        "ix_event_dietary_exceptions_event_active",
        "event_dietary_exceptions",
        ["event_id", "retired_at"],
    )
    op.create_table(
        "event_dietary_exception_tags",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("exception_id", sa.Uuid(), nullable=False),
        sa.Column("dietary_tag_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("retired_by_user_id", sa.Uuid()),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_event_dietary_exception_tags_retirement",
        ),
        sa.ForeignKeyConstraint(
            ["exception_id", "organization_id"],
            ["event_dietary_exceptions.id", "event_dietary_exceptions.organization_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dietary_tag_id", "organization_id"],
            ["dietary_tags.id", "dietary_tags.organization_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_event_dietary_exception_tags_id_org"),
    )
    op.create_index(
        "uq_event_dietary_exception_tags_active_pair",
        "event_dietary_exception_tags",
        ["exception_id", "dietary_tag_id"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_event_dietary_exception_tags_active_pair", table_name="event_dietary_exception_tags"
    )
    op.drop_table("event_dietary_exception_tags")
    op.drop_index("ix_event_dietary_exceptions_event_active", table_name="event_dietary_exceptions")
    op.drop_table("event_dietary_exceptions")
