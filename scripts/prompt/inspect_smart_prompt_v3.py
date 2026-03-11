#!/usr/bin/env python3
"""
Inspect Smart Agent Prompt (v3 Assembly) - Real Flow Inspection
================================================================
Uses Prompt Design System v3 (token-based architecture) to assemble
smart agent prompt with real data from Firestore.

Usage:
    python scripts/prompt/inspect_smart_prompt_v3.py --user-id <user_id>
    APP_ENV=development python scripts/prompt/inspect_smart_prompt_v3.py --user-id <user_id>
    make inspect-smart-v3-dev  # Via Makefile (sets APP_ENV=development)

Output:
    Assembled prompt saved to reports/prompt/<date>-smart-v3-<user>-<time>.md
"""

import asyncio
import argparse
import sys
import os
from datetime import datetime
from typing import Optional, List

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore

from src.config.settings import load_settings
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.adapters.firestore_repo import FirestoreFactRepository

# v3 Imports
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.adapters.security.regex_adapter import RegexSecurityAdapter
from src.services.prompt_v3.context_formatter import ContextFormatter
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter


async def get_biographical_facts(repo: FirestoreFactRepository, user_id: str) -> List[str]:
    """Fetch biographical facts from Firestore using cached method."""
    try:
        # Use cached biographical context (fast read, system method)
        cached_facts = await repo.get_biographical_context_cached(user_id, limit=50)
        return [fact["text"] for fact in cached_facts if "text" in fact]
    except Exception as e:
        print(f"⚠️  Could not fetch biographical facts: {e}")
        return []


async def get_conversation_history(session_store: FirestoreSessionStore, user_id: str) -> List[dict]:
    """Fetch recent conversation history from Firestore using system methods."""
    try:
        # Get latest session ID for user
        session_id = await session_store.get_latest_session_id(user_id)
        if not session_id:
            return []
        
        # Load session
        session = await session_store.load_session(session_id)
        if not session or not session.history:
            return []
        
        # Get last 10 messages
        messages = session.history[-10:]
        
        return [
            {
                "role": msg.role,
                "content": msg.parts[0].text if msg.parts and msg.parts[0].text else ""
            }
            for msg in messages
        ]
    except Exception as e:
        print(f"⚠️  Could not fetch conversation history: {e}")
        return []


