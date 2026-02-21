#!/usr/bin/env python3
"""
E2E Agent Test - Parametrized for All Agents
=============================================
Tests ANY agent using PRODUCTION UserAgentFactory flow.
Intercepts LLM call with Mock to capture assembled prompt.

Usage:
    python scripts/prompt/test_agent_e2e.py --agent-type smart --user-id <user_id>
    make test-agent-dev AGENT=smart  # Via Makefile
    make test-all-agents-dev         # Test all agents

Supported agents:
- smart: SmartResponseAgent
- quick: QuickResponseAgent
- router: RouterAgent
- consolidation: ConsolidationAgent (expected to fail - v2 broken)
- web_search: WebSearchAgent (skipped - inline prompt)
- memory_search: MemorySearchAgent (skipped - no prompt)
"""

import asyncio
import argparse
import sys
import os
from datetime import datetime
from typing import Optional

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.services.user_agent_factory import UserAgentFactory
from src.composition.service_container import ServiceContainer
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.domain.agent import AgentMessage, AgentIntent
from src.ports.llm_service import LLMService, LLMRequest, LLMResponse, ProviderCapabilities

# ============================================================================
# AGENT TYPE MAPPING (Production keys from UserAgentFactory)
# ============================================================================

AGENT_TYPE_MAP = {
    "smart": "smart_agent",
    "quick": "quick_agent",
    "router": "router_agent",
    "consolidation": "consolidation_agent",
    "web_search": "web_agent",
    "memory_search": "memory_agent"
}

SKIP_AGENTS = {
    "memory_search": "Pure vector search - no LLM prompt"
}

# ============================================================================
# MOCK LLM ADAPTER (Intercepts the prompt)
# ============================================================================

