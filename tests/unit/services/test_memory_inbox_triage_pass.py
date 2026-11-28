"""MemoryInboxTriagePass (MCP_MEMORY_SUBMISSIONS_RFC §7, §10.2) over the real inbox store on the evaluating fake
(`tests/support/memory_inbox_store.py`). The pass's scope is the real `MemoryInboxTriageScope` (facts on the evaluating
fake); the coordinator is a stand-in that reads the task it is routed and decides through the scope in the payload, as
the agent's tools do, then answers as the agent does (`encode_triage_result`) or dies. Assertions read what the store
holds and what each pass was sent."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from src.domain.agent import AgentMessage, AgentResponse
from src.domain.consolidation import TRIAGE_AGENT_TASK, consolidation_agent_id
from src.domain.memory_inbox import Basis, SubmissionDraft, SubmittingClient
from src.domain.memory_inbox_runs import PassRequest
from src.domain.memory_inbox_triage_task import (
    TRIAGE_SCOPE_KEY,
    TriageTask,
    decode_triage_task,
    encode_triage_result,
)
from src.domain.request_context import get_effective_account_id
from src.infrastructure.memory_inbox_triage_scope import MemoryInboxTriageScope
from src.services.memory_inbox_triage_pass import MemoryInboxTriagePass
from tests.support.fact_store import FactStore
from tests.support.memory_inbox_store import inbox_store

UTC = timezone.utc
AT = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
CLIENT = SubmittingClient(client_id="c1", client_name="Claude")
OTHER = SubmittingClient(client_id="cli-x", client_name="Other")  # one client's day holds half the user's day
GYM = "Alex plays tennis on Sundays"


def record_args(*ids: str) -> Dict[str, Any]:
    return dict(submission_ids=list(ids), text=GYM, happens_when="every Sunday", said_when="", basis="said",
                directive=False, temporal_class="dynamic", context_priority="medium",
                expiry={"kind": "horizon", "days": 90}, relation="new")


async def record(scope: MemoryInboxTriageScope, *ids: str) -> None:
    assert (await scope.record_candidate(record_args(*ids)))["status"] == "recorded"


async def discard(scope: MemoryInboxTriageScope, ids: List[str], reason: str = "trivial") -> None:
    assert (await scope.discard_submissions({"submission_ids": ids, "reason": reason}))["status"] == "discarded"


def answer(scope: MemoryInboxTriageScope, *, ended: bool = True, failed: bool = False) -> AgentResponse:
    return AgentResponse.success("t", "consolidation_agent",
                                 encode_triage_result(scope.decisions(), ended=ended, failed=failed))


async def discard_all(task: TriageTask, scope: MemoryInboxTriageScope) -> AgentResponse:
    await discard(scope, [s.submission_id for s in task.submissions])
    return answer(scope)


class Coordinator:
    """Routes each task to `decide(task, scope)` (or raises when it is an exception); records the tasks and the account
    the agent would read from the RequestContext."""

    def __init__(self, decide) -> None:
        self.decide = decide
        self.tasks: List[TriageTask] = []
        self.messages: List[AgentMessage] = []
        self.accounts: List[Optional[str]] = []

    async def route_message(self, message: AgentMessage) -> AgentResponse:
        self.messages.append(message)
        self.accounts.append(get_effective_account_id())
        task = decode_triage_task(message.payload)
        self.tasks.append(task)
        if isinstance(self.decide, BaseException):
            raise self.decide
        return await self.decide(task, message.payload[TRIAGE_SCOPE_KEY])


class World:
    def __init__(self, decide=discard_all) -> None:
        self.inbox = inbox_store()
        self.facts = FactStore()
        self.coordinator = Coordinator(decide)
        self.ensured: List[str] = []

        async def ensure(user_id: str) -> None:
            self.ensured.append(user_id)

        self.triage = MemoryInboxTriagePass(
            store=self.inbox.repo, coordinator=self.coordinator, ensure_agents=ensure, clock=lambda: AT,
            new_scope=lambda ids, account_id: MemoryInboxTriageScope(ids, account_id, self.facts.repo))

    async def receive(self, count: int, day: int = 0, client: SubmittingClient = CLIENT) -> List[str]:
        drafts = [SubmissionDraft(information=f"{GYM} ({n})", basis=Basis.SAID, said_when="", happens_when="")
                  for n in range(count)]
        await self.inbox.repo.add_submissions("u1", "a1", client, drafts, AT - timedelta(days=10 - day),
                                              f"2026-11-{15 + day}")
        return [s.submission_id for s in await self.inbox.repo.received("u1", 500)]

    async def run(self):
        return await self.triage.run(PassRequest(user_id="u1", account_id="a1", zone="Europe/Lisbon", language="uk",
                                                 now=AT, deadline=AT + timedelta(minutes=30)))


async def test_up_to_three_batches_of_forty_then_the_rest_waits():
    world = World()
    await world.receive(50)
    await world.receive(50, client=OTHER)
    await world.receive(30, day=1)
    outcome = await world.run()
    assert (outcome.status, outcome.passes, outcome.submissions, outcome.discarded, outcome.left) == \
           ("completed", 3, 120, 120, 10)
    assert [len(t.submissions) for t in world.coordinator.tasks] == [40, 40, 40]
    assert len(world.inbox.of_status("received")) == 10


async def test_the_task_is_routed_to_the_users_agent_inside_its_account():
    world = World()
    await world.receive(2)
    await world.run()
    (message,) = world.coordinator.messages
    assert (message.recipient, message.payload["task"], world.coordinator.accounts) == \
           (consolidation_agent_id("u1"), TRIAGE_AGENT_TASK, ["a1"])
    assert message.context["owner_timezone"] == "Europe/Lisbon" and message.context["suppress_transient_retry"]
    assert message.timeout_ms == 29 * 60 * 1000 and world.ensured == ["u1"]
    assert world.coordinator.tasks[0].language == "uk"


async def test_decisions_are_applied_and_counted():
    async def decide(task: TriageTask, scope: MemoryInboxTriageScope) -> AgentResponse:
        s1, s2, s3 = (s.submission_id for s in task.submissions)
        await record(scope, s1, s2)
        await discard(scope, [s3], "known")
        return answer(scope)

    world = World(decide)
    await world.receive(3)
    outcome = await world.run()
    assert (outcome.status, outcome.candidates, outcome.discarded, outcome.left, outcome.sources) == \
           ("completed", 1, 1, 0, ("Claude",))
    (made,) = world.inbox.of_status("candidate").values()
    assert (made["text"], made["language"], len(made["source_ids"])) == (GYM, "uk", 2)
    assert (len(world.inbox.of_status("triaged")), len(world.inbox.of_status("discarded"))) == (2, 1)


async def test_the_waiting_and_answered_candidates_are_shown_to_the_next_pass():
    async def decide(task: TriageTask, scope: MemoryInboxTriageScope) -> AgentResponse:
        await record(scope, task.submissions[0].submission_id)
        return answer(scope)

    world = World(decide)
    await world.receive(1)
    await world.run()
    await world.receive(1, day=1)
    await world.run()
    assert [c.text for c in world.coordinator.tasks[1].waiting] == [GYM]
    assert world.coordinator.tasks[1].answered == ()


async def _fails(task, scope):
    return AgentResponse.failure("t", "consolidation_agent", "model down")


async def _times_out(task, scope):
    return AgentResponse.timeout("t", "consolidation_agent", "Task execution timeout")


async def _unreadable(task, scope):
    return AgentResponse.success("t", "consolidation_agent", {"ended": 1})


@pytest.mark.parametrize("decide", [RuntimeError("coordinator down"), _fails, _times_out, _unreadable])
async def test_a_dying_pass_still_counts_every_sent_offer(decide):
    world = World(decide)
    ids = await world.receive(45)
    first = await world.run()
    assert (first.status, first.passes, first.submissions, first.candidates, first.left) == ("failed", 1, 40, 0, 45)
    assert [world.inbox.docs[i]["triage_offers"] for i in ids] == [1] * 40 + [0] * 5
    second = await world.run()
    assert (second.status, second.discarded) == ("failed", 40)
    undecided = world.inbox.of_status("discarded")
    assert set(undecided) == set(ids[:40]) and {d["discard_reason"] for d in undecided.values()} == {"undecided"}


async def test_a_turn_cap_keeps_the_decisions_made_before_it():
    async def decide(task: TriageTask, scope: MemoryInboxTriageScope) -> AgentResponse:
        await discard(scope, [task.submissions[0].submission_id], "known")
        return answer(scope, ended=False)

    world = World(decide)
    ids = await world.receive(3)
    outcome = await world.run()
    assert (outcome.status, outcome.passes, outcome.discarded, outcome.left) == ("failed", 1, 1, 2)
    assert world.inbox.docs[ids[0]]["discard_reason"] == "known"
    assert [world.inbox.docs[i]["triage_offers"] for i in ids[1:]] == [1, 1]


async def test_a_raising_loop_keeps_its_decisions_and_ends_the_run_failed():
    async def decide(task: TriageTask, scope: MemoryInboxTriageScope) -> AgentResponse:
        await record(scope, task.submissions[0].submission_id)
        return answer(scope, ended=False, failed=True)

    world = World(decide)
    await world.receive(2)
    outcome = await world.run()
    assert (outcome.status, outcome.candidates, outcome.left) == ("failed", 1, 1)


async def test_an_undecided_submission_is_not_sent_twice_in_one_run():
    async def decide(task: TriageTask, scope: MemoryInboxTriageScope) -> AgentResponse:
        if len(task.submissions) > 1:
            await discard(scope, [s.submission_id for s in task.submissions[1:]])
        return answer(scope)

    world = World(decide)
    ids = await world.receive(41)
    outcome = await world.run()
    sent = [[s.submission_id for s in t.submissions] for t in world.coordinator.tasks]
    assert sent == [ids[:40], ids[40:]]
    assert (outcome.passes, outcome.left) == (2, 2)
    assert world.inbox.docs[ids[0]]["triage_offers"] == 1


async def test_nothing_received_makes_no_agent_call():
    world = World()
    outcome = await world.run()
    assert (outcome.status, outcome.passes, outcome.submissions, outcome.left) == ("empty", 0, 0, 0)
    assert world.coordinator.messages == [] and world.ensured == []


async def test_a_lease_too_short_for_a_pass_routes_nothing():
    world = World()
    await world.receive(2)
    outcome = await world.triage.run(PassRequest(user_id="u1", account_id="a1", zone="UTC", language="en", now=AT,
                                                 deadline=AT + timedelta(seconds=90)))
    assert (outcome.status, outcome.passes, outcome.left) == ("failed", 0, 2)
    assert world.coordinator.messages == []


async def _records_then_times_out(task, scope):
    await record(scope, task.submissions[0].submission_id)
    return AgentResponse.timeout("t", "consolidation_agent", "Task execution timeout")


async def _records_then_raises(task, scope):
    await record(scope, task.submissions[0].submission_id)
    raise RuntimeError("the agent's execution raised")


@pytest.mark.parametrize("decide", [_records_then_times_out, _records_then_raises])
async def test_a_pass_whose_agent_dies_after_a_candidate_keeps_it_and_offers_the_rest(decide):
    world = World(decide)
    ids = await world.receive(3)
    outcome = await world.run()
    assert (outcome.status, outcome.candidates, outcome.left, outcome.sources) == ("failed", 1, 2, ("Claude",))
    (made,) = world.inbox.of_status("candidate").values()
    assert made["source_ids"] == [ids[0]] and world.inbox.docs[ids[0]]["status"] == "triaged"
    assert [world.inbox.docs[i]["triage_offers"] for i in ids[1:]] == [1, 1]


async def test_a_cancelled_run_still_writes_the_decisions_its_scope_holds():
    async def decide(task, scope):
        await record(scope, task.submissions[0].submission_id)
        raise asyncio.CancelledError()

    world = World(decide)
    ids = await world.receive(2)
    with pytest.raises(asyncio.CancelledError):
        await world.run()
    assert world.inbox.docs[ids[0]]["status"] == "triaged" and world.inbox.docs[ids[1]]["triage_offers"] == 1
    assert len(world.inbox.of_status("candidate")) == 1
