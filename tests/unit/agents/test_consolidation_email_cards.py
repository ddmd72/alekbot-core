"""Stage 3 reading email cards (email index v2 §6.7, slice 9): with a batch port the email pass builds its prompt for
`consolidation_email`, sends each carded mail as its filtered card and every other one as its headers (slice 9b), under
the code header, and marks every mail of the batch. The mail, its cards, the
marks and the people list live on the evaluating fake behind the real repositories; the model is scripted and only the
messages it was sent are read."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
from src.adapters.firestore_email_card_store import FirestoreEmailCardStore
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.email import IndexedEmail
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import CardExpect, pending_fields, settled_fields
from src.domain.email_memory_candidate import EMAIL_CANDIDATES_HEADER, header_candidate
from src.domain.llm import LLMResponse, ToolCall
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import FactStore, NoCacheRefresh, make_env_config
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer_json, mail

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE, PEOPLE_COL = "eval_email_facts", "eval_email_state", "eval_confirmed_people"
LISBON = "Europe/Lisbon"
REPORT = LLMResponse(text='{"operations": []}')
OLD = datetime(2026, 10, 1, 8, 0)                     # indexed long before the wait ends
SENT = datetime(2025, 6, 2, 9, 30)


class RecordingPrompts:
    """The prompt builder's two calls the email pass makes: the agent type each prompt was built for, or a failure."""

    def __init__(self, fail: bool = False) -> None:
        self.built: List[str] = []
        self.invalidated: List[str] = []
        self.fail = fail

    async def build_for_agent(self, *, agent_type: str, **_: Any) -> str:
        if self.fail:
            raise KeyError(f"profile {agent_type} not found")
        self.built.append(agent_type)
        return f"SYSTEM PROMPT FOR {agent_type}"

    def invalidate_biographical_cache(self, account_id: str) -> None:
        self.invalidated.append(account_id)


@pytest.fixture
def world():
    with eval_transactions():
        db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                                   STATE: "development_email_indexing_state"})
        env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
        people_env = make_env_config()
        people_env.confirmed_people_collection = PEOPLE_COL
        yield SimpleNamespace(db=db, emails=FirestoreIndexedEmailRepository(db, env),
                              cards=FirestoreEmailCardStore(db, env),
                              people=FirestoreConfirmedPeopleRepository(db, people_env))


def make_agent(world, *, batches: bool = True, people: bool = True, prompts: Optional[RecordingPrompts] = None,
               responses: Optional[List[LLMResponse]] = None, emails: Optional[Any] = None):
    store = FactStore()
    port = FactManagementAdapter(
        store.repo, store.embedder, store.write, SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20))
    execution = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"), execution_context=execution,
        repository=store.repo, embedding_service=store.embedder, fact_write_service=MagicMock(),
        fact_management_port=port, fact_updates=port, prompt_builder=prompts or RecordingPrompts(),
        indexed_email_repo=emails or world.emails, email_memory_batches=(emails or world.emails) if batches else None,
        confirmed_people=world.people if people else None,
        bio_cache=NoCacheRefresh(),
    )
    agent.firsts = []
    agent.systems = []
    agent.store = store
    script = list(responses or [])

    async def call(request, turn=1):
        if len(request.messages) == 1:
            agent.firsts.append(request.messages[0].parts[0].text)
            agent.systems.append(request.system_instruction)
        return script.pop(0) if script else REPORT

    agent._call_llm = call
    return agent


def email(email_id: str, indexed: datetime = OLD, **over: Any) -> IndexedEmail:
    fields: Dict[str, Any] = dict(
        email_id=email_id, user_id=USER, account_id=ACC, source="gmail", text=f"Fact sentence of {email_id}.",
        tags=["mail"], category="travel", metadata={"subject": email_id}, subject=f"Subject {email_id}",
        from_address="sender@example.com", email_date=SENT, indexed_at=indexed)
    fields.update(over)
    return IndexedEmail(**fields)


def booking_fields():
    m = mail()
    inp = build_card_input(m, BOOKING_BODY, [], [], direct=True, nonce=NONCE)
    card = build_card(parse_mail_answer(answer_json()), m, [], inp.evidence, PEOPLE)
    return settled_fields(card, digest="dg", model="flash-lite", now=datetime.now(timezone.utc), vector_pending=False)


async def run(agent, **payload):
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": "consolidate_email", **payload},
                           context={"user_id": USER, "owner_timezone": LISBON})
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent._handle_consolidate_email(message, USER, ACC, [], 0.0)


def numbered(text: str) -> List[Dict[str, Any]]:
    return [json.loads(line.split(". ", 1)[1]) for line in text.splitlines() if re.match(r"^\d+\. \{", line)]


async def marks(world, *email_ids: str) -> List[Any]:
    found = await world.emails.get_consolidation_marks(USER, list(email_ids))
    return [found.get(e) for e in email_ids]


async def seed_card_and_line(world) -> None:
    await world.emails.save_batch([email("msg_rome01", OLD), email("e_pending", OLD + timedelta(hours=1))])
    await world.cards.write_card(USER, "msg_rome01", booking_fields(), None, CardExpect.absent())
    await world.cards.write_card(USER, "e_pending", pending_fields(reason="outage", failures=0, model=""), None,
                                 CardExpect.absent())


