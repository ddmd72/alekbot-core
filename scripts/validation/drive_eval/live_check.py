"""
Live check before deploy (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §22 step 2, §24.12). The real tick, parser, reader,
triage and embeddings, and the real Firestore repository, under a synthetic user id (`zz-synthetic-…`), over a local
folder of real files the owner prepared (a scan, a broken-font Cyrillic PDF, screenshots, a photo folder; JPEG/PNG,
since HEIC renditions come from Graph). It prints each query's result as Smart reads it, then turns indexing off and
ticks again, so the production purge removes the synthetic user's records, chunks and sync state, and checks that
nothing is left. Real model and embedding calls (cents); live Firestore writes under the synthetic id only. Keep the
folder and the report in the gitignored scripts/memory/.

    venv/bin/python -m scripts.validation.drive_eval.live_check --folder scripts/memory/drive_live \\
        --user zz-synthetic-20261014 --query "electricity bill" --query "рахунок 1 205,28"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.domain.drive_index_planner import PlaceOf
from src.domain.drive_search import render_results
from src.domain.user import UserBotConfig, UserProfile
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.image_triage_port import ImageTriagePort
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_search_service import DriveSearchService

from .fixture_drive import FixtureDrive
from .runner import EvalClock, _RecordingQueue, index_fixture, request_for

SYNTHETIC_PREFIX = "zz-synthetic-"
MAX_PURGE_TICKS = 50
_EXIF_TAKEN, _EXIF_MAKE, _EXIF_MODEL, _EXIF_IFD = 36867, 271, 272, 0x8769


class LiveCheckError(RuntimeError):
    """The check refused to run or found something left after the purge."""


def _photo_facets(path: Path) -> Dict[str, Any]:
    """Graph's `photo`/`image` facets from EXIF, so the camera prior and the photo date work as they would live."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            exif = img.getexif()
            facets: Dict[str, Any] = {"image": {"width": img.width, "height": img.height}}
            taken = exif.get_ifd(_EXIF_IFD).get(_EXIF_TAKEN) or exif.get(306)
            photo = {k: v for k, v in (("cameraMake", exif.get(_EXIF_MAKE)), ("cameraModel", exif.get(_EXIF_MODEL)))
                     if v}
            if taken:
                photo["takenDateTime"] = datetime.strptime(str(taken), "%Y:%m:%d %H:%M:%S").strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            if photo:
                facets["photo"] = photo
            return facets
    except Exception:   # noqa: BLE001 — a picture PIL cannot open simply has no facets
        return {}


def manifest_from_folder(root: Path) -> Dict[str, Any]:
    """The eval manifest of a local folder: every file served with its own bytes, dated by its mtime."""
    items: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if path.is_dir():
            items.append({"path": rel, "folder": True, "fs_modified": modified})
            continue
        mime = mimetypes.guess_type(path.name)[0] or ""
        facets = _photo_facets(path) if mime.startswith("image/") else {}
        items.append({"path": rel, "folder": False, "mime": mime, "size": 0, "fs_modified": modified,
                      "facets": facets, "content": rel})
    return {"items": items, "generate": []}


@dataclass
class _Users:
    """The synthetic user, indexing on until the purge."""
    user_id: str
    enabled: bool = True

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        if user_id != self.user_id:
            return None
        return UserProfile(user_id=user_id, account_id=f"{self.user_id}-account",
                           config=UserBotConfig(drive_indexing_enabled=self.enabled))


@dataclass
class LiveReport:
    indexed: Dict[str, int] = field(default_factory=dict)
    summary: Dict[str, int] = field(default_factory=dict)
    answers: List[Dict[str, Any]] = field(default_factory=list)
    purged: bool = False


