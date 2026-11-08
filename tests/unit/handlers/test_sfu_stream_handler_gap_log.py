"""SfuStreamHandler logs inbound-audio gaps: fingerprint and seconds only (RFC §7.4.2)."""
import asyncio
import logging

import pytest

from src.handlers.sfu_stream_handler import SfuStreamHandler, _SfuCall

T1 = "5f0c1a3e-8d2b-4c1f-9a7e-2b6d4e8f1a03"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def _handler(clock):
    return SfuStreamHandler(session_service=None, clock=clock, gap_poll_s=0.01)


@pytest.mark.asyncio
async def test_gap_start_and_end_are_logged_with_duration_and_no_ticket(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    handler = _handler(clock)
    call = _SfuCall(ticket=T1)
    handler._note_packet(call)
    watch = asyncio.ensure_future(handler._watch_gaps(call))
    clock.now = 2.5
    await asyncio.sleep(0.05)
    clock.now = 6.0
    handler._note_packet(call)
    call.end()
    await watch
    lines = [r.getMessage() for r in caplog.records if "inbound audio gap" in r.getMessage()]
    assert len(lines) == 2
    assert "gap started" in lines[0] and "gap ended after 6.0s" in lines[1]
    assert all(T1 not in line for line in lines)


@pytest.mark.asyncio
async def test_no_gap_logged_before_the_first_packet(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    handler = _handler(clock)
    call = _SfuCall(ticket=T1)
    watch = asyncio.ensure_future(handler._watch_gaps(call))
    clock.now = 50.0
    await asyncio.sleep(0.05)
    call.end()
    await watch
    assert not [r for r in caplog.records if "inbound audio gap" in r.getMessage()]
