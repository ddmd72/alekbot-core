import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter, _flatten_usage
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
async def test_receive_events_flattens_real_nested_openai_usage_shape():
    """OpenAI's real response.done usage is nested (verified against
    developers.openai.com's live reference + community-reported payloads,
    checked 2026-09-21) - NOT the flat audio_input_tokens/etc keys a naive
    reading of RFC §6 might assume. _normalize must flatten it into the leg
    keys VoiceCallBuffer.add_usage / calculate_realtime_cost expect, or
    add_usage's **usage unpacking would set a bucket key to a nested dict
    and crash its `bucket.get(key, 0) + value` arithmetic (0 + {dict})."""
    nested_usage = {
        "total_tokens": 400,
        "input_tokens": 200,
        "output_tokens": 200,
        "input_token_details": {
            "text_tokens": 120,
            "audio_tokens": 80,
            "image_tokens": 0,
            "cached_tokens": 40,
            "cached_tokens_details": {"text_tokens": 40, "audio_tokens": 0, "image_tokens": 0},
        },
        "output_token_details": {"text_tokens": 50, "audio_tokens": 150},
    }
    incoming = [{"type": "response.done", "response": {"usage": nested_usage}}]
    ws = FakeWebSocket(incoming=incoming)
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    events = [event async for event in adapter.receive_events()]

    assert events[0].payload["usage"] == {
        "audio_input_tokens": 80,
        "audio_output_tokens": 150,
        "text_input_tokens": 80,
        "text_output_tokens": 50,
        "cached_tokens": 40,
    }
    # Every value is a plain int - safe to unpack into VoiceCallBuffer.add_usage(**usage).
    assert all(isinstance(v, int) for v in events[0].payload["usage"].values())


def test_flatten_usage_folds_reasoning_tokens_into_text_output():
    """Reasoning tokens bill as text output (RFC §4.3/§6). The Realtime API
    does not currently break reasoning_tokens out as its own visible field
    (community-reported: folded into output_token_details.text_tokens), but
    if a future API revision does add one, it must not be silently dropped."""
    usage = {
        "input_token_details": {"text_tokens": 10, "audio_tokens": 0},
        "output_token_details": {"text_tokens": 50, "audio_tokens": 0, "reasoning_tokens": 30},
    }
    assert _flatten_usage(usage)["text_output_tokens"] == 80


def test_flatten_usage_treats_cached_as_text_when_no_modality_breakdown():
    """When the API reports a cached_tokens count without cached_tokens_details
    (no per-modality split), the whole cached count is attributed to text -
    the dominant realtime caching case (cached system instructions/tools) -
    rather than left inside audio_input_tokens where it would be double-billed
    at both the full audio rate and the cached rate."""
    usage = {
        "input_token_details": {"text_tokens": 100, "audio_tokens": 50, "cached_tokens": 20},
        "output_token_details": {"text_tokens": 0, "audio_tokens": 0},
    }
    flat = _flatten_usage(usage)
    assert flat["text_input_tokens"] == 80
    assert flat["audio_input_tokens"] == 50
    assert flat["cached_tokens"] == 20


def test_flatten_usage_passes_through_undifferentiated_totals_unchanged():
    """Some realtime deployments only return top-level input_tokens/output_tokens
    with no *_token_details breakdown at all (community-reported). There is no
    way to split an undifferentiated total into audio/text legs, so this must
    pass the usage through unchanged rather than inventing a split - matching
    the existing test_receive_events_normalizes_audio_delta_and_usage fixture."""
    usage = {"input_tokens": 10, "output_tokens": 5}
    assert _flatten_usage(usage) == usage


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
