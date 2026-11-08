"""submit_transcript carries the resume handle and its provider only when the call has one."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.domain.voice_call_buffer import VoiceCallBuffer


async def _payload(**extra):
    client = AsyncMock()
    client.post.return_value = MagicMock()
    adapter = HttpCallControlPlaneAdapter("https://main.example.com", lambda: "tok", http_client=client)
    await adapter.submit_transcript(call_id="c1", user_id="usr-1", account_id="acc-1",
                                    buffer=VoiceCallBuffer(call_id="c1", turns=[], usage_by_model={}, transcript_text=""),
                                    **extra)
    return client.post.await_args.kwargs["json"]


@pytest.mark.asyncio
async def test_handle_and_provider_ride_the_payload():
    payload = await _payload(resume_handle="conv-0001-invented", resume_provider="xai")

    assert payload["resume_handle"] == "conv-0001-invented" and payload["resume_provider"] == "xai"


@pytest.mark.asyncio
async def test_without_a_handle_the_payload_has_neither_field():
    payload = await _payload()

    assert "resume_handle" not in payload and "resume_provider" not in payload
