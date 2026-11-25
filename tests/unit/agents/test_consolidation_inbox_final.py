"""The consolidation agent's `consolidate_memory_inbox` task (MCP_MEMORY_SUBMISSIONS_RFC §9.1, §9.3, §9.4):
`ConsolidationAgent.execute` runs for real over a scripted model (`tests/support/scripted_llm.py`): its `_call_llm`, the
tool loop and the tool routing, with the facts on the evaluating fake behind the real repository and management adapter.
The pass builds the inbox profile without directives under actor `inbox`; a model `reported_date` is clamped into
`final_span` (a missing one is the newest receipt); the result says `ended` and how many writes the pass made; the
baseline is refreshed after a pass that wrote, also when its loop raised."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.consolidation import FINAL_AGENT_TASK, INBOX_PROFILE_ID
from src.domain.memory_inbox import (
    ANSWER_WINDOW,
    SAID_WHEN_LOOKBACK,
    Basis,
    Candidate,
    CandidateStatus,
    DraftClass,
    Relation,
    RelationKind,
    SourceCopy,
)
from src.domain.memory_inbox_final_task import (
    INBOX_CANDIDATES_HEADER,
    decode_final_result,
    encode_final_task,
)
from src.domain.request_context import RequestContext
from src.ports.biographical_cache_port import BiographicalCacheRefresher
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import FactStore
from tests.support.scripted_llm import ScriptedLLM, execution_context, first_user_text, text, tools
from tests.support.tool_e2e import StubPromptBuilder

UTC = timezone.utc
LISBON = "Europe/Lisbon"
OLDEST = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
NEWEST = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
REPORT = text("Consolidation done.")
GYM = "Alex plays tennis on Sundays."
SEEDED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def candidate(cid: str, received: datetime) -> Candidate:
    source = SourceCopy(submission_id=f"s-{cid}", received_at=received, client_name="Claude",
                        information="Alex trains with Robin", basis=Basis.SAID, said_when="", happens_when="")
    return Candidate(
        candidate_id=cid, user_id=USER, account_id=ACC, status=CandidateStatus.CONFIRMED, created_at=received,
        answer_by=received + ANSWER_WINDOW, language="en", source_ids=(source.submission_id,), sources=(source,),
        text=GYM, happens_when="every Sunday", said_when="", basis=Basis.SAID,
        draft=DraftClass(directive=False, temporal_class="dynamic", context_priority="medium",
                         expiry={"kind": "none"}),
        relation=Relation(RelationKind.NEW, None, None), proposed=None, edited=False,
        confirmed_at=received + timedelta(hours=2),
    )


CANDIDATES = (candidate("k2", NEWEST), candidate("k1", OLDEST))


def create(reported_date: Optional[datetime]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {"directive": False, "temporal_class": "dynamic", "context_priority": "medium",
                                  "tags": ["gym"], "expiry": {"kind": "none"}}
    if reported_date is not None:
        attributes["reported_date"] = reported_date.isoformat()
    return {"content": GYM, "fact_attributes": attributes}


CLOSE: Dict[str, Any] = {"fact_id": "seed-fitlife", "updates": {"state": "archived", "expiry": {"kind": "none"}}}


class CountingRefresh(BiographicalCacheRefresher):
    def __init__(self) -> None:
        self.owners: List[str] = []

    async def refresh_cache(self, owner_id: str, facts_limit: Optional[int] = None,
                            principles_limit: Optional[int] = None) -> None:
        self.owners.append(owner_id)


def make_agent(llm: Any, prompts: StubPromptBuilder, refresh: BiographicalCacheRefresher) -> ConsolidationAgent:
    store = FactStore()
    put_raw(store, "seed-fitlife", "User trains at the FitLife gym on Main Street.",
            entity={"reported_date": SEEDED_AT})
    port = FactManagementAdapter(store.repo, store.embedder, store.write,
                                 SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20))
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"),
        execution_context=execution_context(llm, "consolidation"),
        repository=store.repo, embedding_service=store.embedder,
        fact_management_port=port, fact_updates=port, prompt_builder=prompts, bio_cache=refresh,
    )
    agent.store = store
    return agent


async def run(*script: Any, prompts: Optional[StubPromptBuilder] = None, turns: Optional[int] = None,
              refresh: Optional[CountingRefresh] = None) -> tuple:
    llm = ScriptedLLM(script)
    agent = make_agent(llm, prompts or StubPromptBuilder(), refresh or CountingRefresh())
    if turns is not None:
        agent.MAX_CONSOLIDATION_TURNS = turns
    message = AgentMessage(task_id="t", sender="memory_inbox_final_pass", recipient="consolidation_agent",
                           intent=AgentIntent.DELEGATE, payload={"task": FINAL_AGENT_TASK, **encode_final_task(CANDIDATES)},
                           context={"user_id": USER, "owner_timezone": LISBON})
    with RequestContext(user_id=USER, account_id=ACC):
        response: AgentResponse = await agent.execute(message)
    return response, llm, agent


def created(agent: ConsolidationAgent) -> Dict[str, Any]:
    (doc,) = [d for fid, d in agent.store.docs.items() if fid != "seed-fitlife"]
    return doc


def as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


async def test_the_pass_runs_the_inbox_profile_under_the_inbox_actor():
    prompts = StubPromptBuilder()
    response, llm, agent = await run(tools(("update_fact", CLOSE)), REPORT, prompts=prompts)
    (call,) = prompts.calls
    assert (call["agent_type"], call["include_directives"]) == (INBOX_PROFILE_ID, False)
    lines = first_user_text(llm).split("\n")
    assert lines[0] == INBOX_CANDIDATES_HEADER
    assert [json.loads(line)["candidate_id"] for line in lines[1:3]] == ["k1", "k2"]
    closed = agent.store.docs["seed-fitlife"]
    assert closed["state"] == "archived" and closed["lifecycle_events"][-1]["actor"] == "inbox"
    assert response.status is AgentStatus.SUCCESS and decode_final_result(response.result) == (True, 1)
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_a_reported_date_after_the_newest_receipt_is_clamped_down():
    _, llm, agent = await run(tools(("create_fact", create(NEWEST + timedelta(days=3)))), REPORT)
    assert as_utc(created(agent)["reported_date"]) == NEWEST
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_a_reported_date_far_back_is_bounded_by_the_lookback():
    _, _, agent = await run(tools(("create_fact", create(OLDEST - timedelta(days=900)))), REPORT)
    assert as_utc(created(agent)["reported_date"]) == OLDEST - SAID_WHEN_LOOKBACK
    inside = OLDEST - timedelta(days=40)
    _, _, agent = await run(tools(("create_fact", create(inside))), REPORT)
    assert as_utc(created(agent)["reported_date"]) == inside


async def test_a_missing_reported_date_falls_to_the_newest_receipt():
    _, _, agent = await run(tools(("create_fact", create(None))), REPORT)
    assert as_utc(created(agent)["reported_date"]) == NEWEST


async def test_ended_is_true_on_a_report_and_false_on_a_turn_cap():
    response, _, _ = await run(tools(("create_fact", create(None))), REPORT)
    assert decode_final_result(response.result) == (True, 1)
    response, llm, _ = await run(tools(("create_fact", create(None))), tools(("update_fact", CLOSE)), turns=2)
    assert response.status is AgentStatus.SUCCESS and decode_final_result(response.result) == (False, 2)
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_the_baseline_is_refreshed_after_a_write_also_when_the_loop_raises():
    refresh = CountingRefresh()
    response, _, agent = await run(tools(("create_fact", create(None))), RuntimeError("model down"), refresh=refresh)
    assert response.status is AgentStatus.SUCCESS and decode_final_result(response.result) == (False, 1)
    assert refresh.owners == [ACC] and created(agent)["state"] == "current"
    refresh = CountingRefresh()
    response, _, _ = await run(REPORT, refresh=refresh)
    assert decode_final_result(response.result) == (True, 0) and refresh.owners == []


async def test_a_payload_without_candidates_is_a_failure():
    llm = ScriptedLLM([])
    agent = make_agent(llm, StubPromptBuilder(), CountingRefresh())
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": FINAL_AGENT_TASK}, context={"user_id": USER})
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)
    assert response.status is AgentStatus.FAILED and llm.requests == []
