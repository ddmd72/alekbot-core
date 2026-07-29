"""fetch_url prompt contract + web_search default tier.

The fetch_url system prompt is an inline constant (tracked exception, see
IMPLEMENTATION_ROADMAP.md TD-6) and it is the production path for ~73% of the morning
briefing's web_search cost. Its wording was tuned against gpt-5.4-nano on 2026-07-29
(scripts/websearch/tune_fetch_prompt.py); these tests pin the properties that tuning
established, so a future edit cannot silently undo them.

What cannot be unit-tested is model behaviour. What CAN be pinned is the contract the
measurements identified: both request shapes are addressed, and the self-contradicting
"complete page text" instruction never comes back.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.web_search_agent import WebSearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.llm import LLMRequest, LLMResponse
from src.infrastructure.agent_config import WEB_SEARCH
from src.domain.user import _DEFAULT_AGENT_TIERS, PerformanceTier, UserBotConfig
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities


ECO_MODEL = "gpt-5.4-nano"
AGENT_MODEL = "gpt-5.6-luna"


@pytest.fixture
def mock_llm():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(return_value=LLMResponse(text="content"))
    # Port contract: get_model_for_tier returns a provider-specific model name.
    llm.get_model_for_tier = MagicMock(return_value=ECO_MODEL)
    return llm


@pytest.fixture
def agent(mock_llm):
    ec = AgentExecutionContext(
        agent_type="websearch",
        provider=mock_llm,
        model_name=AGENT_MODEL,
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
    )
    return WebSearchAgent(
        config=AgentConfig(agent_id="web_agent", agent_type="web_search",
                           llm_model=AGENT_MODEL),
        execution_context=ec,
        prompt_builder=MagicMock(build_for_agent=AsyncMock(return_value="cognitive_process {}")),
    )


def _sent(agent) -> LLMRequest:
    return agent._llm.generate_content.call_args.kwargs["request"]


class TestFetchPromptWording:

    def test_does_not_demand_the_complete_page(self):
        """The contradiction that made a small model return nav menus instead of news.

        The per-call user message asks for specific extracted items; a system prompt
        demanding the whole page fights it. luna ignored the conflict, nano obeyed it.
        """
        prompt = WebSearchAgent._FALLBACK_FETCH_SYSTEM.lower()
        assert "without omissions" not in prompt
        assert "complete page text" not in prompt

    def test_addresses_the_no_request_shape(self):
        """Bare-URL fetches are a first-class shape, not an afterthought.

        `_handle_fetch_url` sends the URL alone when there is no query, so the prompt
        must say what to do when the request specifies nothing.
        """
        prompt = WebSearchAgent._FALLBACK_FETCH_SYSTEM.lower()
        assert "if it states nothing" in prompt

    def test_addresses_the_explicit_request_shape(self):
        prompt = WebSearchAgent._FALLBACK_FETCH_SYSTEM.lower()
        assert "if the request states what to extract" in prompt

    def test_excludes_page_furniture(self):
        """Named exclusions are what took chrome markers to zero on nano."""
        prompt = WebSearchAgent._FALLBACK_FETCH_SYSTEM.lower()
        for junk in ("navigation", "menus", "buttons", "cookie"):
            assert junk in prompt, f"missing exclusion: {junk}"

    def test_requires_source_urls(self):
        """The briefing links every item; without URLs the report cannot cite."""
        assert "url for every item" in WebSearchAgent._FALLBACK_FETCH_SYSTEM.lower()

    def test_keeps_plain_text_output_contract(self):
        """fetch_url must not return JSON — the adapter suppresses response_schema
        under grounding, so this prompt line is the only thing holding the format."""
        prompt = WebSearchAgent._FALLBACK_FETCH_SYSTEM
        assert "No JSON" in prompt and "No code blocks" in prompt


class TestFetchRequestShapes:

    @pytest.mark.asyncio
    async def test_bare_url_sends_only_the_url(self, agent):
        message = AgentMessage.create(
            sender="t", recipient="web_agent", intent=AgentIntent.QUERY,
            payload={"url": "https://example.com/feed"},
        )
        await agent.execute(message)
        assert _sent(agent).messages[0].parts[0].text == "https://example.com/feed"

    @pytest.mark.asyncio
    async def test_query_is_prepended_to_the_url(self, agent):
        message = AgentMessage.create(
            sender="t", recipient="web_agent", intent=AgentIntent.QUERY,
            payload={"url": "https://example.com/feed", "query": "last 24h only"},
        )
        await agent.execute(message)
        text = _sent(agent).messages[0].parts[0].text
        assert text == "last 24h only\n\nhttps://example.com/feed"

    @pytest.mark.asyncio
    async def test_both_shapes_use_the_same_system_prompt(self, agent):
        """One prompt serves both shapes — that is why it must describe both."""
        for payload in ({"url": "https://example.com"},
                        {"url": "https://example.com", "query": "q"}):
            agent._llm.generate_content.reset_mock()
            await agent.execute(AgentMessage.create(
                sender="t", recipient="web_agent", intent=AgentIntent.QUERY, payload=payload))
            assert _sent(agent).system_instruction == WebSearchAgent._FALLBACK_FETCH_SYSTEM

    @pytest.mark.asyncio
    async def test_grounding_stays_enabled(self, agent):
        """Without native grounding there is no fetch at all."""
        await agent.execute(AgentMessage.create(
            sender="t", recipient="web_agent", intent=AgentIntent.QUERY,
            payload={"url": "https://example.com"}))
        assert _sent(agent).use_grounding is True


class TestPerIntentTier:
    """The cheap tier applies to `fetch_url` only — `search_web` keeps the agent's tier.

    Measured 2026-07-29: on fetch_url the ECO model matched BALANCED on extracted items at
    ~4.7x lower cost; on real user search_web queries it lost ~25% of findings, ran 2x
    slower (79s outlier vs a 90s agent timeout) and dropped the JSON shape on 1 of 7.
    """

    def test_agent_tier_stays_balanced(self):
        assert _DEFAULT_AGENT_TIERS["web_search"] == PerformanceTier.BALANCED

    def test_resolves_to_balanced_without_stored_override(self):
        assert UserBotConfig().get_tier_for_agent("web_search") == PerformanceTier.BALANCED

    def test_stored_override_still_wins(self):
        """Guards the resolution order: a stored per-user tier beats the class default."""
        cfg = UserBotConfig(agent_tiers={"web_search": PerformanceTier.ECO})
        assert cfg.get_tier_for_agent("web_search") == PerformanceTier.ECO

    def test_fetch_url_tier_is_eco(self):
        assert WEB_SEARCH.fetch_url_tier == PerformanceTier.ECO

    def test_fetch_model_resolved_through_the_port(self, agent, mock_llm):
        """The agent hands a TIER to the provider and never names a model itself."""
        assert agent._fetch_model_name() == ECO_MODEL
        mock_llm.get_model_for_tier.assert_called_once_with(PerformanceTier.ECO)

    def test_disabled_tier_uses_the_agent_model(self, agent, mock_llm):
        """fetch_url_tier=None is the documented off switch."""
        with patch.object(WEB_SEARCH, "fetch_url_tier", None):
            assert agent._fetch_model_name() == AGENT_MODEL
        mock_llm.get_model_for_tier.assert_not_called()

    def test_unsupported_tier_degrades_to_the_agent_model(self, agent, mock_llm):
        """A provider without this tier must not break fetching — degrade, don't raise."""
        mock_llm.get_model_for_tier.side_effect = ValueError("no ECO for this provider")
        assert agent._fetch_model_name() == AGENT_MODEL


