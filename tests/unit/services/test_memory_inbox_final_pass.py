"""MemoryInboxFinalPass (MCP_MEMORY_SUBMISSIONS_RFC §9.1, §9.4, §10.3) over the real inbox store on the evaluating fake
(`tests/support/memory_inbox_store.py`); the candidates reach `confirmed` through the store's own triage and confirm.
The coordinator is a stand-in that reads the task it is routed and answers as the agent does (`encode_final_result`) or
dies. Assertions read what the store holds, what each batch was sent and what the alert sink got."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

import pytest

from src.domain.agent import AgentMessage, AgentResponse
from src.domain.consolidation import FINAL_AGENT_TASK, consolidation_agent_id
from src.domain.memory_inbox import (
    Basis,
    Candidate,
    CandidateDraft,
    CandidateFields,
    DraftClass,
    Relation,
    RelationKind,
    SubmissionDraft,
    SubmittingClient,
    TriageDecisions,
)
from src.domain.memory_inbox_final_task import decode_final_task, encode_final_result
from src.domain.memory_inbox_runs import PassRequest
from src.domain.request_context import get_effective_account_id
from src.ports.alert_sink import AlertSinkPort
from src.services.memory_inbox_final_pass import MemoryInboxFinalPass
from tests.support.memory_inbox_store import inbox_store

UTC = timezone.utc
AT = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
CLIENT = SubmittingClient(client_id="c1", client_name="Claude")
GYM = "Alex plays tennis on Sundays"
DRAFT = DraftClass(directive=False, temporal_class="dynamic", context_priority="medium", expiry={"kind": "none"})
NEW = Relation(kind=RelationKind.NEW, fact_id=None, fact_text=None)

Decide = Callable[[Tuple[Candidate, ...]], Awaitable[AgentResponse]]


def answer(*, ended: bool = True, wrote: int = 1) -> AgentResponse:
    return AgentResponse.success("t", "consolidation_agent", encode_final_result(ended=ended, wrote=wrote))


async def ends(batch: Tuple[Candidate, ...]) -> AgentResponse:
    return answer(wrote=len(batch))


async def turn_cap(batch: Tuple[Candidate, ...]) -> AgentResponse:
    return answer(ended=False, wrote=0)


class Coordinator:
    """Routes each task to `decide(candidates)` (or raises when it is an exception); records the batches and the account
    the agent would read from the RequestContext."""

    def __init__(self, decide: Any) -> None:
        self.decide = decide
        self.batches: List[Tuple[Candidate, ...]] = []
        self.messages: List[AgentMessage] = []
        self.accounts: List[Optional[str]] = []

    async def route_message(self, message: AgentMessage) -> AgentResponse:
        self.messages.append(message)
        self.accounts.append(get_effective_account_id())
        batch = decode_final_task(message.payload)
        self.batches.append(batch)
        if isinstance(self.decide, BaseException):
            raise self.decide
        response: AgentResponse = await self.decide(batch)
        return response


class Sink(AlertSinkPort):
    def __init__(self) -> None:
        self.texts: List[str] = []

    async def post(self, text: str) -> None:
        self.texts.append(text)


class World:
    def __init__(self, decide: Any = ends) -> None:
        self.inbox = inbox_store()
        self.coordinator = Coordinator(decide)
        self.sink = Sink()
        self.now = AT
        self.ensured: List[str] = []

        async def ensure(user_id: str) -> None:
            self.ensured.append(user_id)

        self.final = MemoryInboxFinalPass(store=self.inbox.repo, coordinator=self.coordinator, ensure_agents=ensure,
                                          alert_sink=self.sink, clock=lambda: self.now)

    async def waiting(self, count: int, start: int = 0) -> List[str]:
        """`count` waiting candidates of u1, one per submission, received one minute apart; their ids, oldest first."""
        ids: List[str] = []
        for n in range(start, start + count):
            await self.inbox.repo.add_submissions("u1", "a1", CLIENT, [SubmissionDraft(
                information=f"{GYM} ({n})", basis=Basis.SAID, said_when="", happens_when="")],
                AT - timedelta(days=2) + timedelta(minutes=n), "2026-11-23")
            (sub,) = await self.inbox.repo.received("u1", 1)
            before = set(self.inbox.docs)
            fields = CandidateFields(text=f"{GYM} ({n})", happens_when="every Sunday", said_when="", basis=Basis.SAID)
            await self.inbox.repo.apply_triage("u1", "a1", [sub.submission_id], TriageDecisions(
                candidates=(CandidateDraft((sub.submission_id,), fields, DRAFT, NEW),), discards={}), "en",
                AT - timedelta(days=1))
            ids += [i for i in self.inbox.docs if i not in before]
        return ids

    async def confirmed(self, count: int, start: int = 0) -> List[str]:
        ids = await self.waiting(count, start)
        for cid in ids:
            await self.inbox.repo.confirm(cid, "u1", None, AT - timedelta(hours=2))
        return ids

    async def run(self, deadline: timedelta = timedelta(minutes=30)):
        return await self.final.run(PassRequest(user_id="u1", account_id="a1", zone="Europe/Lisbon", language="",
                                                now=self.now, deadline=AT + deadline))

    def status(self, ids: Sequence[str]) -> List[Tuple[str, Optional[str], int]]:
        return [(self.inbox.docs[i]["status"], self.inbox.docs[i].get("outcome"),
                 self.inbox.docs[i].get("final_attempts", 0)) for i in ids]


async def test_an_ended_batch_marks_every_sent_candidate_consolidated():
    world = World()
    ids = await world.confirmed(3)
    outcome = await world.run()
    assert (outcome.status, outcome.candidates, outcome.wrote, outcome.gave_up) == ("completed", 3, 3, 0)
    assert world.status(ids) == [("consolidated", "done", 0)] * 3
    assert all(world.inbox.docs[i]["purge_after"] == AT + timedelta(days=180) for i in ids)
    assert world.sink.texts == []


async def test_the_task_is_routed_to_the_users_agent_inside_its_account_oldest_receipt_first():
    world = World()
    ids = await world.confirmed(3)
    await world.run()
    (message,) = world.coordinator.messages
    assert (message.recipient, message.payload["task"], world.coordinator.accounts) == \
           (consolidation_agent_id("u1"), FINAL_AGENT_TASK, ["a1"])
    assert message.context["owner_timezone"] == "Europe/Lisbon" and message.timeout_ms == (30 * 60 - 60) * 1000
    assert [c.candidate_id for c in world.coordinator.batches[0]] == ids
    assert world.ensured == ["u1"]


async def test_nothing_confirmed_is_empty_and_routes_nothing():
    world = World()
    waiting = await world.waiting(2)
    outcome = await world.run()
    assert (outcome.status, outcome.candidates) == ("empty", 0)
    assert world.coordinator.messages == [] and world.ensured == []
    assert world.status(waiting) == [("candidate", None, 0)] * 2


@pytest.mark.parametrize("decide", [turn_cap, RuntimeError("route down")])
async def test_a_turn_cap_keeps_them_confirmed_and_counts_an_attempt(decide):
    world = World(decide)
    ids = await world.confirmed(2)
    outcome = await world.run()
    assert (outcome.status, outcome.candidates, outcome.wrote, outcome.gave_up) == ("failed", 2, 0, 0)
    assert world.status(ids) == [("confirmed", None, 1)] * 2


async def test_the_third_failure_gives_up_with_one_alert():
    world = World(turn_cap)
    ids = await world.confirmed(2)
    for _ in range(2):
        await world.run()
    assert world.sink.texts == []
    outcome = await world.run()
    assert (outcome.status, outcome.gave_up) == ("failed", 2)
    assert world.status(ids) == [("consolidated", "gave_up", 3)] * 2
    (alert,) = world.sink.texts
    assert alert.startswith("🚨") and all(i in alert for i in ids) and GYM not in alert
    assert await world.run() == type(outcome)(status="empty", candidates=0, wrote=0, gave_up=0)


async def test_a_candidate_confirmed_after_the_read_is_not_marked():            # Review Focus 3
    world = World()
    sent = await world.confirmed(2)
    (late,) = await world.waiting(1, start=2)

    async def confirm_late_then_end(batch: Tuple[Candidate, ...]) -> AgentResponse:
        await world.inbox.repo.confirm(late, "u1", None, AT)
        return answer()

    world.coordinator.decide = confirm_late_then_end
    await world.run()
    assert [c.candidate_id for c in world.coordinator.batches[0]] == sent
    assert world.status(sent) == [("consolidated", "done", 0)] * 2
    assert world.status([late]) == [("confirmed", None, 0)]


async def test_forty_five_confirmed_go_in_batches_of_twenty():
    world = World()
    ids = await world.confirmed(45)
    outcome = await world.run()
    assert [len(b) for b in world.coordinator.batches] == [20, 20, 5]
    assert [c.candidate_id for b in world.coordinator.batches for c in b] == ids
    assert (outcome.status, outcome.candidates, outcome.wrote) == ("completed", 45, 45)
    assert world.ensured == ["u1"]


async def test_an_exhausted_lease_stops_between_batches():
    world = World()
    ids = await world.confirmed(25)

    async def ends_late(batch: Tuple[Candidate, ...]) -> AgentResponse:
        world.now = AT + timedelta(minutes=29, seconds=30)           # less than a minute of the lease left
        return answer(wrote=len(batch))

    world.coordinator.decide = ends_late
    outcome = await world.run()
    assert (outcome.status, outcome.candidates, outcome.wrote) == ("partial", 20, 20)
    assert world.status(ids[:20]) == [("consolidated", "done", 0)] * 20
    assert world.status(ids[20:]) == [("confirmed", None, 0)] * 5


async def test_a_failed_batch_after_an_ended_one_is_partial_and_stops():
    world = World()
    ids = await world.confirmed(45)
    answers = [answer(wrote=4), answer(ended=False, wrote=2)]

    async def second_fails(batch: Tuple[Candidate, ...]) -> AgentResponse:
        return answers.pop(0)

    world.coordinator.decide = second_fails
    outcome = await world.run()
    assert (outcome.status, outcome.candidates, outcome.wrote) == ("partial", 40, 6)
    assert len(world.coordinator.batches) == 2
    assert world.status(ids[20:40]) == [("confirmed", None, 1)] * 20
    assert world.status(ids[40:]) == [("confirmed", None, 0)] * 5


async def test_a_cancelled_run_still_settles_the_batch_it_sent():
    world = World()
    ids = await world.confirmed(2)
    started = asyncio.Event()

    async def hangs(batch: Tuple[Candidate, ...]) -> AgentResponse:
        started.set()
        await asyncio.sleep(30)
        return answer()

    world.coordinator.decide = hangs
    run = asyncio.create_task(world.run())
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert world.status(ids) == [("confirmed", None, 1)] * 2
