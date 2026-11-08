"""CallControlHub / CallControlLink (VOICE_CALL_CONTROL_CHANNEL_RFC §4.3): attach, replace, unknown digest, teardown,
and the link API later features call (send, commands, welcome state)."""
import asyncio

import pytest

from src.domain.secret_digest import fingerprint
from src.domain.voice_control_protocol import FEATURE_ASK, ByeReason, control_token_digest
from src.services.call_control_hub import CallControlHub
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-0001-invented"
DIGEST = control_token_digest(TOKEN)
CALL_FP = fingerprint("ticket-0001-invented")


@pytest.mark.asyncio
async def test_attach_to_a_live_call_sends_welcome_first():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP)
    page = FakeControlSocket()

    assert await hub.attach(DIGEST, page) is link
    assert page.frames() == [{"type": "welcome", "v": 1, "features": []}]
    assert link.connected
    assert await link.send({"type": "status", "state": "listening"}) is True
    assert page.frames()[-1] == {"type": "status", "state": "listening"}


@pytest.mark.asyncio
async def test_welcome_names_the_offered_features_and_their_current_state():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP, features=[FEATURE_ASK])
    link.set_state(ask="armed")
    page = FakeControlSocket()
    await hub.attach(DIGEST, page)
    assert page.frames() == [{"type": "welcome", "v": 1, "features": ["ask"], "ask": "armed"}]


@pytest.mark.asyncio
async def test_a_new_socket_replaces_the_old_one():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP)
    old, new = FakeControlSocket(), FakeControlSocket()
    await hub.attach(DIGEST, old)

    assert await hub.attach(DIGEST, new) is link

    assert old.types() == ["welcome", "bye"] and old.frames()[-1] == {"type": "bye", "reason": "replaced"}
    assert old.closed
    assert new.types() == ["welcome"] and not new.closed
    await link.send({"type": "status", "state": "thinking"})
    assert new.types() == ["welcome", "status"] and old.types() == ["welcome", "bye"]
    # the replaced socket's handler detaches it later: the new socket stays attached
    await hub.detach(link, old)
    assert link.connected
    await hub.detach(link, new)
    assert not link.connected


@pytest.mark.asyncio
async def test_reconnect_after_a_drop_gets_a_fresh_welcome_with_the_current_state():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP, features=[FEATURE_ASK])
    first = FakeControlSocket()
    await hub.attach(DIGEST, first)
    await hub.detach(link, first)
    assert await link.send({"type": "status"}) is False      # nobody listening: dropped, no raise

    link.set_state(ask="active")
    second = FakeControlSocket()
    assert await hub.attach(DIGEST, second) is link
    assert second.frames() == [{"type": "welcome", "v": 1, "features": ["ask"], "ask": "active"}]


@pytest.mark.asyncio
async def test_unknown_digest_attaches_nothing():
    hub = CallControlHub()
    hub.open(DIGEST, CALL_FP)
    stranger = FakeControlSocket()
    assert await hub.attach(control_token_digest("token-9999-invented"), stranger) is None
    assert stranger.sent == [] and not stranger.closed     # the handler answers bye{auth}, not the hub


@pytest.mark.asyncio
async def test_a_hello_just_before_its_call_registers_waits_for_it():
    hub = CallControlHub()
    page = FakeControlSocket()
    waiting = asyncio.ensure_future(hub.attach(DIGEST, page, wait_s=2.0))
    await asyncio.sleep(0.01)
    hub.open(control_token_digest("token-0002-invented"), fingerprint("ticket-0002-invented"))  # another call
    await asyncio.sleep(0.01)
    assert not waiting.done()
    link = hub.open(DIGEST, CALL_FP)
    assert await asyncio.wait_for(waiting, timeout=1) is link
    assert page.types() == ["welcome"]


@pytest.mark.asyncio
async def test_the_wait_for_a_call_is_bounded():
    hub = CallControlHub()
    assert await hub.attach(DIGEST, FakeControlSocket(), wait_s=0.05) is None


@pytest.mark.asyncio
async def test_teardown_says_call_ended_and_forgets_the_digest():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP)
    page = FakeControlSocket()
    await hub.attach(DIGEST, page)

    await hub.close(link)

    assert page.frames()[-1] == {"type": "bye", "reason": "call_ended"} and page.closed
    assert hub.get(DIGEST) is None and len(hub) == 0
    assert link.closed and not link.connected
    assert await link.send({"type": "status"}) is False
    # the token is dead: a redial with it is unknown
    assert await hub.attach(DIGEST, FakeControlSocket()) is None


@pytest.mark.asyncio
async def test_teardown_without_a_socket_and_with_a_dead_socket_never_raises():
    hub = CallControlHub(send_timeout_s=0.05)
    idle = hub.open(DIGEST, CALL_FP)
    await hub.close(idle)
    assert hub.get(DIGEST) is None

    stalled = hub.open(control_token_digest("token-0002-invented"), CALL_FP)
    page = FakeControlSocket()
    await hub.attach(stalled.digest, page)
    page.stall_sends = True
    await asyncio.wait_for(hub.close(stalled), timeout=1)   # bounded by the send timeout
    assert page.closed and len(hub) == 0


@pytest.mark.asyncio
async def test_teardown_of_an_old_link_never_evicts_a_newer_one():
    hub = CallControlHub()
    old = hub.open(DIGEST, CALL_FP)
    new = hub.open(DIGEST, CALL_FP)
    await hub.close(old)
    assert hub.get(DIGEST) is new


@pytest.mark.asyncio
async def test_a_socket_cannot_attach_to_a_link_shut_meanwhile():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP)
    await link.shut(ByeReason.CALL_ENDED)
    page = FakeControlSocket()
    assert await hub.attach(DIGEST, page, wait_s=0.05) is None
    assert page.sent == []


@pytest.mark.asyncio
async def test_a_stalled_page_never_blocks_a_sender():
    hub = CallControlHub(send_timeout_s=0.05)
    link = hub.open(DIGEST, CALL_FP)
    page = FakeControlSocket()
    await hub.attach(DIGEST, page)
    page.stall_sends = True
    assert await asyncio.wait_for(link.send({"type": "status"}), timeout=1) is False


@pytest.mark.asyncio
async def test_only_the_offered_features_commands_are_accepted_and_the_queue_is_bounded():
    hub = CallControlHub()
    plain = hub.open(DIGEST, CALL_FP)
    assert not plain.accepts("ask_arm")

    asking = hub.open(control_token_digest("token-0002-invented"), CALL_FP, features=[FEATURE_ASK])
    assert asking.accepts("ask_arm") and asking.accepts("ask_disarm") and not asking.accepts("ping")
    accepted = [asking.offer_command({"type": "ask_arm", "seq": n}) for n in range(40)]
    assert accepted.count(True) == 32 and accepted[-1] is False
    assert (await asking.commands.get()) == {"type": "ask_arm", "seq": 0}