async def live_check(folder: Path, user_id: str, queries: Sequence[Mapping[str, Any]], *,
                     repo: DriveIndexRepository, embeddings: EmbeddingService, embedding_space: str,
                     place_of: PlaceOf, extractors: Optional[Mapping[str, ContentExtractorPort]] = None,
                     triage: Optional[ImageTriagePort] = None, throttle_pause_s: float = 30.0) -> LiveReport:
    if not user_id.startswith(SYNTHETIC_PREFIX):
        raise LiveCheckError(f"the live check writes only under a {SYNTHETIC_PREFIX}… user id, not {user_id!r}")
    if await repo.get_sync_state(user_id) is not None:
        raise LiveCheckError(f"{user_id} already has an index; pick a new synthetic id")
    drive = FixtureDrive(manifest_from_folder(folder), base_dir=folder)
    users = _Users(user_id)
    clock = EvalClock(datetime.now(timezone.utc))
    report = LiveReport()
    try:
        indexed = await index_fixture(drive, repo, embeddings, place_of, embedding_space, clock,
                                      throttle_pause_s=throttle_pause_s, extractors=extractors, triage=triage,
                                      user_id=user_id, users=users)
        report.indexed, report.summary = indexed.records, indexed.summary
        search = DriveSearchService(repo, embeddings, users, drive, clock=clock, embedding_space=embedding_space)
        for query in queries:
            result = await search.search(user_id, request_for(dict(query)))
            report.answers.append({"query": dict(query), "status": result.status,
                                   "paths": [c.path for c in result.cards], "text": render_results(result)})
    finally:
        await _purge(drive, repo, embeddings, users, place_of, embedding_space, clock, extractors, triage)
    report.purged = True
    return report


async def _purge(drive: FixtureDrive, repo: DriveIndexRepository, embeddings: EmbeddingService, users: _Users,
                 place_of: PlaceOf, embedding_space: str, clock: EvalClock,
                 extractors: Optional[Mapping[str, ContentExtractorPort]], triage: Optional[ImageTriagePort]) -> None:
    """Indexing off, then the production gate purges (§7.6); checked down to the last chunk."""
    users.enabled = False
    content: Dict[str, Any] = {}
    if extractors is not None:
        content["extractors"] = extractors
    if triage is not None:
        content["triage"] = triage
    service = DriveIndexingService(drive, repo, embeddings, _RecordingQueue(), users, place_of, embedding_space,
                                   clock=clock, **content)
    for _ in range(MAX_PURGE_TICKS):
        if await repo.get_sync_state(users.user_id) is None and not await repo.records_page(users.user_id, None, 1):
            break
        await service.tick(users.user_id)
        clock.advance(60)
    left = {
        "sync_state": await repo.get_sync_state(users.user_id) is not None,
        "records": bool(await repo.records_page(users.user_id, None, 1)),
        "chunks": bool(await repo.chunks_page(users.user_id, None, 1)),
    }
    if any(left.values()):
        raise LiveCheckError(f"the purge left data under {users.user_id}: {left}")


def _query(text: str) -> Dict[str, Any]:
    return {"query": text, "filters": {}}


async def _main(args: argparse.Namespace) -> int:
    """Real Firestore, embeddings and content ports: the owner's run."""
    from google.cloud import firestore

    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    from src.composition.drive_indexing_setup import build_drive_index_repository, build_place_lookup
    from src.config.settings import load_settings

    from . import content_wiring

    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    repo = build_drive_index_repository(db, load_settings()["ENVIRONMENT_CONFIG"])
    extractors, triage = content_wiring.build_real_content()
    report = await live_check(args.folder, args.user, [_query(q) for q in args.query], repo=repo,
                              embeddings=GeminiEmbeddingAdapter(api_key=os.environ["GEMINI_API_KEY"]),
                              embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE,
                              place_of=build_place_lookup(), extractors=extractors, triage=triage)
    print(f"indexed: {report.indexed}\nsummary: {report.summary}")
    for answer in report.answers:
        print(f"\n=== {answer['query']['query']!r} → {answer['status']}\n{answer['text']}")
    out = args.report or Path("scripts/memory") / f"drive_live_check_{args.user}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.__dict__, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\npurged: {report.purged} (report: {out})")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.live_check")
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--user", required=True, help=f"a synthetic id starting with {SYNTHETIC_PREFIX}")
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    try:
        return asyncio.run(_main(args))
    except Exception as exc:   # noqa: BLE001 — any failure is reported, never a half-written index left silently
        print(f"drive live check: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
