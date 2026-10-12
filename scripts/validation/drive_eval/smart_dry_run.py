"""
G3 — does Smart search the drive when the answer may be in the user's documents, and leave it alone when it is not?
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §17.8, §24.11, D11)

A Smart dry run on the scaffolding of scripts/validation/ab_agent_models.py: the real Smart (the owner's user config,
its Firestore prompts, the owner's memory) answers every case of tests/fixtures/drive_eval/gold_g3.json in an
isolated session. Drive reads run for real against the fixture index the eval builds (the same tick, parser, reader
and triage): `search_files_in_drive`, `open_file_from_drive`, `list_files_in_drive`, and `open_file` on a `drive:`
ref. Other read-only delegations (memory, web, mail, maps, compute) run for real on their own models, as in the A/B
dry run. Everything that writes is answered "accepted, not executed" and never runs.

Metric: the search-initiated rate (a case searched when Smart delegated `search_files_in_drive` at least once). On
the positives it is barred (bars_g3.json); on the negatives (chit-chat, memory, web, general questions) it is
reported. One LLM sample per case is noisy (decisions/directive_applicability_gate.md § Variance), so the bar is the
positive rate over `--repeats` runs, not a per-case rule.

OWNER-run: real Smart calls on the owner's account (cents per run). Answers can quote the owner's memory, so the
report goes to the gitignored scripts/memory/drive_g3/.

    venv/bin/python -m scripts.validation.drive_eval.smart_dry_run --record --repeats 3
    venv/bin/python -m scripts.validation.drive_eval.smart_dry_run --repeats 3
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig
from src.domain.drive_search import DriveSearchRequest, DriveSearchResult
from src.domain.user_drive import is_drive_ref
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, FILE_MANAGEMENT, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

from .fixture_drive import FixtureDrive
from .runner import EVAL_USER

SETS = ("positive", "negative")
POSITIVE_CATEGORIES = frozenset({"find", "content", "knowledge_base", "ocr", "version_family", "memory_vs_document"})
NEGATIVE_CATEGORIES = frozenset({"chit_chat", "memory", "web", "general"})
LANGS = frozenset({"en", "ru", "uk", "es", "fr"})
MIN_PER_SET = 10
DRIVE_READ_INTENTS: FrozenSet[str] = frozenset({
    Intent.SEARCH_FILES_IN_DRIVE, Intent.OPEN_FILE_FROM_DRIVE, Intent.LIST_FILES_IN_DRIVE,
})
STUB_RESULT = "Done: '{intent}' accepted and completed successfully."
_EPS = 1e-9

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"
_OUT = _REPO / "scripts" / "memory" / "drive_g3"

EXIT_OK, EXIT_REGRESSION, EXIT_SETUP = 0, 1, 2


class G3Error(RuntimeError):
    """No verdict: a malformed gold file, bars from another gold or model, or no bars."""


# -- gold ------------------------------------------------------------------------------------------------------------

def validate_g3(gold: Sequence[Mapping[str, Any]]) -> None:
    problems: List[str] = []
    ids = [c.get("id") for c in gold]
    problems += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for case in gold:
        cid, kind = case.get("id"), case.get("set")
        if kind not in SETS:
            problems.append(f"{cid}: set must be positive or negative")
        allowed = POSITIVE_CATEGORIES if kind == "positive" else NEGATIVE_CATEGORIES
        if case.get("category") not in allowed:
            problems.append(f"{cid}: category {case.get('category')!r} is not a {kind} category")
        if not str(case.get("text", "")).strip():
            problems.append(f"{cid}: no text")
        if case.get("lang") not in LANGS:
            problems.append(f"{cid}: unknown lang {case.get('lang')!r}")
    for kind in SETS:
        if sum(1 for c in gold if c.get("set") == kind) < MIN_PER_SET:
            problems.append(f"fewer than {MIN_PER_SET} {kind} cases")
    if problems:
        raise G3Error("malformed G3 gold: " + "; ".join(problems))


# -- the fixture side: a FileManagementAgent over the fixture drive and its index -----------------------------------

class _NoChatFiles(FileStoragePort):
    """The dry run has no chat uploads: every chat-file read is a missing file."""

    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        raise FileNotFoundError(filename)

    async def download(self, filename: str, user_id: str) -> bytes:
        raise FileNotFoundError(filename)

    async def delete(self, filename: str, user_id: str) -> None:
        raise FileNotFoundError(filename)

    async def exists(self, filename: str, user_id: str) -> bool:
        return False

    async def get_url(self, filename: str, user_id: str) -> str:
        raise FileNotFoundError(filename)


class AsEvalUser:
    """Search the index the eval built under EVAL_USER, whoever asks (the dry run's Smart is the owner)."""

    def __init__(self, search: Any) -> None:
        self._search = search

    async def search(self, user_id: str, request: DriveSearchRequest) -> DriveSearchResult:
        result: DriveSearchResult = await self._search.search(EVAL_USER, request)
        return result


def fixture_engine(user_id: str, drive: FixtureDrive, search: Any) -> DelegationEngine:
    """A delegation engine whose coordinator knows one agent, a FileManagementAgent over the fixture drive, registered
    under the id the real coordinator routes `user_id`'s drive intents to."""
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = AgentCoordinator(registry=registry)
    storage = _NoChatFiles()
    agent = FileManagementAgent(
        config=AgentConfig(agent_id=f"{FILE_MANAGEMENT.agent_id}_{user_id}", agent_type="file_management",
                           timeout_ms=120_000, capabilities=["file_storage"]),
        conversion_service=FileConversionService(storage=storage, drive=drive), storage=storage,
        drive_service=UserDriveService(drive), drive_search=AsEvalUser(search),
    )
    coordinator.register_agent(agent)
    return DelegationEngine(coordinator)


def is_drive_read(intent: str, context: Mapping[str, Any]) -> bool:
    return intent in DRIVE_READ_INTENTS or (intent == Intent.OPEN_FILE and is_drive_ref(str(context.get("file_ref", ""))))


Dispatch = Callable[..., Awaitable[ToolResult]]


@dataclass
class DryRunDispatch:
    """Stands in for DelegationEngine.dispatch: logs every delegation, sends drive reads to the fixture engine, lets
    other read-only intents through on Smart's own engine, answers every write without running it."""
    real_dispatch: Dispatch
    fixture: DelegationEngine
    read_only: FrozenSet[str]
    calls: List[Dict[str, Any]] = field(default_factory=list)

    async def __call__(self, engine: Any, tool_call: Any, context: Dict[str, Any], *args: Any, **kwargs: Any) -> ToolResult:
        tool_args = tool_call.args or {}
        intent = str(tool_args.get("intent", ""))
        params = tool_args.get("context") or {}
        if is_drive_read(intent, params):
            route = "fixture"
            result = await self.real_dispatch(self.fixture, tool_call, context, *args, **kwargs)
        elif intent in self.read_only:
            route = "real"
            result = await self.real_dispatch(engine, tool_call, context, *args, **kwargs)
        else:
            route = "stub"
            result = ToolResult(name=tool_call.name, result_str=STUB_RESULT.format(intent=intent))
        self.calls.append({"intent": intent, "context": params, "route": route, "failed": result.failed})
        return result


def searched(calls: Sequence[Mapping[str, Any]]) -> bool:
    return any(c["intent"] == Intent.SEARCH_FILES_IN_DRIVE for c in calls)


# -- scores and bars -------------------------------------------------------------------------------------------------

def score(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, float]]:
    """{scope: {runs, searched, rate}} for each set and each `set:category`."""
    groups: Dict[str, List[bool]] = {}
    for row in rows:
        for scope in (row["set"], f"{row['set']}:{row['category']}"):
            groups.setdefault(scope, []).append(bool(row["searched"]))
    return {scope: {"runs": len(v), "searched": sum(v), "rate": sum(v) / len(v)} for scope, v in sorted(groups.items())}


