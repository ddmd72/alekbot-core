"""
Facts S3 dry-run kit (plan T12a, owner Q8): what the model dry run of the new consolidation prompts runs on.

Why a kit: the older benches intercept writes with fake successes, so no expiry rule ever ran, and they build prompts
from Firestore, where the S3 tokens are not uploaded yet. The kit lets the REAL pass loop, tool executor, parser and
`FactManagementAdapter` run against the live facts without writing a fact, a counter or an email mark:

- `NoWriteFactRepository`: reads go to the live repository; every write lands in an in-memory overlay that the next
  read of the same pass sees (a part created earlier in the pass can be named by the compound that is retired).
- `NoWriteIndexedEmails`: reads delegate, `mark_consolidated` and every other write is recorded only.
- `RecordingCounters`: replaces the agent's live lifecycle-counters store.
- `SnapshotTokenOverlay`: serves the five edited `CONSOLIDATION_*` tokens from `prompts_snapshot/`.
- `CostMeter`: prices every model call and stops the run before the next call when the cumulative spend (kept across
  runs in `scripts/memory/`) would pass `--budget-usd`.
- `DryRunKit`: installs all of it on a built `ConsolidationAgent` and writes the report.

Residual (not hidden by the kit): the agent's own `_call_llm` still writes a `prompt_content` row per call. The benches
call `execute()`, never `process()`, so no account usage is recorded; the run's spend exists only in the kit's ledger.
Vector searches, the biographical cache and the paginated / lineage / domain reads see the live store only.

Files written by the kit (spend ledger, its lock file, report) go to `scripts/memory/` (gitignored); any other path is
refused. One kit run at a time holds the ledger lock. Nothing here builds a client at import time.
"""

import asyncio
import fcntl
import hashlib
import itertools
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, AbstractSet, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.billing import calculate_cost
from src.domain.due_fact import DueFact
from src.domain.email import IndexedEmail, IndexingState
from src.domain.entities import FactEntity, FactState
from src.domain.consolidation import REVIEW_PROFILE_ID
from src.domain.exceptions import FactResolutionError, FactWriteConflict
from src.domain.fact_lifecycle import REVIEW_CLOSED_STATES, require_in_place
from src.domain.fact_lifecycle_day import CounterField, FactLifecycleDay, SweepSnapshot, check_count
from src.domain.fact_lifecycle_run_guard import run_guard_held
from src.domain.fact_resolution import ForwardWalk
from src.domain.fact_lifecycle_sweep import stale_fields
from src.domain.fact_review_rules import stale_order_key
from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_tool_result import STATUS_CREATED, WRITTEN_STATUSES, update_result
from src.domain.fact_update_outcome import FactUpdateOutcome
from src.domain.fact_write_context import NO_READABLE_SOURCE_NOTE, FactWriteContext, as_utc
from src.domain.pass_actor import PassActor
from src.domain.prompt_v3.agent_profile import AgentProfile
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.domain.search import EnrichedFact, consolidation_hit
from src.domain.stored_fact import StoredFact
from src.ports.alert_sink import AlertSinkPort
from src.ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from src.ports.fact_management_port import FactManagementPort
from src.ports.fact_update_port import FactUpdatePort
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.ports.prompt_v3.token_repository import TokenRepository
from src.ports.repository import FactRepository
from src.services.fact_write_service import FactWriteService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from src.utils.logger import logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = PROJECT_ROOT / "scripts" / "memory"
SPEND_LEDGER = MEMORY_DIR / "s3_dry_run_spend.json"
DEFAULT_REPORT = MEMORY_DIR / "s3_dry_run_report.json"
SNAPSHOT_DIR = PROJECT_ROOT / "prompts_snapshot"
DEFAULT_BUDGET_USD = 30.0       # the owner's cap (Q8); raising it is his call, see `check_budget_flag`
MAX_OVERLAY_HOPS = 10

# The tokens S3 edits (the sixth, CONSOLIDATION_OUTPUT_SPEC, is unchanged and still comes from Firestore).
S3_TOKEN_IDS: Tuple[str, ...] = (
    "CONSOLIDATION_COGNITIVE_PROCESS",
    "CONSOLIDATION_EXAMPLES",
    "CONSOLIDATION_POLICIES",
    "CONSOLIDATION_TAXONOMY",
    "CONSOLIDATION_TOOLS",
)

# The token the review pass adds (the review's other token, TAXONOMY, is one of the five above), the review's profile and
# blueprint (not in Firestore until the S4 rollout, so served from the snapshot), and what `prepare` requires of it.
REVIEW_TOKEN_IDS: Tuple[str, ...] = ("CONSOLIDATION_LIFECYCLE",)
REVIEW_PROFILE_TOKEN_IDS: Tuple[str, ...] = ("CONSOLIDATION_TAXONOMY", *REVIEW_TOKEN_IDS)

_ALL_STATES: AbstractSet[FactState] = frozenset(FactState)
_PRIORITY_RANK = {"critical": 1, "high": 2, "medium": 3, "low": 4, "archival": 5}
_VECTORS = ("vector", "tags_vector", "metadata_vector")


def _holds_note(joined: Any, note: str) -> bool:
    """Whether `note` is one whole note of a result's `note` (the notes joined with "; "): a note may itself hold "; "."""
    return isinstance(joined, str) and f"; {note}; " in f"; {joined}; "


def _without_vectors(doc: FactEntity, *names: str) -> FactEntity:
    """A copy of an overlay document as the store's projection would return it: the named vectors read as None."""
    return doc.model_copy(deep=True, update={name: None for name in names})


class BudgetExceeded(RuntimeError):
    """The run stops before the next model call: its spend (or its unpriced model) would break the cap."""


class UnpricedModel(BudgetExceeded):
    """The model has no price in `domain.billing`, so the meter could not keep the cap."""


class LedgerBusy(RuntimeError):
    """Another dry-run process holds the spend ledger: one kit run at a time."""


class PathOutsideMemory(ValueError):
    """The kit writes only under `scripts/memory/`."""


class SnapshotTokenError(RuntimeError):
    """The S3 prompt tokens cannot be served as the snapshot has them, so the run would test another prompt."""


class KitNotPrepared(RuntimeError):
    """A model call was made before `DryRunKit.prepare` checked the snapshot tokens."""


def inside_memory_dir(path: Path) -> Path:
    """`path` resolved, or PathOutsideMemory when it is not under `scripts/memory/`."""
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(MEMORY_DIR.resolve())
    except ValueError:
        raise PathOutsideMemory(f"{path} is outside {MEMORY_DIR}: the kit writes only there") from None
    return resolved


# ------------------------------------------------------------------------------------------------ the fact store

