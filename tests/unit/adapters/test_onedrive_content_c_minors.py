"""OneDrive content reads, adversarial review C minors (USER_DRIVE_INDEXING_RFC §8.1, §24.5): a pre-authenticated
download URL answering 404/410 means the content is gone (DriveItemNotFoundError, the tick re-observes the item);
a rendition is capped at the drive models' base64-safe vision cap. Mock boundary: aiohttp.ClientSession."""
from unittest.mock import patch

import pytest

from src.adapters.microsoft.onedrive_adapter import THUMBNAIL_MAX_BYTES
from src.domain.drive_image_mime import MAX_VISION_IMAGE_BYTES
from src.domain.user_drive import DriveItemNotFoundError, DriveThumbnailTooLarge
from tests.unit.adapters.test_onedrive_content_wire import _FILE, _adapter, _resp, _session, _thumbs
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")


@pytest.mark.parametrize("status", [404, 410])
async def test_download_url_gone_is_not_found(tmp_path, status):
    session = _session([_resp(_FILE), _resp(status=status)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
        await _adapter().download_to_file(HANDLE, "f1", str(tmp_path / "f"), max_bytes=8)


async def test_download_url_forbidden_stays_a_value_error(tmp_path):
    session = _session([_resp(_FILE), _resp(status=403)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError) as failed:
        await _adapter().download_to_file(HANDLE, "f1", str(tmp_path / "f"), max_bytes=8)
    assert not isinstance(failed.value, DriveItemNotFoundError)


def test_the_rendition_cap_is_the_vision_cap():
    assert THUMBNAIL_MAX_BYTES == MAX_VISION_IMAGE_BYTES == 3 * 1024 * 1024 + 768 * 1024


@pytest.mark.parametrize("size,kept", [(MAX_VISION_IMAGE_BYTES, True), (MAX_VISION_IMAGE_BYTES + 1, False)])
async def test_a_rendition_over_the_vision_cap_raises_too_large(size, kept):
    chunks = [b"x" * (1024 * 1024)] * (size // (1024 * 1024)) + [b"x" * (size % (1024 * 1024))]
    session = _session([_resp(_thumbs("c2048x2048")), _resp(chunks=chunks)])
    with patch("aiohttp.ClientSession", return_value=session):
        if kept:
            data = await _adapter().get_thumbnail(HANDLE, "f1", 2048)
            assert data is not None and len(data) == size
        else:
            with pytest.raises(DriveThumbnailTooLarge):
                await _adapter().get_thumbnail(HANDLE, "f1", 2048)
