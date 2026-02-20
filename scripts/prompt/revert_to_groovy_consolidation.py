"""
Revert consolidation agent to Groovy format with improvements.

Updates AGENT/consolidation cognitive_process to use Groovy DSL format
while keeping the improvements (response_format, few-shot examples, structured variables).

Usage:
    python3 scripts/prompt/revert_to_groovy_consolidation.py
"""

import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.utils.logger import logger


async def revert_to_groovy():
    """Revert AGENT/consolidation cognitive_process to Groovy format."""

    config = load_settings()
    os.environ["APP_ENV"] = "development"
    env_config = EnvironmentConfig()

    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection_name = f"{env_config.firestore_collection_prefix}prompt_components"
    collection = db.collection(collection_name)

    logger.info("=" * 70)
    logger.info("🔄 REVERT CONSOLIDATION TO GROOVY FORMAT")
    logger.info("=" * 70)
    logger.info(f"Collection: {collection_name}")

    # Load Groovy file
    groovy_path = "ai_templates/components/agent/consolidation/cognitive_process.groovy"
    if not os.path.exists(groovy_path):
        logger.error(f"❌ File not found: {groovy_path}")
        return

    with open(groovy_path, "r", encoding="utf-8") as f:
        groovy_content = f.read()

    logger.info(f"\n📄 Loaded {groovy_path}")
    logger.info(f"   Content length: {len(groovy_content)} chars")
    logger.info(f"   Has response_format: {'response_format' in groovy_content}")
    logger.info(f"   Has few_shot_examples: {'few_shot_examples' in groovy_content}")

    # Step 1: Find existing AGENT/consolidation cognitive_process
    logger.info("\n📌 Step 1: Find existing AGENT/consolidation cognitive_process...")
    query = collection.where(
        filter=firestore.FieldFilter("component_id", "==", "cognitive_process")
    ).where(
        filter=firestore.FieldFilter("owner_type", "==", "AGENT")
    ).where(
        filter=firestore.FieldFilter("owner_value", "==", "consolidation")
    )

    existing_docs = []
    async for doc in query.stream():
        existing_docs.append(doc)
        logger.info(f"   Found document: {doc.id}")
        doc_data = doc.to_dict()
        logger.info(f"   Current version: {doc_data.get('version', 'unknown')}")
        logger.info(f"   Current text length: {len(doc_data.get('text', ''))} chars")

    # Step 2: Delete existing
    if existing_docs:
        logger.info(f"\n📌 Step 2: Delete {len(existing_docs)} existing document(s)...")
        for doc in existing_docs:
            await doc.reference.delete()
            logger.info(f"   🗑️  Deleted: {doc.id}")
    else:
        logger.info("\n📌 Step 2: No existing documents found")

    # Step 3: Create new document with Groovy content
    logger.info("\n📌 Step 3: Create new AGENT/consolidation cognitive_process (Groovy)...")

    doc_data = {
        # Identity
        "component_id": "cognitive_process",
        "owner_type": "AGENT",
        "owner_value": "consolidation",

        # Control
        "is_enabled": True,
        "priority": 100,

        # Content (Groovy format with response_format and few-shot examples)
        "text": groovy_content,

        # Assembly
        "scope": "CLASS_ROOT",
        "order": 10,

        # Metadata
        "version": "2.2",
        "description": "Consolidation agent cognitive process (Groovy format with response_format and few-shot examples)",
        "created_by": "revert_to_groovy_consolidation.py",
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP
    }

    doc_ref = collection.document()
    await doc_ref.set(doc_data)

    logger.info(f"   ✅ Created new document: {doc_ref.id}")
    logger.info(f"   📝 Text length: {len(groovy_content)} chars")
    logger.info(f"   📝 Version: 2.2")
    logger.info(f"   📝 Format: Groovy DSL")

    logger.info("\n" + "=" * 70)
    logger.info("✅ REVERT COMPLETE!")
    logger.info("=" * 70)
    logger.info("AGENT/consolidation reverted to Groovy format with improvements:")
    logger.info("  - Groovy DSL with class wrapper")
    logger.info("  - response_format section at top")
    logger.info("  - few_shot_examples with real output")
    logger.info("  - Structured variables (XML conversation, JSON anchors)")
    logger.info("\nNext: Restart service and test with production user")


if __name__ == "__main__":
    asyncio.run(revert_to_groovy())
