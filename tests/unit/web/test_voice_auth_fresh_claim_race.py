"""`/voice/auth` with no call in flight (P11-G1-1): the dial claims the one-call marker atomically, so a second call that
claims it between the dial's read and its claim keeps it, and the dial is rejected without a ticket or a parked callback."""
import asyncio

import pytest

from tests.unit.web.test_voice_phone_replaces_web_call import MARKER, World, _accepted

OTHER_CALL = {"in_flight": True, "owner": "other-call", "call_id": "other"}


@pytest.mark.asyncio
async def test_a_call_that_claims_the_free_marker_first_wins_over_the_dial():
    w = World()
    real = w.store.set_if_absent

    async def other_call_got_there_first(key, value, ttl_s):
        w.store.data[key] = dict(OTHER_CALL)  # another call claims between the dial's read and its claim
        return await real(key, value, ttl_s)

    w.store.set_if_absent = other_call_got_there_first

    twiml = await w.dial()

    assert not _accepted(twiml) and "<Reject" in twiml
    assert w.store.data[MARKER] == OTHER_CALL
    assert not [k for k in w.store.data if k.startswith(("voice_ticket:", "voice_pending_callback:"))]


@pytest.mark.asyncio
async def test_two_dials_with_no_call_in_flight_let_exactly_one_through():
    w = World()

    first, second = await asyncio.gather(w.dial("CA0001"), w.dial("CA0002"))

    assert sorted([_accepted(first), _accepted(second)]) == [False, True]
    winner = "CA0001" if _accepted(first) else "CA0002"
    loser = "CA0002" if winner == "CA0001" else "CA0001"
    assert f"voice_pending_callback:{winner}" in w.store.data
    assert f"voice_pending_callback:{loser}" not in w.store.data
