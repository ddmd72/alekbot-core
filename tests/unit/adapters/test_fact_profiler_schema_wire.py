"""Wire test for the fact profiler (FACT_PROFILE_RFC §4.1, docs/how_to/ADAPTER_WIRE_TESTING.md), mocked at the Anthropic
SDK boundary: the agent's request goes through a real `ClaudeAdapter` behind `CachingLLMProxy` with the cache config the
strategy resolves, and reaches `messages.stream` as the measured mechanism: the pinned model, adaptive thinking with
effort high, the strict-shaped schema in `output_config.format`, the static system part with `cache_control`, the user
content byte-equal to the fingerprint's message, and no tools (so no `deliver_response` path)."""
import json
from unittest.mock import AsyncMock, MagicMock

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.fact_profiler_agent import AGENT_TYPE, FactProfilerAgent, response_schema
from src.domain.agent import AgentConfig
from src.domain.fact_profile_fingerprint import build_profiler_user_message
from src.domain.fact_profiler_reply import ProfilerStatus
from src.domain.llm import ProviderCapabilities
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, PROMPT_CACHE_BOUNDARY
from src.services.caching_llm_proxy import CachingLLMProxy
from src.services.prompt_cache_strategy import PromptCacheStrategy
from tests.unit.agents.drive_model_fakes import PROMPT, prompt_builder
from tests.unit.agents.test_fact_profiler_agent import ANSWER, ROME, ROME_TEXT

MODEL = "claude-sonnet-5-5"


def _claude(answer):
    adapter = ClaudeAdapter(api_key="k")
    captured = {}
    block = MagicMock()
    block.type, block.text = "text", json.dumps(answer)
    usage = MagicMock(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(return_value=MagicMock(content=[block], usage=usage,
                                                                stop_reason="end_turn"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream
    return adapter, captured


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


async def _run(text, metadata):
    adapter, captured = _claude(ANSWER)
    capabilities = adapter.get_capabilities()
    cache = PromptCacheStrategy().resolve(AGENT_TYPE, ProviderCapabilities(context_caching=True))
    context = AgentExecutionContext(agent_type=AGENT_TYPE, provider=CachingLLMProxy(adapter, cache),
                                    model_name=MODEL, tier=PerformanceTier.BALANCED, capabilities=capabilities,
                                    provider_name="claude", resilience_port=InMemoryProviderResilience())
    prompt = PROMPT + "\n\n" + PROMPT_CACHE_BOUNDARY
    agent = FactProfilerAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE), context,
                              prompt_builder(prompt))
    reply = await agent.profile("acc-1", text, metadata)
    return reply, captured


async def test_the_request_reaches_the_sdk_as_the_measured_mechanism():
    reply, sent = await _run(ROME_TEXT, ROME)
    assert reply.status is ProfilerStatus.OK
    assert sent["model"] == MODEL
    assert sent["thinking"] == {"type": "adaptive"}
    schema = sent["output_config"]["format"]["schema"]
    assert sent["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": schema}}
    assert schema == response_schema()
    assert all(obj["additionalProperties"] is False for obj in _objects(schema))
    assert "tools" not in sent or not sent["tools"]
    assert sent["max_tokens"] == 16_000


async def test_the_static_system_part_is_cached_and_the_user_content_is_the_fingerprints_message():
    _, sent = await _run(ROME_TEXT, ROME)
    assert sent["system"] == [{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}]
    messages = sent["messages"]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    assert messages[0]["content"] == [{"type": "text", "text": build_profiler_user_message(ROME_TEXT, ROME)}]
