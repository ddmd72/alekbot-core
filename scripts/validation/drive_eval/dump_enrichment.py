"""
Record the enrichment snapshot, or dump one user's enrichment for the owner's check (USER_DRIVE_INDEXING_RFC §25.7,
F-22). OWNER-RUN.

    venv/bin/python -m scripts.validation.drive_eval.dump_enrichment --fixture [--cap-usd 0.30]
    venv/bin/python -m scripts.validation.drive_eval.dump_enrichment --user <user_id>

--fixture   Index the synthetic fixture (tree.json + tree_v2.json) once with the real model runners — profiler,
            passages, triage v2 and the document reader, on OpenAI (gpt-6-luna, the drive agents' default; no Gemini
            fallback, so one model answers every call) — wrapped in the recording ports (enrichment_record.py), and
            write tests/fixtures/drive_eval/enrichment.json. Paid (cents); stops at --cap-usd and then writes nothing.
            The profiler, passages and triage prompts come from firestore_utils/uploads (the drafts, assembled
            offline by the real PromptAssemblyService); everything else, the reader's prompt included, from the live
            prompt collections, read-only. Vectors are lexical (offline): the snapshot holds no vector. A re-record is
            a deliberate commit: the eval's numbers move with it.
--user      Read one user's profiles and hint documents from Firestore (read-only) and write them to the gitignored
            scripts/memory/ for the owner's check (private data: never into the repo).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock

_REPO = Path(__file__).resolve().parents[3]
FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"
UPLOADS = _REPO / "firestore_utils" / "uploads"
MEMORY = _REPO / "scripts" / "memory"
PROVIDER = "openai"
_AGENTS = ("drive_reader", "drive_triage", "drive_profiler", "drive_passages")


def merged_assembly(uploads: Path, real_db: Any) -> Any:
    """The real PromptAssemblyService over a read-only store: the upload files where they exist (the v2 drafts), the
    live prompt collections otherwise (the reader's prompt, shared tokens). Nothing is written."""
    from src.composition.prompt_assembly_setup import build_prompt_assembly_service
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    docs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in uploads.glob("*.json")}
    served = {f"{env.domain_prompt_tokens_collection}_system", env.domain_prompt_blueprints_collection,
              env.domain_prompt_profiles_collection}

    def collection(name: str) -> Any:
        real = real_db.collection(name)
        col = MagicMock()

        def document(doc_id: str) -> Any:
            data = docs.get(doc_id) if name in served else None
            if data is None:
                return real.document(doc_id)
            snap = MagicMock(exists=True)
            snap.to_dict.return_value = data
            ref = MagicMock()
            ref.get = AsyncMock(return_value=snap)
            return ref

        col.document.side_effect = document
        return col

    db = MagicMock()
    db.collection.side_effect = collection
    return build_prompt_assembly_service(db, env)[0]


class _Users:
    """The recording user: indexing on, every drive agent pinned to PROVIDER."""

    async def get_user(self, user_id: str) -> Any:
        from src.domain.user import UserBotConfig, UserProfile

        return UserProfile(user_id=user_id, account_id="eval-account",
                           config=UserBotConfig(agent_providers={a: PROVIDER for a in _AGENTS}))


def real_runners(keys: Dict[str, Optional[str]], assembly: Any, splitter: Any) -> Dict[str, Any]:
    from src.composition import drive_model_setup
    from src.composition.drive_passage_runner import DrivePassageRunner
    from src.composition.drive_profiler_runner import DriveProfilerRunner
    from src.composition.drive_reader_runner import DriveReaderRunner
    from src.composition.drive_triage_runner import DriveTriageRunner
    from src.services.prompt_builder import PromptBuilder

    builder = drive_model_setup.build_drive_context_builder(keys)
    prompts = PromptBuilder(repo=None, assembly_service=assembly)
    users = _Users()
    return {"profiler": DriveProfilerRunner(builder, users, prompts),  # type: ignore[arg-type]
            "passages": DrivePassageRunner(builder, users, prompts),  # type: ignore[arg-type]
            "triage": DriveTriageRunner(builder, users, prompts),  # type: ignore[arg-type]
            "reader": DriveReaderRunner(builder, users, prompts, splitter=splitter)}  # type: ignore[arg-type]


