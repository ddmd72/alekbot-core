"""The link's snapshot hook (VOICE_CALL_LIVE_TEXT_RFC §4.7): every attach writes welcome, then the snapshot, before any
live frame; a hook that raises costs the snapshot, never the socket."""
import pytest

from src.domain.secret_digest import fingerprint
from src.domain.voice_control_protocol import FEATURE_TEXT, control_token_digest, snapshot_frame
from src.services.call_control_hub import CallControlHub
from tests.support.control_socket_fake import FakeControlSocket

DIGEST = control_token_digest("token-0001-invented")
CALL_FP = fingerprint("ticket-0001-invented")


@pytest.mark.asyncio
async def test_attach_writes_welcome_then_the_snapshot():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP, features=[FEATURE_TEXT])
    link.set_snapshot(lambda: snapshot_frame(3, [{"line": "l1", "who": "caller", "text": "I have a cleaning."}]))
    page = FakeControlSocket()
    await hub.attach(DIGEST, page)
    assert page.frames() == [
        {"type": "welcome", "v": 1, "features": ["text"]},
        {"v": 1, "seq": 3, "type": "snapshot", "lines": [{"line": "l1", "who": "caller", "text": "I have a cleaning."}]},
    ]


@pytest.mark.asyncio
async def test_a_reconnect_gets_a_fresh_snapshot():
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP)
    counter = {"seq": 0}

    def build():
        counter["seq"] += 1
        return snapshot_frame(counter["seq"], [])

    link.set_snapshot(build)
    first, second = FakeControlSocket(), FakeControlSocket()
    await hub.attach(DIGEST, first)
    await hub.attach(DIGEST, second)
    assert [f["seq"] for f in first.frames() if f["type"] == "snapshot"] == [1]
    assert second.types()[:2] == ["welcome", "snapshot"]
    assert [f["seq"] for f in second.frames() if f["type"] == "snapshot"] == [2]


@pytest.mark.asyncio
async def test_no_hook_means_welcome_only():
    hub = CallControlHub()
    hub.open(DIGEST, CALL_FP)
    page = FakeControlSocket()
    await hub.attach(DIGEST, page)
    assert page.types() == ["welcome"]


@pytest.mark.asyncio
async def test_a_raising_hook_leaves_the_socket_attached_with_welcome_only(caplog):
    hub = CallControlHub()
    link = hub.open(DIGEST, CALL_FP)

    def build():
        raise RuntimeError("boom")

    link.set_snapshot(build)
    page = FakeControlSocket()
    assert await hub.attach(DIGEST, page) is link
    assert page.types() == ["welcome"] and link.connected
    assert await link.send({"type": "status", "state": "listening"}) is True
    assert f"control {CALL_FP}: snapshot not built (RuntimeError)" in caplog.text