class MockGeminiAdapter(LLMService):
    """Mock adapter that intercepts LLM request and saves prompt."""
    
    def __init__(self, user_id: str, agent_type: str):
        self.user_id = user_id
        self.agent_type = agent_type
        self.captured_request: Optional[LLMRequest] = None

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        """Intercept request, save it, return dummy response."""
        print(f"\n⚡ [MOCK LLM] Intercepted generate_content() for {self.agent_type}")
        self.captured_request = request
        
        # Save to report immediately
        await self._save_report(request)
        
        # Return valid JSON for consolidation agent (to avoid parsing errors)
        if self.agent_type == "consolidation":
            mock_response = '```json\n{"new_facts": [], "new_anchors": []}\n```'
        else:
            mock_response = "[MOCK] This is a simulated response. The prompt has been captured."
        
        return LLMResponse(
            text=mock_response,
            tool_calls=[]
        )

    def get_capabilities(self):
        return ProviderCapabilities()
    
    def get_model_for_tier(self, tier):
        return "gemini-mock"
    
    def supports_caching(self):
        return False
        
    async def upload_file(self, file_data, mime_type):
        return None

    async def _save_report(self, request: LLMRequest):
        now = datetime.now()
        date_part = now.strftime("%Y-%m-%d")
        time_part = now.strftime("%H%M%S")
        user_short = self.user_id[:4] if self.user_id else "anon"
        report_path = f"reports/prompt/{date_part}-{self.agent_type}-e2e-{user_short}-{time_part}.md"
        
        os.makedirs("reports/prompt", exist_ok=True)
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# {self.agent_type.title()} Agent E2E Prompt Inspection\n\n")
            f.write(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Agent Type:** {self.agent_type}\n")
            f.write(f"**User ID:** {self.user_id}\n")
            f.write(f"**Model:** {request.model_name}\n")
            f.write(f"**Temperature:** {request.temperature}\n")
            f.write(f"\n---\n\n")
            
            f.write("## 1. System Instruction (The PROMPT)\n\n")
            f.write("```groovy\n")
            f.write(request.system_instruction or "// No system instruction")
            f.write("\n```\n\n")
            
            f.write("## 2. Conversation History (Messages)\n\n")
            for msg in request.messages:
                role = msg.role.upper()
                content = ""
                if msg.parts:
                    content = msg.parts[0].text or "[No text]"
                f.write(f"**{role}:** {content}\n\n")
                
        print(f"✅ Report saved: {report_path}")
        print(f"📏 System Prompt Length: {len(request.system_instruction or '')} chars")


# ============================================================================
# INFRASTRUCTURE HELPERS
# ============================================================================

def create_firestore_client(config: dict) -> firestore.AsyncClient:
    """Factory for Firestore client."""
    database_id = os.getenv("FIRESTORE_DATABASE", "(default)")
    return firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"], database=database_id)


def validate_test_prerequisites(user_id: str, account_id: str, agent_type: str, env_config: EnvironmentConfig):
    """Validate configuration before test execution."""
    if not user_id:
        raise ValueError("DEV_USER_ID required. Set in .env or pass --user-id")
    
    if not account_id:
        raise ValueError("DEV_ACCOUNT_ID required. Set in .env file")
    
    if not env_config.is_development:
        raise ValueError(f"Test must run in development environment. Current: {env_config.env.value}")
    
    if agent_type not in AGENT_TYPE_MAP:
        raise ValueError(f"Unknown agent type: {agent_type}. Supported: {list(AGENT_TYPE_MAP.keys())}")


async def validate_user_data(session_store, user_id: str) -> str:
    """Validate user has existing session data."""
    session_id = await session_store.get_latest_session_id(user_id)
    if not session_id:
        raise ValueError(f"No sessions found for user {user_id}. Run bot first: make dev")
    return session_id


async def create_test_message(
    agent_type: str, 
    user_id: str, 
    session_id: str, 
    account_id: str,
    session_store=None,
    fact_repo = None
) -> AgentMessage:
    """Create test message based on agent type (HEXAGONAL - unified flow)."""
    
    # Agent-specific test messages
    test_messages = {
        "smart": "What do you know about my GitHub project?",
        "quick": "What's the weather today?",
        "router": "Can you help me with something?",
        "consolidation": "$consolidate",  # Production trigger command
        "web_search": "Python web frameworks comparison 2026",
        "memory_search": "What do you know about me?"
    }
    
    # Intent mapping: consolidation uses DELEGATE (direct call), others use QUERY
    intent_map = {
        "consolidation": AgentIntent.DELEGATE,  # Direct call to agent, bypass router
    }
    
    text = test_messages.get(agent_type, "Test message")
    recipient = f"{AGENT_TYPE_MAP[agent_type]}_{user_id}"
    
    # Special handling by agent type
    payload = {}
    if agent_type == "consolidation":
        print(f"\n📊 Creating test batch for consolidation...")
        
        # Create 10 fake messages (Dict format as expected by ConsolidationAgent)
        import time
        messages = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "text": f"Test message {i+1} for consolidation batch",
                "timestamp": time.time() - (10 - i) * 60  # 10 min ago, 9 min ago...
            }
            for i in range(10)
        ]
        print(f"✅ Created {len(messages)} fake messages for test batch")
        
        # Load real biographical facts (use account_id for OAuth Multi-Tenant)
        bio_facts = []
        if fact_repo:
            try:
                bio_facts = await fact_repo.get_biographical_context_cached(account_id, limit=100)
                print(f"✅ Loaded {len(bio_facts)} biographical facts")
            except Exception as e:
                print(f"⚠️  Failed to load biographical facts: {e}")
        
        payload = {
            "messages": messages,
            "biographical_context": bio_facts
        }
    elif agent_type == "web_search":
        # WebSearchAgent expects "query" in payload
        payload = {"query": text}
    else:
        # Other agents: simple text payload
        payload = {"text": text} if text else {}
    
    return AgentMessage.create(
        sender="test_harness",
        recipient=recipient,
        intent=intent_map.get(agent_type, AgentIntent.QUERY),  # Use mapping, default to QUERY
        payload=payload,
        context={
            "user_id": user_id,
            "account_id": account_id,
            "session_id": session_id,
            "routing": {
                "user_tone": "neutral",
                "semantic_lens": ["test", "e2e"],
                "confidence": 0.95
            }
        }
    )


