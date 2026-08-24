"""Focused guarded ingredient-copy preview checks."""

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from cookops.application.ingredient_copy import (
    CopyIngredientToOrganizationCommand,
    IngredientCopyMapping,
    PreviewIngredientCopyCommand,
    copy_ingredient_to_organization,
    preview_ingredient_copy,
)
from cookops.application.organizations import (
    ApplicationServiceError,
    ExecutionContext,
    FieldViolation,
)
from cookops.config import Settings
from cookops.http_ingredient_copy import IngredientCopyHttpServices
from cookops.main import create_app
from cookops.persistence.models import (
    ClientInstallation,
    DietaryTag,
    FieldClock,
    Ingredient,
    IngredientVersion,
    IngredientVersionDietaryTag,
    Organization,
    OrganizationChange,
    OrganizationMembership,
    StoreSection,
    SystemRoleAssignment,
    UnitDefinition,
    User,
)

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="TEST_DATABASE_URL is not set"
)


@pytest.fixture
def copy_database():
    url = os.environ["TEST_DATABASE_URL"]
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", url)
    command.downgrade(configuration, "base")
    command.upgrade(configuration, "head")
    engine = create_engine(url)
    actor, installation = uuid4(), uuid4()
    source_org, destination_org = uuid4(), uuid4()
    ingredient, version, section, tag = (uuid4() for _ in range(4))
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=actor,
                display_name="Copy actor",
                verified_email="copy@example.test",
                normalized_email="copy@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=installation, user_id=actor, installation_kind="browser"
            )
        )
        connection.execute(
            insert(Organization),
            [
                {
                    "id": source_org,
                    "name": "Source",
                    "default_currency": "CZK",
                    "created_by_user_id": actor,
                },
                {
                    "id": destination_org,
                    "name": "Destination",
                    "default_currency": "CZK",
                    "created_by_user_id": actor,
                },
            ],
        )
        connection.execute(
            insert(OrganizationMembership),
            [
                {
                    "organization_id": source_org,
                    "user_id": actor,
                    "invited_email": "copy@example.test",
                    "role": "member",
                    "state": "active",
                    "invited_by_user_id": actor,
                    "claimed_at": now,
                },
                {
                    "organization_id": destination_org,
                    "user_id": actor,
                    "invited_email": "copy@example.test",
                    "role": "organization_admin",
                    "state": "active",
                    "invited_by_user_id": actor,
                    "claimed_at": now,
                },
            ],
        )
        unit = connection.scalar(
            select(UnitDefinition.id).where(
                UnitDefinition.organization_id.is_(None), UnitDefinition.code == "g"
            )
        )
        assert unit is not None
        connection.execute(
            insert(StoreSection).values(
                id=section,
                organization_id=source_org,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=actor,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=tag, organization_id=source_org, seed_key="vegan", created_by_user_id=actor
            )
        )
        connection.execute(
            insert(Ingredient).values(
                id=ingredient,
                organization_id=source_org,
                current_version_id=version,
                created_by_user_id=actor,
            )
        )
        connection.execute(
            insert(IngredientVersionDietaryTag).values(
                ingredient_version_id=version, dietary_tag_id=tag, organization_id=source_org
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=version,
                organization_id=source_org,
                ingredient_id=ingredient,
                name="Carrot",
                normalized_name="carrot",
                canonical_unit_id=unit,
                mass_per_canonical_quantity=1,
                default_store_section_id=section,
                published_by_user_id=actor,
            )
        )
    async_engine = create_async_engine(url, poolclass=NullPool)
    value = SimpleNamespace(
        sessions=async_sessionmaker(async_engine, expire_on_commit=False),
        actor=actor,
        installation=installation,
        source=source_org,
        destination=destination_org,
        ingredient=ingredient,
        version=version,
        section=section,
        tag=tag,
        unit=unit,
        engine=engine,
    )
    yield value
    async_engine.sync_engine.dispose()


def context(db):
    return ExecutionContext(db.actor, db.installation)


