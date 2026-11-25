"""P14 for the memory inbox triage (MCP_MEMORY_SUBMISSIONS_RFC §7.4): every field the triage model can send to
`record_candidate` and `discard_submissions`, to the stored inbox documents.

The real `MemoryInboxTriagePass` routes through the real `AgentCoordinator` to a real `ConsolidationAgent` over a scripted
model (`tests/support/scripted_llm.py`): the agent's `_call_llm`, its loop and tool routing, the triage scope the pass
hands it, and the pass's `apply_triage` on the real inbox store over the evaluating fake. The facts the relation cites
live on the evaluating fake behind the real repository. Only the model is scripted. Every assertion reads a stored
document. The scripted calls are builders (module-level functions), since the submission ids exist only after seeding.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig
from src.domain.consolidation import consolidation_agent_id
from src.domain.memory_inbox import Basis, SubmissionDraft, SubmittingClient
from src.domain.memory_inbox_runs import PassRequest
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.memory_inbox_triage_scope import MemoryInboxTriageScope
from src.services.memory_inbox_triage_pass import MemoryInboxTriagePass
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import FactStore, NoCacheRefresh
from tests.support.memory_inbox_store import inbox_store
from tests.support.scripted_llm import ScriptedLLM, execution_context, text, tools
from tests.support.tool_e2e import StubPromptBuilder
from tests.support.tool_fields import unused

AT = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
CURRENT_ID, OLD_ID = "f-dentist-2", "f-dentist-1"
CURRENT_TEXT = "User's dentist is Dr. Marta Quillen at Clinica Ribeira"
CLARA = "Clara Moreira and the user plan a weekend in Coimbra"
GYM = "Alex plays tennis on Sundays"
REPORT = text('{"candidates": 2, "discarded": 1}')


def RECORD_EVERY_FIELD(first: str, second: str) -> Dict[str, Any]:
    return {
        "submission_ids": [first, second], "text": CLARA, "happens_when": "March 6 to 8, 2027",
        "said_when": "mid-November 2026", "basis": "document", "directive": True, "temporal_class": "DYNAMIC",
        "context_priority": "high", "expiry": {"kind": "date", "date": "2027-03-08"}, "relation": "refines",
        "fact_id": OLD_ID,
    }


def RECORD_HORIZON(submission: str) -> Dict[str, Any]:
    return {
        "submission_ids": [submission], "text": GYM, "happens_when": "every Sunday", "said_when": "", "basis": "said",
        "directive": False, "temporal_class": "dynamic", "context_priority": "medium",
        "expiry": {"kind": "horizon", "days": 90}, "relation": "new", "fact_id": "",
    }


def DISCARD(submission: str) -> Dict[str, Any]:
    return {"submission_ids": [submission], "reason": "known"}


class World:
    """Four received submissions, the cited facts, and the pass wired to the real agent."""

    def __init__(self) -> None:
        self.inbox = inbox_store()
        self.facts = FactStore()
        put_raw(self.facts, OLD_ID, "User's dentist is Dr. Marta Quillen", entity={"lineage_id": "lin-dentist"},
                state="superseded", superseded_by=CURRENT_ID, is_current=False)
        put_raw(self.facts, CURRENT_ID, CURRENT_TEXT, entity={"lineage_id": "lin-dentist", "version": 2})
        self.ids: List[str] = []

    async def seed(self) -> List[str]:
        drafts = [SubmissionDraft(information=f"submission {n}", basis=Basis.SAID, said_when="", happens_when="")
                  for n in range(4)]
        await self.inbox.repo.add_submissions(USER, ACC, SubmittingClient("c1", "Claude"), drafts,
                                              AT - timedelta(days=1), "2026-11-24")
        self.ids = [s.submission_id for s in await self.inbox.repo.received(USER, 10)]
        return self.ids

    async def run(self, script: Callable[[List[str]], List[Any]]) -> ScriptedLLM:
        llm = ScriptedLLM(script(await self.seed()))
        port = FactManagementAdapter(self.facts.repo, self.facts.embedder, self.facts.write,
                                     SearchEnrichmentService(self.facts.repo, self.facts.embedder, 7, 10, 10, 20))
        agent = ConsolidationAgent(
            config=AgentConfig(agent_id=consolidation_agent_id(USER), agent_type="consolidation"),
            execution_context=execution_context(llm, "consolidation"), repository=self.facts.repo,
            embedding_service=self.facts.embedder, fact_management_port=port, fact_updates=port,
            prompt_builder=StubPromptBuilder(), bio_cache=NoCacheRefresh(),
        )
        coordinator = AgentCoordinator()
        coordinator.register_agent(agent)

        async def ensure(user_id: str) -> None:
            return None

        triage = MemoryInboxTriagePass(
            store=self.inbox.repo, coordinator=coordinator, ensure_agents=ensure, clock=lambda: AT,
            new_scope=lambda ids, account_id: MemoryInboxTriageScope(ids, account_id, self.facts.repo))
        outcome = await triage.run(PassRequest(user_id=USER, account_id=ACC, zone="Europe/Lisbon", language="en",
                                               now=AT, deadline=AT + timedelta(minutes=30)))
        assert outcome.status == "completed"
        return llm

    def candidate_citing(self, submission: str) -> Dict[str, Any]:
        (doc,) = [d for d in self.inbox.of_status("candidate").values() if submission in d["source_ids"]]
        return doc


def script_spent(llm: ScriptedLLM) -> None:
    assert llm.unanswered == 0 and llm.pending() == 0


async def test_a_candidate_with_every_field_is_stored():
    world = World()
    llm = await world.run(lambda ids: [tools(("record_candidate", RECORD_EVERY_FIELD(ids[0], ids[1]))), REPORT])
    doc = world.candidate_citing(world.ids[0])
    assert doc["source_ids"] == world.ids[:2]
    assert {k: doc[k] for k in ("text", "happens_when", "said_when", "basis", "language")} == \
           {"text": CLARA, "happens_when": "March 6 to 8, 2027", "said_when": "mid-November 2026", "basis": "document",
            "language": "en"}
    assert doc["draft"] == {"directive": True, "temporal_class": "dynamic", "context_priority": "high",
                            "expiry": {"kind": "date", "date": "2027-03-08"}}
    assert doc["relation"] == {"kind": "refines", "fact_id": CURRENT_ID, "fact_text": CURRENT_TEXT}
    assert [world.inbox.docs[i]["status"] for i in world.ids[:2]] == ["triaged", "triaged"]
    script_spent(llm)


async def test_a_horizon_expiry_is_stored():
    world = World()
    llm = await world.run(lambda ids: [tools(("record_candidate", RECORD_HORIZON(ids[2]))), REPORT])
    doc = world.candidate_citing(world.ids[2])
    assert doc["draft"]["expiry"] == {"kind": "horizon", "days": 90}
    assert doc["relation"] == {"kind": "new", "fact_id": None, "fact_text": None}
    script_spent(llm)


async def test_a_discard_stores_its_submissions_and_reason():
    world = World()
    llm = await world.run(lambda ids: [tools(("discard_submissions", DISCARD(ids[3]))), REPORT])
    stored = world.inbox.docs[world.ids[3]]
    assert (stored["status"], stored["discard_reason"]) == ("discarded", "known")
    assert [world.inbox.docs[i]["triage_offers"] for i in world.ids[:3]] == [1, 1, 1]
    script_spent(llm)


def test_the_scripted_calls_use_every_declared_field():
    assert unused("consolidation_triage", "record_candidate", [RECORD_EVERY_FIELD("a", "b"), RECORD_HORIZON("c")]) == []
    assert unused("consolidation_triage", "discard_submissions", [DISCARD("d")]) == []
