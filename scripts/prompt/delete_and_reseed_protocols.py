"""
Delete old SYSTEM/protocols and re-seed with fixed version (without double wrapper).

Bug: SYSTEM/protocols had double wrapper `protocols { protocols { ... } }` 
Fix: Remove outer wrapper from text, assembly adds it automatically

Usage:
    python3 scripts/prompt/delete_and_reseed_protocols.py
"""

import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.utils.logger import logger


FIXED_PROTOCOLS_TEXT = """/**
 * Protocol for accessing user's long-term memory.
 * MUST be used when user asks about personal data (cars, health, history, etc).
 */
search_memory_protocol {
      when_to_use: "User asks about personal data, preferences, or history."
      actual_tool: "search_memory(query)"
      
      execution_steps: [
        "1. IDENTIFY: Does user_query relate to personal data?",
        "2. FORMULATE: Extract 2-4 specific keywords (English + Russian).",
        "3. EXECUTE: Call 'search_memory(keywords)'.",
        "4. ANALYZE: Do retrieved facts answer the question?",
        "5. SYNTHESIZE: Answer using ONLY retrieved facts. If missing, admit ignorance."
      ]
      
      examples: [
        "Query: 'какая марка моего авто?' -> Call: search_memory(query='Mitsubishi Colt car машина')",
        "Query: 'какой размер перчаток?' -> Call: search_memory(query='glove size перчатки')"
      ]
    }

    /**
     * Protocol for web search via specialized agent.
     * MUST be used for general knowledge, current events, or external facts.
     */
    web_search_protocol {
      when_to_use: "User asks for external info not in memory (news, flights, products, etc)."
      actual_tool: "ask_web_search_agent(query)"
      
      execution_steps: [
        "1. ANALYZE: Extract OBJECT (what) and CRITERIA (conditions) from user query.",
        "2. FORMAT: Construct structured query as 'Object: [what] | Criteria: [conditions]'.",
        "3. EXECUTE: Call 'ask_web_search_agent(query)' and receive response.",
        "4. VERIFY: Check if results match the CRITERIA. If insufficient, note gaps.",
        "5. REFINE: If verification fails, refine query with more specific criteria and retry.",
        "6. COMPILE: Aggregate all valid results from the agent's response.",
        "7. DELIVER: Present the List + Summary structure. Do NOT collapse into single option."
      ]
      
      examples: [
        "User: 'Direct flights Valencia to Krakow this week' -> Tool Query: 'Object: flights Valencia to Krakow | Criteria: direct only, current week'",
        "User: 'Best budget hotels in Barcelona' -> Tool Query: 'Object: hotels in Barcelona | Criteria: budget-friendly, high ratings'"
      ]
    }"""


async def delete_and_reseed():
    """Delete old SYSTEM/protocols and create fixed version."""
    
    config = load_settings()
    os.environ["APP_ENV"] = "development"
    env_config = EnvironmentConfig()
    
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection_name = f"{env_config.firestore_collection_prefix}prompt_components"
    collection = db.collection(collection_name)
    
    logger.info("=" * 70)
    logger.info("🔧 FIX PROTOCOLS DOUBLE WRAPPER BUG")
    logger.info("=" * 70)
    logger.info(f"Collection: {collection_name}")
    
    # Step 1: Find and delete old SYSTEM/protocols
    logger.info("\n📌 Step 1: Delete old SYSTEM/protocols...")
    query = collection.where(
        filter=firestore.FieldFilter("component_id", "==", "protocols")
    ).where(
        filter=firestore.FieldFilter("owner_type", "==", "SYSTEM")
    )
    
    deleted = 0
    async for doc in query.stream():
        old_text = doc.to_dict().get("text", "")
        logger.info(f"   Found document: {doc.id}")
        logger.info(f"   Old text length: {len(old_text)} chars")
        logger.info(f"   Has double wrapper: {'protocols {' in old_text}")
        await doc.reference.delete()
        deleted += 1
        logger.info(f"   🗑️  Deleted")
    
    if deleted == 0:
        logger.warning("   ⚠️  No old SYSTEM/protocols found!")
        return
    
    # Step 2: Create new SYSTEM/protocols with FIXED text (no outer wrapper)
    logger.info("\n📌 Step 2: Create new SYSTEM/protocols (no outer wrapper)...")
    
    doc_data = {
        # Identity
        "component_id": "protocols",
        "owner_type": "SYSTEM",
        "owner_value": None,
        
        # Control
        "is_enabled": True,
        "priority": 100,
        
        # Content (NO outer protocols wrapper - assembly adds it!)
        "text": FIXED_PROTOCOLS_TEXT.strip(),
        
        # Assembly
        "scope": "class.Alek.protocols",
        "order": 70,
        
        # Metadata
        "version": "1.1",
        "description": "SYSTEM default for protocols - FIXED (no double wrapper)",
        "created_by": "delete_and_reseed_protocols.py",
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP
    }
    
    doc_ref = collection.document()
    await doc_ref.set(doc_data)
    
    logger.info(f"   ✅ Created new document: {doc_ref.id}")
    logger.info(f"   📝 Text length: {len(FIXED_PROTOCOLS_TEXT)} chars")
    logger.info(f"   📝 Has outer wrapper: {'protocols {' in FIXED_PROTOCOLS_TEXT[:20]}")
    
    logger.info("\n" + "=" * 70)
    logger.info("✅ FIX COMPLETE!")
    logger.info("=" * 70)
    logger.info("SYSTEM/protocols updated with correct structure (no double wrapper)")
    logger.info("Next: Test Smart agent with `make inspect-smart-new-dev`")


if __name__ == "__main__":
    asyncio.run(delete_and_reseed())
