from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def writable_receipt_media_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every ASGI lifespan test out of the production media path."""
    monkeypatch.setenv("COOKOPS_RECEIPT_MEDIA_ROOT", str(tmp_path / "receipts"))
