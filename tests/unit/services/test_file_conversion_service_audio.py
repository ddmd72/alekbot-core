"""Opening an uploaded audio file transcribes it through the injected audio service (P9 D3).

`resolve_content` is what `open_file` calls. With an audio service the file's bytes reach `transcribe` and the transcript comes
back in the `[File: …]` wrapper; without one the user gets the honest "transcription is not available" alert.
"""
import mimetypes
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from src.ports.audio_transcription_port import AudioTranscriptionPort
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService

AUDIO_BYTES = b"RIFF....WAVEfmt fake audio bytes"


class MemoryStorage(FileStoragePort):
    """Uploads kept in a dict, keyed by (user, filename)."""

    def __init__(self, files: Dict[Tuple[str, str], bytes]) -> None:
        self.files = files

    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        self.files[(user_id, filename)] = data
        return filename

    async def download(self, filename: str, user_id: str) -> bytes:
        return self.files[(user_id, filename)]

    async def delete(self, filename: str, user_id: str) -> None:
        self.files.pop((user_id, filename), None)

    async def exists(self, filename: str, user_id: str) -> bool:
        return (user_id, filename) in self.files

    async def get_url(self, filename: str, user_id: str) -> str:
        return f"https://storage.test/{user_id}/{filename}"


class RecordingTranscriber(AudioTranscriptionPort):
    """Records what it was asked to transcribe: the bytes found at the path, and the mime type."""

    def __init__(self, transcript: str = "good morning, call the dentist at nine") -> None:
        self.transcript = transcript
        self.calls: List[Tuple[bytes, str]] = []

    async def transcribe(self, local_path: str, mime_type: str, languages: Optional[Sequence[str]] = None) -> str:
        with open(local_path, "rb") as f:
            self.calls.append((f.read(), mime_type))
        return self.transcript


@pytest.fixture
def storage():
    return MemoryStorage({("u1", "memo.m4a"): AUDIO_BYTES})


async def test_an_audio_file_is_transcribed_and_the_transcript_is_returned(storage):
    audio = RecordingTranscriber()
    service = FileConversionService(storage=storage, audio_service=audio)

    text = await service.resolve_content("memo.m4a", "u1")

    assert text == "[File: memo.m4a]\ngood morning, call the dentist at nine\n[/File: memo.m4a]"
    assert audio.calls == [(AUDIO_BYTES, mimetypes.guess_type("memo.m4a")[0])]


async def test_without_an_audio_service_the_user_is_told_transcription_is_unavailable(storage):
    service = FileConversionService(storage=storage)

    text = await service.resolve_content("memo.m4a", "u1")

    assert "Audio transcription is not available" in text and "[File:" not in text


async def test_a_failing_transcriber_gives_the_conversion_alert_not_an_exception(storage):
    class Broken(RecordingTranscriber):
        async def transcribe(self, local_path, mime_type, languages=None):
            raise RuntimeError("provider down")

    text = await FileConversionService(storage=storage, audio_service=Broken()).resolve_content("memo.m4a", "u1")

    assert text.startswith("[System:") and "memo.m4a" in text
