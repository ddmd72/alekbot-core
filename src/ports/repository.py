from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Tuple
from ..domain.entities import FactEntity

class FactRepository(ABC):
    """
    Port for fact storage and retrieval (one document per fact, three embedding vectors).

    Holds for every method unless the method says otherwise:
    - Tenancy: `account_id` is the tenant; an `owner_id` argument is an account id. A read scoped to
      an account returns only that account's facts. By-id methods are not scoped: the caller compares
      `account_id`.
    - Current: a read of "current" facts filters on `state == "current"`, never on `is_current`
      (`is_current` means latest document of its lineage).
    - Identity: a returned fact's `id` is its document id.
    - Legacy documents: a stored `owner_id` without `account_id` reads as both `account_id` and
      `created_by_user_id`; a missing `visibility` reads as account_shared, "private" as
      user_private. A document without `state` matches no state-filtered read. A document missing a
      field a read orders by is absent from that read (observed). A stored document that does not
      validate as FactEntity fails the whole call (observed).
    - Vectors are returned as lists of floats. A read with a projection leaves the omitted fields at
      the model default (named per method).
    - Atomicity: nothing is transactional; writes replace whole documents, last writer wins.
    - Errors: backend errors propagate unless the method says it never raises.
    """

    @abstractmethod
    async def add_fact(self, fact: FactEntity) -> str:
        """Store `fact` as document `fact.id` and return `fact.id`.

        Tenancy: stored as given; the caller sets account_id.
        Idempotency: a document with that id is replaced, so one id is one document.
        """
        pass

    @abstractmethod
    async def get_fact_by_id(self, fact_id: str) -> Optional[FactEntity]:
        """Return the fact stored under `fact_id` in any state, with every field and its three vectors.

        Callers write the entity back with update_fact, so nothing stored may be lost on the way.
        Tenancy: none; the caller compares account_id.
        Errors: a missing id returns None, never raises.
        """
        pass

    @abstractmethod
    async def get_facts_by_ids(self, fact_ids: List[str]) -> List[FactEntity]:
        """Return each fact as get_fact_by_id does; missing ids are omitted.

        Contract (observed; no caller in src/).
        Order: input order.
        Tenancy: none; the caller compares account_id.
        """
        pass

    @abstractmethod
    async def get_active_facts(self, owner_id: str, tags: Optional[List[str]] = None) -> List[FactEntity]:
        """Return every `state == "current"` fact of account `owner_id`.

        Args:
            owner_id: account id ("SYSTEM" holds the system prompt components).
            tags: when set, only facts carrying at least one of these tags.

        Order: none; callers sort.
        Limit: none.
        Projection: no vectors; temporal_class, context and ttl_days keep the model default (observed).
        """
        pass

    @abstractmethod
    async def get_active_facts_ordered(
        self,
        account_id: str,
        domain: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[FactEntity]:
        """Return `state == "current"` facts of `account_id` whose context_priority is not archival.

        Args:
            domain: when set, only facts of that domain (e.g. "biographical").
            limit: at most this many; None returns all.

        Order: context_priority critical, high, medium, low; within one priority newest first by
            created_at. The tail is the least essential (the directive cap backstop invalidates it).
        Legacy: a document without a stored priority rank is absent (observed).
        Projection: no vectors; temporal_class, context and ttl_days keep the model default (observed).
        """
        pass

    @abstractmethod
    async def get_longest_facts(self, account_id: str, limit: int) -> List[FactEntity]:
        """Return the `limit` `state == "current"` facts of `account_id` with the most words.

        Order: len(text.split()) descending; ties keep get_active_facts order (observed).
        Projection: as get_active_facts.
        """
        pass

    @abstractmethod
    async def get_paginated_facts(
        self,
        owner_id: str,
        limit: int = 100,
        cursor_doc_id: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Tuple[List[FactEntity], Optional[str]]:
        """Return one page of `state == "current"` facts of account `owner_id` and the next cursor.

        Args:
            limit: page size.
            cursor_doc_id: document id of the last fact of the previous page.
            domain: when set, only facts of that domain.

        Order: newest first by created_at; pages do not overlap and together hold every matching fact.
        Returns: (facts, next_cursor); next_cursor is None exactly when no fact follows this page.
        Legacy: a document without created_at is absent (observed).
        Errors: an unknown cursor id restarts at the first page (observed).
        """
        pass

    @abstractmethod
    async def search_facts(
        self,
        query_vector: List[float],
        vector_field: str = "vector",
        limit: int = 5,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None
    ) -> List[FactEntity]:
        """Cosine vector search over the `state == "current"` facts of one account.

        Args:
            vector_field: "vector" | "metadata_vector" | "tags_vector".
            limit: at most this many results.
            user_id, account_id: explicit overrides of the RequestContext ids.

        Tenancy: account_id, else the RequestContext account, else the user id (observed).
        Order: nearest first (callers rank by position, RRF).
        Projection: `vector` and `context` are returned; tags_vector and metadata_vector are None.
        Errors: no user id (argument or RequestContext) raises ValueError.
        """
        pass

    @abstractmethod
    async def search_facts_by_domain(
        self,
        domains: List[str],
        limit: int = 10,
        account_id: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> List[FactEntity]:
        """Return `state == "current"` facts of one account whose domain is in `domains` (no vectors searched).

        Tenancy: account_id, else the RequestContext account; `user_id` is unused (observed).
        Order: newest first by created_at.
        Limit: at most `limit`.
        Legacy: a document without created_at is absent (observed).
        Errors: no account raises ValueError; empty `domains` returns []; more than 30 domains
            raises (Firestore `in` limit, observed).
        """
        pass

    @abstractmethod
    async def update_fact(self, fact: FactEntity) -> None:
        """Replace document `fact.id` with `fact`, every field including the three vectors.

        Tenancy: none; the caller compares account_id before writing.
        Atomicity: unconditional write, last writer wins; stored fields FactEntity lacks are dropped (observed).
        Idempotency: writing the same entity twice leaves one identical document.
        Errors: a missing id is created, not refused (observed).
        """
        pass

    @abstractmethod
    async def get_lineage(self, lineage_id: str) -> List[FactEntity]:
        """Return every document of `lineage_id`, newest first by created_at.

        Contract (observed; no caller in src/).
        Tenancy: none; any account, any state.
        Legacy: `owner_id` is not migrated, so a legacy document fails the call.
        """
        pass

    @abstractmethod
    async def get_latest_fact_by_lineage(self, owner_id: str, lineage_id: str) -> Optional[FactEntity]:
        """Return the `is_current` document of `lineage_id` in account `owner_id`, any state, or None.

        Contract (observed; its one caller, UserPromptBuilder._get_component, is never called).
        Legacy: when no account_id matches, the legacy `owner_id` field is tried.
        Order: with several `is_current` documents, which one is returned is unspecified.
        """
        pass

    @abstractmethod
    async def add_observation(self, observation: Dict[str, Any], owner_id: str) -> None:
        """Store `observation` in the deprecated observations collection.

        Contract (observed; no caller in src/).
        Document id: observation["id"], else a timestamp id. The input dict gains account_id and
        owner_id, both `owner_id`.
        """
        pass

    @abstractmethod
    async def get_observations(self, owner_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Return up to `limit` observations of account `owner_id`.

        Contract (observed; no caller in src/).
        Order: none.
        Legacy: when no account_id matches, the legacy `owner_id` field is tried.
        """
        pass

    @abstractmethod
    async def archive_observations(self, observation_ids: List[str], owner_id: str) -> None:
        """Move observations of `owner_id` to the archive collection (copy, then delete).

        Tenancy: an observation moves only when its account_id (or legacy owner_id) equals
            `owner_id`; foreign and missing ids are skipped. The one caller passes a user id (observed).
        Atomicity: all moves commit in one batch.
        Errors: in a production environment raises PermissionError (observed).
        """
        pass

    @abstractmethod
    async def add_fact_if_unique(
        self, 
        fact: FactEntity, 
        similarity_threshold: float = 0.85
    ) -> tuple[bool, Optional[str]]:
        """Store `fact` unless a near-duplicate is already current in its account.

        Tenancy: compares only `state == "current"` facts of fact.account_id.
        Duplicate: the nearest fact by cosine within 1 - similarity_threshold that
            SmartDeduplication judges a duplicate. A fact without a vector is stored unchecked.
        Returns: (True, fact.id) when stored; (False, id of the existing duplicate) when skipped.
        Atomicity: check, then write; two concurrent duplicates can both be stored (observed).
        Defaults: the adapter's default threshold is 0.96 (observed); the caller passes it explicitly.
        """
        pass

    @abstractmethod
    async def get_biographical_context(
        self, 
        owner_id: str, 
        limit: int = 100
    ) -> List[FactEntity]:
        """Return the cached biographical context of account `owner_id` as FactEntity objects.

        Contract (observed; no caller in src/). Reads the cache as get_biographical_context_cached.
        Errors: raises KeyError on a non-empty cache, whose entries carry no `type` (observed).
        """
        pass

    @abstractmethod
    async def refresh_biographical_context_cache(
        self, 
        owner_id: str,
        facts_limit: Optional[int] = None,
        principles_limit: Optional[int] = None
    ) -> None:
        """Rebuild the cached biographical context of account `owner_id` from its current facts.

        Content: facts in get_active_facts_ordered order, biographical domain first, then other
            non-directive domains, up to facts_limit; those tagged "mindset" become principles, up to
            principles_limit; agent_directive facts separately.
        Limit: facts_limit and principles_limit are what the cache holds (None = system default).
        Idempotency: refreshing an unchanged account writes the same content.
        Errors: never raises; on a failure the previous cache stays (observed). Without a context
            service it does nothing (observed).
        """
        pass

    @abstractmethod
    async def get_biographical_context_cached(
        self,
        owner_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Return the cached biographical context of account `owner_id` (no fact query).

        Tenancy: owner_id None resolves the RequestContext's effective account (observed); no
            account returns [].
        Returns: dicts with id, text, domain, tags, context_priority and created_at (ISO string):
            the facts, then the principles, then the directives, as of the last refresh.
        Limit: ignored; the whole cache is returned (observed).
        Errors: never raises; a missing or empty cache or a backend failure returns [] (observed).
        """
        pass

    @abstractmethod
    async def invalidate_fact(self, fact_id: str, account_id: str) -> None:
        """Mark fact `fact_id` invalidated (Cabinet write path).

        Tenancy: the stored account_id must equal `account_id`.
        Effect: state invalidated, valid_to and last_updated set to now; is_current and version unchanged.
        Errors: a missing id raises ValueError; a fact of another account raises PermissionError;
            nothing is written in either case.
        Atomicity: read, check, then update; not transactional (observed).
        Idempotency: a repeated call succeeds and moves valid_to and last_updated (observed).
        """
        pass

    @abstractmethod
    async def get_legacy_facts(
        self,
        account_id: str,
        limit: int = 20
    ) -> List[FactEntity]:
        """Return up to `limit` facts of `account_id` without a domain (missing or null), not superseded.

        Contract (observed; no caller in src/).
        Order: document id order, not created_at.
        Legacy: a document without `state` is included.
        Errors: never raises; a failure returns [].
        """
        pass
