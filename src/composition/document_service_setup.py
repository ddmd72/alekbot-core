"""
The main service's document service (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4, §4.5, D1, D6, D14).

`build_document_service` assembles the shared pieces the drive indexer also runs (`drive_model_setup.py`), for the
main service:
- ONE child-process parser (`SubprocessContentExtractor`) that is both the parser and the PDF splitter, as on the
  indexer: in this runtime its slot is shared by every user's opens, page counts and window cuts, sized by
  `MAIN_PARSER_MAX_CHILDREN`. Its children run as the image's unprivileged parser user (`DRIVE_PARSER_USER`); on Cloud
  Run without one the start is refused (`subprocess_content_extractor._resolve_sandbox`).
- the no-tool reader (`DriveReaderRunner`) over the main service's own context builder (one breaker per provider, the
  alerting proxy; the reader's strategy allows only Claude and OpenAI) and a plain `PromptBuilder` over the shared
  assembly service. Not the indexer's `_DrivePromptBuilder`: its whole-cache invalidation also clears the assembly
  cache, which here serves every agent.
- the transcriber, the transcript cache and the graph's one quota service, passed through.
"""
from __future__ import annotations

from typing import Any, Optional

from ..adapters.subprocess_content_extractor import SubprocessContentExtractor
from ..ports.audio_transcription_port import AudioTranscriptionPort
from ..ports.quota_service import QuotaService
from ..ports.transcript_cache_port import TranscriptCachePort
from ..ports.user_repository import UserRepository
from ..services.document_service import DocumentService
from ..services.prompt_builder import PromptBuilder
from .drive_reader_runner import DriveReaderRunner

# Parser children the main service runs at once. A placeholder until spike U0-c measures the instance's headroom
# (EXTERNAL_FILES_RFC §4.4 "Parser slot", §8).
MAIN_PARSER_MAX_CHILDREN = 1


def build_document_service(*, context_builder: Any, users: UserRepository, assembly_service: Any,
                           quota: Optional[QuotaService], transcriber: Optional[AudioTranscriptionPort],
                           transcript_cache: Optional[TranscriptCachePort],
                           parser_user: Optional[str]) -> DocumentService:
    """The document service of the main service. `parser_user` is `DRIVE_PARSER_USER` (None locally: the children
    run as this process's user, logged as a warning)."""
    parser = SubprocessContentExtractor(sandbox_user=parser_user, max_children=MAIN_PARSER_MAX_CHILDREN)
    prompt_builder = PromptBuilder(repo=None, assembly_service=assembly_service)
    reader = DriveReaderRunner(context_builder, users, prompt_builder, parser, quota_service=quota)
    return DocumentService(parser, reader, parser, transcriber=transcriber, transcript_cache=transcript_cache,
                           quota=quota)
