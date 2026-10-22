"""
Vocabulary misses of drive search (USER_DRIVE_INDEXING_RFC §26.6, Δ66). Owner-run and read-only: it reads the
recorded drive searches (Smart's `search_files_in_drive` delegations, the drive specialist's `search_drive` tool calls)
from BigQuery `prompt_content` and asks the user's drive index (Firestore,
`limit 1` reads) whether each query word is held, and in which class of key. It writes nothing but its report.

For every `drive_query` in the window: the query goes through `query_concepts`; per concept the index is asked, per
stream and key family, for one key (`find_by_about_keys`, `find_by_name_keys`, `find_chunks_by_word_keys`,
`find_hints_by_keys`). Reported per word: how often no file holds it (`df = 0`), only an automatic class holds it,
only a translation class holds it, or it is held exactly. For a search followed by an `open_file_from_drive` in the
same trace (the file the user meant): whether that file covers every concept exactly and, if not, the nearest keys it
holds. A profiler change is made for words that repeat in that list, not for one query.

The report names the user's query words and file ids: it is written only under `scripts/memory/` (gitignored, Δ67).

Usage (project and credentials from the environment / ADC; collections from APP_ENV, default development):
    venv/bin/python -m scripts.validation.drive_query_vocabulary --user <user_id> [--since 2026-10-14]
                                                                  [--until YYYY-MM-DD] [--out scripts/memory/<file>.md]
Env:
    GOOGLE_CLOUD_PROJECT     GCP project (required)
    BIGQUERY_PROMPT_DATASET  dataset (default: alek_observability_dev)
    BIGQUERY_PROMPT_TABLE    table   (default: prompt_content)
Needs the `bq` CLI (gcloud SDK), like scripts/billing/usage_report.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

from scripts.validation.drive_query_vocabulary_logic import (
    STREAM_ABOUT, STREAM_CHUNK, STREAM_HINT, STREAM_NAME, OpenedCase, ProbeGroup, SearchRecord, concepts_of,
    open_verdict, pair_searches, parse_since, probe_plan, render_report, status_of, tally_word,
)
from src.domain.drive_index_lean import ChunkId
from src.domain.drive_match import Concept, SourceClass
from src.domain.drive_tiers import FileKeys
from src.ports.drive_index_repository import DriveIndexRepository
from src.utils.logger import logger

_REPO = Path(__file__).resolve().parents[2]
_MEMORY_DIR = _REPO / "scripts" / "memory"
DEFAULT_SINCE = "2026-10-14"        # the Search v2 deploy; BigQuery keeps 30 days
_CHUNK_READ_CAP = 400               # chunks of one opened file whose keys are read


# -- BigQuery --------------------------------------------------------------------------------------------------------

def _table_fqn() -> str:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    dataset = os.environ.get("BIGQUERY_PROMPT_DATASET", "alek_observability_dev")
    table = os.environ.get("BIGQUERY_PROMPT_TABLE", "prompt_content")
    return f"{project}.{dataset}.{table}"


def build_query(fqn: str, with_until: bool) -> str:
    """Smart's and the drive specialist's rows of the user in the window, for the traces that hold a drive search: only
    the calls that matter (Smart's drive intents before the specialist, the specialist's tools after it)."""
    until = "AND timestamp < TIMESTAMP(@until)" if with_until else ""
    base = (f"user_id = @user AND (agent_type LIKE '%smart%' OR agent_type = 'drive') "
            f"AND timestamp >= TIMESTAMP(@since) {until}")
    return f"""
WITH hits AS (
  SELECT DISTINCT trace_id FROM `{fqn}`
  WHERE {base} AND (tool_calls LIKE '%search_files_in_drive%' OR tool_calls LIKE '%search_drive%')
)
SELECT trace_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', timestamp) AS timestamp, tool_calls
FROM `{fqn}`
WHERE {base} AND trace_id IN (SELECT trace_id FROM hits)
  AND (tool_calls LIKE '%search_files_in_drive%' OR tool_calls LIKE '%open_file_from_drive%'
       OR tool_calls LIKE '%search_drive%' OR tool_calls LIKE '%open_drive_file%')
