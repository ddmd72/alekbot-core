"""
Inspect NEW Component-Based Assembly (Session 24 Architecture).

Tests assembly with:
- NEW collection: prompt_components (not facts!)
- 3-level priority: USER > AGENT > SYSTEM
- Fallthrough + Exclusion patterns
- Agent-specific cognitive_process
- Biographical context injection

Usage:
    python scripts/prompt/inspect_component_assembly_v2.py --user-id <uuid> --agent quick
    python scripts/prompt/inspect_component_assembly_v2.py --user-id <uuid> --agent smart
"""

import asyncio
import argparse
import sys
import os
from datetime import datetime
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
    Returns component dict or None if excluded.
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
                return None  # Excluded
            if data.get("text", "").strip():
                return data  # Override with content
            # Fallthrough to AGENT
    
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
            return None  # Excluded
        if data.get("text", "").strip():
            return data  # Override with content
        # Fallthrough to SYSTEM
    
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
            return None  # Excluded
        return data
    
    return None  # Not found


async def get_biographical_context(user_id: str, env_config) -> str:
    """Load biographical context from facts collection."""
    from google.cloud import firestore
    config = load_settings()
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    facts_collection = f"{env_config.firestore_collection_prefix}facts"
    
    # Load facts (any type, limit 100)
    query = db.collection(facts_collection).where(
        filter=firestore.FieldFilter("owner_id", "==", user_id)
    ).where(
        filter=firestore.FieldFilter("is_current", "==", True)
    ).limit(100)
    
    facts = []
    async for doc in query.stream():
        data = doc.to_dict()
        text = data.get('text', '').strip()
        if text:
            facts.append(f"      - {text}")
    
    if facts:
        return "\n".join(facts)
    return "      // No facts found"


