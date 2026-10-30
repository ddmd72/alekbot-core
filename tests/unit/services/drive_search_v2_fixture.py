"""A synthetic Search v2 index on the in-memory repository: records with profiles, place forms and photo fields, chunks
with `word_keys`, hint documents with grounded evidence — every key made by the real keyer (`drive_keys`), so search
tests exercise index/query parity. No model call, no PII (USER_DRIVE_INDEXING_RFC §25.7, F-22's spirit)."""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_enrichment import DriveHintDoc, DriveProfile, HintItem, PhotoObject
from src.domain.drive_index import (
    KEYS_VERSION, MIGRATION_GATE_KEY, DriveChunk, DriveIndexRecord, DriveSyncState, RecordState, Scope,
    TriageVerdict,
)
from src.domain.drive_keys import about_keys_of, name_keys_v2, word_form_keys, word_keys
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_search import DriveSearchRequest
from src.domain.user_drive import DriveConnection
from src.services.drive_search_service import DriveSearchService

from .drive_harness_connections import harness_connections
from .drive_index_fakes import FakeUsers

SPACE = "test-space/8"
ROOT = "root"
AS_OF = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


def vec(seed: str) -> List[float]:
    digest = hashlib.sha256(seed.encode()).digest()
    return [b / 255.0 for b in digest[:8]]


class Embeddings:
    """Query vectors by text: a pinned vector when the test sets one, else a hash."""

    def __init__(self) -> None:
        self.pinned: Dict[str, List[float]] = {}
        self.calls: List[str] = []

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        self.calls.append(text)
        return self.pinned.get(text, vec("q:" + text))

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [await self.get_embedding(t, task_type) for t in texts]


class Drive:
    def __init__(self) -> None:
        self.connection_state = DriveConnection(connected=True, drive_id="d1")

    async def connection(self, user_id: str) -> DriveConnection:
        return self.connection_state


class KeySpyRepo(InMemoryDriveIndexRepository):
    """Counts the v2 reads; the in-memory adapter already refuses more than 30 keys a query (as Firestore does)."""

    def __init__(self) -> None:
        super().__init__()
        self.key_lists: List[Tuple[str, int]] = []

    async def find_by_about_keys(self, user_id, keys, limit, **kw):
        self.key_lists.append(("about", len(keys)))
        return await super().find_by_about_keys(user_id, keys, limit, **kw)

    async def find_chunks_by_word_keys(self, user_id, keys, limit, after_item_id=None):
        self.key_lists.append(("word", len(keys)))
        return await super().find_chunks_by_word_keys(user_id, keys, limit, after_item_id)

    async def find_hints_by_keys(self, user_id, keys, limit, after_item_id=None):
        self.key_lists.append(("hint", len(keys)))
        return await super().find_hints_by_keys(user_id, keys, limit, after_item_id)


