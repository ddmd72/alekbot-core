from abc import ABC, abstractmethod
from datetime import datetime
from typing import AbstractSet, List, Optional, Dict, Any, Mapping, Sequence, Tuple
from ..domain.due_fact import DueFact
from ..domain.entities import FactEntity, FactState
from ..domain.stored_fact import StoredFact

class FactRepository(ABC):
    """
    Port for fact storage and retrieval (one document per fact, three embedding vectors).

    Holds for every method unless the method says otherwise:
    - Tenancy: `account_id` is the tenant; an `owner_id` argument is an account id. A read scoped to
      an account returns only that account's facts. By-id methods are not scoped: the caller compares
      `account_id`.
    - Current: a read of "current" facts filters on `state == "current"`, never on `is_current`
      (`is_current` means latest document of its lineage).
    - Identity: `id` is the document id. get_fact_by_id, get_paginated_facts,
      search_facts_by_domain and get_legacy_facts read it from the stored `id` field, which every
      writer sets equal to the document id; a document written outside this port with a different or
      missing `id` reads back with that value (or a fresh uuid). resolve_current, get_lineage and
      get_latest_fact_by_lineage take it from the document id.
    - Legacy documents: a document with `owner_id` and no `account_id` is absent from every
      account-scoped read (they filter the stored `account_id`). Where one is returned (by-id reads,
      resolve_current), `owner_id` reads as `account_id` and
      `created_by_user_id`; an in-place write (update_fact_fields) refuses it with PermissionError, since tenancy of
      a write is the stored `account_id`. A missing `visibility`
      reads as account_shared, "private" as user_private. A document without `state` matches no
      state-filtered read. A document missing a field a read orders by is absent from that read
      (observed). A stored document that does not validate as FactEntity fails the whole call
      (observed).
    - Vectors are returned as lists of floats. A read with a projection leaves the omitted fields at
      the model default (named per method).
    - Atomicity: add_fact is an unconditional whole-document write, last writer wins. The
      version and in-place writes (write_superseding, update_fact_fields) are transactions
      under an `update_time` precondition: a document changed since it was read raises FactWriteConflict and
      nothing is written. A StoredFact carries the token it was read with; compare it for equality only.
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
        """Return the fact stored under `fact_id` in any state.

        Returns every FactEntity field as stored, vectors as lists, so a read-modify-write
        loses no FactEntity field.
        Tenancy: none; the caller compares account_id.
        Errors: a missing id returns None.
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
        Projection: no vectors; temporal_class, context and replaces_fact_id keep the model default;
            expiry_kind, ttl_days and expires_at are returned (observed).
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
        Projection: no vectors; temporal_class, context, ttl_days, expires_at and replaces_fact_id
            keep the model default (observed).
        """
        pass

    @abstractmethod
    async def get_longest_facts(self, account_id: str, limit: int) -> List[FactEntity]:
        """Return the `limit` `state == "current"` facts of `account_id` with the most words.

        Order: len(text.split()) descending; ties unspecified.
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
        state: str = "current",
    ) -> Tuple[List[FactEntity], Optional[str]]:
        """Return one page of `state` facts of account `owner_id` (default `"current"`) and the next cursor.

        Args:
            limit: page size.
            cursor_doc_id: document id of the last fact of the previous page.
            domain: when set, only facts of that domain.
            state: `"current"` or `"stale"`; anything else raises ValueError before any I/O.

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
        Projection: `vector`, `context` and the expiry decision (expiry_kind, ttl_days, expires_at) are returned;
            tags_vector, metadata_vector and replaces_fact_id are None, is_current is True and last_updated is now
            (model defaults, observed).
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
    async def resolve_current(
        self, fact_id: str, account_id: str, accepted: AbstractSet[FactState]
    ) -> Optional[StoredFact]:
        """Follow `fact_id` through `superseded_by`, within its lineage, to the document a caller may write.

        Args:
            accepted: the states the walk may end in (domain/fact_lifecycle.py: CONSOLIDATION_ACCEPTS, REVIEW_ACCEPTS, CABINET_ACCEPTS).

        Returns: the end document, whole (vectors included, as lists), with the `update_time` it was read with;
            None when the start document is missing or belongs to another account (decided before any hop).
        Errors: FactResolutionError when the chain cannot end in `accepted` (merged into another lineage, no
            successor stored, successor document missing, an end state not accepted, a cycle or more than
            MAX_FORWARD_HOPS hops). Reads are not transactional; the token is what the write is checked against.
        """
        pass

    @abstractmethod
    async def write_superseding(
        self, new_fact: FactEntity, replaced: Sequence[StoredFact], now: datetime, *, actor: str
    ) -> None:
        """Create `new_fact` and close every `replaced` document in one transaction.

        A closed document gets state superseded, is_current False, valid_to and last_updated `now`,
        `superseded_by=new_fact.id` and a `lifecycle_events` entry of its own (from its stored state, `actor`, reason
        "replaced by <id>" for a version, "merged into <id>" for a merge). Used for a version (one replaced document,
        same lineage) and a merge. `new_fact` is stored as given: its own trail is the caller's.
        Stored through the same writer as add_fact (Vector wrapping, priority rank).
        Atomicity: each replaced document must still carry the `update_time` it was read with; otherwise
            FactWriteConflict naming it, nothing written. A read-time abort, or a client that gave up after
            repeated aborts, is also FactWriteConflict (a read-time abort does not say which document was contended,
            so its conflict names the first replaced id). An existing `new_fact.id` fails the commit.
        Errors: ValueError, before any I/O, when `replaced` is empty or `new_fact` lacks one of its three vectors.
        """
        pass

    @abstractmethod
    async def archive_and_add(
        self, archived: StoredFact, new_fact: FactEntity, now: datetime, *, actor: str, reason: str
    ) -> None:
        """Archive the document `archived` in place and create `new_fact`, in one transaction.

        The archived document gets `archive_fields`: state archived, is_current True, valid_to and last_updated `now`, and
        one `lifecycle_events` entry (its stored state -> archived, `actor`, `reason`); `stale_since` is kept. `new_fact`
        is stored as given through the same writer as add_fact (Vector wrapping, priority rank): its own trail is the
        caller's.
        Tenancy: `new_fact.account_id` must be `archived.fact.account_id`; inside the transaction the stored `account_id`
            must be it too (as update_fact_fields).
        Atomicity: the archived document must still carry the `update_time` it was read with; otherwise FactWriteConflict,
            nothing written. A read-time abort, or a client that gave up after repeated aborts, is also FactWriteConflict.
            An existing `new_fact.id` fails the commit with the Firestore client's AlreadyExists
            (google.api_core.exceptions), raised as it is; nothing is archived.
        Errors: ValueError, before any I/O, when the accounts differ, `new_fact` lacks one of its three vectors or
            `archived.fact` is not stale or current (`archive_fields`); PermissionError when the stored `account_id` is
            not `archived.fact.account_id`.
        """
        pass

    @abstractmethod
    async def update_fact_fields(self, stored: StoredFact, fields: Mapping[str, Any]) -> None:
        """Update `fields` of document `stored.fact.id` in place, only those fields, under its precondition.

        Allowed keys: IN_PLACE_FIELDS (domain/fact_lifecycle.py); any other raises InPlaceFieldRefused before any I/O.
        Enum values are stored as their value.
        Errors: FactWriteConflict when the document changed since `stored` was read, or no longer exists;
            PermissionError when the stored `account_id` is not `stored.fact.account_id` (a legacy document, whose
            `owner_id` reads as its account, or a stray `owner_id` beside another account's `account_id`). Nothing is
            written in either case; both are checked inside the transaction.
        """
        pass

    @abstractmethod
    async def get_stale_facts(self, account_id: str) -> List[FactEntity]:
        """Return every `state == "stale"` fact of account `account_id`, longest stale first.

        Order: `stale_since` ascending, a fact without it first, then the id (sorted in code: the query is the two
            equalities on account_id and state and needs no composite index).
        Projection: every field except `tags_vector` and `metadata_vector` (they read as None); `vector` is a list of
            floats (the review's neighbour query); `id` is the document id.
        Tenancy: only that account's documents (the stored `account_id`).
        """
        pass

    @abstractmethod
    async def get_recently_closed(self, account_id: str, since: datetime, limit: int) -> List[FactEntity]:
        """Return at most `limit` facts of account `account_id` that are `archived` or `invalidated` with `valid_to >= since`.

        Order: `valid_to` descending (the newest closure first).
        Projection: no vectors (they read as None); `id` is the document id.
        Tenancy: only that account's documents (the stored `account_id`).
        Index: needs the composite (account_id ASC, state ASC, valid_to DESC) in config/firestore.indexes.json.
        """
        pass

    @abstractmethod
    async def get_lineage(self, account_id: str, lineage_id: str) -> List[FactEntity]:
        """Return every document of `lineage_id` in account `account_id`, oldest version first, any state.

        Tenancy: only that account's documents.
        Projection: no vectors (they read as None); `id` is the document id.
        """
        pass

    @abstractmethod
    async def get_latest_fact_by_lineage(self, owner_id: str, lineage_id: str) -> Optional[FactEntity]:
        """Return the highest-version document of `lineage_id` in account `owner_id` whose state is not
        superseded, or None. No caller in src/.

        Returns the whole document (vectors as lists). Equal versions: which one is returned is unspecified.
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
    async def refresh_biographical_context_cache(
        self, 
        owner_id: str,
        facts_limit: Optional[int] = None,
        principles_limit: Optional[int] = None
    ) -> None:
        """Rebuild the cached biographical context of account `owner_id` from its current facts.

        Content: up to facts_limit facts in get_active_facts_ordered order, biographical domain
            first, then other non-directive domains; of these, facts tagged "mindset" become
            principles (they count toward facts_limit; those beyond principles_limit are dropped);
            agent_directive facts separately, up to 15 (DEFAULT_DIRECTIVES_CACHE_LIMIT).
        Limit: facts_limit and principles_limit are what the cache holds; an explicit value wins over the
            context service's own resolution (account defaults, then system default), None keeps that resolution.
        Idempotency: refreshing an unchanged account writes the same content.
        Errors: never raises; on a failure the previous cache stays (observed). Without a context
            service it does nothing (observed).
        """
        pass

    @abstractmethod
    async def get_biographical_context_cached(
        self,
        owner_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Return the cached biographical context of account `owner_id` (no fact query).

        Tenancy: owner_id None resolves the RequestContext's effective account; no account returns [].
        Returns: dicts with id, text, domain, tags, context_priority and created_at (ISO string):
            the facts, then the principles, then the directives, as of the last refresh.
        Limit: ignored; the whole cache is returned (observed).
        Errors: a missing or empty cache returns []; a backend failure raises (Lelik fails the call
            closed on it); the adapter logs it and re-raises.
        """
        pass

    @abstractmethod
    async def find_due_facts(self, now: datetime) -> List[DueFact]:
        """Return every `state == "current"` fact of every account whose `expires_at` is at or before `now`.

        Projection: account id, expires_at, expiry_kind, lifecycle_events and the change token; no text, no vectors.
        Tenancy: none (the janitor sweeps all accounts); each result names its account.
        Legacy: a document without `expires_at` (or with a null one) is not returned; one without `expiry_kind` is,
            with `expiry_kind=None`; one without `account_id` is skipped (logged).
        Order: none.
        """
        pass

    @abstractmethod
    async def stale_due_fact(self, due: DueFact, now: datetime) -> None:
        """Mark `due` stale in place: state stale, is_current True, valid_to null, `stale_since` and `last_updated`
        `now`, and a `lifecycle_events` entry (actor "janitor", reason "expired <expires_at ISO>"). Nothing else changes.

        Atomicity: a transaction under `due.update_time`.
        Errors: FactWriteConflict when the document changed since it was read, or no longer exists; nothing is written.
        """
        pass

    @abstractmethod
    async def stale_backlog(self) -> Dict[str, Tuple[int, Optional[datetime]]]:
        """Return, per account id, how many facts are `state == "stale"` and the oldest `stale_since` among them.

        A stale fact without `stale_since` counts but has no age; an account whose stale facts all lack it maps to
        (count, None). An account with no stale fact is absent; a document without `account_id` is skipped (logged).
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
