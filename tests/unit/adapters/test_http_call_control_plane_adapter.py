import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.domain.voice_call_buffer import VoiceCallBuffer


@pytest.mark.asyncio
async def test_fetch_session_config_sends_oidc_bearer_token():
    fake_response = MagicMock()
    fake_response.json.return_value = {"instructions": "you are Lelik", "user_id": "u1"}
    fake_response.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.post.return_value = fake_response

    adapter = HttpCallControlPlaneAdapter(
        main_service_url="https://main.example.com",
        id_token_provider=lambda: "fake-oidc-token",
        http_client=fake_client,
    )

    config = await adapter.fetch_session_config(ticket="ticket-123")

    assert config == {"instructions": "you are Lelik", "user_id": "u1"}
    fake_client.post.assert_awaited_once()
    call = fake_client.post.await_args
    assert call.args[0] == "https://main.example.com/voice/session-config"
    assert call.kwargs["headers"] == {"Authorization": "Bearer fake-oidc-token"}
    assert call.kwargs["json"] == {"ticket": "ticket-123"}


@pytest.mark.asyncio
async def test_submit_transcript_posts_buffer_payload():
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.post.return_value = fake_response

    adapter = HttpCallControlPlaneAdapter(
        main_service_url="https://main.example.com",
        id_token_provider=lambda: "fake-oidc-token",
        http_client=fake_client,
    )
    buffer = VoiceCallBuffer(call_id="c1", turns=[], usage_by_model={"gpt-realtime-2.1": {"audio_input_tokens": 10}}, transcript_text="hi")

    await adapter.submit_transcript(call_id="c1", user_id="u1", account_id="a1", buffer=buffer)

    call = fake_client.post.await_args
    assert call.args[0] == "https://main.example.com/voice/submit-transcript"
    assert call.kwargs["json"]["call_id"] == "c1"
    assert call.kwargs["json"]["user_id"] == "u1"
    assert call.kwargs["json"]["account_id"] == "a1"
    assert call.kwargs["json"]["usage_by_model"] == {"gpt-realtime-2.1": {"audio_input_tokens": 10}}
