"""
Card-accuracy revision (USER_DRIVE_INDEXING_RFC §26.6, D37). Owner-run and read-only: it reads the user's drive index
records (and the stored chunks of brief-only cards, whose brief is graded against them) and writes nothing but its
report. It lists:
  1. the lowest-scoring cards (below --below, default 90) with their graded values;
  2. `not_in_text` values grouped by field and by the shape of their printed form, with the number of files;
  3. score distributions by doc type, capture, extractor and fallback use;
  4. with --weights <json file>, the same cards re-scored under a candidate weight table (tables as in
     `src/domain/drive_accuracy.py`: field_weights, grade_miss, source_share, reading_points; an omitted table or key
     keeps the current value; unknown keys are refused), old vs new per card and the distribution deltas.

The report names the user's files: it is written only under `scripts/memory/` (gitignored, Δ67).

Usage (project and credentials from the environment / ADC; collections from APP_ENV, default development):
    venv/bin/python -m scripts.validation.drive_card_review --user <user_id> [--below 90] [--weights <file.json>]
                                                            [--out scripts/memory/<file>.md]
Env:
    GOOGLE_CLOUD_PROJECT     GCP project (optional if ADC carries one)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from dotenv import load_dotenv

from scripts.validation.drive_card_review_logic import (
    DEFAULT_BELOW, ReviewCard, WeightsError, WeightTables, build_card, compare, default_tables, load_tables,
    render_report, rescore,
)
from src.domain.drive_accuracy import card_graded
from src.domain.drive_enrichment import GradedValue, ProfileSource
from src.domain.drive_index import DriveChunk, DriveIndexRecord
from src.ports.drive_index_repository import DriveIndexRepository
from src.utils.logger import logger

_REPO = Path(__file__).resolve().parents[2]
_MEMORY_DIR = _REPO / "scripts" / "memory"
_PAGE = 300
_CHUNK_READ_CAP = 400       # chunks of one brief-only file read to grade its brief


def _brief_only(rec: DriveIndexRecord) -> bool:
    """The score reads the reader's brief against the stored text, not a model profile's stored grades."""
    return rec.brief is not None and (rec.profile is None or rec.profile_source == ProfileSource.TEMPLATE.value)


async def _graded_of(repo: DriveIndexRepository, user_id: str, rec: DriveIndexRecord):
    chunks: List[DriveChunk] = []
    if _brief_only(rec) and rec.indexed_generation is not None:
        if rec.chunk_count > _CHUNK_READ_CAP:
            logger.warning("[DriveCardReview] %s has %d chunks; the first %d read", rec.item_id, rec.chunk_count,
                           _CHUNK_READ_CAP)
        chunks = await repo.get_chunks(user_id, rec.item_id, rec.indexed_generation,
                                       list(range(min(rec.chunk_count, _CHUNK_READ_CAP))))
        chunks.sort(key=lambda c: c.index)
    return card_graded(rec, chunks)


async def collect_cards(repo: DriveIndexRepository, user_id: str) -> List[ReviewCard]:
    """Every record that carries a score, as a review card. Reads only."""
    cards: List[ReviewCard] = []
    after: Optional[str] = None
    seen = 0
    while True:
        page = await repo.records_page(user_id, after, _PAGE)
        if not page:
            break
        for rec in page:
            seen += 1
            graded: Optional[Sequence[GradedValue]] = await _graded_of(repo, user_id, rec)
            if graded is not None:
                cards.append(build_card(rec, graded))
        after = page[-1].item_id
    logger.info("[DriveCardReview] %d records, %d scored cards", seen, len(cards))
    return cards


def review(cards: Sequence[ReviewCard], user_id: str, below: int, tables: Optional[WeightTables] = None,
           weights_name: str = "") -> str:
    """The report for a snapshot: current scores, and the candidate comparison when `tables` is given."""
    current = default_tables()
    old = {c.item_id: rescore(c, current) for c in cards}
    stale = sum(1 for c in cards if c.stored_score is not None and c.stored_score != old[c.item_id])
    comparison = None
    if tables is not None:
        comparison = compare(cards, old, {c.item_id: rescore(c, tables) for c in cards})
    return render_report(user_id=user_id, below=below, cards=cards, scores=old, stale_stored=stale,
                         comparison=comparison, weights_name=weights_name)


def resolve_out(out: Optional[Path], user_id: str) -> Path:
    """The report path, always under `scripts/memory/` (Δ67): the report names the user's files."""
    path = out or _MEMORY_DIR / f"drive_card_review_{user_id}.md"
    path = path if path.is_absolute() else _REPO / path
    resolved = path.resolve()
    if _MEMORY_DIR.resolve() not in resolved.parents:
        raise SystemExit(f"--out must be under {_MEMORY_DIR} (the report names the user's files)")
    return resolved


def read_weights(path: Path) -> WeightTables:
    try:
        return load_tables(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, WeightsError) as exc:
        raise SystemExit(f"--weights {path}: {exc}")


async def _main(args: argparse.Namespace) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    out = resolve_out(args.out, args.user)
    tables = read_weights(args.weights) if args.weights else None
    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    cards = await collect_cards(repo, args.user)
    report = review(cards, args.user, args.below, tables, args.weights.name if args.weights else "")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def _args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_card_review",
                                     description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose drive index is reviewed")
    parser.add_argument("--below", type=int, default=DEFAULT_BELOW,
                        help=f"list cards scoring below this (default {DEFAULT_BELOW})")
    parser.add_argument("--weights", type=Path, default=None,
                        help="JSON file of a candidate weight table; the cards are re-scored, nothing is written")
    parser.add_argument("--out", type=Path, default=None,
                        help="report path, under scripts/memory/ (default drive_card_review_<user>.md)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    load_dotenv()
    sys.exit(asyncio.run(_main(_args(None))))
