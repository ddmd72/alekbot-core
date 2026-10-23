"""Facts S2 · T5: a consolidation pass over the real adapter, repository and search on the evaluating fake (offline).

A scripted LLM drives Stage 1 through the real tool executor: it updates one fact twice with the id it first saw. The second
update must land on the current version, and the Stage 2a cluster must be seeded with, and read back, the new ids.
"""
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig
from src.domain.llm import LLMResponse, ToolCall
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx

REPORT = LLMResponse(text='{"operations": []}')


def update(fact_id, content):
    return LLMResponse(text=None, tool_calls=[ToolCall(name="update_fact", args={"fact_id": fact_id, "updates": {"content": content, "expiry": EXPIRY_NONE}})])


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def build_agent(store):
    search = SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20)
    port = FactManagementAdapter(store.repo, store.embedder, store.write, search)
    pb = MagicMock()
    pb.build_for_agent = AsyncMock(return_value="SYSTEM")
    pb.invalidate_biographical_cache = MagicMock()
    ctx = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"), execution_context=ctx,
        repository=store.repo, embedding_service=store.embedder, fact_write_service=MagicMock(),
        fact_management_port=port, prompt_builder=pb,
    )
    agent.INLINE_CLUSTER_REVIEW = True
    return agent, port


async def test_stage1_updates_one_fact_twice_the_second_lands_on_the_current_version_and_stage2a_is_seeded_with_it(store):
    agent, port = build_agent(store)
    first = (await port.create_fact("mother is Valentina", {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {}, "expiry": EXPIRY_NONE},
        context=ctx()))["fact_id"]
    llm_script: List[LLMResponse] = [update(first, "mother is Valentina Ivanova"), update(first, "mother is Valentina Petrova")]
    requests: List[str] = []

    async def call(request, turn=1):
        text = request.messages[0].parts[0].text
        requests.append(text)
        if "FACT CLUSTER REVIEW" in text:
            return REPORT
        return llm_script.pop(0) if llm_script else REPORT

    agent._call_llm = call
    seeds: List[list] = []
    real_cluster = agent._build_review_cluster

    async def spy_cluster(changed, account_id, *, timezone):
        seeds.append(list(changed))
        return await real_cluster(changed, account_id, timezone=timezone)

    agent._build_review_cluster = spy_cluster

    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3([{"role": "user", "text": "mother's surname"}], USER, ACC, [])

    chain = sorted((d for d in store.docs.values() if d["lineage_id"] == store.docs[first]["lineage_id"]), key=lambda d: d["version"])
    assert [d["version"] for d in chain] == [1, 2, 3]                                   # no fork: v3 is built on v2
    assert [d["state"] for d in chain] == ["superseded", "superseded", "current"]
    assert [d["text"] for d in chain][1:] == ["mother is Valentina Ivanova", "mother is Valentina Petrova"]
    v2, v3 = chain[1]["id"], chain[2]["id"]
    assert chain[2]["replaces_fact_id"] == v2 and chain[1]["replaces_fact_id"] == first

    (seed,) = seeds                                                                      # Stage 2a ran once, on the new ids
    assert seed == [(v2, "mother is Valentina Ivanova"), (v3, "mother is Valentina Petrova")]
    cluster_message = next(t for t in requests if "FACT CLUSTER REVIEW" in t)
    assert v3 in cluster_message and first not in cluster_message and v2 not in cluster_message
