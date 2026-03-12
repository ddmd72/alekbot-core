"""
Unit tests for SlackMediaAdapter.upload_file().

Covers:
- Successful upload — files_upload_v2 called with correct args.
- Upload failure — exception propagated.
"""
import pytest
from unittest.mock import AsyncMock

from src.adapters.slack.media_adapter import SlackMediaAdapter


_FAKE_BYTES = b"PK\x03\x04fake-docx"
_CHANNEL_ID = "D0123456789"


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.files_upload_v2.return_value = {"ok": True}
    return client


@pytest.fixture
def adapter(mock_client):
    return SlackMediaAdapter(app_client=mock_client, bot_token="xoxb-test")


class TestUploadFile:

    async def test_calls_files_upload_v2_with_correct_channel(self, adapter, mock_client):
        await adapter.upload_file(
            file_bytes=_FAKE_BYTES, filename="doc.docx", title="Doc", channel_id=_CHANNEL_ID
        )
        mock_client.files_upload_v2.assert_called_once()
        call_kwargs = mock_client.files_upload_v2.call_args.kwargs
        assert call_kwargs["channel"] == _CHANNEL_ID
        assert call_kwargs["filename"] == "doc.docx"

    async def test_upload_failure_raises(self, adapter, mock_client):
        mock_client.files_upload_v2.side_effect = Exception("upload failed")
        with pytest.raises(Exception, match="upload failed"):
            await adapter.upload_file(
                file_bytes=_FAKE_BYTES, filename="doc.docx", title="Doc", channel_id=_CHANNEL_ID
            )
