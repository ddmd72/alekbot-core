"""A tick harness for Search v2 enrichment (USER_DRIVE_INDEXING_RFC §25.4): ContentWorld with a scripted profiler and
passage-hint port wired into the real DriveIndexingService on the in-memory repository. The fakes implement the real
ports; outcomes are scripted per call, every request is recorded, and model calls may advance the clock."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Callable, Dict, List, Optional, Sequence

from src.domain.drive_enrichment import DriveProfile, HintItem, ProfileEntity
from src.domain.drive_enrichment_calls import HintReply, HintRequest, ModelReplyStatus, ProfileReply, ProfileRequest
from src.ports.document_profiler_port import DocumentProfilerPort
from src.ports.passage_hint_port import PassageHintPort
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_world import ContentWorld
from .drive_index_fakes import FakeEmbeddings
from .drive_index_fakes import LegacyConnections

S = ModelReplyStatus


class NearEmbeddings(FakeEmbeddings):
    """Vectors close to one another (cosine ~ 1): hint grounding's similarity rule never drops a set by chance."""

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        self.calls.append(text)
        digest = hashlib.sha256(text.encode()).digest()
        return [1.0] + [0.9 + b / 2550.0 for b in digest[:7]]


class FakeProfiler(DocumentProfilerPort):
    """Answers `ok` with a profile naming the first capitalised word of the input head (printed there, so
    verification keeps it), unless `script` holds a status for the call (consumed in order). `usd` per call;
    `fallback_share` of it is the fallback's. `before` runs first (a slow call advances the clock)."""

    def __init__(self, usd: float = 0.001) -> None:
        self.requests: List[ProfileRequest] = []
        self.script: List[ModelReplyStatus] = []
        self.usd = usd
        self.fallback_share = 0.0
        self.before: Optional[Callable[[ProfileRequest], None]] = None
        self.profiles: Dict[str, DriveProfile] = {}

    async def profile(self, request: ProfileRequest) -> ProfileReply:
        self.requests.append(request)
        if self.before is not None:
            self.before(request)
        status = self.script.pop(0) if self.script else S.OK
        usd = 0.0 if status is S.UNAVAILABLE else self.usd
        if status is not S.OK:
            return ProfileReply(status=status, profile=None, model="fake-luna", usd=usd,
                                fallback_usd=usd * self.fallback_share)
        profile = self.profiles.get(request.item_id) or _profile_of(request.input.head)
        return ProfileReply(status=S.OK, profile=profile, model="fake-luna", usd=usd,
                            fallback_usd=usd * self.fallback_share)


def _profile_of(head: str) -> DriveProfile:
    word = next((w.strip(".,") for w in head.split() if w[:1].isupper() and len(w.strip(".,")) > 2), "Thing")
    return DriveProfile(title=f"About {word}", what_it_is=f"A document about {word}", language="en",
                        doc_type="contract", life_domains=("home",),
                        entities=(ProfileEntity(name=word, type="organization", printed=word),),
                        topics=("lease",), topic_words=("lease", "alquiler"), label_words=("contract", "contrato"))


class FakePassages(PassageHintPort):
    """Per chunk, one keyword "Tenancy" and one question, each quoting the chunk's first four words (grounded).
    `script` holds per-call batch statuses (consumed in order); `refuse` chunk indexes get `refused`; `delay_s`
    sleeps (real time) before answering calls after the first `fast_calls` (a file over its budget)."""

    def __init__(self, usd_per_chunk: float = 0.0004) -> None:
        self.requests: List[HintRequest] = []
        self.script: List[ModelReplyStatus] = []
        self.refuse: set = set()
        self.usd_per_chunk = usd_per_chunk
        self.fallback_share = 0.0
        self.delay_s = 0.0
        self.fast_calls = 0

    async def hints(self, request: HintRequest) -> HintReply:
        self.requests.append(request)
        if self.delay_s and len(self.requests) > self.fast_calls:
            await asyncio.sleep(self.delay_s)
        status = self.script.pop(0) if self.script else S.OK
        usd = 0.0 if status is S.UNAVAILABLE else self.usd_per_chunk * len(request.chunks)
        fallback = usd * self.fallback_share
        if status is S.UNAVAILABLE:
            return HintReply(status=status, items_by_index={}, model="", usd=0.0)
        if status in (S.FAILED, S.INVALID):
            return HintReply(status=status, items_by_index={}, model="fake-luna", usd=usd, fallback_usd=fallback,
                             outcome_by_index={i: status for i, _ in request.chunks})
        items: Dict[int, Sequence[HintItem]] = {}
        outcomes: Dict[int, ModelReplyStatus] = {}
        for index, text in request.chunks:
            if index in self.refuse:
                outcomes[index] = S.REFUSED
                continue
            quote = " ".join(text.split()[:4])
            items[index] = (HintItem(text="Tenancy", type="keyword", evidence=quote),
                            HintItem(text="What are the payment terms?", type="question", evidence=quote))
            outcomes[index] = S.OK
        return HintReply(status=S.OK, items_by_index=items, model="fake-luna", usd=usd, fallback_usd=fallback,
                         outcome_by_index=outcomes)


class EnrichWorld(ContentWorld):
    def __init__(self, page_size: int = 50, *, upgrade_pass: bool = True) -> None:
        super().__init__(page_size=page_size)
        self.emb = NearEmbeddings()
        self.profiler = FakeProfiler()
        self.passages = FakePassages()
        self.svc = DriveIndexingService(self.drive, self.repo, self.emb, self.queue, self.users,
                                        lambda lat, lon: "Rome (Roma), Italy", "test-space/8",
                                        extractors={"parser": self.parser, "reader": self.reader},
                                        triage=self.triage, profiler=self.profiler, passages=self.passages,
                                        upgrade_pass=upgrade_pass, connections=LegacyConnections(self.users, self.drive), clock=self.clock, monotonic=self.clock.monotonic)

    def hints(self, item_id: str, generation: Optional[int] = None):
        rec = self.rec(item_id)
        gen = generation if generation is not None else (rec.indexed_generation if rec else None)
        return sorted((h for (u, i, g, _), (h, _) in self.repo.hints.items() if u == "u" and i == item_id
                       and (gen is None or g == gen)), key=lambda h: h.index)

    def hint_generations(self, item_id: str) -> List[int]:
        return sorted({g for (u, i, g, _) in self.repo.hints if u == "u" and i == item_id})


def long_text(n: int, word: str = "clause") -> str:
    """`n` paragraphs of about 600 characters: several chunks, a head and a tail apart."""
    para = " ".join(f"The {word} {{i}} sets the payment terms for the tenant in full." for _ in range(10))
    return "\n\n".join(para.replace("{i}", str(i)) for i in range(n))
