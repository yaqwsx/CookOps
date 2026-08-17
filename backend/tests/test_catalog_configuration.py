from datetime import UTC, datetime
from uuid import uuid4

from cookops.application.catalog_configuration import (
    CatalogConfigurationCommand,
    _prepared,
)
from cookops.application.organizations import FieldViolation


def test_unknown_builtin_translation_key_is_rejected() -> None:
    command = CatalogConfigurationCommand(
        mutation_id=uuid4(),
        organization_id=uuid4(),
        entity_id=uuid4(),
        entity_kind="organization_meal_role_preset",
        operation="create",
        client_wall_time=datetime.now(UTC),
        built_in_translation_key="meal_role.brunch",
        position_key="a",
    )

    _, errors = _prepared(command)

    assert errors == (
        FieldViolation("built_in_translation_key", "must_be_supported_translation_key"),
    )
