"""Two edges of a web start replacing the running web call (VOICE_CALL_CONTROL_CHANNEL_RFC §7.5.2), now that the marker
names its call (`owner`) and the close goes through the shared `WebCallMedia`: the running call ends between the refused
claim and the holder read, and the running call's media cannot be closed."""
import logging

import pytest

from src.domain.voice_ticket_keys import one_call_owner
from tests.support.voice_tickets import DIGEST_KEY
from tests.unit.web.test_voice_phone_replaces_web_call import AUTH, CIPHER, MARKER, World
from tests.support.voice_tickets import open_ticket


async def _start(w):
    resp = await w.client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)
    return resp, await resp.get_json()


@pytest.mark.asyncio
async def test_a_call_that_ends_between_the_refused_claim_and_the_holder_read_is_claimed_with_its_owner():
    w = World()
    await w.live_web_call()
    store_get = w.store.get
    marker_reads = []

    async def get(key):
        value = await store_get(key)
        if key == MARKER:
            marker_reads.append(value)
            if len(marker_reads) == 1:  # the claim's read sees the running call; it ends right after
                w.store.data.pop(MARKER, None)
        return value

    w.store.get = get

    resp, body = await _start(w)

    assert resp.status_code == 201
    assert w.room.closed == []  # nothing left to replace
    ticket = await open_ticket(CIPHER, f"voice_web_call:{body['call_id']}", w.store.data[f"voice_web_call:{body['call_id']}"])
    assert w.store.data[MARKER] == {"in_flight": True, "call_id": body["call_id"],
                                    "owner": one_call_owner(DIGEST_KEY, ticket)}


@pytest.mark.asyncio
async def test_a_running_call_whose_media_cannot_be_closed_is_not_replaced(caplog):
    w = World()
    old_call, _ = await w.live_web_call()
    old_marker = dict(w.store.data[MARKER])
    w.room.fails_to_close = True

    resp, body = await _start(w)

    assert resp.status_code == 409 and body["error"] == "a call is already in progress"
    assert w.store.data[MARKER] == old_marker
    assert any(r.levelno == logging.ERROR and old_call in r.getMessage() for r in caplog.records)
