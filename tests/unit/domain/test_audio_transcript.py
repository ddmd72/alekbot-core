"""AudioTranscript (EXTERNAL_FILES_RFC P21): a transcription's text, model and usage; the usage defaults to zeros."""
import dataclasses

import pytest

from src.domain.audio_transcript import AudioTranscript


def test_usage_defaults_to_zero():
    got = AudioTranscript(text="Lukas Weber owns the Q3 budget.", model="gpt-transcribe")
    assert (got.input_tokens, got.output_tokens, got.seconds) == (0, 0, 0.0)


def test_it_is_frozen():
    got = AudioTranscript(text="", model="gpt-transcribe", seconds=60.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.seconds = 1.0  # type: ignore[misc]