# ============================================================================
# MAIN TEST EXECUTION
# ============================================================================

async def test_agent_e2e(agent_type: str, user_id: str, account_id: str):
    """
    E2E test using PRODUCTION UserAgentFactory.
    
    Args:
        agent_type: Type of agent to test (smart, quick, router, etc.)
        user_id: User UUID
        account_id: Account ID for OAuth
        
    Raises:
        ValueError: If configuration is invalid
    """
    print(f"\n{'='*70}")
    print(f"🧪 E2E TEST: {agent_type.upper()} AGENT (PRODUCTION FLOW)")
    print(f"{'='*70}")
    
    # Check if should skip
    if agent_type in SKIP_AGENTS:
        print(f"\n⏭️  SKIPPING: {SKIP_AGENTS[agent_type]}")
        print(f"{'='*70}\n")
        return
    
    # 1. Load Configuration
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    # 2. Validate Prerequisites
    print(f"\n{'='*70}")
    print(f"📊 VALIDATING CONFIGURATION")
    print(f"{'='*70}")
    
    try:
        validate_test_prerequisites(user_id, account_id, agent_type, env_config)
        print(f"✅ User ID: {user_id}")
        print(f"✅ Account ID: {account_id}")
        print(f"✅ Environment: {env_config.env.value}")
        print(f"✅ Agent Type: {agent_type}")
    except ValueError as e:
        print(f"❌ Configuration Error: {e}")
        raise
    
    # 3. Initialize Infrastructure (PRODUCTION)
    print(f"\n{'='*70}")
    print(f"🔧 INITIALIZING PRODUCTION INFRASTRUCTURE")
    print(f"{'='*70}")

    db_client = create_firestore_client(config)
    coordinator = AgentCoordinator()

    account_repo = FirestoreAccountRepository(
        db_client=db_client,
        collection_name=env_config.account_collection_name
    )
    user_repo = FirestoreUserRepository(db_client, env_config, account_repo)

    container = ServiceContainer(
        config=config,
        db_client=db_client,
        env_config=env_config,
        account_repo=account_repo,
    )

    # Inject MockGeminiAdapter before factory construction — agents receive mock at creation time
    mock_llm = MockGeminiAdapter(user_id, agent_type)
    container.llm_service = mock_llm
    container.claude_service = mock_llm
    container.registry.register("gemini", mock_llm)
    container.registry.register("claude", mock_llm)

    print(f"✅ Infrastructure initialized")

    # 4. Create UserAgentFactory (PRODUCTION flow, mock LLM injected)
    print(f"\n{'='*70}")
    print(f"🏭 CREATING USER AGENT FACTORY (PRODUCTION)")
    print(f"{'='*70}")

    factory = UserAgentFactory(
        config=config,
        env_config=env_config,
        coordinator=coordinator,
        user_repo=user_repo,
        account_repo=account_repo,
        **container.agent_services()
    )

    print(f"✅ UserAgentFactory created")
    
    # 5. Create ALL agents using PRODUCTION flow
    print(f"\n{'='*70}")
    print(f"🤖 CREATING AGENTS (PRODUCTION FLOW)")
    print(f"{'='*70}")
    
    agents = await factory.ensure_agents_for_user(user_id)
    
    print(f"✅ Created {len(agents)} agents:")
    for key in agents.keys():
        if key != "last_used":
            print(f"   - {key}")
    
    # 6. Get target agent
    agent_key = AGENT_TYPE_MAP[agent_type]
    agent = agents.get(agent_key)
    
    if not agent:
        raise ValueError(f"Agent '{agent_key}' not found in factory. Available: {list(agents.keys())}")
    
    print(f"\n✅ Target agent: {agent.agent_id}")
    
    # 7. Validate User Data
    print(f"\n{'='*70}")
    print(f"📋 VALIDATING USER DATA")
    print(f"{'='*70}")
    
    session_store = container.session_store
    
    try:
        session_id = await validate_user_data(session_store, user_id)
        print(f"✅ Session ID: {session_id}")
    except ValueError as e:
        print(f"❌ Data Validation Error: {e}")
        raise
    
    # 8. Create test message
    print(f"\n{'='*70}")
    print(f"📨 CREATING TEST MESSAGE")
    print(f"{'='*70}")
    
    fact_repo = container.repository if agent_type == "consolidation" else None
    
    message = await create_test_message(
        agent_type, 
        user_id, 
        session_id, 
        account_id,
        session_store=session_store,
        fact_repo=fact_repo
    )
    print(f"✅ Test message created")
    
    # 10. Execute Agent
    print(f"\n{'='*70}")
    print(f"🚀 EXECUTING AGENT")
    print(f"{'='*70}")
    
    try:
        response = await asyncio.wait_for(
            agent.execute(message),
            timeout=60.0
        )
        
        print(f"\n{'='*70}")
        print(f"✅ TEST COMPLETE")
        print(f"{'='*70}")
        print(f"Result: {response.result.text[:100] if hasattr(response.result, 'text') else str(response.result)[:100]}...")
        
    except asyncio.TimeoutError:
        print(f"\n{'='*70}")
        print(f"❌ TIMEOUT ERROR")
        print(f"{'='*70}")
        print(f"Agent execution exceeded 60 seconds")
        raise
        
    except Exception as e:
        print(f"\n{'='*70}")
        print(f"❌ EXECUTION ERROR")
        print(f"{'='*70}")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise
    
    finally:
        print(f"\n🧹 Cleanup...")
        try:
            if hasattr(db_client, '_client') and hasattr(db_client._client, 'close'):
                db_client._client.close()
        except Exception as cleanup_error:
            print(f"⚠️ Cleanup warning: {cleanup_error}")


