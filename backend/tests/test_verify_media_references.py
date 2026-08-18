from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[2] / "scripts" / "verify_media_references.py"
_SPEC = importlib.util.spec_from_file_location("verify_media_references", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)


def _output(
    key: str = "objects/attachment-1",
    thumbnail: str = "thumbnails/attachment-1",
    data: bytes = b"photo",
) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f'"{key}","{thumbnail}",{len(data)},{digest}\n'


def test_verifies_ready_object_and_thumbnail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "objects").mkdir()
    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "objects" / "attachment-1").write_bytes(b"photo")
    (tmp_path / "thumbnails" / "attachment-1").write_bytes(b"thumb")
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda command, **kwargs: type("Result", (), {"stdout": _output()})(),
    )

    assert verify.verify_media_references(database_name="cookops", media_root=tmp_path) == 1


@pytest.mark.parametrize(
    ("data", "expected"),
    [(b"tampered", "metadata mismatch"), (b"", "metadata mismatch")],
)
def test_rejects_missing_or_tampered_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes, expected: str
) -> None:
    (tmp_path / "objects").mkdir()
    (tmp_path / "thumbnails").mkdir()
    if data:
        (tmp_path / "objects" / "attachment-1").write_bytes(data)
    (tmp_path / "thumbnails" / "attachment-1").write_bytes(b"thumb")
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda command, **kwargs: type("Result", (), {"stdout": _output(data=b"photo")})(),
    )

    with pytest.raises(ValueError, match=expected if data else "safe regular file"):
        verify.verify_media_references(database_name="cookops", media_root=tmp_path)


@pytest.mark.parametrize(
    "output",
    [
        '"../outside","thumbnails/attachment-1",5,abc\n',
        '"objects/attachment-1","thumbnails/attachment-1",nope,abc\n',
        '"objects/attachment-1",5,abc\n',
    ],
)
def test_rejects_unsafe_or_malformed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    (tmp_path / "objects").mkdir()
    (tmp_path / "thumbnails").mkdir()
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda command, **kwargs: type("Result", (), {"stdout": output})(),
    )

    with pytest.raises(ValueError):
        verify.verify_media_references(database_name="cookops", media_root=tmp_path)


def test_rejects_symlinked_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "objects").mkdir()
    (tmp_path / "thumbnails").mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"photo")
    (tmp_path / "objects" / "attachment-1").symlink_to(outside)
    (tmp_path / "thumbnails" / "attachment-1").write_bytes(b"thumb")
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda command, **kwargs: type("Result", (), {"stdout": _output()})(),
    )

    with pytest.raises(ValueError, match="safe regular file"):
        verify.verify_media_references(database_name="cookops", media_root=tmp_path)


def test_rejects_symlinked_intermediate_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "attachment-1").write_bytes(b"photo")
    (tmp_path / "objects").symlink_to(outside, target_is_directory=True)
    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "thumbnails" / "attachment-1").write_bytes(b"thumb")
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda command, **kwargs: type("Result", (), {"stdout": _output()})(),
    )

    with pytest.raises(ValueError, match="safe regular file"):
        verify.verify_media_references(database_name="cookops", media_root=tmp_path)