async def assemble_prompt(
    user_id: str,
    agent_type: str,
    include_biographical: bool = True
):
    """Assemble prompt from NEW prompt_components collection."""
    
    config = load_settings()
    env_config = EnvironmentConfig()
    
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection_name = f"{env_config.firestore_collection_prefix}prompt_components"
    collection = db.collection(collection_name)
    
    logger.info("=" * 70)
    logger.info(f"🔨 ASSEMBLING PROMPT")
    logger.info("=" * 70)
    logger.info(f"Collection:     {collection_name}")
    logger.info(f"Agent Type:     {agent_type}")
    logger.info(f"User ID:        {user_id}")
    logger.info(f"Biographical:   {include_biographical}")
    logger.info("=" * 70)
    
    # Component IDs in assembly order
    component_ids = [
        "cognitive_process",
        "properties",
        "policies",
        "few_shot_examples",
        "protocols",
        "runtime_rules"
    ]
    
    # Resolve all components
    components = {}
    for component_id in component_ids:
        logger.info(f"\n🔍 Resolving '{component_id}'...")
        component = await resolve_component(collection, component_id, agent_type, user_id)
        if component:
            owner_type = component.get("owner_type")
            owner_value = component.get("owner_value", "")
            owner_label = f"{owner_type}/{owner_value}" if owner_value else owner_type
            logger.info(f"   ✅ {owner_label} (text={len(component.get('text', ''))} chars)")
            components[component_id] = component
        else:
            logger.info(f"   🚫 EXCLUDED or NOT FOUND")
    
    # Load biographical context if requested
    biographical = ""
    if include_biographical:
        logger.info(f"\n📚 Loading biographical context...")
        biographical = await get_biographical_context(user_id, env_config)
        logger.info(f"   ✅ Loaded (text={len(biographical)} chars)")
    
    # Assemble prompt
    logger.info(f"\n🔨 Assembling final prompt...")
    prompt_parts = []
    
    # Header
    prompt_parts.append("class Alek extends Agent {")
    prompt_parts.append("")
    
    # Components in order
    for component_id in component_ids:
        component = components.get(component_id)
        if component:
            text = component.get("text", "").strip()
            scope = component.get("scope")
            
            # Wrap with component_id
            prompt_parts.append(f"  {component_id} {{")
            # Indent content
            for line in text.split("\n"):
                prompt_parts.append(f"    {line}" if line.strip() else "")
            prompt_parts.append("  }")
            prompt_parts.append("")
    
    # Biographical context (if present)
    if biographical:
        prompt_parts.append("  knowledge_base {")
        for line in biographical.split("\n"):
            prompt_parts.append(f"    {line}" if line.strip() else "")
        prompt_parts.append("  }")
        prompt_parts.append("")
    
    # Footer
    prompt_parts.append("}")
    prompt_parts.append("")
    prompt_parts.append("Alek.run()")
    
    assembled = "\n".join(prompt_parts)
    
    # Statistics
    logger.info(f"\n📊 ASSEMBLY STATS:")
    logger.info(f"   Components resolved: {len(components)}/{len(component_ids)}")
    logger.info(f"   Total prompt length: {len(assembled)} chars")
    logger.info(f"   Biographical facts:  {'YES' if biographical else 'NO'}")
    
    # Save report
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H%M%S")
    user_short = user_id[:4]
    report_path = f"reports/prompt/{date_part}-assembly-{agent_type}-{user_short}-{time_part}.md"
    os.makedirs("reports/prompt", exist_ok=True)
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# NEW Component Assembly Report (Session 24)\n\n")
        f.write(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**User ID:** {user_id}\n")
        f.write(f"**Agent Type:** {agent_type}\n")
        f.write(f"**Collection:** {collection_name}\n\n")
        
        f.write("## Resolution Results\n\n")
        for component_id in component_ids:
            component = components.get(component_id)
            if component:
                owner_type = component.get("owner_type")
                owner_value = component.get("owner_value", "")
                owner_label = f"{owner_type}/{owner_value}" if owner_value else owner_type
                text_len = len(component.get("text", ""))
                f.write(f"- ✅ `{component_id}` <- **{owner_label}** ({text_len} chars)\n")
            else:
                f.write(f"- ❌ `{component_id}` <- EXCLUDED/NOT FOUND\n")
        
        f.write(f"\n## Statistics\n\n")
        f.write(f"- Components: {len(components)}/{len(component_ids)}\n")
        f.write(f"- Prompt length: {len(assembled)} chars\n")
        f.write(f"- Biographical: {'YES' if biographical else 'NO'}\n\n")
        
        f.write("## Assembled Prompt\n\n")
        f.write("```groovy\n")
        f.write(assembled)
        f.write("\n```\n")
    
    logger.info(f"\n✅ REPORT SAVED: {report_path}")
    
    # Preview
    print("\n" + "=" * 70)
    print("📝 PROMPT PREVIEW (first 1500 chars):")
    print("=" * 70)
    print(assembled[:1500] + "..." if len(assembled) > 1500 else assembled)
    print("=" * 70)
    
    return assembled


async def main():
    parser = argparse.ArgumentParser(description="Inspect NEW Component Assembly (Session 24)")
    parser.add_argument(
        "--user-id",
        required=False,
        help="User UUID (or use DEV_USER_ID from .env)"
    )
    parser.add_argument(
        "--agent",
        choices=["quick", "smart", "router", "websearch", "consolidation"],
        default="quick",
        help="Agent type (default: quick)"
    )
    parser.add_argument(
        "--no-biographical",
        action="store_true",
        help="Skip biographical context injection"
    )
    
    args = parser.parse_args()
    
    user_id = args.user_id or os.getenv("DEV_USER_ID")
    if not user_id:
        raise ValueError("User ID required: --user-id or DEV_USER_ID in .env")
    
    await assemble_prompt(
        user_id=user_id,
        agent_type=args.agent,
        include_biographical=not args.no_biographical
    )


if __name__ == "__main__":
    asyncio.run(main())
