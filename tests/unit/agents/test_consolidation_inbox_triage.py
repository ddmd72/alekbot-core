"""The consolidation agent's `triage_memory_inbox` task (MCP_MEMORY_SUBMISSIONS_RFC §7.1, §7.4): `ConsolidationAgent.execute`
runs for real over a scripted model (`tests/support/scripted_llm.py`): its `_call_llm`, the tool loop and the tool routing,
with the facts on the evaluating fake behind the real repository and management adapter. The pass builds the triage
profile without directives, offers exactly its four tools, writes no fact, and answers the scope's decisions with
`ended` and `failed`, also after a turn cap or a raise."""
from datetime import datetime, timezone
from typing import Any, Dict, List

import asyncio

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.consolidation import INBOX_TRIAGE_PROFILE_ID, TRIAGE_AGENT_TASK
from src.domain.fact_write_context import FactWriteContext
from src.domain.memory_inbox import Basis, DiscardReason, RelationKind, Submission, SubmissionStatus
from src.domain.memory_inbox_triage_task import (
    INBOX_TRIAGE_HEADER,
    TRIAGE_SCOPE_KEY,
    TriageTask,
    decode_triage_result,
    encode_triage_task,
)
from src.domain.pass_actor import PassActor
from src.domain.request_context import RequestContext
from src.infrastructure.memory_inbox_triage_scope import MemoryInboxTriageScope
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import FactStore, NoCacheRefresh
from tests.support.scripted_llm import (
    RoutedLLM,
    ScriptedLLM,
    execution_context,
    first_user_text,
    text,
    tool_results,
    tools,
)
from tests.support.tool_e2e import StubPromptBuilder

LISBON = "Europe/Lisbon"
AT = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
REPORT = text("Triage done.")
GYM = "Alex plays tennis on Sundays"

RECORD: Dict[str, Any] = dict(
    submission_ids=["s1"], text=GYM, happens_when="every Sunday", said_when="", basis="said", directive=False,
    temporal_class="dynamic", context_priority="medium", expiry={"kind": "horizon", "days": 90}, relation="new",
)
DISCARD: Dict[str, Any] = {"submission_ids": ["s2"], "reason": "trivial"}
SEARCH: Dict[str, Any] = {"keywords": ["tennis", "Sundays"], "primary_query": GYM}
CREATE: Dict[str, Any] = {"content": GYM, "fact_attributes": {"directive": False, "temporal_class": "dynamic",
                                                               "context_priority": "medium", "tags": ["gym"],
                                                               "expiry": {"kind": "none"}}}


def submission(sid: str, information: str) -> Submission:
    return Submission(submission_id=sid, user_id=USER, account_id=ACC, status=SubmissionStatus.RECEIVED, received_at=AT,
                      client_id="c1", client_name="Claude", information=information, basis=Basis.SAID, said_when="",
                      happens_when="")


TASK = TriageTask(submissions=(submission("s1", GYM), submission("s2", "The user had coffee this morning")),
                  waiting=(), answered=(), language="en")


def make_agent(llm: Any, prompts: StubPromptBuilder) -> ConsolidationAgent:
    store = FactStore()
    put_raw(store, "f-dentist", "User's dentist is Dr. Marta Quillen")
    port = FactManagementAdapter(store.repo, store.embedder, store.write,
                                 SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20))
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=execution_context(llm, "consolidation"),
        repository=store.repo, embedding_service=store.embedder,
        fact_management_port=port, fact_updates=port, prompt_builder=prompts, bio_cache=NoCacheRefresh(),
    )
    agent.store = store
    return agent


async def run(*script: Any, prompts: StubPromptBuilder = None) -> tuple:
    llm = ScriptedLLM(script)
    agent = make_agent(llm, prompts or StubPromptBuilder())
    message = AgentMessage(task_id="t", sender="memory_inbox_triage_pass", recipient="consolidation_agent",
                           intent=AgentIntent.DELEGATE, payload={"task": TRIAGE_AGENT_TASK, **encode_triage_task(TASK)},
                           context={"user_id": USER, "owner_timezone": LISBON})
    with RequestContext(user_id=USER, account_id=ACC):
        response: AgentResponse = await agent.execute(message)
    return response, llm, agent


def names(declarations: List[Dict[str, Any]]) -> set:
    return {d["name"] for d in declarations}


async def test_the_pass_builds_the_triage_profile_without_directives():
    prompts = StubPromptBuilder()
    response, llm, _ = await run(REPORT, prompts=prompts)
    (call,) = prompts.calls
    assert (call["agent_type"], call["include_directives"]) == (INBOX_TRIAGE_PROFILE_ID, False)
    assert first_user_text(llm).startswith(INBOX_TRIAGE_HEADER.split("{")[0])
    assert response.status is AgentStatus.SUCCESS
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_the_triage_tools_are_exactly_four():
    assert names(ConsolidationAgent._get_tool_declarations(None, "triage")) == \
           {"search_existing_facts", "count_words", "record_candidate", "discard_submissions"}
    _, llm, _ = await run(REPORT)
    assert names(llm.requests[0].tools) == {"search_existing_facts", "count_words", "record_candidate",
                                            "discard_submissions"}


