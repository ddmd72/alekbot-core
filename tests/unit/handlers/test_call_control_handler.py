"""The relay's `/ctl` socket (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2, §9): hello deadline, bad token, version, frame
cap, rate cap, the unauthenticated-socket cap, ping, and the call's teardown reaching the page."""
import asyncio

import pytest

from src.domain.secret_digest import fingerprint
from src.domain.voice_control_protocol import FEATURE_ASK, control_token_digest
from src.handlers.call_control_handler import CallControlHandler
from src.services.call_control_hub import CallControlHub
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-0001-invented"
CALL_FP = fingerprint("ticket-0001-invented")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _world(features=(), **kw):
    hub = CallControlHub()
    link = hub.open(control_token_digest(TOKEN), CALL_FP, features=features)
    kw.setdefault("hello_timeout_s", 0.5)
    kw.setdefault("link_wait_s", 0.05)
    return hub, link, CallControlHandler(hub, **kw)


async def _connect(handler, page):
    task = asyncio.ensure_future(handler.handle_connection(page))
    await asyncio.sleep(0.01)
    return task


async def _finish(task):
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_a_good_hello_gets_welcome_and_pings_get_pongs():
    _, link, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    page.page_says({"type": "ping"})
    page.page_says({"type": "line_please"})        # unknown: ignored
    page.page_says("not json")                     # ignored
    page.page_says(b"\x00\x01")                    # binary: ignored
    page.page_says({"type": "ping"})
    await asyncio.sleep(0.02)
    assert page.types() == ["welcome", "pong", "pong"]
    assert link.connected and not page.closed
    page.page_leaves()
    await _finish(task)
    assert not link.connected                       # detached; the link waits for a reconnect


@pytest.mark.asyncio
async def test_no_hello_within_the_deadline_closes_the_socket():
    _, link, handler = _world(hello_timeout_s=0.05)
    page = FakeControlSocket()
    task = await _connect(handler, page)
    await _finish(task)
    assert page.close_code == 1008 and page.sent == []
    assert not link.connected


@pytest.mark.asyncio
@pytest.mark.parametrize("hello", [
    {"type": "hello", "v": 1, "token": "token-9999-invented"},
    {"type": "hello", "v": 1},
    {"type": "hello", "v": 1, "token": ""},
    {"type": "hello", "v": 1, "token": 42},
])
async def test_a_bad_token_is_told_auth_and_closed(hello):
    _, link, handler = _world()
    page = FakeControlSocket()
    page.page_says(hello)
    await _finish(await _connect(handler, page))
    assert page.frames() == [{"type": "bye", "reason": "auth"}] and page.closed
    assert not link.connected


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [0, -1, "1", None, True, 1.5])
async def test_an_unsupported_version_is_told_version(version):
    _, link, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": version, "token": TOKEN})
    await _finish(await _connect(handler, page))
    assert page.frames() == [{"type": "bye", "reason": "version"}] and page.closed
    assert not link.connected


@pytest.mark.asyncio
async def test_a_newer_page_version_is_answered_with_the_version_the_relay_speaks():
    _, link, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 2, "token": TOKEN})
    task = await _connect(handler, page)
    assert page.frames() == [{"type": "welcome", "v": 1, "features": []}]
    page.page_leaves()
    await _finish(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [{"type": "ping"}, "not json", b"binary", {"no": "type"}])
async def test_a_first_frame_that_is_not_a_hello_closes_without_a_word(first):
    _, link, handler = _world()
    page = FakeControlSocket()
    page.page_says(first)
    await _finish(await _connect(handler, page))
    assert page.close_code == 1008 and page.sent == []


@pytest.mark.asyncio
async def test_an_oversized_hello_is_closed_as_too_big():
    _, link, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN, "pad": "x" * 1024})
    await _finish(await _connect(handler, page))
    assert page.close_code == 1009 and page.sent == [] and not link.connected


@pytest.mark.asyncio
async def test_a_frame_over_1_kb_closes_the_socket_and_one_at_1_kb_does_not():
    _, link, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    at_cap = '{"type":"ping","pad":"' + "x" * (1024 - len('{"type":"ping","pad":""}')) + '"}'
    assert len(at_cap.encode()) == 1024
    page.page_says(at_cap)
    await asyncio.sleep(0.02)
    assert page.types() == ["welcome", "pong"] and not page.closed
    page.page_says(at_cap[:-2] + 'x"}')            # 1025 bytes
    await _finish(task)
    assert page.close_code == 1009 and not link.connected


