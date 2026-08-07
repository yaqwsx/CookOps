"""Private local staging for normalized receipt images."""

import hashlib
import io
import os
import secrets
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from PIL import Image, UnidentifiedImageError


class InvalidReceiptImage(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StagedReceiptImage:
    path: Path
    thumbnail_path: Path
    byte_size: int
    content_hash: bytes
    source_byte_size: int
    source_content_hash: bytes
    media_type: str
    width: int
    height: int


class LocalReceiptMediaStorage:
    """Decode untrusted images before making normalized bytes addressable."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        for directory in ("staging", "objects", "thumbnails"):
            (self._root / directory).mkdir(mode=0o700, parents=True, exist_ok=True)

    def new_stage_path(self) -> Path:
        return self._root / "staging" / secrets.token_hex(32)

    def stage(self, path: Path, chunks: Iterable[bytes], maximum_bytes: int) -> StagedReceiptImage:
        path = self._within(path, "staging")
        thumbnail_path = path.with_name(f"{path.name}.thumbnail")
        total = 0
        source_hash = hashlib.sha256()
        try:
            with path.open("xb") as output:
                for chunk in chunks:
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise InvalidReceiptImage("image is too large")
                    output.write(chunk)
                    source_hash.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as decoded:
                    if decoded.width < 1 or decoded.height < 1 or max(decoded.size) > 2000:
                        raise InvalidReceiptImage("invalid image dimensions")
                    decoded.verify()
            with Image.open(path) as decoded:
                if decoded.width < 1 or decoded.height < 1 or max(decoded.size) > 2000:
                    raise InvalidReceiptImage("invalid image dimensions")
                decoded.load()
                image = decoded.convert("RGB")
            encoded = _jpeg(image, maximum_bytes)
            thumbnail = image.copy()
            thumbnail.thumbnail((512, 512))
            thumbnail_encoded = _jpeg(thumbnail, maximum_bytes)
            path.write_bytes(encoded)
            thumbnail_path.write_bytes(thumbnail_encoded)
            return StagedReceiptImage(
                path,
                thumbnail_path,
                len(encoded),
                hashlib.sha256(encoded).digest(),
                total,
                source_hash.digest(),
                "image/jpeg",
                image.width,
                image.height,
            )
        except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError) as error:
            path.unlink(missing_ok=True)
            thumbnail_path.unlink(missing_ok=True)
            raise InvalidReceiptImage("unsupported or invalid image") from error
        except Exception:
            path.unlink(missing_ok=True)
            thumbnail_path.unlink(missing_ok=True)
            raise

    def promote(self, staged: StagedReceiptImage, attachment_id: UUID) -> tuple[str, str]:
        object_key, thumbnail_key = f"objects/{attachment_id}", f"thumbnails/{attachment_id}"
        os.replace(
            self._within(staged.path, "staging"), self._within(self._root / object_key, "objects")
        )
        os.replace(
            self._within(staged.thumbnail_path, "staging"),
            self._within(self._root / thumbnail_key, "thumbnails"),
        )
        return object_key, thumbnail_key

    def discard(self, attachment_id: UUID) -> None:
        for directory in ("objects", "thumbnails"):
            self._within(self._root / directory / str(attachment_id), directory).unlink(
                missing_ok=True
            )

    def open(self, object_key: str) -> BinaryIO:
        path = self._within(self._root / object_key, "objects", "thumbnails")
        return path.open("rb")

    def _within(self, path: Path, *allowed: str) -> Path:
        resolved = path.resolve(strict=False)
        if not any(resolved.is_relative_to(self._root / directory) for directory in allowed):
            raise FileNotFoundError(path)
        return resolved


def _jpeg(image: Image.Image, maximum_bytes: int) -> bytes:
    for quality in range(92, 39, -8):
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        if len(output.getbuffer()) <= maximum_bytes:
            return output.getvalue()
    raise InvalidReceiptImage("normalized image is too large")
