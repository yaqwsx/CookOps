"""Add client installation and mutation idempotency records."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_client_mutations"
down_revision: str | None = "0003_organization_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "installation_kind IN ('browser', 'agent')",
            name="ck_client_installations_kind",
        ),
        sa.CheckConstraint(
            "(disabled_at IS NULL AND disabled_by_user_id IS NULL) OR "
            "(disabled_at IS NOT NULL AND disabled_by_user_id IS NOT NULL "
            "AND disabled_at >= created_at)",
            name="ck_client_installations_disabled_lifecycle",
        ),
        sa.ForeignKeyConstraint(["disabled_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="uq_client_installations_id_user"),
    )

    op.create_table(
        "mutations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("logical_operation_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("is_system_administration_scope", sa.Boolean(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("client_installation_id", sa.Uuid(), nullable=False),
        sa.Column("oauth_client_id", sa.String(length=255), nullable=True),
        sa.Column("oauth_grant_id", sa.String(length=255), nullable=True),
        sa.Column("client_wall_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "server_received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("command_schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("command_kind", sa.String(length=100), nullable=False),
        sa.Column("target_identities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("outcome_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_change_sequence", sa.BigInteger(), nullable=True),
        sa.Column("last_change_sequence", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "(organization_id IS NOT NULL AND NOT is_system_administration_scope) OR "
            "(organization_id IS NULL AND is_system_administration_scope)",
            name="ck_mutations_scope",
        ),
        sa.CheckConstraint(
            "actor_role IN ('member', 'organization_admin', 'system_admin')",
            name="ck_mutations_actor_role",
        ),
        sa.CheckConstraint(
            "NOT is_system_administration_scope OR actor_role = 'system_admin'",
            name="ck_mutations_system_authority",
        ),
        sa.CheckConstraint(
            "command_schema_version > 0",
            name="ck_mutations_command_schema_version",
        ),
        sa.CheckConstraint(
            "command_kind ~ '^[a-z][a-z0-9_.-]*$'",
            name="ck_mutations_command_kind",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_identities) = 'array' "
            "AND jsonb_array_length(target_identities) > 0 "
            "AND NOT jsonb_path_exists(target_identities, "
            '\'$[*] ? (@.type() != "object" '
            '|| !exists(@.entity_kind) || @.entity_kind.type() != "string" '
            '|| !(@.entity_kind like_regex "^[a-z][a-z0-9_.-]{0,99}$") '
            '|| !exists(@.entity_id) || @.entity_id.type() != "string" '
            "|| !(@.entity_id like_regex "
            '"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))\') '
            "AND NOT jsonb_path_exists(target_identities, "
            '\'$[*].keyvalue() ? (@.key != "entity_kind" && @.key != "entity_id")\')',
            name="ck_mutations_target_identities",
        ),
        sa.CheckConstraint(
            "octet_length(request_hash) = 32",
            name="ck_mutations_request_hash",
        ),
        sa.CheckConstraint(
            "(oauth_client_id IS NULL AND oauth_grant_id IS NULL) OR "
            "(oauth_client_id IS NOT NULL AND btrim(oauth_client_id) <> '' "
            "AND oauth_client_id = btrim(oauth_client_id) "
            "AND oauth_grant_id IS NOT NULL AND btrim(oauth_grant_id) <> '' "
            "AND oauth_grant_id = btrim(oauth_grant_id))",
            name="ck_mutations_oauth_attribution",
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'partially_superseded', 'rejected', 'failed')",
            name="ck_mutations_outcome",
        ),
        sa.CheckConstraint(
            "outcome_payload IS NULL OR jsonb_typeof(outcome_payload) = 'object'",
            name="ck_mutations_outcome_payload",
        ),
        sa.CheckConstraint(
            "(first_change_sequence IS NULL AND last_change_sequence IS NULL "
            "AND (is_system_administration_scope OR outcome IN ('rejected', 'failed'))) OR "
            "(first_change_sequence > 0 AND last_change_sequence >= first_change_sequence "
            "AND organization_id IS NOT NULL "
            "AND outcome IN ('accepted', 'partially_superseded'))",
            name="ck_mutations_change_sequence",
        ),
        sa.CheckConstraint(
            "NOT is_system_administration_scope OR outcome <> 'partially_superseded'",
            name="ck_mutations_system_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["client_installation_id", "actor_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_mutations_client_actor",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("mutations")
    op.drop_table("client_installations")
