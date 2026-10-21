#!/usr/bin/env python3
"""
One-time live-data backfill for the fact S1 defects.

Steps (each independently selectable):
  flags   - SCD2 flags that must agree with `state` (src/domain/fact_lifecycle.py)
  dates   - reported_date stored as a string -> timezone-aware datetime
  vectors - re-embed every fact with version > 1 from its CURRENT text/tags/metadata

Dry-run by default (no writes, no embedding API calls). `--apply` writes. Run `--apply` only AFTER the S1
code is deployed, otherwise later updates re-stale the vectors. Every write carries a precondition on the
`update_time` of the scanned snapshot, so a fact changed by the live system since the scan is skipped
(counted as "changed since scan"; re-run to pick it up). The dry run also reports (never fixes) the other
desync classes as `[audit]` lines.
Reads config from env (GOOGLE_CLOUD_PROJECT, FIRESTORE_DATABASE, GEMINI_API_KEY);
load a .env with `--env-file PATH` or FACT_S1_ENV_FILE. Prints no project/user ids or fact text.

Usage:
    python scripts/migration/fact_s1_backfill.py [--step flags|dates|vectors|all] [--limit N] [--apply]
"""

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv  # noqa: E402
from google.api_core.exceptions import FailedPrecondition  # noqa: E402
from src.domain.entities import FactState  # noqa: E402
from src.domain.fact_embedding import fact_embedding_texts  # noqa: E402
from src.domain.fact_lifecycle import lifecycle_fields  # noqa: E402

STEPS = ("flags", "dates", "vectors")
VECTOR_CONCURRENCY = 5


def _state_of(doc: dict[str, Any]) -> Optional[FactState]:
    raw = doc.get("state")
    if raw is None:
        return None
    try:
        return FactState(getattr(raw, "value", raw))
    except ValueError:
        return None


def _stamp(doc: dict[str, Any]) -> Optional[datetime]:
    """last_updated, falling back to created_at."""
    return doc.get("last_updated") or doc.get("created_at")


