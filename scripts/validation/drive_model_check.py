"""
Live model check for the drive reader and triage (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.3, §24.5, §24.12; D7).
OWNER-RUN: every call is paid (≈ $0.0006 a page). Runs the production runners locally, side by side per provider,
on the owner's real files; nothing is billed to an account and nothing is written to Firestore.

    venv/bin/python -m scripts.validation.drive_model_check read <folder> [--providers claude,openai]
    venv/bin/python -m scripts.validation.drive_model_check triage <renditions dir> [--providers claude,openai]
                                                           [--sizes medium,large]

read    Every PDF and image directly in <folder> (files the owner copied out of the drive, or the c2048x2048
        renditions saved by scripts/onedrive/probe_renditions.py) through DriveReaderRunner. PDFs are cut by the
        child-process parser (SubprocessContentExtractor.split_pdf), as on the indexer.
triage  The renditions saved by probe_renditions.py (`<n>_<size>.<ext>`), per size, in batches of TRIAGE_BATCH
        through DriveTriageRunner: the 176 px vs 800 px question of §24.5.

Prompts come from firestore_utils/uploads (assembled offline by the real PromptAssemblyService, so a prompt can be
tried before it is uploaded) or, with --prompts firestore, from the live prompt collections (read-only).
Keys: ANTHROPIC_API_KEY / OPENAI_API_KEY from .env. Output (file text, briefs, captions: PII) goes only to
scripts/memory/drive_model_check_<stamp>/ (gitignored); the console shows counts, seconds and cost.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock

from dotenv import load_dotenv

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.drive_model_setup import build_drive_model_registry
from src.composition.drive_reader_runner import DriveReaderRunner, PdfSplitter
from src.composition.drive_triage_runner import DriveTriageRunner
from src.composition.prompt_assembly_setup import build_prompt_assembly_service
from src.config.environment import EnvironmentConfig
from src.domain.drive_extraction import DriveModelUnavailable, ExtractionRequest, TriageImage
from src.domain.drive_index_params import TRIAGE_BATCH
from src.domain.user import UserBotConfig
from src.ports.quota_service import QuotaService
from src.services.agent_context_builder import AgentContextBuilder
from src.services.prompt_builder import PromptBuilder
from src.services.provider_registry import ProviderRegistry

_REPO = Path(__file__).resolve().parents[2]
MEMORY = _REPO / "scripts" / "memory"
UPLOADS = _REPO / "firestore_utils" / "uploads"
_KINDS = {".pdf": ("pdf", "application/pdf"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
          ".png": ("image", "image/png"), ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"),
          ".heic": ("image", "image/heic")}
_RENDITION = re.compile(r"^(?P<item>.+)_(?P<size>[A-Za-z0-9]+)\.(?:jpg|jpeg|png)$")


class CostMeter(QuotaService):
    """Collects what each call would bill; no account is charged."""

    def __init__(self) -> None:
        self.calls = 0
        self.cost = 0.0
        self.tokens = 0

    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        self.calls += 1
        self.cost += cost
        self.tokens += tokens


class _Users:
    """One synthetic user whose configuration pins both drive agents to `provider`."""

    def __init__(self, provider: str) -> None:
        self.config = UserBotConfig(agent_providers={"drive_reader": provider, "drive_triage": provider})

    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(config=self.config)


def local_assembly_service(uploads: Path) -> Any:
    """The real PromptAssemblyService over a read-only fake Firestore serving the upload files."""
    env = EnvironmentConfig()
    docs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in uploads.glob("*.json")}
    served = {f"{env.domain_prompt_tokens_collection}_system", env.domain_prompt_blueprints_collection,
              env.domain_prompt_profiles_collection}

    def collection(name: str) -> Any:
        col = MagicMock()

        def document(doc_id: str) -> Any:
            data = docs.get(doc_id) if name in served else None
            snap = MagicMock(exists=data is not None)
            snap.to_dict.return_value = data
            ref = MagicMock()
            ref.get = AsyncMock(return_value=snap)
            return ref

        col.document.side_effect = document
        return col

    db = MagicMock()
    db.collection.side_effect = collection
    return build_prompt_assembly_service(db, env)[0]


def firestore_assembly_service() -> Any:
    from google.cloud import firestore
    db = firestore.AsyncClient(project=os.environ["GOOGLE_CLOUD_PROJECT"],
                               database=os.environ.get("FIRESTORE_DATABASE", "us-production"))
    return build_prompt_assembly_service(db, EnvironmentConfig())[0]


def _context_builder(registry: ProviderRegistry) -> AgentContextBuilder:
    return AgentContextBuilder(registry, resilience_port=InMemoryProviderResilience())


async def check_read(folder: Path, providers: Sequence[str], *, registry: ProviderRegistry, assembly: Any,
                     splitter: PdfSplitter, out_dir: Path) -> Dict[str, Any]:
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _KINDS)
    summary: Dict[str, Any] = {}
    for provider in providers:
        meter = CostMeter()
        runner = DriveReaderRunner(_context_builder(registry), _Users(provider), PromptBuilder(repo=None, assembly_service=assembly),
                                   splitter, quota_service=meter)
        target = out_dir / "read" / provider
        target.mkdir(parents=True, exist_ok=True)
        rows: List[Dict[str, Any]] = []
        for path in files:
            kind, mime = _KINDS[path.suffix.lower()]
            started = time.monotonic()
            request = ExtractionRequest(path=str(path), name=path.name, kind=kind, mime_type=mime,
                                        user_id="model-check", account_id="model-check", max_model_pages=10_000)
            try:
                outcome = await runner.extract(request)
            except DriveModelUnavailable as exc:
                rows.append({"file": path.name, "status": "model_unavailable", "detail": str(exc)})
                continue
            (target / f"{path.stem}.txt").write_text(outcome.text, encoding="utf-8")
            brief = asdict(outcome.brief) if outcome.brief else None
            if brief and brief.get("doc_date"):
                brief["doc_date"] = brief["doc_date"].date().isoformat()
            (target / f"{path.stem}.brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=1),
                                                            encoding="utf-8")
            rows.append({"file": path.name, "status": outcome.status.value, "pages": outcome.pages_total,
                         "pages_sent": outcome.pages_sent, "calls": outcome.model_calls,
                         "cost": round(outcome.model_cost, 6), "truncated": outcome.truncated,
                         "chars": len(outcome.text), "seconds": round(time.monotonic() - started, 1),
                         "detail": outcome.detail})
        summary[provider] = {"files": rows, "calls": meter.calls, "cost": round(meter.cost, 6),
                             "tokens": meter.tokens}
        print(f"[read] {provider}: {len(files)} files, {meter.calls} calls, ${meter.cost:.4f}", flush=True)
    (out_dir / "read" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
    return summary


def renditions(folder: Path, sizes: Sequence[str]) -> Dict[str, List[Path]]:
    """`<item>_<size>.<ext>` files grouped by size, in item order."""
    by_size: Dict[str, List[Path]] = {size: [] for size in sizes}
    for path in sorted(folder.iterdir()):
        m = _RENDITION.match(path.name)
        if m and m.group("size") in by_size:
            by_size[m.group("size")].append(path)
    return by_size


async def check_triage(folder: Path, providers: Sequence[str], sizes: Sequence[str], *, registry: ProviderRegistry,
                       assembly: Any, out_dir: Path) -> Dict[str, Any]:
    groups = renditions(folder, sizes)
    table: Dict[str, Dict[str, Any]] = {}
    totals: Dict[str, Any] = {}
    for provider in providers:
        for size, paths in groups.items():
            meter = CostMeter()
            runner = DriveTriageRunner(_context_builder(registry), _Users(provider), PromptBuilder(repo=None, assembly_service=assembly),
                                       quota_service=meter)
            images = [TriageImage(item_id=_RENDITION.match(p.name).group("item"),  # type: ignore[union-attr]
                                  thumbnail=p.read_bytes(), mime_type="", camera_prior=False) for p in paths]
            for start in range(0, len(images), TRIAGE_BATCH):
                try:
                    batch = await runner.triage("model-check", "model-check", images[start:start + TRIAGE_BATCH])
                except DriveModelUnavailable as exc:
                    print(f"[triage] {provider}/{size}: model unavailable: {exc}", flush=True)
                    break
                for r in batch.results:
                    table.setdefault(r.item_id, {})[f"{provider}/{size}"] = {"verdict": r.verdict.value,
                                                                            "caption": r.caption}
            totals[f"{provider}/{size}"] = {"images": len(images), "calls": meter.calls,
                                            "cost": round(meter.cost, 6)}
            print(f"[triage] {provider}/{size}: {len(images)} images, {meter.calls} calls, ${meter.cost:.4f}",
                  flush=True)
    target = out_dir / "triage"
    target.mkdir(parents=True, exist_ok=True)
    (target / "verdicts.json").write_text(json.dumps({"totals": totals, "items": table}, ensure_ascii=False,
                                                     indent=1), encoding="utf-8")
    return {"totals": totals, "items": table}


def _args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_model_check")
    parser.add_argument("mode", choices=["read", "triage"])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--providers", default="claude,openai")
    parser.add_argument("--sizes", default="medium,large")
    parser.add_argument("--prompts", choices=["local", "firestore"], default="local")
    return parser.parse_args(argv)


async def run(argv: Optional[List[str]] = None, *, keys: Optional[Mapping[str, Optional[str]]] = None,
              registry: Optional[ProviderRegistry] = None, assembly: Any = None,
              splitter: Optional[PdfSplitter] = None, memory: Path = MEMORY) -> Path:
    args = _args(argv)
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    registry = registry or build_drive_model_registry(keys or os.environ)
    if assembly is None:
        assembly = local_assembly_service(UPLOADS) if args.prompts == "local" else firestore_assembly_service()
    out_dir = memory / f"drive_model_check_{datetime.now():%Y%m%d%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "read":
        if splitter is None:
            from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
            splitter = SubprocessContentExtractor()
        await check_read(args.folder, providers, registry=registry, assembly=assembly, splitter=splitter,
                         out_dir=out_dir)
    else:
        await check_triage(args.folder, providers, [s.strip() for s in args.sizes.split(",") if s.strip()],
                           registry=registry, assembly=assembly, out_dir=out_dir)
    print(f"Output → {out_dir}", flush=True)
    return out_dir


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(run(sys.argv[1:]))
