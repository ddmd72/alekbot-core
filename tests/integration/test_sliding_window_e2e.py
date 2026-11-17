import os
import time
import uuid
import random
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime
from typing import List, Dict, Any, Optional

from src.composition.deploy_topology import build_deploy_topology
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.domain.user import UserProfile, UserBotConfig
from src.domain.messaging import MessageContext
from src.domain.agent import AgentMessage, AgentResponse, AgentStatus, AgentIntent
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_repo import FirestoreFactRepository
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.gemini_adapter import GeminiAdapter
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.handlers.conversation_handler import ConversationHandler
from src.composition.user_agent_factory import UserAgentFactory
from src.composition.service_container import ServiceContainer
from src.domain.request_context import RequestContext
from src.ports.llm_port import LLMResponse, ToolCall
from src.utils.logger import logger
from tests.support.aes_gcm_cipher import AesGcmTestCipher

# Constants
TEST_USER_ID = "e2e_test_user_consolidation"
TEST_USER_SLACK_ID = "U_E2E_TEST"
REPORT_DIR = "tests/reports"

# Environment Settings
CLEANUP_ENABLED = os.getenv("E2E_CLEANUP", "true").lower() == "true"
RECREATE_USER = os.getenv("E2E_RECREATE_USER", "false").lower() == "true"

class E2EReport:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filename = f"test_sliding_window_e2e.txt"
        self.path = os.path.join(REPORT_DIR, self.filename)
        os.makedirs(REPORT_DIR, exist_ok=True)
        self.lines = []
        
        self.log(f"\n\n========== E2E RUN: {datetime.now().isoformat()} ==========")
        self.log(f"Test User ID: {user_id}")
        self.log("")

    def log(self, message: str):
        logger.info(message)
        self.lines.append(message)

    def save(self):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(self.lines))
        logger.info(f"📊 E2E Report appended to: {self.path}")