class V2World:
    """Build records by hand; `search` runs the real service over them."""

    def __init__(self, *, migrated: bool = True) -> None:
        self.repo = KeySpyRepo()
        self.emb = Embeddings()
        self.users = FakeUsers()
        self.drive = Drive()
        self.now = AS_OF
        counter = itertools.count(1)
        self.service = DriveSearchService(self.repo, self.emb, self.drive,
                                          harness_connections(self.users, self.drive), embedding_space=SPACE,
                                          clock=lambda: self.now, nonce=lambda: f"nonce{next(counter):04d}")
        versions = {MIGRATION_GATE_KEY: KEYS_VERSION} if migrated else {}
        self.repo.states["u"] = DriveSyncState(user_id="u", connection_id="u", drive_identity="fake:d1", root_item_id=ROOT, applied_space=SPACE,
                                               applied_versions=versions, observed_as_of=AS_OF,
                                               initial_enumeration_done=True)
        self._items = itertools.count(1)

    # -- building -------------------------------------------------------------------------------

    def folder(self, name: str, parent: str = ROOT, item: Optional[str] = None) -> str:
        item = item or f"F{next(self._items):03d}"
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id=item, parent_id=parent, name=name, is_folder=True, kind="folder",
                               scope=Scope.NORMAL, state=RecordState.INDEXED, has_card=True,
                               name_tokens=name_keys_v2(name))
        self._put(rec, vec("card:" + name))
        return item

    def file(self, name: str, *, parent: str = ROOT, kind: str = "pdf", item: Optional[str] = None,
             profile: Optional[DriveProfile] = None, chunks: Sequence[str] = (),
             hints: Optional[Dict[int, Sequence[Tuple[str, str]]]] = None, place_forms: Tuple[str, ...] = (),
             taken: Optional[str] = None, effective: Optional[str] = None, photo: Optional[str] = None,
             photo_objects: Tuple[PhotoObject, ...] = (), valid_until: Optional[str] = None,
             card_vector: Optional[List[float]] = None, generation: int = 1, **fields) -> str:
        item = item or f"I{next(self._items):03d}"
        when = _day(taken or effective or "2026-01-01")
        media = DriveMediaFacts(taken_at=_day(taken).replace(hour=10)) if taken else DriveMediaFacts()
        text = "\n\n".join(chunks)
        rec = DriveIndexRecord(
            user_id="u", connection_id="u", item_id=item, parent_id=parent, name=name, kind=kind, scope=Scope.NORMAL,
            state=RecordState.INDEXED, has_card=True, name_tokens=name_keys_v2(name), effective_date=when,
            media=media, size_bytes=100 + len(text),
            content_identity=ContentIdentity("fakehash", f"h{item}", 100 + len(text)), profile=profile,
            place_forms=place_forms, place_name=place_forms[0] if place_forms else None,
            photo_description=photo or "", photo_objects=photo_objects,
            triage_verdict=TriageVerdict.PHOTO if kind == "image" else None,
            valid_until=_day(valid_until) if valid_until else None,
            indexed_generation=generation if chunks else None, chunk_count=len(chunks), text_chars=len(text),
            keys_version=KEYS_VERSION, **fields)
        rec = replace(rec, about_keys=about_keys_of(rec))
        self._put(rec, card_vector or vec("card:" + name))
        self._chunks(item, kind, chunks, hints or {}, generation)
        return item

    def _put(self, rec: DriveIndexRecord, vector: List[float]) -> None:
        self.repo.records[("u", rec.item_id)] = rec
        self.repo.vectors[("u", rec.item_id)] = vector
        state = self.repo.states["u"]
        carded = sum(1 for r in self.repo.records.values() if r.has_card)
        self.repo.states["u"] = replace(state, counters={**state.counters, "carded": carded})

    def _chunks(self, item: str, kind: str, texts: Sequence[str], hints: Dict[int, Sequence[Tuple[str, str]]],
                generation: int) -> None:
        offset = 0
        for index, text in enumerate(texts):
            keys = word_keys(text)
            chunk = DriveChunk(user_id="u", connection_id="u", item_id=item, generation=generation, index=index, text=text,
                               char_start=offset, char_end=offset + len(text), kind=kind, word_keys=keys,
                               keys_version=KEYS_VERSION)
            self.repo.chunks[("u", item, generation, index)] = (chunk, vec(f"chunk:{item}:{index}"))
            offset += len(text) + 2
            if index in hints:
                items = []
                for keyword, evidence in hints[index]:
                    start = text.find(evidence)
                    assert start >= 0, f"evidence {evidence!r} is not in chunk {index} of {item}"
                    items.append(HintItem(keyword, "keyword", evidence, start, start + len(evidence)))
                held = set(keys)
                hint_keys = tuple(k for i in items for k in word_form_keys(i.text) if k not in held)
                doc = DriveHintDoc(user_id="u", connection_id="u", item_id=item, generation=generation, index=index, kind=kind,
                                   chunk_text_hash=f"t{index}", items=tuple(items),
                                   hint_keys=tuple(dict.fromkeys(hint_keys)), passages_version=1)
                self.repo.hints[("u", item, generation, index)] = (doc, vec(f"hint:{item}:{index}"))

    def set(self, item: str, **fields) -> None:
        self.repo.records[("u", item)] = replace(self.repo.records[("u", item)], **fields)

    # -- searching ------------------------------------------------------------------------------

    async def search(self, query: str = "", **kw):
        return await self.service.search("u", DriveSearchRequest(query=query, **kw))


def _day(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def tier_of(result, item: str) -> Optional[str]:
    return next((c.tier for c in result.cards if c.item_id == item), None)


def card(result, item: str):
    return next(c for c in result.cards if c.item_id == item)
