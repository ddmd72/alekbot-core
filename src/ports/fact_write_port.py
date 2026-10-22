"""
FactWritePort — abstract interface for writing facts with embedding generation.

Justification for port promotion:
- Used in ConsolidationAgent (agents layer) and FactManagementAdapter (adapters layer).
- Encapsulates multi-vector embedding generation + deduplication logic.
- Enables test doubles without real Firestore + Gemini embedding calls.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple


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
                temporal_class, state, context_priority (lowercase enum values), ttl_days, context,
                reported_date.
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
            in input order, so len(saved_ids) == saved_count.
        Idempotency: with skip_deduplication none, a re-run stores new facts; without it a fact
            identical to a current one is skipped.
        Atomicity: every embedding is computed before the first write, so an embedding failure
            stores nothing; writes are per fact, so a later failure keeps the facts already stored.
        Errors: raises (embedding, invalid enum value, backend). Empty facts_data returns (0, 0, [])
            with no calls.
        """