async def inspect_smart_prompt_v3(
    user_id: str,
    account_id: Optional[str] = None
):
    """Inspect Smart Agent prompt using v3 assembly service."""
    
    print(f"\n{'='*70}")
    print(f"🧠 SMART AGENT PROMPT INSPECTION (V3 ASSEMBLY)")
    print(f"{'='*70}")
    print(f"User ID: {user_id}")
    print(f"Account ID: {account_id or 'default'}")
    
    # 1. Setup Infrastructure
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    prefix = env_config.firestore_collection_prefix
    
    print(f"Environment: {env_config.env.value}")
    print(f"Collection prefix: {prefix}")
    
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    # Legacy services (for data retrieval)
    session_store = FirestoreSessionStore(db_client, prefix)
    fact_repo = FirestoreFactRepository(db_client, env_config)
    
    # 2. Initialize v3 Services
    print(f"\n{'='*70}")
    print("🔧 INITIALIZING V3 SERVICES")
    print(f"{'='*70}")
    
    # Security validation
    security_port = RegexSecurityAdapter()
    print("✅ SecurityPort: RegexSecurityAdapter")
    
    # Token repository (dual-collection)
    # FIXED: Use correct collection names (dev_ prefix, not development_)
    token_repo = FirestoreTokenRepository(
        db=db_client,
        system_collection="dev_prompt_system_tokens",
        user_collection="dev_prompt_user_tokens",  # User tokens collection (Phase 5-1)
        security_port=security_port
    )
    print(f"✅ TokenRepository: dev_prompt_system_tokens + dev_prompt_user_tokens")
    
    # Blueprint repository
    blueprint_repo = FirestoreBlueprintRepository(
        db=db_client,
        collection_name="dev_prompt_blueprints"
    )
    print(f"✅ BlueprintRepository: dev_prompt_blueprints")
    
    # Agent profile repository (dual-collection)
    profile_repo = FirestoreAgentProfileRepository(
        db=db_client,
        profiles_collection="dev_prompt_agent_profiles",
        overrides_collection="dev_prompt_agent_profile_user_overrides"
    )
    print(f"✅ ProfileRepository: dev_prompt_agent_profiles + dev_prompt_agent_profile_user_overrides")
    
    # Context formatter
    formatter = ContextFormatter()
    print("✅ ContextFormatter initialized")
    
    # Biographical formatter
    bio_formatter = BiographicalFactsFormatter()
    print("✅ BiographicalFactsFormatter initialized")
    
    # Assembly service
    assembly_service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=security_port,
        formatter=formatter,
        bio_formatter=bio_formatter
    )
    print("✅ PromptAssemblyService initialized")
    
    # 3. Fetch Real Data
    print(f"\n{'='*70}")
    print("📊 FETCHING REAL DATA FROM FIRESTORE")
    print(f"{'='*70}")
    
    biographical_facts = await get_biographical_facts(fact_repo, user_id)
    print(f"✅ Biographical facts: {len(biographical_facts)} items")
    if biographical_facts:
        print(f"   Sample: {biographical_facts[0][:80]}...")
    
    conversation_history = await get_conversation_history(session_store, user_id)
    print(f"✅ Conversation history: {len(conversation_history)} messages")
    if conversation_history:
        print(f"   Latest: {conversation_history[-1]['role']}: {conversation_history[-1]['content'][:60]}...")
    
    # 4. Assemble Prompt
    print(f"\n{'='*70}")
    print("🔨 ASSEMBLING PROMPT WITH V3 SERVICE")
    print(f"{'='*70}")
    
    try:
        assembled_prompt = await assembly_service.assemble(
            agent_type="smart",
            user_id=user_id,
            account_id=account_id,
            biographical_facts=biographical_facts,
            conversation_history=conversation_history
        )
        
        print(f"✅ Prompt assembled: {len(assembled_prompt)} characters")
        
    except Exception as e:
        print(f"❌ Assembly failed: {e}")
        import traceback
        traceback.print_exc()
        
        # Provide helpful hints based on error type
        error_msg = str(e).lower()
        if "not found" in error_msg or "does not exist" in error_msg:
            print(f"\n{'='*70}")
            print("💡 TROUBLESHOOTING HINTS")
            print(f"{'='*70}")
            print("Firestore collections may be empty. Run migrations first:")
            print(f"  1. python scripts/migration/migrate_tokens_split.py --env {env_config.env.value}")
            print(f"  2. python scripts/migration/migrate_profiles_split.py --env {env_config.env.value}")
            print("\nOr verify collections exist:")
            print(f"  - {prefix}_prompt_system_tokens")
            print(f"  - {prefix}_prompt_user_tokens")
            print(f"  - {prefix}_prompt_blueprints")
            print(f"  - {prefix}_prompt_agent_profiles")
        
        sys.exit(1)
    
    # 5. Save Report
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H%M%S")
    user_short = user_id[:4] if user_id else "anon"
    report_path = f"reports/prompt/{date_part}-smart-v3-{user_short}-{time_part}.md"
    
    os.makedirs("reports/prompt", exist_ok=True)
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# Smart Agent Prompt (v3 Assembly)\n\n")
        f.write(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**User ID:** {user_id}\n")
        f.write(f"**Account ID:** {account_id or 'default'}\n")
        f.write(f"**Environment:** {env_config.env.value}\n")
        f.write(f"**Prompt Length:** {len(assembled_prompt)} characters\n")
        f.write(f"**Biographical Facts:** {len(biographical_facts)} items\n")
        f.write(f"**Conversation Messages:** {len(conversation_history)} messages\n")
        f.write(f"\n---\n\n")
        f.write(assembled_prompt)
    
    print(f"\n{'='*70}")
    print(f"✨ INSPECTION COMPLETE")
    print(f"{'='*70}")
    print(f"📄 Report saved: {report_path}")
    print(f"📏 Prompt length: {len(assembled_prompt)} chars")
    
    # Preview
    print(f"\n--- PREVIEW (first 500 chars) ---")
    print(assembled_prompt[:500])
    print("...")
    
    # Close connections (graceful)
    try:
        await db_client.close()
    except (TypeError, AttributeError):
        # AsyncClient.close() may not be awaitable
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Inspect Smart Agent Prompt using v3 Assembly Service"
    )
    parser.add_argument(
        "--user-id",
        help="User UUID (defaults to PROD_USER_ID/DEV_USER_ID from env)"
    )
    parser.add_argument(
        "--account-id",
        help="Account ID (optional, defaults to None)"
    )
    
    args = parser.parse_args()
    
    # Resolve user_id
    user_id = args.user_id or os.getenv("PROD_USER_ID") or os.getenv("DEV_USER_ID")
    if not user_id:
        print("❌ Error: USER_ID required")
        print("   Provide --user-id or set PROD_USER_ID/DEV_USER_ID in .env")
        sys.exit(1)
    
    account_id = args.account_id
    
    asyncio.run(inspect_smart_prompt_v3(
        user_id=user_id,
        account_id=account_id
    ))
