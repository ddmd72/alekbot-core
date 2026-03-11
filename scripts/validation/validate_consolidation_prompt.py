import asyncio
import json
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.embedding_service import EmbeddingService
from src.adapters.gemini_adapter import GeminiAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent

class ValidationReport:
    def __init__(self):
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filename = f"consolidation_validation_report.txt"
        self.path = os.path.join("tests/reports", self.filename)
        os.makedirs("tests/reports", exist_ok=True)
        self.lines = []
        self.log(f"========== CONSOLIDATION AGENT VALIDATION: {datetime.now().isoformat()} ==========\n")

    def log(self, message: str):
        print(message)
        self.lines.append(message)

    def save(self):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(self.lines))
        print(f"\n📄 Report saved to: {self.path}")

async def validate_consolidation_prompt():
    report = ValidationReport()
    
    # 1. Setup
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    from google.cloud import firestore
    db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    repo = FirestoreFactRepository(db_client, env_config)
    llm = GeminiAdapter(api_key=config["GEMINI_API_KEY"])
    embedding = EmbeddingService(api_key=config["GEMINI_API_KEY"])
    
    agent_config = AgentConfig(
        agent_id="validator_chronicler",
        agent_type="consolidation",
        llm_model="models/gemini-3-pro-preview"
    )
    
    agent = ConsolidationAgent(agent_config, llm, repo, embedding)
    
    # Use real user ID for context
    user_id = "os.getenv("USER_ID", "DEMO_USER")" 
    
    # --- SCENARIO 1: RICH FACTS + ANCHORS (10 Messages) ---
    report.log("\n🧪 SCENARIO 1: Rich Facts + Anchors (10 Messages)")
    messages_1 = [
        {"role": "user", "text": "Я начал заниматься йогой каждое утро."},
        {"role": "alek_bot", "text": "Это отличная привычка! Как долго длятся ваши занятия?"},
        {"role": "user", "text": "Обычно около 30 минут. Это помогает мне сфокусироваться."},
        {"role": "alek_bot", "text": "Звучит здорово. Вы давно практикуете?"},
        {"role": "user", "text": "Только начал в январе 2026. Кстати, это напоминает мне нашу поездку в Париж в 2020 году, там было так же спокойно."},
        {"role": "alek_bot", "text": "Путешествия часто дарят нам такие моменты."},
        # Existing Anchor: "User prefers to avoid interactions... high emotional intelligence..."
        # We'll simulate a user statement that REPEATS this sentiment
        {"role": "user", "text": "Да, я вообще не люблю сложные эмоциональные разговоры. Мне проще когда все четко и по делу."}, 
        {"role": "alek_bot", "text": "Понимаю, ясность важна."},
        # New Anchor: Something about technology or learning style
        {"role": "user", "text": "Я считаю, что если ты не учишься новому каждый день, ты деградируешь. Это мой главный принцип."},
        {"role": "alek_bot", "text": "Сильное утверждение!"}
    ]
    
    msg_1 = AgentMessage.create(
        sender="validator",
        recipient=agent.agent_id,
        intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": messages_1},
        context={"user_id": user_id}
    )
    
    response_1 = await agent.execute(msg_1)
    
    if response_1.status == "success":
        result = response_1.result
        report.log(f"✅ Success! Found {result['new_facts']} facts, {result['new_anchors']} anchors.")
        report.log("\n--- LLM JSON PAYLOAD ---")
        report.log(json.dumps(response_1.metadata.get("llm_payload", {}), indent=2, ensure_ascii=False))
    else:
        report.log(f"❌ Failed: {response_1.error}")

    # --- SCENARIO 2: SMALL TALK (Empty Result Expected) ---
    report.log("\n🧪 SCENARIO 2: Small Talk (No Facts)")
    messages_2 = [
        {"role": "user", "text": "Привет!"},
        {"role": "alek_bot", "text": "Привет! Как дела?"},
        {"role": "user", "text": "Все хорошо, спасибо. Как сам?"},
        {"role": "alek_bot", "text": "Тоже отлично. Чем могу помочь?"},
    ]
    
    msg_2 = AgentMessage.create(
        sender="validator",
        recipient=agent.agent_id,
        intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": messages_2},
        context={"user_id": user_id}
    )
    
    response_2 = await agent.execute(msg_2)
    
    if response_2.status == "success":
        result = response_2.result
        report.log(f"✅ Success! Found {result['new_facts']} facts, {result['new_anchors']} anchors.")
        report.log("\n--- LLM JSON PAYLOAD ---")
        report.log(json.dumps(response_2.metadata.get("llm_payload", {}), indent=2, ensure_ascii=False))
        
        if result['new_facts'] == 0 and result['new_anchors'] == 0:
            report.log("   -> Correctly identified NO facts.")
        else:
            report.log("   -> ⚠️ WARNING: Found facts in small talk!")
    else:
        report.log(f"❌ Failed: {response_2.error}")

    report.save()

if __name__ == "__main__":
    asyncio.run(validate_consolidation_prompt())
