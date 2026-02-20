"""
Re-seed SYSTEM/few_shot_examples with FULL 32 examples from OLD Smart agent.

This replaces the minimal 2-example version with complete training data:
- 32 examples total (BAD + GOOD + Ranevskaya + Twain + etc)
- 15K+ characters
- Matches OLD Smart agent exactly

Usage:
    python scripts/prompt/reseed_full_examples.py --env development
"""

import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.utils.logger import logger


async def reseed_examples():
    """Delete old SYSTEM/few_shot_examples and create new one with full content."""
    
    # Load full examples from file
    examples_file = "/tmp/full_examples.txt"
    if not os.path.exists(examples_file):
        logger.error(f"❌ Examples file not found: {examples_file}")
        logger.error("   Run this first: grep -A 300 \"few_shot_examples: '''\" reports/prompt/2026-01-29-smart-f1d6-171640.md | grep -v \"^few_shot_examples: '''\" | head -289 > /tmp/full_examples.txt")
        return
    
    with open(examples_file, "r", encoding="utf-8") as f:
        full_text = f.read().strip()
    
    # Clean up
    if full_text.startswith("few_shot_examples: '''"):
        full_text = full_text[len("few_shot_examples: '''"):].strip()
    if full_text.endswith("'''"):
        full_text = full_text[:-3].strip()
    
    # Wrap properly for Groovy
    wrapped_text = f"few_shot_examples: '''\n{full_text}\n'''"
    
    logger.info("=" * 70)
    logger.info("🔄 RE-SEEDING FEW_SHOT_EXAMPLES")
    logger.info("=" * 70)
    logger.info(f"Source: {examples_file}")
    logger.info(f"Text length: {len(wrapped_text)} chars")
    logger.info(f"Examples found: {wrapped_text.count('- id:')}")
    
    # Initialize Firestore
    config = load_settings()
    os.environ["APP_ENV"] = "development"
    env_config = EnvironmentConfig()
    
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection_name = f"{env_config.firestore_collection_prefix}prompt_components"
    collection = db.collection(collection_name)
    
    logger.info(f"Collection: {collection_name}")
    
    # Step 1: Find and delete old SYSTEM/few_shot_examples
    logger.info("\n📌 Step 1: Delete old SYSTEM/few_shot_examples...")
    query = collection.where(
        filter=firestore.FieldFilter("component_id", "==", "few_shot_examples")
    ).where(
        filter=firestore.FieldFilter("owner_type", "==", "SYSTEM")
    )
    
    deleted = 0
    async for doc in query.stream():
        await doc.reference.delete()
        deleted += 1
        logger.info(f"   🗑️  Deleted old document: {doc.id}")
    
    if deleted == 0:
        logger.info("   ℹ️  No old document found (might be first run)")
    
    # Step 2: Create new SYSTEM/few_shot_examples with full content
    logger.info("\n📌 Step 2: Create new SYSTEM/few_shot_examples with FULL 32 examples...")
    
    doc_data = {
        # Identity
        "component_id": "few_shot_examples",
        "owner_type": "SYSTEM",
        "owner_value": None,
        
        # Control
        "is_enabled": True,
        "priority": 100,
        
        # Content (FULL examples from OLD Smart)
        "text": wrapped_text,
        
        # Assembly
        "scope": "class.Alek.knowledge_base",
        "order": 60,
        
        # Metadata
        "version": "2.0",
        "description": "SYSTEM default for few_shot_examples - FULL 32 examples (matches OLD Smart)",
        "created_by": "reseed_full_examples.py",
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP
    }
    
    doc_ref = collection.document()
    await doc_ref.set(doc_data)
    
    logger.info(f"   ✅ Created new document: {doc_ref.id}")
    logger.info(f"   📝 Text length: {len(wrapped_text)} chars")
    logger.info(f"   📚 Examples: {wrapped_text.count('- id:')}")
    
    logger.info("\n" + "=" * 70)
    logger.info("✅ RE-SEED COMPLETE!")
    logger.info("=" * 70)
    logger.info("Both Quick and Smart agents will now use FULL 32 examples")
    logger.info("Same biographical context (100 facts)")
    logger.info("Next: Test with inspect_component_assembly_v2.py")


if __name__ == "__main__":
    asyncio.run(reseed_examples())
