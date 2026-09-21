import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.domain.voice_audio_frame import AudioFrame


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self.sent: list[dict] = []
        self._incoming = incoming

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_open_sends_ga_session_shape_with_cache_boundary_stripped():
    ws = FakeWebSocket(incoming=[])
    connect = AsyncMock(return_value=ws)
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=connect)

    await adapter.open(
        instructions="You are Lelik.\n<!-- CACHE_BOUNDARY -->\nDynamic bit.",
        reasoning_effort="medium",
        tools=[],
    )

    connect.assert_awaited_once()
    call_kwargs = connect.await_args.kwargs
    assert call_kwargs["additional_headers"] == {"Authorization": "Bearer sk-test"}
    assert "model=gpt-realtime-2.1" in connect.await_args.args[0]

    sent = ws.sent[0]
    assert sent["type"] == "session.update"
    session = sent["session"]
    assert session["type"] == "realtime"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["format"]["type"] == "audio/pcmu"
    assert session["audio"]["output"]["format"]["type"] == "audio/pcmu"
    assert session["reasoning"]["effort"] == "medium"
    assert "CACHE_BOUNDARY" not in session["instructions"]
    assert "truncation" not in session


@pytest.mark.asyncio
async def test_send_audio_forwards_base64_payload_unchanged():
    ws = FakeWebSocket(incoming=[])
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    frame = AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload=b"already-base64", track="inbound")
    await adapter.send_audio(frame)

    sent = ws.sent[-1]
    assert sent == {"type": "input_audio_buffer.append", "audio": "already-base64"}


@pytest.mark.asyncio
async def test_receive_events_normalizes_audio_delta_and_usage():
    incoming = [
        {"type": "response.output_audio.delta", "delta": "b64chunk"},
        {"type": "response.done", "response": {"usage": {"input_tokens": 10, "output_tokens": 5}}},
    ]
    ws = FakeWebSocket(incoming=incoming)
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    events = [event async for event in adapter.receive_events()]

    assert events[0].type == "audio_delta"
    assert events[0].payload["frame"].payload == "b64chunk"
    assert events[1].type == "response_done"
    assert events[1].payload["usage"] == {"input_tokens": 10, "output_tokens": 5}


@pytest.mark.asyncio
async def test_open_enables_input_audio_transcription():
    ws = FakeWebSocket(incoming=[])
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))

    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    session = ws.sent[0]["session"]
    # Without this field, OpenAI never emits conversation.item.input_audio_transcription.completed
    # and user_transcript in _normalize() is unreachable in production.
    assert session["audio"]["input"]["transcription"] == {"model": "gpt-transcribe"}


@pytest.mark.asyncio
async def test_receive_events_normalizes_user_transcript():
    incoming = [
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hello there"},
    ]
    ws = FakeWebSocket(incoming=incoming)
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    events = [event async for event in adapter.receive_events()]

    assert events[0].type == "user_transcript"
    assert events[0].payload == {"text": "hello there"}


@pytest.mark.asyncio
async def test_receive_events_normalizes_model_transcript():
    incoming = [
        {"type": "response.output_audio_transcript.done", "transcript": "hi Dmytro"},
    ]
    ws = FakeWebSocket(incoming=incoming)
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    events = [event async for event in adapter.receive_events()]

    assert events[0].type == "model_transcript"
    assert events[0].payload == {"text": "hi Dmytro"}


@pytest.mark.asyncio
async def test_submit_tool_result_sends_function_call_output():
    ws = FakeWebSocket(incoming=[])
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    await adapter.submit_tool_result(call_id="call_1", output="the answer")

    sent = ws.sent[-1]
    assert sent == {
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": "call_1", "output": "the answer"},
    }


@pytest.mark.asyncio
async def test_submit_message_sends_system_role_item():
    ws = FakeWebSocket(incoming=[])
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    await adapter.submit_message(role="system", text="Alek's answer just arrived: teal")

    sent = ws.sent[-1]
    assert sent["type"] == "conversation.item.create"
    assert sent["item"]["role"] == "system"
    assert sent["item"]["content"][0]["text"] == "Alek's answer just arrived: teal"
