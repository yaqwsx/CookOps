"""Add retained, immutable event-local ingredient price streams."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_event_ingredient_prices"
down_revision: str | None = "0014_receipts_media"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_ingredient_prices",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("current_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["events.id", "events.organization_id"],
            ondelete="RESTRICT",
            name="fk_event_ingredient_prices_event_organization",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_event_ingredient_prices_ingredient_organization",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "organization_id", "ingredient_id", name="uq_event_ingredient_prices_scope"
        ),
        sa.UniqueConstraint(
            "id",
            "event_id",
            "organization_id",
            "ingredient_id",
            name="uq_event_ingredient_prices_id_scope",
        ),
    )

    op.create_table(
        "event_ingredient_price_snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("event_ingredient_price_id", sa.Uuid(), nullable=False),
        sa.Column("previous_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("source_ingredient_price_estimate_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("price_amount", sa.Numeric(), nullable=True),
        sa.Column("priced_quantity", sa.Numeric(), nullable=True),
        sa.Column("priced_unit_id", sa.Uuid(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("captured_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("effective_client_action_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("originating_mutation_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "state IN ('available', 'unavailable')",
            name="ck_event_ingredient_price_snapshots_state",
        ),
        sa.CheckConstraint(
            "(state = 'available' AND price_amount IS NOT NULL "
            "AND price_amount >= 0 "
            "AND price_amount::text NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND priced_quantity IS NOT NULL AND priced_quantity > 0 "
            "AND priced_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND priced_unit_id IS NOT NULL "
            "AND currency IS NOT NULL AND currency ~ '^[A-Z]{3}$') OR "
            "(state = 'unavailable' AND price_amount IS NULL "
            "AND priced_quantity IS NULL AND priced_unit_id IS NULL AND currency IS NULL)",
            name="ck_event_ingredient_price_snapshots_value_shape",
        ),
        sa.CheckConstraint(
            "previous_snapshot_id IS NULL OR previous_snapshot_id <> id",
            name="ck_event_ingredient_price_snapshots_nonrecursive_previous",
        ),
        sa.ForeignKeyConstraint(
            ["event_ingredient_price_id", "event_id", "organization_id", "ingredient_id"],
            [
                "event_ingredient_prices.id",
                "event_ingredient_prices.event_id",
                "event_ingredient_prices.organization_id",
                "event_ingredient_prices.ingredient_id",
            ],
            ondelete="RESTRICT",
            name="fk_event_ingredient_price_snapshots_price_scope",
        ),
        sa.ForeignKeyConstraint(
            ["previous_snapshot_id", "event_ingredient_price_id"],
            [
                "event_ingredient_price_snapshots.id",
                "event_ingredient_price_snapshots.event_ingredient_price_id",
            ],
            ondelete="RESTRICT",
            name="fk_event_ingredient_price_snapshots_previous_same_stream",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["source_ingredient_price_estimate_id", "ingredient_id"],
            ["ingredient_price_estimates.id", "ingredient_price_estimates.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_event_ingredient_price_snapshots_source_ingredient_price",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "organization_id", "currency"],
            ["events.id", "events.organization_id", "events.currency"],
            ondelete="RESTRICT",
            name="fk_event_ingredient_price_snapshots_event_currency",
        ),
        sa.ForeignKeyConstraint(["priced_unit_id"], ["unit_definitions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["captured_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id", "originating_mutation_id"],
            ["mutations.organization_id", "mutations.id"],
            ondelete="RESTRICT",
            name="fk_event_ingredient_price_snapshots_mutation",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "event_ingredient_price_id", name="uq_event_ingredient_price_snapshots_id_price"
        ),
    )
    op.create_index(
        "uq_event_ingredient_price_snapshots_first",
        "event_ingredient_price_snapshots",
        ["event_ingredient_price_id"],
        unique=True,
        postgresql_where=sa.text("previous_snapshot_id IS NULL"),
    )
    op.create_index(
        "uq_event_ingredient_price_snapshots_predecessor",
        "event_ingredient_price_snapshots",
        ["event_ingredient_price_id", "previous_snapshot_id"],
        unique=True,
        postgresql_where=sa.text("previous_snapshot_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_event_ingredient_prices_current_snapshot",
        "event_ingredient_prices",
        "event_ingredient_price_snapshots",
        ["current_snapshot_id", "id"],
        ["id", "event_ingredient_price_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )

    op.execute(
        """
        CREATE FUNCTION event_ingredient_price_prevent_identity_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'event ingredient prices are retained';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.current_snapshot_id IS NOT NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM events
                        WHERE id = NEW.event_id AND lifecycle = 'active'
                    ) THEN
                    RAISE EXCEPTION 'event ingredient price requires active event';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
                OR NEW.event_id IS DISTINCT FROM OLD.event_id
                OR NEW.ingredient_id IS DISTINCT FROM OLD.ingredient_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id THEN
                RAISE EXCEPTION 'event ingredient price identity is immutable';
            END IF;
            IF NEW.current_snapshot_id IS DISTINCT FROM OLD.current_snapshot_id THEN
                IF NOT EXISTS (
                    SELECT 1 FROM events
                    WHERE id = NEW.event_id AND lifecycle = 'active'
                ) OR NOT EXISTS (
                    SELECT 1 FROM event_ingredient_price_snapshots
                    WHERE id = NEW.current_snapshot_id
                      AND event_ingredient_price_id = NEW.id
                      AND previous_snapshot_id IS NOT DISTINCT FROM OLD.current_snapshot_id
                ) THEN
                    RAISE EXCEPTION 'event price pointer must advance one snapshot on active event';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_event_ingredient_price_prevent_identity_mutation
        BEFORE INSERT OR UPDATE OR DELETE ON event_ingredient_prices
        FOR EACH ROW EXECUTE FUNCTION event_ingredient_price_prevent_identity_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION event_ingredient_price_snapshot_prevent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'event ingredient price snapshots are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_event_ingredient_price_snapshot_prevent_mutation
        BEFORE UPDATE OR DELETE OR TRUNCATE ON event_ingredient_price_snapshots
        FOR EACH STATEMENT EXECUTE FUNCTION event_ingredient_price_snapshot_prevent_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION event_ingredient_price_snapshot_require_active_event()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM events
                WHERE id = NEW.event_id AND lifecycle = 'active'
            ) THEN
                RAISE EXCEPTION 'event ingredient prices require an active event';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM event_ingredient_prices
                WHERE id = NEW.event_ingredient_price_id
                  AND current_snapshot_id IS NOT DISTINCT FROM NEW.previous_snapshot_id
            ) THEN
                RAISE EXCEPTION 'event price snapshot must append to current pointer';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER tr_event_ingredient_price_snapshot_require_active_event
        BEFORE INSERT ON event_ingredient_price_snapshots
        FOR EACH ROW EXECUTE FUNCTION event_ingredient_price_snapshot_require_active_event()
        """
    )
    op.execute(
        """
        CREATE FUNCTION event_ingredient_price_snapshot_verify_capture()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            event_currency text;
            mutation_actor_id uuid;
            mutation_client_time timestamptz;
            mutation_server_time timestamptz;
            source_state text;
            source_price_amount numeric;
            source_priced_quantity numeric;
            source_priced_unit_id uuid;
            source_currency text;
        BEGIN
            SELECT currency INTO event_currency
            FROM events WHERE id = NEW.event_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'event ingredient prices require an active event';
            END IF;
            SELECT actor_user_id, client_wall_time, server_received_at
            INTO mutation_actor_id, mutation_client_time, mutation_server_time
            FROM mutations
            WHERE id = NEW.originating_mutation_id
              AND organization_id = NEW.organization_id
              AND outcome = 'accepted'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(target_identities) AS target(identity)
                  WHERE target.identity->>'entity_kind' = 'event'
                    AND target.identity->>'entity_id' = NEW.event_id::text
              );
            IF NOT FOUND
                OR NEW.captured_by_user_id <> mutation_actor_id
                OR NEW.effective_client_action_time <> mutation_client_time
                OR NEW.server_received_at <> mutation_server_time THEN
                RAISE EXCEPTION 'event price snapshot capture must match accepted event mutation';
            END IF;

            IF NEW.source_ingredient_price_estimate_id IS NULL THEN
                IF NEW.state <> 'unavailable' THEN
                    RAISE EXCEPTION 'available event price snapshots require a source estimate';
                END IF;
                RETURN NULL;
            END IF;

            SELECT state, price_amount, priced_quantity, priced_unit_id, currency
            INTO source_state, source_price_amount, source_priced_quantity,
                source_priced_unit_id, source_currency
            FROM ingredient_price_estimates
            WHERE id = NEW.source_ingredient_price_estimate_id
              AND ingredient_id = NEW.ingredient_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'event price snapshot source estimate is missing';
            END IF;
            IF source_state = 'unavailable' THEN
                IF NEW.state <> 'unavailable' THEN
                    RAISE EXCEPTION 'unavailable source requires unavailable event snapshot';
                END IF;
                RETURN NULL;
            END IF;
            IF source_currency <> event_currency THEN
                IF NEW.state <> 'unavailable' THEN
                    RAISE EXCEPTION 'foreign-currency source requires unavailable event snapshot';
                END IF;
                RETURN NULL;
            END IF;
            IF NEW.state <> 'available'
                OR NEW.price_amount <> source_price_amount
                OR NEW.priced_quantity <> source_priced_quantity
                OR NEW.priced_unit_id <> source_priced_unit_id
                OR NEW.currency <> source_currency THEN
                RAISE EXCEPTION 'available event snapshot must copy its compatible source estimate';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER tr_event_ingredient_price_snapshot_verify_capture
        AFTER INSERT ON event_ingredient_price_snapshots
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION event_ingredient_price_snapshot_verify_capture()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER tr_event_ingredient_price_snapshot_verify_capture "
        "ON event_ingredient_price_snapshots"
    )
    op.execute("DROP FUNCTION event_ingredient_price_snapshot_verify_capture()")
    op.execute(
        "DROP TRIGGER tr_event_ingredient_price_snapshot_prevent_mutation "
        "ON event_ingredient_price_snapshots"
    )
    op.execute("DROP FUNCTION event_ingredient_price_snapshot_prevent_mutation()")
    op.execute(
        "DROP TRIGGER tr_event_ingredient_price_snapshot_require_active_event "
        "ON event_ingredient_price_snapshots"
    )
    op.execute("DROP FUNCTION event_ingredient_price_snapshot_require_active_event()")
    op.execute(
        "DROP TRIGGER tr_event_ingredient_price_prevent_identity_mutation "
        "ON event_ingredient_prices"
    )
    op.execute("DROP FUNCTION event_ingredient_price_prevent_identity_mutation()")
    op.drop_constraint(
        "fk_event_ingredient_prices_current_snapshot",
        "event_ingredient_prices",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_event_ingredient_price_snapshots_predecessor",
        table_name="event_ingredient_price_snapshots",
    )
    op.drop_index(
        "uq_event_ingredient_price_snapshots_first",
        table_name="event_ingredient_price_snapshots",
    )
    op.drop_table("event_ingredient_price_snapshots")
    op.drop_table("event_ingredient_prices")
