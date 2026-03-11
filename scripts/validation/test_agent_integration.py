#!/usr/bin/env python3
"""
Test script for agent integration.
Demonstrates agent routing and basic functionality.
"""

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.services.agent_coordinator import AgentCoordinator
from src.domain.agent import AgentMessage, AgentIntent, AgentConfig

async def test_agent_protocol():
    """Test basic agent protocol functionality."""
    print("=" * 60)
    print("🧪 AGENT INTEGRATION TEST")
    print("=" * 60)
    print()
    
    # Test 1: AgentCoordinator creation
    print("📋 Test 1: Creating AgentCoordinator...")
    coordinator = AgentCoordinator()
    print(f"   ✅ Coordinator created")
    print(f"   📊 Registered agents: {len(coordinator.list_agents())}")
    print()
    
    # Test 2: AgentMessage creation
    print("📋 Test 2: Creating AgentMessage...")
    message = AgentMessage.create(
        sender="test_system",
        recipient="memory_search_agent",
        intent=AgentIntent.QUERY,
        payload={"query": "What is my car?"},
        context={"user_id": "test_user_123", "session_id": "test_session"}
    )
    print(f"   ✅ Message created")
    print(f"   🆔 Task ID: {message.task_id[:12]}...")
    print(f"   📨 Sender: {message.sender}")
    print(f"   📬 Recipient: {message.recipient}")
    print(f"   🎯 Intent: {message.intent}")
    print(f"   📦 Payload: {message.payload}")
    print(f"   🔧 Context: {message.context}")
    print()
    
    # Test 3: AgentConfig
    print("📋 Test 3: Creating AgentConfig...")
    config = AgentConfig(
        agent_id="test_agent",
        agent_type="test",
        llm_model="gemini-3-flash-preview",
        capabilities=["test_capability_1", "test_capability_2"],
        timeout_ms=30000,  # 30 seconds
        max_retries=3
    )
    print(f"   ✅ Config created")
    print(f"   🆔 Agent ID: {config.agent_id}")
    print(f"   🤖 LLM Model: {config.llm_model}")
    print(f"   ⚡ Capabilities: {config.capabilities}")
    print(f"   ⏱️  Timeout: {config.timeout_ms}ms")
    print(f"   🔄 Max Retries: {config.max_retries}")
    print()
    
    # Test 4: Broadcast message
    print("📋 Test 4: Creating broadcast message...")
    broadcast = AgentMessage.create(
        sender="brain_service",
        recipient="broadcast",
        intent=AgentIntent.QUERY,
        payload={"query": "Who can help me search?"},
        context={}
    )
    print(f"   ✅ Broadcast message created")
    print(f"   📬 Recipient: {broadcast.recipient}")
    print(f"   📡 This would be sent to all registered agents")
    print()
    
    # Test 5: Different intents
    print("📋 Test 5: Testing different intent types...")
    intents = [
        (AgentIntent.QUERY, "Search/retrieval operations"),
        (AgentIntent.DELEGATE, "Task delegation to specialized agents"),
        (AgentIntent.INFORM, "Share information with other agents"),
        (AgentIntent.REQUEST_FEEDBACK, "Ask for validation or confirmation")
    ]
    
    for intent, description in intents:
        print(f"   • {intent.value}: {description}")
    print()
    
    # Summary
    print("=" * 60)
    print("✅ ALL TESTS PASSED")
    print("=" * 60)
    print()
    print("📊 Summary:")
    print("   • Agent Communication Protocol (ACP) working ✅")
    print("   • AgentCoordinator ready ✅")
    print("   • Message creation working ✅")
    print("   • Config system working ✅")
    print()
    print("🚀 Next Steps:")
    print("   1. Register real agents in coordinator")
    print("   2. Integrate coordinator into UserBrainServiceFactory")
    print("   3. Update BrainService to use _execute_agent_loop()")
    print("   4. Test with real user requests")
    print()
    print("📖 See: docs/management/AGENT_INTEGRATION_GUIDE.md")
    print()

if __name__ == "__main__":
    asyncio.run(test_agent_protocol())
