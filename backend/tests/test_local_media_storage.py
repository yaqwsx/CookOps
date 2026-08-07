import io
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from cookops.media_storage import InvalidReceiptImage, LocalReceiptMediaStorage


def jpeg(width: int = 3, height: int = 2) -> bytes:
    image = Image.new("RGB", (width, height))
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def test_staged_image_is_measured_then_atomically_made_private(tmp_path: Path) -> None:
    storage = LocalReceiptMediaStorage(tmp_path)
    staged = storage.stage(storage.new_stage_path(), [jpeg()], 2_000_000)

    assert (staged.media_type, staged.width, staged.height, staged.byte_size) == (
        "image/jpeg",
        3,
        2,
        staged.byte_size,
    )
    key, thumbnail_key = storage.promote(staged, uuid4())
    with storage.open(key) as source:
        assert source.read().startswith(b"\xff\xd8")
    with storage.open(thumbnail_key) as source:
        assert source.read().startswith(b"\xff\xd8")
    with pytest.raises(FileNotFoundError):
        storage.open("../outside")


@pytest.mark.parametrize("payload", [b"", b"not an image"])
def test_staging_fuzz_rejects_invalid_or_oversized_images_without_leaving_bytes(
    tmp_path: Path, payload: bytes
) -> None:
    storage = LocalReceiptMediaStorage(tmp_path)
    path = storage.new_stage_path()

    with pytest.raises(InvalidReceiptImage):
        storage.stage(path, [payload], 2_000_000)
    assert not path.exists()


def test_staging_rejects_a_stream_that_crosses_its_byte_bound(tmp_path: Path) -> None:
    storage = LocalReceiptMediaStorage(tmp_path)
    path = storage.new_stage_path()

    with pytest.raises(InvalidReceiptImage):
        storage.stage(path, [jpeg(), b"too much"], len(jpeg()))
    assert not path.exists()
