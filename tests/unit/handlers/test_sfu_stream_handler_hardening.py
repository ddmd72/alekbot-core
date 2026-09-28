"""SfuStreamHandler hardening (final-review findings 1, 2, 7): abnormal socket closes are routine
(info, not ERROR, and no orphaned task exception), and the public `/sfu/*` endpoints accept only
uuid-shaped tickets and a bounded number of not-yet-paired calls."""
import asyncio
import logging
import uuid

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosedError

from src.domain.sfu_packet import encode_sfu_packet
from src.handlers import sfu_stream_handler
from src.handlers.sfu_stream_handler import SfuStreamHandler, is_ticket_shaped


class FakeSfuWs:
    """Server-side WebSocket; an exception put on `incoming` is raised from iteration, the way
    websockets raises ConnectionClosedError on an abnormal close."""

    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, data):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)


class FakeSessionService:
    def __init__(self):
        self.calls = []

    async def handle_call(self, ticket, inbound_audio, send_outbound_audio, clear_outbound_audio, playback):
        record = {"ticket": ticket, "frames": []}
        self.calls.append(record)
        async for frame in inbound_audio:
            record["frames"].append(frame)


def _abnormal_close():
    return ConnectionClosedError(None, None)


def _mic_packet(seq: int) -> bytes:
    tone = (np.sin(np.arange(960) / 3) * 8000).astype("<i2")
    return encode_sfu_packet(seq, seq * 960, np.repeat(tone, 2).astype("<i2").tobytes())


def _handler(service, **kw):
    kw.setdefault("frame_interval_s", 0.001)
    kw.setdefault("egress_reattach_s", 0.05)
    return SfuStreamHandler(service, **kw)


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_ticket_shape():
    assert is_ticket_shaped(str(uuid.uuid4()))
    assert not is_ticket_shaped("t1")
    assert not is_ticket_shaped("")
    assert not is_ticket_shaped(uuid.uuid4().hex)  # the page's call_id shape, never a ticket


@pytest.mark.asyncio
async def test_abnormal_egress_close_is_not_an_error_and_the_call_ends_after_the_reattach_window(caplog):
    caplog.set_level(logging.INFO)
    ticket = str(uuid.uuid4())
    service = FakeSessionService()
    handler = _handler(service)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={ticket}")),
             asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={ticket}"))]
    await egress.incoming.put(_mic_packet(1))
    await asyncio.sleep(0.02)
    await egress.incoming.put(_abnormal_close())
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

    assert _errors(caplog) == []
    assert len(service.calls) == 1 and service.calls[0]["frames"]
    assert ingest.closed
    assert any("not reattached" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_abnormal_egress_close_then_reattach_keeps_the_call(caplog):
    caplog.set_level(logging.INFO)
    ticket = str(uuid.uuid4())
    service = FakeSessionService()
    handler = _handler(service, egress_reattach_s=0.3)
    ingest, egress1, egress2 = FakeSfuWs(), FakeSfuWs(), FakeSfuWs()
    t_in = asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={ticket}"))
    t_e1 = asyncio.ensure_future(handler.handle_connection(egress1, f"/sfu/egress?ticket={ticket}"))
    await asyncio.sleep(0.02)
    await egress1.incoming.put(_abnormal_close())
    await asyncio.sleep(0.05)
    t_e2 = asyncio.ensure_future(handler.handle_connection(egress2, f"/sfu/egress?ticket={ticket}"))
    await egress2.incoming.put(_mic_packet(2))
    await asyncio.sleep(0.4)
    assert len(service.calls) == 1 and not ingest.closed
    await egress2.close()
    await asyncio.wait_for(asyncio.gather(t_in, t_e1, t_e2), timeout=2)
    assert service.calls[0]["frames"]
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_abnormal_ingest_close_ends_the_call_without_an_unretrieved_task_exception(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    # Nothing awaits the drain task, so an exception left in it would only surface as
    # "Task exception was never retrieved" at garbage collection. Record the tasks the handler
    # spawns and check none of them finished with an exception.
    spawned = []
    real_ensure_future = asyncio.ensure_future

    def recording_ensure_future(coro):
        task = real_ensure_future(coro)
        spawned.append(task)
        return task

    monkeypatch.setattr(sfu_stream_handler.asyncio, "ensure_future", recording_ensure_future)
    ticket = str(uuid.uuid4())
    service = FakeSessionService()
    handler = _handler(service)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [real_ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={ticket}")),
             real_ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={ticket}"))]
    await asyncio.sleep(0.02)
    await ingest.incoming.put(_abnormal_close())
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

    assert egress.closed and len(service.calls) == 1
    drains = [t for t in spawned if t.get_coro().__name__ == "_drain"]
    assert len(drains) == 1 and drains[0].done()
    assert drains[0].cancelled() or drains[0].exception() is None
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_malformed_ticket_is_rejected_without_starting_a_call():
    service = FakeSessionService()
    handler = _handler(service, pair_timeout_s=5)
    ws = FakeSfuWs()
    await asyncio.wait_for(handler.handle_connection(ws, "/sfu/ingest?ticket=not-a-ticket"), timeout=1)
    assert ws.closed
    assert handler._calls == {} and service.calls == []


@pytest.mark.asyncio
async def test_unpaired_registry_is_capped_but_a_waiting_call_can_still_pair():
    service = FakeSessionService()
    handler = _handler(service, pair_timeout_s=0.5, max_unpaired_calls=2)
    t_a, t_b = str(uuid.uuid4()), str(uuid.uuid4())
    lonely_a, lonely_b = FakeSfuWs(), FakeSfuWs()
    waiting = [asyncio.ensure_future(handler.handle_connection(lonely_a, f"/sfu/ingest?ticket={t_a}")),
               asyncio.ensure_future(handler.handle_connection(lonely_b, f"/sfu/ingest?ticket={t_b}"))]
    await asyncio.sleep(0.02)

    rejected = FakeSfuWs()
    await asyncio.wait_for(
        handler.handle_connection(rejected, f"/sfu/ingest?ticket={uuid.uuid4()}"), timeout=1)
    assert rejected.closed
    assert set(handler._calls) == {t_a, t_b}

    # The second half of an already-registered ticket is not a new entry: it still pairs.
    egress_a = FakeSfuWs()
    t_eg = asyncio.ensure_future(handler.handle_connection(egress_a, f"/sfu/egress?ticket={t_a}"))
    await asyncio.sleep(0.05)
    assert len(service.calls) == 1 and service.calls[0]["ticket"] == t_a

    await egress_a.close()
    await lonely_b.close()
    for task in waiting + [t_eg]:
        task.cancel()
    await asyncio.gather(*waiting, t_eg, return_exceptions=True)
    for entry in list(handler._calls.values()):
        if entry.task is not None:
            entry.task.cancel()
