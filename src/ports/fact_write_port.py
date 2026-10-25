"""
FactWritePort — abstract interface for writing facts with embedding generation.

Justification for port promotion:
- Used in ConsolidationAgent (agents layer) and FactManagementAdapter (adapters layer).
- Encapsulates multi-vector embedding generation + deduplication logic.
- Enables test doubles without real Firestore + Gemini embedding calls.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Sequence, Tuple

from ..domain.entities import FactEntity
from ..domain.new_fact_attributes import NewFactAttributes


class FactWritePort(ABC):
    """Abstract interface for writing facts with automatic multi-vector generation."""

    @abstractmethod
    async def add_facts_batch(
        self,
        account_id: str,
        user_id: str,
        facts_data: List[Dict[str, Any]],
        skip_deduplication: bool = False,
    ) -> Tuple[int, int, List[str]]:
        """
        Add a batch of new facts, each with its three vectors (domain.fact_embedding recipe).

        Args:
            facts_data: dicts with text or content, tags, type, metadata, and optionally domain,
                temporal_class, state, context_priority (lowercase enum values), context,
                reported_date, expiry (the decision) and derived_from. A model `ttl_days` is not read.
            skip_deduplication: True stores every fact; False skips a fact SmartDeduplication judges a
                duplicate (similarity threshold 0.96).

        Tenancy: every fact belongs to `account_id`, created by `user_id`; dedup compares only
            `state == "current"` facts of `account_id`.
        Each fact: a new id and a new lineage, is_current True; tags gain "consolidated" (and
            "anchor" for a principle); defaults: type event (also for an unknown type), state
            current, context_priority medium, reported_date now. The system tags are also appended to
            the input dict's own tags list (observed).
        Order: facts are written in input order; with dedup each is compared with the facts already
            stored, including earlier ones of the same batch.
        Returns: (saved_count, skipped_count, saved_ids); saved_ids holds the id of every stored fact
            in input order, so len(saved_ids) == saved_count. Known defect: the dedup path returns []
            (fact_write_service.py).
        Idempotency: with skip_deduplication none, a re-run stores new facts; without it a fact
            identical to a current one is skipped.
        Atomicity: every fact is parsed before any embedding (parse_new_fact), so a bad value anywhere
            in the batch stores nothing and costs no embedding call; state "superseded" is refused. Every
            embedding is computed before the first write, so an embedding failure stores nothing; writes
            are per fact, so a later backend failure keeps the facts already stored.
        Errors: raises ValueError for an invalid fact (empty content, bad enum or date, tags not a list
            of strings, state superseded), and whatever the embedding service or the backend raises.
            Empty facts_data returns (0, 0, []) with no calls.
        """

    @abstractmethod
    async def build_facts(
        self,
        account_id: str,
        user_id: str,
        facts: Sequence[NewFactAttributes],
    ) -> List[FactEntity]:
        """
        Embed and build new facts from already-parsed attributes; store nothing and check no duplicates.

        Each entity has a new id and a new lineage, is_current True, version 1 and its three vectors
        (domain.fact_embedding recipe, computed before the system tags are added). Tags gain
        "consolidated" (and "anchor" for a principle); they are appended to the attributes' own list,
        which is not copied. reported_date None means now.
        Returns: one entity per input, in input order; an empty input returns [] with no calls.
        Errors: raises whatever the embedding service raises, and RuntimeError when it returns no vector
            for a fact; no entity is returned with a None vector.
        """
