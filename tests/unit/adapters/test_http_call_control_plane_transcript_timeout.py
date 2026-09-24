import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.domain.voice_call_buffer import VoiceCallBuffer


@pytest.mark.asyncio
async def test_submit_transcript_outlasts_the_main_side_summary():
    """The main side summarizes and flushes before it responds; httpx's 5 s default cut it."""
    client = AsyncMock()
    client.post.return_value = MagicMock()
    adapter = HttpCallControlPlaneAdapter("https://m", lambda: "tok", http_client=client)

    await adapter.submit_transcript(call_id="c1", user_id="u1", account_id="a1", buffer=VoiceCallBuffer(call_id="c1"))

    assert client.post.await_args.kwargs["timeout"] == 60
