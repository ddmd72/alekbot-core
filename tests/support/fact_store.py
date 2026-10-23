"""Real fact-store objects wired on the evaluating Firestore fake, for write -> read round-trip tests."""
import hashlib
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Union
from unittest.mock import AsyncMock

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.firestore_repo import FirestoreFactRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.search_enrichment_port import SearchEnrichmentPort
from src.services.fact_write_service import FactWriteService
from tests.support.firestore_eval import EvalDb

FACTS = "eval_domain_facts"
DIM = 768
# The declared indexes and the collection group the fake's facts collection stands for in them (`EvalDb(indexes=, groups=)`).
INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
FACTS_INDEX_GROUP = "development_domain_facts_v2"


def embed(text: str) -> List[float]:
    """Deterministic unit vector: equal texts give equal vectors, different texts are nearly orthogonal."""
    rng = random.Random(int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))
    raw = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = sum(x * x for x in raw) ** 0.5
    return [x / norm for x in raw]


class HashEmbedder(EmbeddingService):
    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        return embed(text)

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [embed(t) for t in texts]


def make_env_config() -> SimpleNamespace:
    return SimpleNamespace(
        firestore_collection_prefix="eval_", domain_facts_collection=FACTS,
        user_context_collection="eval_user_context", is_production=False,
    )


class FactStore:
    """db + repo + write service + management adapter, all real, over one EvalDb.

    `indexes` (a path to `config/firestore.indexes.json` or its parsed list) switches the fake's index refusal on, with the
    facts collection mapped to its declared group; the default is the fake without refusal.
    """

    def __init__(self, indexes: Union[None, str, Path, list] = None) -> None:
        groups: Optional[dict] = None if indexes is None else {FACTS: FACTS_INDEX_GROUP}
        self.db = EvalDb(indexes=indexes, groups=groups)
        self.embedder = HashEmbedder()
        self.repo = FirestoreFactRepository(self.db, make_env_config(), embedding_service=self.embedder)
        self.write = FactWriteService(self.repo, self.embedder)
        self.adapter = FactManagementAdapter(
            self.repo, self.embedder, self.write, AsyncMock(spec=SearchEnrichmentPort)
        )

    @property
    def docs(self) -> dict:
        """The raw stored documents, as Firestore holds them."""
        return self.db.collection(FACTS).docs

    def interfere(self, fact_id: str, fields: Dict[str, Any], after_read: bool = False) -> None:
        """Another writer commits `fields` to the fact when the next transaction reads it (`EvalDb.interfere_on_txn_read`)."""
        self.db.interfere_on_txn_read(FACTS, fact_id, fields, after_read=after_read)
