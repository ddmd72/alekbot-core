"""FACT_PROFILE_RFC §9 row 13: the consolidator decides only "directive or not", stored as the boolean `directive`.

`ConsolidationAgent.execute` runs for real over a scripted `LLMPort`: the tool loop, the tool executor, the fact management
adapter, the write service and the repository on the evaluating Firestore fake (index refusal on). Only the model is
scripted. Stage 1 answers its scripted tool turns and then the report; the rulebook review (Stage 2b) answers the report
and its message is kept; the cluster pass is off. Assertions read the stored documents and the rulebook message.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.entities import ContextPriority, FactEntity
from src.domain.fact_lifecycle import is_directive
from src.domain.llm import LLMRequest, LLMResponse
from src.domain.request_context import RequestContext
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, DROP, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore, NoCacheRefresh
from tests.support.scripted_llm import RoutedLLM, execution_context, tools
from tests.support.tool_e2e import StubPromptBuilder

STAGE1 = "Begin deliberate consolidation."
RULEBOOK = "STANDING DIRECTIVES REVIEW"
REPORT = LLMResponse(text='{"operations": []}')
SPAN_END = datetime(2026, 10, 20, 11, tzinfo=timezone.utc)
MESSAGES = [
    {"role": "user", "text": "Please suggest only vegetarian recipes.", "created_at": datetime(2026, 10, 20, 8, tzinfo=timezone.utc).isoformat()},
    {"role": "user", "text": "I play tennis on Sundays.", "created_at": SPAN_END.isoformat()},
]
SEEDED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

KEEP_SHORT = "Suggest only vegetarian recipes."
TENNIS = "Alex plays tennis on Sundays."
ATTRIBUTES = {"temporal_class": "stable", "state": "current", "context_priority": "high", "tags": ["style"],
              "expiry": {"kind": "none"}}
CREATE_DIRECTIVE: Dict[str, Any] = {"content": KEEP_SHORT, "fact_attributes": {"directive": True, **ATTRIBUTES}}
CREATE_PLAIN: Dict[str, Any] = {"content": TENNIS, "fact_attributes": {"directive": False, **ATTRIBUTES}}
MERGED = "Suggest only vegetarian recipes and answer in English."
MERGE_DIRECTIVES: Dict[str, Any] = {"fact_ids": ["seed-short", "seed-english"], "merged_content": MERGED,
                                    "fact_attributes": {"directive": True, **ATTRIBUTES}}


@pytest.fixture
def store() -> FactStore:
    return FactStore(indexes=INDEXES_FILE)


def seed(store: FactStore, doc_id: str, text: str, **over: Any) -> str:
    return put_raw(store, doc_id, text, entity={"reported_date": SEEDED_AT, "created_at": SEEDED_AT, **over})


def seed_old_directive(store: FactStore, doc_id: str, text: str) -> str:
    """A document written before PR 13: the old `domain` marks it, no `directive` field."""
    return put_raw(store, doc_id, text, entity={"reported_date": SEEDED_AT}, domain="agent_directive", directive=DROP)


class Pass:
    """Stage 1 drains `stage1`, then reports; the rulebook review reports, and its message is kept."""

    def __init__(self, *stage1: LLMResponse) -> None:
        self.stage1: List[LLMResponse] = list(stage1)
        self.rulebook: List[str] = []
        self.llm = RoutedLLM(self._answer, pending=lambda: len(self.stage1))

    def _answer(self, request: LLMRequest) -> Any:
        first = request.messages[0].parts[0].text or ""
        if STAGE1 in first:
            return self.stage1.pop(0) if self.stage1 else REPORT
        if RULEBOOK in first:
            self.rulebook.append(first)
            return REPORT
        return None


async def run(store: FactStore, script: Pass):
    port = FactManagementAdapter(store.repo, store.embedder, store.write,
                                 SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20))
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=execution_context(script.llm, "consolidation"),
        repository=store.repo, embedding_service=store.embedder,
        fact_management_port=port, fact_updates=port, prompt_builder=StubPromptBuilder(),
        bio_cache=NoCacheRefresh(),
    )
    agent.INLINE_CLUSTER_REVIEW = False
    message = AgentMessage(task_id="t", sender="consolidation_queue", recipient="consolidation_agent",
                           intent=AgentIntent.DELEGATE, payload={"task": "consolidate", "messages": MESSAGES},
                           context={"user_id": USER, "owner_timezone": "Europe/Berlin"})
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent.execute(message)


def by_text(store: FactStore, text: str) -> Dict[str, Any]:
    (doc,) = [d for d in store.docs.values() if d["text"] == text]
    return doc


def _attributes(tool: str) -> Dict[str, Any]:
    decls = ConsolidationAgent._get_tool_declarations(None)  # type: ignore[arg-type]
    return next(d for d in decls if d["name"] == tool)["parameters"]["properties"]["fact_attributes"]


# ------------------------------------------------------------------------------------------- the tool schema


@pytest.mark.parametrize("tool", ["create_fact", "merge_facts"])
def test_the_tool_schema_requires_a_boolean_directive_and_has_no_domain(tool):
    attributes = _attributes(tool)
    assert attributes["properties"]["directive"]["type"] == "boolean"
    assert "directive" in attributes["required"]
    assert "domain" not in attributes["properties"] and "domain" not in attributes["required"]


# ------------------------------------------------------------------------------------------- the writes


async def test_a_created_fact_carries_the_directive_decision(store):
    script = Pass(tools(("create_fact", CREATE_DIRECTIVE), ("create_fact", CREATE_PLAIN)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    directive, plain = by_text(store, KEEP_SHORT), by_text(store, TENNIS)
    assert directive["directive"] is True and plain["directive"] is False
    assert "domain" not in directive and "domain" not in plain
    assert script.llm.unanswered == 0 and script.llm.pending() == 0


async def test_a_merge_carries_the_directive_decision(store):
    seed(store, "seed-short", "Suggest only vegetarian recipes.", directive=True)
    seed(store, "seed-english", "Answer in English.", directive=True)
    script = Pass(tools(("merge_facts", MERGE_DIRECTIVES)))

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    merged = by_text(store, MERGED)
    assert merged["directive"] is True and merged["state"] == "current"
    assert script.llm.unanswered == 0 and script.llm.pending() == 0


# ------------------------------------------------------------------------------------------- the rulebook


async def test_the_directive_pass_reads_the_rulebook_by_the_flag(store):
    seed(store, "seed-short", "Suggest only vegetarian recipes.", directive=True)
    seed(store, "seed-tennis", TENNIS)
    seed_old_directive(store, "seed-old", "Answer in English.")
    script = Pass()

    response = await run(store, script)

    assert response.status == AgentStatus.SUCCESS, response.error
    [message] = script.rulebook
    assert "seed-short" in message
    assert "seed-tennis" not in message and "seed-old" not in message


async def test_the_cap_backstop_invalidates_past_the_cap_by_the_flag(store):
    cap = ConsolidationAgent.DIRECTIVE_HARD_CAP
    for i in range(cap):
        seed(store, f"rule-{i:02d}", f"Suggest only vegetarian recipes, rule {i}.", directive=True,
             context_priority=ContextPriority.HIGH)
    seed(store, "rule-tail", "Suggest only vegetarian recipes, the least essential rule.", directive=True,
         context_priority=ContextPriority.LOW)
    seed(store, "seed-tennis", TENNIS, context_priority=ContextPriority.LOW)
    seed_old_directive(store, "seed-old", "Answer in English.")

    response = await run(store, Pass())

    assert response.status == AgentStatus.SUCCESS, response.error
    assert store.docs["rule-tail"]["state"] == "invalidated"
    assert all(store.docs[f"rule-{i:02d}"]["state"] == "current" for i in range(cap))
    assert store.docs["seed-tennis"]["state"] == "current" and store.docs["seed-old"]["state"] == "current"


# ------------------------------------------------------------------------------------------- the old documents


async def test_a_stored_document_with_the_old_domain_and_no_flag_loads_as_no_directive(store):
    seed_old_directive(store, "seed-old", "Answer in English.")
    assert "domain" in store.docs["seed-old"] and "directive" not in store.docs["seed-old"]

    fact = await store.repo.get_fact_by_id("seed-old")

    assert isinstance(fact, FactEntity) and fact.directive is False and not is_directive(fact)
    assert not is_directive({"id": "seed-old", "domain": "agent_directive"})
