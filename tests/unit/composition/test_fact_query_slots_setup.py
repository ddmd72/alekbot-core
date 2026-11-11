"""The memory-search slots call runs on its own pinned model with thinking low; the router's slots call is unchanged."""
import json
from datetime import datetime, timezone

from src.agents.fact_query_slots_agent import FactQuerySlotsAgent
from src.composition.fact_query_slots_setup import AGENT_TYPE, build_search_slots_agent, search_slots_model
from src.domain.agent import AgentConfig
from src.domain.user import UserBotConfig
from src.services.agent_context_builder import AgentProviderStrategy
from tests.support.scripted_llm import ScriptedLLM, execution_context, text
from tests.unit.agents.test_fact_query_slots_agent import _Builder

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
SLOTS = json.dumps({
    "subject": "Hanna's visit", "kind": "event", "people": ["Hanna"], "places": [],
    "time": {"intent": "future", "from": "", "to": ""}, "domains": [], "domain_link": "one_matter",
    "kinds": [], "scope": "top",
})


class _ContextBuilder:
    """Records what the setup asked for and hands back a context on the scripted model."""

    def __init__(self, llm, model="gemini-flash-lite"):
        self.llm, self.model, self.calls = llm, model, []

    def build(self, agent_type, config):
        self.calls.append((agent_type, config))
        return execution_context(self.llm, agent_type, model=self.model)


def _built(llm, model):
    builder = _ContextBuilder(llm)
    agent = build_search_slots_agent(
        context_builder=builder, user_config=UserBotConfig(timezone="Europe/Madrid"), prompt_builder=_Builder(),
        user_id="usr-1", model=model,
    )
    return agent, builder


async def _ask(agent):
    return await agent.query_for_request("Hanna visits Springfield", path="fm", account_id="acc-1", user_id="usr-1")


async def test_memory_search_slots_call_uses_haiku_with_thinking_low():
    llm = ScriptedLLM([text(SLOTS)])
    agent, builder = _built(llm, search_slots_model(None))
    await _ask(agent)
    assert builder.calls[0][0] == AGENT_TYPE
    request = llm.requests[0]
    assert request.model_name == "claude-haiku-5-5"
    assert request.thinking == "low"


async def test_router_slots_call_keeps_its_model_and_no_thinking():
    llm = ScriptedLLM([text(SLOTS)])
    agent = FactQuerySlotsAgent(
        AgentConfig(agent_id="fact_query_slots_router", agent_type="fact_query_slots"),
        execution_context(llm, "router", model="router-cheap-model"), _Builder(), clock=lambda: NOW,
    )
    await _ask(agent)
    request = llm.requests[0]
    assert request.model_name == "router-cheap-model"
    assert request.thinking is None


async def test_the_knob_overrides_the_default_model():
    llm = ScriptedLLM([text(SLOTS)])
    agent, _ = _built(llm, search_slots_model("  claude-sonnet-5-5 "))
    await _ask(agent)
    assert llm.requests[0].model_name == "claude-sonnet-5-5"


def test_an_empty_knob_is_the_default():
    assert search_slots_model("") == search_slots_model(None) == search_slots_model("   ") == "claude-haiku-5-5"


def test_the_slots_provider_entry_is_claude_only():
    strategy = AgentProviderStrategy.get_strategy(AGENT_TYPE)
    assert strategy["default_provider"] == "claude"
    assert strategy["allowed_providers"] == ["claude"]
