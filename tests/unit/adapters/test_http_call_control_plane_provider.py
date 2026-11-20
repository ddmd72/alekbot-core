"""submit_transcript names the call's realtime provider only when it is given."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.domain.voice_call_buffer import VoiceCallBuffer


async def _payload(**extra):
    client = AsyncMock()
    client.post.return_value = MagicMock()
    adapter = HttpCallControlPlaneAdapter("https://main.example.com", lambda: "tok", http_client=client)
    await adapter.submit_transcript(call_id="c1", user_id="u1", account_id="acc-1",
                                    buffer=VoiceCallBuffer(call_id="c1", turns=[], usage_by_model={}, transcript_text=""),
                                    **extra)
    return client.post.await_args.kwargs["json"]


@pytest.mark.asyncio
async def test_the_report_carries_the_provider():
    assert (await _payload(provider="xai"))["provider"] == "xai"


@pytest.mark.asyncio
async def test_no_provider_adds_no_field():
    assert "provider" not in await _payload()