async def test_a_write_tool_call_is_unknown_and_writes_nothing():
    response, llm, agent = await run(tools(("create_fact", CREATE), ("update_fact", {"fact_id": "f-dentist",
                                                                                    "updates": {"state": "archived"}})),
                                     REPORT)
    assert [r for _, r in tool_results(llm)] == [{"error": "Unknown tool: create_fact"},
                                                 {"error": "Unknown tool: update_fact"}]
    assert list(agent.store.docs) == ["f-dentist"] and agent.store.docs["f-dentist"]["state"] == "current"
    assert decode_triage_result(response.result)[0].candidates == ()
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_the_result_carries_the_decisions_and_ended():
    response, llm, _ = await run(tools(("search_existing_facts", SEARCH), ("count_words", {"text": GYM})),
                                 tools(("record_candidate", RECORD), ("discard_submissions", DISCARD)), REPORT)
    decisions, ended, failed = decode_triage_result(response.result)
    (candidate,) = decisions.candidates
    assert (candidate.source_ids, candidate.fields.text, candidate.relation.kind) == (("s1",), GYM, RelationKind.NEW)
    assert dict(decisions.discards) == {"s2": DiscardReason.TRIVIAL}
    assert (ended, failed) == (True, False)
    results = dict(tool_results(llm))
    assert results["count_words"] == {"word_count": 5, "limit": 60, "within_limit": True, "excess": 0}
    assert "error" not in results["search_existing_facts"]
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_a_raising_loop_still_returns_its_decisions_as_failed():
    response, llm, _ = await run(tools(("record_candidate", RECORD)), RuntimeError("model down"))
    decisions, ended, failed = decode_triage_result(response.result)
    assert response.status is AgentStatus.SUCCESS
    assert [c.source_ids for c in decisions.candidates] == [("s1",)]
    assert (ended, failed) == (False, True)
    assert llm.pending() == 0


async def test_a_turn_cap_returns_the_decisions_made_before_it_not_ended():
    llm = ScriptedLLM([tools(("record_candidate", RECORD)), tools(("discard_submissions", DISCARD))])
    agent = make_agent(llm, StubPromptBuilder())
    agent.MAX_CONSOLIDATION_TURNS = 2
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": TRIAGE_AGENT_TASK, **encode_triage_task(TASK)},
                           context={"user_id": USER, "owner_timezone": LISBON})
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)
    decisions, ended, failed = decode_triage_result(response.result)
    assert (len(decisions.candidates), dict(decisions.discards), ended, failed) == \
           (1, {"s2": DiscardReason.TRIVIAL}, False, False)
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_a_payload_without_a_task_is_a_failure():
    llm = ScriptedLLM([])
    agent = make_agent(llm, StubPromptBuilder())
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": TRIAGE_AGENT_TASK}, context={"user_id": USER})
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)
    assert response.status is AgentStatus.FAILED and llm.requests == []


async def test_a_triage_pass_refuses_another_actor():
    llm = ScriptedLLM([])
    agent = make_agent(llm, StubPromptBuilder())
    scope = MemoryInboxTriageScope(["s1"], ACC, agent.store.repo)
    stage1 = FactWriteContext.for_pass(PassActor.STAGE1, "UTC", None)
    triage = FactWriteContext.for_pass(PassActor.INBOX_TRIAGE, "UTC", None)
    for kwargs in (dict(pass_kind="triage", write_context=stage1, triage_scope=scope),
                   dict(pass_kind="consolidation", write_context=triage),
                   dict(pass_kind="triage", write_context=triage),
                   dict(pass_kind="consolidation", write_context=stage1, triage_scope=scope)):
        with pytest.raises(ValueError):
            await agent._run_consolidation_loop("m", "s", USER, ACC, **kwargs)
    assert llm.requests == []


async def test_a_timed_out_execution_leaves_its_decisions_in_the_scope_it_was_handed():
    answers = [tools(("record_candidate", RECORD))]

    async def handler(request):
        if answers:
            return answers.pop(0)
        await asyncio.sleep(30)            # the model hangs: the agent's timeout cancels the execution

    llm = RoutedLLM(handler)
    agent = make_agent(llm, StubPromptBuilder())
    scope = MemoryInboxTriageScope(["s1", "s2"], ACC, agent.store.repo)
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": TRIAGE_AGENT_TASK, **encode_triage_task(TASK), TRIAGE_SCOPE_KEY: scope},
                           context={"user_id": USER, "owner_timezone": LISBON, "suppress_transient_retry": True},
                           timeout_ms=200)
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.process(message)
    assert response.status is AgentStatus.TIMEOUT
    assert [c.source_ids for c in scope.decisions().candidates] == [("s1",)]
    assert len(llm.requests) == 2 and not answers


async def test_a_handed_scope_over_other_submissions_is_refused():
    llm = ScriptedLLM([])
    agent = make_agent(llm, StubPromptBuilder())
    scope = MemoryInboxTriageScope(["s1"], ACC, agent.store.repo)
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": TRIAGE_AGENT_TASK, **encode_triage_task(TASK), TRIAGE_SCOPE_KEY: scope},
                           context={"user_id": USER, "owner_timezone": LISBON})
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)
    assert response.status is AgentStatus.FAILED and llm.requests == []
