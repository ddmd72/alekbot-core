"""
Eval runner: index the fixture with the real tick, then run every gold case through the real search.

Production pieces: DriveIndexingService, DriveSearchService, InMemoryDriveIndexRepository (the second
DriveIndexRepository implementation), the injected embeddings and place lookup, and from Slice 2+4 the injected
extractors (parser and reader) and image triage. Harness pieces: the fixture drive, a queue that only records
continuations (the runner itself plays the queue by ticking again), a user store with indexing on, and a clock that
moves one minute per tick.

G1 cases score the paths of the top K cards; G2 cases (§24.11) also need their expected substring in the window
of the card that holds the path. Requests are built by production's `request_from_fields` (R13).
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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from scripts.validation.drive_eval.eval_connection import EVAL_CONNECTION_ID, EvalDriveConnections, eval_connection
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import Priority, inaccessible
from src.domain.drive_index_params import K
from src.domain.drive_index_planner import PlaceOf
from src.domain.drive_search import DriveSearchRequest, request_from_fields, require_search_terms
from src.domain.user import UserBotConfig, UserProfile
from scripts.validation.drive_eval.eval_connection import ref_item_id
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.document_profiler_port import DocumentProfilerPort
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.image_triage_port import ImageTriagePort
from src.ports.passage_hint_port import PassageHintPort
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_search_service import DriveSearchService

from .fixture_drive import FixtureDrive
from .metrics import (
    CASE_TYPES, CaseOutcome, bar_mismatch, case_hit, expected_substrings, lost_cases, make_bars, recall, regressions,
    substring_hit,
)

EVAL_USER = "eval-user"
EVAL_START = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MAX_TICKS = 400
MAX_ERRORS = 3
MAX_THROTTLED = 6     # consecutive throttled ticks; the service's backoff makes that about half an hour of its backoff (60+120+240+480+960 s)

GOALS = frozenset({"G1", "G2"})
WINDOW_REPORT_CHARS = 400

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
    repo: DriveIndexRepository
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


def gold_paths(gold: Union[Path, Sequence[Path]]) -> List[Path]:
    return [gold] if isinstance(gold, Path) else list(gold)


def gold_sha256(paths: Sequence[Path]) -> str:
    """One file: its own SHA-256 (as Slice 1's bars pinned it); several: one hash over all of them, in order."""
    if len(paths) == 1:
        return sha256_of(paths[0])
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def load_gold(paths: Sequence[Path]) -> List[Dict[str, Any]]:
    gold: List[Dict[str, Any]] = []
    for path in paths:
        gold.extend(json.loads(path.read_text(encoding="utf-8")))
    return gold


def _substring_problems(case: Dict[str, Any]) -> List[str]:
    cid, wanted = case.get("id"), case.get("expected_substring")
    if case.get("goal", "G1") not in GOALS:
        return [f"{cid}: goal must be one of {sorted(GOALS)}"]
    if wanted is None:
        return [f"{cid}: a G2 case needs expected_substring"] if case.get("goal") == "G2" else []
    if isinstance(wanted, str):
        return [] if wanted.strip() else [f"{cid}: expected_substring is empty"]
    if isinstance(wanted, dict) and wanted and all(isinstance(v, str) and v.strip() for v in wanted.values()):
        stray = sorted(set(wanted) - set(case.get("expected_paths", [])))
        return [f"{cid}: expected_substring names {stray}, not expected paths"] if stray else []
    return [f"{cid}: expected_substring must be a string or a {{path: string}} map"]


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
        elif "min_hits" in case:
            most = min(len(case.get("expected_paths", [])), case_k)
            min_hits = case["min_hits"]
            if isinstance(min_hits, bool) or not isinstance(min_hits, int) or not 1 <= min_hits < most:
                problems.append(f"{cid}: min_hits must be an integer in 1..{most - 1} (fewer than its K hits)")
        if "folder_path" in case and (case.get("case") != "place" or case["folder_path"] not in known):
            problems.append(f"{cid}: folder_path must be a folder of the tree, on a place case")
        if "known_hard" in case and not (isinstance(case["known_hard"], str) and case["known_hard"].strip()):
            problems.append(f"{cid}: known_hard must say why the case is hard")
        if "accepted_loss" in case and not (isinstance(case["accepted_loss"], str) and case["accepted_loss"].strip()):
            problems.append(f"{cid}: accepted_loss must say why the loss is accepted")
        if "requires_enrichment" in case and case["requires_enrichment"] is not True:
            problems.append(f"{cid}: requires_enrichment must be true")
        if "ocr" in case and not isinstance(case["ocr"], bool):
            problems.append(f"{cid}: ocr must be true or false")
        problems += _substring_problems(case)
        try:
            request_for(case)   # what production would refuse (an unknown kind, reversed dates) is a gold bug
        except ValueError as exc:
            problems.append(f"{cid}: {exc}")
    if problems:
        raise EvalError("malformed gold file: " + "; ".join(problems))


async def index_fixture(drive: FixtureDrive, repo: DriveIndexRepository, embeddings: EmbeddingService,
                        place_of: PlaceOf, embedding_space: str, clock: EvalClock, *,
                        max_ticks: int = MAX_TICKS, throttle_pause_s: float = 30.0,
                        monotonic: Callable[[], float] = time.monotonic,
                        extractors: Optional[Mapping[str, ContentExtractorPort]] = None,
                        triage: Optional[ImageTriagePort] = None,
                        user_id: str = EVAL_USER, users: Optional[Any] = None,
                        profiler: Optional[DocumentProfilerPort] = None,
                        passages: Optional[PassageHintPort] = None) -> IndexOutcome:
    """Tick until no continuation is pending and no record waits for a later attempt, as the drive queue and the
    10-minute fan-out would (a waiting record moves the clock to its due_at). `extractors` and `triage` are the content
    ports the indexer service is built with (parser and reader, §24.2); the CLI passes the real ones. `user_id` and
    `users` let the live check index under its synthetic user. `profiler` and `passages` are Search v2's enrichment
    ports (§25.4; the v2 eval passes the snapshot's replays)."""
    queue = _RecordingQueue()
    content: Dict[str, Any] = {}
    if extractors is not None:
        content["extractors"] = extractors
    if triage is not None:
        content["triage"] = triage
    if profiler is not None:
        content["profiler"] = profiler
    if passages is not None:
        content["passages"] = passages
    scope = ConnectionScope(user_id, EVAL_CONNECTION_ID)
    connections = EvalDriveConnections(eval_connection(
        user_id, account_id="eval-account" if user_id == EVAL_USER else f"{user_id}-account"))
    service = DriveIndexingService(drive, repo, embeddings, queue, users or _EnabledUsers(),
                                   place_of, embedding_space, clock=clock, monotonic=monotonic,
                                   connections=connections, **content)
    statuses: Counter[str] = Counter()
    summary: Counter[str] = Counter()
    errors = 0
    throttled = 0
    for tick in range(1, max_ticks + 1):
        result = await service.tick(scope)
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
        state = await repo.get_sync_state(scope)
        if state is None:
            raise EvalError(f"tick {tick} left no sync state (status {result.status})")
        if state.continuation_enqueued_at is None and state.round_complete:
            due_at = await _earliest_due_at(repo, scope)
            if due_at is None or await _only_closed_files_wait(repo, scope):
                records = await repo.count_by_state(scope)
                return IndexOutcome(repo, tick, dict(statuses), dict(summary), records)
            # A record waits for a later attempt (a retry, an image with no rendition yet): production's fan-out
            # re-ticks every 10 minutes, so the runner moves the clock to when the record is due and ticks again.
            clock.advance(max(60.0, (due_at - clock.now).total_seconds()))
            continue
        clock.advance(60)
    raise EvalError(f"the index did not settle in {max_ticks} ticks")


async def _only_closed_files_wait(repo: DriveIndexRepository, scope: ConnectionScope) -> bool:
    """Every record that waits is a file the bot cannot open (§26.2): it is re-read only when something changes (an
    observation, or the slow retry), so there is nothing for the runner to wait for, and ticking on would never settle."""
    waiting = await repo.due_records(scope, datetime(9999, 1, 1, tzinfo=timezone.utc), 200)
    return bool(waiting) and all(inaccessible(rec) for rec in waiting)


async def _earliest_due_at(repo: DriveIndexRepository, scope: ConnectionScope) -> Optional[datetime]:
    dates = [d for d in [await repo.oldest_due_at(scope, p) for p in Priority] if d is not None]
    return min(dates, default=None)


def request_for(case: Dict[str, Any]) -> DriveSearchRequest:
    """The request exactly as FileManagementAgent._search_drive builds it; raises ValueError where it refuses."""
    filters = case.get("filters") or {}
    request = request_from_fields(case.get("query"), filters.get("folder"), filters.get("kind"),
                                  filters.get("date_from"), filters.get("date_to"))
    require_search_terms(request)
    return request


def _card_paths(card: Any, drive: FixtureDrive) -> List[str]:
    item_id = ref_item_id(getattr(card, "ref", "") or "")
    own = (drive.path_of(item_id) if item_id else None) or str(getattr(card, "path", "")).strip("/")
    return [own] + [str(p).strip("/") for p in getattr(card, "also_at", ()) or ()]


def _windows(cards: Sequence[Any], drive: FixtureDrive) -> Dict[str, str]:
    """{path: the matched passage its card showed}: a collapsed duplicate shows its passage for every copy.
    v2 cards carry `passage`; an About card has none."""
    out: Dict[str, str] = {}
    for card in cards:
        window = str(getattr(card, "passage", "") or "")
        for path in _card_paths(card, drive):
            out.setdefault(path, window)
    return out


def case_labels(case: Dict[str, Any]) -> tuple:
    return (case.get("goal", "G1"),) + (("ocr",) if case.get("ocr") else ())


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
            cards = list(result.cards)[:case_k]
            top = [_card_paths(card, drive) for card in cards]
            covered = {path for group in top for path in group}
            hit = result.status == "ok" and case_hit(case["expected_paths"], covered, case_k, case.get("min_hits"),
                                                   case.get("folder_path"))
            missing = [p for p in case["expected_paths"] if p not in covered]
            row.update(status=result.status, k=case_k, top=top, hit=hit, missing=missing)
            wanted = expected_substrings(case["expected_paths"], case.get("expected_substring"))
            if wanted:
                windows = _windows(cards, drive)
                found = substring_hit(wanted, windows)
                row.update(path_hit=hit, substring_hit=found, hit=hit and found,
                           windows={p: windows.get(p, "")[:WINDOW_REPORT_CHARS] for p in wanted})
        for key in ("min_hits", "folder_path", "known_hard", "accepted_loss", "ocr", "goal"):
            if key in case:
                row[key] = case[key]
        run.cases.append(row)
        run.outcomes.append(CaseOutcome(case["id"], case["case"], bool(row["hit"]), case_labels(case)))
    return run


async def run_eval(tree_path: Path, gold_path: Union[Path, Sequence[Path]], bars_path: Path, report_path: Path, *,
                   embeddings: EmbeddingService, embedding_space: str, place_of: PlaceOf, record: bool,
                   max_ticks: int = MAX_TICKS, throttle_pause_s: float = 30.0,
                   extractors: Optional[Mapping[str, ContentExtractorPort]] = None,
                   triage: Optional[ImageTriagePort] = None,
                   reader_provider: Optional[str] = None) -> Dict[str, Any]:
    """Index, search, score, then record or compare. Returns the report (also written to report_path).
    `reader_provider` (the CLI's real run) is stamped into the bars, and bars from another reader cannot judge."""
    drive = FixtureDrive.from_file(tree_path)
    golds = gold_paths(gold_path)
    gold = load_gold(golds)
    validate_gold(gold, drive.paths())
    tree_sha, gold_sha, files_sha = sha256_of(tree_path), gold_sha256(golds), drive.content_sha256()
    bars: Optional[Dict[str, Any]] = None
    if not record:
        if not bars_path.exists():
            raise EvalError(f"no bars at {bars_path}; run with --record first")
        bars = json.loads(bars_path.read_text(encoding="utf-8"))
        guarded_ids = [case["id"] for case in gold if "known_hard" not in case]
        mismatch = bar_mismatch(bars, K, tree_sha, gold_sha, guarded_ids, files_sha, reader_provider)
        if mismatch:
            raise EvalError("; ".join(mismatch) + " — review the change, then run with --record")
    clock = EvalClock()
    repo = InMemoryDriveIndexRepository()
    indexed = await index_fixture(drive, repo, embeddings, place_of, embedding_space, clock,
                                  max_ticks=max_ticks, throttle_pause_s=throttle_pause_s,
                                  extractors=extractors, triage=triage)
    if not indexed.records.get("carded"):
        raise EvalError("the index settled with no card; nothing to measure")
    search = DriveSearchService(repo, embeddings, drive, EvalDriveConnections(eval_connection(EVAL_USER)), clock=clock,
                                embedding_space=embedding_space)
    run = await run_cases(search, drive, gold)
    # A known-hard case is run and reported, but guards nothing: it is in no bar and never a lost case.
    hard = {case["id"] for case in gold if "known_hard" in case}
    guarded = [o for o in run.outcomes if o.case_id not in hard]
    table = recall(guarded)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report: Dict[str, Any] = {
        "generated_at": now_iso, "embedding_space": embedding_space, "k": K, "reader_provider": reader_provider,
        "tree": str(tree_path), "gold": [str(g) for g in golds], "items": len(drive.paths()),
        "index": {"ticks": indexed.ticks, "statuses": indexed.statuses, "summary": indexed.summary,
                  "records": indexed.records, "downloads": len(drive.downloads),
                  "thumbnails": len(drive.thumbnail_calls)},
        "recall": table, "search_errors": run.errors, "refused": run.refused, "cases": run.cases,
        "known_hard": {o.case_id: o.hit for o in run.outcomes if o.case_id in hard},
    }
    if run.errors:
        report["verdict"] = "search_errors"   # never recorded as a bar, never a pass
    elif record and run.refused:
        report["verdict"] = "unrecordable"    # a case the service refused (e.g. a folder not found) is a gold bug
    elif record:
        bars_path.parent.mkdir(parents=True, exist_ok=True)
        bars = make_bars(table, guarded, K, embedding_space, now_iso, tree_sha, gold_sha, files_sha, reader_provider)
        bars_path.write_text(json.dumps(bars, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["verdict"] = "recorded"
    else:
        assert bars is not None
        # An embedding change is judged against the same bars (§17.8); the report names both spaces.
        report["bars_embedding_space"] = bars.get("embedding_space")
        report["regressions"] = regressions(table, bars)
        report["lost_cases"] = lost_cases(guarded, bars)
        # One case that hit when the bars were recorded and misses now fails the run (§17.8: no tolerance).
        report["verdict"] = "regression" if report["regressions"] or report["lost_cases"] else "pass"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def exit_code(report: Dict[str, Any]) -> int:
    return {"pass": EXIT_OK, "recorded": EXIT_OK, "regression": EXIT_REGRESSION}.get(report["verdict"], EXIT_SETUP)
