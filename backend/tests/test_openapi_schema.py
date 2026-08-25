import json
from pathlib import Path

from cookops.config import Environment, HumanAuthProvider, Settings
from cookops.main import create_app


def test_openapi_snapshot_is_current() -> None:
    actual = (
        json.dumps(
            create_app(
                Settings(environment=Environment.TEST, human_auth_provider=HumanAuthProvider.DUMMY)
            ).openapi(),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    assert actual == (Path(__file__).parents[1] / "openapi.json").read_text()
