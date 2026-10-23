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

Residual (not hidden by the kit): the agent's own `_call_llm` still writes `prompt_content` and billing usage, as it does
in every bench. Vector searches and the paginated / lineage / domain reads see the live store only.

Files written by the kit (spend ledger, report) go to `scripts/memory/` (gitignored). Nothing here builds a client at
import time.
"""

import itertools
import json
import os
from collections import Counter
from dataclasses import dataclass, field, replace
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
from src.domain.exceptions import FactResolutionError, FactWriteConflict, InPlaceFieldRefused
from src.domain.fact_lifecycle import (
    CABINET_ACCEPTS,
    IN_PLACE_FIELDS,
    closure_fields,
    forward_step,
    lifecycle_event,
    lifecycle_fields,
    with_event,
)
from src.domain.fact_lifecycle_day import COUNTER_FIELDS, FactLifecycleDay, check_snapshot
from src.domain.fact_lifecycle_sweep import stale_fields
from src.domain.fact_write_context import FactWriteContext
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.domain.stored_fact import StoredFact
from src.ports.alert_sink import AlertSinkPort
from src.ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from src.ports.fact_management_port import FactManagementPort
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.prompt_v3.token_repository import TokenRepository
from src.ports.repository import FactRepository
from src.utils.logger import logger

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = PROJECT_ROOT / "scripts" / "memory"
SPEND_LEDGER = MEMORY_DIR / "s3_dry_run_spend.json"
DEFAULT_REPORT = MEMORY_DIR / "s3_dry_run_report.json"
SNAPSHOT_DIR = PROJECT_ROOT / "prompts_snapshot"
DEFAULT_BUDGET_USD = 30.0

# The tokens S3 edits (the sixth, CONSOLIDATION_OUTPUT_SPEC, is unchanged and still comes from Firestore).
S3_TOKEN_IDS: Tuple[str, ...] = (
    "CONSOLIDATION_COGNITIVE_PROCESS",
    "CONSOLIDATION_EXAMPLES",
    "CONSOLIDATION_POLICIES",
    "CONSOLIDATION_TAXONOMY",
    "CONSOLIDATION_TOOLS",
)

_ALL_STATES: AbstractSet[FactState] = frozenset(FactState)
_PRIORITY_RANK = {"critical": 1, "high": 2, "medium": 3, "low": 4, "archival": 5}
_VECTORS = ("vector", "tags_vector", "metadata_vector")


class BudgetExceeded(RuntimeError):
    """The run stops before the next model call: its spend (or its unpriced model) would break the cap."""


class UnpricedModel(BudgetExceeded):
    """The model has no price in `domain.billing`, so the meter could not keep the cap."""


# ------------------------------------------------------------------------------------------------ the fact store

class NoWriteFactRepository(FactRepository):
    """The live fact repository for reads; every write goes to an in-memory overlay and is recorded in `writes`.

    Documents and change tokens of the overlay stand in for the store's: a precondition is checked against the overlay
    token (a synthetic counter per written document) or, for a document the pass has not written, the live
    `update_time`. Vector searches, the paginated, lineage and domain reads, `find_due_facts` and `stale_backlog` see the
    live store only; `get_fact_by_id`, `resolve_current` and the three current-fact lists see the overlay too.
    """

    def __init__(self, real: FactRepository) -> None:
        self._real = real
        self._docs: Dict[str, FactEntity] = {}
        self._tokens: Dict[str, Tuple[str, int]] = {}
        self._clock = itertools.count(1)
        self.writes: List[Dict[str, Any]] = []

    def _record(self, kind: str, **info: Any) -> None:
        self.writes.append({"kind": kind, **info})

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
        self._put(fact)
        self._record("add_fact", fact_id=fact.id)
        return fact.id

    async def get_fact_by_id(self, fact_id: str) -> Optional[FactEntity]:
        doc = self._docs.get(fact_id)
        if doc is not None:
            return doc.model_copy(deep=True)
        return await self._real.get_fact_by_id(fact_id)

    async def resolve_current(
        self, fact_id: str, account_id: str, accepted: AbstractSet[FactState]
    ) -> Optional[StoredFact]:
        stored = await self._read(fact_id, account_id)
        if stored is None or stored.fact.account_id != account_id:
            return None
        origin_lineage_id = stored.fact.lineage_id
        hops = 0
        while True:
            next_id = forward_step(stored.fact, accepted, origin_lineage_id, fact_id, account_id, hops)
            if next_id is None:
                return stored
            hops += 1
            following = await self._read(next_id, account_id)
            if following is None:
                raise FactResolutionError(f"fact {fact_id} was superseded and its successor is unknown; search again")
            stored = following

    async def write_superseding(
        self, new_fact: FactEntity, replaced: Sequence[StoredFact], now: datetime, *, actor: str
    ) -> None:
        if not replaced:
            raise ValueError(f"write_superseding of {new_fact.id} replaces no document; use add_fact")
        missing = [name for name in _VECTORS if getattr(new_fact, name) is None]
        if missing:
            raise ValueError(f"fact {new_fact.id} has no {', '.join(missing)}; a version without vectors leaves vector search")
        for stored in replaced:
            await self._check_token(stored.fact.id, stored.fact.account_id, stored.update_time)
        if new_fact.id in self._docs or await self._real.get_fact_by_id(new_fact.id) is not None:
            raise ValueError(f"fact {new_fact.id} already exists")
        closed: List[FactEntity] = []
        for stored in replaced:
            merged = stored.fact.lineage_id != new_fact.lineage_id
            reason = f"{'merged into' if merged else 'replaced by'} {new_fact.id}"
            entry = lifecycle_event(now, stored.fact.state, FactState.SUPERSEDED, actor, reason)
            fields = {**closure_fields(new_fact.id, now), "lifecycle_events": with_event(stored.fact.lifecycle_events, entry)}
            closed.append(stored.fact.model_copy(update=fields))
        self._put(new_fact)
        for fact in closed:
            self._put(fact)
        self._record("write_superseding", new_id=new_fact.id, replaced=[s.fact.id for s in replaced], actor=actor)

    async def update_fact_fields(self, stored: StoredFact, fields: Mapping[str, Any]) -> None:
        refused = set(fields) - IN_PLACE_FIELDS
        if refused:
            raise InPlaceFieldRefused(refused)
        await self._apply_fields(stored, fields)
        self._record("update_fact_fields", fact_id=stored.fact.id, fields=sorted(fields))

    async def _apply_fields(self, stored: StoredFact, fields: Mapping[str, Any]) -> None:
        await self._check_token(stored.fact.id, stored.fact.account_id, stored.update_time)
        base = self._base(stored)
        self._put(FactEntity.model_validate({**base.model_dump(), **fields}))

    async def invalidate_fact(self, fact_id: str, account_id: str) -> None:
        shown = await self.get_fact_by_id(fact_id)
        if shown is None:
            raise ValueError(f"Fact not found: {fact_id}")
        if shown.account_id != account_id:
            raise PermissionError("Not authorized to modify this fact")
        for attempt in (1, 2):
            stored = await self.resolve_current(fact_id, account_id, CABINET_ACCEPTS)
            if stored is None:
                raise ValueError(f"Fact not found: {fact_id}")
            if stored.fact.id != fact_id and stored.fact.text != shown.text:
                raise FactResolutionError(
                    f"fact {fact_id} was rewritten after the page loaded; reload", current_id=stored.fact.id,
                )
            now = datetime.now(timezone.utc)
            event = lifecycle_event(now, stored.fact.state, FactState.INVALIDATED, "cabinet", "deleted in the Cabinet")
            fields = {
                **lifecycle_fields(FactState.INVALIDATED, now),
                "last_updated": now,
                "lifecycle_events": with_event(stored.fact.lifecycle_events, event),
            }
            try:
                await self._apply_fields(stored, fields)
            except FactWriteConflict:
                if attempt == 2:
                    raise
                continue
            self._record("invalidate_fact", fact_id=stored.fact.id)
            return

    async def stale_due_fact(self, due: DueFact, now: datetime) -> None:
        await self._check_token(due.fact_id, due.account_id, due.update_time)
        base = self._docs.get(due.fact_id) or await self._real.get_fact_by_id(due.fact_id)
        if base is None:
            raise FactWriteConflict(due.fact_id)
        fields = stale_fields(replace(due, lifecycle_events=list(base.lifecycle_events)), now)
        self._put(FactEntity.model_validate({**base.model_dump(), **fields}))
        self._record("stale_due_fact", fact_id=due.fact_id)

    async def add_fact_if_unique(
        self, fact: FactEntity, similarity_threshold: float = 0.85
    ) -> tuple[bool, Optional[str]]:
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
        self, owner_id: str, limit: int = 100, cursor_doc_id: Optional[str] = None, domain: Optional[str] = None
    ) -> Tuple[List[FactEntity], Optional[str]]:
        return await self._real.get_paginated_facts(owner_id, limit, cursor_doc_id, domain)

    async def search_facts(
        self, query_vector: List[float], vector_field: str = "vector", limit: int = 5,
        user_id: Optional[str] = None, account_id: Optional[str] = None,
    ) -> List[FactEntity]:
        return await self._real.search_facts(query_vector, vector_field, limit, user_id, account_id)

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
        self.writes: List[Dict[str, Any]] = []

    async def get_unconsolidated_batch(self, user_id: str, limit: int = 200) -> List[IndexedEmail]:
        return await self._real.get_unconsolidated_batch(user_id, limit)

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
        self.writes.append({"kind": "mark_consolidated", "user_id": user_id, "email_ids": list(email_ids)})

    async def save_batch(self, emails: List[IndexedEmail]) -> int:
        self.writes.append({"kind": "save_batch", "count": len(emails)})
        return len(emails)

    async def update_indexing_state(self, state: IndexingState) -> None:
        self.writes.append({"kind": "update_indexing_state"})

    async def clear_indexing_state(self, user_id: str, provider: str) -> None:
        self.writes.append({"kind": "clear_indexing_state"})

    async def delete_by_user(self, user_id: str) -> None:
        self.writes.append({"kind": "delete_by_user"})

    async def update_vectors(self, user_id: str, email_id: str, vectors: Dict[str, List[float]]) -> None:
        self.writes.append({"kind": "update_vectors", "email_id": email_id})


# ------------------------------------------------------------------------------------------------ counters and alerts

class RecordingCounters(FactLifecycleCountersPort):
    """Stands in for the live lifecycle-counters store: it validates as the production store does and keeps what it is given."""

    def __init__(self) -> None:
        self.adds: List[Tuple[str, date, Dict[str, int]]] = []
        self.sweeps: List[Tuple[str, date, Dict[str, Any]]] = []
        self._claimed: set = set()

    @staticmethod
    def _check(account_id: str, day: date) -> None:
        if not account_id:
            raise ValueError("account_id is required for the fact lifecycle counters")
        if isinstance(day, datetime):
            raise ValueError("day must be a date, not a datetime")

    @property
    def expiry_tool_errors(self) -> int:
        return sum(counts.get("expiry_tool_errors", 0) for _, _, counts in self.adds)

    async def record_sweep(self, account_id: str, day: date, values: Mapping[str, Any]) -> None:
        self._check(account_id, day)
        check_snapshot(values)
        self.sweeps.append((account_id, day, dict(values)))

    async def add(self, account_id: str, day: date, counts: Mapping[str, int]) -> None:
        self._check(account_id, day)
        unknown = sorted(set(counts) - COUNTER_FIELDS)
        if unknown:
            raise ValueError(f"not counter fields: {unknown}")
        for name, value in counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"count {name} must be a non-negative integer")
        added = {name: value for name, value in counts.items() if value}
        if added:
            self.adds.append((account_id, day, added))

    async def for_day(self, day: date) -> List[FactLifecycleDay]:
        totals: Dict[str, int] = Counter()
        for account_id, added_day, counts in self.adds:
            if added_day == day:
                totals[account_id] += counts.get("expiry_tool_errors", 0)
        return [FactLifecycleDay(account_id=a, day=day, expiry_tool_errors=n) for a, n in sorted(totals.items())]

    async def claim_line(self, day: date) -> bool:
        first = day not in self._claimed
        self._claimed.add(day)
        return first


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


# ------------------------------------------------------------------------------------------------ cost

class CostMeter:
    """Prices each model call and keeps a cumulative ledger across runs.

    Before a call it refuses (BudgetExceeded) when the spend so far has reached the cap or when the largest single call
    seen so far would pass it; a model without a price is refused too, since its cost could not be kept under the cap.
    """

    def __init__(self, ledger_path: Path = SPEND_LEDGER, budget_usd: float = DEFAULT_BUDGET_USD) -> None:
        self.ledger_path = Path(ledger_path)
        self.budget_usd = float(budget_usd)
        self.aborted = False
        self._ledger = self._load()
        self._run: Optional[Dict[str, Any]] = None

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
    """The account owner's IANA zone, resolved as `ConsolidationService._owner_timezone` does (first owner's profile;
    any missing step or failure gives UTC with one warning). `override` (`--timezone`) wins and must be a zone."""
    if override:
        try:
            ZoneInfo(override)
        except (KeyError, ValueError, OSError) as exc:
            raise ValueError(f"--timezone {override!r} is not an IANA zone") from exc
        return override
    try:
        account = await account_repo.get_account(account_id)
        if account is None:
            return _unresolved(account_id, "account not found")
        owners = account.owner_user_ids()
        if not owners:
            return _unresolved(account_id, "account has no owner")
        owner = await user_repo.get_user(owners[0])
        if owner is None:
            return _unresolved(account_id, "owner has no profile")
        zone = owner.config.timezone
        ZoneInfo(zone)
        return zone
    except Exception as exc:
        return _unresolved(account_id, type(exc).__name__)


def _unresolved(account_id: str, why: str) -> str:
    logger.warning("[S3Kit] owner timezone unresolved for account %s: %s; using UTC", account_id[:8], why)
    return "UTC"


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


# ------------------------------------------------------------------------------------------------ the write recorder

class WriteRecorder(FactManagementPort):
    """Wraps the real adapter (built on the overlay): one report row per create, update and merge call."""

    def __init__(self, inner: FactManagementPort, kit: "DryRunKit") -> None:
        self._inner = inner
        self._kit = kit

    async def search_existing_facts(
        self, keywords: List[str], primary_query: str, alternative_query: str = "", limit: int = 20, *, timezone: str
    ) -> List[Dict[str, Any]]:
        return await self._inner.search_existing_facts(keywords, primary_query, alternative_query, limit, timezone=timezone)

    async def create_fact(self, content: str, metadata: Dict[str, Any], *, context: FactWriteContext) -> Dict[str, Any]:
        return await self._kit.logged(
            "create", context, metadata.get("expiry"), metadata.get("reported_date"), "fact_id",
            self._inner.create_fact(content, metadata, context=context),
        )

    async def update_fact(
        self, fact_id: str, updates: Dict[str, Any], account_id: str, *, context: FactWriteContext
    ) -> Dict[str, Any]:
        return await self._kit.logged(
            "update", context, updates.get("expiry"), updates.get("reported_date"), "fact_id",
            self._inner.update_fact(fact_id, updates, account_id, context=context),
        )

    async def merge_facts(
        self, fact_ids: List[str], merged_content: str, metadata: Dict[str, Any], *, context: FactWriteContext
    ) -> Dict[str, Any]:
        return await self._kit.logged(
            "merge", context, metadata.get("expiry"), metadata.get("reported_date"), "new_fact_id",
            self._inner.merge_facts(fact_ids, merged_content, metadata, context=context),
        )


# ------------------------------------------------------------------------------------------------ the kit

def add_kit_arguments(parser: Any) -> None:
    """The kit's command-line flags, for the benches that use it."""
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD,
                        help="cumulative model spend cap across all kit runs (default: 30)")
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

    def __post_init__(self) -> None:
        self.meter = CostMeter(self.ledger_path, self.budget_usd)
        self._pass_calls = 0
        self._installed = False

    async def resolve_timezone(self, account_repo: Any, user_repo: Any, account_id: str, override: Optional[str] = None) -> str:
        self.timezone = await owner_timezone(account_repo, user_repo, account_id, override)
        return self.timezone

    def message_context(self, user_id: str, account_id: str, **extra: Any) -> Dict[str, Any]:
        """`AgentMessage.context` for a bench message: the ids, the owner's zone and anything else the bench adds."""
        return {"user_id": user_id, "account_id": account_id, "owner_timezone": self.timezone, **extra}

    def write_context(self, pass_label: str, span: Optional[Tuple[datetime, datetime]] = None) -> FactWriteContext:
        from src.agents.consolidation_agent import ConsolidationAgent

        return ConsolidationAgent._write_context(pass_label, self.timezone, span)

    def install(self, agent: Any) -> None:
        """Put the kit under `agent`: its repository, its fact-management port, its counters, its alert sink, its model
        calls, its prompt tokens and (when it has one) its email repository."""
        if self._installed:
            raise RuntimeError("the kit is already installed")
        live = getattr(agent, "_fact_management", None)
        if live is None:
            raise ValueError("the agent has no fact-management port to run the kit on")
        self.repo = NoWriteFactRepository(agent._repo)
        agent._repo = self.repo
        adapter = FactManagementAdapter(self.repo, live._embedding, live._fact_write_service, live._search_enrichment)
        agent._fact_management = WriteRecorder(adapter, self)
        agent._lifecycle_counters = self.counters
        agent._alert_sink = self.sink
        agent._stats_alerts = True
        if getattr(agent, "_indexed_email_repo", None) is not None:
            self.emails = NoWriteIndexedEmails(agent._indexed_email_repo)
            agent._indexed_email_repo = self.emails
        service = getattr(getattr(agent, "prompt_builder", None), "assembly_service", None)
        if service is not None:
            service.token_repo = SnapshotTokenOverlay(service.token_repo, service.security_port, self.snapshot_dir)
            service.invalidate_cache()
        self._wrap_calls(agent)
        self._wrap_passes(agent)
        self._installed = True

    def _wrap_calls(self, agent: Any) -> None:
        self.meter.wrap(agent)
        metered = agent._call_llm

        async def counted(request: Any, *args: Any, **kwargs: Any) -> Any:
            response = await metered(request, *args, **kwargs)
            self._pass_calls += 1
            return response

        agent._call_llm = counted

    def _wrap_passes(self, agent: Any) -> None:
        loop, close = agent._run_consolidation_loop, agent._report_pass

        async def run_loop(*args: Any, **kwargs: Any) -> Any:
            label = kwargs.get("pass_label") or (args[4] if len(args) > 4 else "consolidation")
            previous = (self.current_pass, self._pass_calls)
            self.current_pass, self._pass_calls = label, 0
            try:
                return await loop(*args, **kwargs)
            finally:
                self.current_pass, self._pass_calls = previous

        async def report_pass(stats: Any, account_id: str) -> None:
            self.passes.append({
                "pass": stats.pass_label,
                "model": stats.model,
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
                "sequence": stats.sequence(),
            })
            await close(stats, account_id)

        agent._run_consolidation_loop = run_loop
        agent._report_pass = report_pass

    async def logged(
        self, action: str, context: FactWriteContext, expiry_sent: Any, reported_sent: Any, id_key: str, call: Any
    ) -> Dict[str, Any]:
        """Run one write call and add its report row. The row's place is taken before the call, so rows keep the order
        the model emitted them in even when a turn's calls run concurrently."""
        row: Dict[str, Any] = {
            "pass": self.current_pass or context.actor,
            "actor": context.actor,
            "action": action,
            "status": "raised",
            "expiry_sent": expiry_sent,
            "reported_date_sent": reported_sent,
            "reported_date_form": reported_date_form(reported_sent),
        }
        self.rows.append(row)
        result = await call
        written_id = result.get(id_key)
        stored = None
        if written_id and self.repo is not None and result.get("status") in ("created", "updated", "merged"):
            stored = await self.repo.get_fact_by_id(written_id)
        row.update({
            "status": result.get("status"),
            "refused": result.get("refused"),
            "fact_id": written_id,
            "decision_stored": None if stored is None else {
                "expiry_kind": getattr(stored.expiry_kind, "value", stored.expiry_kind),
                "ttl_days": stored.ttl_days,
                "expires_at": stored.expires_at.isoformat() if stored.expires_at else None,
            },
            "reported_date_chosen": None if stored is None else stored.reported_date.isoformat(),
            "state_stored": None if stored is None else stored.state.value,
            "notes": result.get("note"),
        })
        return result

    def report(self) -> Dict[str, Any]:
        forms: Dict[str, Counter] = {}
        for row in self.rows:
            if row["reported_date_form"]:
                forms.setdefault(row["pass"], Counter())[row["reported_date_form"]] += 1
        total: Counter = Counter()
        for counted in forms.values():
            total.update(counted)
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
            "passes": self.passes,
            "reported_date_forms": {"per_pass": {k: dict(v) for k, v in forms.items()}, "total": dict(total)},
            "writes": self.rows,
            "store_writes": dict(Counter(w["kind"] for w in (self.repo.writes if self.repo else []))),
            "email_writes": dict(Counter(w["kind"] for w in (self.emails.writes if self.emails else []))),
            "counters": {"expiry_tool_errors": self.counters.expiry_tool_errors},
            "alerts": self.sink.posts,
        }

    def write_report(self, path: Path = DEFAULT_REPORT) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.report(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path
