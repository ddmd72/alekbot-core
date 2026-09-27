"""Wire test: the delegation is named for the main side, and abandoning it is a bounded,
never-raising POST (voice UAT round 1, Task 2)."""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.ports.call_control_plane_port import CallControlPlanePort


def test_port_declares_abandon_delegation():
    assert "abandon_delegation" in CallControlPlanePort.__abstractmethods__


@pytest.mark.asyncio
async def test_delegate_sends_ticket_call_id_and_request_and_outlasts_the_relays_300_s():
    response = MagicMock()
    response.json.return_value = {"output": "done"}
    client = AsyncMock()
    client.post.return_value = response
    adapter = HttpCallControlPlaneAdapter("https://main.example.com/", lambda: "tok", http_client=client)

    output = await adapter.delegate(user_id="u1", account_id="a1", arguments={"intent": "ask_alek"},
                                    call_context=[], ticket="t1", call_id="c1", request="ask_alek: q")

    assert output == "done"
    body = client.post.await_args.kwargs["json"]
    assert (body["ticket"], body["call_id"], body["request"]) == ("t1", "c1", "ask_alek: q")
    assert client.post.await_args.kwargs["timeout"] > 300


@pytest.mark.asyncio
async def test_abandon_posts_the_delegation_id_with_a_short_timeout():
    client = AsyncMock()
    client.post.return_value = MagicMock()
    adapter = HttpCallControlPlaneAdapter("https://main.example.com/", lambda: "tok", http_client=client)

    await adapter.abandon_delegation("t1", "c1")

    call = client.post.await_args
    assert call.args[0] == "https://main.example.com/voice/delegate/abandon"
    assert call.kwargs["json"] == {"ticket": "t1", "call_id": "c1"}
    assert call.kwargs["headers"] == {"Authorization": "Bearer tok"}
    assert call.kwargs["timeout"] == 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "status"])
async def test_abandon_never_raises(failure):
    client = AsyncMock()
    if failure == "transport":
        client.post.side_effect = httpx.ConnectError("down")
    else:
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("500")
        client.post.return_value = response
    adapter = HttpCallControlPlaneAdapter("https://m", lambda: "tok", http_client=client)

    await adapter.abandon_delegation("t1", "c1")  # no exception
    client.post.assert_awaited_once()
