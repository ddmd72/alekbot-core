"""DocumentService's frozen core (EXTERNAL_FILES_RFC §4.4, P13/D18): it is the read port, its allowance is the reader's
per-file ceiling for every store, and `read` answers an outcome (not built yet) instead of raising."""
from typing import Optional, Sequence, Tuple

import pytest

from src.domain.document_read import ReadOutcome, ReadRequest, ReadStatus
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_index_params import MODEL_EXTRACT_MAX_PAGES
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.document_read_port import DocumentReadPort
from src.ports.pdf_splitter_port import PdfSplitterPort
from src.services.document_service import DocumentService


class _Extractor(ContentExtractorPort):
    def __init__(self, extractor_id: str) -> None:
        self.requests = []
        self._id = extractor_id

    @property
    def extractor_id(self) -> str:
        return self._id

    @property
    def version(self) -> int:
        return 1

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        self.requests.append(request)
        return ExtractionOutcome(status=ExtractionStatus.TEXT, text="x")


class _Splitter(PdfSplitterPort):
    def __init__(self) -> None:
        self.calls = 0

    async def pdf_page_count(self, path: str, *, deadline_at: Optional[float] = None) -> int:
        self.calls += 1
        return 1

    async def cut_pdf_window(self, path: str, first_page: int, page_count: int, out_path: str,
                             *, deadline_at: Optional[float] = None) -> None:
        self.calls += 1

    async def cut_pdf_windows(self, path: str, windows: Sequence[Tuple[int, int]], out_paths: Sequence[str],
                              *, deadline_at: Optional[float] = None) -> None:
        self.calls += 1


@pytest.fixture
def parts():
    return _Extractor("parser"), _Extractor("reader"), _Splitter()


def test_the_service_is_the_read_port(parts):
    assert isinstance(DocumentService(*parts), DocumentReadPort)


@pytest.mark.parametrize("store", ["chat", "drive", "email", "workspace", "delivered"])
def test_the_allowance_is_the_readers_ceiling_for_every_store(parts, store):
    assert DocumentService(*parts).allowance("ann", "ann", store) == MODEL_EXTRACT_MAX_PAGES == 200


async def test_read_is_an_outcome_until_part_a(parts):
    parser, reader, splitter = parts
    request = ReadRequest(content=b"%PDF-1.7", name="scan_0012.pdf", declared_mime="application/pdf", store="chat",
                          caller="open", native_formats=(), user_id="ann", account_id="ann", deadline_at=0.0,
                          request_id="r1")
    outcome = await DocumentService(parser, reader, splitter).read(request)
    assert outcome == ReadOutcome(status=ReadStatus.ERROR, detail="not built")
    assert parser.requests == reader.requests == [] and splitter.calls == 0
