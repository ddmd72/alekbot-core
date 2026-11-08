"""/voice/delegate applies the allowlist of the call's own mode: with the VOICE_MCP_TOOLS knob on, the plain companion
call (which reads memory through the MCP server) refuses search_memory, an interpreter call keeps it
(LELIK_VOICE_MCP_RFC §8 O4, O7; §11 S3).

The real blueprint, the real `VoiceCallSetupService.prepare` (it writes the call-mode record the route reads), the
real `LelikAgent` over the real coordinator with only `handle_delegation` scripted, the store on the evaluating fake.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.domain.lelik_context import LelikContext
from src.domain.voice_mcp_servers import voice_session_servers
from src.domain.voice_mode import VoiceMode
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.firestore_eval import eval_transactions
from tests.support.voice_mcp_kit import ACCOUNT, DIGEST_KEY, FACTS_URI, TICKET, USER, Kit


def _lelik() -> LelikAgent:
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "you are Lelik"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_usr-1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES["xai"], mcp_servers=voice_session_servers(FACTS_URI),
    )
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    agent.coordinator = AgentCoordinator(registry=registry, task_queue=None, alert_sink=AsyncMock())
    agent.coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result="Garage space 14 behind 12 Oak Street"))
    return agent


async def _delegate(mode: VoiceMode, minting: bool = True, intent: str = "search_memory"):
    kit, agent = Kit(), _lelik()
    provider = AsyncMock(return_value=agent)
    await VoiceCallSetupService(kit.store, kit.alerts, provider, digest_key=DIGEST_KEY).prepare(
        TICKET, USER, ACCOUNT, "web", mode=mode)
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=kit.store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=kit.alerts,
        lelik_agent_provider=provider, digest_key=DIGEST_KEY, voice_mcp_tokens=kit.voice, voice_mcp_minting=minting))
    response = await app.test_client().post("/voice/delegate", json={
        "user_id": USER, "account_id": ACCOUNT, "arguments": {"intent": intent, "query": "where is the garage"},
        "call_context": [], "ticket": TICKET, "call_id": "call-1", "request": "search_memory: garage"})
    return (await response.get_json())["output"], agent.coordinator.handle_delegation


@pytest.fixture(autouse=True)
def transactions():
    with eval_transactions():
        yield


async def test_the_companion_call_with_the_knob_on_refuses_search_memory():
    output, dispatched = await _delegate(VoiceMode.LELIK)

    assert output.startswith("Tool error:")
    dispatched.assert_not_awaited()


async def test_the_companion_call_still_dispatches_its_other_intents():
    output, dispatched = await _delegate(VoiceMode.LELIK, intent="ask_alek")

    assert output == "Garage space 14 behind 12 Oak Street"
    dispatched.assert_awaited_once()


@pytest.mark.parametrize("mode", [VoiceMode.INTERPRETER, VoiceMode.TUTOR])
async def test_an_interpreter_or_tutor_call_keeps_search_memory(mode):
    output, dispatched = await _delegate(mode)

    assert output == "Garage space 14 behind 12 Oak Street"
    dispatched.assert_awaited_once()
