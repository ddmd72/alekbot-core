"""CloudflareSfuAdapter.attach_agent cleanup: close every adapter created before a mid-step
failure, and close nothing if the very first (ingest) call fails."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.cloudflare_sfu_adapter import CloudflareSfuAdapter
from src.ports.media_room_port import MediaRoomError


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = str(body)
    return r


@pytest.mark.asyncio
async def test_egress_failure_closes_the_already_created_ingest_adapter():
    client = AsyncMock()
    client.request.side_effect = [
        _resp(200, {"tracks": [{"adapterId": "A-in", "sessionId": "P1", "trackName": "lelik"}]}),
        _resp(500, {"error": "boom"}),
        _resp(200, {"tracks": []}),
    ]
    with pytest.raises(MediaRoomError):
        await CloudflareSfuAdapter("app1", "s", http_client=client).attach_agent(
            "S1", "wss://r/sfu/ingest?ticket=t", "wss://r/sfu/egress?ticket=t"
        )

    calls = client.request.await_args_list
    close_call = calls[-1]
    assert close_call.args == ("POST", "https://rtc.live.cloudflare.com/v1/apps/app1/adapters/websocket/close")
    assert close_call.kwargs["json"] == {"tracks": [{"adapterId": "A-in"}]}


@pytest.mark.asyncio
async def test_ingest_failure_closes_nothing():
    client = AsyncMock()
    client.request.side_effect = [_resp(500, {"error": "boom"})]
    with pytest.raises(MediaRoomError):
        await CloudflareSfuAdapter("app1", "s", http_client=client).attach_agent(
            "S1", "wss://r/sfu/ingest?ticket=t", "wss://r/sfu/egress?ticket=t"
        )

    assert client.request.await_count == 1


@pytest.mark.asyncio
async def test_egress_track_error_in_a_2xx_body_closes_the_ingest_adapter():
    client = AsyncMock()
    client.request.side_effect = [
        _resp(200, {"tracks": [{"adapterId": "A-in", "sessionId": "P1", "trackName": "lelik"}]}),
        _resp(200, {"tracks": [{"errorCode": "invalid_endpoint", "errorDescription": "nope"}]}),
        _resp(200, {"tracks": []}),
    ]
    with pytest.raises(MediaRoomError):
        await CloudflareSfuAdapter("app1", "s", http_client=client).attach_agent(
            "S1", "wss://r/sfu/ingest?ticket=t", "wss://r/sfu/egress?ticket=t"
        )

    close_call = client.request.await_args_list[-1]
    assert close_call.args == ("POST", "https://rtc.live.cloudflare.com/v1/apps/app1/adapters/websocket/close")
    assert close_call.kwargs["json"] == {"tracks": [{"adapterId": "A-in"}]}


@pytest.mark.asyncio
async def test_ingest_response_without_tracks_is_a_media_room_error_and_closes_nothing():
    client = AsyncMock()
    client.request.side_effect = [_resp(200, {"tracks": []})]
    with pytest.raises(MediaRoomError):
        await CloudflareSfuAdapter("app1", "s", http_client=client).attach_agent(
            "S1", "wss://r/sfu/ingest?ticket=t", "wss://r/sfu/egress?ticket=t"
        )

    assert client.request.await_count == 1