def test_allowed_preview_is_scoped_and_does_not_mutate(copy_database):
    db = copy_database
    with db.engine.connect() as connection:
        before = connection.execute(
            select(Ingredient.id).where(Ingredient.id == db.ingredient)
        ).scalar_one()
    preview = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert preview.source_version_id == db.version
    assert preview.canonical_unit_id == db.unit
    assert {item.kind for item in preview.mapping_requirements} == {
        "default_store_section",
        "dietary_tag",
    }
    with db.engine.connect() as connection:
        assert (
            connection.execute(
                select(Ingredient.id).where(Ingredient.id == db.ingredient)
            ).scalar_one()
            == before
        )
        assert connection.execute(select(OrganizationChange)).first() is None


def test_preview_includes_retired_historical_dependencies(copy_database):
    db = copy_database
    historical_version, historical_unit, historical_section, historical_tag = (
        uuid4() for _ in range(4)
    )
    now = datetime.now(UTC)
    with db.engine.begin() as connection:
        connection.execute(
            insert(UnitDefinition).values(
                id=historical_unit,
                organization_id=db.source,
                code="custom.spoon",
                custom_name="Spoon",
                normalized_custom_name="spoon",
                dimension="mass",
                base_unit_factor=1,
                rounds_up_to_whole_unit=False,
                allows_ingredient_quantity=True,
                allows_recipe_scaling=False,
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(StoreSection).values(
                id=historical_section,
                organization_id=db.source,
                name="Pantry",
                normalized_name="pantry",
                position_key="b",
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=historical_tag,
                organization_id=db.source,
                name="Seasonal",
                normalized_name="seasonal",
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(IngredientVersionDietaryTag).values(
                ingredient_version_id=historical_version,
                dietary_tag_id=historical_tag,
                organization_id=db.source,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=historical_version,
                organization_id=db.source,
                ingredient_id=db.ingredient,
                name="Carrot (historical)",
                normalized_name="carrot (historical)",
                canonical_unit_id=historical_unit,
                mass_per_canonical_quantity=1,
                default_store_section_id=historical_section,
                published_by_user_id=db.actor,
            )
        )
        current_version = uuid4()
        connection.execute(
            insert(IngredientVersionDietaryTag).values(
                ingredient_version_id=current_version,
                dietary_tag_id=db.tag,
                organization_id=db.source,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=current_version,
                organization_id=db.source,
                ingredient_id=db.ingredient,
                based_on_version_id=historical_version,
                name="Carrot",
                normalized_name="carrot",
                canonical_unit_id=db.unit,
                mass_per_canonical_quantity=1,
                default_store_section_id=db.section,
                published_by_user_id=db.actor,
            )
        )
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == db.ingredient)
            .values(current_version_id=current_version)
        )
        connection.execute(
            update(UnitDefinition)
            .where(UnitDefinition.id == historical_unit)
            .values(retired_at=now, retired_by_user_id=db.actor)
        )
        connection.execute(
            update(StoreSection)
            .where(StoreSection.id == historical_section)
            .values(retired_at=now, retired_by_user_id=db.actor)
        )
        connection.execute(
            update(DietaryTag)
            .where(DietaryTag.id == historical_tag)
            .values(retired_at=now, retired_by_user_id=db.actor)
        )

    first = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    requirements = {(item.kind, item.source_id) for item in first.mapping_requirements}
    assert ("canonical_unit", historical_unit) in requirements
    assert ("default_store_section", historical_section) in requirements
    assert ("dietary_tag", historical_tag) in requirements
    repeated = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert repeated.precondition_fingerprint == first.precondition_fingerprint
    assert repeated.mapping_requirements == first.mapping_requirements

    with db.engine.begin() as connection:
        connection.execute(
            update(DietaryTag).where(DietaryTag.id == historical_tag).values(seed_key="vegetarian")
        )
        connection.execute(
            insert(DietaryTag).values(
                id=uuid4(),
                organization_id=db.destination,
                seed_key="vegetarian",
                created_by_user_id=db.actor,
            )
        )
    second = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert second.precondition_fingerprint != first.precondition_fingerprint
    assert ("dietary_tag", historical_tag) not in {
        (item.kind, item.source_id) for item in second.mapping_requirements
    }


def test_retired_source_is_stale_broken_graph(copy_database):
    db = copy_database
    with db.engine.begin() as connection:
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == db.ingredient)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=db.actor)
        )
    with pytest.raises(ApplicationServiceError, match="stale_precondition"):
        asyncio.run(
            preview_ingredient_copy(
                db.sessions,
                context(db),
                PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
            )
        )


