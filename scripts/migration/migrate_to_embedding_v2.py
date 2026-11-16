"""
One-shot migration: re-embed every stored vector using gemini-embedding-2.

Spec: docs/04_solution_strategy/decisions/embedding_model_migration_v1_to_v2.md.

Strategy per collection:
  - facts (domain_facts_v2)         — inline re-embed: read doc, recompose
                                       text/tags/metadata, batch embed, write back.
  - tasks (task_search_index)       — inline re-embed: read doc, recompose
                                       content/context via TaskIndexingService
                                       static helpers, batch embed, write back.
  (The emails target is gone: the v1 email vectors and their repair were removed, EMAIL_INDEX_V2_RFC slice 10.)

Dry-run is the default. Pass --live to actually write to Firestore. Idempotent:
re-running overwrites v2 vectors with v2 vectors (same task_type=RETRIEVAL_DOCUMENT
text shape), which is a no-op semantically.

Usage:
    python scripts/migration/migrate_to_embedding_v2.py --target facts --env dev
    python scripts/migration/migrate_to_embedding_v2.py --target tasks --env dev --live
    python scripts/migration/migrate_to_embedding_v2.py --target all --env dev --live
"""
import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.services.task_indexing_service import TaskIndexingService
from src.config.settings import load_settings


# ──────────────────────────────────────────────────────────────────────────────
# Text composition helpers
# ──────────────────────────────────────────────────────────────────────────────

def format_metadata_text(metadata: Dict[str, Any]) -> str:
    """Match the production composition used in add_multi_vector_fields.py."""
    if not metadata:
        return ""
    parts = [f"{k}: {v}" for k, v in metadata.items() if v]
    return ". ".join(parts)


def format_tags_text(tags: List[str]) -> str:
    """Match the production composition used in add_multi_vector_fields.py."""
    if not tags:
        return ""
    return ", ".join(tags)


def collection_prefix(env: str) -> str:
    """Return the Firestore collection prefix for an environment."""
    if env == "dev":
        return "development_"
    if env == "prod":
        return ""
    raise ValueError(f"Unknown env: {env!r}. Use 'dev' or 'prod'.")


# ──────────────────────────────────────────────────────────────────────────────
# Stats
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Stats:
    total: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    errors_by_id: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Facts (domain_facts_v2) — inline re-embed
# ──────────────────────────────────────────────────────────────────────────────

async def migrate_facts(
    db: firestore.AsyncClient,
    embedding: GeminiEmbeddingAdapter,
    env: str,
    *,
    limit: Optional[int],
    live: bool,
) -> Stats:
    collection_name = f"{collection_prefix(env)}domain_facts_v2"
    print(f"\n📚 facts → {collection_name} ({'LIVE' if live else 'DRY RUN'})")
    stats = Stats()

    query = db.collection(collection_name)
    if limit is not None:
        query = query.limit(limit)

    docs = [d async for d in query.stream()]
    stats.total = len(docs)
    print(f"   {len(docs)} docs found")

    for i, doc in enumerate(docs, 1):
        data = doc.to_dict()
        text = data.get("text", "") or ""
        tags = data.get("tags", []) or []
        metadata = data.get("metadata", {}) or {}

        tags_text = format_tags_text(tags) or "no tags"
        meta_text = format_metadata_text(metadata) or "no metadata"

        try:
            vectors = await embedding.get_embeddings_batch(
                [text, tags_text, meta_text],
                task_type="RETRIEVAL_DOCUMENT",
            )
        except Exception as exc:
            stats.errors += 1
            stats.errors_by_id.append(doc.id)
            print(f"   ❌ embed failed for {doc.id[:24]}: {exc}")
            continue

        update = {
            "vector": Vector(vectors[0]),
            "tags_vector": Vector(vectors[1]),
            "metadata_vector": Vector(vectors[2]),
        }
        if live:
            try:
                await doc.reference.update(update)
                stats.updated += 1
            except Exception as exc:
                stats.errors += 1
                stats.errors_by_id.append(doc.id)
                print(f"   ❌ write failed for {doc.id[:24]}: {exc}")
        else:
            stats.skipped += 1

        if i % 50 == 0:
            print(f"   progress: {i}/{stats.total}")

    print(
        f"   ✅ facts: updated={stats.updated}, "
        f"skipped(dry-run)={stats.skipped}, errors={stats.errors}"
    )
    return stats


