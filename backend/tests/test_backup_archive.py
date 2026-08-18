from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast
from zipfile import ZipFile

import pytest

_MODULE_PATH = Path(__file__).parents[2] / "scripts" / "backup_archive.py"
_SPEC = importlib.util.spec_from_file_location("backup_archive", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
backup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup)


def _create_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    media = tmp_path / "media"
    (media / "objects").mkdir(parents=True)
    (media / "thumbnails").mkdir()
    (media / "objects" / "receipt-1").write_bytes(b"photo")
    (media / "thumbnails" / "receipt-1").write_bytes(b"thumb")
    output = tmp_path / "backup.zip"
    events: list[str] = []

    def dump(command: list[str], check: bool) -> None:
        assert check is True
        assert command[:2] == ["fake-pg-dump", "--dbname"]
        events.append("dump")
        Path(command[command.index("--file") + 1]).write_bytes(b"dump")

    monkeypatch.setattr(backup.subprocess, "run", dump)
    collect = backup._collect_media

    def collect_after_dump(root: Path, destination: Path) -> list[dict[str, object]]:
        events.append("media")
        return cast(list[dict[str, Any]], collect(root, destination))

    monkeypatch.setattr(backup, "_collect_media", collect_after_dump)
    backup.create_backup(
        database_url="postgresql://example",
        pg_dump="fake-pg-dump",
        media_root=media,
        output=output,
        application_revision="rev-1",
        schema_version="schema-1",
        created_at="2026-08-18T12:00:00Z",
    )
    assert events == ["dump", "media"]
    return output


def test_backup_layout_manifest_and_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _create_backup(tmp_path, monkeypatch)
    assert os.stat(output).st_mode & 0o777 == 0o600
    with ZipFile(output) as archive:
        assert archive.namelist() == [
            "database.dump",
            "media/objects/receipt-1",
            "media/thumbnails/receipt-1",
            "manifest.json",
        ]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == "schema-1"
        assert manifest["application_revision"] == "rev-1"
        assert manifest["created_at"] == "2026-08-18T12:00:00Z"
        assert manifest["expected_restore_procedure"]
        assert manifest["file_count"] == 3
        assert {entry["path"] for entry in manifest["files"]} == set(archive.namelist()[:3])
        for entry in manifest["files"]:
            assert len(entry["sha256"]) == 64
            assert entry["size"] == len(archive.read(entry["path"]))


def test_failed_dump_does_not_publish_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: object, **__: object) -> None:
        raise subprocess.CalledProcessError(1, "fake-pg-dump")

    monkeypatch.setattr(backup.subprocess, "run", fail)
    output = tmp_path / "backup.zip"
    with pytest.raises(subprocess.CalledProcessError):
        backup.create_backup(
            database_url="postgresql://example",
            pg_dump="fake-pg-dump",
            media_root=tmp_path,
            output=output,
            application_revision="rev-1",
            schema_version="schema-1",
        )
    assert not output.exists()


def test_failed_checksum_does_not_publish_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda command, check: Path(command[-1]).write_bytes(b"dump"),
    )
    monkeypatch.setattr(
        backup,
        "_verify_files",
        lambda *_: (_ for _ in ()).throw(ValueError("bad checksum")),
    )
    output = tmp_path / "backup.zip"
    with pytest.raises(ValueError, match="bad checksum"):
        backup.create_backup(
            database_url="postgresql://example",
            pg_dump="fake-pg-dump",
            media_root=tmp_path,
            output=output,
            application_revision="rev-1",
            schema_version="schema-1",
        )
    assert not output.exists()


def test_media_symlink_file_is_rejected(tmp_path: Path) -> None:
    media = tmp_path / "media"
    (media / "objects").mkdir(parents=True)
    (media / "thumbnails").mkdir()
    target = tmp_path / "outside"
    target.write_bytes(b"secret")
    (media / "objects" / "linked").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        backup._collect_media(media, tmp_path / "stage")


def test_media_symlink_directory_is_rejected(tmp_path: Path) -> None:
    media = tmp_path / "media"
    (media / "objects").mkdir(parents=True)
    (media / "thumbnails").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (media / "objects" / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        backup._collect_media(media, tmp_path / "stage")


@pytest.mark.parametrize(
    "created_at",
    ["2026-08-18T12:00:00", "2026-08-18T12:00:00+02:00", "not-a-timestamp"],
)
def test_created_at_must_be_utc_iso8601(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created_at: str
) -> None:
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda command, check: Path(command[-1]).write_bytes(b"dump"),
    )
    with pytest.raises(ValueError, match="ISO-8601 UTC"):
        backup.create_backup(
            database_url="postgresql://example",
            pg_dump="fake-pg-dump",
            media_root=tmp_path,
            output=tmp_path / "backup.zip",
            application_revision="rev-1",
            schema_version="schema-1",
            created_at=created_at,
        )
    assert not (tmp_path / "backup.zip").exists()
