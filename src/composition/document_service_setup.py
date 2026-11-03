"""
The main service's document service (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4, §4.5, D1, D6, D14).

`build_document_service` assembles the shared pieces the drive indexer also runs (`drive_model_setup.py`), for the
main service:
- ONE parse adapter, the caller's, that is both the parser and the PDF splitter. The main service passes the sandbox's
  (`build_sandbox_parser`, the light service): this runtime runs no parser child.
- the no-tool reader (`DriveReaderRunner`) over the main service's own context builder (one breaker per provider, the
  alerting proxy; the reader's strategy allows only Claude and OpenAI) and a plain `PromptBuilder` over the shared
  assembly service. Not the indexer's `_DrivePromptBuilder`: its whole-cache invalidation also clears the assembly
  cache, which here serves every agent.
- the transcriber, the transcript cache and the graph's one quota service, passed through.
"""
from __future__ import annotations

from typing import Any, Optional

from ..ports.audio_transcription_port import AudioTranscriptionPort
from ..ports.quota_service import QuotaService
from ..ports.transcript_cache_port import TranscriptCachePort
from ..ports.user_repository import UserRepository
from ..services.document_service import DocumentService
from ..services.prompt_builder import PromptBuilder
from .drive_reader_runner import DriveReaderRunner


def build_document_service(*, context_builder: Any, users: UserRepository, assembly_service: Any,
                           quota: Optional[QuotaService], transcriber: Optional[AudioTranscriptionPort],
                           transcript_cache: Optional[TranscriptCachePort],
                           parser: Any) -> DocumentService:
    """The document service of the main service. `parser` is one object implementing `ContentExtractorPort` and
    `PdfSplitterPort` (the sandbox's parse adapter)."""
    prompt_builder = PromptBuilder(repo=None, assembly_service=assembly_service)
    reader = DriveReaderRunner(context_builder, users, prompt_builder, parser, quota_service=quota)
    return DocumentService(parser, reader, parser, transcriber=transcriber, transcript_cache=transcript_cache,
                           quota=quota)
