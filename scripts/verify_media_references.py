#!/usr/bin/env python3
"""Verify READY receipt attachment references against restored local media."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import stat
import subprocess
from contextlib import suppress
from pathlib import Path, PurePosixPath

_QUERY = """
SELECT storage_object_key, thumbnail_object_key, byte_size::text,
       encode(content_hash, 'hex')
FROM receipt_attachments
WHERE storage_state = 'ready'
ORDER BY id
"""
_NULL = "[COOKOPS_NULL]"


def _safe_key(key: str, directory: str) -> PurePosixPath:
    if not key or "\\" in key:
        raise ValueError(f"unsafe {directory} storage key")
    path = PurePosixPath(key)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe {directory} storage key: {key!r}")
    if not path.parts or path.parts[0] != directory or len(path.parts) != 2:
        raise ValueError(f"unexpected {directory} storage key: {key!r}")
    return path


def _read_rows(output: str) -> list[tuple[str, str, int, str]]:
    rows = list(csv.reader(io.StringIO(output), strict=True))
    parsed: list[tuple[str, str, int, str]] = []
    for row in rows:
        if not row:
            continue
        if len(row) != 4 or any(value == _NULL for value in row):
            raise ValueError("psql returned a malformed receipt attachment row")
        object_key, thumbnail_key, size_text, digest = row
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ValueError("receipt attachment byte_size is not an integer") from exc
        if size <= 0 or len(digest) != 64:
            raise ValueError("receipt attachment metadata is invalid")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("receipt attachment content_hash is not hexadecimal") from exc
        parsed.append((object_key, thumbnail_key, size, digest.lower()))
    return parsed


def _open_regular(root: Path, key: str, directory: str) -> int:
    relative = _safe_key(key, directory)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd: int | None = None
    file_fd: int | None = None
    returned = False
    try:
        current_fd = os.open(root, directory_flags)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError(f"media reference is not a safe regular file: {key}")
        returned = True
        return file_fd
    except OSError as exc:
        raise ValueError(f"media reference is not a safe regular file: {key}") from exc
    finally:
        if current_fd is not None:
            with suppress(OSError):
                os.close(current_fd)
        if file_fd is not None and not returned:
            with suppress(OSError):
                os.close(file_fd)


def _verify_object(file_fd: int, path: str, size: int, digest: str) -> None:
    actual_size = 0
    actual_digest = hashlib.sha256()
    try:
        with os.fdopen(file_fd, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                actual_size += len(chunk)
                actual_digest.update(chunk)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"cannot read referenced media object: {path}") from exc
    if actual_size != size or actual_digest.hexdigest() != digest:
        raise ValueError(f"media object metadata mismatch: {path}")


def verify_media_references(*, database_name: str, media_root: Path, psql: str = "psql") -> int:
    command = [
        psql,
        "--dbname",
        database_name,
        "--csv",
        "--tuples-only",
        f"--pset=null={_NULL}",
        "--command",
        _QUERY,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot query restored receipt attachment metadata") from exc
    rows = _read_rows(result.stdout)
    root = media_root.absolute()
    for object_key, thumbnail_key, size, digest in rows:
        object_fd = _open_regular(root, object_key, "objects")
        _verify_object(object_fd, object_key, size, digest)
        thumbnail_fd = _open_regular(root, thumbnail_key, "thumbnails")
        with suppress(OSError):
            os.close(thumbnail_fd)
    return len(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--psql", default="psql")
    return parser


def main() -> None:
    count = verify_media_references(**vars(_parser().parse_args()))
    print(f"verified {count} READY receipt attachment media references")


if __name__ == "__main__":
    main()
