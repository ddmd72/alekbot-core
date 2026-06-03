#!/usr/bin/env python3
"""
Inspect Smart Agent E2E Prompt - Real Flow with Mock LLM
================================================================
Simulates the exact flow:
1. Initialize Services (DI)
2. Create SmartResponseAgent (entry point after Router)
3. Simulate Router Message (AgentMessage)
4. Intercept LLM Request using Mock Adapter
5. Save captured System Prompt & Messages to report

Usage:
    python scripts/prompt/inspect_smart_e2e.py --user-id <user_id>
    make test-smart-dev  # Via Makefile

============================================================================
⚠️ KNOWN ISSUE: Inconsistent Collection Naming
============================================================================
LEGACY v2 collections: "development_*" (e.g., development_sessions)
NEW v3 collections: "dev_*" (e.g., dev_prompt_blueprints_v3)

This inconsistency exists in production and must be preserved until
full migration to unified naming convention.

See: docs/2026-02-03-prompt-system-audit.md
============================================================================
"""

import asyncio
import argparse
import sys
import os
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.prompt_builder import PromptBuilder
from src.services.agent_context_builder import AgentExecutionContext
from src.agents.core.smart_response_agent import create_smart_response_agent
from src.domain.agent import AgentMessage, AgentIntent, AgentConfig
from src.ports.llm_service import LLMService, LLMRequest, LLMResponse, ToolCall, Message

# ============================================================================
# MOCK LLM ADAPTER (Intercepts the prompt)
# ============================================================================

class MockGeminiAdapter(LLMService):
    """
    Mock adapter that intercepts the LLM request and saves it.
    Does NOT call real Gemini API.
    """
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.captured_request: Optional[LLMRequest] = None

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        """Intercept request, save it, and return dummy response."""
        print(f"\n⚡ [MOCK LLM] Intercepted generate_content() call")
        self.captured_request = request
        
        # Save to report immediately
        await self._save_report(request)
        
        return LLMResponse(
            text="[MOCK] This is a simulated response. The prompt has been captured.",
            tool_calls=[]
        )

    def get_capabilities(self):
        return None
    
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
        report_path = f"reports/prompt/{date_part}-smart-e2e-{user_short}-{time_part}.md"
        
        os.makedirs("reports/prompt", exist_ok=True)
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# Smart Agent E2E Prompt Inspection\n\n")
            f.write(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
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
# INFRASTRUCTURE HELPERS (Hexagonal Encapsulation)
# ============================================================================

def create_firestore_client(config: dict) -> firestore.AsyncClient:
    """
    Factory function for Firestore client creation.
    
    Encapsulates direct infrastructure dependency to maintain
    hexagonal architecture boundaries.
    """
    return firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])


def validate_test_prerequisites(user_id: str, account_id: str, env_config: EnvironmentConfig):
    """
    Validate all required configuration before test execution.
    
    Fails fast with clear error messages rather than producing
    misleading test results.
    
    Raises:
        ValueError: If any required configuration is missing
    """
    if not user_id:
        raise ValueError(
            "DEV_USER_ID required for dev test. "
            "Set in .env or pass --user-id"
        )
    
    if not account_id:
        raise ValueError(
            "DEV_ACCOUNT_ID required for OAuth flow. "
            "Set in .env file"
        )
    
    if not env_config.is_development:
        raise ValueError(
            f"Test must run in development environment. "
            f"Current: {env_config.env.value}"
        )


async def validate_user_data(
    session_store: FirestoreSessionStore,
    user_id: str
) -> str:
    """
    Validate user has existing session data.
    
    Returns:
        session_id: Latest session ID for user
        
    Raises:
        ValueError: If no sessions found
    """
    session_id = await session_store.get_latest_session_id(user_id)
    if not session_id:
        raise ValueError(
            f"No sessions found for user {user_id}. "
            f"Message the deployed bot first to create a session."
        )
    return session_id


# ============================================================================
# MAIN EXECUTION
# ============================================================================

