"""
E2E Test: Real Prompt Assembly Flow
====================================

Tests the COMPLETE prompt assembly pipeline using REAL infrastructure:
- UserAgentFactory initialization
- PromptComponentService (3-level resolution)
- PromptBuilder.build_for_agent()
- GroovyPromptAssembler

This uses the ACTUAL production code path.

Output: Clean .groovy file with ONLY the assembled prompt (no metadata).

Usage:
    python scripts/prompt/test_e2e_prompt_assembly.py --user-id <uuid> --agent quick
    python scripts/prompt/test_e2e_prompt_assembly.py --user-id <uuid> --agent smart
    python scripts/prompt/test_e2e_prompt_assembly.py --user-id <uuid> --agent both
    
    # With env variable:
    DEV_USER_ID=<uuid> python scripts/prompt/test_e2e_prompt_assembly.py --agent quick
"""

import asyncio
import argparse
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.composition.user_agent_factory import UserAgentFactory
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.utils.logger import logger


async def test_assembly_e2e(user_id: str, agent_type: str, save_dir: str = "reports/prompt/e2e"):
    """
    E2E test: Use REAL infrastructure to assemble prompt.
    
    Args:
        user_id: User UUID
        agent_type: "quick" or "smart"
        save_dir: Output directory for .groovy files
        
    Returns:
        Assembled prompt string
    """
    logger.info("=" * 70)
    logger.info("🚀 E2E PROMPT ASSEMBLY TEST")
    logger.info("=" * 70)
    logger.info(f"User ID:     {user_id}")
    logger.info(f"Agent Type:  {agent_type}")
    logger.info(f"Output Dir:  {save_dir}")
    logger.info("=" * 70)
    
    # 1. Setup (like in main.py)
    config = load_settings()
    env_config = EnvironmentConfig()
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    logger.info("✅ Config loaded")
    logger.info(f"   Environment: {env_config.env.value}")
    logger.info(f"   Collection prefix: {env_config.firestore_collection_prefix}")
    
    # 2. Initialize repositories (account_repo first, then user_repo)
    account_repo = FirestoreAccountRepository(db, env_config)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    
    logger.info("✅ Repositories initialized")
    
    # 3. Create AgentCoordinator
    coordinator = AgentCoordinator()
    
    logger.info("✅ AgentCoordinator created")
    
    # 4. Create UserAgentFactory (with component_service!)
    factory = UserAgentFactory(
        config=config,
        coordinator=coordinator,
        db_client=db,
        env_config=env_config,
        user_repo=user_repo,
        account_repo=account_repo
    )
    
    logger.info("✅ UserAgentFactory initialized")
    logger.info("   ↳ PromptComponentService: ENABLED")
    logger.info(f"   ↳ Collection: {env_config.firestore_collection_prefix}_prompt_components")
    
    # 5. Ensure agents for user (triggers PromptBuilder initialization)
    logger.info(f"🔧 Creating agents for user {user_id[:8]}...")
    agents = await factory.ensure_agents_for_user(user_id)
    
    logger.info("✅ Agents created:")
    for agent_name in agents.keys():
        if agent_name != "last_used":
            logger.info(f"   ↳ {agent_name}")
    
    # 6. Get the appropriate agent and its PromptBuilder
    if agent_type == "quick":
        agent = agents["quick_agent"]
    elif agent_type == "smart":
        agent = agents["smart_agent"]
    elif agent_type == "router":
        agent = agents["router_agent"]
    elif agent_type == "web":
        agent = agents["web_agent"]
    elif agent_type == "consolidation":
        agent = agents["consolidation_agent"]
    else:
        raise ValueError(f"Unknown agent_type: {agent_type}")
    
    prompt_builder = getattr(agent, "prompt_builder", None)
    if prompt_builder is None:
        if agent_type in {"web", "router"}:
            prompt_builder = agents["quick_agent"].prompt_builder
        elif agent_type == "consolidation":
            prompt_builder = agents["smart_agent"].prompt_builder
        else:
            raise AttributeError(f"{type(agent).__name__} has no prompt_builder")
    
    logger.info(f"✅ Using {agent_type}_agent")
    logger.info(f"   ↳ PromptBuilder: {type(prompt_builder).__name__}")
    
    # 7. Build prompt using REAL method (production flow)
    logger.info(f"🔨 Building prompt via PromptBuilder.build_for_agent()...")
    
    # Map CLI agent names to PromptBuilder agent_type
    prompt_agent_type_map = {
        "quick": "quick",
        "smart": "smart",
        "web": "websearch",
        "router": "router",
        "consolidation": "consolidation"
    }
    prompt_agent_type = prompt_agent_type_map.get(agent_type, agent_type)

    assembled_prompt = await prompt_builder.build_for_agent(
        agent_type=prompt_agent_type,
        user_id=user_id,
        routing_metadata=None,
        semantic_context="",
        capabilities=None
    )
    
    logger.info(f"✅ Prompt assembled!")
    logger.info(f"   ↳ Length: {len(assembled_prompt)} chars")
    logger.info(f"   ↳ Lines: {len(assembled_prompt.splitlines())}")
    
    # 8. Save to file (CLEAN .groovy format, NO metadata)
    os.makedirs(save_dir, exist_ok=True)
    
    now = datetime.now()
    filename = f"e2e-{agent_type}-{now.strftime('%y%m%d-%H%M')}.groovy"
    filepath = os.path.join(save_dir, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(assembled_prompt)
    
    logger.info("=" * 70)
    logger.info("✅ E2E TEST COMPLETE!")
    logger.info("=" * 70)
    logger.info(f"📄 Saved: {filepath}")
    logger.info(f"📊 Size: {len(assembled_prompt)} chars")
    logger.info("=" * 70)
    
    print(f"\n✅ E2E Test Complete!")
    print(f"📄 Saved: {filepath}")
    print(f"📊 Prompt length: {len(assembled_prompt)} chars")
    print(f"💡 Open file to see EXACT prompt sent to LLM")
    
    return assembled_prompt


async def main():
    parser = argparse.ArgumentParser(
        description="E2E Prompt Assembly Test (Real Infrastructure)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/prompt/test_e2e_prompt_assembly.py --user-id abc123 --agent quick
  python scripts/prompt/test_e2e_prompt_assembly.py --user-id abc123 --agent smart
  python scripts/prompt/test_e2e_prompt_assembly.py --user-id abc123 --agent both
  
  # With environment variable:
  DEV_USER_ID=abc123 python scripts/prompt/test_e2e_prompt_assembly.py --agent quick
        """
    )
    
    parser.add_argument(
        "--user-id",
        help="User UUID (or use DEV_USER_ID from .env)"
    )
    parser.add_argument(
        "--agent",
        choices=["quick", "smart", "router", "web", "consolidation", "both"],
        default="quick",
        help="Agent type to test (default: quick)"
    )
    parser.add_argument(
        "--output-dir",
        default="reports/prompt/e2e",
        help="Output directory for .groovy files (default: reports/prompt/e2e)"
    )
    
    args = parser.parse_args()
    
    # Get user_id from args or environment
    user_id = args.user_id or os.getenv("DEV_USER_ID")
    if not user_id:
        print("❌ Error: User ID required!")
        print("   Provide via --user-id or set DEV_USER_ID in .env")
        sys.exit(1)
    
    try:
        if args.agent == "both":
            print("\n🔄 Testing BOTH agents...\n")
            await test_assembly_e2e(user_id, "quick", args.output_dir)
            print("\n" + "=" * 70 + "\n")
            await test_assembly_e2e(user_id, "smart", args.output_dir)
        else:
            await test_assembly_e2e(user_id, args.agent, args.output_dir)
    except Exception as e:
        logger.error(f"❌ E2E test failed: {e}", exc_info=True)
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
