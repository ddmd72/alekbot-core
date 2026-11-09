"""`listening` on the relay's `/ctl` socket (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2, §4.4): accepted on every call whatever
its features, never queued as a feature command, a repeat ignored; the frames that came before it behave as they did."""
import asyncio

import pytest

from src.domain.secret_digest import fingerprint
from src.domain.voice_control_protocol import FEATURE_ASK, control_token_digest
from src.handlers.call_control_handler import CallControlHandler
from src.services.call_control_hub import CallControlHub
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-2d6e90b1-invented"
CALL_FP = fingerprint("ticket-a07c55e3-invented")


async def _connected(features):
    hub = CallControlHub()
    link = hub.open(control_token_digest(TOKEN), CALL_FP, features=features)
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    task = asyncio.ensure_future(CallControlHandler(hub, hello_timeout_s=0.5, link_wait_s=0.05).handle_connection(page))
    await asyncio.sleep(0.01)
    return link, page, task


async def _leave(page, task):
    page.page_leaves()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.parametrize("features", [(), (FEATURE_ASK,)], ids=["no-features", "ask"])
@pytest.mark.asyncio
async def test_listening_marks_the_link_on_every_call_and_is_not_a_command(features):
    link, page, task = await _connected(features)
    assert not link.listened.is_set()
    page.page_says({"type": "listening"})
    await asyncio.sleep(0.01)
    assert link.listened.is_set() and link.commands.empty()
    assert page.types() == ["welcome"] and not page.closed       # nothing is answered
    await _leave(page, task)


@pytest.mark.asyncio
async def test_a_second_listening_is_ignored():
    link, page, task = await _connected(())
    page.page_says({"type": "listening"})
    page.page_says({"type": "listening", "extra": 1})
    await asyncio.sleep(0.01)
    assert link.listened.is_set() and link.commands.empty() and not page.closed
    assert link.mark_listening() is False                          # the link itself reports the repeat
    await _leave(page, task)


@pytest.mark.asyncio
async def test_the_first_mark_reports_true_and_only_the_first():
    link = CallControlHub().open(control_token_digest(TOKEN), CALL_FP)
    assert [link.mark_listening(), link.mark_listening()] == [True, False] and link.listened.is_set()


@pytest.mark.asyncio
async def test_listening_before_hello_is_not_a_hello():
    hub = CallControlHub()
    link = hub.open(control_token_digest(TOKEN), CALL_FP)
    page = FakeControlSocket()
    page.page_says({"type": "listening"})
    await asyncio.wait_for(CallControlHandler(hub, hello_timeout_s=0.5).handle_connection(page), timeout=2)
    assert page.closed and page.close_code == 1008 and not link.listened.is_set()


@pytest.mark.asyncio
async def test_the_older_frames_behave_as_before_next_to_listening():
    link, page, task = await _connected((FEATURE_ASK,))
    page.page_says({"type": "ping"})
    page.page_says({"type": "listening"})
    page.page_says({"type": "ask_arm", "seq": 1})
    page.page_says({"type": "line_please"})                         # unknown: still ignored
    await asyncio.sleep(0.01)
    assert page.types() == ["welcome", "pong"]
    assert link.commands.qsize() == 1 and link.commands.get_nowait() == {"type": "ask_arm", "seq": 1}
    await _leave(page, task)


@pytest.mark.asyncio
async def test_a_call_without_ask_still_drops_ask_commands():
    link, page, task = await _connected(())
    page.page_says({"type": "ask_arm", "seq": 1})
    page.page_says({"type": "listening"})
    await asyncio.sleep(0.01)
    assert link.commands.empty() and link.listened.is_set()
    await _leave(page, task)
