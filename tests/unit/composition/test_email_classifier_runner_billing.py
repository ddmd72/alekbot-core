"""EmailClassifierRunner billing edges (roadmap 14b, fix round 1): a multi-turn batch is billed once per call, a batch
that ends in an error is billed for the turns it made before the error leaves, and a job without an account runs
unbilled. Same world as test_email_classifier_runner.py; here the model answers from a script."""
import asyncio
import json
from typing import List, Union

import pytest

from src.domain.exceptions import LLMUnavailableError
from src.domain.llm import LLMRequest, LLMResponse, UsageMetadata
from src.utils.telemetry import get_request_context
from tests.unit.composition.test_email_classifier_runner import World, _meta, _Model, _profile

Turn = Union[LLMResponse, BaseException]


def _answer(text: str, prompt_tokens: int, completion_tokens: int) -> LLMResponse:
    return LLMResponse(text=text, usage_metadata=UsageMetadata(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens))


class _Scripted(_Model):
    """Answers the n-th request with the n-th scripted turn (an exception is raised)."""

    def __init__(self, *turns: Turn) -> None:
        super().__init__()
        self.turns: List[Turn] = list(turns)

    async def generate_content(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request.model_copy(deep=True))
        self.users.append(get_request_context()["user_id"])
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        return turn


async def _world(model: _Model) -> World:
    world = World(model, _profile("usr-1", "acc-1"))
    await world.seed()
    return world


async def test_a_re_asked_batch_bills_each_call_once_into_the_jobs_account():
    model = _Scripted(_answer("not json", 1000, 40), _answer(json.dumps([]), 1300, 60))
    world = await _world(model)

    job = await world.index("usr-1", "acc-1")

    assert job.status == "completed" and job.emails_failed == 0
    assert len(model.requests) == 2 and model.turns == []
    usage = world.usage("acc-1")
    assert usage["daily_tokens"] == 1040 + 1360     # both calls, each once
    assert usage["total_requests"] == 1             # one execution, one flush
    await world.store.flush()
    assert [(r.account_id, r.turn, r.total_tokens) for r in world.store.rows] == [("acc-1", 1, 1040),
                                                                                 ("acc-1", 2, 1360)]


async def test_an_error_after_a_billed_turn_bills_that_turn_then_leaves_the_batch():
    # Cancellation is the error that reaches the agent's `finally` unchanged: the batch ends, the turn already made
    # is in the account's ledger, the error goes on to the caller.
    model = _Scripted(_answer("not json", 1000, 40), asyncio.CancelledError())
    world = await _world(model)

    with pytest.raises(asyncio.CancelledError):
        await world.runner.classify_batch([_meta("m1")], "usr-1", account_id="acc-1")

    assert len(model.requests) == 2
    assert world.usage("acc-1")["daily_tokens"] == 1040


async def test_a_provider_outage_mid_batch_bills_the_turns_made_and_propagates_for_a_retry():
    model = _Scripted(_answer("not json", 1000, 40), LLMUnavailableError("503 from the model"))
    world = await _world(model)

    raised = None
    try:
        await world.runner.classify_batch([_meta("m1")], "usr-1", account_id="acc-1")
    except Exception as exc:   # the outcome under test is whether it propagates
        raised = exc
    assert world.usage("acc-1")["daily_tokens"] == 1040
    assert raised is not None


async def test_a_job_without_an_account_is_classified_and_bills_no_account():
    model = _Scripted(_answer(json.dumps([]), 900, 30))
    world = await _world(model)

    results = await world.runner.classify_batch([_meta("m1")], "usr-1", account_id="")

    assert [(r.email_id, r.valuable, r.failure) for r in results] == [("m1", False, None)]
    assert len(model.requests) == 1
    assert world.usage("acc-1").get("daily_tokens", 0) == 0
    assert world.usage("acc-2").get("daily_tokens", 0) == 0
    await world.store.flush()
    assert [(r.user_id, r.account_id or None) for r in world.store.rows] == [("usr-1", None)]
