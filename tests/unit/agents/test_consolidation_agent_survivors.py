"""The consolidator's prompt belongs to its tenant (survivors of the weekly mutation run, 2026-10-27).

Every pass builds its prompt through `PromptBuilderPort.build_for_agent`. The builder's semantics (the port's docstring
does not state them; `PromptBuilder.build_for_agent` and `PromptAssemblyService._assemble_static_template` do): the
account and the user pick the ACCOUNT- and USER-level overrides, and facts the caller passes are rendered as given (only
`None` makes the builder read the account's cache itself). The fake below keeps those semantics: per-tenant override
text, its own account cache for a `None` fact list. Real passes, the real fact adapter on the evaluating fake store, a
scripted LLM; the assertions read the prompt the model received, never the builder's call arguments.

The six `consolidation` profile tokens are `non_overridable` in today's profile data, so a dropped user or account id
changes no rendered prompt with the real builder today; these tests pin the agent's side of the contract (forward the
tenant), which holds for any profile, not the current data (reviewer ruling, 2026-10-27).
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.email import IndexedEmail
from src.domain.entities import FactDomain
from src.domain.llm import LLMResponse, ToolCall
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, OTHER, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore

OTHER_USER = "usr-2"
REPORT = LLMResponse(text='{"operations": []}')
STAGE1 = "Begin deliberate consolidation."
CLUSTER = "FACT CLUSTER REVIEW"
RULEBOOK = "STANDING DIRECTIVES REVIEW"
EMAIL = "[system_alert]"
ATTRS = {"domain": "biographical", "temporal_class": "stable", "context_priority": "medium", "tags": ["t"],
         "metadata": {}, "type": "event", "expiry": {"kind": "none"}}
PASSED = [{"id": "b-1", "text": "the user's mother is Maria", "domain": "biographical", "tags": []}]


class TenantPrompts:
    """A prompt builder with the port's tenant semantics: the account's and the user's override text go into the
    prompt of that tenant only; facts passed are rendered as given, `None` reads the account's cache."""

    USER_OVERRIDES = {USER: "USER OVERRIDE of usr-1", OTHER_USER: "USER OVERRIDE of usr-2"}
    ACCOUNT_OVERRIDES = {ACC: "ACCOUNT OVERRIDE of acc-1", OTHER: "ACCOUNT OVERRIDE of acc-2"}
    CACHE = {ACC: [{"id": "c-1", "text": "stale cached copy", "domain": "biographical", "tags": []}]}

    async def build_for_agent(
        self,
        agent_type: str,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None,
        routing_metadata: Any = None,
        capabilities: Any = None,
        biographical_facts: Optional[List[Dict[str, Any]]] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        include_biographical: bool = True,
        include_directives: bool = True,
        **_: Any,
    ) -> str:
        if biographical_facts is None:
            biographical_facts = list(self.CACHE.get(account_id, [])) if account_id else []
        facts = [f["text"] for f in biographical_facts
                 if include_directives or f.get("domain") != FactDomain.AGENT_DIRECTIVE.value]
        return "\n".join([
            f"class {agent_type}",
            self.ACCOUNT_OVERRIDES.get(account_id or "", "no account override"),
            self.USER_OVERRIDES.get(user_id or "", "no user override"),
            f"facts: {facts}",
        ])

    def invalidate_biographical_cache(self, account_id: str) -> None:
        pass


def tenant_prompt(facts: List[str]) -> str:
    return "\n".join(["class consolidation", "ACCOUNT OVERRIDE of acc-1", "USER OVERRIDE of usr-1", f"facts: {facts}"])


def create(content: str) -> ToolCall:
    return ToolCall(name="create_fact", args={"content": content, "fact_attributes": dict(ATTRS)})


