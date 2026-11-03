"""
DocumentService — the document service of the main runtime (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4, D1, D6, P13,
P21). One public class.

Bytes in, outcome out (P2): the open path stages a file and calls `read`; the service kind-checks the bytes, applies
the caps, decides native or pipeline (the parser in its child, the text-or-scan router, the reader within the
deadline) and returns a closed-schema `ReadOutcome`. It keeps nothing between calls and holds no store credential.

Its seams are ports, so the sandbox's parse slices swap adapters under it with no caller change (D14): the parser and
the reader are `ContentExtractorPort`s, the page operations a `PdfSplitterPort` (in the main service the parser adapter
serves both, `composition/document_service_setup.py`). The transcriber and the transcript cache serve P21's chat
audio; the quota service bills what the service itself spends.

Built in U3's core with its contract only: `read` answers ERROR "not built" until part A.
"""
from __future__ import annotations

from typing import Optional

from ..domain.document_read import ReadOutcome, ReadRequest, ReadStatus
from ..domain.drive_index_params import MODEL_EXTRACT_MAX_PAGES
from ..ports.audio_transcription_port import AudioTranscriptionPort
from ..ports.content_extractor_port import ContentExtractorPort
from ..ports.document_read_port import DocumentReadPort
from ..ports.pdf_splitter_port import PdfSplitterPort
from ..ports.quota_service import QuotaService
from ..ports.transcript_cache_port import TranscriptCachePort


class DocumentService(DocumentReadPort):

    def __init__(self, parser: ContentExtractorPort, reader: ContentExtractorPort, splitter: PdfSplitterPort, *,
                 transcriber: Optional[AudioTranscriptionPort] = None,
                 transcript_cache: Optional[TranscriptCachePort] = None,
                 quota: Optional[QuotaService] = None) -> None:
        self._parser = parser
        self._reader = reader
        self._splitter = splitter
        self._transcriber = transcriber
        self._transcript_cache = transcript_cache
        self._quota = quota

    def allowance(self, user_id: str, account_id: str, store: str) -> int:
        """Pages the reader may read for one open (P13, D18): the reader's per-file ceiling `MODEL_EXTRACT_MAX_PAGES`
        for every store, the drive included. An interactive open books nothing against the drive's monthly reader
        pages, so `BUDGET_EXHAUSTED` is unreachable from the service. The one place a later budget replaces (D5)."""
        return MODEL_EXTRACT_MAX_PAGES

    async def read(self, request: ReadRequest) -> ReadOutcome:
        """See `DocumentReadPort.read`. Not built yet (U3 part A)."""
        return ReadOutcome(status=ReadStatus.ERROR, detail="not built")
