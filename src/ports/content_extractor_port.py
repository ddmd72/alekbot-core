"""
ContentExtractorPort — one file on local disk to text (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.3).

Several implementations: the child-process parser and the document reader agent, chained per kind by
`drive_extraction.EXTRACTOR_CHAINS`. Composition builds the registry `{extractor_id: adapter}`; the record stores
`extracted_by` as `extractor_id@version`, so a new parser for one kind re-extracts that kind only (§12.3).
"""
from abc import ABC, abstractmethod

from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest


class ContentExtractorPort(ABC):

    @property
    @abstractmethod
    def extractor_id(self) -> str:
        """The id `EXTRACTOR_CHAINS` names this extractor by (e.g. `drive_extraction.PARSER`)."""

    @property
    @abstractmethod
    def version(self) -> int:
        """Bumped when this extractor's output for the same file changes."""

    @abstractmethod
    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        """Text of `request.path`. Every per-file failure is an outcome (`no_text`, `timeout`, `error`, ...), never
        an exception. The one exception is the reader's DriveModelUnavailable: the model cannot be used now (429,
        5xx, timeout, a missing prompt), which defers model work for the user, not this file (§24.8)."""
