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
    store_claim = w.store.set_if_absent
    marker_claims = []

    async def set_if_absent(key, value, ttl_s):
        claimed = await store_claim(key, value, ttl_s)
        if key == MARKER:
            marker_claims.append(claimed)
            if len(marker_claims) == 1:  # the claim is refused by the running call; it ends right after
                w.store.data.pop(MARKER, None)
        return claimed

    w.store.set_if_absent = set_if_absent

    resp, body = await _start(w)

    assert resp.status_code == 201
    assert marker_claims == [False, True]  # refused while the call ran, claimed once it had ended
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
