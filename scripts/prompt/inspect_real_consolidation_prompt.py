import asyncio
import argparse
import sys
import os
import json
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
from src.adapters.gemini_adapter import GeminiAdapter
from src.services.embedding_service import EmbeddingService
from src.services.agent_context_builder import AgentExecutionContext
from src.services.prompt_builder import PromptBuilder
from src.services.prompt_component_service import PromptComponentService
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig
from src.domain.user import PerformanceTier

async def inspect_prompt(
    user_id: str,
    session_id: Optional[str] = None,
    batch_size: Optional[int] = None,
    prompt_version: str = "v2"
):
    print(f"\n🔍 INITIALIZING INSPECTION")
    print(f"User ID: {user_id}")
    print(f"Prompt Version: {prompt_version}")
    
    # 1. Setup Infrastructure
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])

    session_store = FirestoreSessionStore(db_client, env_config.firestore_collection_prefix)
    repo = FirestoreFactRepository(db_client, env_config)
    llm = GeminiAdapter(api_key=config["GEMINI_API_KEY"])
    embedding = EmbeddingService(api_key=config["GEMINI_API_KEY"])

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
        agent_type="consolidation",
        provider=llm,
        model_name="gemini-3-pro-preview",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=llm.get_capabilities()
    )
    
    # 3. Initialize Agent (to access logic)
    agent_config = AgentConfig(
        agent_id="inspector",
        agent_type="consolidation",
        llm_model="gemini-3-pro-preview"
    )
    
    agent = ConsolidationAgent(
        config=agent_config,
        execution_context=execution_context,
        repository=repo,
        embedding_service=embedding,
        prompt_version=prompt_version,
        prompt_builder=prompt_builder
    )
    
    # 4. Load Session Data
    session = None
    if session_id:
        print(f"📥 Loading session {session_id}...")
        session = await session_store.load_session(session_id)
    else:
        print("⚠️ No session_id provided. Trying to find recent sessions...")
        sessions_ref = db_client.collection(f"{env_config.firestore_collection_prefix}sessions")
        query = sessions_ref.where(filter=firestore.FieldFilter("owner_id", "==", user_id)).limit(1)
        docs = await query.get()
        if docs:
            session_id = docs[0].id
            print(f"✅ Found session: {session_id}")
            session = await session_store.load_session(session_id)
        else:
            print("❌ No sessions found for user.")
            return

    if not session or not session.messages:
        print("❌ Session empty or not found.")
        return
        
    print(f"✅ Loaded session with {len(session.messages)} messages")
    
    # 5. Extract Batch
    target_batch_size = batch_size or 10
    print(f"📦 Extracting batch of {target_batch_size} messages...")
    
    old_messages = session.extract_oldest_messages(count=target_batch_size)
    if not old_messages:
        print("⚠️ No messages available for extraction. Using last messages for inspection.")
        old_messages = session.messages[-target_batch_size:]
    
    # Serialize
    serialized = []
    for msg in old_messages:
        item = {
            "role": msg.role,
            "parts": [{"text": p.text} for p in msg.parts if p.text],
        }
        if hasattr(msg, 'created_at'):
            item["timestamp"] = msg.created_at
        serialized.append(item)
        
    print(f"✅ Batch prepared with {len(serialized)} messages")

    # 6. Build Prompt (using PromptComponentService for full assembly)
    print("🔨 Assembling prompt...")

    # Load template using PromptComponentService (component-based assembly)
    from src.domain.prompt import TEMPLATE_CONSOLIDATION
    template = await component_service.get_assembled_prompt(
        template=TEMPLATE_CONSOLIDATION,
        agent_type="consolidation",  # Triggers AGENT-level component resolution
        user_id=None  # Consolidation doesn't use user context
    )

    print(f"🔍 Template length: {len(template)}")
    print(f"🔍 Template preview (first 500 chars):\n{template[:500]}...")

    # SESSION_26: Use structured conversation and VariableFormatter
    structured_conversation = agent._prepare_structured_conversation(serialized)
    existing_anchors_list = await agent._get_existing_anchors_list(user_id)

    # Fetch biographical context for injection
    try:
        bio_context_raw = await repo.get_biographical_context_cached(user_id, limit=100)
    except Exception as e:
        print(f"⚠️ Failed to load biographical context: {e}")
        bio_context_raw = []

    # Prepare variables for formatting
    variables = {
        "CONVERSATION_INPUT": structured_conversation,
        "BIOGRAPHICAL_CONTEXT": bio_context_raw,
        "EXISTING_ANCHORS": existing_anchors_list
    }

    # Inject variables with formatting (XML for Claude)
    final_prompt = await prompt_builder.inject_variables_with_formatting(
        prompt=template,
        variables=variables,
        template=TEMPLATE_CONSOLIDATION
    )

    # For preview: convert to legacy format
    conv_text = agent._build_conversation_text(serialized)
    existing_anchors_str = await agent._format_existing_anchors(user_id)
    bio_context_str = json.dumps(bio_context_raw, indent=2, ensure_ascii=False)

    # 7. Report
    now = datetime.now()
    date_part = now.strftime("%Y-%m-%d")
    time_part = now.strftime("%H%M%S")
    user_short = user_id[:4]
    report_path = f"reports/prompt/{date_part}-console-{user_short}-{time_part}.md"
    os.makedirs("reports/prompt", exist_ok=True)
    
    report_content = f"""
=================================================
🔍 REAL CONSOLIDATION PROMPT INSPECTION
=================================================
Date: {datetime.now().isoformat()}
User ID: {user_id}
Session ID: {session_id}
Batch Size: {len(serialized)}
Prompt Version: {prompt_version}
=================================================

--- DEBUG: ASSEMBLED PROMPT ---
Template length: {len(template)}
Template preview (first 1000 chars):
{template[:1000]}
=================================================

--- SERIALIZED BATCH SAMPLE (First 2) ---
{json.dumps(serialized[:2], indent=2, default=str)}

--- CONVERSATION INPUT (Injected) ---
{conv_text}

--- EXISTING ANCHORS (Injected) ---
{existing_anchors_str}

--- FINAL ASSEMBLED PROMPT ---
{final_prompt}
"""
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)
        
    print(f"\n✨ DONE! Prompt saved to: {report_path}")
    print("\n--- PREVIEW (Conversation Input) ---")
    print(conv_text[:500] + "..." if len(conv_text) > 500 else conv_text)
    
    if len(existing_anchors_str) > 0:
        print("\n--- PREVIEW (Anchors) ---")
        print(existing_anchors_str[:500] + "..." if len(existing_anchors_str) > 500 else existing_anchors_str)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect Consolidation Agent Prompt with Real Data")
    parser.add_argument("--user-id", help="User UUID (defaults to PROD_USER_ID/DEV_USER_ID from env)")
    parser.add_argument("--session-id", help="Session ID (optional, will find latest if missing)")
    parser.add_argument("--batch-size", type=int, help="Number of messages to include in batch")
    parser.add_argument("--prompt-version", default="v2", help="Prompt version (v2 or legacy)")
    
    args = parser.parse_args()
    user_id = args.user_id or os.getenv("PROD_USER_ID") or os.getenv("DEV_USER_ID")
    if not user_id:
        raise ValueError("USER_ID required: provide --user-id or set PROD_USER_ID/DEV_USER_ID in .env")

    asyncio.run(inspect_prompt(
        user_id=user_id,
        session_id=args.session_id,
        batch_size=args.batch_size,
        prompt_version=args.prompt_version
    ))