def select_flags_update(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Update dict that brings is_current/valid_to in line with state, or None."""
    state = _state_of(doc)
    if state not in (FactState.SUPERSEDED, FactState.ARCHIVED, FactState.INVALIDATED):
        return None
    stamp = _stamp(doc)
    if stamp is None:
        return None
    target = lifecycle_fields(state, stamp)
    update: dict[str, Any] = {}
    if doc.get("is_current") is not target["is_current"]:
        # Archived/invalidated stay current by contract; only superseded flips it.
        if state == FactState.SUPERSEDED:
            update["is_current"] = target["is_current"]
    if doc.get("valid_to") is None:
        update["valid_to"] = target["valid_to"]
    return update or None


def select_date_update(doc: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Parsed reported_date when it is stored as a string; None otherwise (or unparseable)."""
    raw = doc.get("reported_date")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return {"reported_date": parsed}


def is_unparseable_date(doc: dict[str, Any]) -> bool:
    return isinstance(doc.get("reported_date"), str) and select_date_update(doc) is None


def select_vector_candidate(doc: dict[str, Any]) -> Optional[list[str]]:
    """The three embedding texts for a fact with version > 1, else None."""
    version = doc.get("version")
    if not isinstance(version, int) or version <= 1:
        return None
    text = doc.get("text")
    if not text:
        return None
    return fact_embedding_texts(text, doc.get("tags"), doc.get("metadata"))


def audit_classes(doc: dict[str, Any]) -> list[str]:
    """Report-only desync classes beyond what the `flags` step fixes (never written)."""
    state = _state_of(doc)
    found: list[str] = []
    if state in (FactState.ARCHIVED, FactState.INVALIDATED) and doc.get("is_current") is False:
        found.append("archived/invalidated with is_current False")
    if state in (FactState.CURRENT, FactState.STALE):
        if doc.get("is_current") is False:
            found.append("current/stale with is_current False")
        if doc.get("valid_to") is not None:
            found.append("current/stale with valid_to set")
    return found


WRITE_OK = "ok"
WRITE_CHANGED = "changed"
WRITE_FAILED = "failed"


async def guarded_update(ref: Any, payload: dict[str, Any], update_time: Any, make_option: Callable[..., Any]) -> str:
    """Write only if the document is unchanged since the scanned snapshot.

    `make_option` is `client.write_option`; a FailedPrecondition means another writer got there first.
    """
    try:
        await ref.update(payload, option=make_option(last_update_time=update_time))
        return WRITE_OK
    except FailedPrecondition:
        return WRITE_CHANGED
    except Exception as exc:  # report, keep going
        print(f"  FAILED {_short(ref.id)}: {type(exc).__name__}")
        return WRITE_FAILED


def _state_name(doc: dict[str, Any]) -> str:
    raw = doc.get("state")
    return str(getattr(raw, "value", raw))


def _short(doc_id: str) -> str:
    return doc_id[:8]


async def _run(args: argparse.Namespace) -> None:
    from google.cloud import firestore

    from src.config.environment import EnvironmentConfig

    env_config = EnvironmentConfig()
    collection = env_config.domain_facts_collection
    database = os.getenv("FIRESTORE_DATABASE") or env_config.firestore_database_id
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("Missing GOOGLE_CLOUD_PROJECT (use --env-file)")

    steps = STEPS if args.step == "all" else (args.step,)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Collection: {collection}\nDatabase:   {database}\nMode:       {mode}\nSteps:      {', '.join(steps)}")

    db = firestore.AsyncClient(project=project, database=database)
    col = db.collection(collection)
    summary: list[str] = []

    fields = ["state", "is_current", "valid_to", "last_updated", "created_at", "reported_date",
              "version", "text", "tags", "metadata"]
    scanned = 0
    # Items are (ref, doc, payload, update_time of the scanned snapshot).
    flags: list[tuple[Any, dict, dict, Any]] = []
    dates: list[tuple[Any, dict, dict, Any]] = []
    bad_dates: list[Any] = []
    vectors: list[tuple[Any, dict, list[str], Any]] = []
    audit: Counter = Counter()
    async for snap in col.select(fields).stream():
        if args.limit and scanned >= args.limit:
            break
        scanned += 1
        doc = snap.to_dict() or {}
        ut = snap.update_time
        for cls in audit_classes(doc):
            audit[cls] += 1
        if "flags" in steps and (u := select_flags_update(doc)):
            flags.append((snap.reference, doc, u, ut))
        if "dates" in steps:
            if u := select_date_update(doc):
                dates.append((snap.reference, doc, u, ut))
            elif is_unparseable_date(doc):
                bad_dates.append(snap.reference)
        if "vectors" in steps and (t := select_vector_candidate(doc)):
            vectors.append((snap.reference, doc, t, ut))
    print(f"Scanned:    {scanned} docs\n")
    if args.apply:
        print("NOTE: --apply must run after the S1 code is deployed, otherwise later updates re-stale vectors.\n")
    for cls in ("archived/invalidated with is_current False", "current/stale with is_current False",
                "current/stale with valid_to set"):
        print(f"[audit] {cls}: {audit.get(cls, 0)}")
    print()

    def report(name: str, items: list, describe) -> None:
        print(f"[{name}] candidates: {len(items)}")
        print(f"[{name}] by state: {dict(Counter(_state_name(i[1]) for i in items))}")
        for ref, doc, payload, _ut in items[:5]:
            print(f"  {_short(ref.id)}  {describe(doc, payload)}")
        print()

    async def apply_updates(name: str, items: list) -> None:
        results = Counter()
        for ref, _doc, payload, ut in items:
            results[await guarded_update(ref, payload, ut, db.write_option)] += 1
        print(f"[{name}] written: {results[WRITE_OK]}, changed since scan, skipped - re-run: "
              f"{results[WRITE_CHANGED]}, failed: {results[WRITE_FAILED]}\n")
        summary.append(f"{name}: {len(items)} candidates, {results[WRITE_OK]} written, "
                       f"{results[WRITE_CHANGED]} changed since scan, {results[WRITE_FAILED]} failed")

    if "flags" in steps:
        report("flags", flags, lambda d, u: ", ".join(f"{k}={v.isoformat() if isinstance(v, datetime) else v}" for k, v in u.items()))
        if args.apply:
            await apply_updates("flags", flags)
        else:
            summary.append(f"flags: {len(flags)} candidates")

    if "dates" in steps:
        report("dates", dates, lambda d, u: f"reported_date -> {u['reported_date'].isoformat()}")
        print(f"[dates] unparseable (skipped): {len(bad_dates)}\n")
        if args.apply:
            await apply_updates("dates", dates)
        else:
            summary.append(f"dates: {len(dates)} candidates, {len(bad_dates)} unparseable")

    if "vectors" in steps:
        report("vectors", vectors, lambda d, t: f"text_len={len(t[0])} version={d.get('version')}")
        if args.apply:
            await _apply_vectors(vectors, summary, db.write_option)
        else:
            summary.append(f"vectors: {len(vectors)} candidates")

    print("SUMMARY: " + "; ".join(summary))


async def _apply_vectors(vectors: list, summary: list[str], make_option: Callable[..., Any]) -> None:
    from google.cloud.firestore_v1.vector import Vector

    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("Missing GEMINI_API_KEY")
    embedder = GeminiEmbeddingAdapter(api_key=api_key)
    sem = asyncio.Semaphore(VECTOR_CONCURRENCY)

    async def one(ref, texts: list[str], update_time: Any) -> str:
        # `texts` come from the scanned snapshot; the precondition proves the doc still has that content.
        async with sem:
            try:
                v, tv, mv = await embedder.get_embeddings_batch(texts, "RETRIEVAL_DOCUMENT")
            except Exception as exc:
                print(f"  FAILED {_short(ref.id)}: {type(exc).__name__}")
                return WRITE_FAILED
            payload = {"vector": Vector(v), "tags_vector": Vector(tv), "metadata_vector": Vector(mv)}
            return await guarded_update(ref, payload, update_time, make_option)

    results = Counter(await asyncio.gather(*(one(ref, t, ut) for ref, _d, t, ut in vectors)))
    print(f"[vectors] written: {results[WRITE_OK]}, changed since scan, skipped - re-run: "
          f"{results[WRITE_CHANGED]}, failed: {results[WRITE_FAILED]}\n")
    summary.append(f"vectors: {len(vectors)} candidates, {results[WRITE_OK]} written, "
                   f"{results[WRITE_CHANGED]} changed since scan, {results[WRITE_FAILED]} failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step", choices=[*STEPS, "all"], default="all")
    parser.add_argument("--limit", type=int, default=0, help="scan at most N docs")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--env-file", default=os.getenv("FACT_S1_ENV_FILE"), help="path to a .env file")
    args = parser.parse_args()
    load_dotenv(args.env_file) if args.env_file else load_dotenv()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
