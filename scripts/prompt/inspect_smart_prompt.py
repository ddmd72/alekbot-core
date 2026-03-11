import asyncio
import argparse
import sys
import os
from datetime import datetime
from typing import Optional

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config.settings import load_settings
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.adapters.firestore_repo import FirestoreFactRepository
from src.adapters.firestore_prompt_repository import FirestorePromptComponentRepository
from src.adapters.groovy_prompt_assembler import GroovyPromptAssembler
from src.adapters.xml_prompt_assembler import XmlPromptAssembler
from src.adapters.claude_adapter import ClaudeAdapter
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_component_service import PromptComponentService
from src.services.agent_context_builder import AgentExecutionContext
from src.agents.core.smart_response_agent import create_smart_response_agent
from src.domain.agent import AgentConfig, RoutingMetadata
from src.domain.user import PerformanceTier

async def inspect_smart_prompt(
    user_id: str
):
    print(f"\n🧠 INITIALIZING SMART PROMPT INSPECTION")
    print(f"User ID: {user_id}")
    
    # 1. Setup Infrastructure
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    session_store = FirestoreSessionStore(db_client, env_config.firestore_collection_prefix)
    repo = FirestoreFactRepository(db_client, env_config)
    llm = ClaudeAdapter(api_key=config["ANTHROPIC_API_KEY"])

    # Initialize component system for 3-level hierarchy
    prompt_component_repo = FirestorePromptComponentRepository(
        db_client=db_client,
        collection_name=f"{env_config.firestore_collection_prefix}prompt_components"
    )
    # Support both Groovy and XML formats
    assemblers = {
        "groovy": GroovyPromptAssembler(),
        "xml": XmlPromptAssembler()
    }
    component_service = PromptComponentService(
        repository=prompt_component_repo,
        assembler=assemblers,
        cache_ttl=3600
    )

    prompt_builder = PromptBuilder(repo, component_service=component_service)
    
    # 2. Create execution context
    execution_context = AgentExecutionContext(
        agent_type="smart",
        provider=llm,
        model_name="claude-sonnet-4-5-20250929",
        tier=PerformanceTier.BALANCED,
        capabilities=llm.get_capabilities()
    )
    
    # 3. Initialize Agent using factory
    agent = create_smart_response_agent(
        execution_context=execution_context,
        session_store=session_store,
        prompt_builder=prompt_builder,
        repository=repo,
        user_id=user_id
    )
    
    # 4. Build Prompt
    print("🔨 Assembling prompt...")
    system_prompt = await agent._build_system_prompt(routing_metadata=RoutingMetadata.from_dict({}))

    # 5. Report
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H%M%S")
    user_short = user_id[:4]
    report_path = f"reports/prompt/{date_part}-smart-{user_short}-{time_part}.md"
    os.makedirs("reports/prompt", exist_ok=True)
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(system_prompt)
        
    print(f"\n✨ DONE! Smart Agent prompt saved to: {report_path}")
    print("\n--- PREVIEW ---")
    print(system_prompt[:1000] + "..." if len(system_prompt) > 1000 else system_prompt)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect Smart Response Agent Prompt with Real Data")
    parser.add_argument("--user-id", help="User UUID (defaults to PROD_USER_ID/DEV_USER_ID from env)")

    args = parser.parse_args()
    user_id = args.user_id or os.getenv("PROD_USER_ID") or os.getenv("DEV_USER_ID")
    if not user_id:
        raise ValueError("USER_ID required: provide --user-id or set PROD_USER_ID/DEV_USER_ID in .env")

    asyncio.run(inspect_smart_prompt(
        user_id=user_id
    ))
