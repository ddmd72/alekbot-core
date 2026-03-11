import asyncio
import json
import os
import sys
from typing import List

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from src.adapters.gemini_adapter import GeminiAdapter
from src.config.settings import load_settings
from src.ports.llm_service import Message, MessagePart
from src.agents.core.router_agent import create_router_agent
from src.infrastructure.agent_coordinator import AgentCoordinator

async def test_semantic_lens():
    print("🧪 Testing Semantic Lens Extraction...")
    config = load_settings()
    llm = GeminiAdapter(api_key=config["GEMINI_API_KEY"])
    
    # 1. Test Triage Keyword Extraction
    prompt_path = "src/agents/prompts/triage_router_v1.groovy"
    with open(prompt_path, 'r') as f:
        system_prompt = f.read()
        
    test_queries = [
        "Как дела с моим проектом по медицине?",
        "Яка погода в Валенсії?",
        "Напомни мои замеры для костюма",
        "Что там с тестированием нового роутера?"
    ]
    
    for query in test_queries:
        print(f"\nQuery: {query}")
        response = await llm.generate_content(
            model_name="gemini-3-flash-preview",
            system_instruction=system_prompt,
            messages=[Message(role="user", parts=[MessagePart(text=query)])],
            temperature=0.0
        )
        
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:-3].strip()
        elif raw_text.startswith("```"):
            raw_text = raw_text[3:-3].strip()
            
        try:
            decision = json.loads(raw_text)
            lens = decision.get("semantic_lens", [])
            print(f"✅ Extracted Lens: {lens}")
            if len(lens) != 5:
                print(f"❌ Expected 5 keywords, got {len(lens)}")
        except Exception as e:
            print(f"❌ Failed to parse JSON: {e}")
            print(f"Raw: {raw_text}")

if __name__ == "__main__":
    asyncio.run(test_semantic_lens())
