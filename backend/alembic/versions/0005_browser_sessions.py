"""Add revocable server-side browser sessions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_browser_sessions"
down_revision: str | None = "0004_client_mutations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("secret_hmac", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "octet_length(secret_hmac) = 32",
            name="ck_browser_sessions_secret_hmac",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_browser_sessions_expiry",
        ),
        sa.CheckConstraint(
            "last_used_at IS NULL OR (last_used_at >= created_at AND last_used_at < expires_at)",
            name="ck_browser_sessions_last_used_at",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL "
            "AND revoked_at >= created_at "
            "AND (last_used_at IS NULL OR last_used_at <= revoked_at))",
            name="ck_browser_sessions_revocation_attribution",
        ),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hmac", name="uq_browser_sessions_secret_hmac"),
    )


def downgrade() -> None:
    op.drop_table("browser_sessions")
