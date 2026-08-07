"""Persist the exact browser-normalized upload identity beside server media bytes."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_media_source_identity"
down_revision: str | None = "0016_shopping_snapshot_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("receipt_attachments", sa.Column("source_byte_size", sa.BigInteger()))
    op.add_column("receipt_attachments", sa.Column("source_content_hash", sa.LargeBinary(32)))
    op.create_check_constraint(
        "ck_receipt_attachments_source_identity",
        "receipt_attachments",
        "(source_byte_size IS NULL AND source_content_hash IS NULL) OR "
        "(source_byte_size > 0 AND octet_length(source_content_hash) = 32)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_receipt_attachments_source_identity", "receipt_attachments", type_="check"
    )
    op.drop_column("receipt_attachments", "source_content_hash")
    op.drop_column("receipt_attachments", "source_byte_size")
