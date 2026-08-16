"""
Wire tests for OpenAITranscriptionAdapter.

Mocked at the SDK boundary (`client.audio.transcriptions.create`) per
docs/how_to/ADAPTER_WIRE_TESTING.md — a port-level mock cannot detect
translation regressions (file tuple shape, model, vocabulary prompt).
"""

import os
import pytest
from unittest.mock import MagicMock

from src.adapters.openai_transcription_adapter import OpenAITranscriptionAdapter
from src.ports.audio_transcription_port import AudioTranscriptionPort


def _make_transcription(text: str):
    result = MagicMock()
    result.text = text
    return result


def _install(adapter, captured: dict, *, text: str = "распознанный текст", raises=None):
    async def mock_create(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        return _make_transcription(text)

    adapter.client.audio.transcriptions.create = mock_create


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "audio_message.m4a"
    path.write_bytes(b"fake-audio-bytes")
    return str(path)


class TestOpenAITranscriptionAdapterWire:

    def test_implements_port(self):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        assert isinstance(adapter, AudioTranscriptionPort)

    async def test_sends_file_tuple_with_name_bytes_and_mime(self, audio_file):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(audio_file, "audio/mp4")

        # The API infers the container format from the filename extension,
        # so the basename must survive into the request.
        assert captured["file"] == ("audio_message.m4a", b"fake-audio-bytes", "audio/mp4")

    async def test_sends_default_model(self, audio_file):
        """`gpt-transcribe` is the only family that accepts `languages`."""
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(audio_file, "audio/mp4")

        assert captured["model"] == "gpt-transcribe"

    async def test_model_override_is_sent(self, audio_file):
        adapter = OpenAITranscriptionAdapter(api_key="test-key", model="gpt-4o-transcribe")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(audio_file, "audio/mp4")

        assert captured["model"] == "gpt-4o-transcribe"

    async def test_languages_are_sent_in_order(self, audio_file):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(audio_file, "audio/mp4", languages=["ru", "uk", "en"])

        assert captured["languages"] == ["ru", "uk", "en"]

    async def test_languages_omitted_when_not_set(self, audio_file):
        """Absent means auto-detect; an empty list would pin the recogniser to nothing."""
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(audio_file, "audio/mp4")

        assert "languages" not in captured

    async def test_empty_language_list_is_omitted(self, audio_file):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(audio_file, "audio/mp4", languages=[])

        assert "languages" not in captured

    async def test_no_language_bound_prompt_is_sent(self, audio_file):
        """A prose prompt must match the audio language, which is a per-user setting —
        so the adapter must not carry one of its own."""
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(audio_file, "audio/mp4")

        assert "prompt" not in captured

    async def test_returns_stripped_text(self, audio_file):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured, text="  напомни завтра позвонить  ")

        result = await adapter.transcribe(audio_file, "audio/mp4")

        assert result == "напомни завтра позвонить"

    async def test_empty_transcription_returns_empty_string(self, audio_file):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured, text="")

        assert await adapter.transcribe(audio_file, "audio/mp4") == ""

    async def test_ogg_mime_passes_through(self, tmp_path):
        # Telegram voice notes arrive as ogg/opus.
        path = tmp_path / "voice.ogg"
        path.write_bytes(b"ogg-bytes")
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(str(path), "audio/ogg")

        assert captured["file"] == ("voice.ogg", b"ogg-bytes", "audio/ogg")

    async def test_oga_extension_is_rewritten_to_ogg(self, tmp_path):
        """Telegram serves voice at `…/file_0.oga`, and the download layer names the temp
        file after that URL. `oga` is valid Ogg but the API rejects the name (400
        'Unsupported file format oga'), so the extension comes from the mime type."""
        path = tmp_path / "tmpaxdb_file_0.oga"
        path.write_bytes(b"ogg-bytes")
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(str(path), "audio/ogg")

        assert captured["file"] == ("tmpaxdb_file_0.ogg", b"ogg-bytes", "audio/ogg")

    async def test_slack_video_mp4_voice_memo_gets_mp4_extension(self, tmp_path):
        path = tmp_path / "tmp_audio_message"
        path.write_bytes(b"mp4-bytes")
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(str(path), "video/mp4")

        assert captured["file"][0] == "tmp_audio_message.mp4"

    async def test_unknown_mime_keeps_the_path_extension(self, tmp_path):
        path = tmp_path / "recording.wav"
        path.write_bytes(b"wav-bytes")
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        await adapter.transcribe(str(path), "application/octet-stream")

        assert captured["file"][0] == "recording.wav"

    async def test_provider_error_propagates(self, audio_file):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured, raises=RuntimeError("boom"))

        # The port contract says the caller handles degradation; file_conversion
        # turns this into a [System: ...] alert.
        with pytest.raises(RuntimeError):
            await adapter.transcribe(audio_file, "audio/mp4")

    async def test_missing_file_raises(self):
        adapter = OpenAITranscriptionAdapter(api_key="test-key")
        captured = {}
        _install(adapter, captured)

        with pytest.raises(FileNotFoundError):
            await adapter.transcribe("/nonexistent/path/voice.ogg", "audio/ogg")
