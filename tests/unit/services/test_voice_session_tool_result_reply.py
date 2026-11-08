"""Every tool result reaches speech: the reply the relay owes after a delegation answer or a remote MCP call.

No realtime provider speaks a tool result by itself: after a function_call_output, and after the remote MCP calls of
a response that ended holding only those calls, nothing is said until `response.create` (live probe on xAI: a function
result submitted while idle got no response in ten seconds; `response.create` was spoken in well under a second). The
relay starts that reply when the floor is free, after the active response or after the caller's turn, and never twice
when the provider took the result up itself.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import src.services.voice_session_service as voice_module
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.remote_tool_server import RemoteToolServer
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_XAI = VOICE_PROVIDER_PROFILES["xai"].session
_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]
_ANSWER = "Springfield is sunny at 21 degrees with a light westerly wind."


def _config():
    return {"instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS,
            "voice": _XAI.to_dict()}


def _tool_call(call_id="c1"):
    return E(type="tool_call", payload={"call_id": call_id, "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": "search_web", "query": "weather"})})


def _remote(pending=(), finished=(), continued=False, cancelled=False):
    return {"calls": max(len(pending) + len(finished), 1), "pending": list(pending), "finished": list(finished),
            "continued": continued, "cancelled": cancelled}


def _opening():
    return [E(type="response_created", payload={}), E(type="response_done", payload={})]


async def _hold():
    await asyncio.sleep(10)


def _session(events, order):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    session.request_response.side_effect = lambda: order.append("create")
    session.submit_tool_result.side_effect = lambda *_: order.append("result")
    return session


async def _run(session, delegate=None, seconds=0.4, **kwargs):
    control = AsyncMock()
    control.fetch_session_config.return_value = _config()
    control.delegate.side_effect = delegate or AsyncMock(return_value=_ANSWER)

    async def inbound():
        await asyncio.sleep(seconds)
        return
        yield  # pragma: no cover

    await VoiceSessionService(realtime_session_factory=MagicMock(return_value=session), control_plane=control,
                              alert_sink=AsyncMock(), **kwargs).handle_call(
        ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())


def _lines(fake_logger, level="info"):
    return [call.args[0] for call in getattr(fake_logger, level).call_args_list]


class TestDelegationResult:
    async def test_a_result_arriving_while_idle_triggers_a_reply(self, monkeypatch):
        fake_logger = MagicMock()
        monkeypatch.setattr(voice_module, "logger", fake_logger)
        order: list = []

        async def slow(**_):
            await asyncio.sleep(0.05)
            return _ANSWER

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield _tool_call()
            yield E(type="response_done", payload={})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order), delegate=slow)

        assert order == ["create", "result", "create"]  # pickup, the answer, its reply
        assert any("tool result submitted: kind=function call=c1" in line for line in _lines(fake_logger))
        assert any("reply triggered after tool result (waited " in line for line in _lines(fake_logger))

    async def test_a_result_arriving_during_an_active_response_is_replied_to_after_its_response_done(self):
        order: list = []
        release = asyncio.Event()

        async def gated(**_):
            await release.wait()
            return _ANSWER

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield _tool_call()
            yield E(type="response_done", payload={})
            yield E(type="response_created", payload={})  # the provider's own next reply
            release.set()
            await asyncio.sleep(0.05)
            order.append("done")
            yield E(type="response_done", payload={})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order), delegate=gated)

        assert order == ["create", "done", "result", "create"]

    async def test_a_result_arriving_while_the_caller_speaks_is_replied_to_after_their_turn(self):
        order: list = []
        release = asyncio.Event()

        async def gated(**_):
            await release.wait()
            return _ANSWER

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield _tool_call()
            yield E(type="response_done", payload={})
            yield E(type="speech_started", payload={})
            release.set()
            await asyncio.sleep(0.05)  # the answer goes in while the caller speaks
            order.append("turn")
            yield E(type="speech_stopped", payload={})
            yield E(type="turn_committed", payload={})  # nothing replied to the turn on its own
            await _hold()
            yield  # pragma: no cover

        session = _session(events, order)
        await _run(session, delegate=gated)

        assert order == ["create", "turn", "create"]
        assert any(_ANSWER in call.args[1] for call in session.submit_message.await_args_list)

    async def test_the_providers_own_reply_to_the_callers_turn_takes_the_result_up_without_a_second_create(
            self, monkeypatch):
        fake_logger = MagicMock()
        monkeypatch.setattr(voice_module, "logger", fake_logger)
        order: list = []
        release = asyncio.Event()

        async def gated(**_):
            await release.wait()
            return _ANSWER

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield _tool_call()
            yield E(type="response_done", payload={})
            yield E(type="speech_started", payload={})
            release.set()
            await asyncio.sleep(0.05)
            yield E(type="speech_stopped", payload={})
            yield E(type="response_created", payload={})  # the provider replies to the caller's turn
            yield E(type="turn_committed", payload={})
            yield E(type="audio_delta", payload={"frame": MagicMock(), "item_id": "a1"})
            yield E(type="response_done", payload={})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order), delegate=gated)

        assert order == ["create"]  # the pickup only
        lines = _lines(fake_logger)
        assert any("reply started by the provider after tool result" in line for line in lines)
        assert any("first audio after tool result: " in line for line in lines)


class TestRemoteToolResult:
    async def test_a_response_holding_only_running_calls_is_replied_to_once_they_finish(self, monkeypatch):
        fake_logger = MagicMock()
        monkeypatch.setattr(voice_module, "logger", fake_logger)
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="response_done", payload={"remote_tools": _remote(pending=("m1", "m2"))})
            order.append("first")
            yield E(type="remote_tool_done", payload={"item_id": "m1", "label": "alek_memory", "ok": True})
            order.append("second")
            yield E(type="remote_tool_done", payload={"item_id": "m2", "label": "alek_drive", "ok": True})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        assert order == ["create", "first", "second", "create"]
        lines = _lines(fake_logger)
        assert any("tool result submitted: kind=mcp server=alek_memory" in line for line in lines)
        assert any("tool result submitted: kind=mcp server=alek_drive" in line for line in lines)
        assert any("reply triggered after tool result (waited " in line for line in lines)

    async def test_calls_already_finished_at_response_done_are_replied_to_at_once(self, monkeypatch):
        fake_logger = MagicMock()
        monkeypatch.setattr(voice_module, "logger", fake_logger)
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="response_done", payload={"remote_tools": _remote(finished=("alek_drive",))})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        assert order == ["create", "create"]
        assert any("tool result submitted: kind=mcp server=alek_drive" in line for line in _lines(fake_logger))

    async def test_a_call_finishing_during_an_active_response_is_replied_to_after_its_response_done(self):
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="response_done", payload={"remote_tools": _remote(pending=("m1",))})
            yield E(type="response_created", payload={})  # the provider replies to something else meanwhile
            yield E(type="remote_tool_done", payload={"item_id": "m1", "label": "alek_drive", "ok": True})
            order.append("done")
            yield E(type="response_done", payload={})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        assert order == ["create", "done", "create"]

    async def test_a_call_finishing_while_the_caller_speaks_is_replied_to_after_their_turn(self):
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="response_done", payload={"remote_tools": _remote(pending=("m1",))})
            yield E(type="speech_started", payload={})
            yield E(type="remote_tool_done", payload={"item_id": "m1", "label": "alek_drive", "ok": True})
            order.append("turn")
            yield E(type="speech_stopped", payload={})
            yield E(type="turn_committed", payload={})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        assert order == ["create", "turn", "create"]

    async def test_a_response_that_spoke_after_its_calls_gets_no_second_create(self):
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="response_done", payload={"remote_tools": _remote(finished=("alek_drive",), continued=True)})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        assert order == ["create"]

    async def test_a_provider_reply_starting_after_the_calls_finished_takes_them_up_without_a_create(self):
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="speech_started", payload={})  # the caller holds the floor while the call finishes
            yield E(type="response_done", payload={"remote_tools": _remote(pending=("m1",))})
            yield E(type="remote_tool_done", payload={"item_id": "m1", "label": "alek_drive", "ok": True})
            yield E(type="speech_stopped", payload={})
            yield E(type="response_created", payload={})  # the provider continues by itself
            yield E(type="turn_committed", payload={})
            yield E(type="response_done", payload={})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        assert order == ["create"]

    async def test_a_response_the_caller_cut_gets_no_reply_for_its_calls(self):
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="response_done", payload={"remote_tools": _remote(pending=("m1",), cancelled=True)})
            yield E(type="remote_tool_done", payload={"item_id": "m1", "label": "alek_drive", "ok": True})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        assert order == ["create"]

    async def test_a_call_never_reported_finished_still_gets_a_reply_after_the_wait(self, monkeypatch):
        fake_logger = MagicMock()
        monkeypatch.setattr(voice_module, "logger", fake_logger)
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield E(type="response_done", payload={"remote_tools": _remote(pending=("m1",))})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order), remote_tool_wait_s=0.05)

        assert order == ["create", "create"]
        assert any("not reported finished after" in line for line in _lines(fake_logger, "warning"))


class TestLogLinesCarryNoPayload:
    async def test_no_log_line_holds_the_answer_or_the_arguments(self, monkeypatch):
        fake_logger = MagicMock()
        monkeypatch.setattr(voice_module, "logger", fake_logger)
        order: list = []

        async def events():
            for e in _opening():
                yield e
            yield E(type="response_created", payload={})
            yield _tool_call()
            yield E(type="response_done", payload={"remote_tools": _remote(pending=("m1",))})
            await asyncio.sleep(0.05)
            yield E(type="remote_tool_done", payload={"item_id": "m1", "label": "alek_drive", "ok": True})
            yield E(type="response_created", payload={})
            yield E(type="audio_delta", payload={"frame": MagicMock(), "item_id": "a1"})
            yield E(type="response_done", payload={})
            await _hold()
            yield  # pragma: no cover

        await _run(_session(events, order))

        text = "\n".join(str(call.args[0]) for level in ("info", "warning", "error")
                         for call in getattr(fake_logger, level).call_args_list)
        assert "tool result submitted: kind=function" in text and "tool result submitted: kind=mcp" in text
        assert "first audio after tool result: " in text
        assert "Springfield" not in text and "weather" not in text.replace("search_web", "")


class _FakeWebSocket:
    def __init__(self, incoming: list):
        self.sent: list = []
        self._incoming = incoming

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            await asyncio.sleep(10)  # the provider goes quiet; the call ends with the inbound stream
        return json.dumps(self._incoming.pop(0))

    async def close(self) -> None:
        pass


class TestOverTheXaiWire:
    async def test_a_response_done_holding_only_an_mcp_call_is_followed_by_response_create_once_it_finishes(self):
        """The real adapter under the real loop: the wire shape of the call that went unspoken."""
        server = RemoteToolServer("alek_drive", "https://mcp.example.test/drive-mcp", "The owner's indexed files.",
                                  ("search_drive",), token="tok-drive")
        item = {"type": "mcp_call", "id": "m1", "server_label": "alek_drive", "name": "search_drive",
                "arguments": "{}", "status": "in_progress"}
        ws = _FakeWebSocket([
            {"type": "response.created", "response": {}},
            {"type": "response.done", "response": {"status": "completed", "output": []}, "usage": {}},
            {"type": "response.created", "response": {}},
            {"type": "response.output_item.added", "item": item},
            {"type": "response.done", "response": {"status": "completed", "output": [item]}, "usage": {}},
            {"type": "response.mcp_call.completed", "item_id": "m1"},
        ])
        adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))
        control = AsyncMock()
        control.fetch_session_config.return_value = _config()

        async def inbound():
            await asyncio.sleep(0.3)
            return
            yield  # pragma: no cover

        service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=adapter),
                                      control_plane=control, alert_sink=AsyncMock())
        await service._run_call("t1", _config(), _XAI, inbound(), AsyncMock(), AsyncMock(),
                                voice_module._CallState(playback=PlaybackTracker(), ticket="t1",
                                                        turn_ownership=_XAI.turn_ownership),
                                mcp_servers=(server,))

        creates = [index for index, sent in enumerate(ws.sent) if sent["type"] == "response.create"]
        assert len(creates) == 2  # the pickup, then the reply that speaks the call's result
        assert creates[1] == len(ws.sent) - 1
