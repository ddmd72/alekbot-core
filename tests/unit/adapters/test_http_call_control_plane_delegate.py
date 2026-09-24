import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter


@pytest.mark.asyncio
async def test_delegate_posts_the_tool_call_and_returns_output():
    response = MagicMock()
    response.json.return_value = {"output": "sunny"}
    client = AsyncMock()
    client.post.return_value = response
    adapter = HttpCallControlPlaneAdapter("https://main.example.com/", lambda: "tok", http_client=client)

    output = await adapter.delegate(user_id="u1", account_id="a1",
                                    arguments={"intent": "search_web", "query": "q"},
                                    call_context=[{"role": "user", "text": "hi"}])

    assert output == "sunny"
    call = client.post.await_args
    assert call.args[0] == "https://main.example.com/voice/delegate"
    assert call.kwargs["json"] == {"user_id": "u1", "account_id": "a1",
                                   "arguments": {"intent": "search_web", "query": "q"},
                                   "call_context": [{"role": "user", "text": "hi"}]}
    assert call.kwargs["headers"] == {"Authorization": "Bearer tok"}
    # Alek takes tens of seconds; httpx's 5 s default would cut every ask_alek.
    assert call.kwargs["timeout"] >= 120


@pytest.mark.asyncio
async def test_delegate_raises_on_http_error_without_retry():
    response = MagicMock()
    response.raise_for_status.side_effect = RuntimeError("500")
    client = AsyncMock()
    client.post.return_value = response
    adapter = HttpCallControlPlaneAdapter("https://m", lambda: "tok", http_client=client)
    with pytest.raises(RuntimeError):
        await adapter.delegate(user_id="u1", account_id="a1", arguments={}, call_context=[])
    client.post.assert_awaited_once()
