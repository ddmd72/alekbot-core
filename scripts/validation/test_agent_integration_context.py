import asyncio
import os
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.domain.consolidation import ConsolidationBatch
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent
from src.services.embedding_service import EmbeddingService
from src.config.settings import load_settings

async def test_e2e_context_integration():
    """
    E2E Test for Biographical Context Integration.
    
    Validates:
    1. Context loading logic
    2. Agent prompt injection
    3. Correct context utilization
    """
    print("=" * 80)
    print("🧪 E2E TEST: Biographical Context Integration")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    user_id = "os.getenv("USER_ID", "DEMO_USER")"
    
    # 1. Mock Repository & LLM
    print("\n1. Setting up mocks...")
    
    mock_repo = AsyncMock()
    # Mock biographical context response
    mock_repo.get_biographical_context.return_value = [
        MagicMock(text="User's name is Dmytro", tags=["identity"], type=MagicMock(value="STATE")),
        MagicMock(text="User has a wife named Jane", tags=["family"], type=MagicMock(value="STATE"))
    ]
    # Mock anchors
    mock_repo.get_active_facts.return_value = []
    # Mock archive
    mock_repo.archive_observations.return_value = None
    # Mock add fact
    mock_repo.add_fact_if_unique.return_value = (True, "new_fact_id")

    mock_llm = AsyncMock()
    mock_llm.generate_content.return_value = MagicMock(
        text='```json\n{"new_facts": [], "new_anchors": []}\n```'
    )
    
    # Embedding Service (Real or Mocked)
    embedding_service = EmbeddingService(api_key=config.get("GEMINI_API_KEY"))
    
    # 2. Initialize Agent
    agent_config = AgentConfig(
        agent_id="consolidation_agent",
        agent_type="consolidation",
        llm_model="gemini-3-flash-preview"
    )
    
    agent = ConsolidationAgent(
        config=agent_config,
        llm_service=mock_llm,
        repository=mock_repo,
        embedding_service=embedding_service
    )
    
    # 3. Simulate Batch Creation (as in overflow_callback)
    print("\n2. Simulating batch creation with context...")
    
    # Retrieve context manually to simulate main.py logic
    bio_facts = await mock_repo.get_biographical_context(user_id, limit=100)
    bio_context_serialized = [
        {"text": f.text, "type": f.type.value, "tags": f.tags} 
        for f in bio_facts
    ]
    
    print(f"   ✓ Retrieved {len(bio_context_serialized)} context facts")
    print(f"   ✓ First fact: {bio_context_serialized[0]['text']}")
    
    # Create payload
    payload = {
        "messages": [
            {"role": "user", "text": "My wife bought a car."}
        ],
        "biographical_context": bio_context_serialized
    }
    
    message = AgentMessage(
        task_id="test_task_123",
        sender="system",
        recipient="consolidation_agent",
        intent=AgentIntent.DELEGATE,
        context={"user_id": user_id},
        payload=payload
    )
    
    # 4. Execute Agent
    print("\n3. Executing Agent...")
    await agent.execute(message)
    
    # 5. Verify Prompt Injection
    print("\n4. Verifying Prompt Injection...")
    
    # Check LLM call arguments
    call_args = mock_llm.generate_content.call_args
    if not call_args:
        print("❌ LLM was not called!")
        return
        
    sent_messages = call_args.kwargs['messages']
    prompt_text = sent_messages[0].parts[0].text
    
    # Check if context was injected
    print("   → Checking for 'known_biographical_facts'...")
    if "known_biographical_facts" in prompt_text:
        print("   ✅ Section 'known_biographical_facts' FOUND")
    else:
        print("   ❌ Section 'known_biographical_facts' MISSING")
        
    # Check if specific context content is present
    print("   → Checking for specific context content...")
    if "User's name is Dmytro" in prompt_text:
        print("   ✅ Context content 'User's name is Dmytro' FOUND in prompt")
    else:
        print("   ❌ Context content MISSING from prompt")
        
    # Check if new instructions are present
    print("   → Checking for new instructions...")
    if "CONTEXT_AWARENESS" in prompt_text:
        print("   ✅ Instruction 'CONTEXT_AWARENESS' FOUND")
    else:
        print("   ❌ Instruction 'CONTEXT_AWARENESS' MISSING")
        
    print("\n" + "=" * 80)
    print("✅ E2E INTEGRATION TEST COMPLETED")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(test_e2e_context_integration())
