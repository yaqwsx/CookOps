from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
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