def test_source_member_or_destination_member_is_denied(copy_database):
    db = copy_database
    with db.engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(OrganizationMembership.organization_id == db.destination)
            .values(role="member")
        )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            preview_ingredient_copy(
                db.sessions,
                context(db),
                PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
            )
        )
    assert error.value.code == "forbidden"


def test_disabled_user_is_denied_without_mutation(copy_database):
    db = copy_database
    with db.engine.begin() as connection:
        connection.execute(
            update(User)
            .where(User.id == db.actor)
            .values(disabled_at=datetime.now(UTC), disabled_by_user_id=db.actor)
        )
    with pytest.raises(ApplicationServiceError) as error:
        asyncio.run(
            preview_ingredient_copy(
                db.sessions,
                context(db),
                PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
            )
        )
    assert error.value.code == "forbidden"
    with db.engine.connect() as connection:
        assert connection.execute(select(OrganizationChange)).first() is None


def test_system_admin_can_preview_without_memberships(copy_database):
    db = copy_database
    with db.engine.begin() as connection:
        connection.execute(
            delete(OrganizationMembership).where(OrganizationMembership.user_id == db.actor)
        )
        connection.execute(
            insert(SystemRoleAssignment).values(
                id=uuid4(),
                user_id=db.actor,
                invited_email="copy@example.test",
                role="system_admin",
                granted_by_user_id=db.actor,
                claimed_at=datetime.now(UTC),
            )
        )
    preview = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert preview.source_ingredient_id == db.ingredient


def test_retired_source_tag_remains_previewable_and_fingerprint_changes(copy_database):
    db = copy_database
    with db.engine.begin() as connection:
        connection.execute(
            update(DietaryTag)
            .where(DietaryTag.id == db.tag)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=db.actor)
        )
    first = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert any(item.kind == "dietary_tag" for item in first.mapping_requirements)
    with db.engine.begin() as connection:
        connection.execute(
            update(DietaryTag).where(DietaryTag.id == db.tag).values(seed_key="vegetarian")
        )
    changed_tag = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert changed_tag.precondition_fingerprint != first.precondition_fingerprint
    with db.engine.begin() as connection:
        connection.execute(
            insert(DietaryTag).values(
                id=uuid4(),
                organization_id=db.destination,
                seed_key="vegan",
                created_by_user_id=db.actor,
            )
        )
    second = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert second.precondition_fingerprint != first.precondition_fingerprint
    with db.engine.begin() as connection:
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == db.ingredient)
            .values(retired_at=datetime.now(UTC), retired_by_user_id=db.actor)
        )
    with pytest.raises(ApplicationServiceError, match="stale_precondition"):
        asyncio.run(
            preview_ingredient_copy(
                db.sessions,
                context(db),
                PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
            )
        )


def test_authenticated_http_preview_route(copy_database):
    db = copy_database
    settings = Settings(browser_session_cookie_name="cookops_session")
    app = create_app(settings, readiness_probe=lambda: _ready())

    async def authenticate(_: str):
        return _authenticated(db.actor)

    app.state.ingredient_copy = IngredientCopyHttpServices(
        browser_sessions=SimpleNamespace(authenticate=authenticate),
        session_factory=db.sessions,
    )
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/organizations/{db.destination}/ingredient-copy-preview/{db.source}/{db.ingredient}",
            cookies={"cookops_session": "session"},
        )
    assert response.status_code == 200
    assert response.json()["source_version_id"] == str(db.version)


async def _ready() -> bool:
    return True


def _authenticated(user_id):
    return SimpleNamespace(user_id=user_id)


def _copy_http_app(db, *, authenticated_user_id=None):
    settings = Settings(
        browser_session_cookie_name="cookops_session",
        browser_origin="https://testserver",
    )
    app = create_app(settings, readiness_probe=lambda: _ready())
    user_id = db.actor if authenticated_user_id is None else authenticated_user_id

    async def authenticate(secret: str):
        return _authenticated(user_id) if secret == "session" else None

    app.state.ingredient_copy = IngredientCopyHttpServices(
        browser_sessions=SimpleNamespace(authenticate=authenticate),
        session_factory=db.sessions,
    )
    return app


