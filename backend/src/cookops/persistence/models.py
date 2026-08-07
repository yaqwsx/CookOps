from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "normalized_email <> '' AND normalized_email = lower(btrim(verified_email))",
            name="ck_users_normalized_email",
        ),
        CheckConstraint(
            "(disabled_at IS NULL AND disabled_by_user_id IS NULL) OR "
            "(disabled_at IS NOT NULL AND disabled_by_user_id IS NOT NULL)",
            name="ck_users_disabled_attribution",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    display_name: Mapped[str] = mapped_column(String(200))
    verified_email: Mapped[str] = mapped_column(String(320))
    normalized_email: Mapped[str] = mapped_column(String(320), unique=True)
    preferred_locale: Mapped[str | None] = mapped_column(String(35))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    last_successful_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    __table_args__ = (
        CheckConstraint("provider IN ('google', 'dummy')", name="ck_external_identities_provider"),
        CheckConstraint(
            "normalized_verified_email <> '' "
            "AND normalized_verified_email = lower(btrim(verified_email))",
            name="ck_external_identities_normalized_email",
        ),
        Index(
            "uq_external_identities_provider_subject",
            "provider",
            "provider_subject",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(16))
    provider_subject: Mapped[str] = mapped_column(String(255))
    verified_email: Mapped[str] = mapped_column(String(320))
    normalized_verified_email: Mapped[str] = mapped_column(String(320))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    __table_args__ = (
        CheckConstraint(
            "octet_length(secret_hmac) = 32",
            name="ck_browser_sessions_secret_hmac",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_browser_sessions_expiry",
        ),
        CheckConstraint(
            "last_used_at IS NULL OR (last_used_at >= created_at AND last_used_at < expires_at)",
            name="ck_browser_sessions_last_used_at",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL "
            "AND revoked_at >= created_at "
            "AND (last_used_at IS NULL OR last_used_at <= revoked_at))",
            name="ck_browser_sessions_revocation_attribution",
        ),
        UniqueConstraint("secret_hmac", name="uq_browser_sessions_secret_hmac"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    secret_hmac: Mapped[bytes] = mapped_column(LargeBinary(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("name <> ''", name="ck_organizations_name_not_empty"),
        CheckConstraint(
            "default_currency ~ '^[A-Z]{3}$'", name="ck_organizations_default_currency"
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_organizations_retirement_attribution",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    default_currency: Mapped[str] = mapped_column(
        String(3), default="CZK", server_default=text("'CZK'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('member', 'organization_admin')", name="ck_memberships_role"),
        CheckConstraint("state IN ('invited', 'active', 'removed')", name="ck_memberships_state"),
        CheckConstraint(
            "invited_email <> '' AND invited_email = lower(btrim(invited_email))",
            name="ck_memberships_invited_email",
        ),
        CheckConstraint(
            "(user_id IS NULL AND claimed_at IS NULL) OR "
            "(user_id IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_memberships_claim_attribution",
        ),
        CheckConstraint(
            "(state = 'invited' AND user_id IS NULL AND removed_at IS NULL "
            "AND removed_by_user_id IS NULL) OR "
            "(state = 'active' AND user_id IS NOT NULL AND removed_at IS NULL "
            "AND removed_by_user_id IS NULL) OR "
            "(state = 'removed' AND removed_at IS NOT NULL AND removed_by_user_id IS NOT NULL)",
            name="ck_memberships_lifecycle",
        ),
        Index(
            "uq_memberships_active_user",
            "organization_id",
            "user_id",
            unique=True,
            postgresql_where=text("state = 'active' AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_memberships_open_invited_email",
            "organization_id",
            "invited_email",
            unique=True,
            postgresql_where=text("state IN ('invited', 'active')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    invited_email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(32), default="member", server_default=text("'member'"))
    state: Mapped[str] = mapped_column(
        String(16), default="invited", server_default=text("'invited'")
    )
    invited_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class SystemRoleAssignment(Base):
    __tablename__ = "system_role_assignments"
    __table_args__ = (
        CheckConstraint("role = 'system_admin'", name="ck_system_role_assignments_role"),
        CheckConstraint(
            "invited_email <> '' AND invited_email = lower(btrim(invited_email))",
            name="ck_system_role_assignments_invited_email",
        ),
        CheckConstraint(
            "(user_id IS NULL AND claimed_at IS NULL) OR "
            "(user_id IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_system_role_assignments_claim_attribution",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="ck_system_role_assignments_revocation_attribution",
        ),
        Index(
            "uq_system_role_assignments_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_system_role_assignments_active_invited_email",
            "invited_email",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    invited_email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(
        String(32), default="system_admin", server_default=text("'system_admin'")
    )
    granted_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class StoreSection(Base):
    __tablename__ = "store_sections"
    __table_args__ = (
        CheckConstraint(
            "btrim(name) <> '' AND normalized_name = lower(btrim(name))",
            name="ck_store_sections_normalized_name",
        ),
        CheckConstraint("position_key ~ '^[0-9A-Za-z]+$'", name="ck_store_sections_position_key"),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_store_sections_retirement_attribution",
        ),
        UniqueConstraint(
            "organization_id",
            "normalized_name",
            name="uq_store_sections_organization_name",
        ),
        UniqueConstraint("id", "organization_id", name="uq_store_sections_id_organization"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    position_key: Mapped[str] = mapped_column(String(255, collation="C"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class OrganizationMealRolePreset(Base):
    __tablename__ = "organization_meal_role_presets"
    __table_args__ = (
        CheckConstraint(
            "(built_in_translation_key IS NOT NULL "
            "AND built_in_translation_key ~ '^[a-z][a-z0-9_.-]*$' "
            "AND custom_name IS NULL AND normalized_custom_name IS NULL) OR "
            "(built_in_translation_key IS NULL AND custom_name IS NOT NULL "
            "AND btrim(custom_name) <> '' "
            "AND normalized_custom_name IS NOT NULL "
            "AND normalized_custom_name = lower(btrim(custom_name)))",
            name="ck_meal_role_presets_display_identity",
        ),
        CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_meal_role_presets_position_key"
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_meal_role_presets_retirement_attribution",
        ),
        UniqueConstraint(
            "organization_id",
            "built_in_translation_key",
            name="uq_meal_role_presets_builtin_key",
        ),
        UniqueConstraint(
            "organization_id",
            "normalized_custom_name",
            name="uq_meal_role_presets_custom_name",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    built_in_translation_key: Mapped[str | None] = mapped_column(String(100))
    custom_name: Mapped[str | None] = mapped_column(String(200))
    normalized_custom_name: Mapped[str | None] = mapped_column(String(200))
    position_key: Mapped[str] = mapped_column(String(255, collation="C"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class RecipeTag(Base):
    __tablename__ = "recipe_tags"
    __table_args__ = (
        CheckConstraint(
            "btrim(name) <> '' AND normalized_name = lower(btrim(name))",
            name="ck_recipe_tags_normalized_name",
        ),
        CheckConstraint("color ~ '^#[0-9A-Fa-f]{6}$'", name="ck_recipe_tags_color"),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_recipe_tags_retirement_attribution",
        ),
        UniqueConstraint(
            "organization_id",
            "normalized_name",
            name="uq_recipe_tags_organization_name",
        ),
        UniqueConstraint("id", "organization_id", name="uq_recipe_tags_id_organization"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    color: Mapped[str] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class DietaryTag(Base):
    __tablename__ = "dietary_tags"
    __table_args__ = (
        CheckConstraint(
            "seed_key IS NULL OR seed_key IN ('vegetarian', 'vegan', 'gluten', 'lactose')",
            name="ck_dietary_tags_seed_key",
        ),
        CheckConstraint(
            "(name IS NULL AND normalized_name IS NULL AND seed_key IS NOT NULL) OR "
            "(name IS NOT NULL AND btrim(name) <> '' "
            "AND normalized_name IS NOT NULL "
            "AND normalized_name = lower(btrim(name)))",
            name="ck_dietary_tags_display_identity",
        ),
        CheckConstraint(
            "color IS NULL OR color ~ '^#[0-9A-Fa-f]{6}$'", name="ck_dietary_tags_color"
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_dietary_tags_retirement_attribution",
        ),
        UniqueConstraint(
            "organization_id",
            "seed_key",
            name="uq_dietary_tags_seed_key",
        ),
        UniqueConstraint(
            "organization_id",
            "normalized_name",
            name="uq_dietary_tags_organization_name",
        ),
        UniqueConstraint("id", "organization_id", name="uq_dietary_tags_id_organization"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    seed_key: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200))
    normalized_name: Mapped[str | None] = mapped_column(String(200))
    color: Mapped[str | None] = mapped_column(String(7))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class UnitDefinition(Base):
    __tablename__ = "unit_definitions"
    __table_args__ = (
        CheckConstraint(
            "(organization_id IS NULL AND custom_name IS NULL "
            "AND normalized_custom_name IS NULL AND created_by_user_id IS NULL "
            "AND retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(organization_id IS NOT NULL AND custom_name IS NOT NULL "
            "AND btrim(custom_name) <> '' AND normalized_custom_name IS NOT NULL "
            "AND normalized_custom_name = lower(btrim(custom_name)) "
            "AND created_by_user_id IS NOT NULL)",
            name="ck_unit_definitions_scope_and_display",
        ),
        CheckConstraint("code ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_unit_definitions_code"),
        CheckConstraint(
            "dimension IN ('mass', 'volume', 'count', 'custom')",
            name="ck_unit_definitions_dimension",
        ),
        CheckConstraint(
            "(dimension IN ('mass', 'volume') "
            "AND base_unit_factor IS NOT NULL AND base_unit_factor > 0 "
            "AND base_unit_factor::text NOT IN ('NaN', 'Infinity', '-Infinity')) OR "
            "(dimension IN ('count', 'custom') AND base_unit_factor IS NULL)",
            name="ck_unit_definitions_base_factor",
        ),
        CheckConstraint(
            "allows_ingredient_quantity OR allows_recipe_scaling",
            name="ck_unit_definitions_permitted_context",
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_unit_definitions_retirement_attribution",
        ),
        UniqueConstraint("id", "organization_id", name="uq_unit_definitions_id_organization"),
        Index(
            "uq_unit_definitions_system_code",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
        Index(
            "uq_unit_definitions_organization_code",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
        Index(
            "uq_unit_definitions_organization_name",
            "organization_id",
            "normalized_custom_name",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    code: Mapped[str] = mapped_column(String(100))
    custom_name: Mapped[str | None] = mapped_column(String(200))
    normalized_custom_name: Mapped[str | None] = mapped_column(String(200))
    dimension: Mapped[str] = mapped_column(String(16))
    base_unit_factor: Mapped[Decimal | None] = mapped_column(Numeric)
    rounds_up_to_whole_unit: Mapped[bool] = mapped_column(Boolean)
    allows_ingredient_quantity: Mapped[bool] = mapped_column(Boolean)
    allows_recipe_scaling: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class Ingredient(Base):
    __tablename__ = "ingredients"
    __table_args__ = (
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_ingredients_retirement_attribution",
        ),
        ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["ingredient_versions.id", "ingredient_versions.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_ingredients_current_version",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["current_price_estimate_id", "id"],
            ["ingredient_price_estimates.id", "ingredient_price_estimates.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_ingredients_current_price_estimate",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint("id", "organization_id", name="uq_ingredients_id_organization"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    current_price_estimate_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class IngredientVersion(Base):
    __tablename__ = "ingredient_versions"
    __table_args__ = (
        CheckConstraint(
            "btrim(name) <> '' AND normalized_name = lower(btrim(name))",
            name="ck_ingredient_versions_normalized_name",
        ),
        CheckConstraint(
            "mass_per_canonical_quantity > 0 "
            "AND mass_per_canonical_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_ingredient_versions_positive_mass_conversion",
        ),
        CheckConstraint(
            "based_on_version_id IS NULL OR based_on_version_id <> id",
            name="ck_ingredient_versions_nonrecursive_base",
        ),
        ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_versions_ingredient_organization",
        ),
        ForeignKeyConstraint(
            ["based_on_version_id", "ingredient_id"],
            ["ingredient_versions.id", "ingredient_versions.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_versions_based_on_same_ingredient",
        ),
        ForeignKeyConstraint(
            ["default_store_section_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_versions_store_section_organization",
        ),
        UniqueConstraint("id", "organization_id", name="uq_ingredient_versions_id_organization"),
        UniqueConstraint("id", "ingredient_id", name="uq_ingredient_versions_id_ingredient"),
        Index("ix_ingredient_versions_ingredient_id", "ingredient_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_id: Mapped[UUID] = mapped_column(Uuid)
    based_on_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    canonical_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    mass_per_canonical_quantity: Mapped[Decimal] = mapped_column(Numeric)
    default_store_section_id: Mapped[UUID | None] = mapped_column(Uuid)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    published_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class IngredientVersionDietaryTag(Base):
    __tablename__ = "ingredient_version_dietary_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["ingredient_version_id", "organization_id"],
            ["ingredient_versions.id", "ingredient_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_version_tags_version_organization",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["dietary_tag_id", "organization_id"],
            ["dietary_tags.id", "dietary_tags.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_version_tags_tag_organization",
        ),
    )

    ingredient_version_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    dietary_tag_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid)


class IngredientPriceEstimate(Base):
    __tablename__ = "ingredient_price_estimates"
    __table_args__ = (
        CheckConstraint(
            "state IN ('available', 'unavailable')",
            name="ck_ingredient_price_estimates_state",
        ),
        CheckConstraint(
            "(state = 'available' AND price_amount IS NOT NULL "
            "AND price_amount >= 0 "
            "AND price_amount::text NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND priced_quantity IS NOT NULL AND priced_quantity > 0 "
            "AND priced_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND priced_unit_id IS NOT NULL "
            "AND currency IS NOT NULL AND currency ~ '^[A-Z]{3}$') OR "
            "(state = 'unavailable' AND price_amount IS NULL "
            "AND priced_quantity IS NULL AND priced_unit_id IS NULL AND currency IS NULL)",
            name="ck_ingredient_price_estimates_value_shape",
        ),
        CheckConstraint(
            "based_on_estimate_id IS NULL OR based_on_estimate_id <> id",
            name="ck_ingredient_price_estimates_nonrecursive_base",
        ),
        ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_price_estimates_ingredient_organization",
        ),
        ForeignKeyConstraint(
            ["based_on_estimate_id", "ingredient_id"],
            ["ingredient_price_estimates.id", "ingredient_price_estimates.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_ingredient_price_estimates_based_on_same_ingredient",
        ),
        UniqueConstraint("id", "ingredient_id", name="uq_ingredient_price_estimates_id_ingredient"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_id: Mapped[UUID] = mapped_column(Uuid)
    based_on_estimate_id: Mapped[UUID | None] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(16))
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    priced_quantity: Mapped[Decimal | None] = mapped_column(Numeric)
    priced_unit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    currency: Mapped[str | None] = mapped_column(String(3))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    published_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class Recipe(Base):
    __tablename__ = "recipes"
    __table_args__ = (
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_recipes_retirement_attribution",
        ),
        ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["recipe_versions.id", "recipe_versions.recipe_id"],
            ondelete="RESTRICT",
            name="fk_recipes_current_version",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint("id", "organization_id", name="uq_recipes_id_organization"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class RecipeVersion(Base):
    __tablename__ = "recipe_versions"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_recipe_versions_name_not_empty"),
        CheckConstraint(
            "scaling_model = 'single_variable'", name="ck_recipe_versions_scaling_model"
        ),
        CheckConstraint(
            "base_scaling_amount > 0 "
            "AND base_scaling_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_recipe_versions_positive_base_scaling_amount",
        ),
        CheckConstraint(
            "estimated_diners_per_scaling_unit IS NULL OR "
            "(estimated_diners_per_scaling_unit > 0 "
            "AND estimated_diners_per_scaling_unit::text NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name="ck_recipe_versions_positive_estimated_diners",
        ),
        CheckConstraint(
            "based_on_version_id IS NULL OR based_on_version_id <> id",
            name="ck_recipe_versions_nonrecursive_base",
        ),
        ForeignKeyConstraint(
            ["recipe_id", "organization_id"],
            ["recipes.id", "recipes.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_versions_recipe_organization",
        ),
        ForeignKeyConstraint(
            ["based_on_version_id", "recipe_id"],
            ["recipe_versions.id", "recipe_versions.recipe_id"],
            ondelete="RESTRICT",
            name="fk_recipe_versions_based_on_same_recipe",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("id", "organization_id", name="uq_recipe_versions_id_organization"),
        UniqueConstraint("id", "recipe_id", name="uq_recipe_versions_id_recipe"),
        Index("ix_recipe_versions_recipe_id", "recipe_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    recipe_id: Mapped[UUID] = mapped_column(Uuid)
    based_on_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    scaling_model: Mapped[str] = mapped_column(
        String(32), default="single_variable", server_default=text("'single_variable'")
    )
    scaling_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    base_scaling_amount: Mapped[Decimal] = mapped_column(Numeric)
    estimated_diners_per_scaling_unit: Mapped[Decimal | None] = mapped_column(Numeric)
    round_suggestions_up: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    published_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class RecipeVersionTag(Base):
    __tablename__ = "recipe_version_tags"
    __table_args__ = (
        ForeignKeyConstraint(
            ["recipe_version_id", "organization_id"],
            ["recipe_versions.id", "recipe_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_tags_version_organization",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["recipe_tag_id", "organization_id"],
            ["recipe_tags.id", "recipe_tags.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_tags_tag_organization",
        ),
    )

    recipe_version_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    recipe_tag_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid)


class RecipeVersionIngredientLine(Base):
    __tablename__ = "recipe_version_ingredient_lines"
    __table_args__ = (
        CheckConstraint(
            "base_quantity >= 0 AND base_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_recipe_version_lines_nonnegative_base_quantity",
        ),
        CheckConstraint(
            "scaling_behavior IN ('proportional', 'fixed')",
            name="ck_recipe_version_lines_scaling_behavior",
        ),
        CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_recipe_version_lines_position_key"
        ),
        ForeignKeyConstraint(
            ["recipe_id", "organization_id"],
            ["recipes.id", "recipes.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_recipe_organization",
        ),
        ForeignKeyConstraint(
            ["recipe_version_id", "organization_id"],
            ["recipe_versions.id", "recipe_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_version_organization",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["recipe_version_id", "recipe_id"],
            ["recipe_versions.id", "recipe_versions.recipe_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_version_recipe",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["ingredient_version_id", "organization_id"],
            ["ingredient_versions.id", "ingredient_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_recipe_version_lines_ingredient_version_organization",
        ),
        UniqueConstraint(
            "recipe_version_id", "line_key", name="uq_recipe_version_lines_version_line_key"
        ),
        Index("ix_recipe_version_lines_recipe_id_line_key", "recipe_id", "line_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    recipe_id: Mapped[UUID] = mapped_column(Uuid)
    recipe_version_id: Mapped[UUID] = mapped_column(Uuid)
    line_key: Mapped[UUID] = mapped_column(Uuid)
    ingredient_version_id: Mapped[UUID] = mapped_column(Uuid)
    base_quantity: Mapped[Decimal] = mapped_column(Numeric)
    preferred_display_unit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    note: Mapped[str | None] = mapped_column(Text)
    position_key: Mapped[str] = mapped_column(String(255, collation="C"))
    scaling_behavior: Mapped[str] = mapped_column(
        String(16), default="proportional", server_default=text("'proportional'")
    )
    include_in_portion_weight: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_events_name_not_empty"),
        CheckConstraint("end_date >= start_date", name="ck_events_date_range"),
        CheckConstraint(
            "base_expected_attendance >= 0", name="ck_events_nonnegative_base_attendance"
        ),
        CheckConstraint(
            "budget_amount >= 0 AND budget_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_events_nonnegative_budget",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_events_currency"),
        CheckConstraint("lifecycle IN ('active', 'archived')", name="ck_events_lifecycle"),
        CheckConstraint(
            "(lifecycle = 'active' AND current_archive_snapshot_id IS NULL "
            "AND archived_at IS NULL AND archived_by_user_id IS NULL) OR "
            "(lifecycle = 'archived' AND current_archive_snapshot_id IS NOT NULL "
            "AND archived_at IS NOT NULL AND archived_by_user_id IS NOT NULL "
            "AND archived_at >= created_at)",
            name="ck_events_archive_lifecycle_attribution",
        ),
        ForeignKeyConstraint(
            ["current_archive_snapshot_id", "id"],
            ["event_archive_snapshots.id", "event_archive_snapshots.event_id"],
            ondelete="RESTRICT",
            name="fk_events_current_archive_snapshot",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("id", "organization_id", name="uq_events_id_organization"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[date] = mapped_column()
    end_date: Mapped[date] = mapped_column()
    location: Mapped[str | None] = mapped_column(String(300))
    general_note: Mapped[str | None] = mapped_column(Text)
    base_expected_attendance: Mapped[int] = mapped_column()
    budget_amount: Mapped[Decimal] = mapped_column(Numeric)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    lifecycle: Mapped[str] = mapped_column(String(16), server_default=text("'active'"))
    current_archive_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class EventArchiveSnapshot(Base):
    __tablename__ = "event_archive_snapshots"
    __table_args__ = (
        CheckConstraint("archive_schema_version > 0", name="ck_event_archive_snapshots_schema"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_event_archive_snapshots_payload"
        ),
        CheckConstraint(
            "jsonb_typeof(attachment_manifest) = 'array'",
            name="ck_event_archive_snapshots_attachment_manifest",
        ),
        CheckConstraint(
            "octet_length(content_hash) = 32", name="ck_event_archive_snapshots_content_hash"
        ),
        ForeignKeyConstraint(
            ["previous_snapshot_id", "event_id"],
            ["event_archive_snapshots.id", "event_archive_snapshots.event_id"],
            ondelete="RESTRICT",
            name="fk_event_archive_snapshots_previous_snapshot",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("id", "event_id", name="uq_event_archive_snapshots_id_event"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    previous_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid)
    archive_schema_version: Mapped[int] = mapped_column(SmallInteger)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    attachment_manifest: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class EventDay(Base):
    __tablename__ = "event_days"
    __table_args__ = (
        CheckConstraint(
            "provenance IN ('range_generated', 'manually_added')",
            name="ck_event_days_provenance",
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_event_days_retirement_attribution",
        ),
        Index(
            "uq_event_days_active_event_date",
            "event_id",
            "calendar_date",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
        UniqueConstraint("id", "event_id", name="uq_event_days_id_event"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    calendar_date: Mapped[date] = mapped_column()
    note: Mapped[str | None] = mapped_column(Text)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    provenance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class EventMealRole(Base):
    __tablename__ = "event_meal_roles"
    __table_args__ = (
        CheckConstraint(
            "(built_in_translation_key IS NOT NULL "
            "AND built_in_translation_key ~ '^[a-z][a-z0-9_.-]*$' "
            "AND custom_name IS NULL AND normalized_custom_name IS NULL) OR "
            "(built_in_translation_key IS NULL AND custom_name IS NOT NULL "
            "AND btrim(custom_name) <> '' "
            "AND normalized_custom_name IS NOT NULL "
            "AND normalized_custom_name = lower(btrim(custom_name)))",
            name="ck_event_meal_roles_display_identity",
        ),
        CheckConstraint("position_key ~ '^[0-9A-Za-z]+$'", name="ck_event_meal_roles_position_key"),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_event_meal_roles_retirement_attribution",
        ),
        UniqueConstraint(
            "event_id", "built_in_translation_key", name="uq_event_meal_roles_builtin_key"
        ),
        UniqueConstraint(
            "event_id", "normalized_custom_name", name="uq_event_meal_roles_custom_name"
        ),
        UniqueConstraint("id", "event_id", name="uq_event_meal_roles_id_event"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    source_preset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization_meal_role_presets.id", ondelete="RESTRICT")
    )
    built_in_translation_key: Mapped[str | None] = mapped_column(String(100))
    custom_name: Mapped[str | None] = mapped_column(String(200))
    normalized_custom_name: Mapped[str | None] = mapped_column(String(200))
    position_key: Mapped[str] = mapped_column(String(255, collation="C"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class ScheduledRecipe(Base):
    __tablename__ = "scheduled_recipes"
    __table_args__ = (
        CheckConstraint("diner_count >= 0", name="ck_scheduled_recipes_nonnegative_diners"),
        CheckConstraint(
            "attendance_mode IN ('follows_event', 'manual')",
            name="ck_scheduled_recipes_attendance_mode",
        ),
        CheckConstraint(
            "consumption_percentage >= 0 "
            "AND consumption_percentage::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_scheduled_recipes_nonnegative_consumption_percentage",
        ),
        CheckConstraint(
            "selected_scale_amount >= 0 "
            "AND selected_scale_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_scheduled_recipes_nonnegative_selected_scale",
        ),
        CheckConstraint(
            "scale_mode IN ('suggested', 'manual')", name="ck_scheduled_recipes_scale_mode"
        ),
        CheckConstraint(
            "position_key ~ '^[0-9A-Za-z]+$'", name="ck_scheduled_recipes_position_key"
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_scheduled_recipes_retirement_attribution",
        ),
        ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["events.id", "events.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_event_organization",
        ),
        ForeignKeyConstraint(
            ["event_day_id", "event_id"],
            ["event_days.id", "event_days.event_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_day_event",
        ),
        ForeignKeyConstraint(
            ["event_meal_role_id", "event_id"],
            ["event_meal_roles.id", "event_meal_roles.event_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_role_event",
        ),
        ForeignKeyConstraint(
            ["recipe_id", "organization_id"],
            ["recipes.id", "recipes.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_recipe_organization",
        ),
        ForeignKeyConstraint(
            ["recipe_version_id", "organization_id"],
            ["recipe_versions.id", "recipe_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_recipe_version_organization",
        ),
        ForeignKeyConstraint(
            ["recipe_version_id", "recipe_id"],
            ["recipe_versions.id", "recipe_versions.recipe_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_recipes_recipe_version_recipe",
        ),
        UniqueConstraint(
            "id", "event_id", "organization_id", name="uq_scheduled_recipes_id_event_organization"
        ),
        Index(
            "ix_scheduled_recipes_event_day_role_position",
            "event_id",
            "event_day_id",
            "event_meal_role_id",
            "position_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    event_day_id: Mapped[UUID] = mapped_column(Uuid)
    event_meal_role_id: Mapped[UUID] = mapped_column(Uuid)
    recipe_id: Mapped[UUID] = mapped_column(Uuid)
    recipe_version_id: Mapped[UUID] = mapped_column(Uuid)
    diner_count: Mapped[int] = mapped_column()
    attendance_mode: Mapped[str] = mapped_column(String(16))
    consumption_percentage: Mapped[Decimal] = mapped_column(Numeric)
    selected_scale_amount: Mapped[Decimal] = mapped_column(Numeric)
    scale_mode: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    position_key: Mapped[str] = mapped_column(String(255, collation="C"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class ScheduledIngredientOverride(Base):
    __tablename__ = "scheduled_ingredient_overrides"
    __table_args__ = (
        CheckConstraint("override_kind IN ('replace', 'add')", name="ck_scheduled_overrides_kind"),
        CheckConstraint(
            "quantity >= 0 AND quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_scheduled_overrides_nonnegative_quantity",
        ),
        CheckConstraint(
            "(override_kind = 'replace' AND target_line_key IS NOT NULL "
            "AND include_in_portion_weight IS NULL) OR "
            "(override_kind = 'add' AND target_line_key IS NULL "
            "AND include_in_portion_weight IS NOT NULL)",
            name="ck_scheduled_overrides_shape",
        ),
        CheckConstraint(
            "position_key IS NULL OR position_key ~ '^[0-9A-Za-z]+$'",
            name="ck_scheduled_overrides_position_key",
        ),
        CheckConstraint(
            "last_modified_at >= created_at", name="ck_scheduled_overrides_audit_order"
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_scheduled_overrides_retirement_attribution",
        ),
        ForeignKeyConstraint(
            ["scheduled_recipe_id", "event_id", "organization_id"],
            [
                "scheduled_recipes.id",
                "scheduled_recipes.event_id",
                "scheduled_recipes.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_scheduled_recipe",
        ),
        ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_ingredient_organization",
        ),
        ForeignKeyConstraint(
            ["ingredient_version_id", "organization_id"],
            ["ingredient_versions.id", "ingredient_versions.organization_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_ingredient_version_organization",
        ),
        ForeignKeyConstraint(
            ["ingredient_version_id", "ingredient_id"],
            ["ingredient_versions.id", "ingredient_versions.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_scheduled_overrides_ingredient_version_ingredient",
        ),
        Index(
            "uq_scheduled_overrides_active_replacement",
            "scheduled_recipe_id",
            "target_line_key",
            unique=True,
            postgresql_where=text("override_kind = 'replace' AND retired_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    scheduled_recipe_id: Mapped[UUID] = mapped_column(Uuid)
    override_kind: Mapped[str] = mapped_column(String(16))
    target_line_key: Mapped[UUID | None] = mapped_column(Uuid)
    ingredient_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_version_id: Mapped[UUID] = mapped_column(Uuid)
    quantity: Mapped[Decimal] = mapped_column(Numeric)
    include_in_portion_weight: Mapped[bool | None] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
    position_key: Mapped[str | None] = mapped_column(String(255, collation="C"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    last_modified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    last_modified_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class ShoppingList(Base):
    __tablename__ = "shopping_lists"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_shopping_lists_name_not_empty"),
        ForeignKeyConstraint(
            ["event_id", "organization_id"],
            ["events.id", "events.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_lists_event_organization",
        ),
        ForeignKeyConstraint(
            ["current_generation_revision_id", "id"],
            ["shopping_generation_revisions.id", "shopping_generation_revisions.shopping_list_id"],
            ondelete="RESTRICT",
            name="fk_shopping_lists_current_generation_revision",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        UniqueConstraint(
            "id", "organization_id", "event_id", name="uq_shopping_lists_id_org_event"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(200))
    current_generation_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class ShoppingGenerationRevision(Base):
    __tablename__ = "shopping_generation_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["shopping_list_id", "organization_id", "event_id"],
            [
                "shopping_lists.id",
                "shopping_lists.organization_id",
                "shopping_lists.event_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_generation_revisions_list_scope",
        ),
        ForeignKeyConstraint(
            ["parent_revision_id", "shopping_list_id"],
            [
                "shopping_generation_revisions.id",
                "shopping_generation_revisions.shopping_list_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_generation_revisions_parent_same_list",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "parent_revision_id IS NULL OR parent_revision_id <> id",
            name="ck_shopping_generation_revisions_nonrecursive_parent",
        ),
        UniqueConstraint("id", "shopping_list_id", name="uq_shopping_generation_revisions_id_list"),
        UniqueConstraint(
            "id",
            "shopping_list_id",
            "organization_id",
            "event_id",
            name="uq_shopping_generation_revisions_id_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    shopping_list_id: Mapped[UUID] = mapped_column(Uuid)
    parent_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    generated_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class ShoppingRevisionSource(Base):
    __tablename__ = "shopping_revision_sources"
    __table_args__ = (
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["scheduled_recipe_id", "event_id", "organization_id"],
            [
                "scheduled_recipes.id",
                "scheduled_recipes.event_id",
                "scheduled_recipes.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_revision_sources_scheduled_recipe_scope",
        ),
    )

    generation_revision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    shopping_list_id: Mapped[UUID] = mapped_column(Uuid)
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    scheduled_recipe_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)


class ShoppingIngredientRow(Base):
    __tablename__ = "shopping_ingredient_rows"
    __table_args__ = (
        CheckConstraint("btrim(ingredient_name) <> ''", name="ck_shopping_rows_ingredient_name"),
        CheckConstraint(
            "available_supply_quantity >= 0 "
            "AND available_supply_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_rows_nonnegative_available_supply",
        ),
        CheckConstraint(
            "manual_purchase_target IS NULL OR (manual_purchase_target >= 0 "
            "AND manual_purchase_target::text NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name="ck_shopping_rows_nonnegative_manual_target",
        ),
        CheckConstraint(
            "manual_target_automatic_value IS NULL OR (manual_target_automatic_value >= 0 "
            "AND manual_target_automatic_value::text NOT IN ('NaN', 'Infinity', '-Infinity'))",
            name="ck_shopping_rows_nonnegative_manual_auto_value",
        ),
        CheckConstraint(
            "aggregate_fulfilment_credit >= 0 "
            "AND aggregate_fulfilment_credit::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_rows_nonnegative_aggregate_credit",
        ),
        CheckConstraint(
            "(manual_purchase_target IS NULL AND manual_target_automatic_value IS NULL "
            "AND manual_target_generation_revision_id IS NULL) OR "
            "(manual_purchase_target IS NOT NULL AND manual_target_automatic_value IS NOT NULL "
            "AND manual_target_generation_revision_id IS NOT NULL)",
            name="ck_shopping_rows_manual_target_basis",
        ),
        CheckConstraint(
            "(default_store_section_id IS NULL AND default_store_section_name IS NULL) OR "
            "(default_store_section_id IS NOT NULL AND btrim(default_store_section_name) <> '')",
            name="ck_shopping_rows_default_section_snapshot",
        ),
        CheckConstraint(
            "(aggregate_credit_updated_at IS NULL AND aggregate_credit_updated_by_user_id IS NULL "
            "AND aggregate_credit_updated_by_installation_id IS NULL) OR "
            "(aggregate_credit_updated_at IS NOT NULL "
            "AND aggregate_credit_updated_by_user_id IS NOT NULL "
            "AND aggregate_credit_updated_by_installation_id IS NOT NULL)",
            name="ck_shopping_rows_aggregate_credit_attribution",
        ),
        ForeignKeyConstraint(
            ["shopping_list_id", "organization_id", "event_id"],
            ["shopping_lists.id", "shopping_lists.organization_id", "shopping_lists.event_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_list_scope",
        ),
        ForeignKeyConstraint(
            ["ingredient_id", "organization_id"],
            ["ingredients.id", "ingredients.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_ingredient_organization",
        ),
        ForeignKeyConstraint(
            ["manual_target_generation_revision_id", "shopping_list_id"],
            [
                "shopping_generation_revisions.id",
                "shopping_generation_revisions.shopping_list_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_rows_manual_target_generation",
        ),
        ForeignKeyConstraint(
            ["default_store_section_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_default_section_organization",
        ),
        ForeignKeyConstraint(
            ["store_section_override_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_override_section_organization",
        ),
        ForeignKeyConstraint(
            ["aggregate_credit_updated_by_installation_id", "aggregate_credit_updated_by_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_shopping_rows_aggregate_credit_actor",
        ),
        UniqueConstraint(
            "id",
            "shopping_list_id",
            "ingredient_id",
            "organization_id",
            "event_id",
            name="uq_shopping_rows_id_list_ingredient_scope",
        ),
        UniqueConstraint(
            "shopping_list_id", "ingredient_id", name="uq_shopping_rows_list_ingredient"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    shopping_list_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_name: Mapped[str] = mapped_column(String(200))
    calculation_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("unit_definitions.id", ondelete="RESTRICT")
    )
    available_supply_quantity: Mapped[Decimal] = mapped_column(
        Numeric, default=Decimal("0"), server_default=text("0")
    )
    manual_purchase_target: Mapped[Decimal | None] = mapped_column(Numeric)
    manual_target_automatic_value: Mapped[Decimal | None] = mapped_column(Numeric)
    manual_target_generation_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    default_store_section_id: Mapped[UUID | None] = mapped_column(Uuid)
    default_store_section_name: Mapped[str | None] = mapped_column(String(200))
    store_section_override_id: Mapped[UUID | None] = mapped_column(Uuid)
    note: Mapped[str | None] = mapped_column(Text)
    aggregate_fulfilment_credit: Mapped[Decimal] = mapped_column(
        Numeric, default=Decimal("0"), server_default=text("0")
    )
    aggregate_credit_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    aggregate_credit_updated_by_user_id: Mapped[UUID | None] = mapped_column(Uuid)
    aggregate_credit_updated_by_installation_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class ShoppingContribution(Base):
    __tablename__ = "shopping_contributions"
    __table_args__ = (
        CheckConstraint(
            "fulfilment_credit >= 0 "
            "AND fulfilment_credit::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_contributions_nonnegative_credit",
        ),
        CheckConstraint(
            "(fulfilment_updated_at IS NULL AND fulfilment_updated_by_user_id IS NULL "
            "AND fulfilment_updated_by_installation_id IS NULL) OR "
            "(fulfilment_updated_at IS NOT NULL AND fulfilment_updated_by_user_id IS NOT NULL "
            "AND fulfilment_updated_by_installation_id IS NOT NULL)",
            name="ck_shopping_contributions_fulfilment_attribution",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["scheduled_recipe_id", "event_id", "organization_id"],
            [
                "scheduled_recipes.id",
                "scheduled_recipes.event_id",
                "scheduled_recipes.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_shopping_contributions_scheduled_recipe_scope",
        ),
        ForeignKeyConstraint(
            ["fulfilment_updated_by_installation_id", "fulfilment_updated_by_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_shopping_contributions_fulfilment_actor",
        ),
        UniqueConstraint(
            "shopping_list_id",
            "scheduled_recipe_id",
            "ingredient_id",
            name="uq_shopping_contributions_list_source_ingredient",
        ),
        UniqueConstraint(
            "id",
            "shopping_list_id",
            "ingredient_id",
            "organization_id",
            "event_id",
            name="uq_shopping_contributions_id_list_ingredient_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    shopping_list_id: Mapped[UUID] = mapped_column(Uuid)
    shopping_ingredient_row_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_id: Mapped[UUID] = mapped_column(Uuid)
    scheduled_recipe_id: Mapped[UUID] = mapped_column(Uuid)
    fulfilment_credit: Mapped[Decimal] = mapped_column(
        Numeric, default=Decimal("0"), server_default=text("0")
    )
    fulfilment_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilment_updated_by_user_id: Mapped[UUID | None] = mapped_column(Uuid)
    fulfilment_updated_by_installation_id: Mapped[UUID | None] = mapped_column(Uuid)


class ShoppingContributionSnapshot(Base):
    __tablename__ = "shopping_contribution_snapshots"
    __table_args__ = (
        CheckConstraint(
            "generated_quantity >= 0 "
            "AND generated_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_shopping_contribution_snapshots_nonnegative_quantity",
        ),
        CheckConstraint(
            "jsonb_typeof(source_details) = 'object'",
            name="ck_shopping_contribution_snapshots_source_details",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["ingredient_version_id", "ingredient_id"],
            ["ingredient_versions.id", "ingredient_versions.ingredient_id"],
            ondelete="RESTRICT",
            name="fk_shopping_contribution_snapshots_ingredient_version",
        ),
        UniqueConstraint(
            "generation_revision_id",
            "shopping_contribution_id",
            name="uq_shopping_contribution_snapshots_generation_contribution",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    shopping_list_id: Mapped[UUID] = mapped_column(Uuid)
    generation_revision_id: Mapped[UUID] = mapped_column(Uuid)
    shopping_contribution_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_id: Mapped[UUID] = mapped_column(Uuid)
    active_in_revision: Mapped[bool] = mapped_column(Boolean)
    generated_quantity: Mapped[Decimal] = mapped_column(Numeric)
    ingredient_version_id: Mapped[UUID] = mapped_column(Uuid)
    ingredient_name: Mapped[str] = mapped_column(String(200))
    source_details: Mapped[dict[str, object]] = mapped_column(JSONB)


class AdHocShoppingItem(Base):
    __tablename__ = "ad_hoc_shopping_items"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_ad_hoc_shopping_items_name_not_empty"),
        CheckConstraint(
            "target_amount >= 0 AND target_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_ad_hoc_shopping_items_nonnegative_target",
        ),
        CheckConstraint(
            "fulfilment_credit >= 0 "
            "AND fulfilment_credit::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_ad_hoc_shopping_items_nonnegative_credit",
        ),
        CheckConstraint(
            "(retired_at IS NULL AND retired_by_user_id IS NULL) OR "
            "(retired_at IS NOT NULL AND retired_by_user_id IS NOT NULL)",
            name="ck_ad_hoc_shopping_items_retirement_attribution",
        ),
        CheckConstraint(
            "(fulfilment_updated_at IS NULL AND fulfilment_updated_by_user_id IS NULL "
            "AND fulfilment_updated_by_installation_id IS NULL) OR "
            "(fulfilment_updated_at IS NOT NULL AND fulfilment_updated_by_user_id IS NOT NULL "
            "AND fulfilment_updated_by_installation_id IS NOT NULL)",
            name="ck_ad_hoc_shopping_items_fulfilment_attribution",
        ),
        ForeignKeyConstraint(
            ["shopping_list_id", "organization_id", "event_id"],
            ["shopping_lists.id", "shopping_lists.organization_id", "shopping_lists.event_id"],
            ondelete="RESTRICT",
            name="fk_ad_hoc_shopping_items_list_scope",
        ),
        ForeignKeyConstraint(
            ["store_section_id", "organization_id"],
            ["store_sections.id", "store_sections.organization_id"],
            ondelete="RESTRICT",
            name="fk_ad_hoc_shopping_items_section_organization",
        ),
        ForeignKeyConstraint(
            ["fulfilment_updated_by_installation_id", "fulfilment_updated_by_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_ad_hoc_shopping_items_fulfilment_actor",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    shopping_list_id: Mapped[UUID] = mapped_column(Uuid)
    name: Mapped[str] = mapped_column(String(200))
    target_amount: Mapped[Decimal] = mapped_column(Numeric)
    unit_id: Mapped[UUID] = mapped_column(ForeignKey("unit_definitions.id", ondelete="RESTRICT"))
    store_section_id: Mapped[UUID] = mapped_column(Uuid)
    note: Mapped[str | None] = mapped_column(Text)
    fulfilment_credit: Mapped[Decimal] = mapped_column(
        Numeric, default=Decimal("0"), server_default=text("0")
    )
    fulfilment_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilment_updated_by_user_id: Mapped[UUID | None] = mapped_column(Uuid)
    fulfilment_updated_by_installation_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class ClientInstallation(Base):
    __tablename__ = "client_installations"
    __table_args__ = (
        CheckConstraint(
            "installation_kind IN ('browser', 'agent')",
            name="ck_client_installations_kind",
        ),
        CheckConstraint(
            "(disabled_at IS NULL AND disabled_by_user_id IS NULL) OR "
            "(disabled_at IS NOT NULL AND disabled_by_user_id IS NOT NULL "
            "AND disabled_at >= created_at)",
            name="ck_client_installations_disabled_lifecycle",
        ),
        UniqueConstraint("id", "user_id", name="uq_client_installations_id_user"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    installation_kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )


class Mutation(Base):
    __tablename__ = "mutations"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_mutations_id_organization"),
        CheckConstraint(
            "(organization_id IS NOT NULL AND NOT is_system_administration_scope) OR "
            "(organization_id IS NULL AND is_system_administration_scope)",
            name="ck_mutations_scope",
        ),
        CheckConstraint(
            "actor_role IN ('member', 'organization_admin', 'system_admin')",
            name="ck_mutations_actor_role",
        ),
        CheckConstraint(
            "NOT is_system_administration_scope OR actor_role = 'system_admin'",
            name="ck_mutations_system_authority",
        ),
        CheckConstraint(
            "command_schema_version > 0",
            name="ck_mutations_command_schema_version",
        ),
        CheckConstraint(
            "command_kind ~ '^[a-z][a-z0-9_.-]*$'",
            name="ck_mutations_command_kind",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "octet_length(request_hash) = 32",
            name="ck_mutations_request_hash",
        ),
        CheckConstraint(
            "(oauth_client_id IS NULL AND oauth_grant_id IS NULL) OR "
            "(oauth_client_id IS NOT NULL AND btrim(oauth_client_id) <> '' "
            "AND oauth_client_id = btrim(oauth_client_id) "
            "AND oauth_grant_id IS NOT NULL AND btrim(oauth_grant_id) <> '' "
            "AND oauth_grant_id = btrim(oauth_grant_id))",
            name="ck_mutations_oauth_attribution",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'partially_superseded', 'rejected', 'failed')",
            name="ck_mutations_outcome",
        ),
        CheckConstraint(
            "outcome_payload IS NULL OR jsonb_typeof(outcome_payload) = 'object'",
            name="ck_mutations_outcome_payload",
        ),
        CheckConstraint(
            "(first_change_sequence IS NULL AND last_change_sequence IS NULL "
            "AND (is_system_administration_scope OR outcome IN ('rejected', 'failed'))) OR "
            "(first_change_sequence > 0 AND last_change_sequence >= first_change_sequence "
            "AND organization_id IS NOT NULL "
            "AND outcome IN ('accepted', 'partially_superseded'))",
            name="ck_mutations_change_sequence",
        ),
        CheckConstraint(
            "NOT is_system_administration_scope OR outcome <> 'partially_superseded'",
            name="ck_mutations_system_outcome",
        ),
        ForeignKeyConstraint(
            ["client_installation_id", "actor_user_id"],
            ["client_installations.id", "client_installations.user_id"],
            ondelete="RESTRICT",
            name="fk_mutations_client_actor",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    logical_operation_id: Mapped[UUID | None] = mapped_column(Uuid)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    is_system_administration_scope: Mapped[bool] = mapped_column(Boolean)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid)
    actor_role: Mapped[str] = mapped_column(String(32))
    client_installation_id: Mapped[UUID] = mapped_column(Uuid)
    oauth_client_id: Mapped[str | None] = mapped_column(String(255))
    oauth_grant_id: Mapped[str | None] = mapped_column(String(255))
    client_wall_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    server_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    command_schema_version: Mapped[int] = mapped_column(SmallInteger)
    command_kind: Mapped[str] = mapped_column(String(100))
    target_identities: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    outcome: Mapped[str] = mapped_column(String(32))
    outcome_payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    first_change_sequence: Mapped[int | None] = mapped_column(BigInteger)
    last_change_sequence: Mapped[int | None] = mapped_column(BigInteger)


class FieldClock(Base):
    """The deterministic winning action for one synchronizable scalar field.

    Attribution is intentionally sourced from ``winning_mutation_id``.  That keeps
    it impossible for a clock to claim a different actor or installation than the
    recorded idempotency command.
    """

    __tablename__ = "field_clocks"
    __table_args__ = (
        CheckConstraint(
            "entity_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_field_clocks_entity_kind"
        ),
        CheckConstraint(
            "field_name ~ '^[a-z][a-z0-9_.-]{0,99}$'", name="ck_field_clocks_field_name"
        ),
        ForeignKeyConstraint(
            ["organization_id", "winning_mutation_id"],
            ["mutations.organization_id", "mutations.id"],
            ondelete="RESTRICT",
            name="fk_field_clocks_winning_mutation",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    entity_kind: Mapped[str] = mapped_column(String(100), primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    field_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    winning_client_wall_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    winning_mutation_id: Mapped[UUID] = mapped_column(Uuid)


class OrganizationChange(Base):
    __tablename__ = "organization_changes"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_organization_changes_positive_sequence"),
        CheckConstraint(
            "entity_kind ~ '^[a-z][a-z0-9_.-]{0,99}$'",
            name="ck_organization_changes_entity_kind",
        ),
        CheckConstraint(
            "operation ~ '^[a-z][a-z0-9_.-]{0,99}$'",
            name="ck_organization_changes_operation",
        ),
        CheckConstraint(
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
        ForeignKeyConstraint(
            ["organization_id", "mutation_id"],
            [
                "organization_change_transactions.organization_id",
                "organization_change_transactions.mutation_id",
            ],
            ondelete="RESTRICT",
            name="fk_organization_changes_transaction",
        ),
        Index("ix_organization_changes_mutation_id", "mutation_id"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mutation_id: Mapped[UUID] = mapped_column(Uuid)
    entity_id: Mapped[UUID] = mapped_column(Uuid)
    entity_kind: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )


class OrganizationChangeHead(Base):
    __tablename__ = "organization_change_heads"
    __table_args__ = (
        CheckConstraint("next_sequence > 0", name="ck_organization_change_heads_next_sequence"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    next_sequence: Mapped[int] = mapped_column(BigInteger)


class OrganizationChangeTransaction(Base):
    __tablename__ = "organization_change_transactions"
    __table_args__ = (
        CheckConstraint(
            "first_change_sequence > 0 AND last_change_sequence >= first_change_sequence",
            name="ck_organization_change_transactions_range",
        ),
        ExcludeConstraint(
            ("organization_id", "="),
            (text("int8range(first_change_sequence, last_change_sequence, '[]')"), "&&"),
            name="ex_organization_change_transactions_nonoverlapping_range",
        ),
        ForeignKeyConstraint(
            ["organization_id", "mutation_id"],
            ["mutations.organization_id", "mutations.id"],
            ondelete="RESTRICT",
            name="fk_organization_change_transactions_mutation",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    mutation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    first_change_sequence: Mapped[int] = mapped_column(BigInteger)
    last_change_sequence: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