async def test_all_agents(user_id: str, account_id: str):
    """Test all supported agents."""
    results = {}
    
    for agent_type in AGENT_TYPE_MAP.keys():
        print(f"\n\n{'#'*70}")
        print(f"# Testing: {agent_type.upper()}")
        print(f"{'#'*70}\n")
        
        try:
            await test_agent_e2e(agent_type, user_id, account_id)
            results[agent_type] = "✅ PASS"
        except Exception as e:
            if agent_type == "consolidation" and "component_service" in str(e):
                results[agent_type] = "⚠️  EXPECTED FAIL (v2 broken)"
            elif agent_type == "consolidation" and "assembly_service" in str(e):
                results[agent_type] = "❌ FAIL (missing assembly_service)"
            else:
                results[agent_type] = f"❌ FAIL: {str(e)[:50]}"
    
    # Summary
    print(f"\n\n{'='*70}")
    print(f"📊 TEST SUMMARY")
    print(f"{'='*70}")
    for agent_type, result in results.items():
        print(f"  {agent_type:20s} {result}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="E2E Test: Agent Prompt Assembly (Production Flow)"
    )
    parser.add_argument(
        "--agent-type",
        required=True,
        help="Agent type to test (smart, quick, router, consolidation, web_search, memory_search, all)"
    )
    parser.add_argument(
        "--user-id",
        help="User UUID (defaults to DEV_USER_ID from .env)"
    )
    
    args = parser.parse_args()
    
    # Validate inputs
    user_id = args.user_id or os.getenv("DEV_USER_ID")
    account_id = os.getenv("DEV_ACCOUNT_ID")
    
    if not user_id:
        print("❌ Error: DEV_USER_ID required")
        print("   Set in .env or pass --user-id")
        sys.exit(1)
    
    if not account_id:
        print("❌ Error: DEV_ACCOUNT_ID required")
        print("   Set in .env file")
        sys.exit(1)
    
    # Run test
    try:
        if args.agent_type == "all":
            asyncio.run(test_all_agents(user_id, account_id))
        else:
            asyncio.run(test_agent_e2e(args.agent_type, user_id, account_id))
    except ValueError as e:
        print(f"\n❌ Validation failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