async def record_fixture(cap_usd: float, out: Path) -> int:
    from google.cloud import firestore

    from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
    from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
    from src.composition.drive_indexing_setup import build_place_lookup
    from src.domain.drive_extraction import PARSER, READER
    from src.domain.drive_index import PASSAGES_VERSION, PROFILE_VERSION, TRIAGE_VERSION

    from . import fixture_texts_v2
    from .enrichment_record import CostCapReached, CostMeter, recording_ports
    from .enrichment_snapshot import EnrichmentSnapshot
    from .lexical_embeddings import EMBEDDING_SPACE, LexicalEmbeddings
    from .parse_cache import ParseCache
    from .runner import EvalClock, index_fixture
    from .search_v2_eval import READER_VERSION, v2_drive

    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        print("OPENAI_API_KEY is not set (.env)", file=sys.stderr)
        return 2
    keys = {"OPENAI_API_KEY": os.getenv("OPENAI_API_KEY")}   # no Gemini key: no fallback answers
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
                               database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    parser = SubprocessContentExtractor()
    real = real_runners(keys, merged_assembly(UPLOADS, db), parser)
    if real["reader"].version != READER_VERSION:
        print(f"the reader is at version {real['reader'].version}; search_v2_eval pins {READER_VERSION}",
              file=sys.stderr)
        return 2
    drive = v2_drive(FIXTURES / "tree.json", FIXTURES / "tree_v2.json")
    snapshot = EnrichmentSnapshot()
    meter = CostMeter(cap_usd=cap_usd)
    ports = recording_ports(real, snapshot, meter, path_of=drive.path_of, bulk_of=drive.generated_member,
                            bulk_profile=fixture_texts_v2.bulk_profile)
    try:
        outcome = await index_fixture(
            drive, InMemoryDriveIndexRepository(), LexicalEmbeddings(), build_place_lookup(), EMBEDDING_SPACE,
            EvalClock(), throttle_pause_s=0,
            extractors={PARSER: ParseCache(parser, MEMORY / "drive_eval_parse_cache"), READER: ports["reader"]},
            triage=ports["triage"], profiler=ports["profiler"], passages=ports["passages"])
    except CostCapReached as exc:
        print(f"stopped: {exc}; nothing written", file=sys.stderr)
        return 1
    snapshot.meta = {"recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "provider": PROVIDER, "models": meter.models, "calls": meter.calls,
                     "cost_usd": round(meter.spent, 4), "profile_version": PROFILE_VERSION,
                     "passages_version": PASSAGES_VERSION, "triage_version": TRIAGE_VERSION,
                     "reader_version": READER_VERSION, "ticks": outcome.ticks}
    snapshot.save(out)
    print(f"recorded {len(snapshot.profiles)} profiles, {len(snapshot.hints)} chunks' hints, "
          f"{len(snapshot.photos)} pictures, {len(snapshot.reader)} reader files; {meter.calls} calls, "
          f"${meter.spent:.4f} → {out}")
    return 0


async def dump_user(user_id: str) -> int:
    """Read-only: every record with a profile or photo fields, and its readable generation's hint documents."""
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    out: Dict[str, Any] = {"user_id": user_id, "records": []}
    after: Optional[str] = None
    while True:
        page = await repo.records_page(user_id, after, 200)
        for rec in page:
            if rec.profile is None and not rec.photo_description:
                continue
            hints = (await repo.get_hints(user_id, rec.item_id, rec.indexed_generation)
                     if rec.indexed_generation else [])
            out["records"].append({"item_id": rec.item_id, "name": rec.name, "kind": rec.kind,
                                   "profile": asdict(rec.profile) if rec.profile else None,
                                   "photo_description": rec.photo_description,
                                   "hints": [asdict(h) for h in hints]})
        if len(page) < 200:
            break
        after = page[-1].item_id
    MEMORY.mkdir(parents=True, exist_ok=True)
    path = MEMORY / f"drive_enrichment_{user_id[:8]}_{datetime.now():%Y%m%d%H%M%S}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"{len(out['records'])} described records → {path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.dump_enrichment")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true", help="record the synthetic fixture's snapshot (paid)")
    mode.add_argument("--user", help="dump one user's enrichment to scripts/memory/ (read-only)")
    parser.add_argument("--cap-usd", type=float, default=0.30)
    parser.add_argument("--out", type=Path, default=FIXTURES / "enrichment.json")
    args = parser.parse_args(argv)
    load_dotenv(_REPO / ".env")
    if args.fixture:
        return asyncio.run(record_fixture(args.cap_usd, args.out))
    return asyncio.run(dump_user(args.user))


if __name__ == "__main__":
    sys.exit(main())
