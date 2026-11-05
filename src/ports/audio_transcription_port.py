"""
AudioTranscriptionPort
======================

Port for audio-to-text transcription (mp3, wav, m4a, ogg → plain text).

## Why a port

Audio transcription is a system boundary (external speech recognition service).
The port allows swapping implementations without changing ConversationHandler and
file_conversion_service: just create a new adapter and wire it in build_application_graph.

## Current status: LIVE

`OpenAITranscriptionAdapter` is wired in build_application_graph when OPENAI_API_KEY is set; without a
key the port stays None and ConversationHandler sends an honest "transcription
unavailable" alert.

## Two callers, two meanings

- **Voice messages** (Slack voice memo, Telegram voice note) are transcribed in
  ConversationHandler, and the transcript becomes the user's own message text — down the
  file path it would be a reference-only part that never reaches session history.
- **Attached audio files** (an uploaded mp3) are transcribed by the document service, and the
  transcript is file content wrapped in `[File: …]`.

## History

An earlier SpeechRecognitionAdapter (markitdown → Google Web Speech API, free) was
abandoned: ~50 requests/day without a key, English only (UnknownValueError on
Russian/Ukrainian), and zero quality on Cloud Run.

## Metered transcription (EXTERNAL_FILES_RFC §4.4, P21)

`transcribe_metered` makes the same request as `transcribe` and also returns the provider's usage, so a transcribing
open can bill the account per call. `transcribe` stays for the callers that bill nothing (voice messages).

## Supported formats (when adapter is present)

audio/mpeg (mp3), audio/wav (wav), audio/mp4 (m4a), audio/x-m4a (m4a alt), audio/ogg (ogg)
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from ..domain.audio_transcript import AudioTranscript


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
            languages: Languages the speaker may use, ISO-639-1. A speaker who
                uses several languages is the normal case, so this is a list, not one code.
                Order carries no known meaning — the provider treats them as
                *possible* languages. None → let the provider auto-detect.

        Returns:
            Transcribed text content.

        Raises:
            Exception: On transcription failure. Caller handles graceful degradation.
        """

    @abstractmethod
    async def transcribe_metered(
        self,
        local_path: str,
        mime_type: str,
        languages: Optional[Sequence[str]] = None,
    ) -> AudioTranscript:
        """
        Transcribe as `transcribe` does, and report the model and the provider's usage.

        Usage comes back token-shaped (input/output tokens) or duration-shaped (seconds); what the
        provider does not report is zero.

        Raises:
            Exception: On transcription failure, as `transcribe`.
        """