@pytest.mark.asyncio
async def test_sliding_window_e2e():
    """
    Comprehensive E2E test for Sliding Window Consolidation.
    Covers: Overflow, the deliberate consolidation of the overflowed batch (scripted model), and Session Updates.
    """
    report = E2EReport(TEST_USER_ID)
    
    try:
        # 1. SETUP
        report.log("--- STEP 1: Setup Infrastructure ---")
        config = load_settings()
        env_config = config["ENVIRONMENT_CONFIG"]
        
        from google.cloud import firestore
        if env_config.use_emulator:
            db_client = firestore.AsyncClient(project="emulator-project")
            report.log("  🏠 Using Firestore EMULATOR")
        else:
            db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
            report.log(f"  ☁️ Using Firestore CLOUD (mode: {env_config.env.value})")

        account_repo = FirestoreAccountRepository(db_client, env_config.firestore_collection_prefix)
        user_repo = FirestoreUserRepository(db_client, env_config, account_repo)
        fact_repo = FirestoreFactRepository(db_client, env_config)
        
        # Initialize Persistent Test User
        if RECREATE_USER:
            report.log("  🔄 Recreate mode: Deleting old user profile...")
            await db_client.collection(f"{env_config.firestore_collection_prefix}users").document(TEST_USER_ID).delete()
        
        user = await user_repo.get_user(TEST_USER_ID)
        if not user:
            report.log(f"  🆕 Creating new test user: {TEST_USER_ID}")
            user = UserProfile(
                user_id=TEST_USER_ID,
                account_id=f"account-{TEST_USER_ID}",
                display_name="E2E Test User",
                platform_identities={"slack": TEST_USER_SLACK_ID},
                config=UserBotConfig(
                    consolidation_threshold=5,
                    consolidation_batch_size=3,
                ),
            )
            await user_repo.create_user(user)
        else:
            report.log(f"  ✅ Using existing test user: {TEST_USER_ID}")
            # Reset settings
            if not user.account_id:
                user.account_id = f"account-{TEST_USER_ID}"
            user.config.consolidation_threshold = 5
            user.config.consolidation_batch_size = 3
            await user_repo.update_user(user)

        # Initial Clean of data for this user
        await cleanup_test_data(TEST_USER_ID, db_client, env_config, report, True, account_id=user.account_id)
        
        # Initialize Core Components
        coordinator = AgentCoordinator()

        # Consolidation Queue
        consolidation_queue = FirestoreConsolidationQueue(db_client, env_config)

        # Session Store with Overflow Callback
        async def overflow_callback(uid, sid, msgs):
            report.log(f"  🔔 [CALLBACK] Overflow detected for {uid}! Extracted {len(msgs)} messages.")
            batch = ConsolidationBatch(user_id=uid, session_id=sid, messages=[{
                "role": m.role, "parts": [{"text": p.text} for p in m.parts if p.text]
            } for m in msgs])
            await consolidation_queue.enqueue_batch(batch)
            report.log(f"  📦 [CALLBACK] Batch enqueued: {batch.batch_id}")

        session_store = FirestoreSessionStore(
            db_client,
            env_config.firestore_collection_prefix,
            max_history_length=5,
            batch_size=3,
            overflow_callback=overflow_callback
        )

        # Wire all services via ServiceContainer, then override session_store with test one
        container = ServiceContainer(
            cipher=AesGcmTestCipher(),
            config=config,
            db_client=db_client,
            env_config=env_config, deploy_topology=build_deploy_topology({}, env_config),
            account_repo=account_repo,
        )
        container.session_store = session_store


        agent_factory = UserAgentFactory(
            config=config,
            deploy_topology=build_deploy_topology(config, env_config),
            coordinator=coordinator,
            user_repo=user_repo,
            account_repo=account_repo,
            services=container.agent_services()
        )
        
        handler = ConversationHandler(
            coordinator=coordinator,
            agent_factory=agent_factory,
            consolidation_queue=None # Disable auto-processing in test
        )
        
        # 2. ROUND 1: Initial Consolidation
        report.log("\n--- STEP 2: Round 1 - Initial Consolidation ---")
        weight = random.randint(1, 100)
        messages = [
            f"Я вешу {weight}кг",
            "Понял",
            "Я думаю что одна голова хорошо а две лучше",
            "Интересная мысль",
            "Привет",
            "Привет!"
        ]
        
        report.log(f"  📨 Sending 6 messages (weight={weight}kg, anchor=two heads)...")
        for i, text in enumerate(messages):
            # We simulate user and bot messages alternating
            role = "user" if i % 2 == 0 else "bot"
            report.log(f"    [{role.upper()}] {text}")
            
            context = MessageContext(
                session_id=f"session_{report.timestamp}",
                user_id=TEST_USER_ID,
                account_id=user.account_id or TEST_USER_ID,
                text=text,
                metadata={"platform": "slack"}
            )

            # We don't need real Slack response channel for integration test
            resp_channel = AsyncMock()
            resp_channel.send_status_with_phrase = AsyncMock(return_value=("id", "phrase"))
            resp_channel.send_chunked_message = AsyncMock()
            
            # Override coordinator to return simple success to avoid huge LLM chains during flow test
            coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
                task_id="t", agent_id="router", result={"content": "OK"}
            ))
            
            report.log(f"      - Message {i+1} sent")
            await handler.handle_message(context, resp_channel)
            
            # Check current history length
            session = await session_store.load_session(context.session_id)
            report.log(f"      - History size: {len(session.history)}")
            
        # Check if batch was created (with retry as it's async)
        report.log("  🔍 Checking Firestore for consolidation batch...")
        target_batch = None
        for i in range(20): # Increased retry
            await asyncio.sleep(1)
            # Try a direct query to avoid potential index issues with complex filter in get_pending_batches
            docs = await db_client.collection(f"{env_config.firestore_collection_prefix}consolidation_queue").where("user_id", "==", TEST_USER_ID).get()
            report.log(f"    (Attempt {i+1}) Found {len(docs)} batches in total for user via direct query")
            
            batches = await consolidation_queue.get_pending_batches(user_id=TEST_USER_ID, limit=10)
            target_batch = next((b for b in batches if b.user_id == TEST_USER_ID), None)
            if target_batch:
                break
        
        # If not found via port, check direct docs
        if not target_batch and len(docs) > 0:
             report.log(f"    ⚠️ Batch found via direct query but NOT via port. First doc status: {docs[0].to_dict().get('status')}")
             
        assert target_batch is not None, "Batch should have been created via overflow"
        report.log(f"  ✅ Batch created: {target_batch.batch_id} (status: {target_batch.status})")
        assert len(target_batch.messages) == 3, "Batch size should be 3"
        
        # Trigger Manual Processing (Simulate Background Task)
        report.log("  ⏳ Triggering manual consolidation processing (with mocked LLM for stability)...")
        # Get the real consolidation agent
        agents = await agent_factory.ensure_agents_for_user(TEST_USER_ID)
        agent = agents["consolidation_agent"]
        
        # Script the deliberate process: Stage 1 creates the two facts, every later turn (Stage 1's report, the
        # Stage 2a cluster review) reports and writes nothing.
        def create_fact(content: str, fact_type: str, domain: str) -> ToolCall:
            return ToolCall(name="create_fact", args={"content": content, "fact_attributes": {
                "domain": domain, "temporal_class": "stable", "context_priority": "medium", "tags": [domain],
                "metadata": {}, "type": fact_type, "expiry": {"kind": "none"}}})

        stage1 = LLMResponse(text=None, tool_calls=[
            create_fact(f"User weighs {weight}kg", "state", "health"),
            create_fact("One head is good, two is better", "principle", "biographical"),
        ])
        report_only = LLMResponse(text='{"operations": []}')
        scripted = iter([stage1])

        async def scripted_model(*args, **kwargs):
            return next(scripted, report_only)

        agent_msg = AgentMessage.create(
            sender="task_worker",
            recipient=agent.agent_id,
            intent=AgentIntent.DELEGATE,
            payload={
                "task": "consolidate",
                "messages": target_batch.messages
            },
            context={"user_id": TEST_USER_ID}
        )

        mock_prompt = "You are Life Chronicler. Extract facts from the conversation."
        with patch.object(agent._llm, "generate_content", AsyncMock(side_effect=scripted_model)), \
             patch.object(agent.prompt_builder, "build_for_agent", AsyncMock(return_value=mock_prompt)):
            async with RequestContext(user_id=TEST_USER_ID, account_id=user.account_id):
                response = await agent.execute(agent_msg)
        assert response.status == AgentStatus.SUCCESS, response.error
        assert response.metadata["version"] == "v3"
        report.log(f"  ✅ Consolidation successful: {response.result.get('message')}")

        # Delete batch after successful processing (Simulation of Handler logic)
        await consolidation_queue.delete_batch(target_batch.batch_id)
        report.log(f"  ✅ Batch {target_batch.batch_id} DELETED after success")

        # Verify Facts
        active_facts = await fact_repo.get_active_facts(user.account_id)
        report.log(f"  🔍 Verifying saved facts (Total: {len(active_facts)})")

        weight_fact = next((f for f in active_facts if str(weight) in f.text), None)
        anchor_fact = next((f for f in active_facts if "anchor" in f.tags or "PRINCIPLE" == f.type.name), None)

        assert weight_fact is not None, f"Weight fact with {weight} should be saved"
        report.log(f"    ✅ Weight fact: '{weight_fact.text}'")
        assert anchor_fact is not None, "Anchor (principle) should be saved"
        report.log(f"    ✅ Anchor fact: '{anchor_fact.text}'")

        # 3. CLEANUP
        report.log("\n--- STEP 3: Cleanup ---")
        await cleanup_test_data(TEST_USER_ID, db_client, env_config, report, CLEANUP_ENABLED, account_id=user.account_id)
        
        report.log("\n========== ALL CHECKS PASSED ==========")
        
    except Exception as e:
        report.log(f"\n❌ TEST FAILED: {str(e)}")
        import traceback
        report.log(traceback.format_exc())
        raise e
    finally:
        report.save()

