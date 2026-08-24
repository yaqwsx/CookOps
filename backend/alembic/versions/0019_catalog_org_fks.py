"""Restore organization foreign keys omitted from catalog tables."""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_catalog_org_fks"
down_revision: str | None = "0018_event_dietary_exceptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_ingredient_versions_organization",
        "ingredient_versions",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_ingredient_version_tags_organization",
        "ingredient_version_dietary_tags",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_event_dietary_exception_tags_organization",
        "event_dietary_exception_tags",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_event_dietary_exception_tags_organization",
        "event_dietary_exception_tags",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ingredient_version_tags_organization",
        "ingredient_version_dietary_tags",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_ingredient_versions_organization",
        "ingredient_versions",
        type_="foreignkey",
    )
