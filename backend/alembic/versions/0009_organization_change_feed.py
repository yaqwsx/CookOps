"""Add organization-local canonical change-feed records."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_organization_change_feed"
down_revision: str | None = "0008_event_role_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_unique_constraint(
        "uq_mutations_id_organization",
        "mutations",
        ["id", "organization_id"],
    )
    op.create_table(
        "organization_change_heads",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("next_sequence > 0", name="ck_organization_change_heads_next_sequence"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("organization_id"),
    )
    op.create_table(
        "organization_change_transactions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("mutation_id", sa.Uuid(), nullable=False),
        sa.Column("first_change_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_change_sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "first_change_sequence > 0 AND last_change_sequence >= first_change_sequence",
            name="ck_organization_change_transactions_range",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "mutation_id"],
            ["mutations.organization_id", "mutations.id"],
            ondelete="RESTRICT",
            name="fk_organization_change_transactions_mutation",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("organization_id", "mutation_id"),
        postgresql.ExcludeConstraint(
            ("organization_id", "="),
            (sa.text("int8range(first_change_sequence, last_change_sequence, '[]')"), "&&"),
            name="ex_organization_change_transactions_nonoverlapping_range",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION organization_change_head_prevent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.next_sequence <> 1 THEN
                    RAISE EXCEPTION 'organization change head must begin at sequence one';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND pg_trigger_depth() > 1 THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'organization change head is allocator-managed';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_organization_change_head_prevent_mutation
        BEFORE INSERT OR UPDATE OR DELETE ON organization_change_heads
        FOR EACH ROW EXECUTE FUNCTION organization_change_head_prevent_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_organization_change_head_prevent_truncate
        BEFORE TRUNCATE ON organization_change_heads
        FOR EACH STATEMENT EXECUTE FUNCTION organization_change_head_prevent_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reserve_organization_change_transaction(
            p_organization_id uuid,
            p_mutation_id uuid,
            p_change_count integer
        )
        RETURNS TABLE(first_change_sequence bigint, last_change_sequence bigint)
        LANGUAGE plpgsql
        AS $$
        DECLARE
            allocated_first bigint;
            allocated_last bigint;
        BEGIN
            IF p_change_count <= 0 THEN
                RAISE EXCEPTION 'organization change count must be positive';
            END IF;

            INSERT INTO organization_change_heads (organization_id, next_sequence)
            VALUES (p_organization_id, 1)
            ON CONFLICT (organization_id) DO NOTHING;

            SELECT next_sequence
            INTO allocated_first
            FROM organization_change_heads
            WHERE organization_id = p_organization_id
            FOR UPDATE;

            allocated_last := allocated_first + p_change_count - 1;
            INSERT INTO organization_change_transactions (
                organization_id,
                mutation_id,
                first_change_sequence,
                last_change_sequence
            )
            VALUES (p_organization_id, p_mutation_id, allocated_first, allocated_last);

            RETURN QUERY SELECT allocated_first, allocated_last;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION organization_change_transaction_allocate_sequence()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_first bigint;
        BEGIN
            INSERT INTO organization_change_heads (organization_id, next_sequence)
            VALUES (NEW.organization_id, 1)
            ON CONFLICT (organization_id) DO NOTHING;

            SELECT next_sequence
            INTO expected_first
            FROM organization_change_heads
            WHERE organization_id = NEW.organization_id
            FOR UPDATE;

            IF NEW.first_change_sequence <> expected_first THEN
                RAISE EXCEPTION 'organization change transaction must begin at the next sequence';
            END IF;

            UPDATE organization_change_heads
            SET next_sequence = NEW.last_change_sequence + 1
            WHERE organization_id = NEW.organization_id;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_organization_change_transaction_allocate_sequence
        BEFORE INSERT ON organization_change_transactions
        FOR EACH ROW EXECUTE FUNCTION organization_change_transaction_allocate_sequence()
        """
    )
    op.create_table(
        "organization_changes",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("mutation_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("entity_kind", sa.String(length=100), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence > 0", name="ck_organization_changes_positive_sequence"),
        sa.CheckConstraint(
            "entity_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'",
            name="ck_organization_changes_entity_kind",
        ),
        sa.CheckConstraint(
            "operation ~ '^[a-z][a-z0-9_.-]{0,99}$'",
            name="ck_organization_changes_operation",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND payload ? 'record_schema_version' "
            "AND jsonb_typeof(payload -> 'record_schema_version') = 'number' "
            "AND (payload ->> 'record_schema_version') ~ '^[1-9][0-9]*$' "
            "AND payload ? 'record' AND jsonb_typeof(payload -> 'record') = 'object' "
            "AND NOT jsonb_path_exists(payload, "
            '\'$.keyvalue() ? (@.key != "record_schema_version" && @.key != "record")\') '
            "AND octet_length(payload::text) <= 262144 "
            "AND NOT jsonb_path_exists(payload, "
            '\'$.** ? (@.type() == "string" && @ like_regex "^data:" flag "i")\')',
            name="ck_organization_changes_payload",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "mutation_id"],
            [
                "organization_change_transactions.organization_id",
                "organization_change_transactions.mutation_id",
            ],
            ondelete="RESTRICT",
            name="fk_organization_changes_transaction",
        ),
        sa.PrimaryKeyConstraint("organization_id", "sequence"),
    )
    op.execute(
        """
        CREATE FUNCTION organization_change_validate_range()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            transaction_first bigint;
            transaction_last bigint;
        BEGIN
            SELECT first_change_sequence, last_change_sequence
            INTO transaction_first, transaction_last
            FROM organization_change_transactions
            WHERE organization_id = NEW.organization_id AND mutation_id = NEW.mutation_id;

            IF NEW.sequence < transaction_first OR NEW.sequence > transaction_last THEN
                RAISE EXCEPTION 'organization change sequence is outside its transaction range';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_organization_change_validate_range
        BEFORE INSERT ON organization_changes
        FOR EACH ROW EXECUTE FUNCTION organization_change_validate_range()
        """
    )
    op.execute(
        """
        CREATE FUNCTION organization_change_transaction_verify_complete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            change_count bigint;
            observed_first bigint;
            observed_last bigint;
            mutation_first bigint;
            mutation_last bigint;
        BEGIN
            SELECT count(*), min(sequence), max(sequence)
            INTO change_count, observed_first, observed_last
            FROM organization_changes
            WHERE organization_id = NEW.organization_id AND mutation_id = NEW.mutation_id;
            SELECT first_change_sequence, last_change_sequence
            INTO mutation_first, mutation_last
            FROM mutations
            WHERE organization_id = NEW.organization_id AND id = NEW.mutation_id;

            IF mutation_first IS DISTINCT FROM NEW.first_change_sequence
                OR mutation_last IS DISTINCT FROM NEW.last_change_sequence
                OR change_count <> NEW.last_change_sequence - NEW.first_change_sequence + 1
                OR observed_first IS DISTINCT FROM NEW.first_change_sequence
                OR observed_last IS DISTINCT FROM NEW.last_change_sequence THEN
                RAISE EXCEPTION 'organization change transaction is incomplete or mismatched';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER tr_organization_change_transaction_verify_complete
        AFTER INSERT ON organization_change_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION organization_change_transaction_verify_complete()
        """
    )
    op.execute(
        """
        CREATE FUNCTION organization_change_prevent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'organization changes are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_organization_change_prevent_mutation
        BEFORE UPDATE OR DELETE ON organization_changes
        FOR EACH ROW EXECUTE FUNCTION organization_change_prevent_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_organization_change_prevent_truncate
        BEFORE TRUNCATE ON organization_changes
        FOR EACH STATEMENT EXECUTE FUNCTION organization_change_prevent_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_organization_change_transaction_prevent_mutation
        BEFORE UPDATE OR DELETE ON organization_change_transactions
        FOR EACH ROW EXECUTE FUNCTION organization_change_prevent_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION mutation_prevent_change_range_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.first_change_sequence IS DISTINCT FROM OLD.first_change_sequence
                OR NEW.last_change_sequence IS DISTINCT FROM OLD.last_change_sequence THEN
                RAISE EXCEPTION 'mutation change range is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_mutation_prevent_change_range_mutation
        BEFORE UPDATE OF first_change_sequence, last_change_sequence ON mutations
        FOR EACH ROW EXECUTE FUNCTION mutation_prevent_change_range_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION mutation_verify_organization_change_publication()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            transaction_first bigint;
            transaction_last bigint;
            change_count bigint;
        BEGIN
            IF NEW.is_system_administration_scope
                OR NEW.outcome NOT IN ('accepted', 'partially_superseded') THEN
                RETURN NULL;
            END IF;

            SELECT first_change_sequence, last_change_sequence
            INTO transaction_first, transaction_last
            FROM organization_change_transactions
            WHERE organization_id = NEW.organization_id AND mutation_id = NEW.id;
            SELECT count(*)
            INTO change_count
            FROM organization_changes
            WHERE organization_id = NEW.organization_id AND mutation_id = NEW.id;

            IF transaction_first IS DISTINCT FROM NEW.first_change_sequence
                OR transaction_last IS DISTINCT FROM NEW.last_change_sequence
                OR change_count <> NEW.last_change_sequence - NEW.first_change_sequence + 1 THEN
                RAISE EXCEPTION 'accepted mutation lacks complete change publication';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER tr_mutation_verify_organization_change_publication
        AFTER INSERT OR UPDATE ON mutations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION mutation_verify_organization_change_publication()
        """
    )
    op.create_index("ix_organization_changes_mutation_id", "organization_changes", ["mutation_id"])


def downgrade() -> None:
    op.drop_table("organization_changes")
    op.execute("DROP FUNCTION organization_change_validate_range()")
    op.drop_table("organization_change_transactions")
    op.execute("DROP FUNCTION organization_change_prevent_mutation()")
    op.execute("DROP FUNCTION organization_change_transaction_verify_complete()")
    op.execute("DROP FUNCTION organization_change_transaction_allocate_sequence()")
    op.execute("DROP FUNCTION reserve_organization_change_transaction(uuid, uuid, integer)")
    op.drop_table("organization_change_heads")
    op.execute("DROP FUNCTION organization_change_head_prevent_mutation()")
    op.execute("DROP TRIGGER tr_mutation_verify_organization_change_publication ON mutations")
    op.execute("DROP FUNCTION mutation_verify_organization_change_publication()")
    op.execute("DROP TRIGGER tr_mutation_prevent_change_range_mutation ON mutations")
    op.execute("DROP FUNCTION mutation_prevent_change_range_mutation()")
    op.drop_constraint("uq_mutations_id_organization", "mutations", type_="unique")
