"""
OpenAITranscriptionAdapter
==========================

AudioTranscriptionPort over OpenAI's `/v1/audio/transcriptions`.

Default model is `gpt-transcribe`: it is the only family that accepts `languages`, and a
multilingual speaker is the normal case here, not an edge one. Env-overridable
(`OPENAI_TRANSCRIPTION_MODEL`) the same way the deep-research adapters pin theirs.
"""

import asyncio
import os
from typing import Optional, Sequence

from openai import AsyncOpenAI

from ..ports.audio_transcription_port import AudioTranscriptionPort
from ..utils.logger import logger

# The API infers the container from the filename extension, and accepts only
# flac/mp3/mp4/mpeg/mpga/m4a/ogg/wav/webm. The path we are handed is a temp file named
# after the download URL, so its extension is an artifact of the platform, not a fact
# about the audio: Telegram voice notes arrive as `.oga` (valid Ogg, rejected by name).
# The mime type is the reliable input, so the extension is derived from it.
_EXTENSION_BY_MIME = {
    "audio/ogg": ".ogg",
    "audio/oga": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",  # Slack voice memos are served as video/mp4 on some paths
}


class OpenAITranscriptionAdapter(AudioTranscriptionPort):
    """Transcribe audio files to text via OpenAI speech-to-text models."""

    DEFAULT_MODEL = "gpt-transcribe"

    def __init__(self, api_key: str, model: Optional[str] = None) -> None:
        self.client = AsyncOpenAI(api_key=api_key, timeout=120.0, max_retries=2)
        self.model = model or self.DEFAULT_MODEL
        logger.info(f"✅ [OpenAITranscriptionAdapter] Initialized: model={self.model}")

    async def transcribe(
        self,
        local_path: str,
        mime_type: str,
        languages: Optional[Sequence[str]] = None,
    ) -> str:
        def _read() -> bytes:
            with open(local_path, "rb") as f:
                return f.read()

        audio_bytes = await asyncio.to_thread(_read)
        stem, path_ext = os.path.splitext(os.path.basename(local_path))
        filename = stem + _EXTENSION_BY_MIME.get(mime_type.lower(), path_ext)

        # Omitted rather than defaulted: an empty list would pin the recogniser to nothing,
        # while an absent param lets it auto-detect.
        extra = {"languages": list(languages)} if languages else {}

        try:
            # Tuple form (name, bytes, content_type): the API picks the container format
            # from the filename extension, so the name must keep it.
            result = await self.client.audio.transcriptions.create(
                file=(filename, audio_bytes, mime_type),
                model=self.model,
                **extra,
            )
        except Exception as exc:
            logger.error(
                f"[OpenAITranscriptionAdapter] Transcription failed for '{filename}' "
                f"({mime_type}, {len(audio_bytes)} bytes, languages={list(languages or [])}): "
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            raise

        text = (result.text or "").strip()
        logger.info(
            f"[OpenAITranscriptionAdapter] Transcribed '{filename}' "
            f"({len(audio_bytes)} bytes, languages={list(languages or [])}) → {len(text)} chars"
        )
        return text
