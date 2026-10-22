from abc import ABC, abstractmethod
from typing import List, Dict, Any


class FactManagementPort(ABC):
    """
    Port for the consolidator's deliberate fact tools: search, create, update, merge, discard.

    Holds for every method:
    - Tenancy: a write belongs to the account named by the call (`account_id` argument or
      `metadata["account_id"]`); a fact of another account is treated as absent. Search is scoped to
      the RequestContext's effective account.
    - Results are JSON-serialisable dicts; the caller hands them to the model verbatim.
    - Errors: never raises. A failure is a result with `status: "failed"` and a `message`; search
      returns [].
    - Atomicity: concurrent writes to one fact are serialised within one adapter instance only
      (observed).
    """

    @abstractmethod
    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Multi-vector search (text, tags, metadata vectors, RRF); no domain filter, no semantic dedup.

        Args:
            keywords: domain keywords, matched against the tags and metadata vectors.
            primary_query: main search phrase.
            alternative_query: second phrasing; empty means primary_query again.

        Tenancy: only `state == "current"` facts of the RequestContext's effective account.
        Order: RRF score, best first.
        Limit: at most `limit` results, each fact_id once.
        Returns: dicts with fact_id, content, similarity, source, type, domain, temporal_class,
            state, context_priority, tags, metadata, reported_date (ISO string), version.
            `similarity` may be None (observed: None for every vector hit).
        Errors: a failed search returns [], indistinguishable from no match (observed).
        """
        pass

    @abstractmethod
    async def create_fact(self, content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Create one fact with a new lineage; no dedup.

        Args:
            content: fact text.
            metadata: must hold account_id, user_id, domain, temporal_class, context_priority; may
                hold type, state, tags, metadata, context, ttl_days, reported_date. Enum values are
                case-insensitive.

        Tenancy: the fact belongs to metadata["account_id"], created by metadata["user_id"].
        Returns: {"fact_id", "status": "created" | "failed", "message"}; fact_id is None unless created.
        Defaults: type event (also for an unknown type), state current, reported_date now.
        Lifecycle: is_current True and no valid_to whatever `state` says (observed).
        Idempotency: none; a repeated call creates another fact.
        Errors: a missing required key or an invalid domain, temporal_class, state or
            context_priority is "failed" with nothing written. A required key present with the
            value None passes the check (observed).
        """
        pass

    @abstractmethod
    async def update_fact(
        self, fact_id: str, updates: Dict[str, Any], account_id: str
    ) -> Dict[str, Any]:
        """Update one fact in place.

        Args:
            updates: content (the text), tags (replaced whole), metadata (a dict, merged key by key),
                temporal_class and state (case-insensitive), reported_date (ISO string or datetime).
                Absent keys keep the stored value; other keys are ignored (observed).
            account_id: the caller's account.

        Tenancy: a fact of another account is "not_found", like a missing id; nothing is written.
        Returns: {"fact_id", "status": "updated" | "not_found" | "failed", "message"}, plus
            "version" when updated.
        Lifecycle: a state change applies domain.fact_lifecycle.lifecycle_fields; re-applying the
            stored state leaves valid_to unchanged.
        Version: +1 only when text, tags, metadata or temporal_class change; such a change also sets
            reported_date to now unless the caller sends one. A state-only update keeps version and
            reported_date. last_updated is set on every update.
        Vectors: all three are recomputed when text, tags or metadata change.
        Atomicity: an invalid enum or date or an embedding failure is "failed" with nothing written.
        Idempotency: repeating an update changes only last_updated.
        """
        pass

    @abstractmethod
    async def merge_facts(
        self,
        fact_ids: List[str],
        merged_content: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Supersede the source facts and create one merged fact with a new lineage.

        Args:
            metadata: attributes of the merged fact, as create_fact; account_id and user_id are
                required (observed: the taxonomy keys create_fact requires are not checked).

        Tenancy: missing sources and sources of another account are skipped and not reported.
        Returns: {"new_fact_id", "old_fact_ids" (the superseded sources), "old_facts_state",
            "status": "merged" | "failed", "message"}.
        Lifecycle: each superseded source gets lifecycle_fields(SUPERSEDED).
        Atomicity: sources are superseded only when the merged fact is created; a failed merge leaves
            every source unchanged. Known defect: the implementation supersedes the sources before the
            merged fact exists (fact_management_adapter.py:376-430).
        Idempotency: none; an already superseded source is superseded again and another merged fact
            is created (observed).
        Errors: no eligible source is "failed" with nothing written.
        """
        pass
