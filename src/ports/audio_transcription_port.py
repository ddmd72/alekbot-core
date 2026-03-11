"""
AudioTranscriptionPort
======================

Port for audio-to-text transcription (mp3, wav, m4a, ogg → plain text).

## Why a port

Audio transcription is a system boundary (external speech recognition service).
The port allows swapping implementations without changing ConversationHandler and
file_conversion_service: just create a new adapter and wire it in main.py.

## Current status: NOT IN USE

Audio files are detected (MIME audio/*), but audio_service=None everywhere →
ConversationHandler sends an honest alert "transcription unavailable".

## Why it was disabled

Tried SpeechRecognitionAdapter (markitdown → Google Web Speech API, free):
- ~50 requests/day without a key
- English only (UnknownValueError on Russian/Ukrainian)
- Does not work on GCP Cloud Run (no blocks, but quality is zero)

## How to connect a proper implementation

1. Create an adapter, e.g. WhisperAdapter (OpenAI) or GoogleCloudSpeechAdapter:
   src/adapters/whisper_adapter.py → implements AudioTranscriptionPort

2. Pass it in main.py:
   audio_service = WhisperAdapter(api_key=config["OPENAI_API_KEY"])

3. Pass to SlackAdapterFactory.create_adapter(..., audio_service=audio_service)
   and TelegramWebhookAdapter(..., audio_service=audio_service)
   — the DI chain is already ready, nothing else needs to change.

## Supported formats (when adapter is present)

audio/mpeg (mp3), audio/wav (wav), audio/mp4 (m4a), audio/x-m4a (m4a alt), audio/ogg (ogg)
"""

from abc import ABC, abstractmethod


class AudioTranscriptionPort(ABC):
    """Abstract port for audio-to-text transcription."""

    @abstractmethod
    async def transcribe(self, local_path: str, mime_type: str) -> str:
        """
        Transcribe an audio file to plain text.

        Args:
            local_path: Absolute path to the audio file on disk.
            mime_type: MIME type of the audio file (e.g. "audio/mpeg").

        Returns:
            Transcribed text content.

        Raises:
            Exception: On transcription failure. Caller handles graceful degradation.
        """