async def test_the_pass_sends_the_header_and_a_card_or_the_headers_per_mail_under_the_email_prompt(world):
    await seed_card_and_line(world)
    await world.people.replace(ACC, PEOPLE, "owner_script")
    prompts = RecordingPrompts()
    agent = make_agent(world, prompts=prompts)

    response = await run(agent, number_of_batches=1)

    assert response.result["batches_processed"] == 1
    assert prompts.built == ["consolidation_email"]
    assert agent.systems == ["SYSTEM PROMPT FOR consolidation_email"]
    (first,) = agent.firsts
    assert first.startswith(EMAIL_CANDIDATES_HEADER + "\n1. {")
    assert "[system_alert]" not in first and "Today:" in first
    card, header = numbered(first)
    assert card["email_id"] == "msg_rome01" and "fact" not in card and card["about"] == "owner"
    assert {"label": "booking_reference", "value": "RM-55120"} in card["printed_values"]
    assert card["date"] == "2025-06-02T10:30:00+01:00"                     # the sent moment in the owner's zone
    assert card["summary"] == "Hotel booking in Rome for 10–14 Aug 2025, 560 EUR."
    assert header == header_candidate(email("e_pending"), LISBON)            # unsettled past the wait: its headers
    assert header == {"email_id": "e_pending", "date": "2025-06-02T10:30:00+01:00",
                      "from": "sender@example.com", "subject": "Subject e_pending"}
    assert "Fact sentence" not in first                                    # a stored v1 fact is never sent


async def test_every_mail_of_the_batch_is_marked_the_line_form_ones_too(world):
    await seed_card_and_line(world)
    await world.emails.save_batch([email("young", datetime.now(timezone.utc).replace(tzinfo=None))])
    agent = make_agent(world)

    await run(agent, number_of_batches=1)

    first, second, young = await marks(world, "msg_rome01", "e_pending", "young")
    assert first is not None and second is not None
    assert young is None                                                   # still waiting for its card


async def test_the_default_card_batch_is_fifty(world):
    await world.emails.save_batch([email(f"e{i:02}", OLD + timedelta(minutes=i)) for i in range(51)])
    agent = make_agent(world)

    await run(agent, number_of_batches=1)

    marked = await marks(world, *[f"e{i:02}" for i in range(51)])
    assert sum(m is not None for m in marked) == 50 and marked[50] is None
    assert len(numbered(agent.firsts[0])) == 50


async def test_a_prompt_failure_reads_and_marks_nothing(world):
    await seed_card_and_line(world)
    agent = make_agent(world, prompts=RecordingPrompts(fail=True))

    with pytest.raises(KeyError):
        await run(agent, number_of_batches=1)

    assert agent.firsts == []
    assert await marks(world, "msg_rome01", "e_pending") == [None, None]


async def test_an_unreadable_people_list_withholds_title_and_summary_and_the_pass_completes(world):
    await seed_card_and_line(world)
    world.db.collection(PEOPLE_COL).docs[ACC] = {"user": {"name": ""}, "people": "not a list"}
    agent = make_agent(world)

    response = await run(agent, number_of_batches=1)

    card, _ = numbered(agent.firsts[0])
    assert "title" not in card and "summary" not in card and card["printed_values"]
    assert response.result["batches_processed"] == 1
    assert all(m is not None for m in await marks(world, "msg_rome01", "e_pending"))


async def test_without_a_people_repository_title_and_summary_are_kept(world):
    await seed_card_and_line(world)
    agent = make_agent(world, people=False)

    await run(agent, number_of_batches=1)

    card, _ = numbered(agent.firsts[0])
    assert card["title"] and card["summary"]


async def test_with_nothing_waiting_no_model_is_called_and_nothing_is_refreshed(world):
    prompts = RecordingPrompts()
    agent = make_agent(world, prompts=prompts)

    response = await run(agent, number_of_batches=2)

    assert response.result == {"batches_processed": 0, "facts_affected": 0}
    assert agent.firsts == [] and prompts.invalidated == []


class MarkFails(FirestoreIndexedEmailRepository):
    async def mark_consolidated(self, user_id, email_ids, consolidated_at):
        raise RuntimeError("mark refused")


async def test_a_pass_that_wrote_and_then_failed_still_refreshes_the_cache(world):
    await world.emails.save_batch([email("e1", OLD)])
    emails = MarkFails(world.db, SimpleNamespace(domain_email_facts_collection=FACTS,
                                                 email_indexing_state_collection=STATE))
    created = LLMResponse(text=None, tool_calls=[ToolCall(name="create_fact", args={
        "content": "Alex Doe holds a hotel booking.", "fact_attributes": {
            "domain": "biographical", "temporal_class": "stable", "context_priority": "medium", "tags": ["t"],
            "metadata": {}, "type": "event", "expiry": {"kind": "none"}}})])
    prompts = RecordingPrompts()
    agent = make_agent(world, prompts=prompts, responses=[created], emails=emails)

    with pytest.raises(RuntimeError, match="mark refused"):
        await run(agent, number_of_batches=1)

    assert [d["text"] for d in agent.store.docs.values()] == ["Alex Doe holds a hotel booking."]
    assert prompts.invalidated == [ACC]
    assert await marks(world, "e1") == [None]