async def inspect_smart_e2e(user_id: str, account_id: str):
    """
    E2E inspection of Smart Agent prompt assembly.
    
    Args:
        user_id: User UUID (required)
        account_id: Account ID for OAuth flow (required)
        
    Raises:
        ValueError: If configuration is invalid or user data is missing
    """
    print(f"\n{'='*70}")
    print(f"🧠 SMART AGENT E2E INSPECTION (REAL FLOW + MOCK LLM)")
    print(f"{'='*70}")
    
    # 1. Load Configuration
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    # 2. Validate Prerequisites (Fail Fast)
    print(f"\n{'='*70}")
    print(f"📊 VALIDATING CONFIGURATION")
    print(f"{'='*70}")
    
    try:
        validate_test_prerequisites(user_id, account_id, env_config)
        print(f"✅ User ID: {user_id}")
        print(f"✅ Account ID: {account_id}")
        print(f"✅ Environment: {env_config.env.value}")
    except ValueError as e:
        print(f"❌ Configuration Error: {e}")
        raise
    
    # 3. Initialize Infrastructure (Hexagonal Boundary)
    print(f"\n{'='*70}")
    print(f"🔧 INITIALIZING INFRASTRUCTURE")
    print(f"{'='*70}")
    
    db_client = create_firestore_client(config)
    prefix = env_config.firestore_collection_prefix
    
    # Real Repositories
    repository = FirestoreFactRepository(db_client, env_config)
    session_store = FirestoreSessionStore(db_client, prefix)
    
    # Initialize v3 assembly service
    from src.adapters.security.regex_adapter import RegexSecurityAdapter
    from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
    from src.adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
    from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
    from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
    from src.services.prompt_v3.context_formatter import ContextFormatter
    from src.services.prompt_v3.biographical_formatter import BiographicalFactsFormatter

    security_port = RegexSecurityAdapter()
    
    # NOTE: v3 collections use "dev_" prefix (not "development_")
    # This naming inconsistency is documented at top of file
    v3_prefix = "dev_" if env_config.is_development else ""
    
    token_repo = FirestoreTokenRepository(
        db=db_client,
        system_collection=f"{v3_prefix}prompt_system_tokens",
        user_collection=f"{v3_prefix}prompt_user_tokens",
        security_port=security_port
    )
    blueprint_repo = FirestoreBlueprintRepository(
        db=db_client,
        collection_name=f"{v3_prefix}prompt_blueprints"
    )
    profile_repo = FirestoreAgentProfileRepository(
        db=db_client,
        profiles_collection=f"{v3_prefix}prompt_agent_profiles",
        overrides_collection=f"{v3_prefix}prompt_agent_profile_user_overrides"
    )
    
    print(f"✅ Firestore client initialized")
    print(f"✅ v3 repositories initialized (prefix: {v3_prefix})")
    
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
    
    # PromptBuilder with v3 assembly service
    prompt_builder = PromptBuilder(
        repo=repository, 
        assembly_service=assembly_service
    )
    
    # Mock LLM
    mock_llm = MockGeminiAdapter(user_id=user_id)
    
    # Execution Context
    from src.ports.llm_service import ProviderCapabilities
    
    execution_context = AgentExecutionContext(
        agent_type="smart_response",  # Required field
        tier="balanced",             # Required field
        provider=mock_llm,
        model_name="gemini-mock",
        capabilities=ProviderCapabilities(
            supports_tools=True,
            supports_structured_output=True,
            supports_system_instruction=True,
            max_context_tokens=100000
        )
    )
    
    # 4. Validate User Data
    print(f"\n{'='*70}")
    print(f"📋 VALIDATING USER DATA")
    print(f"{'='*70}")
    
    try:
        session_id = await validate_user_data(session_store, user_id)
        print(f"✅ Session ID: {session_id}")
    except ValueError as e:
        print(f"❌ Data Validation Error: {e}")
        raise
    
    # 5. Create Smart Agent (Entry Point)
    print(f"\n{'='*70}")
    print(f"🤖 CREATING SMART AGENT")
    print(f"{'='*70}")
    
    smart_agent = create_smart_response_agent(
        execution_context=execution_context,
        session_store=session_store,
        prompt_builder=prompt_builder,
        repository=repository,
        embedding_service=None,  # Not needed for E2E test
        coordinator=None,  # Not needed - we mock LLM before delegation
        user_id=user_id,
        model_name="gemini-mock"
    )
    
    print(f"✅ SmartResponseAgent created: {smart_agent.agent_id}")
    
    # 6. Simulate Router Message
    print(f"\n{'='*70}")
    print(f"📨 SIMULATING ROUTER MESSAGE")
    print(f"{'='*70}")
    
    message = AgentMessage.create(
        sender="router_agent",
        recipient=f"smart_response_agent_{user_id}",
        intent=AgentIntent.QUERY,
        payload={
            "text": "What do you know about my GitHub project?" 
        },
        context={
            "user_id": user_id,
            "account_id": account_id,
            "session_id": session_id,
            "routing": {
                "user_tone": "neutral",
                "semantic_lens": ["github", "project", "code"],
                "confidence": 0.95
            }
        }
    )
    
    print(f"✅ Router message created")
    
    # 7. Execute Agent (Triggers flow -> prompt_builder -> llm)
    print(f"\n{'='*70}")
    print(f"🚀 EXECUTING AGENT")
    print(f"{'='*70}")
    
    try:
        # Add timeout to prevent hanging
        response = await asyncio.wait_for(
            smart_agent.execute(message),
            timeout=60.0  # 60 seconds max
        )
        
        print(f"\n{'='*70}")
        print(f"✅ TEST COMPLETE")
        print(f"{'='*70}")
        print(f"Result: {response.result.text[:100]}...")
        
    except asyncio.TimeoutError:
        print(f"\n{'='*70}")
        print(f"❌ TIMEOUT ERROR")
        print(f"{'='*70}")
        print(f"Agent execution exceeded 60 seconds")
        raise
        
    except ValueError as e:
        print(f"\n{'='*70}")
        print(f"❌ CONFIGURATION ERROR")
        print(f"{'='*70}")
        print(f"Error: {e}")
        raise
        
    except Exception as e:
        print(f"\n{'='*70}")
        print(f"❌ UNEXPECTED ERROR")
        print(f"{'='*70}")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        # Firestore AsyncClient cleanup
        # Note: close() might hang in some environments, wrap in try/except
        print(f"\n🧹 Cleanup...")
        try:
            # Don't await - just close synchronously if possible
            if hasattr(db_client, '_client') and hasattr(db_client._client, 'close'):
                db_client._client.close()
        except Exception as cleanup_error:
            print(f"⚠️ Cleanup warning: {cleanup_error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="E2E Test: Smart Agent Prompt Assembly (Development Only)"
    )
    parser.add_argument(
        "--user-id",
        help="User UUID (defaults to DEV_USER_ID from .env)"
    )
    
    args = parser.parse_args()
    
    # Strict validation: DEV only, no PROD fallback
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
    
    try:
        asyncio.run(inspect_smart_e2e(user_id, account_id))
    except ValueError as e:
        print(f"\n❌ Validation failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