def _http_copy_payload(db, *, mutation_id=None):
    destination_section, destination_tag = uuid4(), uuid4()
    with db.engine.begin() as connection:
        connection.execute(
            insert(StoreSection).values(
                id=destination_section,
                organization_id=db.destination,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=destination_tag,
                organization_id=db.destination,
                seed_key="vegan",
                created_by_user_id=db.actor,
            )
        )
    preview = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    targets = {
        "default_store_section": destination_section,
        "dietary_tag": destination_tag,
    }
    return {
        "source_organization_id": str(db.source),
        "ingredient_id": str(db.ingredient),
        "client_installation_id": str(db.installation),
        "precondition_fingerprint": preview.precondition_fingerprint,
        "mappings": [
            {
                "kind": item.kind,
                "source_id": str(item.source_id),
                "destination_id": str(targets[item.kind]),
            }
            for item in preview.mapping_requirements
        ],
        "mutation_id": str(mutation_id or uuid4()),
        "client_wall_time": datetime.now(UTC).isoformat(),
    }


def test_authenticated_http_copy_route_and_replay(copy_database):
    db = copy_database
    payload = _http_copy_payload(db)
    app = _copy_http_app(db)
    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/organizations/{db.destination}/ingredient-copy",
            json=payload,
            cookies={"cookops_session": "session"},
            headers={"origin": "https://testserver"},
        )
        second = client.post(
            f"/api/v1/organizations/{db.destination}/ingredient-copy",
            json=payload,
            cookies={"cookops_session": "session"},
            headers={"origin": "https://testserver"},
        )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_body, second_body = first.json(), second.json()
    assert first_body["replayed"] is False
    assert second_body["replayed"] is True
    assert second_body["destination_ingredient_id"] == first_body["destination_ingredient_id"]
    assert second_body["first_change_sequence"] == first_body["first_change_sequence"]
    assert second_body["last_change_sequence"] == first_body["last_change_sequence"]
    with db.engine.connect() as connection:
        changes = connection.execute(
            select(OrganizationChange).where(
                OrganizationChange.mutation_id == UUID(payload["mutation_id"])
            )
        ).all()
        assert (
            len(changes)
            == first_body["last_change_sequence"] - first_body["first_change_sequence"] + 1
        )


def test_http_copy_requires_session_and_exact_origin_without_mutation(copy_database):
    db = copy_database
    payload = _http_copy_payload(db)
    app = _copy_http_app(db)
    route = f"/api/v1/organizations/{db.destination}/ingredient-copy"
    with TestClient(app) as client:
        missing_session = client.post(route, json=payload, headers={"origin": "https://testserver"})
        wrong_origin = client.post(
            route,
            json=payload,
            cookies={"cookops_session": "session"},
            headers={"origin": "https://attacker.example"},
        )
        absent_origin = client.post(route, json=payload, cookies={"cookops_session": "session"})
    assert missing_session.status_code == 401
    assert wrong_origin.status_code == absent_origin.status_code == 403
    assert wrong_origin.json() == absent_origin.json() == {"detail": {"code": "forbidden"}}
    with db.engine.connect() as connection:
        assert connection.execute(select(OrganizationChange)).first() is None


def test_http_copy_forbidden_is_non_enumerating_and_payload_is_strict(copy_database):
    db = copy_database
    payload = _http_copy_payload(db)
    app = _copy_http_app(db, authenticated_user_id=uuid4())
    route = f"/api/v1/organizations/{db.destination}/ingredient-copy"
    with TestClient(app) as client:
        forbidden = client.post(
            route,
            json=payload,
            cookies={"cookops_session": "session"},
            headers={"origin": "https://testserver"},
        )
        extra = client.post(
            route,
            json={**payload, "unexpected": True},
            cookies={"cookops_session": "session"},
            headers={"origin": "https://testserver"},
        )
        malformed = client.post(
            route,
            json={**payload, "ingredient_id": 42},
            cookies={"cookops_session": "session"},
            headers={"origin": "https://testserver"},
        )
    assert forbidden.status_code == 404
    assert forbidden.json() == {"detail": {"code": "not_found"}}
    assert extra.status_code == 422
    assert malformed.status_code == 422