async def cleanup_test_data(user_id: str, db_client, env_config, report: E2EReport, enabled: bool, account_id: Optional[str] = None):
    """Clean up sessions, batches and facts."""
    if not enabled:
        report.log(f"  ⚠️ CLEANUP SKIPPED (E2E_CLEANUP=false). User: {user_id}")
        return

    report.log("  🧹 CLEANUP: Removing test data...")
    prefix = env_config.firestore_collection_prefix

    # 1. Delete sessions
    sessions_col = db_client.collection(f"{prefix}sessions")
    sessions = sessions_col.where("owner_id", "==", user_id).stream()
    scount = 0
    async_tasks = []
    async for doc in sessions:
        async_tasks.append(doc.reference.delete())
        scount += 1
    if async_tasks: await asyncio.gather(*async_tasks)
    if scount > 0: report.log(f"    ✅ Deleted {scount} sessions")

    # 2. Delete batches (using correct collection name)
    batches_col = db_client.collection(f"{prefix}consolidation_queue")
    batches = batches_col.where("user_id", "==", user_id).stream()
    bcount = 0
    async_tasks = []
    async for doc in batches:
        async_tasks.append(doc.reference.delete())
        bcount += 1
    if async_tasks: await asyncio.gather(*async_tasks)
    if bcount > 0: report.log(f"    ✅ Deleted {bcount} batches from queue")

    # 3. Delete facts (query by account_id in the correct collection)
    facts_col = db_client.collection(env_config.domain_facts_collection)
    facts_query = account_id or user_id
    facts = facts_col.where("account_id", "==", facts_query).stream()
    fcount = 0
    async_tasks = []
    async for doc in facts:
        async_tasks.append(doc.reference.delete())
        fcount += 1
    if async_tasks: await asyncio.gather(*async_tasks)
    if fcount > 0: report.log(f"    ✅ Deleted {fcount} facts")
    
    # Keep user profile
    report.log(f"    ℹ️  User profile PRESERVED: {user_id}")

if __name__ == "__main__":
    from src.domain.agent import AgentIntent, AgentResponse, AgentStatus
    from src.domain.consolidation import ConsolidationBatch
    from unittest.mock import MagicMock, AsyncMock, patch
    asyncio.run(test_sliding_window_e2e())
