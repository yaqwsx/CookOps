"""Capture event prices in immutable shopping revisions.

Existing immutable revisions have no trustworthy historical price source, so their
all-NULL capture tuple explicitly means unavailable rather than a guessed backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_shopping_snapshot_prices"
down_revision: str | None = "0015_event_ingredient_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shopping_contribution_snapshots", sa.Column("event_price_snapshot_id", sa.Uuid())
    )
    op.add_column("shopping_contribution_snapshots", sa.Column("price_amount", sa.Numeric()))
    op.add_column("shopping_contribution_snapshots", sa.Column("priced_quantity", sa.Numeric()))
    op.add_column("shopping_contribution_snapshots", sa.Column("priced_unit_id", sa.Uuid()))
    op.add_column("shopping_contribution_snapshots", sa.Column("currency", sa.String(length=3)))
    op.create_unique_constraint(
        "uq_event_ingredient_price_snapshots_scope",
        "event_ingredient_price_snapshots",
        ["id", "event_id", "organization_id", "ingredient_id"],
    )
    op.create_check_constraint(
        "ck_shopping_contribution_snapshots_price_shape",
        "shopping_contribution_snapshots",
        "(event_price_snapshot_id IS NULL AND price_amount IS NULL "
        "AND priced_quantity IS NULL AND priced_unit_id IS NULL AND currency IS NULL) OR "
        "(event_price_snapshot_id IS NOT NULL AND price_amount IS NOT NULL "
        "AND priced_quantity IS NOT NULL AND priced_unit_id IS NOT NULL AND currency IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_shopping_contribution_snapshots_event_price",
        "shopping_contribution_snapshots",
        "event_ingredient_price_snapshots",
        ["event_price_snapshot_id", "event_id", "organization_id", "ingredient_id"],
        ["id", "event_id", "organization_id", "ingredient_id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE FUNCTION shopping_contribution_snapshot_verify_price() RETURNS trigger AS $$
        DECLARE source event_ingredient_price_snapshots%ROWTYPE;
        BEGIN
            IF NEW.event_price_snapshot_id IS NULL THEN RETURN NEW; END IF;
            SELECT * INTO source FROM event_ingredient_price_snapshots
            WHERE id = NEW.event_price_snapshot_id;
            IF source.state <> 'available' OR NEW.price_amount <> source.price_amount
                OR NEW.priced_quantity <> source.priced_quantity
                OR NEW.priced_unit_id <> source.priced_unit_id
                OR NEW.currency <> source.currency THEN
                RAISE EXCEPTION 'shopping price capture must copy an available event snapshot';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER tr_shopping_contribution_snapshot_verify_price
        BEFORE INSERT ON shopping_contribution_snapshots
        FOR EACH ROW EXECUTE FUNCTION shopping_contribution_snapshot_verify_price();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER tr_shopping_contribution_snapshot_verify_price "
        "ON shopping_contribution_snapshots"
    )
    op.execute("DROP FUNCTION shopping_contribution_snapshot_verify_price()")
    op.drop_constraint(
        "fk_shopping_contribution_snapshots_event_price",
        "shopping_contribution_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_shopping_contribution_snapshots_price_shape",
        "shopping_contribution_snapshots",
        type_="check",
    )
    op.drop_constraint(
        "uq_event_ingredient_price_snapshots_scope",
        "event_ingredient_price_snapshots",
        type_="unique",
    )
    for column in (
        "currency",
        "priced_unit_id",
        "priced_quantity",
        "price_amount",
        "event_price_snapshot_id",
    ):
        op.drop_column("shopping_contribution_snapshots", column)
