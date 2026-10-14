"""
ParseCache — the child-process parser's outcomes kept by input, for repeated eval runs (USER_DRIVE_INDEXING_RFC §25.7).

The parser is deterministic (same bytes, kind and parser version, same text), and it is the slow part of an eval run:
one sandboxed child process per file, about half a second each, over 1 400 bulk files. The cache answers a file it
has seen from memory or from `directory` (one JSON file per input, under the gitignored scripts/memory/), and passes
anything else to the real parser. Never used by the indexer: an eval convenience only. Delete the directory after a
parser change (the parser version is part of the key, so a bump misses anyway).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, Optional

from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest
from src.ports.content_extractor_port import ContentExtractorPort

from .enrichment_snapshot import outcome_from_json, outcome_to_json


class ParseCache(ContentExtractorPort):
    def __init__(self, inner: ContentExtractorPort, directory: Optional[Path] = None) -> None:
        self._inner = inner
        self._dir = directory
        self._memory: Dict[str, Dict] = {}
        self.hits = 0
        self.misses = 0
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def extractor_id(self) -> str:
        return self._inner.extractor_id

    @property
    def version(self) -> int:
        return self._inner.version

    def _key(self, request: ExtractionRequest, data: bytes) -> str:
        head = f"{self._inner.extractor_id}@{self._inner.version}:{request.kind}:{request.mime_type}:"
        return hashlib.sha256(head.encode("utf-8") + data).hexdigest()

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        with open(request.path, "rb") as fh:
            key = self._key(request, fh.read())
        stored = self._memory.get(key)
        if stored is None and self._dir is not None and (self._dir / f"{key}.json").exists():
            stored = json.loads((self._dir / f"{key}.json").read_text(encoding="utf-8"))
        if stored is not None:
            self.hits += 1
            return outcome_from_json(stored)
        self.misses += 1
        outcome = await self._inner.extract(request)
        data = outcome_to_json(outcome)
        self._memory[key] = data
        if self._dir is not None:
            (self._dir / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return outcome
