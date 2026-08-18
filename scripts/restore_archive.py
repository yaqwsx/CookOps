#!/usr/bin/env python3
"""Validate and restore an operator-only CookOps backup archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections import namedtuple
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile, ZipInfo

_EXPECTED_MANIFEST_KEYS = {
    "schema_version",
    "application_revision",
    "created_at",
    "file_count",
    "expected_restore_procedure",
    "files",
}
_EXPECTED_FILE_KEYS = {"path", "kind", "size", "sha256"}
_MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMBER_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024


RestoreReport = namedtuple("RestoreReport", ("schema_version", "application_revision"))


def _safe_member(name: str) -> str:
    if "\\" in name:
        raise ValueError(f"unsafe ZIP member path: {name}")
    member = PurePosixPath(name)
    if member.is_absolute() or any(part in ("", ".", "..") for part in member.parts):
        raise ValueError(f"unsafe ZIP member path: {name}")
    return member.as_posix()


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("manifest created_at must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("manifest created_at must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("manifest created_at must be an ISO-8601 UTC timestamp")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _regular_zip_member(info: ZipInfo, name: str) -> None:
    # ZIP external attributes can mark a member as a Unix symlink or directory.
    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(mode) or stat.S_ISDIR(mode) or name.endswith("/"):
        raise ValueError(f"ZIP member is not a regular file: {name}")


def _parse_manifest(raw: bytes) -> tuple[dict[str, Any], RestoreReport]:
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest.json is too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("manifest.json is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _EXPECTED_MANIFEST_KEYS:
        raise ValueError("manifest.json has an unexpected schema")
    schema = value["schema_version"]
    revision = value["application_revision"]
    if not isinstance(schema, str) or not schema.strip():
        raise ValueError("manifest schema_version must be a non-empty string")
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("manifest application_revision must be a non-empty string")
    _utc_timestamp(value["created_at"])
    if not isinstance(value["expected_restore_procedure"], str) or not value[
        "expected_restore_procedure"
    ].strip():
        raise ValueError("manifest expected_restore_procedure must be a non-empty string")
    files = value["files"]
    count = value["file_count"]
    if not isinstance(files, list) or not files:
        raise ValueError("manifest files must be a non-empty list")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(files):
        raise ValueError("manifest file_count does not match files")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != _EXPECTED_FILE_KEYS:
            raise ValueError("manifest file entry has an unexpected schema")
        path = item["path"]
        if not isinstance(path, str):
            raise ValueError("manifest file path must be a string")
        path = _safe_member(path)
        if path in seen:
            raise ValueError(f"duplicate manifest file path: {path}")
        seen.add(path)
        if path == "database.dump":
            if item["kind"] != "database":
                raise ValueError("database.dump must have kind=database")
        elif path.startswith("media/objects/"):
            if item["kind"] != "objects":
                raise ValueError("object media must have kind=objects")
        elif path.startswith("media/thumbnails/"):
            if item["kind"] != "thumbnails":
                raise ValueError("thumbnail media must have kind=thumbnails")
        else:
            raise ValueError(f"unexpected manifest file path: {path}")
        size = item["size"]
        digest = item["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid file size: {path}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"invalid SHA-256 checksum: {path}")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError(f"invalid SHA-256 checksum: {path}") from exc
        item["path"] = path
    if "database.dump" not in seen:
        raise ValueError("manifest must contain database.dump")
    return value, RestoreReport(schema, revision)


def _validate_zip(zip_file: ZipFile) -> tuple[dict[str, Any], RestoreReport]:
    infos = zip_file.infolist()
    names: list[str] = []
    declared_total = 0
    for info in infos:
        name = _safe_member(info.filename)
        _regular_zip_member(info, name)
        if info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
            raise ValueError(f"ZIP member exceeds uncompressed size limit: {name}")
        declared_total += info.file_size
        names.append(name)
    if declared_total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError("backup ZIP exceeds aggregate uncompressed size limit")
    if len(names) != len(set(names)):
        raise ValueError("backup ZIP contains duplicate members")
    if "manifest.json" not in names:
        raise ValueError("backup ZIP is missing manifest.json")
    manifest_info = zip_file.getinfo("manifest.json")
    if manifest_info.file_size > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest.json is too large")
    raw_manifest = zip_file.read(manifest_info)
    if len(raw_manifest) != manifest_info.file_size:
        raise ValueError("manifest.json size verification failed")
    manifest, report = _parse_manifest(raw_manifest)
    entries = {entry["path"]: entry for entry in manifest["files"]}
    expected = set(entries) | {"manifest.json"}
    if set(names) != expected:
        raise ValueError("backup ZIP members do not match manifest")
    actual_total = len(raw_manifest)
    for path, entry in entries.items():
        info = zip_file.getinfo(path)
        if info.file_size != entry["size"]:
            raise ValueError(f"declared size verification failed: {path}")
        digest = hashlib.sha256()
        size = 0
        with zip_file.open(info) as payload:
            for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                size += len(chunk)
                actual_total += len(chunk)
                if size > MAX_MEMBER_UNCOMPRESSED_BYTES:
                    raise ValueError(f"ZIP member exceeds uncompressed size limit: {path}")
                if actual_total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ValueError("backup ZIP exceeds aggregate uncompressed size limit")
                digest.update(chunk)
        if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise ValueError(f"checksum verification failed: {path}")
    return manifest, report


def _validate_archive(archive: Path) -> tuple[dict[str, Any], RestoreReport]:
    try:
        with ZipFile(archive) as zip_file:
            return _validate_zip(zip_file)
    except (BadZipFile, OSError) as exc:
        raise ValueError(f"cannot validate backup ZIP: {archive}") from exc


def _ensure_target_is_safe(target: Path, allow_nonempty: bool) -> None:
    if target.is_symlink():
        raise ValueError(f"media target must not be a symlink: {target}")
    if not target.exists():
        return
    if not target.is_dir():
        raise ValueError(f"media target is not a directory: {target}")
    if any(target.iterdir()) and not allow_nonempty:
        raise ValueError("media target is non-empty; pass --allow-nonempty to replace it")


def _extract_media(zip_file: ZipFile, manifest: dict[str, Any], stage: Path) -> Path:
    media = stage / "media"
    (media / "objects").mkdir(parents=True, mode=0o700)
    (media / "thumbnails").mkdir(mode=0o700)
    for entry in manifest["files"]:
        path = entry["path"]
        destination = stage / path
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                with zip_file.open(path) as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            raise
    return media


def _publish_media(stage: Path, target: Path) -> None:
    parent = target.parent
    if not parent.is_dir():
        raise ValueError(f"media target parent does not exist: {parent}")
    old: Path | None = None
    if target.exists():
        old = Path(tempfile.mkdtemp(prefix=f".{target.name}.old-", dir=parent))
        old.rmdir()
        os.replace(target, old)
    try:
        os.replace(stage, target)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if old is not None and old.exists():
            os.replace(old, target)
        raise
    if old is not None:
        shutil.rmtree(old)


def restore_archive(
    *,
    archive: Path,
    database_url: str,
    pg_restore: str,
    media_root: Path,
    allow_nonempty: bool = False,
) -> RestoreReport:
    archive = archive.absolute()
    media_root = media_root.absolute()
    with ZipFile(archive) as zip_file:
        manifest, report = _validate_zip(zip_file)
        _ensure_target_is_safe(media_root, allow_nonempty)
        parent = media_root.parent
        if not parent.is_dir():
            raise ValueError(f"media target parent does not exist: {parent}")
        staging = Path(tempfile.mkdtemp(prefix=f".{media_root.name}.restore-", dir=parent))
        staging.chmod(0o700)
        try:
            staged_media = _extract_media(zip_file, manifest, staging)
            dump = staging / "database.dump"
            subprocess.run(
                [pg_restore, "--dbname", database_url, "--exit-on-error", str(dump)],
                check=True,
            )
            _publish_media(staged_media, media_root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--pg-restore", default="pg_restore")
    parser.add_argument(
        "--media-root", "--new-media-root", dest="media_root", type=Path, required=True
    )
    parser.add_argument("--allow-nonempty", action="store_true")
    return parser


def main() -> None:
    report = restore_archive(**vars(_parser().parse_args()))
    print(
        f"restored application_revision={report.application_revision} "
        f"schema_version={report.schema_version}"
    )


if __name__ == "__main__":
    main()