@pytest.mark.parametrize("field", ["client_installation_id", "mutation_id"])
def test_http_copy_rejects_zero_identity_without_mutation(copy_database, field):
    db = copy_database
    payload = {**_http_copy_payload(db), field: "00000000-0000-0000-0000-000000000000"}
    app = _copy_http_app(db)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/organizations/{db.destination}/ingredient-copy",
            json=payload,
            cookies={"cookops_session": "session"},
            headers={"origin": "https://testserver"},
        )
    assert response.status_code == 422
    with db.engine.connect() as connection:
        assert connection.execute(select(OrganizationChange)).first() is None


def test_http_command_error_preserves_validation_contract():
    from cookops.http_ingredient_copy import _command_error

    error = ApplicationServiceError(
        "validation_failed",
        field_violations=(FieldViolation("mappings", "missing"),),
        retry_same_identity=False,
    )
    response = _command_error(error)
    assert response.status_code == 422
    assert response.detail == {
        "code": "validation_failed",
        "field_violations": [{"path": "mappings", "code": "missing"}],
        "retry_same_identity": False,
    }


def test_http_copy_rejects_unowned_disabled_and_non_browser_installations(copy_database):
    db = copy_database
    route = f"/api/v1/organizations/{db.destination}/ingredient-copy"
    app = _copy_http_app(db)
    foreign_user, foreign_installation = uuid4(), uuid4()
    disabled_installation, agent_installation = uuid4(), uuid4()
    with db.engine.begin() as connection:
        connection.execute(
            insert(User).values(
                id=foreign_user,
                display_name="Foreign copy actor",
                verified_email="foreign-copy@example.test",
                normalized_email="foreign-copy@example.test",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=foreign_installation,
                user_id=foreign_user,
                installation_kind="browser",
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=disabled_installation,
                user_id=db.actor,
                installation_kind="browser",
                disabled_at=datetime.now(UTC),
                disabled_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(ClientInstallation).values(
                id=agent_installation,
                user_id=db.actor,
                installation_kind="agent",
            )
        )

    payload = _http_copy_payload(db)
    for installation_kind, installation_id in (
        ("foreign", foreign_installation),
        ("disabled", disabled_installation),
        ("agent", agent_installation),
    ):
        request_payload = {**payload, "client_installation_id": str(installation_id)}
        with TestClient(app) as client:
            response = client.post(
                route,
                json=request_payload,
                cookies={"cookops_session": "session"},
                headers={"origin": "https://testserver"},
            )
        assert response.status_code == 404, (installation_kind, response.text)
        assert response.json() == {"detail": {"code": "not_found"}}

    with db.engine.connect() as connection:
        assert connection.execute(select(OrganizationChange)).first() is None


def _prepare_multiversion_copy(db):
    historical_version, historical_unit, destination_unit = (uuid4() for _ in range(3))
    destination_section, destination_tag = (uuid4() for _ in range(2))
    with db.engine.begin() as connection:
        connection.execute(
            update(DietaryTag)
            .where(DietaryTag.id == db.tag)
            .values(seed_key=None, name="Seasonal", normalized_name="seasonal")
        )
        connection.execute(
            insert(UnitDefinition).values(
                id=historical_unit,
                organization_id=db.source,
                code="custom.spoon",
                custom_name="Spoon",
                normalized_custom_name="spoon",
                dimension="mass",
                base_unit_factor=1,
                rounds_up_to_whole_unit=False,
                allows_ingredient_quantity=True,
                allows_recipe_scaling=False,
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(UnitDefinition).values(
                id=destination_unit,
                organization_id=db.destination,
                code="custom.spoon",
                custom_name="Spoon",
                normalized_custom_name="spoon",
                dimension="mass",
                base_unit_factor=1,
                rounds_up_to_whole_unit=False,
                allows_ingredient_quantity=True,
                allows_recipe_scaling=False,
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(StoreSection).values(
                id=destination_section,
                organization_id=db.destination,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=destination_tag,
                organization_id=db.destination,
                name="Seasonal",
                normalized_name="seasonal",
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(IngredientVersionDietaryTag).values(
                ingredient_version_id=historical_version,
                dietary_tag_id=db.tag,
                organization_id=db.source,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=historical_version,
                organization_id=db.source,
                ingredient_id=db.ingredient,
                based_on_version_id=db.version,
                name="Carrot (historical)",
                normalized_name="carrot (historical)",
                canonical_unit_id=historical_unit,
                mass_per_canonical_quantity=1,
                default_store_section_id=db.section,
                published_by_user_id=db.actor,
            )
        )
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == db.ingredient)
            .values(current_version_id=historical_version)
        )
    return SimpleNamespace(
        destination_unit=destination_unit,
        destination_section=destination_section,
        destination_tag=destination_tag,
    )


def _copy_command(db, setup, *, create_custom_tag=False):
    preview = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    targets = {
        "canonical_unit": setup.destination_unit,
        "default_store_section": setup.destination_section,
        "dietary_tag": None if create_custom_tag else setup.destination_tag,
    }
    mappings = tuple(
        IngredientCopyMapping(item.kind, item.source_id, targets[item.kind])
        for item in preview.mapping_requirements
    )
    return CopyIngredientToOrganizationCommand(
        db.source,
        db.destination,
        db.ingredient,
        preview.precondition_fingerprint,
        mappings,
    )


def test_copy_multiversion_graph_has_only_destination_references(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    with db.engine.begin() as connection:
        connection.execute(delete(DietaryTag).where(DietaryTag.id == setup.destination_tag))
    result = asyncio.run(
        copy_ingredient_to_organization(
            db.sessions, context(db), _copy_command(db, setup, create_custom_tag=True)
        )
    )
    assert result.destination_organization_id == db.destination
    with db.engine.connect() as connection:
        copied_versions = (
            connection.execute(
                select(IngredientVersion.id).where(
                    IngredientVersion.ingredient_id == result.destination_ingredient_id
                )
            )
            .scalars()
            .all()
        )
        assert len(copied_versions) == 2
        copied_version_rows = connection.execute(
            select(
                IngredientVersion.organization_id,
                IngredientVersion.canonical_unit_id,
                IngredientVersion.default_store_section_id,
            ).where(IngredientVersion.id.in_(copied_versions))
        ).all()
        assert {item.organization_id for item in copied_version_rows} == {db.destination}
        assert {item.canonical_unit_id for item in copied_version_rows} <= {
            db.unit,
            setup.destination_unit,
        }
        assert {item.default_store_section_id for item in copied_version_rows} == {
            setup.destination_section
        }
        copied_tags = (
            connection.execute(
                select(IngredientVersionDietaryTag.dietary_tag_id).where(
                    IngredientVersionDietaryTag.ingredient_version_id.in_(copied_versions)
                )
            )
            .scalars()
            .all()
        )
        copied_tag_rows = (
            connection.execute(
                select(DietaryTag.organization_id).where(DietaryTag.id.in_(copied_tags))
            )
            .scalars()
            .all()
        )
        assert set(copied_tag_rows) == {db.destination}
        copied_tag_id = copied_tags[0]
        assert copied_tag_id != setup.destination_tag
        assert (
            connection.execute(
                select(DietaryTag.organization_id).where(DietaryTag.id == copied_tag_id)
            ).scalar_one()
            == db.destination
        )
        changes = connection.execute(
            select(OrganizationChange.entity_kind, OrganizationChange.payload)
            .where(OrganizationChange.mutation_id == result.mutation_id)
            .order_by(OrganizationChange.sequence)
        ).all()
        change_records = {
            item.entity_kind: item.payload["record"]
            for item in changes
            if item.entity_kind != "ingredient_version"
        }
        ingredient_record = change_records["ingredient"]
        assert (
            ingredient_record["created_at"]
            == connection.execute(
                select(Ingredient.created_at).where(
                    Ingredient.id == result.destination_ingredient_id
                )
            )
            .scalar_one()
            .isoformat()
        )
        assert set(ingredient_record["field_clocks"]) == {
            "lifecycle",
            "current_version_id",
            "current_price_estimate_id",
        }
        version_records = [
            item.payload["record"] for item in changes if item.entity_kind == "ingredient_version"
        ]
        assert len(version_records) == 2
        assert all(record["published_at"] for record in version_records)
        tag_record = change_records["dietary_tag"]
        assert (
            tag_record["created_at"]
            == connection.execute(
                select(DietaryTag.created_at).where(DietaryTag.id == copied_tag_id)
            )
            .scalar_one()
            .isoformat()
        )
        assert set(tag_record["field_clocks"]) == {"name", "color", "lifecycle"}
        assert {item.entity_kind for item in changes} >= {
            "ingredient",
            "ingredient_version",
            "dietary_tag",
        }
        clocks = (
            connection.execute(
                select(FieldClock.field_name).where(FieldClock.entity_id == copied_tag_id)
            )
            .scalars()
            .all()
        )
        assert set(clocks) == {"lifecycle", "name", "color"}


def test_copy_allows_many_to_one_explicit_dietary_tag_mapping(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    source_tag, new_version = uuid4(), uuid4()
    with db.engine.begin() as connection:
        connection.execute(
            insert(DietaryTag).values(
                id=source_tag,
                organization_id=db.source,
                name="Seasonal alternate",
                normalized_name="seasonal alternate",
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(IngredientVersionDietaryTag).values(
                ingredient_version_id=new_version,
                dietary_tag_id=source_tag,
                organization_id=db.source,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=new_version,
                organization_id=db.source,
                ingredient_id=db.ingredient,
                based_on_version_id=db.version,
                name="Carrot alternate",
                normalized_name="carrot alternate",
                canonical_unit_id=db.unit,
                mass_per_canonical_quantity=1,
                default_store_section_id=db.section,
                published_by_user_id=db.actor,
            )
        )
        connection.execute(
            update(Ingredient)
            .where(Ingredient.id == db.ingredient)
            .values(current_version_id=new_version)
        )

    command = _copy_command(db, setup)
    assert sum(item.kind == "dietary_tag" for item in command.mappings) == 2
    result = asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    with db.engine.connect() as connection:
        copied_tag_ids = (
            connection.execute(
                select(IngredientVersionDietaryTag.dietary_tag_id)
                .join(
                    IngredientVersion,
                    IngredientVersion.id == IngredientVersionDietaryTag.ingredient_version_id,
                )
                .where(IngredientVersion.ingredient_id == result.destination_ingredient_id)
            )
            .scalars()
            .all()
        )
    assert len(copied_tag_ids) == 3
    assert set(copied_tag_ids) == {setup.destination_tag}


def test_copy_reuses_active_seeded_destination_tag_without_mapping(copy_database):
    db = copy_database
    destination_section = uuid4()
    destination_tag = uuid4()
    with db.engine.begin() as connection:
        connection.execute(
            insert(StoreSection).values(
                id=destination_section,
                organization_id=db.destination,
                name="Produce",
                normalized_name="produce",
                position_key="a",
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(DietaryTag).values(
                id=destination_tag,
                organization_id=db.destination,
                seed_key="vegan",
                created_by_user_id=db.actor,
            )
        )
    preview = asyncio.run(
        preview_ingredient_copy(
            db.sessions,
            context(db),
            PreviewIngredientCopyCommand(db.source, db.destination, db.ingredient),
        )
    )
    assert all(item.kind != "dietary_tag" for item in preview.mapping_requirements)
    command = CopyIngredientToOrganizationCommand(
        db.source,
        db.destination,
        db.ingredient,
        preview.precondition_fingerprint,
        tuple(
            IngredientCopyMapping(item.kind, item.source_id, destination_section)
            for item in preview.mapping_requirements
        ),
    )
    result = asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    with db.engine.connect() as connection:
        copied_tags = (
            connection.execute(
                select(IngredientVersionDietaryTag.dietary_tag_id).where(
                    IngredientVersionDietaryTag.ingredient_version_id
                    == result.destination_version_id
                )
            )
            .scalars()
            .all()
        )
        assert set(copied_tags) == {destination_tag}


def test_copy_accepted_retry_replays_without_duplicate_graph_or_feed(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    command = _copy_command(db, setup)
    first = asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    second = asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    assert second.replayed is True
    assert second.destination_ingredient_id == first.destination_ingredient_id
    assert second.destination_version_id == first.destination_version_id
    assert (second.first_change_sequence, second.last_change_sequence) == (
        first.first_change_sequence,
        first.last_change_sequence,
    )
    with db.engine.connect() as connection:
        assert connection.execute(
            select(Ingredient.id).where(Ingredient.organization_id == db.destination)
        ).all() == [(first.destination_ingredient_id,)]
        assert (
            len(
                connection.execute(
                    select(OrganizationChange).where(
                        OrganizationChange.mutation_id == first.mutation_id
                    )
                ).all()
            )
            == first.last_change_sequence - first.first_change_sequence + 1
        )


def test_copy_replay_after_role_revocation_is_forbidden(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    command = _copy_command(db, setup)
    asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    with db.engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(OrganizationMembership.organization_id == db.destination)
            .values(role="member")
        )
    with pytest.raises(ApplicationServiceError, match="forbidden"):
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))


def test_copy_stale_mapping_leaves_no_copy(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    command = _copy_command(db, setup)
    with db.engine.begin() as connection:
        connection.execute(
            update(DietaryTag)
            .where(DietaryTag.id == db.tag)
            .values(name="Renamed", normalized_name="renamed")
        )
    with pytest.raises(ApplicationServiceError, match="stale_precondition") as first_error:
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    with pytest.raises(ApplicationServiceError, match="stale_precondition") as replay_error:
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    assert replay_error.value.code == first_error.value.code
    assert replay_error.value.field_violations == first_error.value.field_violations
    with db.engine.connect() as connection:
        assert (
            connection.execute(
                select(Ingredient.id).where(Ingredient.organization_id == db.destination)
            ).first()
            is None
        )


def test_copy_authorization_error_leaves_no_copy(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    command = _copy_command(db, setup)
    with db.engine.begin() as connection:
        connection.execute(
            update(OrganizationMembership)
            .where(OrganizationMembership.organization_id == db.destination)
            .values(role="member")
        )
    with pytest.raises(ApplicationServiceError, match="forbidden"):
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))


def test_copy_invalid_mapping_rolls_back(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    command = _copy_command(db, setup)
    invalid = CopyIngredientToOrganizationCommand(
        command.source_organization_id,
        command.destination_organization_id,
        command.ingredient_id,
        command.precondition_fingerprint,
        tuple(
            IngredientCopyMapping(item.kind, item.source_id, uuid4()) for item in command.mappings
        ),
    )
    with pytest.raises(ApplicationServiceError, match="validation_failed") as first_error:
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), invalid))
    with pytest.raises(ApplicationServiceError, match="validation_failed") as replay_error:
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), invalid))
    assert replay_error.value.code == first_error.value.code
    assert replay_error.value.field_violations == first_error.value.field_violations
    with db.engine.connect() as connection:
        assert (
            connection.execute(
                select(Ingredient.id).where(Ingredient.organization_id == db.destination)
            ).first()
            is None
        )


