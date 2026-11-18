"""`/voice/auth` and the one-call marker (P11-G1-1, review round): a dial that replaced a web call claims the released
marker atomically, and a failure after the claim never leaves the user locked out for the marker's TTL."""
import asyncio
import logging

import pytest

from tests.support.voice_tickets import DIGEST_KEY, ticket_key
from tests.unit.web.test_voice_phone_replaces_web_call import MARKER, World, _accepted

LATE_WEB_START = {"in_flight": True, "owner": "late-web-call", "call_id": "web-late"}


@pytest.mark.asyncio
async def test_a_call_that_takes_the_released_marker_first_wins_over_the_dial():
    w = World()
    _, web_ticket = await w.live_web_call()
    w.relay_reports_after(web_ticket, after_s=0.02)
    real = w.store.set_if_absent

    async def web_start_got_there_first(key, value, ttl_s):
        w.store.data[key] = dict(LATE_WEB_START)  # a web start claims between the release and the dial's claim
        return await real(key, value, ttl_s)

    w.store.set_if_absent = web_start_got_there_first

    twiml = await w.dial()

    assert not _accepted(twiml) and "<Reject" in twiml
    assert w.store.data[MARKER] == LATE_WEB_START


@pytest.mark.asyncio
async def test_two_dials_that_read_the_same_web_holder_let_exactly_one_through():
    w = World()
    _, web_ticket = await w.live_web_call()
    w.relay_reports_after(web_ticket, after_s=0.05)

    first, second = await asyncio.gather(w.dial("CA0001"), w.dial("CA0002"))

    assert sorted([_accepted(first), _accepted(second)]) == [False, True]
    winner = "CA0001" if _accepted(first) else "CA0002"
    assert f"voice_pending_callback:{winner}" in w.store.data
    loser = "CA0002" if winner == "CA0001" else "CA0001"
    assert f"voice_pending_callback:{loser}" not in w.store.data


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["ticket", "pending"])
async def test_a_failed_write_after_the_claim_releases_the_marker_and_the_ticket(failing, caplog):
    caplog.set_level(logging.ERROR)
    w = World()
    real_set = w.store.set

    async def set_failing(key, value, ttl_s):
        if key.startswith("voice_ticket:" if failing == "ticket" else "voice_pending_callback:"):
            raise RuntimeError("store unavailable")
        await real_set(key, value, ttl_s)

    w.store.set = set_failing

    twiml = await w.dial()

    assert "<Reject" in twiml
    assert MARKER not in w.store.data
    assert not [k for k in w.store.data if k.startswith(("voice_ticket:", "voice_pending_callback:"))]
    assert any("parking the callback" in r.getMessage() and "usr-1" in r.getMessage() for r in caplog.records)
    w.store.set = real_set
    assert _accepted(await w.dial("CA0009"))  # the user can dial again at once


@pytest.mark.asyncio
async def test_a_failing_release_after_a_failed_write_is_logged_and_the_dial_is_still_rejected(caplog):
    caplog.set_level(logging.ERROR)
    w = World()

    async def always_fails(*args, **kwargs):
        raise RuntimeError("store unavailable")

    real_delete = w.store.delete
    real_set = w.store.set

    async def set_failing(key, value, ttl_s):
        if key.startswith("voice_ticket:"):
            raise RuntimeError("store unavailable")
        await real_set(key, value, ttl_s)

    w.store.set, w.store.delete = set_failing, always_fails
    twiml = await w.dial()

    assert "<Reject" in twiml
    assert sum("expires on its own TTL" in r.getMessage() for r in caplog.records) == 2
    w.store.delete = real_delete
