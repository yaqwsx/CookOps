from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from cookops.application.recipes import (
    PublishRecipeVersionCommand,
    RecipeIngredientLineInput,
    _prepare_command,
    _request_hash,
)


def _command(**kwargs: object) -> PublishRecipeVersionCommand:
    version_id = uuid4()
    return PublishRecipeVersionCommand(
        mutation_id=uuid4(),
        recipe_id=uuid4(),
        recipe_version_id=uuid4(),
        based_on_version_id=uuid4(),
        organization_id=uuid4(),
        name="Recipe",
        description=None,
        scaling_unit_id=uuid4(),
        base_scaling_amount=Decimal("1"),
        client_wall_time=datetime.now(UTC),
        recipe_tag_ids=(),
        ingredient_lines=(
            RecipeIngredientLineInput(
                id=uuid4(),
                line_key=uuid4(),
                ingredient_version_id=version_id,
                base_quantity=Decimal("1"),
                position_key="a",
                scaling_behavior="fixed",
                include_in_portion_weight=False,
            ),
        ),
        **kwargs,
    )


def test_recipe_publish_hash_legacy_and_guarded_canonicalization() -> None:
    legacy_command = _command()
    explicit_command = replace(
        legacy_command, catalog_update=False, expected_current_ingredient_versions=()
    )
    legacy, explicit = _prepare_command(legacy_command), _prepare_command(explicit_command)
    assert not legacy.violations and not explicit.violations

    def publish_hash(prepared: object, based_on_version_id: object) -> bytes:
        return _request_hash(
            prepared,  # type: ignore[arg-type]
            command_kind="recipe.publish_version",
            based_on_version_id=based_on_version_id,  # type: ignore[arg-type]
        )

    assert publish_hash(legacy, legacy_command.based_on_version_id) == publish_hash(
        explicit, explicit_command.based_on_version_id
    )
    false_nonempty = _prepare_command(
        replace(explicit_command, expected_current_ingredient_versions=((uuid4(), uuid4()),))
    )
    assert false_nonempty.violations
    assert publish_hash(legacy, legacy_command.based_on_version_id) != publish_hash(
        false_nonempty, explicit_command.based_on_version_id
    )

    ingredient_id, first, second = uuid4(), uuid4(), uuid4()
    other_ingredient = uuid4()
    base = _command(
        catalog_update=True,
        expected_current_ingredient_versions=((ingredient_id, first), (other_ingredient, second)),
    )
    reversed_pairs = _prepare_command(base)
    assert not reversed_pairs.violations
    reordered = _prepare_command(
        replace(
            base,
            expected_current_ingredient_versions=tuple(
                reversed(base.expected_current_ingredient_versions)
            ),
        )
    )
    assert publish_hash(reversed_pairs, base.based_on_version_id) == publish_hash(
        reordered, base.based_on_version_id
    )
    changed = _prepare_command(
        replace(
            base,
            expected_current_ingredient_versions=(
                (ingredient_id, uuid4()),
                (other_ingredient, second),
            ),
        )
    )
    assert publish_hash(reversed_pairs, base.based_on_version_id) != publish_hash(
        changed, base.based_on_version_id
    )
