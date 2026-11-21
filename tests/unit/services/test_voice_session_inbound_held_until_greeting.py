"""The relay forwards no caller audio to the provider until it has requested the greeting (Lelik speaks first): frames
before that are consumed and dropped, never buffered; the ones after reach the session. The call's end still shows
while frames are being dropped. The web wait is driven by hand (the cap's `asyncio.wait` waits on an event), so no
test depends on the wall clock."""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.secret_digest import fingerprint
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services import voice_session_service as vss
from src.services.voice_session_service import VoiceSessionService

_REAL_WAIT = asyncio.wait
_REAL_SLEEP = asyncio.sleep
CAP = 4.0
TICKET = "ticket-0d6c-invented"


def _frame(tag: bytes) -> AudioFrame:
    return AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload=tag, track="inbound")


class _Call:
    """One call whose provider session records, in order, the greeting request and every forwarded frame."""

    def __init__(self, monkeypatch, *, call_kind=None, resumed=False):
        self.order = []
        self.cap_fires = asyncio.Event()
        self.inbound = asyncio.Queue()
        self.hold_pickup = asyncio.Event()

        async def fake_wait(fs, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            if timeout == CAP:
                timer = asyncio.ensure_future(self.cap_fires.wait())
                try:
                    done, pending = await _REAL_WAIT(set(fs) | {timer}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    timer.cancel()
                return done - {timer}, pending - {timer}
            return await _REAL_WAIT(fs, timeout=timeout, return_when=return_when)

        monkeypatch.setattr(vss.asyncio, "wait", fake_wait)
        self.session = AsyncMock()
        self.session.resumed = resumed

        async def request_response():
            await self.hold_pickup.wait()
            self.order.append("pickup")

        async def send_audio(frame):
            self.order.append(frame.payload)

        self.session.request_response.side_effect = request_response
        self.session.send_audio.side_effect = send_audio

        async def events():
            await _REAL_SLEEP(30)
            yield E(type="noop", payload={})  # pragma: no cover

        self.session.receive_events = MagicMock(return_value=events())
        control = AsyncMock()
        control.fetch_session_config.return_value = {
            "instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1", "tools": [],
            **({"call_kind": call_kind} if call_kind else {}),
            **({"resume": {"handle": "handle-1"}} if resumed else {})}
        self.service = VoiceSessionService(
            realtime_session_factory=MagicMock(return_value=self.session), control_plane=control,
            alert_sink=AsyncMock(), web_pickup_max_wait_s=CAP if call_kind == "web" else 0.0)

    async def _inbound(self):
        while True:
            frame = await self.inbound.get()
            if frame is None:
                return
            yield frame

    async def start(self):
        self.task = asyncio.ensure_future(self.service.handle_call(
            ticket=TICKET, inbound_audio=self._inbound(), send_outbound_audio=AsyncMock(),
            clear_outbound_audio=AsyncMock(), playback=PlaybackTracker()))
        await self.settle()

    @staticmethod
    async def settle():
        for _ in range(40):
            await _REAL_SLEEP(0)

    async def say(self, *tags):
        for tag in tags:
            self.inbound.put_nowait(_frame(tag))
        await self.settle()

    async def stop(self):
        self.hold_pickup.set()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_web_call_drops_audio_until_the_greeting_is_requested(monkeypatch, caplog):
    call = _Call(monkeypatch, call_kind="web")
    with caplog.at_level(logging.INFO):
        await call.start()
        await call.say(b"hello-1", b"hello-2")          # the page has not said `listening` yet
        assert call.order == []
        call.cap_fires.set()                            # the wait ends, the pickup is requested but still in flight
        await call.settle()
        await call.say(b"hello-3")
        assert call.order == []                         # not forwarded while the greeting request is being made
        call.hold_pickup.set()
        await call.settle()
        await call.say(b"hello-4", b"hello-5")
    assert call.order == ["pickup", b"hello-4", b"hello-5"]   # dropped frames are never flushed later
    lines = [r.getMessage() for r in caplog.records if "caller audio forwarded" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith(f"voice call {fingerprint(TICKET)}: caller audio forwarded")
    assert "3 frames dropped" in lines[0] and "hello" not in lines[0]
    await call.stop()


@pytest.mark.asyncio
async def test_phone_call_drops_audio_before_the_pickup_request_and_forwards_after(monkeypatch):
    call = _Call(monkeypatch)
    await call.start()                                  # the pickup request is in flight
    await call.say(b"hello-1")
    assert call.order == []
    call.hold_pickup.set()
    await call.settle()
    await call.say(b"hello-2")
    assert call.order == ["pickup", b"hello-2"]
    await call.stop()


@pytest.mark.asyncio
async def test_a_caller_hanging_up_during_the_web_wait_ends_the_call_without_a_greeting(monkeypatch):
    call = _Call(monkeypatch, call_kind="web")
    call.hold_pickup.set()
    await call.start()
    await call.say(b"hello-1")
    call.inbound.put_nowait(None)                       # the caller hangs up while the page has not listened
    await asyncio.wait_for(call.task, timeout=2)        # the call ends on its own: the forward loop finished
    assert call.order == []
    call.session.request_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_call_sends_the_resume_note_then_the_pickup_then_the_audio(monkeypatch):
    call = _Call(monkeypatch, resumed=True)

    async def submit_message(role, text):
        call.order.append("note")

    call.session.submit_message.side_effect = submit_message
    await call.start()
    await call.say(b"hello-1")
    assert b"hello-1" not in call.order
    call.hold_pickup.set()
    await call.settle()
    await call.say(b"hello-2")
    assert call.order == ["note", "pickup", b"hello-2"]
    await call.stop()