ORDER BY trace_id, timestamp
"""


def fetch_rows(user_id: str, since: str, until: Optional[str]) -> List[Dict[str, Any]]:
    """Run the query through the `bq` CLI (parameters bound by bq, no string interpolation of the user id)."""
    cmd = ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=200000",
           f"--project_id={os.environ['GOOGLE_CLOUD_PROJECT']}",
           f"--parameter=user:STRING:{user_id}", f"--parameter=since:DATE:{since}"]
    if until:
        cmd.append(f"--parameter=until:DATE:{until}")
    cmd.append(build_query(_table_fqn(), bool(until)))
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"bq query failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout or "[]")


# -- Firestore -------------------------------------------------------------------------------------------------------

async def _held(repo: DriveIndexRepository, user_id: str, group: ProbeGroup) -> bool:
    """One `limit 1` question."""
    if group.stream == STREAM_ABOUT:
        return bool(await repo.find_by_about_keys(user_id, group.keys, 1))
    if group.stream == STREAM_NAME:
        return bool(await repo.find_by_name_keys(user_id, group.keys, 1))
    if group.stream == STREAM_CHUNK:
        return bool(await repo.find_chunks_by_word_keys(user_id, group.keys, 1))
    if group.stream == STREAM_HINT:
        return bool(await repo.find_hints_by_keys(user_id, group.keys, 1))
    raise ValueError(f"unknown stream {group.stream!r}")


class _Prober:
    """Asks the index once per distinct question: the same key list in the same stream is answered from a cache."""

    def __init__(self, repo: DriveIndexRepository, user_id: str) -> None:
        self._repo, self._user = repo, user_id
        self._cache: Dict[Tuple[str, Tuple[str, ...]], bool] = {}
        self.reads = 0

    async def classes_of(self, concept: Concept, name_only: bool) -> List[SourceClass]:
        held: List[SourceClass] = []
        for group in probe_plan(concept, name_only):
            key = (group.stream, group.keys)
            if key not in self._cache:
                self._cache[key] = await _held(self._repo, self._user, group)
                self.reads += 1
            if self._cache[key]:
                held.append(group.source_class)
        return held


async def _file_keys(repo: DriveIndexRepository, user_id: str, item_id: str) -> Optional[FileKeys]:
    """The opened file's record keys and the keys of its current-generation chunks and hint documents."""
    rec = (await repo.get_records(user_id, [item_id])).get(item_id)
    if rec is None:
        return None
    ids: List[ChunkId] = []
    if rec.indexed_generation is not None:
        ids = [(item_id, rec.indexed_generation, i) for i in range(min(rec.chunk_count, _CHUNK_READ_CAP))]
        if rec.chunk_count > _CHUNK_READ_CAP:
            logger.warning("[DriveVocabulary] an opened file has %d chunks; keys of the first %d read",
                           rec.chunk_count, _CHUNK_READ_CAP)
    chunk_keys = await repo.get_chunk_keys(user_id, ids) if ids else {}
    hint_keys = await repo.get_hint_keys(user_id, ids) if ids else {}
    return FileKeys(item_id=item_id, about_keys=frozenset(rec.about_keys), name_keys=frozenset(rec.name_tokens),
                    chunk_keys={k: frozenset(v) for k, v in chunk_keys.items()},
                    hint_keys={k: frozenset(v) for k, v in hint_keys.items()})


# -- the run ---------------------------------------------------------------------------------------------------------

async def measure(repo: DriveIndexRepository, user_id: str, searches: Sequence[SearchRecord]) -> Dict[str, Any]:
    """Tallies per word and the verdict for every opened file. Reads only."""
    prober = _Prober(repo, user_id)
    tallies: Dict[str, Any] = {}
    opened: List[OpenedCase] = []
    scored = skipped = 0
    for s in searches:
        if not s.drive_query:
            skipped += 1
            continue
        concepts, name_only, _, _ = concepts_of(s.drive_query)
        scored += 1
        for concept in concepts:
            tally_word(tallies, concept, status_of(await prober.classes_of(concept, name_only)))
        for item_id in s.opened:
            file = await _file_keys(repo, user_id, item_id)
            opened.append(OpenedCase(s.drive_query, open_verdict(item_id, file, concepts)))
    logger.info("[DriveVocabulary] %d searches scored, %d index reads", scored, prober.reads)
    return {"tallies": tallies, "opened": opened, "scored": scored, "skipped": skipped}


def resolve_out(out: Optional[Path], user_id: str, since: str) -> Path:
    """The report path, always under `scripts/memory/` (Δ67): the report names the user's files."""
    path = (out or _MEMORY_DIR / f"drive_query_vocabulary_{since}.md")
    path = path if path.is_absolute() else _REPO / path
    resolved = path.resolve()
    if _MEMORY_DIR.resolve() not in resolved.parents:
        raise SystemExit(f"--out must be under {_MEMORY_DIR} (the report names the user's files)")
    return resolved


async def _main(args: argparse.Namespace) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    out = resolve_out(args.out, args.user, args.since)
    rows = fetch_rows(args.user, args.since, args.until)
    searches = pair_searches(rows)
    logger.info("[DriveVocabulary] %d rows, %d searches", len(rows), len(searches))
    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    result = await measure(repo, args.user, searches)
    report = render_report(user_id=args.user, since=args.since, until=args.until or "", searches=len(searches),
                           queries_scored=result["scored"], skipped_filter_only=result["skipped"],
                           tallies=result["tallies"], opened=result["opened"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def _args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_query_vocabulary",
                                     description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose searches and index are measured")
    parser.add_argument("--since", type=parse_since, default=DEFAULT_SINCE,
                        help=f"first day, YYYY-MM-DD (default {DEFAULT_SINCE}, the Search v2 deploy; "
                             "BigQuery keeps 30 days)")
    parser.add_argument("--until", type=parse_since, default=None, help="first day NOT included, YYYY-MM-DD")
    parser.add_argument("--out", type=Path, default=None,
                        help="report path, under scripts/memory/ (default drive_query_vocabulary_<since>.md)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    load_dotenv()
    sys.exit(asyncio.run(_main(_args(None))))