def test_copy_custom_tag_name_collision_is_retained_rejection(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    command = _copy_command(db, setup, create_custom_tag=True)
    with pytest.raises(ApplicationServiceError, match="validation_failed") as first_error:
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    with pytest.raises(ApplicationServiceError, match="validation_failed") as replay_error:
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    assert replay_error.value.field_violations == first_error.value.field_violations
    with db.engine.connect() as connection:
        assert (
            connection.execute(
                select(Ingredient.id).where(Ingredient.organization_id == db.destination)
            ).first()
            is None
        )


def test_copy_name_collision_rolls_back(copy_database):
    db = copy_database
    setup = _prepare_multiversion_copy(db)
    command = _copy_command(db, setup)
    collision_ingredient, collision_version = uuid4(), uuid4()
    with db.engine.begin() as connection:
        connection.execute(
            insert(Ingredient).values(
                id=collision_ingredient,
                organization_id=db.destination,
                current_version_id=collision_version,
                created_by_user_id=db.actor,
            )
        )
        connection.execute(
            insert(IngredientVersion).values(
                id=collision_version,
                organization_id=db.destination,
                ingredient_id=collision_ingredient,
                name="Carrot (historical)",
                normalized_name="carrot (historical)",
                canonical_unit_id=db.unit,
                mass_per_canonical_quantity=1,
                published_by_user_id=db.actor,
            )
        )
    with pytest.raises(ApplicationServiceError, match="validation_failed"):
        asyncio.run(copy_ingredient_to_organization(db.sessions, context(db), command))
    with db.engine.connect() as connection:
        assert connection.execute(
            select(Ingredient.id).where(Ingredient.organization_id == db.destination)
        ).all() == [(collision_ingredient,)]
