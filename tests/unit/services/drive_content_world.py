"""A tick harness with content: the real DriveIndexingService on the in-memory repository, a drive with file bytes,
a scripted parser, reader and triage (USER_DRIVE_INDEXING_RFC §8, §24). Admission is switched on by rebinding the
policy sets, as the flip does; once the flip has landed the rebinding is a no-op."""
from __future__ import annotations

import struct

from typing import Callable, Dict, List, Optional

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain import drive_index_policy as policy
from src.domain.drive_extraction import PARSER, READER, ExtractionOutcome, ExtractionRequest
from src.domain.drive_index import DriveChunk, DriveIndexRecord
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS
from src.services.drive_indexing_service import DriveIndexingService, TickResult

from .drive_content_fakes import FakeContentDrive, FakeExtractor, FakeTriage
from .drive_index_fakes import Clock, FakeEmbeddings, FakeQueue, FakeUsers
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

PDF_HEAD = b"%PDF-1.7\n"
# a ZIP local header naming [Content_Types].xml: what an OOXML file starts with, so sniff_kind keeps its kind
OOXML_HEAD = b"PK\x03\x04" + struct.pack("<HHHHHIIIHH", 20, 0, 0, 0, 0, 0, 0, 0, 19, 0) + b"[Content_Types].xml"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


class HookedExtractor(FakeExtractor):
    """FakeExtractor with a hook run before each extraction (a slow file, a concurrent change)."""

    def __init__(self, extractor_id: str = PARSER, version: int = 1) -> None:
        super().__init__(extractor_id, version)
        self.before: Optional[Callable[[ExtractionRequest], None]] = None

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        if self.before is not None:
            self.before(request)
        return await super().extract(request)


class ContentWorld:
    def __init__(self, page_size: int = 50, *, wired: bool = True) -> None:
        self.drive = FakeContentDrive(page_size=page_size)
        self.repo = InMemoryDriveIndexRepository()
        self.emb = FakeEmbeddings()
        self.queue = FakeQueue()
        self.users = FakeUsers()
        self.clock = Clock()
        self.parser = HookedExtractor(PARSER)
        self.reader = HookedExtractor(READER)
        self.triage = FakeTriage()
        extractors = {PARSER: self.parser, READER: self.reader} if wired else None
        self.svc = DriveIndexingService(self.drive, self.repo, self.emb, self.queue, self.users,
                                        lambda lat, lon: "Rome (Roma), Italy", "test-space/8",
                                        extractors=extractors, triage=self.triage if wired else None,
                                        connections=LegacyConnections(self.users, self.drive), clock=self.clock, monotonic=self.clock.monotonic)
        self.results: List[TickResult] = []

    async def tick(self, nudge=None) -> TickResult:
        result = await self.svc.tick(U_SCOPE, nudge)
        self.results.append(result)
        return result

    async def settle(self, max_ticks: int = 60) -> TickResult:
        """Run ticks while each one enqueues a continuation, as the drive queue would (delays elapse)."""
        for _ in range(max_ticks):
            before = len(self.queue.tasks)
            result = await self.tick()
            continued = [t for t in self.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not continued:
                return result
            self.clock.advance(max(1, max(t["delay"] for t in continued)))
        raise AssertionError("did not settle")

    def rec(self, item_id: str) -> Optional[DriveIndexRecord]:
        return self.repo.records.get(("u", item_id))

    def chunks(self, item_id: str) -> List[DriveChunk]:
        return [c for (u, i, _, _), (c, _) in sorted(self.repo.chunks.items()) if u == "u" and i == item_id]

    def generations(self, item_id: str) -> List[int]:
        return sorted({c.generation for c in self.chunks(item_id)})

    def readable(self, item_id: str) -> List[DriveChunk]:
        """Chunks search may return (§7.4): the record exists, is not deleting, and names their generation."""
        rec = self.rec(item_id)
        if rec is None or rec.state.value == "deleting" or rec.indexed_generation is None:
            return []
        return [c for c in self.chunks(item_id) if c.generation == rec.indexed_generation]

    def card_texts(self) -> Dict[str, str]:
        from src.domain.drive_card import card_text
        return {k[1]: card_text(self.repo.records[k]) for k in self.repo.vectors if k in self.repo.records}

    def summary_total(self, key: str) -> int:
        return sum(r.summary.get(key, 0) for r in self.results)

    def add_doc(self, name: str, text: str, parent: str = "root", **kw) -> str:
        ext = name.lower().rsplit(".", 1)[-1]
        head = PDF_HEAD if ext == "pdf" else OOXML_HEAD if ext in ("docx", "xlsx", "pptx") else b""
        data = head + text.encode("utf-8")
        return self.drive.add_file(name, data, parent=parent, **kw)

    def add_image(self, name: str, thumbnail: Optional[bytes] = JPEG, data: bytes = JPEG, *,
                  camera: bool = False, **kw) -> str:
        facets = {"image": {"width": 4000, "height": 3000}}
        if camera:
            facets["photo"] = {"takenDateTime": "2026-08-01T10:00:00Z", "cameraMake": "Apple"}
        return self.drive.add_file(name, data, thumbnail=thumbnail, mime_type="image/jpeg", facets=facets, **kw)


def prose(n_sentences: int, seed_word: str = "clause") -> str:
    return " ".join(f"The {seed_word} number {i} sets the payment terms for the tenant." for i in range(n_sentences))

