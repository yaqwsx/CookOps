from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

_MODULE_PATH = Path(__file__).parents[2] / "scripts" / "restore_archive.py"
_SPEC = importlib.util.spec_from_file_location("restore_archive", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
restore = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(restore)


def _archive(tmp_path: Path, *, dump: bytes = b"dump", photo: bytes = b"photo") -> Path:
    files = {
        "database.dump": ("database", dump),
        "media/objects/receipt-1": ("objects", photo),
    }
    manifest = {
        "schema_version": "schema-1",
        "application_revision": "rev-1",
        "created_at": "2026-08-18T12:00:00Z",
        "file_count": len(files),
        "expected_restore_procedure": "restore",
        "files": [
            {
                "path": path,
                "kind": kind,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for path, (kind, data) in files.items()
        ],
    }
    output = tmp_path / "backup.zip"
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path, (_, data) in files.items():
            archive.writestr(path, data)
        archive.writestr("manifest.json", json.dumps(manifest))
    return output


def test_valid_restore_stages_media_and_reports_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    target = tmp_path / "new-media"
    calls: list[list[str]] = []

    def fake_restore(command: list[str], check: bool) -> None:
        assert check is True
        calls.append(command)

    monkeypatch.setattr(restore.subprocess, "run", fake_restore)
    report = restore.restore_archive(
        archive=archive,
        database_url="postgresql://example",
        pg_restore="fake-pg-restore",
        media_root=target,
    )

    assert report == restore.RestoreReport("schema-1", "rev-1")
    assert (target / "objects" / "receipt-1").read_bytes() == b"photo"
    assert calls[0][:-1] == [
        "fake-pg-restore",
        "--dbname",
        "postgresql://example",
        "--exit-on-error",
    ]
    assert calls[0][-1].endswith("/database.dump")


def test_clean_database_passes_exact_pg_restore_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(restore.subprocess, "run", lambda command, **_: calls.append(command))
    restore.restore_archive(
        archive=_archive(tmp_path),
        database_name="cookops",
        pg_restore="fake-pg-restore",
        media_root=tmp_path / "new-media",
        clean_database=True,
    )
    assert calls[0][:-1] == [
        "fake-pg-restore",
        "--dbname",
        "cookops",
        "--clean",
        "--if-exists",
        "--exit-on-error",
    ]


def test_payload_validation_streams_without_zip_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path, photo=b"photo" * 500_000)
    original_read = ZipFile.read

    def guard(
        self: ZipFile,
        name: str | ZipInfo,
        pwd: bytes | None = None,
    ) -> bytes:
        member = name if isinstance(name, str) else name.filename
        if member != "manifest.json":
            raise AssertionError("payload validation must stream with ZipFile.open")
        return original_read(self, name, pwd)

    monkeypatch.setattr(ZipFile, "read", guard)
    monkeypatch.setattr(restore.subprocess, "run", lambda *_args, **_kwargs: None)
    restore.restore_archive(
        archive=archive,
        database_url="postgresql://example",
        pg_restore="fake-pg-restore",
        media_root=tmp_path / "new-media",
    )


def test_declared_size_mismatch_is_rejected_before_restore_or_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    with ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(members["manifest.json"])
    manifest["files"][0]["size"] += 1
    members["manifest.json"] = json.dumps(manifest).encode()
    with ZipFile(archive, "w", ZIP_DEFLATED) as target_archive:
        for name, data in members.items():
            target_archive.writestr(name, data)
    called = False

    def fail(*_: object, **__: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(restore.subprocess, "run", fail)
    target = tmp_path / "new-media"
    with pytest.raises(ValueError, match="declared size"):
        restore.restore_archive(
            archive=archive,
            database_url="postgresql://example",
            pg_restore="fake-pg-restore",
            media_root=target,
        )
    assert not called
    assert not target.exists()


def test_oversized_declared_member_is_rejected_before_restore_or_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    monkeypatch.setattr(restore, "MAX_MEMBER_UNCOMPRESSED_BYTES", 3)
    monkeypatch.setattr(restore.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("restore"))
    target = tmp_path / "new-media"
    with pytest.raises(ValueError, match="member exceeds"):
        restore.restore_archive(
            archive=archive,
            database_url="postgresql://example",
            pg_restore="fake-pg-restore",
            media_root=target,
        )
    assert not target.exists()


def test_oversized_aggregate_is_rejected_before_restore_or_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    monkeypatch.setattr(restore, "MAX_MEMBER_UNCOMPRESSED_BYTES", 10_000)
    monkeypatch.setattr(restore, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 10)
    monkeypatch.setattr(restore.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("restore"))
    target = tmp_path / "new-media"
    with pytest.raises(ValueError, match="aggregate"):
        restore.restore_archive(
            archive=archive,
            database_url="postgresql://example",
            pg_restore="fake-pg-restore",
            media_root=target,
        )
    assert not target.exists()


def test_tampered_archive_is_rejected_before_restore_or_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    with ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}
    members["media/objects/receipt-1"] = b"tampered"
    with ZipFile(archive, "w", ZIP_DEFLATED) as target_archive:
        for name, data in members.items():
            target_archive.writestr(name, data)
    called = False

    def fail(*_: object, **__: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(restore.subprocess, "run", fail)
    target = tmp_path / "new-media"
    with pytest.raises(ValueError, match="checksum|declared size"):
        restore.restore_archive(
            archive=archive,
            database_url="postgresql://example",
            pg_restore="fake-pg-restore",
            media_root=target,
        )
    assert not called
    assert not target.exists()


@pytest.mark.parametrize(
    "members",
    [
        [("../outside", b"bad"), ("manifest.json", b"{}")],
        [("database.dump", b"a"), ("database.dump", b"b")],
    ],
)
def test_unsafe_or_duplicate_zip_members_are_rejected(
    tmp_path: Path, members: list[tuple[str, bytes]]
) -> None:
    archive = tmp_path / "bad.zip"
    with ZipFile(archive, "w") as output:
        for name, data in members:
            output.writestr(name, data)
    with pytest.raises(ValueError):
        restore.restore_archive(
            archive=archive,
            database_url="postgresql://example",
            pg_restore="fake-pg-restore",
            media_root=tmp_path / "new-media",
        )


def test_nonempty_media_target_requires_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    target = tmp_path / "media"
    target.mkdir()
    (target / "keep").write_bytes(b"keep")
    monkeypatch.setattr(restore.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("restore"))
    with pytest.raises(ValueError, match="non-empty"):
        restore.restore_archive(
            archive=archive,
            database_url="postgresql://example",
            pg_restore="fake-pg-restore",
            media_root=target,
        )
    assert (target / "keep").read_bytes() == b"keep"
