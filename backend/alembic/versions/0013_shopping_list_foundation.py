"""Add the immutable shopping-generation and mutable fulfilment foundation.

Price snapshots intentionally remain deferred: their authoritative event-owned
source has not been introduced yet.  This revision nevertheless establishes the
stable row, contribution, and generation identities that later price materializers
will populate without changing shopping-list identity or refresh semantics.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_shopping_list_foundation"
down_revision: str | None = "0012_event_archives_field_clocks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shopping_lists",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("current_generation_revision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_shopping_lists_name_not_empty"),
        sa.ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["events.id", "events.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_lists_event_organization",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "organization_id", "event_id", name="uq_shopping_lists_id_org_event"
        ),
    )
    op.create_table(
        "shopping_generation_revisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_list_id", sa.Uuid(), nullable=False),
        sa.Column("parent_revision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("generated_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "parent_revision_id IS NULL OR parent_revision_id <> id",
            name="ck_shopping_generation_revisions_nonrecursive_parent",
        ),
        sa.ForeignKeyConstraint(
            ["shopping_list_id", "organization_id", "event_id"],
            ["shopping_lists.id", "shopping_lists.organization_id", "shopping_lists.event_id"],
            ondelete="RESTRICT",
            name="fk_shopping_generation_revisions_list_scope",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id", "shopping_list_id"],
            ["shopping_generation_revisions.id", "shopping_generation_revisions.shopping_list_id"],
            ondelete="RESTRICT",
            name="fk_shopping_generation_revisions_parent_same_list",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["generated_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "shopping_list_id", name="uq_shopping_generation_revisions_id_list"
        ),
        sa.UniqueConstraint(
            "id",
            "shopping_list_id",
            "organization_id",
            "event_id",
            name="uq_shopping_generation_revisions_id_scope",
        ),
    )
    op.create_foreign_key(
        "fk_shopping_lists_current_generation_revision",
        "shopping_lists",
        "shopping_generation_revisions",
        ["current_generation_revision_id", "id"],
        ["id", "shopping_list_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "shopping_revision_sources",
        sa.Column("generation_revision_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_list_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_recipe_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_revision_id", "shopping_list_id", "organization_id", "event_id"],
            [
                "shopping_generation_revisions.id",
                "shopping_generation_revisions.shopping_list_id",
                "shopping_generation_revisions.organization_id",
                "shopping_generation_revisions.event_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_revision_sources_generation_scope",
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_recipe_id", "event_id", "organization_id"],
            [
                "scheduled_recipes.id",
                "scheduled_recipes.event_id",
                "scheduled_recipes.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_revision_sources_scheduled_recipe_scope",
        ),
        sa.PrimaryKeyConstraint("generation_revision_id", "scheduled_recipe_id"),
    )
    op.create_table(
        "shopping_ingredient_rows",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_list_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_name", sa.String(length=200), nullable=False),
        sa.Column("calculation_unit_id", sa.Uuid(), nullable=False),
        sa.Column(
            "available_supply_quantity", sa.Numeric(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("manual_purchase_target", sa.Numeric(), nullable=True),
        sa.Column("manual_target_automatic_value", sa.Numeric(), nullable=True),
        sa.Column("manual_target_generation_revision_id", sa.Uuid(), nullable=True),
        sa.Column("default_store_section_id", sa.Uuid(), nullable=True),
        sa.Column("default_store_section_name", sa.String(length=200), nullable=True),
        sa.Column("store_section_override_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "aggregate_fulfilment_credit", sa.Numeric(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("aggregate_credit_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("aggregate_credit_updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("aggregate_credit_updated_by_installation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("btrim(ingredient_name) <> ''", name="ck_shopping_rows_ingredient_name"),
        sa.CheckConstraint(
            "available_supply_quantity >= 0 "
            "AND available_supply_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_rows_nonnegative_available_supply",
        ),
        sa.CheckConstraint(
            "manual_purchase_target IS NULL OR (manual_purchase_target >= 0 "
            "AND manual_purchase_target::text NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name="ck_shopping_rows_nonnegative_manual_target",
        ),
        sa.CheckConstraint(
            "manual_target_automatic_value IS NULL OR (manual_target_automatic_value >= 0 "
            "AND manual_target_automatic_value::text NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name="ck_shopping_rows_nonnegative_manual_auto_value",
        ),
        sa.CheckConstraint(
            "aggregate_fulfilment_credit >= 0 "
            "AND aggregate_fulfilment_credit::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_rows_nonnegative_aggregate_credit",
        ),
        sa.CheckConstraint(
            "(manual_purchase_target IS NULL AND manual_target_automatic_value IS NULL "
            "AND manual_target_generation_revision_id IS NULL) OR "
            "(manual_purchase_target IS NOT NULL AND manual_target_automatic_value IS NOT NULL "
            "AND manual_target_generation_revision_id IS NOT NULL)",
            name="ck_shopping_rows_manual_target_basis",
        ),
        sa.CheckConstraint(
            "(default_store_section_id IS NULL AND default_store_section_name IS NULL) OR "
            "(default_store_section_id IS NOT NULL AND btrim(default_store_section_name) <> '')",
            name="ck_shopping_rows_default_section_snapshot",
        ),
        sa.CheckConstraint(
            "(aggregate_credit_updated_at IS NULL AND aggregate_credit_updated_by_user_id IS NULL "
            "AND aggregate_credit_updated_by_installation_id IS NULL) OR "
            "(aggregate_credit_updated_at IS NOT NULL "
            "AND aggregate_credit_updated_by_user_id IS NOT NULL "
            "AND aggregate_credit_updated_by_installation_id IS NOT NULL)",
            name="ck_shopping_rows_aggregate_credit_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["shopping_list_id", "organization_id", "event_id"],
            ["shopping_lists.id", "shopping_lists.organization_id", "shopping_lists.event_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_list_scope",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_ingredient_organization",
        ),
        sa.ForeignKeyConstraint(
            ["calculation_unit_id"], ["unit_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["manual_target_generation_revision_id", "shopping_list_id"],
            ["shopping_generation_revisions.id", "shopping_generation_revisions.shopping_list_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_manual_target_generation",
        ),
        sa.ForeignKeyConstraint(
            ["default_store_section_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_default_section_organization",
        ),
        sa.ForeignKeyConstraint(
            ["store_section_override_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_override_section_organization",
        ),
        sa.ForeignKeyConstraint(
            ["aggregate_credit_updated_by_installation_id", "aggregate_credit_updated_by_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_aggregate_credit_actor",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "shopping_list_id",
            "ingredient_id",
            "organization_id",
            "event_id",
            name="uq_shopping_rows_id_list_ingredient_scope",
        ),
        sa.UniqueConstraint(
            "shopping_list_id", "ingredient_id", name="uq_shopping_rows_list_ingredient"
        ),
    )
    op.create_table(
        "shopping_contributions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_list_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_ingredient_row_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_recipe_id", sa.Uuid(), nullable=False),
        sa.Column("fulfilment_credit", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("fulfilment_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfilment_updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("fulfilment_updated_by_installation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "fulfilment_credit >= 0 "
            "AND fulfilment_credit::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_contributions_nonnegative_credit",
        ),
        sa.CheckConstraint(
            "(fulfilment_updated_at IS NULL AND fulfilment_updated_by_user_id IS NULL "
            "AND fulfilment_updated_by_installation_id IS NULL) OR "
            "(fulfilment_updated_at IS NOT NULL AND fulfilment_updated_by_user_id IS NOT NULL "
            "AND fulfilment_updated_by_installation_id IS NOT NULL)",
            name="ck_shopping_contributions_fulfilment_attribution",
        ),
        sa.ForeignKeyConstraint(
            [
                "shopping_ingredient_row_id",
                "shopping_list_id",
                "ingredient_id",
                "organization_id",
                "event_id",
            ],
            [
                "shopping_ingredient_rows.id",
                "shopping_ingredient_rows.shopping_list_id",
                "shopping_ingredient_rows.ingredient_id",
                "shopping_ingredient_rows.organization_id",
                "shopping_ingredient_rows.event_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_contributions_row_list_ingredient",
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_recipe_id", "event_id", "organization_id"],
            [
                "scheduled_recipes.id",
                "scheduled_recipes.event_id",
                "scheduled_recipes.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_contributions_scheduled_recipe_scope",
        ),
        sa.ForeignKeyConstraint(
            ["fulfilment_updated_by_installation_id", "fulfilment_updated_by_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_shopping_contributions_fulfilment_actor",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shopping_list_id",
            "scheduled_recipe_id",
            "ingredient_id",
            name="uq_shopping_contributions_list_source_ingredient",
        ),
        sa.UniqueConstraint(
            "id",
            "shopping_list_id",
            "ingredient_id",
            "organization_id",
            "event_id",
            name="uq_shopping_contributions_id_list_ingredient_scope",
        ),
    )
    op.create_table(
        "shopping_contribution_snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_list_id", sa.Uuid(), nullable=False),
        sa.Column("generation_revision_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_contribution_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_id", sa.Uuid(), nullable=False),
        sa.Column("active_in_revision", sa.Boolean(), nullable=False),
        sa.Column("generated_quantity", sa.Numeric(), nullable=False),
        sa.Column("ingredient_version_id", sa.Uuid(), nullable=False),
        sa.Column("ingredient_name", sa.String(length=200), nullable=False),
        sa.Column("source_details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "generated_quantity >= 0 "
            "AND generated_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_contribution_snapshots_nonnegative_quantity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_details) = 'object'",
            name="ck_shopping_contribution_snapshots_source_details",
        ),
        sa.ForeignKeyConstraint(
            ["generation_revision_id", "shopping_list_id", "organization_id", "event_id"],
            [
                "shopping_generation_revisions.id",
                "shopping_generation_revisions.shopping_list_id",
                "shopping_generation_revisions.organization_id",
                "shopping_generation_revisions.event_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_contribution_snapshots_generation_scope",
        ),
        sa.ForeignKeyConstraint(
            [
                "shopping_contribution_id",
                "shopping_list_id",
                "ingredient_id",
                "organization_id",
                "event_id",
            ],
            [
                "shopping_contributions.id",
                "shopping_contributions.shopping_list_id",
                "shopping_contributions.ingredient_id",
                "shopping_contributions.organization_id",
                "shopping_contributions.event_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_contribution_snapshots_contribution_scope",
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_version_id", "ingredient_id"],
            ["ingredient_versions.id", "ingredient_versions.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_shopping_contribution_snapshots_ingredient_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "generation_revision_id",
            "shopping_contribution_id",
            name="uq_shopping_contribution_snapshots_generation_contribution",
        ),
    )
    op.create_table(
        "ad_hoc_shopping_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("shopping_list_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("target_amount", sa.Numeric(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("store_section_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("fulfilment_credit", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("fulfilment_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfilment_updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("fulfilment_updated_by_installation_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_ad_hoc_shopping_items_name_not_empty"),
        sa.CheckConstraint(
            "target_amount >= 0 AND target_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_ad_hoc_shopping_items_nonnegative_target",
        ),
        sa.CheckConstraint(
            "fulfilment_credit >= 0 "
            "AND fulfilment_credit::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_ad_hoc_shopping_items_nonnegative_credit",
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_ad_hoc_shopping_items_retirement_attribution",
        ),
        sa.CheckConstraint(
            "(fulfilment_updated_at IS NULL AND fulfilment_updated_by_user_id IS NULL "
            "AND fulfilment_updated_by_installation_id IS NULL) OR "
            "(fulfilment_updated_at IS NOT NULL AND fulfilment_updated_by_user_id IS NOT NULL "
            "AND fulfilment_updated_by_installation_id IS NOT NULL)",
            name="ck_ad_hoc_shopping_items_fulfilment_attribution",
        ),
        sa.ForeignKeyConstraint(
            ["shopping_list_id", "organization_id", "event_id"],
            ["shopping_lists.id", "shopping_lists.organization_id", "shopping_lists.event_id"],
            ondelete="RESTRICT",
            name="fk_ad_hoc_shopping_items_list_scope",
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["unit_definitions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["store_section_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_ad_hoc_shopping_items_section_organization",
        ),
        sa.ForeignKeyConstraint(
            ["fulfilment_updated_by_installation_id", "fulfilment_updated_by_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_ad_hoc_shopping_items_fulfilment_actor",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["retired_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        """
        CREATE FUNCTION shopping_generation_prevent_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'shopping generation records are immutable';
        END;
        $$
        """
    )
    for table in (
        "shopping_generation_revisions",
        "shopping_revision_sources",
        "shopping_contribution_snapshots",
    ):
        op.execute(
            f"""
            CREATE TRIGGER tr_{table}_prevent_mutation
            BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION shopping_generation_prevent_mutation()
            """
        )


def downgrade() -> None:
    for table in (
        "shopping_contribution_snapshots",
        "shopping_revision_sources",
        "shopping_generation_revisions",
    ):
        op.execute(f"DROP TRIGGER tr_{table}_prevent_mutation ON {table}")
    op.execute("DROP FUNCTION shopping_generation_prevent_mutation()")
    op.drop_table("ad_hoc_shopping_items")
    op.drop_table("shopping_contribution_snapshots")
    op.drop_table("shopping_contributions")
    op.drop_table("shopping_ingredient_rows")
    op.drop_table("shopping_revision_sources")
    op.drop_constraint(
        "fk_shopping_lists_current_generation_revision", "shopping_lists", type_="foreignkey"
    )
    op.drop_table("shopping_generation_revisions")
    op.drop_table("shopping_lists")
