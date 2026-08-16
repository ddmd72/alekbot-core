"""
AudioTranscriptionPort
======================

Port for audio-to-text transcription (mp3, wav, m4a, ogg → plain text).

## Why a port

Audio transcription is a system boundary (external speech recognition service).
The port allows swapping implementations without changing ConversationHandler and
file_conversion_service: just create a new adapter and wire it in main.py.

## Current status: LIVE

`OpenAITranscriptionAdapter` is wired in main.py when OPENAI_API_KEY is set; without a
key the port stays None and ConversationHandler sends an honest "transcription
unavailable" alert.

## Two callers, two meanings

- **Voice messages** (Slack voice memo, Telegram voice note) are transcribed in
  ConversationHandler, and the transcript becomes the user's own message text — down the
  file path it would be a reference-only part that never reaches session history.
- **Attached audio files** (an uploaded mp3) go through `convert_file_to_text`, and the
  transcript is file content wrapped in `[File: …]`.

## History

An earlier SpeechRecognitionAdapter (markitdown → Google Web Speech API, free) was
abandoned: ~50 requests/day without a key, English only (UnknownValueError on
Russian/Ukrainian), and zero quality on Cloud Run.

## Supported formats (when adapter is present)

audio/mpeg (mp3), audio/wav (wav), audio/mp4 (m4a), audio/x-m4a (m4a alt), audio/ogg (ogg)
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence


class AudioTranscriptionPort(ABC):
    """Abstract port for audio-to-text transcription."""

    @abstractmethod
    async def transcribe(
        self,
        local_path: str,
        mime_type: str,
        languages: Optional[Sequence[str]] = None,
    ) -> str:
        """
        Transcribe an audio file to plain text.

        Args:
            local_path: Absolute path to the audio file on disk.
            mime_type: MIME type of the audio file (e.g. "audio/mpeg").
            languages: Languages the speaker may use, ISO-639-1. A multilingual
                household is the normal case, so this is a list, not one code.
                Order carries no known meaning — the provider treats them as
                *possible* languages. None → let the provider auto-detect.

        Returns:
            Transcribed text content.

        Raises:
            Exception: On transcription failure. Caller handles graceful degradation.
        """
