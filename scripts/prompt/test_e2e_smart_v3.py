#!/usr/bin/env python3
"""
E2E Test: Smart Agent Prompt Assembly v3
=========================================
Simulates real user message → final LLM prompt flow.

Mocks:
    - Router decision (agent_type = "smart")

Real:
    - User data from Firestore (facts, history)
    - PromptAssemblyService v3
    - All repositories (tokens, blueprints, profiles)

Output:
    - Final LLM prompt (all {{}} replaced)
    - Saved to reports/prompt/<timestamp>.md

Usage:
    python scripts/prompt/test_e2e_smart_v3.py --user-id <user_id> --message "Як справи?"
    make test-e2e-smart-v3-dev  # Via Makefile
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
from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter
from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.adapters.security.regex_adapter import RegexSecurityAdapter
from src.services.prompt_v3.context_formatter import ContextFormatter


async def get_biographical_facts(repo: FirestoreFactRepository, account_id: str) -> List[dict]:
    """Fetch biographical facts from Firestore cached context.
    
    Args:
        repo: FirestoreFactRepository instance
        account_id: Account ID (not user_id - uses owner_id/account_id field)
    
    Returns:
        List of biographical fact dictionaries
    """
    # Direct call - let exceptions propagate
    cached_facts = await repo.get_biographical_context_cached(account_id, limit=50)
    return [fact for fact in cached_facts if isinstance(fact, dict) and fact.get('text')]


async def get_conversation_history(session_store: FirestoreSessionStore, user_id: str) -> List[dict]:
    """Fetch recent conversation history from Firestore."""
    # Get latest session ID for user
    session_id = await session_store.get_latest_session_id(user_id)
    if not session_id:
        return []
    
    # Load session state
    session_state = await session_store.load_session(session_id)
    messages = session_state.history[-10:]  # Last 10 messages
    
    # Convert Message objects to dict format
    return [
        {
            "role": msg.role,
            "content": msg.parts[0].text if msg.parts and msg.parts[0].text else ""
        }
        for msg in messages
        if msg.parts  # Skip messages without parts
    ]


async def get_account_id(db_client: firestore.AsyncClient, user_id: str) -> Optional[str]:
    """Fetch user's account_id from development_users_oauth collection."""
    # Direct call - let exceptions propagate
    doc = await db_client.collection('development_users_oauth').document(user_id).get()
    if doc.exists:
        data = doc.to_dict()
        return data.get('account_id')
    return None


async def test_e2e_smart_v3(
    user_id: str,
    user_message: str,
    account_id: Optional[str] = None
):
    """E2E test: User message → Final LLM prompt."""
    
    print("=" * 70)
    print("E2E TEST: Smart Agent Prompt Assembly v3")
    print("=" * 70)
    print(f"User Message: {user_message}")
    print(f"User ID: {user_id}")
    print(f"Account ID: {account_id or 'will be fetched'}")
    print("=" * 70)
    
    # 1. Setup Infrastructure
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    # Legacy services (for data retrieval)
    session_store = FirestoreSessionStore(db_client, env_config.firestore_collection_prefix)
    fact_repo = FirestoreFactRepository(db_client, env_config)
    
    # 2. Initialize v3 Services
    security_port = RegexSecurityAdapter()
    
    token_repo = FirestoreTokenRepository(
        db=db_client,
        system_collection="dev_prompt_system_tokens",
        user_collection="dev_prompt_user_tokens",
        security_port=security_port
    )
    
    blueprint_repo = FirestoreBlueprintRepository(
        db=db_client,
        collection_name="dev_prompt_blueprints_v3"
    )
    
    profile_repo = FirestoreAgentProfileRepository(
        db=db_client,
        profiles_collection="dev_agent_profiles_v3",
        overrides_collection="dev_prompt_user_token_overrides"
    )
    
    formatter = ContextFormatter()
    bio_formatter = BiographicalFactsFormatter()
    
    assembly_service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=security_port,
        formatter=formatter,
        bio_formatter=bio_formatter
    )
    
    # 3. Fetch Real Data
    print("\n📊 Fetching data from Firestore...")
    
    # Get account_id (required for biographical facts)
    if not account_id:
        account_id = await get_account_id(db_client, user_id)
        print(f"  Account ID: {account_id or 'NOT FOUND'}")
    
    # Fetch biographical facts (requires account_id)
    if account_id:
        biographical_facts = await get_biographical_facts(fact_repo, account_id)
        print(f"  Biographical facts: {len(biographical_facts)}")
    else:
        print("  ⚠️  Skipping biographical facts (no account_id)")
        biographical_facts = []
    
    # Fetch conversation history
    conversation_history = await get_conversation_history(session_store, user_id)
    print(f"  Conversation messages: {len(conversation_history)}")
    
    # 4. Assemble Prompt (direct call, let exceptions propagate)
    print("\n🔧 Assembling prompt...")
    assembled_prompt = await assembly_service.assemble(
        agent_type="smart",
        user_id=user_id,
        account_id=account_id,
        biographical_facts=biographical_facts,
        conversation_history=conversation_history
    )
    
    # 5. Output Results
    print("\n" + "=" * 70)
    print("✅ FINAL LLM PROMPT")
    print("=" * 70)
    print(f"Length: {len(assembled_prompt)} characters")
    print(f"Biographical facts: {len(biographical_facts)} items")
    print(f"Conversation messages: {len(conversation_history)} messages")
    print("=" * 70)
    print()
    print(assembled_prompt)
    print()
    print("=" * 70)
    
    # 6. Save Report
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H%M%S")
    user_short = user_id[:4] if user_id else "anon"
    report_path = f"reports/prompt/{date_part}-e2e-smart-v3-{user_short}-{time_part}.md"
    
    os.makedirs("reports/prompt", exist_ok=True)
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# E2E Test: Smart Agent Prompt (v3)\n\n")
        f.write(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**User Message:** {user_message}\n")
        f.write(f"**Agent Type:** smart (mocked)\n")
        f.write(f"**User ID:** {user_id}\n")
        f.write(f"**Account ID:** {account_id or 'default'}\n")
        f.write(f"**Environment:** {env_config.env.value}\n")
        f.write(f"**Prompt Length:** {len(assembled_prompt)} characters\n")
        f.write(f"**Biographical Facts:** {len(biographical_facts)} items\n")
        f.write(f"**Conversation Messages:** {len(conversation_history)} messages\n")
        f.write(f"\n---\n\n")
        f.write(assembled_prompt)
    
    print(f"📄 Report saved: {report_path}")
    
    # Close connections
    try:
        await db_client.close()
    except (TypeError, AttributeError):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="E2E Test: Smart Agent Prompt Assembly v3"
    )
    parser.add_argument(
        "--user-id",
        help="User UUID (defaults to DEV_USER_ID from env)"
    )
    parser.add_argument(
        "--message",
        default="Як справи?",
        help="User message to simulate (default: 'Як справи?')"
    )
    parser.add_argument(
        "--account-id",
        help="Account ID (optional, defaults to None)"
    )
    
    args = parser.parse_args()
    
    # Resolve user_id
    user_id = args.user_id or os.getenv("DEV_USER_ID")
    if not user_id:
        print("❌ Error: USER_ID required")
        print("   Provide --user-id or set DEV_USER_ID in .env")
        sys.exit(1)
    
    asyncio.run(test_e2e_smart_v3(
        user_id=user_id,
        user_message=args.message,
        account_id=args.account_id
    ))
