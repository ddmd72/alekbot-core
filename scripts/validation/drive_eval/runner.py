"""
Eval runner: index the fixture with the real tick, then run every gold case through the real search.

Production pieces: DriveIndexingService, DriveSearchService, InMemoryDriveIndexRepository (the second
DriveIndexRepository implementation), the injected embeddings and place lookup. Harness pieces: the
fixture drive, a queue that only records continuations (the runner itself plays the queue by ticking
again), a user store with indexing on, and a clock that moves one minute per tick.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index_params import K
from src.domain.drive_index_planner import PlaceOf
from src.domain.drive_search import DriveSearchRequest, request_from_fields
from src.domain.user import UserBotConfig, UserProfile
from src.domain.user_drive import parse_drive_ref
from src.ports.embedding_service import EmbeddingService
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_search_service import DriveSearchService

from .fixture_drive import FixtureDrive
from .metrics import (
    CASE_TYPES, CaseOutcome, bar_mismatch, case_hit, lost_cases, make_bars, recall, regressions,
)

EVAL_USER = "eval-user"
EVAL_START = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MAX_TICKS = 400
MAX_ERRORS = 3
MAX_THROTTLED = 6     # consecutive throttled ticks; the service's backoff makes that about half an hour of its backoff (60+120+240+480+960 s)

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_SETUP = 2


class EvalError(RuntimeError):
    """No verdict: a malformed gold file, an index that did not settle or holds no card, no bars, or bars
    recorded on another K, tree or gold file."""


class EvalClock:
    def __init__(self, start: datetime = EVAL_START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _RecordingQueue:
    """Stands in for the drive queue: the runner re-ticks instead of dispatching."""

    def __init__(self) -> None:
        self.tasks: List[Dict[str, Any]] = []

    async def enqueue_worker_task(self, task_type: str, payload: Dict[str, Any], delay_seconds: int = 0,
                                  deadline_seconds: Optional[int] = None,
                                  dedup_key: Optional[str] = None) -> Optional[str]:
        self.tasks.append({"task_type": task_type, "payload": payload, "delay": delay_seconds, "key": dedup_key})
        return f"eval-{len(self.tasks)}"


class _EnabledUsers:
    """The one eval user, indexing on."""

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        if user_id != EVAL_USER:
            return None
        return UserProfile(user_id=user_id, account_id="eval-account",
                           config=UserBotConfig(drive_indexing_enabled=True))


@dataclass
class IndexOutcome:
    repo: InMemoryDriveIndexRepository
    ticks: int
    statuses: Dict[str, int]
    summary: Dict[str, int]
    records: Dict[str, int]


@dataclass
class EvalRun:
    outcomes: List[CaseOutcome] = field(default_factory=list)
    cases: List[Dict[str, Any]] = field(default_factory=list)
    errors: int = 0       # a search raised
    refused: int = 0      # a search answered with a status other than "ok"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_gold(gold: Sequence[Dict[str, Any]], paths: Sequence[str], k: int = K) -> None:
    """A malformed case would record a meaningless bar: refuse it before anything runs."""
    known = set(paths)
    problems: List[str] = []
    ids = [case.get("id") for case in gold]
    problems += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for case in gold:
        cid = case.get("id")
        if case.get("case") not in CASE_TYPES:
            problems.append(f"{cid}: unknown case type {case.get('case')!r}")
        if not case.get("expected_paths"):
            problems.append(f"{cid}: no expected_paths")
        problems += [f"{cid}: {p} is not in the tree" for p in case.get("expected_paths", []) if p not in known]
        case_k = case.get("k", k)
        if not isinstance(case_k, int) or not 1 <= case_k <= k:
            problems.append(f"{cid}: k must be an integer in 1..{k}")
        try:
            request_for(case)   # what production would refuse (an unknown kind, reversed dates) is a gold bug
        except ValueError as exc:
            problems.append(f"{cid}: {exc}")
    if problems:
        raise EvalError("malformed gold file: " + "; ".join(problems))


async def index_fixture(drive: FixtureDrive, repo: InMemoryDriveIndexRepository, embeddings: EmbeddingService,
                        place_of: PlaceOf, embedding_space: str, clock: EvalClock, *,
                        max_ticks: int = MAX_TICKS, throttle_pause_s: float = 30.0,
                        monotonic: Callable[[], float] = time.monotonic) -> IndexOutcome:
    """Tick until no continuation is pending, as the drive queue would."""
    queue = _RecordingQueue()
    service = DriveIndexingService(drive, repo, embeddings, queue, _EnabledUsers(),
                                   place_of, embedding_space, clock=clock, monotonic=monotonic)
    statuses: Counter[str] = Counter()
    summary: Counter[str] = Counter()
    errors = 0
    throttled = 0
    for tick in range(1, max_ticks + 1):
        result = await service.tick(EVAL_USER)
        statuses[result.status] += 1
        summary.update(result.summary)
        if result.status == "error":
            errors += 1
            if errors >= MAX_ERRORS:
                raise EvalError(f"{errors} ticks ended in error; see the [DriveIndex] log lines")
        if result.status == "throttled":
            throttled += 1
            if throttled >= MAX_THROTTLED:
                raise EvalError(f"{throttled} consecutive ticks were throttled; the quota is busy, run again later")
            # Wait as long as the service asked (its backoff or the provider's retry-after), never less than the pause.
            queued = queue.tasks[-1]["delay"] if queue.tasks else 0
            await asyncio.sleep(max(throttle_pause_s, queued))
        else:
            throttled = 0
        state = await repo.get_sync_state(EVAL_USER)
        if state is None:
            raise EvalError(f"tick {tick} left no sync state (status {result.status})")
        if state.continuation_enqueued_at is None and state.round_complete:
            records = await repo.count_by_state(EVAL_USER)
            return IndexOutcome(repo, tick, dict(statuses), dict(summary), records)
        clock.advance(60)
    raise EvalError(f"the index did not settle in {max_ticks} ticks")


def request_for(case: Dict[str, Any]) -> DriveSearchRequest:
    """The request exactly as FileManagementAgent._search_drive builds it; raises ValueError where it refuses."""
    filters = case.get("filters") or {}
    return request_from_fields(case.get("query"), filters.get("folder"), filters.get("kind"),
                               filters.get("date_from"), filters.get("date_to"))


def _card_paths(card: Any, drive: FixtureDrive) -> List[str]:
    item_id = parse_drive_ref(getattr(card, "ref", "") or "")
    own = (drive.path_of(item_id) if item_id else None) or str(getattr(card, "path", "")).strip("/")
    return [own] + [str(p).strip("/") for p in getattr(card, "also_at", ()) or ()]


async def run_cases(search: DriveSearchService, drive: FixtureDrive, gold: Sequence[Dict[str, Any]],
                    k: int = K) -> EvalRun:
    run = EvalRun()
    for case in gold:
        row: Dict[str, Any] = {"id": case["id"], "case": case["case"], "lang": case.get("lang"),
                               "query": case.get("query", ""), "filters": case.get("filters") or {},
                               "expected": list(case["expected_paths"])}
        case_k = int(case.get("k", k))
        try:
            result = await search.search(EVAL_USER, request_for(case))
        except Exception as exc:   # a crash is a finding, not a reason to lose the other cases
            run.errors += 1
            row.update(status=f"exception: {type(exc).__name__}: {exc}", top=[], hit=False)
        else:
            run.refused += result.status != "ok"
            top = [_card_paths(card, drive) for card in list(result.cards)[:case_k]]
            covered = {path for group in top for path in group}
            hit = result.status == "ok" and case_hit(case["expected_paths"], covered, case_k)
            missing = [p for p in case["expected_paths"] if p not in covered]
            row.update(status=result.status, k=case_k, top=top, hit=hit, missing=missing)
        run.cases.append(row)
        run.outcomes.append(CaseOutcome(case["id"], case["case"], bool(row["hit"])))
    return run


async def run_eval(tree_path: Path, gold_path: Path, bars_path: Path, report_path: Path, *,
                   embeddings: EmbeddingService, embedding_space: str, place_of: PlaceOf, record: bool,
                   max_ticks: int = MAX_TICKS, throttle_pause_s: float = 30.0) -> Dict[str, Any]:
    """Index, search, score, then record or compare. Returns the report (also written to report_path)."""
    drive = FixtureDrive.from_file(tree_path)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    validate_gold(gold, drive.paths())
    tree_sha, gold_sha = sha256_of(tree_path), sha256_of(gold_path)
    bars: Optional[Dict[str, Any]] = None
    if not record:
        if not bars_path.exists():
            raise EvalError(f"no bars at {bars_path}; run with --record first")
        bars = json.loads(bars_path.read_text(encoding="utf-8"))
        mismatch = bar_mismatch(bars, K, tree_sha, gold_sha, [case["id"] for case in gold])
        if mismatch:
            raise EvalError("; ".join(mismatch) + " — review the change, then run with --record")
    clock = EvalClock()
    repo = InMemoryDriveIndexRepository()
    indexed = await index_fixture(drive, repo, embeddings, place_of, embedding_space, clock,
                                  max_ticks=max_ticks, throttle_pause_s=throttle_pause_s)
    if not indexed.records.get("carded"):
        raise EvalError("the index settled with no card; nothing to measure")
    search = DriveSearchService(repo, embeddings, _EnabledUsers(), drive, clock=clock)
    run = await run_cases(search, drive, gold)
    table = recall(run.outcomes)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report: Dict[str, Any] = {
        "generated_at": now_iso, "embedding_space": embedding_space, "k": K,
        "tree": str(tree_path), "gold": str(gold_path), "items": len(drive.paths()),
        "index": {"ticks": indexed.ticks, "statuses": indexed.statuses, "summary": indexed.summary,
                  "records": indexed.records},
        "recall": table, "search_errors": run.errors, "refused": run.refused, "cases": run.cases,
    }
    if run.errors:
        report["verdict"] = "search_errors"   # never recorded as a bar, never a pass
    elif record and run.refused:
        report["verdict"] = "unrecordable"    # a case the service refused (e.g. a folder not found) is a gold bug
    elif record:
        bars_path.parent.mkdir(parents=True, exist_ok=True)
        bars = make_bars(table, run.outcomes, K, embedding_space, now_iso, tree_sha, gold_sha)
        bars_path.write_text(json.dumps(bars, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["verdict"] = "recorded"
    else:
        assert bars is not None
        # An embedding change is judged against the same bars (§17.8); the report names both spaces.
        report["bars_embedding_space"] = bars.get("embedding_space")
        report["regressions"] = regressions(table, bars)
        report["lost_cases"] = lost_cases(run.outcomes, bars)
        report["verdict"] = "regression" if report["regressions"] else "pass"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def exit_code(report: Dict[str, Any]) -> int:
    return {"pass": EXIT_OK, "recorded": EXIT_OK, "regression": EXIT_REGRESSION}.get(report["verdict"], EXIT_SETUP)
