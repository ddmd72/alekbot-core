"""
FactProfileService — the profile step of the memory-write lease (docs/10_rfcs/FACT_PROFILE_RFC.md §4.2, §4.4).

Public interface:
    FactProfileService(repo, profiler, embeddings, atlas, max_per_run, concurrency, call_budget_s, clock)
        async scan(account_id) -> FactProfileScan                 what a run would find; reads, writes nothing
        async run(account_id, deadline) -> FactProfileRun         never raises; `deadline` is the lease's `until`

One run, inside a lease the caller holds, reads the account's current facts once and brings every profile up to date:
  1. classify each fact against its content and the code inputs (registry, atlas, directive or not);
  2. free work: a profile whose only stale part is code is re-derived from its stored answer; a standing directive gets
     its fixed profile (no call); a failed fact is not retried once abandoned;
  3. model calls for the rest (newest first, at most `max_per_run`, `concurrency` at a time, each started only while the
     lease leaves `call_budget_s`); the first `unavailable` reply stops new calls: an outage is nobody's failure;
  4. one embedding batch for the label texts, then one compare-and-set write per fact (a fact that changed meanwhile is
     left for the next cycle), stopping when the lease has no time left;
  5. one baseline refresh when any profile was written, so the search snapshot (keyed by the baseline) sees it.
The service never touches the lease. Logs carry ids (8 characters), counts, reasons, ms and usd, never fact content.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from ..domain.entities import FactDomain, FactEntity
from ..domain.fact_profile import FactProfile
from ..domain.fact_profile_answer import ProfileAnswer
from ..domain.fact_profile_derive import DeriveOutcome, derive_profile, directive_profile
from ..domain.fact_profile_fingerprint import (
    ABANDON_AFTER_FAILURES,
    ProfileStatus,
    classify_profile,
    failure_fields,
    profile_derivation,
    profile_fingerprint,
    registry_digest,
)
from ..domain.fact_profile_run import FactProfileRun, FactProfileScan
from ..domain.fact_profiler_reply import FactProfilerReply, ProfilerStatus
from ..domain.memory_write_lease import agent_timeout_s
from ..domain.place_names import PlaceNames
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_profiler_port import FactProfilerPort
from ..ports.repository import FactRepository
from ..utils.logger import logger

SYSTEM_ACCOUNT = "SYSTEM"
Atlas = Callable[[], Awaitable[Tuple[PlaceNames, str]]]

_WRITTEN, _REDERIVED, _DIRECTIVE, _FAILED = "written", "rederived", "directive", "failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_directive(fact: FactEntity) -> bool:
    """A standing directive. Until the consolidator marks them itself, the standing-directive channel is the domain."""
    return fact.domain == FactDomain.AGENT_DIRECTIVE


def _stored_answer(fact: FactEntity) -> Optional[ProfileAnswer]:
    """The stored model answer when it still validates against the current schema."""
    if not fact.profile_answer:
        return None
    try:
        return ProfileAnswer.model_validate(fact.profile_answer)
    except ValidationError:
        return None


def _drops_text(drops: Dict[str, int]) -> str:
    return ",".join(f"{reason}:{n}" for reason, n in sorted(drops.items())) or "none"


@dataclass
class _Item:
    fact: FactEntity
    status: ProfileStatus
    is_directive: bool
    derivation: str
    answer: Optional[ProfileAnswer]

    @property
    def needs_call(self) -> bool:
        if self.is_directive or self.status in (ProfileStatus.FRESH, ProfileStatus.ABANDONED):
            return False
        return self.status is not ProfileStatus.STALE_DERIVATION or self.answer is None


@dataclass
class _Pending:
    """One write decided, not yet made."""
    fact: FactEntity
    kind: str
    fields: Dict[str, Any]
    label_text: Optional[str] = None
    drops: Dict[str, int] = field(default_factory=dict)
    ms: int = 0
    usd: float = 0.0
    abandoned: bool = False
    outcome: str = "written"   # the log word of a stored profile: written, or rederived


@dataclass
class _Called:
    item: _Item
    reply: FactProfilerReply
    ms: int


@dataclass
class _Tally:
    scan: FactProfileScan = field(default_factory=FactProfileScan)
    rederived: int = 0
    directives: int = 0
    called: int = 0
    written: int = 0
    failed: int = 0
    newly_abandoned: int = 0
    deferred: int = 0
    lost_cas: int = 0
    usd: float = 0.0
    drops: Dict[str, int] = field(default_factory=dict)
    outage: bool = False
    refreshed: bool = False
    error: bool = False

    def freeze(self) -> FactProfileRun:
        return FactProfileRun(
            scan=self.scan, rederived=self.rederived, directives=self.directives, called=self.called,
            written=self.written, failed=self.failed, newly_abandoned=self.newly_abandoned, deferred=self.deferred,
            lost_cas=self.lost_cas, usd=self.usd, drops=dict(self.drops), outage=self.outage,
            refreshed=self.refreshed, error=self.error)


class FactProfileService:

    def __init__(
        self,
        repo: FactRepository,
        profiler: FactProfilerPort,
        embeddings: EmbeddingService,
        atlas: Atlas,
        max_per_run: Optional[int],
        call_budget_s: float,
        concurrency: int = 4,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repo = repo
        self._profiler = profiler
        self._embeddings = embeddings
        self._atlas = atlas
        self._max_per_run = max_per_run
        self._call_budget_s = call_budget_s
        self._concurrency = concurrency
        self._clock = clock

    async def scan(self, account_id: str) -> FactProfileScan:
        """The account's facts by status and the model calls a run would make (no cap applied). Reads only."""
        if account_id == SYSTEM_ACCOUNT:
            return FactProfileScan()
        _, items = await self._plan(account_id)
        return self._count(items)

    async def run(self, account_id: str, deadline: datetime) -> FactProfileRun:
        """One profile step under a lease that expires at `deadline`. Never raises: an error is logged and returned as
        `error=True` with the counts reached so far."""
        if account_id == SYSTEM_ACCOUNT:
            return FactProfileRun()
        tally = _Tally()
        try:
            await self._run(account_id, deadline, tally)
        except Exception:
            tally.error = True
            logger.error(f"[FactProfile] run failed account={account_id[:8]}", exc_info=True)
        run = tally.freeze()
        scan = run.scan
        logger.info(
            f"[FactProfile] run account={account_id[:8]} scanned={scan.scanned} missing={scan.missing} "
            f"stale={scan.stale_input + scan.stale_derivation} rederived={run.rederived} written={run.written} "
            f"failed={run.failed} deferred={run.deferred} lost_cas={run.lost_cas} usd={run.usd:.4f}")
        return run

    # ------------------------------------------------------------------------------------------------ planning

    async def _plan(self, account_id: str) -> Tuple[PlaceNames, List[_Item]]:
        facts = await self._repo.list_profile_states(account_id)
        names, atlas = await self._atlas()
        registry = registry_digest()
        items: List[_Item] = []
        for fact in facts:
            directive = _is_directive(fact)
            derivation = profile_derivation(registry, atlas, directive)
            items.append(_Item(fact, classify_profile(fact, derivation), directive, derivation, _stored_answer(fact)))
        return names, items

    @staticmethod
    def _count(items: List[_Item]) -> FactProfileScan:
        def n(status: ProfileStatus) -> int:
            return sum(1 for item in items if item.status is status)

        return FactProfileScan(
            scanned=len(items), missing=n(ProfileStatus.MISSING), stale_input=n(ProfileStatus.STALE_INPUT),
            stale_derivation=n(ProfileStatus.STALE_DERIVATION), abandoned=n(ProfileStatus.ABANDONED),
            fresh=n(ProfileStatus.FRESH), to_call=sum(1 for item in items if item.needs_call))

    async def _run(self, account_id: str, deadline: datetime, tally: _Tally) -> None:
        names, items = await self._plan(account_id)
        tally.scan = self._count(items)

        pending: List[_Pending] = []
        for item in items:
            if item.status in (ProfileStatus.FRESH, ProfileStatus.ABANDONED):
                continue
            if item.is_directive:
                pending.append(self._directive_write(item))
            elif item.status is ProfileStatus.STALE_DERIVATION and item.answer is not None:
                pending.append(self._derived_write(item, item.answer, names))

        targets = sorted((i for i in items if i.needs_call),
                         key=lambda i: (-i.fact.created_at.timestamp(), i.fact.id))
        if self._max_per_run is not None:
            targets = targets[:self._max_per_run]
        called = await self._call_all(account_id, targets, deadline)
        for entry in called:
            if entry is None:
                continue
            tally.called += 1
            tally.usd += entry.reply.usd
            if entry.reply.status is ProfilerStatus.UNAVAILABLE:
                tally.outage = True
                continue
            pending.append(self._called_write(entry, names))
        tally.deferred += tally.scan.to_call - tally.called

        await self._attach_vectors(pending)
        await self._write_all(pending, deadline, tally)

        if tally.written:
            try:
                await self._repo.refresh_biographical_context_cache(owner_id=account_id)
                tally.refreshed = True
            except Exception:
                logger.warning(f"[FactProfile] baseline refresh failed account={account_id[:8]}", exc_info=True)

    # ------------------------------------------------------------------------------------------------ model calls

    async def _call_all(self, account_id: str, targets: List[_Item], deadline: datetime) -> List[Optional[_Called]]:
        gate = asyncio.Semaphore(self._concurrency)
        outage = asyncio.Event()

        async def one(item: _Item) -> Optional[_Called]:
            async with gate:
                if outage.is_set() or agent_timeout_s(deadline, self._clock()) <= self._call_budget_s:
                    return None
                started = self._clock()
                reply = await self._ask(account_id, item.fact)
                if reply.status is ProfilerStatus.UNAVAILABLE:
                    outage.set()
                return _Called(item, reply, int((self._clock() - started).total_seconds() * 1000))

        return list(await asyncio.gather(*(one(item) for item in targets)))

    async def _ask(self, account_id: str, fact: FactEntity) -> FactProfilerReply:
        try:
            return await self._profiler.profile(account_id, fact.text, fact.metadata)
        except Exception:
            logger.error(f"[FactProfile] profiler raised fact={fact.id[:8]}", exc_info=True)
            return FactProfilerReply.unavailable()

    # ------------------------------------------------------------------------------------------------ what to write

    def _directive_write(self, item: _Item) -> _Pending:
        fact, profile = item.fact, directive_profile()
        if item.status is ProfileStatus.STALE_DERIVATION:
            fields = self._rederived_fields(profile, item.derivation)
        else:
            fields = self._success_fields(profile, fact, item.derivation, None)
        fields["label_vector"] = None
        return _Pending(fact, _DIRECTIVE, fields,
                        outcome="rederived" if item.status is ProfileStatus.STALE_DERIVATION else "written")

    def _derived_write(self, item: _Item, answer: ProfileAnswer, names: PlaceNames) -> _Pending:
        outcome = derive_profile(answer, item.fact.text, item.fact.metadata, names)
        if outcome.redo_reason is not None:
            return self._failure(item.fact, outcome.profile.profile_drops)
        return _Pending(item.fact, _REDERIVED, self._rederived_fields(outcome.profile, item.derivation),
                        label_text=self._label_text(outcome.profile), drops=dict(outcome.profile.profile_drops),
                        outcome="rederived")

    def _called_write(self, entry: _Called, names: PlaceNames) -> _Pending:
        item, reply = entry.item, entry.reply
        if reply.status is not ProfilerStatus.OK or reply.answer is None:
            pending = self._failure(item.fact, {})
        else:
            outcome: DeriveOutcome = derive_profile(reply.answer, item.fact.text, item.fact.metadata, names)
            if outcome.redo_reason is not None:
                pending = self._failure(item.fact, outcome.profile.profile_drops)
            else:
                fields = self._success_fields(outcome.profile, item.fact, item.derivation, reply.model)
                pending = _Pending(item.fact, _WRITTEN, fields, label_text=self._label_text(outcome.profile),
                                   drops=dict(outcome.profile.profile_drops))
        pending.ms, pending.usd = entry.ms, reply.usd
        return pending

    def _failure(self, fact: FactEntity, drops: Any) -> _Pending:
        fingerprint = profile_fingerprint(fact.text, fact.metadata)
        fields = failure_fields(fact.profile_failures, fact.profile_failed_fingerprint, fingerprint)
        return _Pending(fact, _FAILED, fields, drops=dict(drops),
                        abandoned=fields["profile_failures"] >= ABANDON_AFTER_FAILURES)

    @staticmethod
    def _label_text(profile: FactProfile) -> Optional[str]:
        return ", ".join(profile.label_words) if profile.label_words else None

    @staticmethod
    def _rederived_fields(profile: FactProfile, derivation: str) -> Dict[str, Any]:
        return {**profile.stored(), "profile_derivation": derivation, "label_vector": None}

    def _success_fields(self, profile: FactProfile, fact: FactEntity, derivation: str,
                        model: Optional[str]) -> Dict[str, Any]:
        return {
            **profile.stored(), "label_vector": None,
            "profile_fingerprint": profile_fingerprint(fact.text, fact.metadata),
            "profile_derivation": derivation, "profile_model": model, "profiled_at": self._clock(),
            "profile_failures": None, "profile_failed_fingerprint": None,
        }

    # ------------------------------------------------------------------------------------------------ writing

    async def _attach_vectors(self, pending: List[_Pending]) -> None:
        """One embedding batch for every label text; a profile with no label words keeps `label_vector` None."""
        labelled = [p for p in pending if p.label_text is not None]
        if not labelled:
            return
        vectors = await self._embeddings.get_embeddings_batch([p.label_text for p in labelled if p.label_text])
        if len(vectors) != len(labelled):
            raise ValueError(f"embedding batch returned {len(vectors)} vectors for {len(labelled)} texts")
        for p, vector in zip(labelled, vectors):
            p.fields["label_vector"] = list(vector)

    async def _write_all(self, pending: List[_Pending], deadline: datetime, tally: _Tally) -> None:
        for index, p in enumerate(pending):
            if agent_timeout_s(deadline, self._clock()) <= 0:
                tally.deferred += len(pending) - index
                return
            wrote = await self._repo.write_fact_profile(p.fact.id, p.fields, p.fact.profile_fingerprint)
            if not wrote:
                tally.lost_cas += 1
                self._log_fact(p, "lost_cas")
                continue
            for reason, n in p.drops.items():
                tally.drops[reason] = tally.drops.get(reason, 0) + n
            if p.kind == _FAILED:
                tally.failed += 1
                if p.abandoned:
                    tally.newly_abandoned += 1
                self._log_fact(p, "abandoned" if p.abandoned else "failed")
                continue
            tally.written += 1
            if p.kind == _DIRECTIVE:
                tally.directives += 1
            elif p.kind == _REDERIVED:
                tally.rederived += 1
            self._log_fact(p, p.outcome)

    @staticmethod
    def _log_fact(p: _Pending, outcome: str) -> None:
        logger.info(f"[FactProfile] fact={p.fact.id[:8]} outcome={outcome} drops={_drops_text(p.drops)} "
                    f"ms={p.ms} usd={p.usd:.4f}")
