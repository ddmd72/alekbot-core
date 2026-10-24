"""Facts S3 · T4b: the consolidator builds the real write context of each pass (owner's zone, span, label).

A scripted LLM drives the real tool executor, adapter, write service and repository on the evaluating Firestore fake, so
every assertion reads a stored document. The email pass reads its candidates through `FirestoreIndexedEmailRepository`
on the same fake: its dates come back naive, as they do in production.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.email import IndexedEmail
from src.domain.entities import FactDomain
from src.domain.fact_write_context import as_utc
from src.domain.llm import LLMResponse, ToolCall
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on

REPORT = LLMResponse(text='{"operations": []}')
PARIS = "Europe/Paris"
STAGE1 = "Begin deliberate consolidation."
CLUSTER = "FACT CLUSTER REVIEW"
RULEBOOK = "STANDING DIRECTIVES REVIEW"


def utc(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


OMIT = object()


def attrs(**over: Any) -> Dict[str, Any]:
    """Fact attributes as the model sends them; a key given as OMIT is not sent at all (absent is not null)."""
    base = {"domain": "biographical", "temporal_class": "stable", "context_priority": "medium", "tags": ["t"],
            "metadata": {}, "type": "event", "expiry": EXPIRY_NONE}
    base.update(over)
    return {k: v for k, v in base.items() if v is not OMIT}


def create(content: str, **over: Any) -> ToolCall:
    return ToolCall(name="create_fact", args={"content": content, "fact_attributes": attrs(**over)})


def calls(*tool_calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=list(tool_calls))


def by_text(store: FactStore, text: str) -> Dict[str, Any]:
    (doc,) = [d for d in store.docs.values() if d["text"] == text]
    return doc


def at(value: Any) -> datetime:
    return as_utc(value)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def make_agent(store: FactStore, script: Callable[[str, str], LLMResponse], *, email_repo: Optional[Any] = None):
    """An agent whose LLM answers `script(first_message_text, pass_marker)`; every request is kept in `agent.requests`."""
    port = FactManagementAdapter(
        store.repo, store.embedder, store.write, SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20)
    )
    pb = MagicMock()
    pb.build_for_agent = AsyncMock(return_value="SYSTEM")
    pb.invalidate_biographical_cache = MagicMock()
    execution = AgentExecutionContext(
        agent_type="consolidation", provider=MagicMock(spec=LLMPort), model_name="gemini-pro",
        tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )
    agent = ConsolidationAgent(
        config=AgentConfig(agent_id="consolidation_agent", agent_type="consolidation"), execution_context=execution,
        repository=store.repo, embedding_service=store.embedder, fact_write_service=MagicMock(),
        fact_management_port=port, fact_updates=port,  # reviewer edit (S4 T6b): the adapter is both ports
        prompt_builder=pb, indexed_email_repo=email_repo,
    )
    agent.INLINE_CLUSTER_REVIEW = False
    agent.requests = []

    async def call(request, turn=1):
        first = request.messages[0].parts[0].text
        agent.requests.append(first)
        return script(first, str(len(request.messages)))

    agent._call_llm = call
    return agent


def once(per_pass: Dict[str, LLMResponse]) -> Callable[[str, str], LLMResponse]:
    """The pass (by its marker in the first message) answers its tool calls on the first turn, then reports."""
    spent: set = set()

    def script(first: str, turns: str) -> LLMResponse:
        for marker, response in per_pass.items():
            if marker in first and marker not in spent:
                spent.add(marker)
                return response
        return REPORT

    return script


# ----------------------------------------------------------------------------------------------- Stage 1


MESSAGES = [
    {"role": "user", "text": "m1", "created_at": utc(20, 8).timestamp()},
    {"role": "model", "text": "m2", "created_at": utc(20, 9).isoformat()},
    {"role": "user", "text": "m3", "created_at": "2026-10-20T11:00:00Z"},
    {"role": "user", "text": "m4", "created_at": "last Tuesday"},
    {"role": "user", "text": "m5"},
]


async def test_stage1_create_gets_the_span_end_and_expires_in_the_owner_zone(store):
    agent = make_agent(store, once({STAGE1: calls(
        create("trip to Rome", expiry=on("2027-01-15")),
        create("takes the pills", expiry=horizon(30)),
    )}))
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(MESSAGES, USER, ACC, [], owner_timezone=PARIS)

    trip, pills = by_text(store, "trip to Rome"), by_text(store, "takes the pills")
    assert at(trip["reported_date"]) == utc(20, 11) == at(pills["reported_date"])      # the last readable time
    assert at(trip["expires_at"]) == datetime(2027, 1, 15, 23, tzinfo=timezone.utc)    # the end of 15 Jan, Paris
    assert at(pills["expires_at"]) == utc(20, 11) + timedelta(days=30)                  # a horizon counts from the span end


async def test_stage1_without_an_owner_zone_uses_utc(store):
    agent = make_agent(store, once({STAGE1: calls(create("trip to Rome", expiry=on("2027-01-15")))}))
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(MESSAGES, USER, ACC, [])
    assert at(by_text(store, "trip to Rome")["expires_at"]) == datetime(2027, 1, 16, tzinfo=timezone.utc)


async def test_stage1_clamps_a_model_date_into_the_batch_and_keeps_one_inside_it(store):
    agent = make_agent(store, once({STAGE1: calls(
        create("inside", reported_date=utc(20, 9, 30).isoformat()),
        create("too late", reported_date=utc(22, 9).isoformat()),
        create("too early", reported_date=utc(1, 9).isoformat()),
    )}))
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(MESSAGES, USER, ACC, [], owner_timezone=PARIS)
    assert [at(by_text(store, t)["reported_date"]) for t in ("inside", "too late", "too early")] == [
        utc(20, 9, 30), utc(20, 11), utc(20, 8)]


async def test_a_batch_without_readable_times_has_no_span_and_the_write_is_dated_now(store):
    agent = make_agent(store, once({STAGE1: calls(create("no clock"))}))
    before = datetime.now(timezone.utc)
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3([{"role": "user", "text": "x", "created_at": "soon"}], USER, ACC, [])
    assert before <= at(by_text(store, "no clock")["reported_date"]) <= datetime.now(timezone.utc)


async def test_the_zone_reaches_the_pass_from_the_messages_context(store):
    agent = make_agent(store, once({STAGE1: calls(create("trip to Rome", expiry=on("2027-01-15")))}))
    store.repo.refresh_biographical_context_cache = AsyncMock()
    message = AgentMessage(
        task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": MESSAGES}, context={"user_id": USER, "owner_timezone": PARIS},
    )
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent._handle_consolidate(message, USER, ACC, [], 0.0)
    assert response.status.value == "success", response
    assert at(by_text(store, "trip to Rome")["expires_at"]) == datetime(2027, 1, 15, 23, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------------------------- cleanup passes


def trail_actors(doc: Dict[str, Any]) -> List[str]:
    return [e["actor"] for e in doc["lifecycle_events"]]


async def test_stage2a_ignores_a_model_date_and_honours_derived_from(store):
    state: Dict[str, str] = {}

    def script(first: str, turns: str) -> LLMResponse:
        if STAGE1 in first and "s1" not in state:
            state["s1"] = "done"
            return calls(create("one big fact about Rome and pills", reported_date=utc(20, 9).isoformat()))
        if CLUSTER in first and "2a" not in state:
            state["2a"] = "done"
            source_id = by_text(store, "one big fact about Rome and pills")["id"]
            return calls(
                create("Rome part", derived_from=[source_id], reported_date=utc(20, 10).isoformat()),
                create("pills part", reported_date=utc(20, 10).isoformat()),
            )
        return REPORT

    agent = make_agent(store, script)
    agent.INLINE_CLUSTER_REVIEW = True
    with RequestContext(user_id=USER, account_id=ACC):
        await agent._execute_deliberate_process_v3(MESSAGES, USER, ACC, [], owner_timezone=PARIS)

    assert at(by_text(store, "one big fact about Rome and pills")["reported_date"]) == utc(20, 9)   # Stage 1 keeps the model's date
    assert at(by_text(store, "Rome part")["reported_date"]) == utc(20, 9)       # derived_from wins over the model's date
    assert at(by_text(store, "pills part")["reported_date"]) == utc(20, 11)     # no source: the end of the batch, not the model's date


async def test_every_pass_writes_under_its_own_actor_and_the_backstop_says_why(store):
    cap = ConsolidationAgent.DIRECTIVE_HARD_CAP
    for i in range(cap + 3):                                    # two are archived by the passes, one is left over the cap
        put_raw(store, f"d-{i:02d}", f"rule {i:02d}", entity={"domain": FactDomain.AGENT_DIRECTIVE})
    put_raw(store, "plain", "plain fact")
    done: set = set()

    def archive(fact_id: str) -> ToolCall:
        return ToolCall(name="update_fact", args={"fact_id": fact_id, "updates": {"state": "archived"}})

    def script(first: str, turns: str) -> LLMResponse:
        for marker, response in (
            (STAGE1, calls(create("seed fact"), archive("plain"))),
            (CLUSTER, calls(archive("d-00"))),
            (RULEBOOK, calls(archive("d-01"))),
        ):
            if marker in first and marker not in done:
                done.add(marker)
                return response
        return REPORT

    agent = make_agent(store, script)
    agent.INLINE_CLUSTER_REVIEW = True
    with RequestContext(user_id=USER, account_id=ACC):
        result = await agent._execute_deliberate_process_v3(MESSAGES, USER, ACC, [], owner_timezone=PARIS)

    last = {fid: d["lifecycle_events"][-1] for fid, d in store.docs.items() if d["lifecycle_events"]}
    assert (last["plain"]["actor"], last["d-00"]["actor"], last["d-01"]["actor"]) == ("stage1", "stage2a", "stage2b")
    (capped,) = [fid for fid, entry in last.items() if entry["actor"] == "backstop"]
    assert last[capped]["reason"] == "directive cap" and store.docs[capped]["state"] == "invalidated"
    assert [op["fact_id"] for op in result["operations"] if op.get("reason") == "directive cap backstop"] == [capped]


async def test_the_cluster_pass_builds_its_own_context_without_a_span(store):
    agent = make_agent(store, once({CLUSTER: calls(
        create("merged view", reported_date=utc(20, 9).isoformat(), expiry=on("2027-01-15")),
    )}))
    message = AgentMessage(
        task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate_cluster", "cluster": [{"fact_id": "x", "content": "c", "similarity": 0.5}]},
        context={"user_id": USER, "owner_timezone": PARIS},
    )
    store.repo.refresh_biographical_context_cache = AsyncMock()
    before = datetime.now(timezone.utc)
    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent._handle_consolidate_cluster(message, USER, ACC, [], 0.0)
    assert response.status.value == "success", response
    doc = by_text(store, "merged view")
    assert before <= at(doc["reported_date"]) <= datetime.now(timezone.utc)             # cleanup pass, no span: now, the model's date ignored
    assert at(doc["expires_at"]) == datetime(2027, 1, 15, 23, tzinfo=timezone.utc)      # the owner's zone reached the pass


# ----------------------------------------------------------------------------------------------- email pass


def email(email_id: str, when: datetime, indexed: datetime) -> IndexedEmail:
    return IndexedEmail(
        email_id=email_id, user_id=USER, account_id=ACC, source="gmail", text=f"mail {email_id}", tags=["mail"],
        category="travel", metadata={"subject": email_id}, subject=email_id, from_address="a@example.com",
        email_date=when, indexed_at=indexed,
    )


async def test_an_email_pass_with_naive_candidate_dates_writes_its_facts_and_marks_the_emails_consolidated():
    store = FactStore()      # no index refusal: the email repository's own indexes are not this task's subject
    env = SimpleNamespace(domain_email_facts_collection="eval_email_facts", email_indexing_state_collection="eval_email_state")
    emails = FirestoreIndexedEmailRepository(store.db, env)
    first, last = datetime(2026, 10, 10, 9, 0), datetime(2026, 10, 12, 15, 0)             # naive, as the repository returns them
    await emails.save_batch([email("e1", first, datetime(2026, 10, 13, 1)), email("e2", last, datetime(2026, 10, 13, 2))])
    assert all(e.email_date.tzinfo is None for e in await emails.get_unconsolidated_batch(USER))

    agent = make_agent(store, once({"system_alert": calls(
        create("flight booked", reported_date="2026-10-11T00:00:00+00:00", expiry=on("2027-01-15")),
        create("no date sent"),
        create("date from the future", reported_date="2026-10-30T00:00:00+00:00"),
    )}), email_repo=emails)
    message = AgentMessage(
        task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate_email", "number_of_batches": 1, "batch_size": 10},
        context={"user_id": USER, "owner_timezone": PARIS},
    )
    store.repo.refresh_biographical_context_cache = AsyncMock()
    response = await agent._handle_consolidate_email(message, USER, ACC, [], 0.0)

    assert response.status.value == "success" and response.result["batches_processed"] == 1
    assert at(by_text(store, "flight booked")["reported_date"]) == utc(11, 0)             # inside the span: kept
    assert at(by_text(store, "no date sent")["reported_date"]) == utc(12, 15)             # span end, read as UTC
    assert at(by_text(store, "date from the future")["reported_date"]) == utc(12, 15)     # clamped into the span
    assert at(by_text(store, "flight booked")["expires_at"]) == datetime(2027, 1, 15, 23, tzinfo=timezone.utc)
    assert await emails.get_unconsolidated_batch(USER) == []                              # marked consolidated


# ----------------------------------------------------------------------------------------------- top-level expiry


def tool_result(responses, index=0) -> Dict[str, Any]:
    return json.loads(responses[index].result_str)


async def test_a_top_level_expiry_is_moved_into_place_on_create_update_and_merge(store):
    agent = make_agent(store, lambda first, turns: REPORT)
    run = lambda *tcs: agent._execute_fact_management_tools(list(tcs), USER, ACC, write_context=ctx())     # noqa: E731

    created = await run(
        ToolCall(name="create_fact", args={"content": "walks daily", "fact_attributes": attrs(expiry=OMIT), "expiry": horizon(30)}),
        ToolCall(name="create_fact", args={"content": "nested wins", "fact_attributes": attrs(expiry=horizon(5)), "expiry": EXPIRY_NONE}),
    )
    assert [tool_result(created, i)["status"] for i in (0, 1)] == ["created", "created"], [r.result_str for r in created]
    assert (by_text(store, "walks daily")["expiry_kind"], by_text(store, "walks daily")["ttl_days"]) == ("horizon", 30)
    assert (by_text(store, "nested wins")["expiry_kind"], by_text(store, "nested wins")["ttl_days"]) == ("horizon", 5)

    walks = by_text(store, "walks daily")["id"]
    updated = await run(ToolCall(name="update_fact", args={
        "fact_id": walks, "updates": {"content": "walks twice daily"}, "expiry": horizon(10)}))
    assert tool_result(updated)["status"] == "updated", updated[0].result_str
    assert (by_text(store, "walks twice daily")["expiry_kind"], by_text(store, "walks twice daily")["ttl_days"]) == ("horizon", 10)

    other = (await run(ToolCall(name="create_fact", args={"content": "walks far", "fact_attributes": attrs()})))
    assert tool_result(other)["status"] == "created"
    merged = await run(ToolCall(name="merge_facts", args={
        "fact_ids": [by_text(store, "walks twice daily")["id"], by_text(store, "walks far")["id"]],
        "merged_content": "walks a lot", "fact_attributes": attrs(expiry=OMIT), "expiry": horizon(7)}))
    assert tool_result(merged)["status"] == "merged", merged[0].result_str
    assert (by_text(store, "walks a lot")["expiry_kind"], by_text(store, "walks a lot")["ttl_days"]) == ("horizon", 7)


async def test_a_missing_expiry_error_says_where_it_belongs_and_nothing_is_written(store):
    agent = make_agent(store, lambda first, turns: REPORT)
    seeded = await agent._execute_fact_management_tools(
        [ToolCall(name="create_fact", args={"content": "seed", "fact_attributes": attrs()})], USER, ACC, write_context=ctx())
    seed_id = tool_result(seeded)["fact_id"]
    before = dict(store.docs)

    responses = await agent._execute_fact_management_tools(
        [
            ToolCall(name="create_fact", args={"content": "no expiry", "fact_attributes": attrs(expiry=OMIT)}),
            ToolCall(name="update_fact", args={"fact_id": seed_id, "updates": {"content": "seed changed"}}),
        ],
        USER, ACC, write_context=ctx())
    create_error, update_error = tool_result(responses, 0), tool_result(responses, 1)
    assert create_error["refused"] == "expiry" and "inside `fact_attributes`" in create_error["message"]
    assert update_error["refused"] == "expiry" and "inside `updates`" in update_error["message"]
    assert store.docs == before


# ----------------------------------------------------------------------------------------------- span and zone helpers


def test_the_span_leaves_out_an_unreadable_time_instead_of_failing():
    span = ConsolidationAgent._times_span([utc(20, 8).timestamp(), "not a time", "2026-10-20T11:00:00Z", None, {}], "message times")
    assert span == (utc(20, 8), utc(20, 11))


def test_the_span_of_nothing_readable_is_none():
    assert ConsolidationAgent._times_span(["soon", "later"], "message times") is None
    assert ConsolidationAgent._times_span([], "message times") is None


def test_a_naive_time_in_the_span_is_utc_whatever_the_host_zone():
    assert ConsolidationAgent._times_span([datetime(2026, 10, 10, 9), "2026-10-12T15:00:00"], "email dates") == (
        utc(10, 9), utc(12, 15))


@pytest.mark.parametrize("context,expected", [
    ({}, "UTC"), ({"owner_timezone": ""}, "UTC"), ({"owner_timezone": PARIS}, PARIS),
    ({"owner_timezone": "Mars/Olympus"}, "UTC"), ({"owner_timezone": 5}, "UTC"),
])
def test_the_owner_zone_is_read_from_the_message_and_defaults_to_utc(context, expected):
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE, payload={}, context=context)
    assert ConsolidationAgent._owner_timezone(message) == expected
