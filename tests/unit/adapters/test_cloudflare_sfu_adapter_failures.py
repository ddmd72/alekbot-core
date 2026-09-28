"""CloudflareSfuAdapter failure shapes (final-review findings 4 and 5), mocked at the httpx
boundary: transport errors and unparseable 2xx bodies surface as MediaRoomError (so the
attach_agent cleanup runs), an empty 2xx body is `{}` (POC behaviour), and closing an adapter the
SFU no longer has is routine, not an ERROR."""
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.adapters.cloudflare_sfu_adapter import CloudflareSfuAdapter
from src.ports.media_room_port import MediaRoomError

BASE = "https://rtc.live.cloudflare.com/v1/apps/app1"
SECRET = "the-app-secret"


def _resp(status, body=None, text=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = text if text is not None else json.dumps(body)
    return r


def _adapter(client):
    return CloudflareSfuAdapter("app1", SECRET, http_client=client)


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_timeout_on_the_egress_call_is_a_media_room_error_and_closes_the_ingest_adapter(caplog):
    client = AsyncMock()
    client.request.side_effect = [
        _resp(200, {"tracks": [{"adapterId": "A-in", "sessionId": "P1", "trackName": "lelik"}]}),
        httpx.ReadTimeout("timed out"),
        _resp(200, {"tracks": []}),
    ]
    with pytest.raises(MediaRoomError):
        await _adapter(client).attach_agent("S1", "wss://r/sfu/ingest?ticket=t", "wss://r/sfu/egress?ticket=t")

    close_call = client.request.await_args_list[-1]
    assert close_call.args == ("POST", f"{BASE}/adapters/websocket/close")
    assert close_call.kwargs["json"] == {"tracks": [{"adapterId": "A-in"}]}
    assert all(SECRET not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_transport_error_on_open_caller_is_a_media_room_error():
    client = AsyncMock()
    client.request.side_effect = httpx.ConnectError("no route")
    with pytest.raises(MediaRoomError):
        await _adapter(client).open_caller("OFFER", "0")


@pytest.mark.asyncio
async def test_empty_2xx_body_is_an_empty_object():
    client = AsyncMock()
    empty = _resp(200, text="")
    empty.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    client.request.return_value = empty
    await _adapter(client).complete_negotiation("S1", "ANSWER")  # must not raise
    assert await _adapter(client)._call("PUT", "/sessions/S1/renegotiate", {}) == {}


@pytest.mark.asyncio
async def test_non_json_2xx_body_is_a_media_room_error():
    client = AsyncMock()
    garbage = _resp(200, text="<html>oops</html>")
    garbage.json.side_effect = json.JSONDecodeError("Expecting value", "<html>oops</html>", 0)
    client.request.return_value = garbage
    with pytest.raises(MediaRoomError):
        await _adapter(client).open_caller("OFFER", "0")


@pytest.mark.asyncio
async def test_closing_an_already_closed_adapter_logs_no_error_and_does_not_raise(caplog):
    caplog.set_level(logging.INFO)
    client = AsyncMock()
    client.request.side_effect = [_resp(404, {"errorCode": "not found"}), _resp(200, {"tracks": []})]
    await _adapter(client).close(["A-in", "A-eg"])

    assert client.request.await_count == 2
    assert _errors(caplog) == []
    assert any("already closed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_real_close_failure_is_logged_as_a_warning_not_an_error(caplog):
    client = AsyncMock()
    client.request.side_effect = [_resp(500, {"error": "boom"})]
    await _adapter(client).close(["A-in"])  # must not raise
    assert _errors(caplog) == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)