class NoWriteFactRepository(FactRepository):
    """The live fact repository for reads; every write goes to an in-memory overlay and is recorded in `writes`.

    Documents and change tokens of the overlay stand in for the store's: a precondition is checked against the overlay
    token (a synthetic counter per written document) or, for a document the pass has not written, the live
    `update_time`. Vector searches (which drop a hit the run closed, but add none), the paginated, lineage and domain
    reads, `find_due_facts` and `stale_backlog` see the live store only; `get_fact_by_id`, `resolve_current`, the three
    current-fact lists and the review's two reads (`get_stale_facts`, `get_recently_closed`) see the overlay too.

    Tenancy: `update_fact_fields` refuses with PermissionError when the document's `account_id` is not the one `stored`
    was read with, as the live store does. The kit reads live documents through the port, which shows a legacy
    `owner_id` (or a stray one beside another account's `account_id`) as the caller's account, so it does not reproduce
    the live store's refusal for such documents; none is live (FACT_LIFECYCLE_RFC D-7).
    """

    def __init__(self, real: FactRepository) -> None:
        self._real = real
        self._docs: Dict[str, FactEntity] = {}
        self._tokens: Dict[str, Tuple[str, int]] = {}
        self._clock = itertools.count(1)
        self._lock = asyncio.Lock()     # a write is check-then-put with a live read between: one at a time, as a transaction is
        self.writes: List[Dict[str, Any]] = []

    def _record(self, kind: str, **info: Any) -> None:
        self.writes.append({"kind": kind, **info})

    def overlay_document(self, fact_id: str) -> Optional[FactEntity]:
        """The pass's own copy of a document (written or changed in this run), else None."""
        doc = self._docs.get(fact_id)
        return doc.model_copy(deep=True) if doc is not None else None

    def closed_count(self) -> int:
        """How many overlay documents are not current (each can make a live search hit drop out or merge into another)."""
        return sum(1 for doc in self._docs.values() if doc.state is not FactState.CURRENT)

    def forget_overlay(self) -> None:
        """Drop the pass's own documents so the next run starts from the live store; `writes` keeps the record."""
        self._docs.clear()
        self._tokens.clear()

    def _put(self, fact: FactEntity) -> None:
        self._docs[fact.id] = fact.model_copy(deep=True)
        self._tokens[fact.id] = ("overlay", next(self._clock))

    async def _read(self, fact_id: str, account_id: str) -> Optional[StoredFact]:
        """The document as it stands in the overlay, else the live one, with the token a write is checked against."""
        doc = self._docs.get(fact_id)
        if doc is not None:
            return StoredFact(doc.model_copy(deep=True), self._tokens[fact_id])
        return await self._real.resolve_current(fact_id, account_id, _ALL_STATES)

    async def _check_token(self, fact_id: str, account_id: str, token: object) -> None:
        current = await self._read(fact_id, account_id)
        if current is None or current.update_time != token:
            raise FactWriteConflict(fact_id)

    def _base(self, stored: StoredFact) -> FactEntity:
        """The document a write changes: the overlay's copy, else the one `stored` was read as (its token still holds)."""
        return self._docs.get(stored.fact.id) or stored.fact

    async def add_fact(self, fact: FactEntity) -> str:
        async with self._lock:
            self._put(fact)
            self._record("add_fact", fact_id=fact.id)
        return fact.id

    def seed_document(self, fact: FactEntity) -> None:
        """Put a document in the overlay as it is, without a record in `writes`: a bench case's input (a fact the live
        store lacks), not a write of the run. It has no live rank, so no vector search finds it."""
        self._put(fact)

    async def get_fact_by_id(self, fact_id: str) -> Optional[FactEntity]:
        doc = self._docs.get(fact_id)
        if doc is not None:
            return doc.model_copy(deep=True)
        return await self._real.get_fact_by_id(fact_id)

    async def resolve_current(
        self, fact_id: str, account_id: str, accepted: AbstractSet[FactState]
    ) -> Optional[StoredFact]:
        walk = ForwardWalk(fact_id, account_id, accepted)
        while walk.wanted is not None:
            walk.feed(await self._read(walk.wanted, account_id))
        return walk.result

    async def write_superseding(
        self, new_fact: FactEntity, closures: Sequence[Tuple[StoredFact, Mapping[str, Any]]]
    ) -> None:
        if not closures:
            raise ValueError(f"write_superseding of {new_fact.id} replaces no document; use add_fact")
        for _, fields in closures:
            require_in_place(fields)
        missing = [name for name in _VECTORS if getattr(new_fact, name) is None]
        if missing:
            raise ValueError(f"fact {new_fact.id} has no {', '.join(missing)}; a version without vectors leaves vector search")
        async with self._lock:
            await self._supersede(new_fact, closures)

    async def _supersede(self, new_fact: FactEntity, closures: Sequence[Tuple[StoredFact, Mapping[str, Any]]]) -> None:
        for stored, _ in closures:
            await self._check_token(stored.fact.id, stored.fact.account_id, stored.update_time)
        if new_fact.id in self._docs or await self._real.get_fact_by_id(new_fact.id) is not None:
            raise ValueError(f"fact {new_fact.id} already exists")
        # A document read under its token and still carrying it is unchanged, so the fields apply to it as read.
        closed = [stored.fact.model_copy(update=dict(fields)) for stored, fields in closures]
        self._put(new_fact)
        for fact in closed:
            self._put(fact)
        self._record("write_superseding", new_id=new_fact.id, replaced=[stored.fact.id for stored, _ in closures])

    async def archive_and_add(
        self, archived: StoredFact, fields: Mapping[str, Any], new_fact: FactEntity
    ) -> None:
        require_in_place(fields)
        account_id = archived.fact.account_id
        if new_fact.account_id != account_id:
            raise ValueError(f"fact {new_fact.id} belongs to another account than the archived {archived.fact.id}")
        missing = [name for name in _VECTORS if getattr(new_fact, name) is None]
        if missing:
            raise ValueError(f"fact {new_fact.id} has no {', '.join(missing)}; a fact without vectors leaves vector search")
        async with self._lock:
            current = await self._read(archived.fact.id, account_id)
            if current is None or current.update_time != archived.update_time:
                raise FactWriteConflict(archived.fact.id)
            if current.fact.account_id != account_id:
                raise PermissionError("Not authorized to modify this fact")
            if new_fact.id in self._docs or await self._real.get_fact_by_id(new_fact.id) is not None:
                raise ValueError(f"fact {new_fact.id} already exists")
            self._put(new_fact)
            self._put(archived.fact.model_copy(update=dict(fields)))
            self._record("archive_and_add", archived_id=archived.fact.id, new_id=new_fact.id)

    async def update_fact_fields(self, stored: StoredFact, fields: Mapping[str, Any]) -> None:
        require_in_place(fields)
        async with self._lock:
            await self._apply_fields(stored, fields)
            self._record("update_fact_fields", fact_id=stored.fact.id, fields=sorted(fields))

    async def _apply_fields(self, stored: StoredFact, fields: Mapping[str, Any]) -> None:
        current = await self._read(stored.fact.id, stored.fact.account_id)
        if current is None or current.update_time != stored.update_time:
            raise FactWriteConflict(stored.fact.id)
        # The store's tenancy precondition (`FirestoreFactRepository.update_fact_fields`), on the document as the kit sees it.
        if current.fact.account_id != stored.fact.account_id:
            raise PermissionError("Not authorized to modify this fact")
        base = self._base(stored)
        self._put(FactEntity.model_validate({**base.model_dump(), **fields}))

    async def stale_due_fact(self, due: DueFact, fields: Mapping[str, Any]) -> None:
        require_in_place(fields)
        async with self._lock:
            await self._check_token(due.fact_id, due.account_id, due.update_time)
            # The whole document, to hold the overlay copy; the fields are applied to it as given.
            base = self._docs.get(due.fact_id) or await self._real.get_fact_by_id(due.fact_id)
            if base is None:
                raise FactWriteConflict(due.fact_id)
            self._put(FactEntity.model_validate({**base.model_dump(), **fields}))
            self._record("stale_due_fact", fact_id=due.fact_id)

    async def add_fact_if_unique(
        self, fact: FactEntity, similarity_threshold: float = 0.85
    ) -> tuple[bool, Optional[str]]:
        async with self._lock:
            self._put(fact)
            self._record("add_fact_if_unique", fact_id=fact.id, duplicate_check="not run")
        return True, fact.id

    async def refresh_biographical_context_cache(
        self, owner_id: str, facts_limit: Optional[int] = None, principles_limit: Optional[int] = None
    ) -> None:
        self._record("refresh_biographical_context_cache")

    def _current_view(
        self, listed: Sequence[FactEntity], account_id: str, domain: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> List[FactEntity]:
        """The live list with the pass's own writes applied: closed documents leave it, new current ones join it."""
        def holds(doc: FactEntity) -> bool:
            return (
                doc.account_id == account_id and doc.state is FactState.CURRENT
                and (domain is None or (doc.domain is not None and doc.domain.value == domain))
                and (not tags or bool(set(tags) & set(doc.tags)))
            )

        out: List[FactEntity] = []
        for fact in listed:
            doc = self._docs.get(fact.id)
            if doc is None:
                out.append(fact)
            elif holds(doc):
                out.append(doc.model_copy(deep=True))
        listed_ids = {fact.id for fact in listed}
        out.extend(doc.model_copy(deep=True) for fact_id, doc in self._docs.items() if fact_id not in listed_ids and holds(doc))
        return out

    async def get_active_facts(self, owner_id: str, tags: Optional[List[str]] = None) -> List[FactEntity]:
        return self._current_view(await self._real.get_active_facts(owner_id, tags), owner_id, tags=tags)

    async def get_active_facts_ordered(
        self, account_id: str, domain: Optional[str] = None, limit: Optional[int] = None
    ) -> List[FactEntity]:
        facts = self._current_view(await self._real.get_active_facts_ordered(account_id, domain, None), account_id, domain)

        def rank(fact: FactEntity) -> int:
            return _PRIORITY_RANK.get(getattr(fact.context_priority, "value", fact.context_priority), 3)

        facts = sorted((f for f in facts if rank(f) < _PRIORITY_RANK["archival"]), key=lambda f: -f.created_at.timestamp())
        facts.sort(key=rank)
        return facts[:limit] if limit else facts

    async def get_longest_facts(self, account_id: str, limit: int) -> List[FactEntity]:
        facts = await self.get_active_facts(account_id)
        return sorted(facts, key=lambda f: len(f.text.split()), reverse=True)[:limit]

    async def get_paginated_facts(
        self, owner_id: str, limit: int = 100, cursor_doc_id: Optional[str] = None, domain: Optional[str] = None,
        state: str = "current",
    ) -> Tuple[List[FactEntity], Optional[str]]:
        return await self._real.get_paginated_facts(owner_id, limit, cursor_doc_id, domain, state)

    async def get_stale_facts(self, account_id: str) -> List[FactEntity]:
        """The live stale list with the pass's own writes applied: a fact the run staled joins it, one it changed shows the
        overlay document, one it moved out of stale (revived, archived, closed) leaves it. Same projection and order."""
        out: Dict[str, FactEntity] = {}
        for fact in await self._real.get_stale_facts(account_id):
            if fact.id not in self._docs:
                out[fact.id] = fact
        for fact_id, doc in self._docs.items():
            if doc.account_id == account_id and doc.state is FactState.STALE:
                out[fact_id] = _without_vectors(doc, "tags_vector", "metadata_vector")
        return sorted(out.values(), key=stale_order_key)

    async def get_recently_closed(self, account_id: str, since: datetime, limit: int) -> List[FactEntity]:
        """The live list with the pass's own writes applied (as `get_stale_facts`). Each overlay document can displace one
        live row, so the live read asks for that many more than `limit`."""
        out: Dict[str, FactEntity] = {}
        for fact in await self._real.get_recently_closed(account_id, since, limit + len(self._docs)):
            if fact.id not in self._docs:
                out[fact.id] = fact
        floor = as_utc(since)
        for fact_id, doc in self._docs.items():
            if (
                doc.account_id == account_id and doc.state in REVIEW_CLOSED_STATES
                and doc.valid_to is not None and as_utc(doc.valid_to) >= floor
            ):
                out[fact_id] = _without_vectors(doc, *_VECTORS)
        ordered = sorted(out.values(), key=lambda f: f.id)
        ordered.sort(key=lambda f: as_utc(f.valid_to) if f.valid_to is not None else floor, reverse=True)
        return ordered[:limit]

    async def search_facts(
        self, query_vector: List[float], vector_field: str = "vector", limit: int = 5,
        user_id: Optional[str] = None, account_id: Optional[str] = None,
    ) -> List[FactEntity]:
        """The live hits without the ones the run closed (their overlay copy is not current): production's search sees only
        current facts. A fact the run created is not a hit (it has no live rank)."""
        hits = await self._real.search_facts(query_vector, vector_field, limit + self.closed_count(), user_id, account_id)
        kept = [h for h in hits if (doc := self._docs.get(h.id)) is None or doc.state is FactState.CURRENT]
        return kept[:limit]

    async def search_facts_by_domain(
        self, domains: List[str], limit: int = 10, account_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> List[FactEntity]:
        return await self._real.search_facts_by_domain(domains, limit, account_id, user_id)

    async def get_lineage(self, account_id: str, lineage_id: str) -> List[FactEntity]:
        return await self._real.get_lineage(account_id, lineage_id)

    async def get_latest_fact_by_lineage(self, owner_id: str, lineage_id: str) -> Optional[FactEntity]:
        return await self._real.get_latest_fact_by_lineage(owner_id, lineage_id)

    async def get_biographical_context_cached(self, owner_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return await self._real.get_biographical_context_cached(owner_id, limit)

    async def find_due_facts(self, now: datetime) -> List[DueFact]:
        return await self._real.find_due_facts(now)

    async def stale_backlog(self) -> Dict[str, Tuple[int, Optional[datetime]]]:
        return await self._real.stale_backlog()

    async def get_legacy_facts(self, account_id: str, limit: int = 20) -> List[FactEntity]:
        return await self._real.get_legacy_facts(account_id, limit)


class NoWriteIndexedEmails(IndexedEmailRepository):
    """Indexed emails for reads (naive `email_date`s, as production returns them); every write is recorded only."""

    def __init__(self, real: IndexedEmailRepository) -> None:
        self._real = real
        self._marked: set = set()
        self.writes: List[Dict[str, Any]] = []

    async def get_unconsolidated_batch(self, user_id: str, limit: int = 200) -> List[IndexedEmail]:
        """The live batch without the emails this run marked consolidated (the live store never learns of the mark)."""
        hidden = sum(1 for marked_user, _ in self._marked if marked_user == user_id)
        batch = await self._real.get_unconsolidated_batch(user_id, limit + hidden)
        return [e for e in batch if (user_id, e.email_id) not in self._marked][:limit]

    async def find_nearest(
        self, user_id: str, vectors: Dict[str, List[float]], limit: int = 10, state: str = "current",
        date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    ) -> List[IndexedEmail]:
        return await self._real.find_nearest(user_id, vectors, limit, state, date_from, date_to)

    async def get_indexing_state(self, user_id: str, provider: str) -> Optional[IndexingState]:
        return await self._real.get_indexing_state(user_id, provider)

    async def count_by_user(self, user_id: str, provider: Optional[str] = None) -> int:
        return await self._real.count_by_user(user_id, provider)

    async def get_pending_embeddings(self, limit: int = 100) -> List[IndexedEmail]:
        return await self._real.get_pending_embeddings(limit)

    async def mark_consolidated(self, user_id: str, email_ids: List[str], consolidated_at: datetime) -> None:
        self._marked.update((user_id, email_id) for email_id in email_ids)
        self.writes.append({"kind": "mark_consolidated", "user_id": user_id, "email_ids": list(email_ids)})

    async def save_batch(self, emails: List[IndexedEmail]) -> int:
        self.writes.append({"kind": "save_batch", "count": len(emails)})
        return len(emails)

    async def update_indexing_state(self, state: IndexingState) -> None:
        self.writes.append({"kind": "update_indexing_state"})

    async def clear_indexing_state(self, user_id: str, provider: str) -> None:
        self.writes.append({"kind": "clear_indexing_state"})

    async def set_reconnect_flags(self, user_id: str, provider: str, *, reconnect_notified: Optional[bool] = None,
                                  disconnected_by_user_at: Any = None) -> None:
        self.writes.append({"kind": "set_reconnect_flags"})

    async def list_indexing_user_ids(self, provider: str) -> List[str]:
        return await self._real.list_indexing_user_ids(provider)

    async def delete_by_user(self, user_id: str) -> None:
        self.writes.append({"kind": "delete_by_user"})

    async def update_vectors(self, user_id: str, email_id: str, vectors: Dict[str, List[float]]) -> None:
        self.writes.append({"kind": "update_vectors", "email_id": email_id})


# ------------------------------------------------------------------------------------------------ counters and alerts

class RecordingCounters(FactLifecycleCountersPort):
    """Stands in for the live lifecycle-counters store: it keeps the typed values it is given and refuses what the
    production store refuses."""

    def __init__(self) -> None:
        self.adds: List[Tuple[str, date, Dict[CounterField, int]]] = []
        self.sweeps: List[Tuple[str, date, SweepSnapshot]] = []
        self._claimed: set = set()
        self._run_owner: Optional[str] = None
        self._run_until: Optional[datetime] = None

    @staticmethod
    def _check(account_id: str, day: date) -> None:
        if not account_id:
            raise ValueError("account_id is required for the fact lifecycle counters")
        if isinstance(day, datetime):
            raise ValueError("day must be a date, not a datetime")

    @property
    def expiry_tool_errors(self) -> int:
        return sum(counts.get(CounterField.EXPIRY_TOOL_ERRORS, 0) for _, _, counts in self.adds)

    async def record_sweep(self, account_id: str, day: date, snapshot: SweepSnapshot) -> None:
        self._check(account_id, day)
        self.sweeps.append((account_id, day, snapshot))

    async def add(self, account_id: str, day: date, counts: Mapping[CounterField, int]) -> None:
        self._check(account_id, day)
        added: Dict[CounterField, int] = {}
        for name, value in counts.items():
            counter = CounterField(name)
            check_count(counter.value, value)
            if value:
                added[counter] = value
        if added:
            self.adds.append((account_id, day, added))

    async def for_day(self, day: date) -> List[FactLifecycleDay]:
        snapshots = {account_id: snapshot for account_id, added_day, snapshot in self.sweeps if added_day == day}
        totals: Dict[str, Counter] = {}
        for account_id, added_day, counts in self.adds:
            if added_day == day:
                totals.setdefault(account_id, Counter()).update(counts)
        return [
            FactLifecycleDay.of(account_id, day, snapshots.get(account_id), dict(totals.get(account_id, {})))
            for account_id in sorted(set(snapshots) | set(totals))
        ]

    async def claim_line(self, day: date) -> bool:
        first = day not in self._claimed
        self._claimed.add(day)
        return first

    async def acquire_run(self, owner: str, now: datetime, until: datetime) -> bool:
        if not owner:
            raise ValueError("owner is required to take the janitor's run guard")
        if now.tzinfo is None or until.tzinfo is None:
            raise ValueError("now and until must be timezone-aware")
        if until <= now:
            raise ValueError("until must be later than now")
        if run_guard_held(self._run_until, now):
            return False
        self._run_owner, self._run_until = owner, until
        return True

    async def release_run(self, owner: str) -> None:
        if self._run_owner == owner:
            self._run_owner = self._run_until = None


class StatsSink(AlertSinkPort):
    """Collects the 📊 stats lines and 🚨 alerts the agent posts (run the agent with `stats_alerts=True`)."""

    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


# ------------------------------------------------------------------------------------------------ prompt tokens

@lru_cache(maxsize=1)
def _serializer() -> Any:
    import importlib.util

    path = PROJECT_ROOT / "firestore_utils" / "snapshot_serializer.py"
    spec = importlib.util.spec_from_file_location("snapshot_serializer", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SnapshotTokenOverlay(TokenRepository):
    """Serves the S3 tokens from `prompts_snapshot/` (built as `FirestoreTokenRepository.get` builds them, through
    `Token.create` and the same security port) and every other token from `real`. Read only."""

    def __init__(
        self, real: TokenRepository, security_port: Any, snapshot_dir: Path = SNAPSHOT_DIR,
        token_ids: Sequence[str] = S3_TOKEN_IDS,
    ) -> None:
        self._real = real
        self._security = security_port
        self._dir = Path(snapshot_dir)
        self._ids = tuple(token_ids)
        self._built: Dict[str, Token] = {}

    async def _load(self, token_id: str) -> Token:
        if token_id in self._built:
            return self._built[token_id]
        path = next((p for sub in ("system", "user") if (p := self._dir / "tokens" / sub / f"{token_id}.groovy").is_file()), None)
        if path is None:
            raise KeyError(f"Token not found in the snapshot: {token_id}")
        doc = _serializer().token_from_file(path.read_text(encoding="utf-8"))
        if doc.get("token_id") != token_id:
            raise ValueError(f"snapshot file for {token_id} names token {doc.get('token_id')!r}")
        token = await Token.create(
            id=TokenId(token_id), category=TokenCategory(doc["category"]), class_=TokenClass(doc["class"]),
            content=doc["content"], metadata=doc.get("metadata", {}), security_port=self._security,
        )
        self._built[token_id] = token
        return token

    async def get(self, token_id: TokenId) -> Token:
        if str(token_id) in self._ids:
            return await self._load(str(token_id))
        return await self._real.get(token_id)

    async def _merged(self, listed: List[Token], keep: Any) -> List[Token]:
        served = {tid: await self._load(tid) for tid in self._ids}
        merged = [served.pop(str(token.id), token) if str(token.id) in served else token for token in listed]
        return merged + [token for token in served.values() if keep(token)]

    async def list_by_category(self, category: TokenCategory) -> List[Token]:
        return await self._merged(await self._real.list_by_category(category), lambda t: t.category == category)

    async def list_by_class(self, token_class: TokenClass) -> List[Token]:
        return await self._merged(await self._real.list_by_class(token_class), lambda t: t.class_ == token_class)

    async def list_all(self) -> List[Token]:
        return await self._merged(await self._real.list_all(), lambda t: True)

    async def exists(self, token_id: TokenId) -> bool:
        return str(token_id) in self._ids or await self._real.exists(token_id)

    async def save(self, token: Token) -> None:
        raise PermissionError("the snapshot token overlay is read only")

    async def delete(self, token_id: TokenId) -> None:
        raise PermissionError("the snapshot token overlay is read only")


def _snapshot_document(snapshot_dir: Path, kind: str, doc_id: str) -> Dict[str, Any]:
    """A profile or blueprint document from `prompts_snapshot/<kind>/<doc_id>.yaml`; KeyError when it has no file."""
    path = Path(snapshot_dir) / kind / f"{doc_id}.yaml"
    if not path.is_file():
        raise KeyError(f"{kind[:-1].capitalize()} not found in the snapshot: {doc_id}")
    return dict(_serializer().doc_from_yaml(path.read_text(encoding="utf-8")))


class SnapshotProfileOverlay(AgentProfileRepository):
    """Serves the review's agent profile from `prompts_snapshot/profiles/` (built as `FirestoreAgentProfileRepository`
    builds one) and every other profile, and every override, from `real`. Read only."""

    def __init__(self, real: AgentProfileRepository, snapshot_dir: Path = SNAPSHOT_DIR,
                 agent_ids: Sequence[str] = (REVIEW_PROFILE_ID,)) -> None:
        self._real = real
        self._dir = Path(snapshot_dir)
        self._ids = tuple(agent_ids)

    async def get_agent_profile(self, agent_id: str) -> AgentProfile:
        if agent_id not in self._ids:
            return await self._real.get_agent_profile(agent_id)
        doc = _snapshot_document(self._dir, "profiles", agent_id)
        if doc.get("agent_id") != agent_id:
            raise ValueError(f"snapshot profile file for {agent_id} names agent {doc.get('agent_id')!r}")
        tokens = {tid: ProfileToken.from_dict(tid, data) for tid, data in (doc.get("tokens") or {}).items()}
        return AgentProfile(blueprint_id=doc.get("blueprint_id", f"{agent_id}_agent_v1"), tokens=tokens)

    async def get_override_tokens(self, owner_type: OwnerType, owner_id: str) -> Dict[str, ProfileToken]:
        return await self._real.get_override_tokens(owner_type, owner_id)

    async def set_override_tokens(
        self, owner_type: OwnerType, owner_id: str, tokens: Dict[str, ProfileToken], clear_ids: Optional[set] = None,
    ) -> None:
        raise PermissionError("the snapshot profile overlay is read only")

    async def delete_profile(self, owner_type: OwnerType, owner_value: str) -> None:
        raise PermissionError("the snapshot profile overlay is read only")


class SnapshotBlueprintOverlay(BlueprintRepository):
    """Serves the review's blueprint from `prompts_snapshot/blueprints/` (built as `FirestoreBlueprintRepository` builds
    one) and every other blueprint from `real`. Read only."""

    def __init__(self, real: BlueprintRepository, snapshot_dir: Path = SNAPSHOT_DIR,
                 blueprint_ids: Sequence[str] = ("consolidation_review_v1",)) -> None:
        self._real = real
        self._dir = Path(snapshot_dir)
        self._ids = tuple(blueprint_ids)

    def _load(self, blueprint_id: str) -> Blueprint:
        doc = _snapshot_document(self._dir, "blueprints", blueprint_id)
        blueprint = Blueprint(id=doc["blueprint_id"], outer_class=doc["outer_class"], class_order=list(doc["class_order"]))
        blueprint.validate()
        return blueprint

    async def get(self, blueprint_id: str) -> Blueprint:
        return self._load(blueprint_id) if blueprint_id in self._ids else await self._real.get(blueprint_id)

    async def list_all(self) -> List[Blueprint]:
        listed = await self._real.list_all()
        served = {bid: self._load(bid) for bid in self._ids}
        return [served.pop(b.id, b) for b in listed] + list(served.values())

    async def exists(self, blueprint_id: str) -> bool:
        return blueprint_id in self._ids or await self._real.exists(blueprint_id)

    async def save(self, blueprint: Blueprint) -> None:
        raise PermissionError("the snapshot blueprint overlay is read only")

    async def delete(self, blueprint_id: str) -> None:
        raise PermissionError("the snapshot blueprint overlay is read only")


# ------------------------------------------------------------------------------------------------ cost

class CostMeter:
    """Prices each model call and keeps a cumulative ledger across runs.

    Before a call it refuses (BudgetExceeded) when the spend so far has reached the cap or when the largest single call
    seen so far would pass it; a model without a price is refused too, since its cost could not be kept under the cap.
    """

    def __init__(self, ledger_path: Path = SPEND_LEDGER, budget_usd: float = DEFAULT_BUDGET_USD) -> None:
        self.ledger_path = inside_memory_dir(ledger_path)
        self.budget_usd = float(budget_usd)
        self.aborted = False
        self._lock_fd: Optional[int] = None
        self._acquire_lock()
        self._ledger = self._load()
        self._run: Optional[Dict[str, Any]] = None

    def _acquire_lock(self) -> None:
        """Hold an exclusive lock on the ledger for the whole run, so two runs cannot lose each other's spend."""
        lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LedgerBusy(
                f"another dry-run process holds {lock_path.name}: run one bench at a time (the spend cap is shared)"
            ) from None
        self._lock_fd = fd

    def close(self) -> None:
        """End the run: release the ledger lock (the process ending does it too)."""
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _load(self) -> Dict[str, Any]:
        ledger: Dict[str, Any] = {"total_usd": 0.0, "calls": 0, "unmetered_calls": 0, "max_call_usd": 0.0, "by_model": {}, "runs": []}
        if self.ledger_path.is_file():
            ledger.update(json.loads(self.ledger_path.read_text(encoding="utf-8")))
        return ledger

    def _save(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._ledger, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.ledger_path)

    @property
    def spent_usd(self) -> float:
        return float(self._ledger["total_usd"])

    @property
    def run_usd(self) -> float:
        return float(self._run["usd"]) if self._run else 0.0

    @property
    def calls(self) -> int:
        return int(self._ledger["calls"])

    @property
    def unmetered_calls(self) -> int:
        return int(self._ledger["unmetered_calls"])

    def check(self, model: str) -> None:
        """Raise BudgetExceeded when the next call must not be made."""
        if calculate_cost(model, 1_000_000, 1_000_000) == 0.0:
            self.aborted = True
            raise UnpricedModel(f"model {model!r} has no price in domain.billing: the cap cannot be kept; run stopped")
        spent, biggest = self.spent_usd, float(self._ledger["max_call_usd"])
        if spent >= self.budget_usd or spent + biggest > self.budget_usd:
            self.aborted = True
            raise BudgetExceeded(
                f"spend ${spent:.4f} (largest call ${biggest:.4f}) would pass the ${self.budget_usd:.2f} cap; run stopped"
            )

    def record(self, model: str, response: Any) -> float:
        """Price `response` and add it to the ledger (saved at once, so a crash keeps it)."""
        usage = getattr(response, "usage_metadata", None)
        cost = 0.0
        if usage is None:
            self._ledger["unmetered_calls"] += 1
        else:
            cost = calculate_cost(
                model, usage.prompt_tokens, usage.completion_tokens, usage.cache_read_tokens, usage.cache_creation_tokens
            )
        if self._run is None:
            self._run = {"started": datetime.now(timezone.utc).isoformat(), "usd": 0.0, "calls": 0}
            self._ledger["runs"].append(self._run)
        self._run["usd"] = round(self._run["usd"] + cost, 6)
        self._run["calls"] += 1
        self._ledger["total_usd"] = round(self._ledger["total_usd"] + cost, 6)
        self._ledger["calls"] += 1
        self._ledger["max_call_usd"] = max(float(self._ledger["max_call_usd"]), cost)
        leg = self._ledger["by_model"].setdefault(model, {"calls": 0, "usd": 0.0})
        leg["calls"] += 1
        leg["usd"] = round(leg["usd"] + cost, 6)
        self._save()
        return cost

    def wrap(self, agent: Any) -> None:
        """Meter `agent._call_llm`."""
        inner = agent._call_llm

        async def metered(request: Any, *args: Any, **kwargs: Any) -> Any:
            self.check(request.model_name)
            response = await inner(request, *args, **kwargs)
            self.record(request.model_name, response)
            return response

        agent._call_llm = metered


# ------------------------------------------------------------------------------------------------ owner zone

async def owner_timezone(account_repo: Any, user_repo: Any, account_id: str, override: Optional[str] = None) -> str:
    """The account owner's IANA zone, resolved by production's `OwnerTimezoneResolver` over the kit's stores (any missing
    step or failure gives UTC with one warning). `override` (`--timezone`) wins and must be a zone."""
    if override:
        try:
            ZoneInfo(override)
        except (KeyError, ValueError, OSError) as exc:
            raise ValueError(f"--timezone {override!r} is not an IANA zone") from exc
        return override
    return (await OwnerTimezoneResolver(account_repo, user_repo).resolve(account_id)).zone


def reported_date_form(value: Any) -> Optional[str]:
    """How the model wrote a `reported_date`: `naive` (ISO without an offset, a bare date included), `aware` (ISO with an
    offset), `other` (anything else), or None when none was sent."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return "other"
        return "aware" if parsed.tzinfo is not None else "naive"
    return "other"


def derived_from_count(value: Any) -> int:
    """How many fact ids a create call named in `derived_from` (an id or a list of ids; anything else names none)."""
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if isinstance(value, (list, tuple)):
        return sum(1 for item in value if item)
    return 0


def hit_from_document(doc: FactEntity, hit: Dict[str, Any], timezone: str) -> Dict[str, Any]:
    """A search hit re-rendered from the overlay document by production's own shaping (`EnrichedFact.from_fact`,
    `consolidation_hit`); an extra key of `hit` stays."""
    shaped = consolidation_hit(EnrichedFact.from_fact(doc, hit.get("source", ""), hit.get("similarity")), timezone)
    return {**hit, **shaped}


# ------------------------------------------------------------------------------------------------ the write recorder

class OwnFactsSearch:
    """The search port Stage 2a's cluster builder sees on the kit: the real search, plus the facts the run wrote, each
    returned (with relevance 1, as a fact finding itself) by the search of its own content."""

    def __init__(self, inner: FactManagementPort, own: Dict[str, List[Dict[str, Any]]]) -> None:
        self._inner = inner
        self._own = own

    async def search_existing_facts(
        self, keywords: List[str], primary_query: str, alternative_query: str = "", limit: int = 20, *, timezone: str
    ) -> List[Dict[str, Any]]:
        hits = await self._inner.search_existing_facts(keywords, primary_query, alternative_query, limit, timezone=timezone)
        return [*self._own.get(primary_query, []), *hits][:limit]


class WriteRecorder(FactManagementPort, FactUpdatePort):
    """Wraps the real adapter (built on the overlay): one report row per create, update (model or typed) and merge call.

    The wrapped port must offer the typed update too (a FactUpdatePort, or a wrapper that forwards its methods); one that
    does not is refused when the recorder is built."""

    def __init__(self, inner: FactManagementPort, kit: "DryRunKit") -> None:
        if not callable(getattr(inner, "apply_fact_update", None)):
            raise TypeError(f"{type(inner).__name__} has no typed update path (FactUpdatePort)")
        self._inner = inner
        self._kit = kit

    async def search_existing_facts(
        self, keywords: List[str], primary_query: str, alternative_query: str = "", limit: int = 20, *, timezone: str
    ) -> List[Dict[str, Any]]:
        spare = self._kit.repo.closed_count() if self._kit.repo else 0       # hits the correction will drop
        hits = await self._inner.search_existing_facts(
            keywords, primary_query, alternative_query, limit + spare, timezone=timezone)
        return self._kit.correct_hits(hits, timezone)[:limit]

    async def create_fact(self, content: str, metadata: Dict[str, Any], *, context: FactWriteContext) -> Dict[str, Any]:
        return await self._kit.logged(
            "create", context, metadata.get("expiry"), metadata.get("reported_date"), "fact_id",
            self._inner.create_fact(content, metadata, context=context), derived_from=metadata.get("derived_from"),
        )

    async def update_fact(
        self, fact_id: str, updates: Dict[str, Any], account_id: str, *, context: FactWriteContext
    ) -> Dict[str, Any]:
        return await self._kit.logged(
            "update", context, updates.get("expiry"), updates.get("reported_date"), "fact_id",
            self._inner.update_fact(fact_id, updates, account_id, context=context),
        )

    async def apply_fact_update(
        self,
        fact_id: str,
        update: ParsedFactUpdate,
        account_id: str,
        *,
        context: FactWriteContext,
        evidence_at: Optional[datetime] = None,
        reason: Optional[str] = None,
        history: Optional[str] = None,
        expected_id: Optional[str] = None,
    ) -> FactUpdateOutcome:
        # The report row shows what was sent; a typed update sends no model JSON, so its decision is shown as written.
        reported = update.reported_date.isoformat() if update.reported_date is not None else None
        return await self._kit.logged_typed(
            context, update.expiry, reported,
            self._inner.apply_fact_update(  # type: ignore[attr-defined]  # checked when built
                fact_id, update, account_id, context=context, evidence_at=evidence_at, reason=reason, history=history,
                expected_id=expected_id,
            ),
        )

    async def merge_facts(
        self, fact_ids: List[str], merged_content: str, metadata: Dict[str, Any], *, context: FactWriteContext
    ) -> Dict[str, Any]:
        return await self._kit.logged(
            "merge", context, metadata.get("expiry"), metadata.get("reported_date"), "new_fact_id",
            self._inner.merge_facts(fact_ids, merged_content, metadata, context=context),
        )


# ------------------------------------------------------------------------------------------------ the kit

class BudgetAboveOwnerCap(ValueError):
    """`--budget-usd` is above the owner's cap and `--allow-over-owner-cap` was not given."""


def check_budget_flag(budget_usd: float, allow_over_owner_cap: bool = False) -> float:
    """`budget_usd`, or BudgetAboveOwnerCap when it is above the owner's $30 cap and the owner's flag is not set."""
    if budget_usd > DEFAULT_BUDGET_USD and not allow_over_owner_cap:
        raise BudgetAboveOwnerCap(
            f"--budget-usd {budget_usd:g} is above the owner's ${DEFAULT_BUDGET_USD:g} cap; raising it is the owner's call: "
            "add --allow-over-owner-cap only on his word"
        )
    return budget_usd


def add_kit_arguments(parser: Any) -> None:
    """The kit's command-line flags, for the benches that use it."""
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD,
                        help="cumulative model spend cap across all kit runs (default and ceiling: 30; "
                             "a higher value needs --allow-over-owner-cap)")
    parser.add_argument("--allow-over-owner-cap", action="store_true",
                        help="allow --budget-usd above the owner's $30 cap (the owner's call, never on your own)")
    parser.add_argument("--timezone", default=None,
                        help="IANA zone for the passes (default: the account owner's, as the consolidation service reads it)")


@dataclass
class DryRunKit:
    """Installs the kit on a built `ConsolidationAgent` and reports what its passes did."""

    ledger_path: Path = SPEND_LEDGER
    budget_usd: float = DEFAULT_BUDGET_USD
    snapshot_dir: Path = SNAPSHOT_DIR
    timezone: str = "UTC"
    counters: RecordingCounters = field(default_factory=RecordingCounters)
    sink: StatsSink = field(default_factory=StatsSink)
    repo: Optional[NoWriteFactRepository] = None
    emails: Optional[NoWriteIndexedEmails] = None
    rows: List[Dict[str, Any]] = field(default_factory=list)
    passes: List[Dict[str, Any]] = field(default_factory=list)
    current_pass: str = ""
    with_review: bool = False       # the run also runs the review pass: its token, profile and blueprint come from the snapshot

    def __post_init__(self) -> None:
        self.meter = CostMeter(self.ledger_path, self.budget_usd)
        self._pass_calls = 0
        self._installed = False
        self._prepared = False
        self._service: Any = None
        self.served_tokens: List[Dict[str, str]] = []
        self.overrides_blocked: List[Dict[str, str]] = []
        self.review_tokens: List[Dict[str, str]] = []
        self.review_overrides_blocked: List[Dict[str, str]] = []
        self.recorder: Optional[WriteRecorder] = None
        self.review_world: Any = None

    def close(self) -> None:
        self.meter.close()

    def new_run(self) -> None:
        """Start a run on the live state again (a bench that replays one input needs every run to see the same store)."""
        if self.repo is not None:
            self.repo.forget_overlay()

    async def resolve_timezone(self, account_repo: Any, user_repo: Any, account_id: str, override: Optional[str] = None) -> str:
        self.timezone = await owner_timezone(account_repo, user_repo, account_id, override)
        return self.timezone

    def message_context(self, user_id: str, account_id: str, **extra: Any) -> Dict[str, Any]:
        """`AgentMessage.context` for a bench message: the ids, the owner's zone and anything else the bench adds."""
        return {"user_id": user_id, "account_id": account_id, "owner_timezone": self.timezone, **extra}

    def write_context(self, pass_label: str, span: Optional[Tuple[datetime, datetime]] = None) -> FactWriteContext:
        return FactWriteContext.for_pass(PassActor(pass_label), self.timezone, span)

    def install(self, agent: Any) -> None:
        """Put the kit under `agent`: its repository, its fact-management port, its counters, its alert sink, its model
        calls, its prompt tokens and (when it has one) its email repository."""
        if self._installed:
            raise RuntimeError("the kit is already installed")
        live = getattr(agent, "_fact_management", None)
        if live is None:
            raise ValueError("the agent has no fact-management port to run the kit on")
        if getattr(agent, "prompt_version", None) != "v3":
            raise ValueError(
                f"the kit runs the v3 passes only (agent prompt_version is {getattr(agent, 'prompt_version', None)!r}); "
                "the v2 path writes through the live write service"
            )
        service = getattr(getattr(agent, "prompt_builder", None), "assembly_service", None)
        if service is None:
            raise ValueError("the agent has no prompt assembly service: the snapshot tokens could not be served")
        self.repo = NoWriteFactRepository(agent._repo)
        agent._repo = self.repo
        write_service = FactWriteService(self.repo, live._embedding)
        agent._fact_write_service = write_service
        adapter = FactManagementAdapter(self.repo, live._embedding, write_service, live._search_enrichment)
        recorder = WriteRecorder(adapter, self)
        agent._fact_management = recorder
        self.recorder = recorder
        agent._fact_updates = recorder  # the typed path too: a review's writes must not reach the live store
        agent._lifecycle_counters = self.counters
        agent._alert_sink = self.sink
        agent._stats_alerts = True
        if getattr(agent, "_indexed_email_repo", None) is not None:
            self.emails = NoWriteIndexedEmails(agent._indexed_email_repo)
            agent._indexed_email_repo = self.emails
        token_ids = (*S3_TOKEN_IDS, *REVIEW_TOKEN_IDS) if self.with_review else S3_TOKEN_IDS
        service.token_repo = SnapshotTokenOverlay(service.token_repo, service.security_port, self.snapshot_dir, token_ids)
        if self.with_review:
            service.profile_repo = SnapshotProfileOverlay(service.profile_repo, self.snapshot_dir)
            service.blueprint_repo = SnapshotBlueprintOverlay(service.blueprint_repo, self.snapshot_dir)
        service.invalidate_cache()
        self._service = service
        self._wrap_calls(agent)
        self._wrap_passes(agent)
        self._wrap_review_cluster(agent)
        self._installed = True

    async def stage_stale(self, ids: Sequence[str], at: Optional[datetime] = None) -> List[str]:
        """Stale the current facts `ids` in the overlay, with the fields the janitor writes (`stale_fields`): the review's
        input, since the live janitor is log-only and stales nothing. The live store is untouched; the facts' own
        `expires_at` stays as stored (a fact with none is staged as due at `at`). A fact that is not current, or has no
        document, raises ValueError before anything is staged. `at` is when the janitor ran (default: now); use the
        review's clock. Returns the ids."""
        if self.repo is None:
            raise RuntimeError("install the kit before staging stale facts")
        moment = as_utc(at) if at is not None else datetime.now(timezone.utc)
        staged: List[Tuple[DueFact, Dict[str, Any]]] = []
        for fact_id in dict.fromkeys(ids):
            fact = await self.repo.get_fact_by_id(fact_id)
            if fact is None:
                raise ValueError(f"fact {fact_id} has no document: it cannot be staged stale")
            try:
                stored = await self.repo.resolve_current(fact_id, fact.account_id, frozenset({FactState.CURRENT}))
            except FactResolutionError:
                stored = None
            if stored is None or stored.fact.id != fact_id:
                raise ValueError(f"fact {fact_id} is not current: it cannot be staged stale")
            due = DueFact(
                fact_id=fact_id, account_id=fact.account_id, expires_at=stored.fact.expires_at or moment,
                expiry_kind=stored.fact.expiry_kind, update_time=stored.update_time,
                lifecycle_events=list(stored.fact.lifecycle_events),
            )
            staged.append((due, stale_fields(due, moment)))
        for due, fields in staged:
            await self.repo.stale_due_fact(due, fields)
        return [due.fact_id for due, _ in staged]

    def review_stand_ins(
        self, *, account_repo: Any, user_repo: Any, live_sessions: Any = None, clock: Any = None,
    ) -> Any:
        """The stand-ins around this kit's overlay store and write recorder for a review run (`review_kit_stand_ins`),
        with the real `FactAnswerService` over them. Their record joins the report's `review` section."""
        from scripts.consolidation.review_kit_stand_ins import build_review_stand_ins

        if self.repo is None or self.recorder is None:
            raise RuntimeError("install the kit before building the review's stand-ins")
        options = {} if clock is None else {"clock": clock}
        self.review_world = build_review_stand_ins(
            repo=self.repo, fact_updates=self.recorder, counters=self.counters, account_repo=account_repo,
            user_repo=user_repo, live_sessions=live_sessions, **options,
        )
        return self.review_world

    def current_in_overlay(self, doc: Optional[FactEntity]) -> Optional[FactEntity]:
        """`doc` followed through the overlay to the document that is current now; None when the run closed it for good
        (invalidated, stale, archived) or its successor is not in the overlay."""
        for _ in range(MAX_OVERLAY_HOPS):
            if doc is None or doc.state is FactState.CURRENT:
                return doc
            if doc.state is not FactState.SUPERSEDED or not doc.superseded_by or self.repo is None:
                return None
            doc = self.repo.overlay_document(doc.superseded_by)
        return None

    def correct_hits(self, hits: List[Dict[str, Any]], timezone: str) -> List[Dict[str, Any]]:
        """Search hits as production's search would show them after this run's writes: a hit the run closed is replaced by
        its current overlay successor (dropped when it has none), a hit the run changed shows the overlay document."""
        if self.repo is None:
            return hits
        out: List[Dict[str, Any]] = []
        seen: set = set()
        for hit in hits:
            doc = self.repo.overlay_document(hit["fact_id"])
            if doc is not None:
                doc = self.current_in_overlay(doc)
                if doc is None:
                    continue
                hit = hit_from_document(doc, hit, timezone)
            if hit["fact_id"] not in seen:
                seen.add(hit["fact_id"])
                out.append(hit)
        return out

    def _wrap_review_cluster(self, agent: Any) -> None:
        """Stage 2a's cluster is built from searches, which cannot see this run's writes; in production each fact the run
        wrote finds itself. Run production's own `_build_review_cluster` (same merge, same sort by length, same cut) over
        the searches with the run's facts, read back from the overlay, added as hits of their own content."""
        from types import SimpleNamespace

        async def review_cluster(changed: Any, account_id: str, *, timezone: str) -> List[Dict[str, Any]]:
            from src.agents.consolidation_agent import ConsolidationAgent

            own: Dict[str, List[Dict[str, Any]]] = {}
            for fact_id, content in changed:
                doc = self.current_in_overlay(self.repo.overlay_document(fact_id)) if self.repo else None
                if doc is not None:
                    own.setdefault(content, []).append({**ConsolidationAgent._cluster_entry(doc, timezone), "similarity": 1.0})
            shim = SimpleNamespace(_fact_management=OwnFactsSearch(agent._fact_management, own))
            return await ConsolidationAgent._build_review_cluster(shim, changed, account_id, timezone=timezone)

        agent._build_review_cluster = review_cluster

    def _wrap_calls(self, agent: Any) -> None:
        self.meter.wrap(agent)
        metered = agent._call_llm

        async def counted(request: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._prepared:
                raise KitNotPrepared("await kit.prepare(user_id, account_id) before the first model call")
            response = await metered(request, *args, **kwargs)
            self._pass_calls += 1
            return response

        agent._call_llm = counted

    async def _serve(self, token_ids: Sequence[str]) -> Dict[str, Token]:
        """The tokens through the overlay; one that cannot be served (a missing file, an id mismatch, a security refusal)
        raises here instead of the assembly service dropping it from the prompt."""
        served: Dict[str, Token] = {}
        for token_id in token_ids:
            try:
                served[token_id] = await self._service.token_repo.get(TokenId(token_id))
            except Exception as exc:
                raise SnapshotTokenError(f"snapshot token {token_id} cannot be served: {type(exc).__name__}: {exc}") from exc
        return served

    @staticmethod
    def _require_listed(agent_id: str, profile: Any, token_ids: Sequence[str]) -> None:
        absent = [token_id for token_id in token_ids if token_id not in profile.tokens]
        if absent:
            raise SnapshotTokenError(f"the {agent_id} profile does not list {', '.join(absent)}: the prompt would lack them")

    async def _overrides_checked(
        self, profile: Any, served: Dict[str, Token], user_id: str, account_id: str,
    ) -> List[Dict[str, str]]:
        """The account and user overrides that would replace a served token: refused, unless the profile's
        `non_overridable` flag blocks them in production (those are returned for the report)."""
        overlay = self._service.token_repo
        blocked: List[Dict[str, str]] = []
        for level, owner_id in ((OwnerType.ACCOUNT, account_id), (OwnerType.USER, user_id)):
            if not owner_id:
                continue
            for override_id in await self._service.profile_repo.get_override_tokens(level, owner_id):
                try:
                    other = await overlay.get(TokenId(override_id))
                except KeyError:
                    continue
                for token_id, token in served.items():
                    if override_id == token_id or (token.class_, token.category) != (other.class_, other.category):
                        continue
                    entry = {"level": level.value, "override": str(override_id), "replaces": token_id}
                    if profile.tokens[token_id].non_overridable:
                        blocked.append(entry)
                    else:
                        raise SnapshotTokenError(
                            f"a {level.value} override ({override_id}) would replace snapshot token {token_id}: "
                            "the dry run would not test the snapshot text"
                        )
        return blocked

    @staticmethod
    def _digests(served: Dict[str, Token]) -> List[Dict[str, str]]:
        return [
            {"id": token_id, "sha256": hashlib.sha256(token.content.encode("utf-8")).hexdigest()}
            for token_id, token in served.items()
        ]

    async def _prepare_review(self, user_id: str, account_id: str) -> None:
        """The same check for the review pass: its token serves, its profile (from the snapshot) lists the taxonomy and the
        lifecycle token, its blueprint has a section for each (a token whose class the blueprint does not list is not
        rendered), and no override replaces either."""
        served = await self._serve(REVIEW_PROFILE_TOKEN_IDS)
        profile = await self._service.profile_repo.get_agent_profile(REVIEW_PROFILE_ID)
        self._require_listed(REVIEW_PROFILE_ID, profile, REVIEW_PROFILE_TOKEN_IDS)
        try:
            blueprint = await self._service.blueprint_repo.get(profile.blueprint_id)
        except Exception as exc:
            raise SnapshotTokenError(
                f"the {REVIEW_PROFILE_ID} blueprint {profile.blueprint_id} cannot be read: {type(exc).__name__}: {exc}"
            ) from exc
        unplaced = [token_id for token_id, token in served.items() if str(token.class_) not in blueprint.class_order]
        if unplaced:
            raise SnapshotTokenError(
                f"the blueprint {profile.blueprint_id} has no section for {', '.join(unplaced)}: the review prompt would lack them"
            )
        self.review_overrides_blocked = await self._overrides_checked(profile, served, user_id, account_id)
        self.review_tokens = self._digests({token_id: served[token_id] for token_id in REVIEW_TOKEN_IDS})

    async def prepare(self, user_id: str, account_id: str) -> None:
        """Check, before any model call, that the prompt will carry the snapshot text of the five S3 tokens.

        All five load through the overlay (a missing file, an id mismatch or a security refusal raises here instead of the
        assembly service dropping the token), the agent's profile must list each, and no account or user override may
        replace one (the profile's `non_overridable` flag blocks an override in production; those are recorded). The tokens
        served (id and sha256 of the body) go into the report. A kit built `with_review` makes the same check for the review
        pass (`_prepare_review`): its lifecycle token, profile and blueprint come from the snapshot."""
        served = await self._serve(S3_TOKEN_IDS)
        profile = await self._service.profile_repo.get_agent_profile("consolidation")
        self._require_listed("consolidation", profile, S3_TOKEN_IDS)
        self.overrides_blocked = await self._overrides_checked(profile, served, user_id, account_id)
        self.served_tokens = self._digests(served)
        if self.with_review:
            await self._prepare_review(user_id, account_id)
        self._prepared = True

    def _wrap_passes(self, agent: Any) -> None:
        loop, close = agent._run_consolidation_loop, agent._report_pass

        async def run_loop(*args: Any, **kwargs: Any) -> Any:
            label = kwargs["write_context"].actor.value
            previous = (self.current_pass, self._pass_calls)
            self.current_pass, self._pass_calls = label, 0
            try:
                return await loop(*args, **kwargs)
            finally:
                self.current_pass, self._pass_calls = previous

        async def report_pass(stats: Any, account_id: str, user_id: str) -> None:
            from src.agents.consolidation_agent import _sequence_text

            self.passes.append({
                "pass": stats.actor.value,
                "model": agent.model_name,
                "llm_calls": self._pass_calls,
                "max_turns": stats.max_turns,
                "tool_turns": stats.turns_used,
                "final_report": self._pass_calls > stats.turns_used,
                "tool_error_turns": stats.error_turns,
                "expiry_refusals": stats.expiry_errors,
                "born_due": stats.born_due,
                "abandoned_refusals": stats.abandoned,
                "writes": stats.writes,
                "repeats": stats.repeats,
                "sequence": _sequence_text(stats),
            })
            await close(stats, account_id, user_id)

        agent._run_consolidation_loop = run_loop
        agent._report_pass = report_pass

    async def logged(
        self, action: str, context: FactWriteContext, expiry_sent: Any, reported_sent: Any, id_key: str, call: Any,
        derived_from: Any = None,
    ) -> Dict[str, Any]:
        """Run one write call and add its report row. The row's place is taken before the call, so rows keep the order
        the model emitted them in even when a turn's calls run concurrently."""
        row = self._open_row(action, context, expiry_sent, reported_sent, derived_from)
        result = await call
        written_id = result.get(id_key)
        written = result.get("status") in WRITTEN_STATUSES
        await self._close_row(
            row, result.get("status"), result.get("refused"), written_id, written, result.get("note"),
            _holds_note(result.get("note"), NO_READABLE_SOURCE_NOTE),
        )
        return result

    async def logged_typed(
        self, context: FactWriteContext, expiry_sent: Any, reported_sent: Any, call: Any,
    ) -> FactUpdateOutcome:
        """`logged` for a typed update: what was written and the notes' codes come from the typed outcome; the row's
        status, `refused` and notes are in the words of the model's dict (`update_result`), so both kinds of row read
        alike in the report."""
        row = self._open_row("update", context, expiry_sent, reported_sent, None)
        outcome: FactUpdateOutcome = await call
        shown = update_result(outcome)
        await self._close_row(
            row, shown["status"], shown.get("refused"), outcome.written_id if outcome.written else outcome.fact_id,
            outcome.written, shown.get("note"), NO_READABLE_SOURCE_NOTE in outcome.notes,
        )
        return outcome

    def _open_row(
        self, action: str, context: FactWriteContext, expiry_sent: Any, reported_sent: Any, derived_from: Any,
    ) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "pass": self.current_pass or context.actor,
            "actor": context.actor,
            "action": action,
            "status": "raised",
            "expiry_sent": expiry_sent,
            "reported_date_sent": reported_sent,
            "reported_date_form": reported_date_form(reported_sent),
            "cleanup": context.floors_due_horizon,
            "derived_from_named": derived_from_count(derived_from),
            "derived_from_unreadable": False,
        }
        self.rows.append(row)
        return row

    async def _close_row(
        self, row: Dict[str, Any], status: Any, refused: Any, written_id: Any, written: bool, notes: Any,
        derived_from_unreadable: bool,
    ) -> None:
        stored = None
        if written_id and self.repo is not None and written:
            stored = await self.repo.get_fact_by_id(written_id)
        row.update({
            "status": status,
            "refused": refused,
            "fact_id": written_id,
            "decision_stored": None if stored is None else {
                "expiry_kind": getattr(stored.expiry_kind, "value", stored.expiry_kind),
                "ttl_days": stored.ttl_days,
                "expires_at": stored.expires_at.isoformat() if stored.expires_at else None,
            },
            "reported_date_chosen": None if stored is None else stored.reported_date.isoformat(),
            "state_stored": None if stored is None else stored.state.value,
            "notes": notes,
            "derived_from_unreadable": derived_from_unreadable,
        })

    def report(self) -> Dict[str, Any]:
        forms: Dict[str, Counter] = {}
        for row in self.rows:
            if row["reported_date_form"]:
                forms.setdefault(row["pass"], Counter())[row["reported_date_form"]] += 1
        total: Counter = Counter()
        for counted in forms.values():
            total.update(counted)
        undated: Counter = Counter(
            row["pass"] for row in self.rows
            if row["action"] == "create" and row["status"] == STATUS_CREATED and row["cleanup"]
            and (not row["derived_from_named"] or row["derived_from_unreadable"])
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "timezone": self.timezone,
            "spend": {
                "run_usd": self.meter.run_usd,
                "total_usd": self.meter.spent_usd,
                "budget_usd": self.meter.budget_usd,
                "calls": self.meter.calls,
                "unmetered_calls": self.meter.unmetered_calls,
                "aborted": self.meter.aborted,
            },
            "served_tokens": self.served_tokens,
            "overrides_blocked": self.overrides_blocked,
            "review_tokens": self.review_tokens,
            "review_overrides_blocked": self.review_overrides_blocked,
            "passes": self.passes,
            "reported_date_forms": {"per_pass": {k: dict(v) for k, v in forms.items()}, "total": dict(total)},
            "cleanup_creates_without_derived_from": {"per_pass": dict(undated), "total": sum(undated.values())},
            "writes": self.rows,
            "store_writes": dict(Counter(w["kind"] for w in (self.repo.writes if self.repo else []))),
            "email_writes": dict(Counter(w["kind"] for w in (self.emails.writes if self.emails else []))),
            "counters": {"expiry_tool_errors": self.counters.expiry_tool_errors},
            "alerts": self.sink.posts,
            "review": self.review_world.report() if self.review_world is not None else None,
        }

    def write_report(self, path: Path = DEFAULT_REPORT) -> Path:
        path = inside_memory_dir(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.report(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path
