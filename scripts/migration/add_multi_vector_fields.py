"""
Migration: Add metadata_vector and tags_vector to existing facts.

This script:
1. Reads existing facts from Firestore
2. Generates metadata_vector from metadata fields
3. Generates tags_vector from tags array
4. Updates facts in Firestore

Usage:
    python scripts/migration/add_multi_vector_fields.py --limit 10  # Test mode
    python scripts/migration/add_multi_vector_fields.py --live      # Full migration
"""
import asyncio
import sys
import os
import argparse
from typing import List, Dict, Any

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter


def format_metadata_text(metadata: Dict[str, Any]) -> str:
    """Format metadata dict into text for embedding."""
    if not metadata:
        return ""
    
    parts = []
    for key, value in metadata.items():
        if value:  # Skip empty values
            parts.append(f"{key}: {value}")
    
    return ". ".join(parts) if parts else ""


def format_tags_text(tags: List[str]) -> str:
    """Format tags array into text for embedding."""
    if not tags:
        return ""
    
    return ", ".join(tags)


async def migrate_facts(limit: int = None, live: bool = False):
    """Migrate facts to add metadata_vector and tags_vector."""
    
    print("=" * 80)
    print("🚀 MULTI-VECTOR MIGRATION")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = EnvironmentConfig()
    db_id = env_config.firestore_database_id
    collection_name = env_config.domain_facts_collection
    
    print(f"\n📊 Configuration:")
    print(f"   Database: {db_id}")
    print(f"   Collection: {collection_name}")
    print(f"   Mode: {'🔴 LIVE UPDATE' if live else '🟢 DRY RUN'}")
    print(f"   Limit: {limit if limit else 'ALL'}")
    
    # Initialize services
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id
    )
    
    embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])
    
    # Get facts
    print(f"\n📖 Reading facts...")
    query = db.collection(collection_name)
    
    if limit:
        query = query.limit(limit)
    
    docs = [doc async for doc in query.stream()]
    print(f"✅ Found {len(docs)} facts to process")
    
    # Process facts
    stats = {
        "total": len(docs),
        "already_has_metadata_vector": 0,
        "already_has_tags_vector": 0,
        "needs_metadata_vector": 0,
        "needs_tags_vector": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0
    }
    
    print(f"\n🔍 Analyzing facts...")
    
    to_update = []
    for doc in docs:
        data = doc.to_dict()
        fact_id = doc.id
        
        has_metadata_vector = 'metadata_vector' in data
        has_tags_vector = 'tags_vector' in data
        
        if has_metadata_vector:
            stats["already_has_metadata_vector"] += 1
        if has_tags_vector:
            stats["already_has_tags_vector"] += 1
        
        needs_update = False
        
        if not has_metadata_vector:
            stats["needs_metadata_vector"] += 1
            needs_update = True
        
        if not has_tags_vector:
            stats["needs_tags_vector"] += 1
            needs_update = True
        
        if needs_update:
            to_update.append({
                "id": fact_id,
                "ref": doc.reference,
                "metadata": data.get("metadata", {}),
                "tags": data.get("tags", []),
                "has_metadata_vector": has_metadata_vector,
                "has_tags_vector": has_tags_vector
            })
    
    print(f"\n📊 Analysis Results:")
    print(f"   Total facts: {stats['total']}")
    print(f"   Already have metadata_vector: {stats['already_has_metadata_vector']}")
    print(f"   Already have tags_vector: {stats['already_has_tags_vector']}")
    print(f"   Need metadata_vector: {stats['needs_metadata_vector']}")
    print(f"   Need tags_vector: {stats['needs_tags_vector']}")
    print(f"   Facts to update: {len(to_update)}")
    
    if not to_update:
        print(f"\n✅ All facts already have multi-vector fields!")
        return
    
    if not live:
        print(f"\n🟢 DRY RUN MODE - No changes will be made")
        print(f"\n📝 Sample updates:")
        for i, fact in enumerate(to_update[:3], 1):
            print(f"\n   [{i}] Fact: {fact['id'][:20]}...")
            print(f"       Metadata: {fact['metadata']}")
            print(f"       Tags: {fact['tags']}")
            print(f"       Needs metadata_vector: {not fact['has_metadata_vector']}")
            print(f"       Needs tags_vector: {not fact['has_tags_vector']}")
        
        print(f"\n💡 Run with --live to apply changes")
        return
    
    # Generate embeddings
    print(f"\n🧮 Generating embeddings...")
    
    metadata_texts = []
    tags_texts = []
    
    for fact in to_update:
        if not fact["has_metadata_vector"]:
            metadata_text = format_metadata_text(fact["metadata"])
            metadata_texts.append(metadata_text if metadata_text else "no metadata")
        else:
            metadata_texts.append(None)
        
        if not fact["has_tags_vector"]:
            tags_text = format_tags_text(fact["tags"])
            tags_texts.append(tags_text if tags_text else "no tags")
        else:
            tags_texts.append(None)
    
    # Generate embeddings in batches
    print(f"   Generating metadata embeddings...")
    metadata_embeddings = []
    for text in metadata_texts:
        if text is not None:
            emb = await embedding_service.get_embedding(text, task_type="RETRIEVAL_DOCUMENT")
            metadata_embeddings.append(emb)
        else:
            metadata_embeddings.append(None)
    
    print(f"   Generating tags embeddings...")
    tags_embeddings = []
    for text in tags_texts:
        if text is not None:
            emb = await embedding_service.get_embedding(text, task_type="RETRIEVAL_DOCUMENT")
            tags_embeddings.append(emb)
        else:
            tags_embeddings.append(None)
    
    print(f"✅ Embeddings generated!")
    
    # Update facts
    print(f"\n💾 Updating facts in Firestore...")
    
    for i, fact in enumerate(to_update):
        try:
            update_data = {}
            
            if metadata_embeddings[i] is not None:
                update_data["metadata_vector"] = Vector(metadata_embeddings[i])
            
            if tags_embeddings[i] is not None:
                update_data["tags_vector"] = Vector(tags_embeddings[i])
            
            if update_data:
                await fact["ref"].update(update_data)
                stats["updated"] += 1
                
                if (i + 1) % 10 == 0:
                    print(f"   Progress: {i + 1}/{len(to_update)}")
            else:
                stats["skipped"] += 1
        
        except Exception as e:
            print(f"   ❌ Error updating {fact['id']}: {e}")
            stats["errors"] += 1
    
    print(f"\n✅ Migration complete!")
    print(f"\n📊 Final Stats:")
    print(f"   Total processed: {stats['total']}")
    print(f"   Updated: {stats['updated']}")
    print(f"   Skipped: {stats['skipped']}")
    print(f"   Errors: {stats['errors']}")


async def main():
    parser = argparse.ArgumentParser(description="Add multi-vector fields to facts")
    parser.add_argument("--limit", type=int, help="Limit number of facts to process")
    parser.add_argument("--live", action="store_true", help="Apply changes (default: dry run)")
    
    args = parser.parse_args()
    
    await migrate_facts(limit=args.limit, live=args.live)


if __name__ == "__main__":
    asyncio.run(main())
