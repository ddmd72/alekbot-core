"""RFC §5.5: before every call after the mark, the run sees chat written since its snapshot."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.llm import MessagePart
from src.domain.turn_clock import CURRENT_TURN_CLOCK, MEANWHILE_HEADER, TurnClock
from src.infrastructure.delegation_engine import DelegationEngine
from src.ports.llm_port import LLMRequest, LLMResponse, Message


def _req():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


async def _run(clock, call_llm):
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        return await DelegationEngine(MagicMock()).execute(
            call_llm=call_llm, base_request=_req(), context={}, max_turns=5,
            terminal_tool="deliver_response", use_turn_clock=True)
    finally:
        CURRENT_TURN_CLOCK.reset(token)


async def test_marked_turn_gets_the_new_messages_once():
    new = [Message(role="user", parts=[MessagePart(text="also check Tuesday")], created_at=50.0)]
    fetch = AsyncMock(side_effect=[new, []])
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10, fetch_since=fetch)
    clock.marked, clock.snapshot_at = True, 10.0
    seen = []

    async def call_llm(req, turn):
        seen.append(req)
        return LLMResponse(text="x")

    await _run(clock, call_llm)
    fetch.assert_awaited_with(10.0)
    texts = [p.text for p in seen[0].messages[-1].parts if p.text]
    assert any(t.startswith(MEANWHILE_HEADER) and "also check Tuesday" in t for t in texts)
    assert clock.seen_until == 50.0


async def test_own_pair_is_excluded():
    own = Message(role="model", parts=[MessagePart(text="working on it")], created_at=60.0)
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10,
                            fetch_since=AsyncMock(return_value=[own]))
    clock.marked, clock.snapshot_at = True, 10.0
    clock.own_created_ats.add(60.0)
    seen = []

    async def call_llm(req, turn):
        seen.append(req)
        return LLMResponse(text="x")

    await _run(clock, call_llm)
    assert not any(MEANWHILE_HEADER in (p.text or "") for p in seen[0].messages[-1].parts)


async def test_not_marked_does_not_fetch():
    fetch = AsyncMock(return_value=[])
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10, fetch_since=fetch)

    async def call_llm(req, turn):
        return LLMResponse(text="x")

    await _run(clock, call_llm)
    fetch.assert_not_awaited()


async def test_fetch_failure_is_skipped():
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10,
                            fetch_since=AsyncMock(side_effect=RuntimeError("firestore down")))
    clock.marked = True

    async def call_llm(req, turn):
        return LLMResponse(text="x")

    assert (await _run(clock, call_llm)).text == "x"