def make_g3_bars(table: Mapping[str, Mapping[str, float]], gold_sha256: str, model: str, repeats: int,
                 recorded_at: str) -> Dict[str, Any]:
    return {"gold_sha256": gold_sha256, "smart_model": model, "repeats": repeats, "recorded_at": recorded_at,
            "positive_rate": table.get("positive", {}).get("rate", 0.0),
            "negative_rate_reported": table.get("negative", {}).get("rate", 0.0)}


def judge(table: Mapping[str, Mapping[str, float]], bars: Mapping[str, Any], gold_sha256: str, model: str) -> str:
    """"pass" or "regression"; G3Error when the bars cannot judge this run."""
    if bars.get("gold_sha256") != gold_sha256:
        raise G3Error("gold_g3.json changed since the G3 bars were recorded — review it, then run with --record")
    if bars.get("smart_model") != model:
        raise G3Error(f"the bars were recorded with Smart on {bars.get('smart_model')}, this run is on {model}")
    now = table.get("positive", {}).get("rate", 0.0)
    return "regression" if now < float(bars["positive_rate"]) - _EPS else "pass"


# -- the owner's run ---------------------------------------------------------------------------------------------------

def _args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.smart_dry_run")
    parser.add_argument("--tree", type=Path, default=_FIXTURES / "tree.json")
    parser.add_argument("--gold", type=Path, default=_FIXTURES / "gold_g3.json")
    parser.add_argument("--bars", type=Path, default=_FIXTURES / "bars_g3.json")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--record", action="store_true")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Real Smart, Firestore prompts and model keys: the owner's run."""
    from google.cloud import firestore

    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.adapters.firestore_user_repo import FirestoreUserRepository
    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
    from src.composition.drive_indexing_setup import build_place_lookup
    from src.composition.service_container import ServiceContainer
    from src.composition.user_agent_factory import UserAgentFactory
    from src.config.settings import load_settings
    from src.domain.agent import AgentIntent, AgentMessage
    from src.domain.llm import MessagePart
    from src.domain.request_context import RequestContext
    from src.services.drive_search_service import DriveSearchService

    from . import content_wiring
    from .runner import EvalClock, _EnabledUsers, index_fixture

    from scripts.validation.ab_agent_models import _READ_ONLY_INTENTS   # the A/B dry run's read-only set

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    validate_g3(gold)
    gold_sha = hashlib.sha256(args.gold.read_bytes()).hexdigest()
    user_id, account_id = os.environ["DEV_USER_ID"], os.environ["DEV_ACCOUNT_ID"]

    drive = FixtureDrive.from_file(args.tree)
    repo = InMemoryDriveIndexRepository()
    embeddings = GeminiEmbeddingAdapter(api_key=os.environ["GEMINI_API_KEY"])
    extractors, triage = content_wiring.build_real_content()
    clock = EvalClock()
    await index_fixture(drive, repo, embeddings, build_place_lookup(), GeminiEmbeddingAdapter.EMBEDDING_SPACE, clock,
                        extractors=extractors, triage=triage)
    search = DriveSearchService(repo, embeddings, _EnabledUsers(), drive, clock=clock,
                                embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE)

    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    factory = UserAgentFactory(config=settings, env_config=env_config, coordinator=AgentCoordinator(registry=registry),
                               user_repo=user_repo, account_repo=account_repo, **container.agent_services())
    smart = (await factory.ensure_agents_for_user(user_id))["smart_agent"]
    dispatcher = DryRunDispatch(DelegationEngine.dispatch, fixture_engine(user_id, drive, search),
                                frozenset(_READ_ONLY_INTENTS))

    async def guarded(self: DelegationEngine, tool_call: Any, context: Dict[str, Any], *a: Any, **kw: Any) -> ToolResult:
        return await dispatcher(self, tool_call, context, *a, **kw)

    DelegationEngine.dispatch = guarded
    rows: List[Dict[str, Any]] = []
    models: set = set()
    smart_llm = smart._call_llm

    async def capturing(request: Any, *a: Any, **kw: Any) -> Any:
        models.add(request.model_name)   # Smart's tier resolves per request: pin the models that ran
        return await smart_llm(request, *a, **kw)

    smart._call_llm = capturing
    for repeat in range(args.repeats):
        for case in gold:
            dispatcher.calls.clear()
            msg = AgentMessage.create(
                sender="drive_g3_dry_run", recipient=smart.agent_id, intent=AgentIntent.QUERY,
                payload={"text": case["text"]},
                context={"user_id": user_id, "account_id": account_id,
                         "session_id": f"{user_id}:drive_g3_dry_run:{case['id']}:{repeat}",
                         "current_message_parts": [MessagePart(text=case["text"])]})
            async with RequestContext(user_id=user_id, account_id=account_id):
                response = await smart.execute(msg)
            row = {"id": case["id"], "set": case["set"], "category": case["category"], "repeat": repeat,
                   "searched": searched(dispatcher.calls), "calls": list(dispatcher.calls),
                   "answer": str(response.result)[:4000]}
            rows.append(row)
            print(f"{'S' if row['searched'] else '-'} [{case['set'][:3]}] {case['id']}: "
                  f"{[c['intent'] for c in row['calls']]}")
    model = ",".join(sorted(models))
    table = score(rows)
    for scope, line in table.items():
        print(f"{scope:<32} {int(line['searched']):>3}/{int(line['runs']):<3} rate = {line['rate']:.2f}")
    _OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    (_OUT / f"g3_{stamp}.json").write_text(json.dumps({"model": model, "scores": table, "rows": rows},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
    if args.record:
        bars = make_g3_bars(table, gold_sha, model, args.repeats, datetime.now(timezone.utc).isoformat())
        args.bars.write_text(json.dumps(bars, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("verdict: recorded")
        return EXIT_OK
    if not args.bars.exists():
        raise G3Error(f"no bars at {args.bars}; run with --record first")
    verdict = judge(table, json.loads(args.bars.read_text(encoding="utf-8")), gold_sha, model)
    print(f"verdict: {verdict}")
    return EXIT_OK if verdict == "pass" else EXIT_REGRESSION


def main(argv: Optional[Sequence[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(_REPO / ".env")
    try:
        return asyncio.run(_run(_args(argv)))
    except G3Error as exc:
        print(f"drive G3: no verdict — {exc}", file=sys.stderr)
        return EXIT_SETUP
    except Exception as exc:   # noqa: BLE001 — content ports or credentials missing: no verdict, never a bar
        print(f"drive G3: no verdict — {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
