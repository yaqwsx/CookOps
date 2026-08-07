"""Add event receipts and receipt-media metadata.

This revision deliberately stores only receipt and upload-ticket metadata.  The
media object-store interface, upload HTTP endpoints, image processing, and
change-feed publication belong to later vertical slices; image bytes are never
stored in PostgreSQL.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_receipts_media"
down_revision: str | None = "0013_shopping_list_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Receipt currency is intentionally stored with the receipt so immutable
    # archive payloads and sync records do not need to infer a historical value.
    # The composite key makes that stored value equal the event's one currency.
    op.create_unique_constraint(
        "uq_events_id_organization_currency", "events", ["id", "organization_id", "currency"]
    )

    op.create_table(
        "receipts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("total_amount", sa.Numeric(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "last_modified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_modified_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_receipts_title_not_empty"),
        sa.CheckConstraint(
            "total_amount >= 0 AND total_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_receipts_nonnegative_total",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_receipts_currency"),
        sa.CheckConstraint("last_modified_at >= created_at", name="ck_receipts_audit_order"),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_receipts_retirement_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "organization_id", "currency"],
            ["events.id", "events.organization_id", "events.currency"],
            ondelete="RESTRICT",
            name="fk_receipts_event_organization_currency",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["last_modified_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_receipts_id_organization"),
        sa.UniqueConstraint(
            "id", "event_id", "organization_id", name="uq_receipts_id_event_organization"
        ),
        sa.Index("ix_receipts_event_created_at", "event_id", "created_at"),
    )

    op.create_table(
        "receipt_attachments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("storage_state", sa.String(length=16), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("position_key", sa.String(length=255, collation="C"), nullable=False),
        sa.Column("storage_object_key", sa.String(length=500), nullable=True),
        sa.Column("thumbnail_object_key", sa.String(length=500), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("pixel_width", sa.Integer(), nullable=True),
        sa.Column("pixel_height", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "storage_state IN ('pending', 'ready', 'failed')",
            name="ck_receipt_attachments_storage_state",
        ),
        sa.CheckConstraint(
            "media_type IN ('image/jpeg', 'image/webp')",
            name="ck_receipt_attachments_media_type",
        ),
        sa.CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_receipt_attachments_position_key"
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_receipt_attachments_retirement_attribution",
        ),
        sa.CheckConstraint(
            "(storage_state IN ('pending', 'failed') "
            "AND storage_object_key IS NULL AND thumbnail_object_key IS NULL "
            "AND byte_size IS NULL AND pixel_width IS NULL AND pixel_height IS NULL "
            "AND content_hash IS NULL AND finalized_at IS NULL AND finalized_by_user_id IS NULL) "
            "OR (storage_state = 'ready' "
            "AND btrim(storage_object_key) <> '' AND btrim(thumbnail_object_key) <> '' "
            "AND byte_size > 0 AND pixel_width > 0 AND pixel_height > 0 "
            "AND octet_length(content_hash) = 32 "
            "AND finalized_at IS NOT NULL AND finalized_by_user_id IS NOT NULL "
            "AND finalized_at >= created_at)",
            name="ck_receipt_attachments_storage_metadata",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id", "organization_id"],
            ["receipts.id", "receipts.organization_id"],
            ondelete="RESTRICT",
            name="fk_receipt_attachments_receipt_organization",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["finalized_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_receipt_attachments_id_organization"),
        sa.Index("ix_receipt_attachments_receipt_position", "receipt_id", "position_key", "id"),
    )

    # A finalized attachment names immutable bytes.  It may still be retired or
    # restored, but neither its content metadata nor storage state may be rewritten.
    op.execute(
        """
        CREATE FUNCTION receipt_attachment_prevent_ready_content_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.media_type IS DISTINCT FROM OLD.media_type THEN
                RAISE EXCEPTION 'receipt attachment media type is immutable';
            END IF;
            IF OLD.storage_state = 'ready' AND (
                NEW.storage_state IS DISTINCT FROM OLD.storage_state
                OR NEW.storage_object_key IS DISTINCT FROM OLD.storage_object_key
                OR NEW.thumbnail_object_key IS DISTINCT FROM OLD.thumbnail_object_key
                OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
                OR NEW.pixel_width IS DISTINCT FROM OLD.pixel_width
                OR NEW.pixel_height IS DISTINCT FROM OLD.pixel_height
                OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at
                OR NEW.finalized_by_user_id IS DISTINCT FROM OLD.finalized_by_user_id
            ) THEN
                RAISE EXCEPTION 'ready receipt attachment content is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_receipt_attachment_prevent_ready_content_mutation
        BEFORE UPDATE ON receipt_attachments
        FOR EACH ROW EXECUTE FUNCTION receipt_attachment_prevent_ready_content_mutation()
        """
    )

    op.create_table(
        "media_upload_tickets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("receipt_attachment_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("oauth_client_id", sa.String(length=255), nullable=True),
        sa.Column("oauth_grant_id", sa.String(length=255), nullable=True),
        sa.Column("secret_hmac", sa.LargeBinary(length=32), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("maximum_byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("octet_length(secret_hmac) = 32", name="ck_media_upload_tickets_secret"),
        sa.CheckConstraint(
            "media_type IN ('image/jpeg', 'image/webp')",
            name="ck_media_upload_tickets_media_type",
        ),
        sa.CheckConstraint(
            "maximum_byte_size > 0", name="ck_media_upload_tickets_positive_maximum_size"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_media_upload_tickets_expiry"),
        sa.CheckConstraint(
            "used_at IS NULL OR (used_at >= created_at AND used_at <= expires_at)",
            name="ck_media_upload_tickets_use_time",
        ),
        sa.CheckConstraint(
            "(oauth_client_id IS NULL AND oauth_grant_id IS NULL) OR "
            "(oauth_client_id IS NOT NULL AND btrim(oauth_client_id) <> '' "
            "AND oauth_client_id = btrim(oauth_client_id) "
            "AND oauth_grant_id IS NOT NULL AND btrim(oauth_grant_id) <> '' "
            "AND oauth_grant_id = btrim(oauth_grant_id))",
            name="ck_media_upload_tickets_oauth_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_attachment_id"], ["receipt_attachments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hmac", name="uq_media_upload_tickets_secret_hmac"),
        sa.Index("ix_media_upload_tickets_attachment", "receipt_attachment_id"),
    )
    op.execute(
        """
        CREATE FUNCTION media_upload_ticket_verify_attachment_media_type()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM receipt_attachments
                WHERE id = NEW.receipt_attachment_id
                  AND media_type = NEW.media_type
            ) THEN
                RAISE EXCEPTION 'media upload ticket media type must match receipt attachment';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER tr_media_upload_ticket_verify_attachment_media_type
        AFTER INSERT OR UPDATE ON media_upload_tickets
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION media_upload_ticket_verify_attachment_media_type()
        """
    )
    op.execute(
        """
        CREATE FUNCTION media_upload_ticket_prevent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.receipt_attachment_id IS DISTINCT FROM OLD.receipt_attachment_id
                OR NEW.user_id IS DISTINCT FROM OLD.user_id
                OR NEW.oauth_client_id IS DISTINCT FROM OLD.oauth_client_id
                OR NEW.oauth_grant_id IS DISTINCT FROM OLD.oauth_grant_id
                OR NEW.secret_hmac IS DISTINCT FROM OLD.secret_hmac
                OR NEW.media_type IS DISTINCT FROM OLD.media_type
                OR NEW.maximum_byte_size IS DISTINCT FROM OLD.maximum_byte_size
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
                OR OLD.used_at IS NOT NULL
                OR NEW.used_at IS NULL THEN
                RAISE EXCEPTION 'media upload ticket is single-use and immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_media_upload_ticket_prevent_mutation
        BEFORE UPDATE ON media_upload_tickets
        FOR EACH ROW EXECUTE FUNCTION media_upload_ticket_prevent_mutation()
        """
    )


def downgrade() -> None:
    # ``IF EXISTS`` also makes a development downgrade recoverable when an
    # interrupted local upgrade was repaired manually before Alembic is rerun.
    op.execute(
        "DROP TRIGGER IF EXISTS tr_media_upload_ticket_prevent_mutation ON media_upload_tickets"
    )
    op.execute("DROP FUNCTION IF EXISTS media_upload_ticket_prevent_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS tr_media_upload_ticket_verify_attachment_media_type "
        "ON media_upload_tickets"
    )
    op.execute("DROP FUNCTION IF EXISTS media_upload_ticket_verify_attachment_media_type()")
    op.drop_table("media_upload_tickets")
    op.execute(
        "DROP TRIGGER IF EXISTS tr_receipt_attachment_prevent_ready_content_mutation "
        "ON receipt_attachments"
    )
    op.execute("DROP FUNCTION IF EXISTS receipt_attachment_prevent_ready_content_mutation()")
    op.drop_table("receipt_attachments")
    op.drop_table("receipts")
    op.drop_constraint("uq_events_id_organization_currency", "events", type_="unique")