# ──────────────────────────────────────────────────────────────────────────────
# Tasks (task_search_index) — inline re-embed using TaskIndexingService helpers
# ──────────────────────────────────────────────────────────────────────────────

async def migrate_tasks(
    db: firestore.AsyncClient,
    embedding: GeminiEmbeddingAdapter,
    env: str,
    *,
    limit: Optional[int],
    live: bool,
) -> Stats:
    collection_name = f"{collection_prefix(env)}task_search_index"
    print(f"\n✅ tasks → {collection_name} ({'LIVE' if live else 'DRY RUN'})")
    stats = Stats()

    query = db.collection(collection_name)
    if limit is not None:
        query = query.limit(limit)

    docs = [d async for d in query.stream()]
    stats.total = len(docs)
    print(f"   {len(docs)} docs found")

    for doc in docs:
        data = doc.to_dict()
        content_text = task_content_text_from_dict(data)
        context_text = task_context_text_from_dict(data)

        try:
            vectors = await embedding.get_embeddings_batch(
                [content_text, context_text],
                task_type="RETRIEVAL_DOCUMENT",
            )
        except Exception as exc:
            stats.errors += 1
            stats.errors_by_id.append(doc.id)
            print(f"   ❌ embed failed for {doc.id[:24]}: {exc}")
            continue

        update = {
            "content_vector": Vector(vectors[0]),
            "context_vector": Vector(vectors[1]),
        }
        if live:
            try:
                await doc.reference.update(update)
                stats.updated += 1
            except Exception as exc:
                stats.errors += 1
                stats.errors_by_id.append(doc.id)
                print(f"   ❌ write failed for {doc.id[:24]}: {exc}")
        else:
            stats.skipped += 1

    print(
        f"   ✅ tasks: updated={stats.updated}, "
        f"skipped(dry-run)={stats.skipped}, errors={stats.errors}"
    )
    return stats


def task_content_text_from_dict(d: Dict[str, Any]) -> str:
    """Replicate TaskIndexingService._content_text against a raw Firestore dict.

    The TaskSearchEntry stored in Firestore does not preserve `body` or
    `checklist_items`; only `title` is available. For docs missing those fields
    we fall back to title alone — matches the original write path for tasks
    indexed before body/checklist were embedded.
    """
    return d.get("title", "") or ""


def task_context_text_from_dict(d: Dict[str, Any]) -> str:
    """Replicate TaskIndexingService._context_text against a raw Firestore dict."""
    parts: List[str] = []
    list_name = d.get("list_name")
    if list_name:
        parts.append(list_name)
    parts.extend(d.get("tags", []) or [])
    importance = d.get("importance")
    if importance:
        parts.append(str(importance))
    return " ".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    config = load_settings()
    db_id = os.getenv("FIRESTORE_DATABASE", "us-production")
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id,
    )
    embedding = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])

    print("=" * 80)
    print(f"🚀 EMBEDDING v2 MIGRATION  target={args.target}  env={args.env}  "
          f"limit={args.limit}  live={args.live}")
    print(f"   DB: {db_id}")
    print("=" * 80)

    if args.target in ("facts", "all"):
        await migrate_facts(db, embedding, args.env,
                            limit=args.limit, live=args.live)
    if args.target in ("tasks", "all"):
        await migrate_tasks(db, embedding, args.env,
                            limit=args.limit, live=args.live)

    print("\n" + "=" * 80)
    print(f"✅ Migration done (target={args.target})")
    if not args.live:
        print("   This was a DRY RUN. Pass --live to apply.")
    print("=" * 80)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Re-embed every vector with gemini-embedding-2")
    parser.add_argument("--target", choices=["facts", "tasks", "all"], required=True)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N docs per collection (debug)")
    parser.add_argument("--live", action="store_true",
                        help="Apply changes. Without this flag, runs as DRY RUN.")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