class OneBatchOfEmails:
    def __init__(self) -> None:
        when = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
        self.batch = [IndexedEmail(
            email_id="e1", user_id=USER, account_id=ACC, source="gmail", text="mail e1", tags=["mail"],
            category="travel", metadata={"subject": "e1"}, subject="e1", from_address="a@example.com",
            email_date=when, indexed_at=when,
        )]

    async def get_unconsolidated_batch(self, user_id: str, limit: int = 10) -> List[IndexedEmail]:
        return self.batch

    async def mark_consolidated(self, user_id: str, ids: List[str], when: Any) -> None:
        self.batch = []


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def make_agent(store: FactStore, *, email_repo: Any = None) -> ConsolidationAgent:
    """An agent whose LLM writes one fact on Stage 1 and reports on every other turn; `agent.seen` keeps
    (first message text, system instruction) of every pass."""
    port = FactManagementAdapter(
        store.repo, store.embedder, store.write, SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20)
    )
    execution = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"), execution_context=execution,
        repository=store.repo, embedding_service=store.embedder, fact_write_service=MagicMock(),
        fact_management_port=port, fact_updates=port, prompt_builder=TenantPrompts(), indexed_email_repo=email_repo,
    )
    seen: List[Tuple[str, str]] = []
    agent.seen = seen
    wrote: set = set()

    async def refresh(**_: Any) -> None:
        return None

    store.repo.refresh_biographical_context_cache = refresh

    async def call(request, turn=1):
        first = request.messages[0].parts[0].text
        if len(request.messages) == 1:
            agent.seen.append((first, request.system_instruction))
        if STAGE1 in first and len(request.messages) == 1 and STAGE1 not in wrote:
            wrote.add(STAGE1)
            return LLMResponse(text=None, tool_calls=[create("the user wants the Springfield news every morning")])
        return REPORT

    agent._call_llm = call
    return agent


def message(task: str, payload: Dict[str, Any]) -> AgentMessage:
    return AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                        payload={"task": task, **payload}, context={"user_id": USER, "owner_timezone": "UTC"})


def prompt_of(agent: ConsolidationAgent, marker: str) -> str:
    (system,) = [system for first, system in agent.seen if marker in first]
    return system


async def test_stage_1_2a_and_2b_prompts_are_the_tenants_and_carry_the_facts_passed(store):
    put_raw(store, "d-1", "never use emojis", entity={"domain": FactDomain.AGENT_DIRECTIVE})
    agent = make_agent(store)
    agent.INLINE_CLUSTER_REVIEW = True
    batch = [{"role": "user", "text": "remind me to read the Springfield news every morning", "created_at": "2026-10-22T10:00:00Z"}]
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(batch, USER, ACC, PASSED, owner_timezone="UTC")

    expected = tenant_prompt(["the user's mother is Maria"])
    assert prompt_of(agent, STAGE1) == expected
    assert prompt_of(agent, CLUSTER) == expected
    assert prompt_of(agent, RULEBOOK) == expected


async def test_stage_2b_renders_an_empty_fact_list_as_passed_not_the_accounts_cache(store):
    put_raw(store, "d-1", "never use emojis", entity={"domain": FactDomain.AGENT_DIRECTIVE})
    agent = make_agent(store)
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._review_directives(USER, ACC, [], agent._write_context("stage2b"))
    assert prompt_of(agent, RULEBOOK) == tenant_prompt([])


async def test_the_cluster_task_prompt_is_the_tenants(store):
    agent = make_agent(store)
    cluster = [{"fact_id": "x", "content": "c", "similarity": 0.5}]
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent._handle_consolidate_cluster(
            message("consolidate_cluster", {"cluster": cluster}), USER, ACC, PASSED, 0.0)
    assert response.status.value == "success", response
    assert prompt_of(agent, CLUSTER) == tenant_prompt(["the user's mother is Maria"])


async def test_the_email_task_prompt_is_the_tenants(store):
    agent = make_agent(store, email_repo=OneBatchOfEmails())
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent._handle_consolidate_email(
            message("consolidate_email", {"number_of_batches": 1}), USER, ACC, PASSED, 0.0)
    assert response.status.value == "success", response
    assert prompt_of(agent, EMAIL) == tenant_prompt(["the user's mother is Maria"])


async def test_the_legacy_v2_prompt_is_the_tenants(store):
    agent = make_agent(store)
    agent.prompt_version = "v2"
    batch = [{"role": "user", "text": "remind me to read the Springfield news every morning", "created_at": "2026-10-22T10:00:00Z"}]
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent._handle_consolidate(message("consolidate", {"messages": batch}), USER, ACC, PASSED, 0.0)
    assert response.status.value == "success", response
    ((first, _system),) = agent.seen
    assert first == tenant_prompt(["the user's mother is Maria"])  # v2 sends the built prompt as the user turn
