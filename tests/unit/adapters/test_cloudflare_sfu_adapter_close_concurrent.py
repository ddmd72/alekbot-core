"""CloudflareSfuAdapter.close(): the legs close concurrently, mocked at the httpx boundary, so a hung SFU costs one
request timeout, not one per leg (VOICE_CALL_CONTROL_CHANNEL_RFC §7.5.4: a phone dial closes a web call's legs under
Twilio's webhook deadline). Each leg is still closed exactly once and a failure of one never stops the other."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.cloudflare_sfu_adapter import CloudflareSfuAdapter


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


@pytest.mark.asyncio
async def test_both_legs_are_in_flight_at_once():
    in_flight, both, starved = [], asyncio.Event(), []

    async def request(method, url, **kwargs):
        in_flight.append(kwargs["json"]["tracks"][0]["adapterId"])
        if len(in_flight) == 2:
            both.set()
        try:
            await asyncio.wait_for(both.wait(), timeout=0.5)
        except asyncio.TimeoutError:  # a sequential close never starts the second leg while the first is pending
            starved.append(in_flight[-1])
            raise
        return _resp(200, {"tracks": []})

    client = AsyncMock()
    client.request.side_effect = request

    await asyncio.wait_for(CloudflareSfuAdapter("app1", "s", http_client=client).close(["A-in", "A-eg"]), timeout=2)

    assert sorted(in_flight) == ["A-eg", "A-in"]
    assert starved == []


@pytest.mark.asyncio
async def test_one_leg_failing_still_closes_the_other_and_does_not_raise():
    async def request(method, url, **kwargs):
        if kwargs["json"]["tracks"][0]["adapterId"] == "A-in":
            raise RuntimeError("connection reset")
        return _resp(200, {"tracks": []})

    client = AsyncMock()
    client.request.side_effect = request

    await CloudflareSfuAdapter("app1", "s", http_client=client).close(["A-in", "A-eg"])

    closed = sorted(c.kwargs["json"]["tracks"][0]["adapterId"] for c in client.request.await_args_list)
    assert closed == ["A-eg", "A-in"]