@pytest.mark.asyncio
async def test_the_cap_counts_bytes_not_characters():
    _, _, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    under = '{"type":"x","t":"' + "é" * 500 + '"}'
    over = '{"type":"x","t":"' + "é" * 510 + '"}'
    assert len(over) < 1024 < len(over.encode()) and len(under.encode()) <= 1024
    page.page_says(under)
    await asyncio.sleep(0.02)
    assert not page.closed
    page.page_says(over)
    await _finish(task)
    assert page.close_code == 1009


@pytest.mark.asyncio
async def test_more_than_10_frames_in_a_second_closes_the_socket():
    clock = Clock()
    _, link, handler = _world(clock=clock)
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    for _ in range(10):
        page.page_says({"type": "ping"})
    await asyncio.sleep(0.02)
    assert page.types().count("pong") == 10 and not page.closed      # 10 in a second: allowed
    page.page_says({"type": "ping"})                                  # the 11th within the same second
    await _finish(task)
    assert page.close_code == 1008 and page.types().count("pong") == 10
    assert not link.connected


@pytest.mark.asyncio
async def test_frames_spread_over_time_stay_under_the_rate_cap():
    clock = Clock()
    _, _, handler = _world(clock=clock)
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    for _ in range(30):
        page.page_says({"type": "ping"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        clock.now += 0.2                                              # 5 frames per second
    await asyncio.sleep(0.02)
    assert page.types().count("pong") == 30 and not page.closed
    page.page_leaves()
    await _finish(task)


@pytest.mark.asyncio
async def test_sockets_waiting_for_hello_are_capped():
    _, _, handler = _world(max_unauthenticated=1, hello_timeout_s=0.3)
    silent, extra = FakeControlSocket(), FakeControlSocket()
    first = await _connect(handler, silent)
    await _finish(await _connect(handler, extra))
    assert extra.close_code == 1013 and extra.sent == []
    await _finish(first)                                  # its deadline frees the slot
    late = FakeControlSocket()
    late.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, late)
    assert late.types() == ["welcome"]
    late.page_leaves()
    await _finish(task)


@pytest.mark.asyncio
async def test_a_hello_before_the_call_registers_is_held_until_it_does():
    hub = CallControlHub()
    handler = CallControlHandler(hub, link_wait_s=2.0)
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    assert page.sent == []
    hub.open(control_token_digest(TOKEN), CALL_FP)
    await asyncio.sleep(0.02)
    assert page.types() == ["welcome"]
    page.page_leaves()
    await _finish(task)


@pytest.mark.asyncio
async def test_the_calls_teardown_reaches_the_page_and_ends_the_handler():
    hub, link, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    await hub.close(link)
    await _finish(task)
    assert page.frames()[-1] == {"type": "bye", "reason": "call_ended"} and page.closed


@pytest.mark.asyncio
async def test_a_second_socket_with_the_same_token_replaces_the_first():
    _, link, handler = _world()
    first, second = FakeControlSocket(), FakeControlSocket()
    first.page_says({"type": "hello", "v": 1, "token": TOKEN})
    first_task = await _connect(handler, first)
    second.page_says({"type": "hello", "v": 1, "token": TOKEN})
    second_task = await _connect(handler, second)
    await _finish(first_task)
    assert first.frames()[-1] == {"type": "bye", "reason": "replaced"}
    assert link.connected and second.types() == ["welcome"]
    second.page_leaves()
    await _finish(second_task)


@pytest.mark.asyncio
async def test_feature_commands_reach_the_link_only_when_the_feature_is_offered():
    _, plain, handler = _world()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    page.page_says({"type": "ask_arm", "seq": 1})
    page.page_leaves()
    await _finish(task)
    assert plain.commands.empty()

    _, asking, handler = _world(features=[FEATURE_ASK])
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = await _connect(handler, page)
    page.page_says({"type": "ask_arm", "seq": 1})
    page.page_leaves()
    await _finish(task)
    assert asking.commands.get_nowait() == {"type": "ask_arm", "seq": 1}
