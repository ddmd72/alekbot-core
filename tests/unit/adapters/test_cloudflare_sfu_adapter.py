"""CloudflareSfuAdapter wire test: request shapes and order, mocked at the httpx boundary."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.cloudflare_sfu_adapter import CloudflareSfuAdapter
from src.ports.media_room_port import MediaRoomError

BASE = "https://rtc.live.cloudflare.com/v1/apps/app1"


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = str(body)
    return r


@pytest.mark.asyncio
async def test_open_caller_creates_session_then_publishes_mic():
    client = AsyncMock()
    client.request.side_effect = [
        _resp(201, {"sessionId": "S1"}),
        _resp(200, {"sessionDescription": {"type": "answer", "sdp": "ANSWER"}}),
    ]
    leg = await CloudflareSfuAdapter("app1", "secret", http_client=client).open_caller("OFFER", "0")

    assert (leg.session_id, leg.answer_sdp) == ("S1", "ANSWER")
    first, second = client.request.await_args_list
    assert first.args == ("POST", f"{BASE}/sessions/new")
    assert first.kwargs["headers"]["Authorization"] == "Bearer secret"
    assert second.args == ("POST", f"{BASE}/sessions/S1/tracks/new")
    assert second.kwargs["json"] == {"sessionDescription": {"type": "offer", "sdp": "OFFER"},
                                     "tracks": [{"location": "local", "mid": "0", "trackName": "mic"}]}


@pytest.mark.asyncio
async def test_attach_agent_creates_ingest_egress_and_pulls_lelik():
    client = AsyncMock()
    client.request.side_effect = [
        _resp(200, {"tracks": [{"adapterId": "A-in", "sessionId": "P1", "trackName": "lelik"}]}),
        _resp(200, {"tracks": [{"adapterId": "A-eg", "trackName": "mic"}]}),
        _resp(200, {"requiresImmediateRenegotiation": True, "sessionDescription": {"type": "offer", "sdp": "OFFER2"}}),
    ]
    leg = await CloudflareSfuAdapter("app1", "s", http_client=client).attach_agent("S1", "wss://r/sfu/ingest?ticket=t", "wss://r/sfu/egress?ticket=t")

    assert leg.adapter_ids == ["A-in", "A-eg"] and leg.offer_sdp == "OFFER2"
    ingest, egress, pull = [c.kwargs["json"] for c in client.request.await_args_list]
    assert ingest == {"tracks": [{"location": "local", "trackName": "lelik", "inputCodec": "pcm", "endpoint": "wss://r/sfu/ingest?ticket=t"}]}
    assert egress == {"tracks": [{"location": "remote", "sessionId": "S1", "trackName": "mic", "outputCodec": "pcm", "endpoint": "wss://r/sfu/egress?ticket=t"}]}
    assert pull == {"tracks": [{"location": "remote", "sessionId": "P1", "trackName": "lelik"}]}


@pytest.mark.asyncio
async def test_error_status_raises_media_room_error():
    client = AsyncMock()
    client.request.return_value = _resp(500, {"error": "boom"})
    with pytest.raises(MediaRoomError):
        await CloudflareSfuAdapter("app1", "s", http_client=client).open_caller("OFFER", "0")


@pytest.mark.asyncio
async def test_close_is_best_effort_and_closes_every_adapter():
    client = AsyncMock()
    client.request.side_effect = [_resp(500, {}), _resp(200, {"tracks": []})]
    await CloudflareSfuAdapter("app1", "s", http_client=client).close(["A-in", "A-eg"])
    bodies = [c.kwargs["json"] for c in client.request.await_args_list]
    assert bodies == [{"tracks": [{"adapterId": "A-in"}]}, {"tracks": [{"adapterId": "A-eg"}]}]