class TestPerIntentModelReachesTheRequest:
    """End of the chain: the resolved model must actually land on the LLMRequest."""

    @pytest.mark.asyncio
    async def test_fetch_url_request_uses_the_cheap_model(self, agent):
        await agent.execute(AgentMessage.create(
            sender="t", recipient="web_agent", intent=AgentIntent.QUERY,
            payload={"url": "https://example.com"}))
        assert _sent(agent).model_name == ECO_MODEL

    @pytest.mark.asyncio
    async def test_search_web_request_keeps_the_agent_model(self, agent):
        await agent.execute(AgentMessage.create(
            sender="t", recipient="web_agent", intent=AgentIntent.QUERY,
            payload={"query": "who won the match"}, context={}))
        assert _sent(agent).model_name == AGENT_MODEL

    @pytest.mark.asyncio
    async def test_the_two_intents_do_not_share_a_model(self, agent):
        """The whole point of the split — regression guard if someone re-reads
        self.model_name inside _call_grounded_llm."""
        await agent.execute(AgentMessage.create(
            sender="t", recipient="web_agent", intent=AgentIntent.QUERY,
            payload={"url": "https://example.com"}))
        fetch_model = _sent(agent).model_name
        agent._llm.generate_content.reset_mock()
        await agent.execute(AgentMessage.create(
            sender="t", recipient="web_agent", intent=AgentIntent.QUERY,
            payload={"query": "q"}, context={}))
        assert fetch_model != _sent(agent).model_name
