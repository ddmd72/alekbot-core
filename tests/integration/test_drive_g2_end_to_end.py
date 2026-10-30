"""G2 end to end on the committed fixture folder (USER_DRIVE_INDEXING_RFC §17.8, §24.11): the real tick with the real
sandboxed parser on the fixture files, a scripted reader and triage (no model calls), the in-memory index, then the
real DriveSearchService — every text-layer G2 case finds its expected substring in the window of its card.

Admission is switched on by rebinding the policy sets, as the flip does (the assembled tree is pre-flip). Embeddings
are a deterministic bag of words, so the chunk and card kNN rank by shared words instead of by hash noise. One child
process per file (~1 s each) makes this too slow for the unit suite: run with `make test-integration`."""
import hashlib
import json
import math
import re
from pathlib import Path
from typing import List

import pytest

from scripts.validation.drive_eval.fixture_drive import FixtureDrive
from scripts.validation.drive_eval.eval_connection import EvalDriveConnections, eval_connection
from scripts.validation.drive_eval.runner import EVAL_USER, EvalClock, index_fixture, run_cases
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain import drive_index_policy as policy
from src.domain.drive_extraction import PARSER, READER
from src.services.drive_search_service import DriveSearchService
from tests.unit.services.drive_content_fakes import FakeExtractor, FakeTriage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "drive_eval"
SPACE = "bag-of-words/256"


class BagOfWordsEmbeddings:
    """Hashed word counts, L2-normalised: cosine similarity is lexical overlap."""

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        vector = [0.0] * 256
        for word in re.findall(r"\w+", text.casefold()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 256] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [await self.get_embedding(t, task_type) for t in texts]


@pytest.fixture
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", policy.SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", policy.SLICE2_TRIAGED_KINDS)


async def test_text_layer_g2_cases_find_their_substring_in_the_window(admitted):
    tree = json.loads((FIXTURES / "tree.json").read_text(encoding="utf-8"))
    # the hand-written items only: the generated thousands (Vault, invoices, camera roll) add minutes, not coverage
    drive = FixtureDrive({"items": tree["items"]}, base_dir=FIXTURES)
    paths = set(drive.paths())
    candidates = [case for case in json.loads((FIXTURES / "gold_g2.json").read_text(encoding="utf-8"))
                  if not case.get("ocr") and set(case["expected_paths"]) <= paths]
    # owner-accepted v2 strict-AND losses (Δ94) and cases found only through recorded enrichment this test never loads
    accepted = [c["id"] for c in candidates if c.get("accepted_loss")]
    enrichment = [c["id"] for c in candidates if c.get("requires_enrichment") and not c.get("accepted_loss")]
    gold = [c for c in candidates if not c.get("accepted_loss") and not c.get("requires_enrichment")]
    print(f"excluded as accepted_loss: {accepted}; skipped as requires_enrichment: {enrichment}")
    assert len(candidates) >= 20 and len(gold) >= 18   # the text-layer set must not shrink silently
    repo, embeddings, clock = InMemoryDriveIndexRepository(), BagOfWordsEmbeddings(), EvalClock()
    reader = FakeExtractor(READER)
    indexed = await index_fixture(drive, repo, embeddings, lambda lat, lon: "Rome (Roma), Italy", SPACE, clock,
                                  throttle_pause_s=0,
                                  extractors={PARSER: SubprocessContentExtractor(), READER: reader},
                                  triage=FakeTriage())
    assert indexed.records.get("indexed", 0) >= len({p for c in gold for p in c["expected_paths"]})
    # a PDF without a text layer goes past the parser to the reader (§24.3)
    assert "Scan_0012.pdf" in {r.name for r in reader.requests}

    search = DriveSearchService(repo, embeddings, drive, EvalDriveConnections(eval_connection(EVAL_USER)), clock=clock,
                                embedding_space=SPACE)
    run = await run_cases(search, drive, gold)
    missed = {row["id"]: row.get("windows") or row["top"] for row in run.cases if not row["hit"]}
    assert not missed, (missed, f"accepted_loss: {accepted}", f"requires_enrichment: {enrichment}")
