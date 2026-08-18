#!/usr/bin/env python3
"""Create and verify an operator-only CookOps backup archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

_RESTORE_PROCEDURE = (
    "Validate manifest and SHA-256 checksums; restore database.dump with pg_restore; "
    "copy media/objects and media/thumbnails into a new receipt-media root; run "
    "compatibility migrations and verify database/media references."
)


def _safe_member(path: str) -> str:
    member = PurePosixPath(path)
    if member.is_absolute() or any(part in ("", ".", "..") for part in member.parts):
        raise ValueError(f"unsafe ZIP member path: {path}")
    return member.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_regular_file(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"symlink is not allowed in media root: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not path.is_file():
        raise ValueError(f"media path escapes the configured root: {path}")


def _open_directory(path: str | Path, *, dir_fd: int | None = None) -> int:
    try:
        return os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        raise ValueError(f"media directory is not a real directory: {path}") from exc


def _copy_media_file(source_fd: int, staged: Path) -> None:
    source_stat = os.fstat(source_fd)
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError(f"media path is not a regular file: {staged}")
    target_fd = os.open(
        staged,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(source_fd, "rb") as source, os.fdopen(target_fd, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        with suppress(OSError):
            os.close(target_fd)
        raise


def _collect_media_directory(
    directory_fd: int,
    *,
    destination: Path,
    relative: PurePosixPath,
    kind: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with os.scandir(directory_fd) as directory:
        for entry in sorted(directory, key=lambda item: item.name):
            if entry.is_symlink():
                raise ValueError(f"symlink is not allowed in media root: {relative / entry.name}")
            entry_relative = relative / entry.name
            if entry.is_dir(follow_symlinks=False):
                child_fd = _open_directory(entry.name, dir_fd=directory_fd)
                try:
                    entries.extend(
                        _collect_media_directory(
                            child_fd,
                            destination=destination,
                            relative=entry_relative,
                            kind=kind,
                        )
                    )
                finally:
                    os.close(child_fd)
                continue
            source_fd = os.open(
                entry.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                    raise ValueError(f"media path is not a regular file: {entry_relative}")
                member = _safe_member(f"media/{entry_relative}")
                staged = destination / member
                staged.parent.mkdir(parents=True, exist_ok=True)
                _copy_media_file(source_fd, staged)
            except BaseException:
                with suppress(OSError):
                    os.close(source_fd)
                raise
            entries.append(
                {
                    "path": member,
                    "kind": kind,
                    "size": staged.stat().st_size,
                    "sha256": _sha256(staged),
                }
            )
    return entries


def _collect_media(root: Path, destination: Path) -> list[dict[str, Any]]:
    root = root.absolute()
    root_fd = _open_directory(root)
    entries: list[dict[str, Any]] = []
    try:
        for kind in ("objects", "thumbnails"):
            try:
                source_fd = os.open(
                    kind,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError(f"media directory is not a real directory: {root / kind}") from exc
            try:
                entries.extend(
                    _collect_media_directory(
                        source_fd,
                        destination=destination,
                        relative=PurePosixPath(kind),
                        kind=kind,
                    )
                )
            finally:
                os.close(source_fd)
    finally:
        os.close(root_fd)
    return entries


def _validated_created_at(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("created_at must be an ISO-8601 UTC timestamp")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _verify_files(files: list[dict[str, Any]], root: Path) -> None:
    for entry in files:
        path = root / entry["path"]
        _assert_regular_file(path, root)
        if path.stat().st_size != entry["size"] or _sha256(path) != entry["sha256"]:
            raise ValueError(f"checksum verification failed: {entry['path']}")


def create_backup(
    *,
    database_url: str,
    pg_dump: str,
    media_root: Path,
    output: Path,
    application_revision: str,
    schema_version: str,
    created_at: str | None = None,
) -> None:
    output = output.absolute()
    output_parent = output.parent
    if not output_parent.is_dir():
        raise ValueError(f"output directory does not exist: {output_parent}")
    created = _validated_created_at(created_at)

    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output_parent))
    temporary.chmod(0o700)
    try:
        dump = temporary / "database.dump"
        subprocess.run(
            [pg_dump, "--dbname", database_url, "--format=custom", "--file", str(dump)],
            check=True,
        )
        _assert_regular_file(dump, temporary)
        files: list[dict[str, Any]] = [
            {
                "path": "database.dump",
                "kind": "database",
                "size": dump.stat().st_size,
                "sha256": _sha256(dump),
            }
        ]
        files.extend(_collect_media(media_root, temporary))
        _verify_files(files, temporary)
        manifest = {
            "schema_version": schema_version,
            "application_revision": application_revision,
            "created_at": created,
            "file_count": len(files),
            "expected_restore_procedure": _RESTORE_PROCEDURE,
            "files": files,
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        archive = temporary / f"{output.name}.tmp"
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
            for entry in files:
                zip_file.write(temporary / entry["path"], entry["path"])
            zip_file.write(manifest_path, "manifest.json")
        os.chmod(archive, 0o600)
        with archive.open("rb") as source:
            os.fsync(source.fileno())
        os.replace(archive, output)
        parent_fd = os.open(output_parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--pg-dump", default="pg_dump")
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--application-revision", required=True)
    parser.add_argument("--schema-version", required=True)
    parser.add_argument(
        "--created-at", help="UTC ISO-8601 timestamp, useful for reproducible tests"
    )
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    create_backup(**vars(arguments))


if __name__ == "__main__":
    main()
