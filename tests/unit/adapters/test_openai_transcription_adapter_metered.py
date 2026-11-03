"""
Behind-port tests of `OpenAITranscriptionAdapter.transcribe_metered` (EXTERNAL_FILES_RFC §4.4 P21, "Metered").

The SDK boundary is faked (`adapter.client` is a stand-in for `AsyncOpenAI` whose `audio.transcriptions.create` returns
the SDK's own `Transcription` type), per docs/how_to/ADAPTER_WIRE_TESTING.md: the request must be the one `transcribe`
sends, and the usage is read from both of the SDK's shapes (tokens, duration) and from a response without one.
"""
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from openai.types.audio.transcription import Transcription, UsageDuration, UsageTokens

from src.adapters.openai_transcription_adapter import OpenAITranscriptionAdapter
from src.domain.audio_transcript import AudioTranscript

LINE = "Lukas Weber owns the Q3 budget."


class _FakeAsyncOpenAI:
    """`AsyncOpenAI` as far as the adapter reaches: `audio.transcriptions.create(**kwargs)`."""

    def __init__(self, response: Transcription) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._response = response
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Transcription:
        self.calls.append(kwargs)
        return self._response


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / "meeting_2026_09_14.m4a"
    path.write_bytes(b"fake-audio-bytes")
    return str(path)


def _adapter(response: Transcription, model: str = "") -> OpenAITranscriptionAdapter:
    adapter = OpenAITranscriptionAdapter(api_key="test-key", model=model or None)
    adapter.client = _FakeAsyncOpenAI(response)
    return adapter


async def test_token_shaped_usage_is_reported(recording):
    usage = UsageTokens(type="tokens", input_tokens=1200, output_tokens=40, total_tokens=1240)
    adapter = _adapter(Transcription(text=f"  {LINE}\n", usage=usage))

    got = await adapter.transcribe_metered(recording, "audio/mp4")

    assert got == AudioTranscript(text=LINE, model="gpt-transcribe", input_tokens=1200, output_tokens=40, seconds=0.0)


async def test_duration_shaped_usage_is_reported(recording):
    adapter = _adapter(Transcription(text=LINE, usage=UsageDuration(type="duration", seconds=93.5)),
                       model="whisper-1")

    got = await adapter.transcribe_metered(recording, "audio/mp4")

    assert got == AudioTranscript(text=LINE, model="whisper-1", input_tokens=0, output_tokens=0, seconds=93.5)


async def test_no_usage_reports_zeros(recording):
    adapter = _adapter(Transcription(text=LINE))

    got = await adapter.transcribe_metered(recording, "audio/mp4")

    assert got == AudioTranscript(text=LINE, model="gpt-transcribe")


async def test_the_metered_request_is_the_plain_one(recording):
    """Same file tuple, model and languages as `transcribe`: metering changes what is read back, never what is sent."""
    response = Transcription(text=LINE, usage=UsageDuration(type="duration", seconds=1.0))
    plain, metered = _adapter(response), _adapter(response)

    text = await plain.transcribe(recording, "audio/mp4", languages=["en", "es"])
    got = await metered.transcribe_metered(recording, "audio/mp4", languages=["en", "es"])

    assert plain.client.calls == metered.client.calls == [{
        "file": ("meeting_2026_09_14.m4a", b"fake-audio-bytes", "audio/mp4"),
        "model": "gpt-transcribe", "languages": ["en", "es"]}]
    assert text == got.text == LINE


async def test_a_failure_raises_as_transcribe_does(recording):
    adapter = OpenAITranscriptionAdapter(api_key="test-key")

    async def _fail(**kwargs):
        raise RuntimeError("upstream 500")

    adapter.client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=_fail)))
    with pytest.raises(RuntimeError, match="upstream 500"):
        await adapter.transcribe_metered(recording, "audio/mp4")
