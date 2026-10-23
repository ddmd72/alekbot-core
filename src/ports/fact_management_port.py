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
    - Atomicity: no lock. Every write to a fact document is a transaction under the `update_time` precondition of the
      document it read; a document that changed meanwhile is read again once, a second change is a "failed" result.
      The writes of one fact are therefore serialised by the store, across processes.
    - Ids: a fact id from an earlier search may have been versioned or merged since; update_fact and merge_facts follow
      `superseded_by` within the lineage to the current document (domain.fact_lifecycle.CONSOLIDATION_ACCEPTS).
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
        Lifecycle: is_current True and no valid_to whatever `state` says (observed). State "superseded" is
            refused: a document must not be born closed with no successor.
        Idempotency: none; a repeated call creates another fact.
        Errors: a missing required key or an invalid domain, temporal_class, state (including superseded) or
            context_priority is "failed" with nothing written. A required key present with the
            value None passes the check (observed).
        """
        pass

    @abstractmethod
    async def update_fact(
        self, fact_id: str, updates: Dict[str, Any], account_id: str
    ) -> Dict[str, Any]:
        """Update one fact: a new version document for a content change, an in-place write for a lifecycle change.

        Args:
            fact_id: any id of the fact's chain; a superseded id is followed to the current version.
            updates: content (non-empty text), tags (a non-empty list, replaced whole; `null` and `[]` are refused),
                metadata (a dict, merged key by key; a non-dict is ignored), temporal_class and state
                (case-insensitive), reported_date (ISO string or datetime), superseded_by (an id; required by
                state SUPERSEDED, refused otherwise, refused with any content key). Absent keys keep the stored value;
                other keys are ignored (observed).
            account_id: the caller's account.

        Tenancy: a fact of another account, or a missing id, is "not_found"; nothing is written.
        Returns: {"fact_id", "status": "updated" | "not_found" | "failed", "message"}, plus "version" when updated.
            `fact_id` is the id of the document written: the new version's id after a content change, else the
            current document's id, which differs from the argument when it was followed forward. When the argument
            was followed forward, the result also has "resolved_from" (the argument) and says so in its message.
        Versions: a change of text, tags, metadata or temporal_class writes a new document (new id, same lineage,
            version + 1, `replaces_fact_id`, `created_at` copied, `valid_from` now) and closes the old one
            (superseded, `superseded_by` = the new id) in one transaction. reported_date is the given value, else
            now. A change that leaves the three embedding texts alone (temporal_class only) keeps the vectors;
            otherwise all three are recomputed.
        In place: a state-only, reported_date-only or empty update writes only those fields (and last_updated) onto
            the current document; version unchanged. Re-applying the stored state leaves valid_to unchanged.
            State SUPERSEDED closes the fact in favour of `superseded_by`, resolved forward to its current version;
            a missing or foreign successor, or the fact itself, is refused.
        Tags: the system tags the stored version carries ("consolidated", "anchor") and the update omits are kept,
            appended at the end.
        Resolution: only a `current` document can be updated by the consolidator. An id that was merged into another
            lineage, whose successor is unknown, or whose end is archived, invalidated or stale is "failed" with
            a message that names the current id when there is one.
        Conflict: a document changed between the read and the write is read again once, re-embedding only when the
            embedding texts changed; a second conflict is "failed" ("changed concurrently; search again"). An update
            with content or tags (full replacements) is also "failed" the same way when the read-again lands on a
            different document: a version the caller never saw arrived meanwhile and would lose its change.
        Atomicity: an invalid enum, date, tags or content, or an embedding failure is "failed" with nothing written.
        Idempotency: repeating an update that changes nothing writes only last_updated, in place.
        """
        pass

    @abstractmethod
    async def merge_facts(
        self,
        fact_ids: List[str],
        merged_content: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create one merged fact with a new lineage and close every source in its favour, in one transaction.

        Args:
            fact_ids: the sources; each is followed to its current version. Duplicates after following collapse,
                input order kept.
            metadata: attributes of the merged fact, as create_fact; account_id and user_id are
                required (observed: the taxonomy keys create_fact requires are not checked). State "superseded"
                is refused.

        Order: every argument of the merged fact is validated, then the sources are resolved, then the merged fact
            is embedded; nothing is written before the single transaction.
        Tenancy: missing sources and sources of another account are skipped and not reported.
        Returns: {"new_fact_id", "old_fact_ids" (the resolved source ids), "old_facts_state", "status": "merged" |
            "failed", "message"}.
        Lifecycle: each source gets state superseded, is_current False, valid_to and `superseded_by` = the new id.
        Atomicity: all or nothing. An invalid value, an embedding failure or a lost race writes nothing and leaves
            every source as it was.
        Resolution: a source whose chain ends in another lineage (already merged) or in a fact that is not current
            fails the whole merge with that message.
        Conflict: sources that changed between the read and the write are read again once. If the second read
            returns other ids (a source was versioned meanwhile) the merge fails with "source changed; search
            again": the merged text was written from the old text.
        Idempotency: none; a repeated call finds its sources merged and fails.
        Errors: no eligible source is "failed" ("Failed to supersede any facts") with nothing written.
        """
        pass
