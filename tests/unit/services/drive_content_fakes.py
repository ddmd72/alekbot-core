"""Fakes for Slice 2+4 content work (USER_DRIVE_INDEXING_RFC §9, §24): a scripted extractor, a scripted image triage
and a drive with file bytes and thumbnails. They implement the real ports, so a test fails when a port changes."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageReply, TriageResultV2
from src.domain.drive_extraction import (
    PARSER, DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage,
)
from src.domain.drive_index import TriageVerdict
from src.domain.user_drive import DriveFileTooLarge, DriveItemNotFoundError
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.image_triage_port import ImageTriagePort

from .drive_index_fakes import ROOT, FakeDrive


class FakeExtractor(ContentExtractorPort):
    """Outcomes scripted by item name or by a suffix of the temp path; anything unscripted reads the file as UTF-8
    (`text`, or `no_text` when empty). `unavailable=True` raises DriveModelUnavailable, as the reader does in an
    outage. Every request is recorded."""

    def __init__(self, extractor_id: str = PARSER, version: int = 1,
                 outcomes: Optional[Mapping[str, ExtractionOutcome]] = None) -> None:
        self._id = extractor_id
        self._version = version
        self.outcomes: Dict[str, ExtractionOutcome] = dict(outcomes or {})
        self.requests: List[ExtractionRequest] = []
        self.unavailable = False

    @property
    def extractor_id(self) -> str:
        return self._id

    @property
    def version(self) -> int:
        return self._version

    def _scripted(self, request: ExtractionRequest) -> Optional[ExtractionOutcome]:
        if request.name in self.outcomes:
            return self.outcomes[request.name]
        return next((o for key, o in self.outcomes.items() if request.path.endswith(key)), None)

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        self.requests.append(request)
        if self.unavailable:
            raise DriveModelUnavailable("scripted outage")
        outcome = self._scripted(request)
        if outcome is None:
            with open(request.path, "rb") as fh:
                text = fh.read().decode("utf-8", errors="replace")
            status = ExtractionStatus.TEXT if text.strip() else ExtractionStatus.NO_TEXT
            outcome = ExtractionOutcome(status=status, text=text)
        if outcome.status is ExtractionStatus.TEXT and not outcome.extracted_by:
            outcome = replace(outcome, extracted_by=f"{self._id}@{self._version}")
        return outcome


class FakeTriage(ImageTriagePort):
    """Triage v2 (USER_DRIVE_INDEXING_RFC §25.13.1): verdicts scripted by item id as (verdict, description);
    unscripted images are photographs described "a photo". A photograph's result carries a PhotoDescription, a text
    image's none. `unavailable=True` answers the batch with the `unavailable` status and no results (an outage is a
    status, never an exception). Every batch is recorded and costs one call at `cost_per_call`."""

    def __init__(self, verdicts: Optional[Mapping[str, Tuple[TriageVerdict, str]]] = None,
                 default: TriageVerdict = TriageVerdict.PHOTO, cost_per_call: float = 0.0) -> None:
        self.cost_per_call = cost_per_call
        self.verdicts: Dict[str, Tuple[TriageVerdict, str]] = dict(verdicts or {})
        self.default = default
        self.calls: List[List[TriageImage]] = []
        self.unavailable = False

    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageReply:
        self.calls.append(list(images))
        if self.unavailable:
            return TriageReply(status=ModelReplyStatus.UNAVAILABLE, results=(), model="", usd=0.0)
        results: List[TriageResultV2] = []
        for image in images:
            verdict, description = self.verdicts.get(
                image.item_id, (self.default, "a photo" if self.default is TriageVerdict.PHOTO else ""))
            photo = PhotoDescription(description=description) if verdict is TriageVerdict.PHOTO else None
            results.append(TriageResultV2(item_id=image.item_id, verdict=verdict, photo=photo))
        return TriageReply(status=ModelReplyStatus.OK, results=tuple(results), model="fake-triage" if images else "",
                           usd=self.cost_per_call if images else 0.0, model_calls=1 if images else 0)


class FakeContentDrive(FakeDrive):
    """FakeDrive with file bytes and thumbnails. `download_to_file` writes what fits under `max_bytes` and then
    raises DriveFileTooLarge, as a cut stream does; `download_errors` scripts one exception per item id."""

    def __init__(self, page_size: int = 50, reemit_parents: bool = False) -> None:
        super().__init__(page_size=page_size, reemit_parents=reemit_parents)
        self.content: Dict[str, bytes] = {}
        self.thumbnails: Dict[str, bytes] = {}
        self.download_errors: Dict[str, Exception] = {}
        self.downloads: List[str] = []
        self.thumbnail_calls: List[Tuple[str, str]] = []
        self._versions = 1

    def add_file(self, name: str, data: bytes, parent: str = ROOT, thumbnail: Optional[bytes] = None,
                 **kw: Any) -> str:
        """A file whose size is its bytes; `thumbnail` is what the provider renders for it (None: no rendition)."""
        item_id = self.add(name, parent, size_bytes=len(data), **kw)
        self.content[item_id] = data
        if thumbnail is not None:
            self.thumbnails[item_id] = thumbnail
        return item_id

    def edit(self, item_id: str, data: bytes, **kw: Any) -> None:
        """New content: a new cTag and size, logged like any change (`modified_at` etc. via kw)."""
        self._versions += 1
        self.content[item_id] = data
        self.items[item_id] = replace(self.items[item_id], content_version=f"c{self._versions}",
                                      size_bytes=len(data), **kw)
        self.log.append(self.items[item_id])

    async def download_to_file(self, handle: Any, item_id: str, dest_path: str, max_bytes: int) -> int:
        self.downloads.append(item_id)
        if item_id in self.download_errors:
            raise self.download_errors.pop(item_id)
        if item_id not in self.items:
            raise DriveItemNotFoundError(item_id)
        data = self.content.get(item_id, b"")
        with open(dest_path, "wb") as fh:
            fh.write(data[:max_bytes])
        if len(data) > max_bytes:
            raise DriveFileTooLarge(max_bytes)
        return len(data)

    async def get_thumbnail(self, handle: Any, item_id: str, max_edge_px: int) -> Optional[bytes]:
        self.thumbnail_calls.append((item_id, max_edge_px))
        if item_id not in self.items:
            raise DriveItemNotFoundError(item_id)
        return self.thumbnails.get(item_id)
