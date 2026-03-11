"""
Test Component Resolution - verify 3-level priority system works.

Tests:
- Quick agent: should get quick cognitive_process + SYSTEM fallthrough
- Smart agent: should get smart cognitive_process + SYSTEM fallthrough
- User override: should override agent/system

Usage:
    python scripts/prompt/test_component_resolution.py --agent quick
    python scripts/prompt/test_component_resolution.py --agent smart
"""

import asyncio
import argparse
import sys
import os
from typing import Optional, List

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.utils.logger import logger


async def resolve_component(
    collection,
    component_id: str,
    agent_type: str,
    user_id: Optional[str] = None
):
    """
    Resolve component using 3-level priority: USER > AGENT > SYSTEM
    
    Returns component or None if excluded/not found.
    """
    
    # 1. Try USER level
    if user_id:
        query = collection.where(
            filter=firestore.FieldFilter("component_id", "==", component_id)
        ).where(
            filter=firestore.FieldFilter("owner_type", "==", "USER")
        ).where(
            filter=firestore.FieldFilter("owner_value", "==", user_id)
        ).limit(1)
        
        docs = [doc async for doc in query.stream()]
        if docs:
            data = docs[0].to_dict()
            if not data.get("is_enabled", True):
                logger.info(f"   🚫 USER excluded '{component_id}'")
                return None
            if data.get("text", "").strip():
                logger.info(f"   👤 USER override '{component_id}' (text length: {len(data['text'])})")
                return data
            logger.info(f"   👤 USER fallthrough '{component_id}' (empty text)")
    
    # 2. Try AGENT level
    query = collection.where(
        filter=firestore.FieldFilter("component_id", "==", component_id)
    ).where(
        filter=firestore.FieldFilter("owner_type", "==", "AGENT")
    ).where(
        filter=firestore.FieldFilter("owner_value", "==", agent_type)
    ).limit(1)
    
    docs = [doc async for doc in query.stream()]
    if docs:
        data = docs[0].to_dict()
        if not data.get("is_enabled", True):
            logger.info(f"   🚫 AGENT/{agent_type} excluded '{component_id}'")
            return None
        if data.get("text", "").strip():
            logger.info(f"   🤖 AGENT/{agent_type} override '{component_id}' (text length: {len(data['text'])})")
            return data
        logger.info(f"   🤖 AGENT/{agent_type} fallthrough '{component_id}' (empty text)")
    
    # 3. Try SYSTEM level
    query = collection.where(
        filter=firestore.FieldFilter("component_id", "==", component_id)
    ).where(
        filter=firestore.FieldFilter("owner_type", "==", "SYSTEM")
    ).limit(1)
    
    docs = [doc async for doc in query.stream()]
    if docs:
        data = docs[0].to_dict()
        if not data.get("is_enabled", True):
            logger.info(f"   🚫 SYSTEM excluded '{component_id}'")
            return None
        logger.info(f"   ⚙️  SYSTEM default '{component_id}' (text length: {len(data.get('text', ''))})")
        return data
    
    logger.warning(f"   ❌ NOT FOUND '{component_id}'")
    return None


async def test_resolution(env: str, agent_type: str, user_id: Optional[str] = None):
    """Test component resolution for agent."""
    
    config = load_settings()
    os.environ["APP_ENV"] = env
    env_config = EnvironmentConfig()
    
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection_name = f"{env_config.firestore_collection_prefix}prompt_components"
    collection = db.collection(collection_name)
    
    logger.info("=" * 70)
    logger.info(f"🧪 TESTING COMPONENT RESOLUTION")
    logger.info("=" * 70)
    logger.info(f"Environment:  {env}")
    logger.info(f"Collection:   {collection_name}")
    logger.info(f"Agent Type:   {agent_type}")
    logger.info(f"User ID:      {user_id or 'None'}")
    logger.info("=" * 70)
    
    # Test all component IDs
    component_ids = [
        "cognitive_process",
        "properties",
        "policies",
        "few_shot_examples",
        "protocols",
        "runtime_rules"
    ]
    
    results = {}
    
    for component_id in component_ids:
        logger.info(f"\n🔍 Resolving '{component_id}':")
        component = await resolve_component(collection, component_id, agent_type, user_id)
        results[component_id] = component
    
    # Summary
    logger.info("\n" + "=" * 70)
    logger.info("📊 RESOLUTION SUMMARY")
    logger.info("=" * 70)
    
    for component_id, component in results.items():
        if component:
            owner_type = component.get("owner_type")
            owner_value = component.get("owner_value", "")
            owner_label = f"{owner_type}/{owner_value}" if owner_value else owner_type
            scope = component.get("scope")
            order = component.get("order")
            text_len = len(component.get("text", ""))
            
            logger.info(f"✅ {component_id:20} <- {owner_label:15} (scope={scope}, order={order}, text={text_len} chars)")
        else:
            logger.info(f"❌ {component_id:20} <- NOT RESOLVED")
    
    logger.info("=" * 70)
    
    # Build assembled prompt preview
    logger.info("\n📝 ASSEMBLED PROMPT PREVIEW:")
    logger.info("=" * 70)
    
    prompt_parts = []
    prompt_parts.append("class Alek extends Agent {")
    prompt_parts.append("")
    
    for component_id in ["cognitive_process", "properties", "policies", "few_shot_examples", "protocols", "runtime_rules"]:
        component = results.get(component_id)
        if component and component.get("text"):
            text = component.get("text", "").strip()
            # Show first 200 chars
            preview = text[:200] + "..." if len(text) > 200 else text
            prompt_parts.append(f"  // {component_id} ({component.get('owner_type')})")
            prompt_parts.append(f"  {component_id} " + preview)
            prompt_parts.append("")
    
    prompt_parts.append("}")
    prompt_parts.append("")
    prompt_parts.append("Alek.run()")
    
    print("\n".join(prompt_parts))


async def main():
    parser = argparse.ArgumentParser(description="Test component resolution")
    parser.add_argument(
        "--env",
        choices=["development", "production"],
        default="development",
        help="Environment"
    )
    parser.add_argument(
        "--agent",
        choices=["quick", "smart", "router", "websearch", "consolidation"],
        default="quick",
        help="Agent type to test"
    )
    parser.add_argument(
        "--user-id",
        help="Optional user ID for testing USER-level overrides"
    )
    
    args = parser.parse_args()
    
    await test_resolution(args.env, args.agent, args.user_id)


if __name__ == "__main__":
    asyncio.run(main())
